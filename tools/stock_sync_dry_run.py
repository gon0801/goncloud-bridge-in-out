"""Dry-run del envio de stock Odoo -> MeLi y Amazon: que se manda ahora y antes.

No escribe nada: lee Odoo por XML-RPC, abre bridge.db en solo lectura y no
llama a MeLi ni a Amazon. Por cada codigo muestra la cantidad y el destino
con la logica vieja (MeLi qty_available; Amazon un solo seller SKU, siempre
a MX) y con la actual (stock_disponible.disponible y worker.amazon_targets).

Uso, dentro de bridge-api con ODOO_* en el entorno:
    python3 tools/stock_sync_dry_run.py [--muestra 15] [--csv-us cambios.csv]

--csv-us escribe las publicaciones FBM de Amazon US cuya cantidad cambiaria
respecto a lo que lista el reporte de Amazon de hoy. Las filas que el
reporte ya no trae (fetched_at de un dia anterior al ultimo reporte de su
marketplace) no entran: amazon-prices-sync las borra en su siguiente corrida.
El titulo es el item-name del reporte; como Amazon casi siempre lo manda
vacio, cae al nombre del producto en Odoo.
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import defusedxml.xmlrpc as _defusedxml_xmlrpc

_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import worker  # noqa: E402
from stock_disponible import CAMPOS_ODOO, disponible  # noqa: E402

DB = os.getenv("BRIDGE_DB", "/data/bridge.db")
MX = "A1AM78C64UM0Y8"


def odoo():
    url, db = os.environ["ODOO_URL"], os.environ["ODOO_DB"]
    pwd = os.environ["ODOO_PASSWORD"]
    uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(
        db, os.environ["ODOO_USER"], pwd, {}
    )
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    def ejecutar(modelo, metodo, args, kw=None):
        return models.execute_kw(db, uid, pwd, modelo, metodo, args, kw or {})

    return ejecutar


def productos_odoo(ejecutar) -> list[dict]:
    return ejecutar(
        "product.product",
        "search_read",
        [
            [
                ("active", "=", True),
                ("default_code", "!=", False),
                "|",
                ("sell_on_meli", "=", True),
                ("sell_on_amazon_fbm", "=", True),
            ]
        ],
        {
            "fields": [
                "default_code",
                "name",
                "product_tmpl_id",
                "sell_on_meli",
                "sell_on_amazon_fbm",
                *CAMPOS_ODOO,
            ]
        },
    )


def kits_sobrestimados(ejecutar, productos: list[dict]) -> list[dict]:
    """Kits phantom cuyo disponible de Odoo supera al de sus componentes.

    Para un kit Odoo calcula qty_available y outgoing_qty cada uno como el
    minimo sobre los componentes, asi que disponible() da
    min(fisico_i) - min(salidas_i), que puede superar min(fisico_i - salidas_i)
    cuando un componente tambien sale por otra venta.
    """
    boms = ejecutar(
        "mrp.bom",
        "search_read",
        [[("type", "=", "phantom"), ("active", "=", True)]],
        {"fields": ["product_tmpl_id", "product_id", "product_qty", "bom_line_ids"]},
    )
    por_producto, por_plantilla = {}, {}
    for b in boms:
        if b["product_id"]:
            por_producto.setdefault(b["product_id"][0], b)
        else:
            por_plantilla.setdefault(b["product_tmpl_id"][0], b)
    kits = {}
    for p in productos:
        bom = por_producto.get(p["id"]) or por_plantilla.get(p["product_tmpl_id"][0])
        if bom:
            kits[p["id"]] = (p, bom)
    lineas = {
        line["id"]: line
        for line in ejecutar(
            "mrp.bom.line",
            "read",
            [sorted({i for _, b in kits.values() for i in b["bom_line_ids"]})],
            {"fields": ["product_id", "product_qty"]},
        )
    }
    componentes = {
        c["id"]: c
        for c in ejecutar(
            "product.product",
            "read",
            [sorted({line["product_id"][0] for line in lineas.values()})],
            {"fields": ["default_code", *CAMPOS_ODOO]},
        )
    }
    salida = []
    for p, bom in kits.values():
        por_kit = [
            (lineas[i]["product_id"][0], lineas[i]["product_qty"] / bom["product_qty"])
            for i in bom["bom_line_ids"]
            if lineas[i]["product_qty"]
        ]
        if not por_kit:
            continue
        por_componentes = min(int(disponible(componentes[c]) // q) for c, q in por_kit)
        if disponible(p) > por_componentes:
            salida.append(
                {
                    "sku": p["default_code"],
                    "disponible_odoo": disponible(p),
                    "disponible_por_componentes": por_componentes,
                }
            )
    return salida


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--muestra", type=int, default=15)
    ap.add_argument("--csv-us")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    meli_mapeados = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT sku, COUNT(*) FROM sku_mapping WHERE channel='meli' GROUP BY sku"
        )
    }
    ultimo_reporte = dict(
        conn.execute(
            "SELECT marketplace_id, MAX(fetched_at) FROM amazon_listing_prices "
            "GROUP BY marketplace_id"
        )
    )
    publicacion = {
        (s, m): {
            "qty": q,
            "asin": asin,
            "titulo": (titulo or "")[:70],
            "en_reporte_de_hoy": (f or "")[:10] >= (ultimo_reporte[m] or "")[:10],
        }
        for s, m, q, asin, titulo, f in conn.execute(
            "SELECT seller_sku, marketplace_id, quantity, asin, item_name, "
            "fetched_at FROM amazon_listing_prices"
        )
    }

    ejecutar = odoo()
    productos = productos_odoo(ejecutar)
    meli_filas, amazon_filas = [], []
    for p in productos:
        code = p["default_code"].strip()
        ahora = disponible(p)
        if p["sell_on_meli"] and code in meli_mapeados:
            antes = max(int(p.get("qty_available") or 0), 0)
            meli_filas.append(
                {
                    "sku": code,
                    "publicaciones": meli_mapeados[code],
                    "qty_antes": antes,
                    "qty_ahora": ahora,
                    "outgoing_qty": p.get("outgoing_qty"),
                }
            )
        if p["sell_on_amazon_fbm"]:
            viejo = conn.execute(
                "SELECT seller_sku FROM amazon_sku_mapping WHERE odoo_default_code=?",
                (code,),
            ).fetchone()
            destinos = []
            for t in worker.amazon_targets(conn, code):
                pub = publicacion.get((t.seller_sku, t.marketplace_id), {})
                destinos.append(
                    {
                        "seller_sku": t.seller_sku,
                        "marketplace": t.market,
                        "envia": t.skip_reason is None,
                        "motivo_omitido": t.skip_reason,
                        "qty_listada_hoy": pub.get("qty"),
                        "asin": pub.get("asin"),
                        "titulo": pub.get("titulo") or (p.get("name") or "")[:70],
                        "en_reporte_de_hoy": pub.get("en_reporte_de_hoy", False),
                        "odoo_sku": code,
                        "qty": ahora,
                    }
                )
            if viejo or destinos:
                amazon_filas.append(
                    {
                        "sku": code,
                        "antes": viejo
                        and {"seller_sku": viejo[0], "marketplace": "MX", "qty": ahora},
                        "antes_canal_real": viejo
                        and conn.execute(
                            "SELECT fulfillment_channel FROM amazon_listing_prices "
                            "WHERE seller_sku=? AND marketplace_id=?",
                            (viejo[0], MX),
                        ).fetchone(),
                        "ahora": destinos,
                    }
                )

    meli_cambian = [f for f in meli_filas if f["qty_antes"] != f["qty_ahora"]]
    print("== MeLi: muestra de SKUs cuya cantidad cambia ==")
    for f in sorted(meli_cambian, key=lambda f: f["qty_ahora"] - f["qty_antes"])[
        : args.muestra
    ]:
        print(json.dumps(f, ensure_ascii=False))

    multi = [f for f in amazon_filas if sum(d["envia"] for d in f["ahora"]) > 1]
    con_us = [
        f
        for f in amazon_filas
        if any(d["envia"] and d["marketplace"] == "US" for d in f["ahora"])
    ]
    print("== Amazon: muestra de codigos con varias publicaciones FBM ==")
    for f in multi[: args.muestra]:
        print(json.dumps(f, ensure_ascii=False))
    print("== Amazon: muestra de codigos con publicacion FBM en US ==")
    for f in con_us[: args.muestra]:
        print(json.dumps(f, ensure_ascii=False))

    envios = [d for f in amazon_filas for d in f["ahora"] if d["envia"]]
    viejas = [d for d in envios if not d["en_reporte_de_hoy"]]
    envios = [d for d in envios if d["en_reporte_de_hoy"]]
    us = [d for d in envios if d["marketplace"] == "US"]
    us_cambia = [d for d in us if d["qty_listada_hoy"] != d["qty"]]
    if args.csv_us:
        with open(args.csv_us, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "seller_sku",
                    "asin",
                    "titulo",
                    "qty_listada_hoy",
                    "qty_nueva",
                    "odoo_sku",
                ]
            )
            for d in sorted(us_cambia, key=lambda d: (d["odoo_sku"], d["seller_sku"])):
                w.writerow(
                    [
                        d["seller_sku"],
                        d["asin"],
                        d["titulo"],
                        "" if d["qty_listada_hoy"] is None else d["qty_listada_hoy"],
                        d["qty"],
                        d["odoo_sku"],
                    ]
                )
    kits = kits_sobrestimados(ejecutar, productos)
    print("== Kits phantom con disponible de Odoo mayor al de sus componentes ==")
    for k in kits[: args.muestra]:
        print(json.dumps(k, ensure_ascii=False))
    antes_por_canal = Counter(
        (f["antes_canal_real"] or ["SIN_PUBLICACION_MX"])[0]
        for f in amazon_filas
        if f["antes"]
    )
    resumen = {
        "meli_skus": len(meli_filas),
        "meli_skus_que_cambian": len(meli_cambian),
        "meli_unidades_que_se_dejan_de_ofrecer": sum(
            f["qty_antes"] - f["qty_ahora"] for f in meli_cambian
        ),
        "amazon_codigos": len(amazon_filas),
        "amazon_patches_antes": sum(1 for f in amazon_filas if f["antes"]),
        "amazon_patches_antes_por_canal_real_en_mx": dict(antes_por_canal),
        "amazon_patches_ahora": len(envios),
        "amazon_patches_ahora_por_marketplace": dict(
            Counter(d["marketplace"] for d in envios)
        ),
        "amazon_patches_a_filas_que_ya_no_vienen_en_el_reporte": len(viejas),
        "amazon_omitidos_por_motivo": dict(
            Counter(
                d["motivo_omitido"]
                for f in amazon_filas
                for d in f["ahora"]
                if not d["envia"]
            )
        ),
        "amazon_us_publicaciones_que_cambian_vs_listado_hoy": len(us_cambia),
        "amazon_us_de_0_a_mayor": sum(
            1 for d in us_cambia if not d["qty_listada_hoy"] and d["qty"] > 0
        ),
        "amazon_us_de_mayor_a_0": sum(
            1 for d in us_cambia if (d["qty_listada_hoy"] or 0) > 0 and d["qty"] == 0
        ),
        "amazon_us_unidades_listadas_hoy": sum(d["qty_listada_hoy"] or 0 for d in us),
        "amazon_us_unidades_ahora": sum(d["qty"] for d in us),
        "kits_phantom_sobrestimados": len(kits),
    }
    print("== Resumen ==")
    print(json.dumps(resumen, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
