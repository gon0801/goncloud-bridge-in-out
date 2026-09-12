#!/usr/bin/env python3
"""Restringe las recetas phantom a la operacion de entrega de EHV-MX.

Que resuelve
------------
Modelo hibrido decidido para los almacenes por canal (PENDIENTES #3):

    FBM   sale de EHV-MX      -> la receta aplica, el kit explota en componentes
    FULL  sale de Meli-Full   -> sin receta aplicable, el kit sale como unidad
    FBA   sale de FBA-MX/US   -> sin receta aplicable, el kit sale como unidad

MercadoLibre FULL y Amazon FBA guardan kits ARMADOS. Con la receta aplicando a
toda operacion, un picking desde esos almacenes se explotaria en componentes
que alli no existen. `mrp.bom.picking_type_id` limita la receta a un tipo de
operacion; fijandola en la entrega de EHV-MX, los otros almacenes mueven el kit
entero.

Verificado antes de escribir nada (2026-09-12, sobre un SKU no publicado):
restringir la receta NO altera `qty_available`. El calculo de disponibilidad de
kits no filtra por tipo de operacion.

    ANTES    qty_available = 77.0
    BoM restringida a picking_type = EHV-MX/Delivery Orders
    DESPUES  qty_available = 77.0

Salvaguardas
------------
* Solo recetas ACTIVAS. Esta base tiene 1541 phantom archivadas contra 1009
  vivas, residuo de migraciones de componentes; tocarlas no sirve de nada.
* El respaldo se escribe y se RELEE antes de la primera escritura. Si el
  respaldo falla, el script aborta sin tocar Odoo. (El 2026-09-12 un purgado de
  `sku_mapping` corrio con el respaldo fallado porque el borrado no dependia de
  el; aqui si depende.)
* Muestrea `qty_available` antes y despues. Si cambia en algun producto, avisa
  fuerte: es la senal de que el supuesto de arriba dejo de valer.
* Idempotente: las recetas que ya apuntan al destino no se reescriben.

Uso
---
    # ver que haria (default)
    docker exec bridge-api python3 /data/odoo_migrar_boms_picking_type.py

    # aplicar
    docker exec bridge-api python3 /data/odoo_migrar_boms_picking_type.py --aplicar

    # deshacer con el respaldo que imprimio la corrida
    docker exec bridge-api python3 /data/odoo_migrar_boms_picking_type.py \\
        --revertir /data/bom_picking_type_20260912T190000Z.json
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
LOTE = 100
MUESTRA = 12


def boms_a_migrar(boms: list, destino_id: int) -> list:
    """Ids de recetas que hay que escribir para quedar en `destino_id`.

    Idempotencia: una receta que ya apunta al destino se deja en paz. Reescribir
    1009 registros identicos ensucia el historial de Odoo y alarga la ventana de
    escritura sin ganar nada.
    """
    pendientes = []
    for b in boms:
        actual = b.get("picking_type_id")
        actual_id = actual[0] if isinstance(actual, (list, tuple)) and actual else None
        if actual_id != destino_id:
            pendientes.append(b["id"])
    return pendientes


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


def muestrear(od, ids):
    filas = (
        od.kw(
            "product.product",
            "read",
            [ids],
            {"fields": ["default_code", "qty_available"]},
        )
        or []
    )
    return {f["id"]: f.get("qty_available") for f in filas}


def revertir(od, ruta):
    with open(ruta, encoding="utf-8") as fh:
        datos = json.load(fh)
    filas = datos.get("boms") or []
    print(f"Revirtiendo {len(filas)} recetas desde {ruta}")
    for i in range(0, len(filas), LOTE):
        trozo = filas[i : i + LOTE]
        por_valor = {}
        for f in trozo:
            por_valor.setdefault(f.get("picking_type_id") or False, []).append(f["id"])
        for valor, ids in por_valor.items():
            od.kw("mrp.bom", "write", [ids, {"picking_type_id": valor}])
        print(f"  {min(i + LOTE, len(filas))}/{len(filas)}")
    print("Listo.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Restringe recetas phantom a un tipo de operacion"
    )
    ap.add_argument("--almacen", default="EHV", help="codigo del almacen (default EHV)")
    ap.add_argument(
        "--aplicar", action="store_true", help="escribir (sin esto solo simula)"
    )
    ap.add_argument("--revertir", metavar="ARCHIVO", help="restaurar desde un respaldo")
    ap.add_argument("--backup-dir", default=os.path.dirname(DB_PATH) or ".")
    args = ap.parse_args()

    od = Odoo()
    if args.revertir:
        return revertir(od, args.revertir)

    pts = (
        od.kw(
            "stock.picking.type",
            "search_read",
            [[["code", "=", "outgoing"], ["warehouse_id.code", "=", args.almacen]]],
            {"fields": ["id", "name", "warehouse_id"]},
        )
        or []
    )
    if len(pts) != 1:
        raise SystemExit(
            f"ERROR: se esperaba 1 operacion de entrega para el almacen '{args.almacen}', "
            f"hay {len(pts)}. Revisa antes de migrar."
        )
    destino = pts[0]
    print(
        f"Destino: {destino['warehouse_id'][1]} / {destino['name']} (id {destino['id']})"
    )

    boms = (
        od.kw(
            "mrp.bom",
            "search_read",
            [[["type", "=", "phantom"], ["active", "=", True]]],
            {"fields": ["id", "picking_type_id"], "limit": 5000},
        )
        or []
    )
    pendientes = boms_a_migrar(boms, destino["id"])
    print(f"Recetas phantom activas: {len(boms)}  ·  a modificar: {len(pendientes)}")
    if not pendientes:
        print("Nada que hacer: ya estan restringidas.")
        return 0

    prods = (
        od.kw(
            "product.product",
            "search",
            [
                [
                    ["active", "=", True],
                    ["sell_on_meli", "=", True],
                    ["qty_available", ">", 0],
                ]
            ],
            {"limit": MUESTRA},
        )
        or []
    )
    antes = muestrear(od, prods) if prods else {}
    print(f"Muestra de control: {len(antes)} productos publicados con stock")

    if not args.aplicar:
        print("\nSIMULACION. Con --aplicar se escribirian esas recetas.")
        return 0

    # Respaldo primero, y releerlo. Si esto falla, no se toca Odoo.
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ruta = os.path.join(args.backup_dir, f"bom_picking_type_{sello}.json")
    original = [
        {
            "id": b["id"],
            "picking_type_id": (
                b["picking_type_id"][0]
                if isinstance(b.get("picking_type_id"), (list, tuple))
                and b["picking_type_id"]
                else False
            ),
        }
        for b in boms
        if b["id"] in set(pendientes)
    ]
    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump({"destino": destino["id"], "creado": sello, "boms": original}, fh)
        with open(ruta, encoding="utf-8") as fh:
            releido = json.load(fh)
        if len(releido.get("boms") or []) != len(original):
            raise ValueError("el respaldo releido no coincide")
    except Exception as e:
        raise SystemExit(f"ERROR: no se pudo respaldar ({e}). NO se toco Odoo.")
    print(f"Respaldo verificado: {ruta}")

    for i in range(0, len(pendientes), LOTE):
        trozo = pendientes[i : i + LOTE]
        od.kw("mrp.bom", "write", [trozo, {"picking_type_id": destino["id"]}])
        print(f"  {min(i + LOTE, len(pendientes))}/{len(pendientes)}")

    despues = muestrear(od, prods) if prods else {}
    cambiados = [pid for pid, v in antes.items() if despues.get(pid) != v]
    print()
    if cambiados:
        print(
            f"!! ALERTA: {len(cambiados)} productos de la muestra cambiaron de disponibilidad."
        )
        for pid in cambiados[:8]:
            print(f"   producto {pid}: {antes[pid]} -> {despues.get(pid)}")
        print(f"   Revierte con:  --revertir {ruta}")
        return 1
    print(f"OK: la disponibilidad no cambio en los {len(antes)} productos de control.")
    print(f"Para deshacer:  --revertir {ruta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
