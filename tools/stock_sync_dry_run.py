"""Dry-run del stock Odoo -> MeLi y Amazon FBM: lo que se publica hoy y con esta version.

No escribe nada: lee Odoo por XML-RPC con el mismo contexto de almacenes que
los snapshotters, abre bridge.db en solo lectura y no llama a MeLi ni a
Amazon. Por cada publicacion compara la cantidad de hoy (qty_available menos
outgoing_qty del producto; en un kit, cada campo es el minimo de Odoo sobre
sus componentes) con la de `stock_disponible.disponibles`, que calcula el kit
desde lo libre de cada componente.

Una publicacion es un mapeo MeLi del codigo o un destino FBM de Amazon que
el worker manda (worker.amazon_targets sin motivo para omitirlo). Sale 1 si
alguna publicacion sube: el calculo por componente nunca da mas que el de hoy.

Uso, dentro de bridge-api con ODOO_* en el entorno:
    python3 tools/stock_sync_dry_run.py [--muestra 15] [--codigos SET-ITA-,PERS-ITA-]
        [--csv cambios.csv]
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
from stock_disponible import (  # noqa: E402
    CAMPOS_ALMACEN,
    CAMPOS_ODOO,
    almacen_apartado,
    contexto_publicable,
    disponible,
    disponibles,
)

DB = os.getenv("BRIDGE_DB", "/data/bridge.db")


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


def publicaciones(conn, p: dict) -> list[str]:
    code = p["default_code"].strip()
    salida = []
    if p["sell_on_meli"]:
        salida += [
            f"meli:{item}"
            for (item,) in conn.execute(
                "SELECT remote_item_id || ':' || remote_variation_id FROM sku_mapping "
                "WHERE channel='meli' AND sku=?",
                (code,),
            )
        ]
    if p["sell_on_amazon_fbm"]:
        salida += [
            f"amazon_{t.market}:{t.seller_sku}"
            for t in worker.amazon_targets(conn, code)
            if t.skip_reason is None
        ]
    return salida


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--muestra", type=int, default=15)
    ap.add_argument("--codigos", default="", help="prefijos que se listan completos")
    ap.add_argument("--csv")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ejecutar = odoo()
    apartado = almacen_apartado(conn)
    almacenes = (
        ejecutar("stock.warehouse", "search_read", [[]], {"fields": CAMPOS_ALMACEN})
        if apartado
        else []
    )
    contexto = contexto_publicable(almacenes, apartado)
    opciones = {
        "fields": ["default_code", "sell_on_meli", "sell_on_amazon_fbm", *CAMPOS_ODOO]
    }
    if contexto:
        opciones["context"] = contexto
    productos = ejecutar(
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
        opciones,
    )
    libres = disponibles(ejecutar, productos, contexto)

    filas = []
    for p in productos:
        pubs = publicaciones(conn, p)
        if pubs:
            filas.append(
                {
                    "sku": p["default_code"].strip(),
                    "hoy": disponible(p),
                    "nuevo": libres[p["id"]],
                    "publicaciones": pubs,
                }
            )
    cambian = [f for f in filas if f["hoy"] != f["nuevo"]]
    suben = [f for f in cambian if f["nuevo"] > f["hoy"]]
    prefijos = tuple(x for x in args.codigos.split(",") if x)

    def linea(f):
        return f"{f['sku']}: {f['hoy']} -> {f['nuevo']} ({len(f['publicaciones'])} pub)"

    print(f"== Muestra de los que mas bajan (contexto {contexto or 'todos'}) ==")
    for f in sorted(cambian, key=lambda f: f["nuevo"] - f["hoy"])[: args.muestra]:
        print(linea(f))
    if prefijos:
        print(f"== Todos los {', '.join(prefijos)} ==")
        for f in sorted(filas, key=lambda f: f["sku"]):
            if f["sku"].startswith(prefijos):
                print(linea(f))
    for f in suben:
        print("SUBE " + linea(f))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["sku", "publicacion", "qty_hoy", "qty_nueva"])
            for f in sorted(cambian, key=lambda f: f["sku"]):
                for pub in f["publicaciones"]:
                    w.writerow([f["sku"], pub, f["hoy"], f["nuevo"]])

    pubs = [(f, pub) for f in filas for pub in f["publicaciones"]]
    pubs_cambian = [(f, pub) for f, pub in pubs if f["hoy"] != f["nuevo"]]
    resumen = {
        "contexto": contexto,
        "skus_publicados": len(filas),
        "skus_que_cambian": len(cambian),
        "skus_que_suben": len(suben),
        "publicaciones": len(pubs),
        "publicaciones_que_cambian": len(pubs_cambian),
        "publicaciones_que_cambian_por_canal": dict(
            Counter(pub.split(":")[0] for _, pub in pubs_cambian)
        ),
        "publicaciones_que_cambian_por_familia": dict(
            Counter(f["sku"].split("-")[0] for f, _ in pubs_cambian)
        ),
        "publicaciones_que_quedan_en_0": sum(
            1 for f, _ in pubs_cambian if f["nuevo"] == 0
        ),
        "unidades_publicadas_hoy": sum(f["hoy"] for f, _ in pubs),
        "unidades_publicadas_nuevo": sum(f["nuevo"] for f, _ in pubs),
    }
    print("== Resumen ==")
    print(json.dumps(resumen, ensure_ascii=False, indent=1))
    return 1 if suben else 0


if __name__ == "__main__":
    sys.exit(main())
