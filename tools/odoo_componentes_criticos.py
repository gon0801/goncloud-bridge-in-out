#!/usr/bin/env python3
"""Detecta componentes cuyo stock esta bloqueando publicaciones en los canales.

Por que existe
--------------
El catalogo es casi todo kits: 1009 de 1092 SKUs tienen BoM phantom, y detras
de todos ellos hay apenas **65 componentes**. Un kit no tiene stock propio: su
disponibilidad se deriva del componente mas escaso. Asi que un solo componente
en cero arrastra decenas de publicaciones a "sin stock".

Caso real (2026-09-12): 8 componentes en negativo dejaban **94 de 302 SKUs
vendibles en MeLi** reportando 0. Y el diagnostico importaba, porque no era
sobreventa:

    SKU               entradas  salidas  neto  ultima entrada
    EST-CAR-ROJ            158      225   -67  2026-03-01
    ARR-22-PLA-VBU           0        6    -6  NUNCA

`EST-CAR-ROJ` despachaba ese mismo dia. Si sigue saliendo, existe fisicamente:
lo que falta es registrar las entradas en Odoo. Seis meses de compras sin
capturar, y 94 publicaciones apagadas mientras habia mercancia en la bodega.

Que reporta
-----------
Por cada componente con stock bajo Y demanda reciente:

  * stock actual y balance historico entradas/salidas
  * fecha de la ultima entrada registrada (o NUNCA)
  * cuantos SKUs vendibles quedan bloqueados por el

Y lo clasifica para que el orden de atencion sea obvio.

Es de SOLO LECTURA. No escribe nada en Odoo.

Uso
---
    docker exec bridge-api python3 /data/odoo_componentes_criticos.py
    docker exec bridge-api python3 /data/odoo_componentes_criticos.py --umbral 10
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")

# Clasificacion, en orden de urgencia.
NUNCA_CARGADO = "NUNCA_CARGADO"  # 0 entradas historicas: nunca entro a Odoo
NEGATIVO = "NEGATIVO"  # bloqueando publicaciones AHORA
EN_RIESGO = "EN_RIESGO"  # stock bajo y saliendo: cae en dias
SANO = "SANO"


def clasificar_componente(stock: float, entradas: float, salidas_recientes: int) -> str:
    """Como priorizar un componente segun su stock y su movimiento.

    `NUNCA_CARGADO` va primero aunque el faltante sea chico: un componente que
    salio sin haber entrado nunca no es un error de conteo, es un alta que
    falta. Arreglarlo con un ajuste de inventario sin entender eso lo deja
    volver a pasar.
    """
    if entradas <= 0 and salidas_recientes > 0:
        return NUNCA_CARGADO
    if stock < 0:
        return NEGATIVO
    if salidas_recientes > 0 and stock <= 3:
        return EN_RIESGO
    return SANO


def get_setting(key: str) -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "SELECT value FROM bridge_settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else ""


class Odoo:
    def __init__(self):
        self.url = get_setting("odoo_url").rstrip("/")
        self.db = get_setting("odoo_db")
        self.user = get_setting("odoo_user")
        self.pw = get_setting("odoo_password")
        if not all((self.url, self.db, self.user, self.pw)):
            raise SystemExit("ERROR: faltan credenciales de Odoo en bridge_settings")
        self.uid = self._call(
            "common", "authenticate", [self.db, self.user, self.pw, {}]
        )
        if not self.uid:
            raise SystemExit("ERROR: no se pudo autenticar contra Odoo")

    def _call(self, service, method, args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": 1,
            "params": {"service": service, "method": method, "args": args},
        }
        req = urllib.request.Request(
            self.url + "/jsonrpc",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        raw = json.loads(urllib.request.urlopen(req, timeout=120).read())
        if "error" in raw:
            raise SystemExit(f"ERROR Odoo: {json.dumps(raw['error'])[:300]}")
        return raw.get("result")

    def kw(self, model, method, args, kwargs=None):
        return self._call(
            "object",
            "execute_kw",
            [self.db, self.uid, self.pw, model, method, args, kwargs or {}],
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Componentes que bloquean publicaciones")
    ap.add_argument(
        "--umbral", type=float, default=3, help="stock <= umbral (default 3)"
    )
    ap.add_argument(
        "--dias", type=int, default=90, help="ventana de demanda (default 90)"
    )
    ap.add_argument("--json", action="store_true", help="salida JSON")
    args = ap.parse_args()

    od = Odoo()
    desde = (datetime.now(timezone.utc) - timedelta(days=args.dias)).strftime(
        "%Y-%m-%d"
    )

    lineas = od.kw(
        "mrp.bom.line", "search_read", [[]], {"fields": ["product_id"], "limit": 20000}
    )
    comp_ids = sorted({ln["product_id"][0] for ln in (lineas or [])})
    if not comp_ids:
        print("No se encontraron componentes en lineas de BoM.")
        return 0

    comps = od.kw(
        "product.product",
        "read",
        [comp_ids],
        {"fields": ["default_code", "qty_available"]},
    )

    hallazgos = []
    for c in comps or []:
        stock = c.get("qty_available") or 0
        if stock > args.umbral:
            continue

        salidas_rec = od.kw(
            "stock.move.line",
            "search_count",
            [
                [
                    ["product_id", "=", c["id"]],
                    ["state", "=", "done"],
                    ["date", ">=", desde],
                    ["location_id.usage", "=", "internal"],
                    ["location_dest_id.usage", "!=", "internal"],
                ]
            ],
        )
        if not salidas_rec:
            continue

        ent = (
            od.kw(
                "stock.move.line",
                "search_read",
                [
                    [
                        ["product_id", "=", c["id"]],
                        ["state", "=", "done"],
                        ["location_dest_id.usage", "=", "internal"],
                        ["location_id.usage", "!=", "internal"],
                    ]
                ],
                {"fields": ["quantity", "date"], "order": "date desc", "limit": 2000},
            )
            or []
        )
        entradas = sum(x["quantity"] for x in ent)

        # Cuantos SKUs vendibles dependen de este componente
        usos = (
            od.kw(
                "mrp.bom.line",
                "search_read",
                [[["product_id", "=", c["id"]]]],
                {"fields": ["bom_id"], "limit": 3000},
            )
            or []
        )
        bom_ids = list({u["bom_id"][0] for u in usos})
        boms = (
            od.kw("mrp.bom", "read", [bom_ids], {"fields": ["product_id"]})
            if bom_ids
            else []
        )
        kit_ids = [b["product_id"][0] for b in (boms or []) if b.get("product_id")]
        vendibles = []
        if kit_ids:
            vendibles = (
                od.kw(
                    "product.product",
                    "search",
                    [
                        [
                            ["id", "in", kit_ids],
                            ["active", "=", True],
                            ["sell_on_meli", "=", True],
                        ]
                    ],
                )
                or []
            )
        bloqueados = len(vendibles)

        hallazgos.append(
            {
                "sku": c.get("default_code") or f"(id {c['id']})",
                "stock": stock,
                "entradas": entradas,
                "salidas_recientes": salidas_rec,
                "ultima_entrada": ent[0]["date"][:10] if ent else None,
                "skus_bloqueados": bloqueados,
                "_kit_ids": vendibles,
                "clase": clasificar_componente(stock, entradas, salidas_rec),
            }
        )

    orden = {NUNCA_CARGADO: 0, NEGATIVO: 1, EN_RIESGO: 2, SANO: 3}
    hallazgos.sort(key=lambda h: (orden[h["clase"]], h["stock"]))

    if args.json:
        limpio = [{k: v for k, v in h.items() if k != "_kit_ids"} for h in hallazgos]
        print(json.dumps(limpio, indent=2, ensure_ascii=False))
        return 0

    if not hallazgos:
        print(
            f"Sin componentes con stock <= {args.umbral} y demanda en {args.dias} dias."
        )
        return 0

    print(f"Componentes con stock <= {args.umbral} y salidas en {args.dias} dias\n")
    print(
        "%-22s %8s %9s %9s %-12s %s"
        % (
            "COMPONENTE",
            "STOCK",
            "ENTRADAS",
            "SALIDAS",
            "ULT.ENTRADA",
            "SKUs BLOQUEADOS",
        )
    )
    for h in hallazgos:
        print(
            "%-22s %8.0f %9.0f %9d %-12s %d   [%s]"
            % (
                h["sku"],
                h["stock"],
                h["entradas"],
                h["salidas_recientes"],
                h["ultima_entrada"] or "NUNCA",
                h["skus_bloqueados"],
                h["clase"],
            )
        )

    # Union, no suma: un mismo kit puede depender de varios componentes en
    # falta. Sumar por componente inflaba el numero (107 en vez de 94 el
    # 2026-09-12), y un numero inflado no sirve para decidir.
    afectados = set()
    for h in hallazgos:
        if h["clase"] in (NEGATIVO, NUNCA_CARGADO):
            afectados.update(h["_kit_ids"])
    print(
        f"\nSKUs vendibles distintos afectados por componentes en falta: {len(afectados)}"
    )
    print("Un conteo fisico de estos componentes + ajuste de inventario los reactiva.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
