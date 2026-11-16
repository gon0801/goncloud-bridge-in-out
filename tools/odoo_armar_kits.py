#!/usr/bin/env python3
"""Aparta en un almacen de canal (Flex en FBA - MX, Full, FBA-US) los componentes de N kits.

Que resuelve
------------
Para vender un kit desde otro almacen, ese almacen necesita las piezas. Esto
las mueve de EHV/Stock a la ubicacion de stock del almacen destino con un solo
picking, y lo deshace con `--desarmar`.

Por que componentes y no el kit armado
--------------------------------------
Las recetas de los kits son phantom. En Odoo 17, `stock.move._action_explode`
busca la receta phantom sin filtrar por tipo de operacion
(`mrp/models/stock_move.py`: `_bom_find(move.product_id, ..., bom_type='phantom')`),
asi que cualquier move de un kit que tenga tipo de operacion se convierte en
moves de sus componentes. El `picking_type_id` de la receta no lo evita. Por
eso:

* un picking que diera de alta el kit en el destino se explotaria en
  componentes, y
* la venta del kit desde el destino (la entrega del almacen de la SO) tambien
  se explota y descuenta los componentes de ese almacen.

Ademas `qty_available` de un kit siempre se calcula desde sus componentes. Lo
que si funciona es lo que esta herramienta hace: poner en el destino las piezas
de N kits. La venta Flex las descuenta de ahi y los snapshotters, que apartan
ese almacen, dejan de ofrecerlas en MeLi y Amazon FBM.

Limite: Odoo no sabe que piezas estan armadas. Si otro kit del destino
comparte un componente, su venta puede tomarlo.

Salvaguardas
------------
* Simula por defecto. Escribe solo con `--aplicar --folio <clave>`.
* Exige que el kit tenga EXACTAMENTE una receta activa y que sea phantom.
* Verifica que TODOS los componentes alcancen en el origen antes de crear el
  picking.
* Solo metodos publicos por RPC: Odoo 17 rechaza los que empiezan con guion
  bajo. `action_confirm`, `action_assign`, escribir `quantity`/`picked` en los
  moves y `button_validate`.
* El picking lleva en `origin` el folio. Repetir el comando con el mismo folio
  termina un picking a medias o no hace nada si ya esta hecho; nunca duplica.
  Un folio reusado con otro SKU o cantidad aborta.
* Un move suelto del folio en draft, sin picking (lo dejo la version que
  llamaba metodos privados, como el 9173), se cancela al aplicar. En otro
  estado aborta.
* Si al confirmar Odoo cambia los productos del picking (un componente que es
  kit se exploto), se cancela el picking sin mover stock.
* Si al validar Odoo pide el asistente de backorder, aborta sin validar.

Uso
---
    docker exec bridge-api python3 /data/odoo_armar_kits.py \\
        --sku SET-PLA-CH-CEN-DOR --cantidad 4 --destino FLEX

    # y con --aplicar --folio <clave> para ejecutarlo; si se corta, el mismo
    # comando lo reanuda. La reversa, con su propio folio:
    ... --destino FLEX --desarmar --aplicar --folio <clave>

`--destino` acepta el nombre de un almacen de Odoo ("FBA - MX") o `FLEX`, que
es el almacen del setting `warehouse_amazon_flex_mx` y, sin el, el de
`warehouse_amazon_fba_mx`: Flex usa el almacen FBA - MX.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
ALMACEN_PRINCIPAL = "EHV"
SETTINGS_FLEX = ("warehouse_amazon_flex_mx", "warehouse_amazon_fba_mx")
CAMPOS_ALMACEN = ["id", "name", "code", "lot_stock_id", "int_type_id"]
CAMPOS_MOVE = ["id", "product_id", "product_uom_qty", "state", "picking_id", "location_id", "location_dest_id"]


def plan_de_componentes(lineas_bom: list, cantidad_kits: int) -> list:
    """Cuanto de cada componente hace falta para `cantidad_kits`.

    Devuelve `[{"product_id", "nombre", "necesario"}]`.

    La receta expresa cantidades para UN kit (`product_qty` por linea); se
    multiplica sin redondear: si una receta pidiera 0.5 de algo, 3 kits piden
    1.5 y el faltante debe verse como tal, no esconderse en un redondeo.
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
    """Componentes que no alcanzan. Lista vacia = se puede mover."""
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
    return (row[0] or "").strip() if row else ""


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


@dataclass(frozen=True)
class Linea:
    product_id: int
    nombre: str
    cantidad: float


@dataclass(frozen=True)
class Traslado:
    """El picking: todas sus lineas van de `origen` a `destino` con el tipo de
    operacion `tipo`."""

    tipo: int
    origen: int
    destino: int
    lineas: tuple


def nombre_destino(destino: str) -> str:
    """Nombre del almacen destino. `FLEX` sale de los settings."""
    if destino.upper() != "FLEX":
        return destino
    for key in SETTINGS_FLEX:
        valor = get_setting(key)
        if valor:
            return valor
    raise SystemExit(f"ERROR: --destino FLEX necesita uno de los settings {list(SETTINGS_FLEX)}")


def almacen(od, dominio: list, que: str) -> dict:
    r = od.kw("stock.warehouse", "search_read", [dominio], {"fields": CAMPOS_ALMACEN}) or []
    if len(r) != 1:
        raise SystemExit(f"ERROR: se esperaba 1 almacen {que}, hay {len(r)}")
    return r[0]


def traslado(plan: list, ehv: dict, destino: dict, desarmar: bool) -> Traslado:
    lineas = tuple(Linea(p["product_id"], p["nombre"], p["necesario"]) for p in plan)
    if desarmar:
        return Traslado(destino["int_type_id"][0], destino["lot_stock_id"][0], ehv["lot_stock_id"][0], lineas)
    return Traslado(ehv["int_type_id"][0], ehv["lot_stock_id"][0], destino["lot_stock_id"][0], lineas)


def _firma(pares) -> list:
    return sorted((pid, round(qty, 6)) for pid, qty in pares)


def _de_moves(moves) -> list:
    return _firma((m["product_id"][0], m["product_uom_qty"]) for m in moves)


def _esperado(t: Traslado) -> list:
    return _firma((ln.product_id, ln.cantidad) for ln in t.lineas)


def picking_del_folio(od, t: Traslado, origin: str):
    """El picking vivo del folio, o None. Si no cuadra con este comando, aborta."""
    pickings = [
        p
        for p in od.kw(
            "stock.picking",
            "search_read",
            [[["origin", "=", origin]]],
            {"fields": ["id", "state", "location_id", "location_dest_id"]},
        )
        or []
        if p["state"] != "cancel"
    ]
    if len(pickings) > 1:
        raise SystemExit(f"ERROR: el folio tiene {len(pickings)} pickings vivos: {[p['id'] for p in pickings]}")
    if not pickings:
        return None
    p = pickings[0]
    ruta = (p["location_id"][0], p["location_dest_id"][0]) == (t.origen, t.destino)
    if not ruta or _de_moves(moves_de(od, p["id"])) != _esperado(t):
        raise SystemExit(
            f"ERROR: el picking {p['id']} del folio no coincide con este comando "
            f"(otro SKU, cantidad o destino); usa otro folio"
        )
    return p


def sueltos_del_folio(od, origin: str) -> list:
    """Moves del folio sin picking. Solo un draft se puede cancelar sin efecto."""
    sueltos = [
        m
        for m in od.kw(
            "stock.move",
            "search_read",
            [[["origin", "=", origin], ["picking_id", "=", False]]],
            {"fields": CAMPOS_MOVE},
        )
        or []
        if m["state"] != "cancel"
    ]
    otros = [f"{m['id']} ({m['state']})" for m in sueltos if m["state"] != "draft"]
    if otros:
        raise SystemExit(f"ERROR: el folio tiene moves sueltos que ya no estan en draft: {otros}; resuelvelos en Odoo")
    return sueltos


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
        disponible[pid] = disponible.get(pid, 0) + (q["quantity"] or 0) - (q["reserved_quantity"] or 0)
    return disponible


def moves_de(od, picking_id: int) -> list:
    movs = od.kw("stock.move", "search_read", [[["picking_id", "=", picking_id]]], {"fields": CAMPOS_MOVE}) or []
    return [m for m in movs if m["state"] != "cancel"]


def ejecutar(od, t: Traslado, picking, origin: str, etiqueta: str) -> int:
    """Lleva el traslado a done solo con metodos publicos."""
    pid = picking["id"] if picking else None
    if pid is None:
        pid = od.kw(
            "stock.picking",
            "create",
            [
                {
                    "picking_type_id": t.tipo,
                    "location_id": t.origen,
                    "location_dest_id": t.destino,
                    "origin": origin,
                    "move_ids": [
                        (
                            0,
                            0,
                            {
                                "name": f"{origin}: {ln.nombre[:40]} -> {etiqueta}",
                                "origin": origin,
                                "product_id": ln.product_id,
                                "product_uom_qty": ln.cantidad,
                                "location_id": t.origen,
                                "location_dest_id": t.destino,
                            },
                        )
                        for ln in t.lineas
                    ],
                }
            ],
        )

    movs = moves_de(od, pid)
    if any(m["state"] == "draft" for m in movs):
        od.kw("stock.picking", "action_confirm", [[pid]])
        movs = moves_de(od, pid)
    if _de_moves(movs) != _esperado(t):
        od.kw("stock.picking", "action_cancel", [[pid]])
        raise SystemExit(
            f"ERROR: al confirmar el picking {pid} Odoo cambio sus productos "
            f"(un componente con receta phantom se exploto); se cancelo sin mover stock"
        )
    if any(m["state"] != "assigned" for m in movs):
        od.kw("stock.picking", "action_assign", [[pid]])
        movs = moves_de(od, pid)
        cortos = [m["product_id"][1] for m in movs if m["state"] != "assigned"]
        if cortos:
            raise SystemExit(
                f"ERROR: el picking {pid} no pudo reservar {cortos}; queda sin validar. "
                f"El mismo comando lo reanuda cuando haya stock"
            )
    for m in movs:
        od.kw("stock.move", "write", [[m["id"]], {"quantity": m["product_uom_qty"], "picked": True}])

    res = od.kw("stock.picking", "button_validate", [[pid]])
    if isinstance(res, dict):
        if res.get("res_model") != "stock.immediate.transfer":
            raise SystemExit(
                f"ERROR: Odoo pidio el asistente '{res.get('res_model')}' al validar el picking "
                f"{pid}: las cantidades no cuadran con lo pedido; queda sin validar"
            )
        od.kw("stock.picking", "button_validate", [[pid]], {"context": {"skip_immediate": True}})
    estado = od.kw("stock.picking", "read", [[pid]], {"fields": ["state"]})[0]["state"]
    if estado != "done":
        raise SystemExit(f"ERROR: el picking {pid} no quedo en done")
    return pid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sku", required=True)
    ap.add_argument("--cantidad", type=int, required=True)
    ap.add_argument("--destino", required=True, help="nombre del almacen de Odoo, o FLEX")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--folio", help="clave unica; repetir con el mismo folio reanuda sin duplicar")
    ap.add_argument(
        "--desarmar",
        action="store_true",
        help="reversa: los componentes vuelven del destino a EHV/Stock",
    )
    args = ap.parse_args(argv)

    if args.cantidad <= 0:
        raise SystemExit("ERROR: --cantidad debe ser > 0")
    if args.aplicar and not args.folio:
        raise SystemExit("ERROR: --aplicar requiere --folio, la clave que permite reanudar")

    nombre = nombre_destino(args.destino)
    od = Odoo()
    ehv = almacen(od, [["code", "=", ALMACEN_PRINCIPAL]], f"con codigo {ALMACEN_PRINCIPAL}")
    destino = almacen(od, [["name", "=", nombre]], f"llamado '{nombre}'")
    if destino["id"] == ehv["id"]:
        raise SystemExit(f"ERROR: el destino '{nombre}' es el almacen principal")

    prods = (
        od.kw(
            "product.product",
            "search_read",
            [[["default_code", "=", args.sku]]],
            {"fields": ["id", "default_code", "product_tmpl_id"]},
        )
        or []
    )
    if len(prods) != 1:
        raise SystemExit(f"ERROR: se esperaba 1 producto con SKU '{args.sku}', hay {len(prods)}")
    kit = prods[0]

    boms = (
        od.kw(
            "mrp.bom",
            "search_read",
            [
                [
                    "|",
                    ["product_id", "=", kit["id"]],
                    "&",
                    ["product_id", "=", False],
                    ["product_tmpl_id", "=", kit["product_tmpl_id"][0]],
                ]
            ],
            {"fields": ["id", "bom_line_ids", "type"]},
        )
        or []
    )
    if len(boms) != 1:
        raise SystemExit(
            f"ERROR: el kit tiene {len(boms)} recetas activas, se esperaba 1. "
            f"Con varias no se puede saber cual usar."
        )
    if boms[0]["type"] != "phantom":
        raise SystemExit(
            f"ERROR: la receta {boms[0]['id']} es '{boms[0]['type']}', no phantom; "
            f"un kit con receta de fabricacion se arma con una orden de fabricacion"
        )

    lineas = od.kw("mrp.bom.line", "read", [boms[0]["bom_line_ids"]], {"fields": ["product_id", "product_qty"]}) or []
    plan = plan_de_componentes(lineas, args.cantidad)
    t = traslado(plan, ehv, destino, args.desarmar)
    stock = {ehv["lot_stock_id"][0]: ehv["lot_stock_id"][1], destino["lot_stock_id"][0]: destino["lot_stock_id"][1]}

    verbo = "Desarmado" if args.desarmar else "Armado"
    origin = f"{verbo} {args.folio}" if args.folio else None
    picking = picking_del_folio(od, t, origin) if origin else None
    sueltos = sueltos_del_folio(od, origin) if origin else []
    estado = "pendiente" if not picking else ("hecho" if picking["state"] == "done" else "a medias")

    print(f"{verbo} {args.cantidad} x {kit['default_code']}: componentes {stock[t.origen]} -> {stock[t.destino]}")
    print(f"Almacen destino: {destino['name']} (codigo {destino['code']}); tipo de operacion {t.tipo}")
    print(f"Picking {picking['id'] if picking else 'nuevo'}: {estado}\n")
    print("%-44s %9s" % ("COMPONENTE", "CANTIDAD"))
    for ln in t.lineas:
        print("%-44s %9.1f" % (ln.nombre[:44], ln.cantidad))
    for m in sueltos:
        print(
            f"Move suelto {m['id']} del folio en draft ({m['product_id'][1]} "
            f"{m['location_id'][1]} -> {m['location_dest_id'][1]}): se cancela al aplicar"
        )

    if estado == "pendiente":
        hay = disponible_en(od, [ln.product_id for ln in t.lineas], t.origen)
        falta = faltantes(plan, hay)
        if falta:
            print(f"\nNO SE PUEDE. Faltan en {stock[t.origen]}:")
            for f in falta:
                print("   %-40s hay %.1f, faltan %.1f" % (f["nombre"][:40], f["hay"], f["falta"]))
            return 1

    if not args.aplicar:
        print("\nSIMULACION. Con --aplicar --folio <clave> se ejecuta.")
        return 0

    if sueltos:
        od.kw("stock.move", "write", [[m["id"] for m in sueltos], {"state": "cancel"}])
    if estado == "hecho":
        print("\nYa estaba hecho; no se crea nada.")
        return 0
    pid = ejecutar(od, t, picking, origin, destino["name"])
    print(f"\nListo. Picking {pid} en done.")
    if not args.desarmar:
        print("Para deshacer: el mismo comando con --desarmar --aplicar --folio <otra clave>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
