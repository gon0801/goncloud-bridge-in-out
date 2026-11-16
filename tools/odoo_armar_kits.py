#!/usr/bin/env python3
"""Arma kits y los deja en un almacen de canal (Meli-Full, FBA-MX, FBA-US, Flex-MX).

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
    kit:          Virtual Locations/Production -> Full|FBAMX|FBAUS|FLEX /Stock

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
* Cada movimiento lleva en `origin` el folio del armado. Odoo no da una
  transaccion que abarque varios movimientos, asi que el folio es el registro
  de lo hecho: repetir el comando con el mismo folio termina un movimiento a
  medias y crea solo los que faltan, sin duplicar.
* `--desarmar` es la reversa: el kit sale del destino y los componentes
  vuelven a EHV/Stock. Saca primero el kit, para que un corte nunca deje kits
  sin los componentes que los respaldan.

Uso
---
    docker exec bridge-api python3 /data/odoo_armar_kits.py \\
        --sku SET-ARR-COF-22-VCO-DOR --cantidad 20 --destino Full

    # y con --aplicar --folio <clave unica> para ejecutarlo; si se corta,
    # el mismo comando lo reanuda. La reversa, con su propio folio:
    ... --destino FLEX --desarmar --aplicar --folio <clave>
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass, replace

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
LOC_PRODUCCION = "Virtual Locations/Production"
DESTINOS = {
    "Full": "Full/Stock",
    "FBAMX": "FBAMX/Stock",
    "FBAUS": "FBAUS/Stock",
    "FLEX": "FLEX/Stock",
}
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


@dataclass(frozen=True)
class Paso:
    product_id: int
    nombre: str
    cantidad: float
    origen: int
    destino: int


def pasos(plan, kit, cantidad, loc_ehv, loc_prod, loc_canal, desarmar=False):
    """Movimientos en orden. Armar consume los componentes y despues da de
    alta el kit; desarmar invierte cada paso y el orden."""
    consumo = [
        Paso(p["product_id"], p["nombre"], p["necesario"], loc_ehv, loc_prod)
        for p in plan
    ]
    alta = Paso(kit["id"], kit["default_code"], cantidad, loc_prod, loc_canal)
    armado = [*consumo, alta]
    if not desarmar:
        return armado
    return [replace(p, origen=p.destino, destino=p.origen) for p in reversed(armado)]


def estado_de(paso: Paso, movs: list) -> tuple:
    """("hecho" | "a medias" | "pendiente", id del move) segun los movimientos
    del folio. Un folio reusado con otra cantidad aborta."""
    propios = [
        m
        for m in movs
        if m["product_id"][0] == paso.product_id
        and m["location_id"][0] == paso.origen
        and m["location_dest_id"][0] == paso.destino
        and m["state"] != "cancel"
    ]
    if not propios:
        return "pendiente", None
    if len(propios) > 1:
        ids = [m["id"] for m in propios]
        raise SystemExit(f"ERROR: el folio tiene {len(propios)} movimientos de {paso.nombre}: {ids}")
    m = propios[0]
    if abs(m["product_uom_qty"] - paso.cantidad) > 1e-6:
        raise SystemExit(
            f"ERROR: el folio ya movio {m['product_uom_qty']:g} de {paso.nombre} y este "
            f"comando pide {paso.cantidad:g}; usa otro folio"
        )
    return ("hecho" if m["state"] == "done" else "a medias"), m["id"]


def disponible_en(od, ids: list, loc: int) -> dict:
    quants = (
        od.kw(
            "stock.quant",
            "search_read",
            [[["product_id", "in", ids], ["location_id", "child_of", loc]]],
            {"fields": ["product_id", "quantity", "reserved_quantity"]},
        )
        or []
    )
    disponible: dict = {}
    for q in quants:
        pid = q["product_id"][0]
        disponible[pid] = (
            disponible.get(pid, 0)
            + (q["quantity"] or 0)
            - (q["reserved_quantity"] or 0)
        )
    return disponible


def crear(od, paso: Paso, origin: str, nombre: str) -> int:
    return od.kw(
        "stock.move",
        "create",
        [
            {
                "name": nombre,
                "origin": origin,
                "product_id": paso.product_id,
                "product_uom_qty": paso.cantidad,
                "location_id": paso.origen,
                "location_dest_id": paso.destino,
            }
        ],
    )


def completar(od, mid: int, cantidad: float) -> None:
    """Lleva a done un move en cualquier estado previo."""
    estado = od.kw("stock.move", "read", [[mid]], {"fields": ["state"]})[0]["state"]
    if estado == "draft":
        od.kw("stock.move", "_action_confirm", [[mid]])
    od.kw("stock.move", "_action_assign", [[mid]])
    od.kw("stock.move", "write", [[mid], {"quantity": cantidad, "picked": True}])
    od.kw("stock.move", "_action_done", [[mid]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Arma kits hacia un almacen de canal")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--cantidad", type=int, required=True)
    ap.add_argument("--destino", required=True, choices=sorted(DESTINOS))
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument(
        "--folio",
        help="clave unica del armado; repetir con el mismo folio reanuda sin duplicar",
    )
    ap.add_argument(
        "--desarmar",
        action="store_true",
        help="reversa: el kit sale del destino y los componentes vuelven a EHV/Stock",
    )
    args = ap.parse_args(argv)

    if args.cantidad <= 0:
        raise SystemExit("ERROR: --cantidad debe ser > 0")
    if args.aplicar and not args.folio:
        raise SystemExit("ERROR: --aplicar requiere --folio, la clave que permite reanudar")

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

    nombres = {ORIGEN: od.loc(ORIGEN), LOC_PRODUCCION: od.loc(LOC_PRODUCCION)}
    nombres[DESTINOS[args.destino]] = od.loc(DESTINOS[args.destino])
    loc_nombre = {v: k for k, v in nombres.items()}
    loc_prod = nombres[LOC_PRODUCCION]
    lista = pasos(
        plan,
        kit,
        args.cantidad,
        nombres[ORIGEN],
        loc_prod,
        nombres[DESTINOS[args.destino]],
        args.desarmar,
    )

    verbo = "Desarmado" if args.desarmar else "Armado"
    origin = f"{verbo} {args.folio}" if args.folio else None
    movs = (
        od.kw(
            "stock.move",
            "search_read",
            [[["origin", "=", origin]]],
            {
                "fields": [
                    "id",
                    "product_id",
                    "location_id",
                    "location_dest_id",
                    "product_uom_qty",
                    "state",
                ]
            },
        )
        if origin
        else []
    ) or []
    estados = [estado_de(p, movs) for p in lista]

    print(f"{verbo} {args.cantidad} x {kit['default_code']}  <->  {DESTINOS[args.destino]}")
    print(f"Disponibilidad actual del kit: {kit['qty_available']}\n")
    print("%-34s %9s  %-30s %s" % ("PRODUCTO", "CANTIDAD", "DE -> A", "ESTADO"))
    for p, (estado, _) in zip(lista, estados):
        ruta = f"{loc_nombre[p.origen]} -> {loc_nombre[p.destino]}"
        print("%-34s %9.1f  %-30s %s" % (p.nombre[:34], p.cantidad, ruta, estado))

    # Solo lo que aun no se crea sale de una ubicacion con stock que verificar;
    # Production es virtual y siempre entrega.
    por_salir = [
        p for p, (e, _) in zip(lista, estados) if e == "pendiente" and p.origen != loc_prod
    ]
    if por_salir:
        hay = disponible_en(od, [p.product_id for p in por_salir], por_salir[0].origen)
        pedido = [
            {"product_id": p.product_id, "nombre": p.nombre, "necesario": p.cantidad}
            for p in por_salir
        ]
        falta = faltantes(pedido, hay)
        if falta:
            print(f"\nNO SE PUEDE. Faltan en {loc_nombre[por_salir[0].origen]}:")
            for f in falta:
                print("   %-40s faltan %.1f" % (f["nombre"][:40], f["falta"]))
            return 1

    if not args.aplicar:
        print("\nSIMULACION. Con --aplicar --folio <clave> se ejecutan los movimientos.")
        return 0

    hechos = []
    for p, (estado, mid) in zip(lista, estados):
        if estado == "hecho":
            continue
        if mid is None:
            mid = crear(od, p, origin, f"{origin}: {p.nombre[:30]} -> {loc_nombre[p.destino]}")
        completar(od, mid, p.cantidad)
        hechos.append(mid)

    despues = od.kw(
        "product.product", "read", [[kit["id"]]], {"fields": ["qty_available"]}
    )[0]
    print(f"\nListo. {len(hechos)} movimientos nuevos o completados: {hechos}")
    print(
        f"Disponibilidad del kit: {kit['qty_available']} -> {despues['qty_available']}"
    )
    if not args.desarmar:
        print("Para deshacer: el mismo comando con --desarmar --aplicar --folio <clave>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
