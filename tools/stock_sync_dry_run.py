"""Dry-run del envio de stock Odoo -> MeLi y Amazon: que se manda ahora y antes.

No escribe nada: lee Odoo por XML-RPC, abre bridge.db en solo lectura y no
llama a MeLi ni a Amazon. Por cada codigo muestra la cantidad y el destino
con la logica vieja (MeLi qty_available; Amazon un solo seller SKU, siempre
a MX) y con la actual (stock_disponible.disponible y worker.amazon_targets).

Uso, dentro de bridge-api con ODOO_* en el entorno:
    python3 tools/stock_sync_dry_run.py [--muestra 15]
"""

import argparse
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
MX = worker.AMAZON_MARKETPLACES[11][1]


def productos_odoo() -> list[dict]:
    url, db = os.environ["ODOO_URL"], os.environ["ODOO_DB"]
    pwd = os.environ["ODOO_PASSWORD"]
    uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(
        db, os.environ["ODOO_USER"], pwd, {}
    )
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return models.execute_kw(
        db,
        uid,
        pwd,
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
                "sell_on_meli",
                "sell_on_amazon_fbm",
                *CAMPOS_ODOO,
            ]
        },
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--muestra", type=int, default=15)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    meli_mapeados = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT sku, COUNT(*) FROM sku_mapping WHERE channel='meli' GROUP BY sku"
        )
    }
    qty_listada = {
        (s, m): q
        for s, m, q in conn.execute(
            "SELECT seller_sku, marketplace_id, quantity FROM amazon_listing_prices"
        )
    }

    meli_filas, amazon_filas = [], []
    for p in productos_odoo():
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
            destinos = [
                {
                    "seller_sku": t.seller_sku,
                    "marketplace": t.market,
                    "envia": t.skip_reason is None,
                    "motivo_omitido": t.skip_reason,
                    "qty_listada_hoy": qty_listada.get(
                        (t.seller_sku, t.marketplace_id)
                    ),
                    "qty": ahora,
                }
                for t in worker.amazon_targets(conn, code)
            ]
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
    us = [d for d in envios if d["marketplace"] == "US"]
    us_cambia = [d for d in us if d["qty_listada_hoy"] != d["qty"]]
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
    }
    print("== Resumen ==")
    print(json.dumps(resumen, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
