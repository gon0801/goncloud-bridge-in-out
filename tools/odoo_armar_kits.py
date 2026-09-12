#!/usr/bin/env python3
"""Arma kits y los deja en un almacen de canal (Meli-Full, FBA-MX, FBA-US).

Que resuelve
------------
Segundo paso del modelo hibrido de PENDIENTES #3. El primero
(`odoo_migrar_boms_picking_type.py`) hizo que las recetas apliquen SOLO en la
entrega de EHV-MX, para que FULL y FBA muevan el kit como unidad armada.

Pero faltaba algo: **un producto con receta phantom no acumula stock propio**.
Los kits existen solo como derivacion de sus componentes, asi que no hay forma
directa de decir "hay 40 kits en la bodega de MeLi".

Esto lo resuelve moviendo por `Virtual Locations/Production`:

    componentes:  EHV/Stock            -> Virtual Locations/Production
    kit:          Virtual Locations/Production -> Full|FBAMX|FBAUS /Stock

Es lo que hace una orden de fabricacion, pero sin necesitar recetas nuevas. Y a
diferencia de dos ajustes de inventario sueltos, la valuacion queda coherente:
el costo sale de los componentes y entra en el kit, sin inventar ni destruir
valor.

Salvaguardas
------------
* Simula por defecto. Escribe solo con `--aplicar`.
* Exige que el kit tenga EXACTAMENTE una receta activa. Esta base tiene 1541
  recetas archivadas contra 1009 vivas; agarrar la equivocada armaria el kit
  con los componentes de otra version.
* Verifica que TODOS los componentes alcancen antes de mover uno solo. Un
  armado a medias deja componentes consumidos sin kit que los justifique.
* Imprime el plan completo — que sale, que entra, en que ubicacion — antes de
  tocar nada.

Uso
---
    docker exec bridge-api python3 /data/odoo_armar_kits.py \\
        --sku SET-ARR-COF-22-VCO-DOR --cantidad 20 --destino Full

    # y con --aplicar para ejecutarlo
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
LOC_PRODUCCION = "Virtual Locations/Production"
DESTINOS = {"Full": "Full/Stock", "FBAMX": "FBAMX/Stock", "FBAUS": "FBAUS/Stock"}
ORIGEN = "EHV/Stock"


def plan_de_componentes(lineas_bom: list, cantidad_kits: int) -> list:
    """Cuanto de cada componente hace falta para armar `cantidad_kits`.

    Devuelve `[{"product_id", "nombre", "necesario"}]`.

    La receta expresa cantidades para UN kit (`product_qty` por linea); se
    multiplica. Se redondea hacia arriba nunca: si una receta pidiera 0.5 de
    algo, armar 3 kits pide 1.5 y el faltante debe verse como tal, no
    esconderse en un redondeo.
    """
    plan = []
    for ln in lineas_bom:
        pid = ln["product_id"]
        plan.append(
            {
                "product_id": pid[0] if isinstance(pid, (list, tuple)) else pid,
                "nombre": pid[1] if isinstance(pid, (list, tuple)) else str(pid),
                "necesario": (ln.get("product_qty") or 0) * cantidad_kits,
            }
        )
    return plan


def faltantes(plan: list, disponible: dict) -> list:
    """Componentes que no alcanzan. Lista vacia = se puede armar."""
    out = []
    for p in plan:
        hay = disponible.get(p["product_id"], 0)
        if hay < p["necesario"]:
            out.append({**p, "hay": hay, "falta": p["necesario"] - hay})
    return out


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
        raw = json.loads(urllib.request.urlopen(req, timeout=180).read())
        if "error" in raw:
            raise SystemExit(f"ERROR Odoo: {json.dumps(raw['error'])[:400]}")
        return raw.get("result")

    def kw(self, model, method, args, kwargs=None):
        return self._call(
            "object",
            "execute_kw",
            [self.db, self.uid, self.pw, model, method, args, kwargs or {}],
        )

    def loc(self, nombre):
        r = self.kw(
            "stock.location",
            "search_read",
            [[["complete_name", "=", nombre]]],
            {"fields": ["id"], "limit": 1},
        )
        if not r:
            raise SystemExit(f"ERROR: no existe la ubicacion '{nombre}'")
        return r[0]["id"]


def mover(od, product_id, cantidad, origen, destino, nombre):
    mid = od.kw(
        "stock.move",
        "create",
        [
            {
                "name": nombre,
                "product_id": product_id,
                "product_uom_qty": cantidad,
                "location_id": origen,
                "location_dest_id": destino,
            }
        ],
    )
    od.kw("stock.move", "_action_confirm", [[mid]])
    od.kw("stock.move", "_action_assign", [[mid]])
    od.kw("stock.move", "write", [[mid], {"quantity": cantidad, "picked": True}])
    od.kw("stock.move", "_action_done", [[mid]])
    return mid


def main() -> int:
    ap = argparse.ArgumentParser(description="Arma kits hacia un almacen de canal")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--cantidad", type=int, required=True)
    ap.add_argument("--destino", required=True, choices=sorted(DESTINOS))
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()

    if args.cantidad <= 0:
        raise SystemExit("ERROR: --cantidad debe ser > 0")

    od = Odoo()
    prods = (
        od.kw(
            "product.product",
            "search_read",
            [[["default_code", "=", args.sku]]],
            {"fields": ["id", "default_code", "qty_available"]},
        )
        or []
    )
    if len(prods) != 1:
        raise SystemExit(
            f"ERROR: se esperaba 1 producto con SKU '{args.sku}', hay {len(prods)}"
        )
    kit = prods[0]

    boms = (
        od.kw(
            "mrp.bom",
            "search_read",
            [[["product_id", "=", kit["id"]], ["active", "=", True]]],
            {"fields": ["id", "bom_line_ids", "product_qty"]},
        )
        or []
    )
    if len(boms) != 1:
        raise SystemExit(
            f"ERROR: el kit tiene {len(boms)} recetas activas, se esperaba 1. "
            f"Con varias no se puede saber cual usar."
        )
    bom = boms[0]

    lineas = (
        od.kw(
            "mrp.bom.line",
            "read",
            [bom["bom_line_ids"]],
            {"fields": ["product_id", "product_qty"]},
        )
        or []
    )
    plan = plan_de_componentes(lineas, args.cantidad)

    loc_origen = od.loc(ORIGEN)
    ids = [p["product_id"] for p in plan]
    quants = (
        od.kw(
            "stock.quant",
            "search_read",
            [[["product_id", "in", ids], ["location_id", "child_of", loc_origen]]],
            {"fields": ["product_id", "quantity", "reserved_quantity"]},
        )
        or []
    )
    disponible = {}
    for q in quants:
        pid = q["product_id"][0]
        disponible[pid] = (
            disponible.get(pid, 0)
            + (q["quantity"] or 0)
            - (q["reserved_quantity"] or 0)
        )

    print(
        f"Armar {args.cantidad} x {kit['default_code']}  ->  {DESTINOS[args.destino]}"
    )
    print(f"Disponibilidad actual del kit: {kit['qty_available']}\n")
    print("%-40s %9s %9s" % ("COMPONENTE", "NECESARIO", "HAY EN EHV"))
    for p in plan:
        print(
            "%-40s %9.1f %9.1f"
            % (p["nombre"][:40], p["necesario"], disponible.get(p["product_id"], 0))
        )

    falta = faltantes(plan, disponible)
    if falta:
        print("\nNO SE PUEDE ARMAR. Faltan:")
        for f in falta:
            print("   %-40s faltan %.1f" % (f["nombre"][:40], f["falta"]))
        return 1

    if not args.aplicar:
        print("\nSIMULACION. Con --aplicar se ejecutan los movimientos.")
        return 0

    loc_prod = od.loc(LOC_PRODUCCION)
    loc_destino = od.loc(DESTINOS[args.destino])
    etiqueta = f"Armado {args.sku} x{args.cantidad}"

    movs = []
    for p in plan:
        movs.append(
            mover(
                od,
                p["product_id"],
                p["necesario"],
                loc_origen,
                loc_prod,
                f"{etiqueta}: consumo {p['nombre'][:30]}",
            )
        )
    movs.append(
        mover(
            od,
            kit["id"],
            args.cantidad,
            loc_prod,
            loc_destino,
            f"{etiqueta}: alta en {args.destino}",
        )
    )

    despues = od.kw(
        "product.product", "read", [[kit["id"]]], {"fields": ["qty_available"]}
    )[0]
    print(f"\nListo. {len(movs)} movimientos: {movs}")
    print(
        f"Disponibilidad del kit: {kit['qty_available']} -> {despues['qty_available']}"
    )
    print(
        "Para deshacer, revierte esos movimientos en Odoo (Inventario > Movimientos)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
