#!/usr/bin/env python3
import os
import sys
import sqlite3
import argparse
from datetime import datetime, timezone
import defusedxml.xmlrpc as _defusedxml_xmlrpc

# monkey_patch() DEBE correr antes de importar xmlrpc.client: parcha el parser
# XML de la stdlib contra entidades maliciosas. Como es una sentencia a nivel
# de modulo, ruff marca E402 en TODO import posterior; por eso los imports que
# siguen llevan `noqa: E402`. No los muevas arriba: romperias la mitigacion.
_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

BRIDGE_DB = (
    os.getenv("BRIDGE_DB_PATH")
    or os.getenv("BRIDGE_DB")
    or "/mnt/data/appdata/bridge/data/bridge.db"
)

# Odoo creds por ENV (NO hardcode)
ODOO_URL = os.getenv("ODOO_URL", "").rstrip("/")
ODOO_DB = os.getenv("ODOO_DB", "")
ODOO_USER = os.getenv("ODOO_USER", "")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "")

DEFAULT_LOCATION_DOMAIN = [
    ("usage", "=", "internal"),
]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def must_env(name, val):
    if not val:
        die(f"FALTA ENV {name}. Define {name} en tu entorno antes de correr apply.")


def db():
    con = sqlite3.connect(BRIDGE_DB, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    return con


def get_setting(con, key, default="0"):
    row = con.execute(
        "SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,)
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else default


def get_delta(con, delta_id: str):
    row = con.execute(
        """
        SELECT delta_id, order_id, sku, qty_delta, reason, applied_to_odoo
        FROM inbound_stock_deltas
        WHERE delta_id=?
        LIMIT 1
    """,
        (delta_id,),
    ).fetchone()
    return row


def is_allowed(con, sku: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM inbound_allowed_skus
        WHERE sku=? AND enabled=1
        LIMIT 1
    """,
        (sku,),
    ).fetchone()
    return row is not None


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        die("Odoo auth falló (uid vacío). Revisa ODOO_DB/USER/PASSWORD/URL.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def odoo_search(models, uid, model, domain, limit=1):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, "search", [domain], {"limit": limit}
    )


def odoo_read(models, uid, model, ids, fields):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, "read", [ids], {"fields": fields}
    )


def odoo_write(models, uid, model, ids, vals):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, "write", [ids, vals])


def odoo_call(models, uid, model, method, ids, *args, **kwargs):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, method, [ids, *args], kwargs or {}
    )


def main():
    ap = argparse.ArgumentParser(
        description="Apply ONE inbound_stock_deltas row to Odoo via stock.quant inventory adjustment."
    )
    ap.add_argument("--delta-id", required=True, help="Exact delta_id to apply")
    ap.add_argument(
        "--location-id",
        type=int,
        default=0,
        help="Force Odoo location_id (internal). If 0, auto-pick first internal.",
    )
    args = ap.parse_args()

    must_env("ODOO_URL", ODOO_URL)
    must_env("ODOO_DB", ODOO_DB)
    must_env("ODOO_USER", ODOO_USER)
    must_env("ODOO_PASSWORD", ODOO_PASSWORD)

    con = db()

    # Safety gates (CANÓNICO)
    inbound_enabled = get_setting(con, "meli_inbound_enabled", "0")
    dry_run = get_setting(con, "meli_inbound_dry_run", "1")
    apply_enabled = get_setting(con, "meli_inbound_apply_stock_enabled", "0")

    if inbound_enabled != "1":
        die("BLOQUEADO: meli_inbound_enabled != 1")
    if dry_run != "0":
        die("BLOQUEADO: meli_inbound_dry_run != 0 (quita dry_run antes de apply)")
    if apply_enabled != "1":
        die(
            "BLOQUEADO: meli_inbound_apply_stock_enabled != 1 (botón rojo sigue apagado)"
        )

    row = get_delta(con, args.delta_id)
    if not row:
        die("No existe ese delta_id en inbound_stock_deltas.")
    delta_id, order_id, sku, qty_delta, reason, applied = row

    if int(applied or 0) == 1:
        die("Ya está aplicado (applied_to_odoo=1).")

    if not is_allowed(con, sku):
        die(f"BLOQUEADO: SKU no permitido por allowlist inbound_allowed_skus: {sku}")

    qty_delta = int(qty_delta)

    # Conecta Odoo
    uid, models = odoo_connect()

    # 1) Buscar producto por default_code (SKU)
    prod_ids = odoo_search(
        models, uid, "product.product", [("default_code", "=", sku)], limit=2
    )
    if not prod_ids:
        die(f"SKU no existe en Odoo: {sku}")
    if len(prod_ids) > 1:
        die(
            f"SKU duplicado en Odoo (más de 1 product.product con default_code={sku}). Arregla eso antes."
        )

    product_id = prod_ids[0]

    # 2) Elegir location_id
    if args.location_id:
        location_id = args.location_id
    else:
        loc_ids = odoo_search(
            models, uid, "stock.location", DEFAULT_LOCATION_DOMAIN, limit=1
        )
        if not loc_ids:
            die("No encontré stock.location internal para ajustar inventario.")
        location_id = loc_ids[0]

    # 3) Leer quant (si existe)
    quant_ids = odoo_search(
        models,
        uid,
        "stock.quant",
        [("product_id", "=", product_id), ("location_id", "=", location_id)],
        limit=1,
    )

    if quant_ids:
        qid = quant_ids[0]
        q = odoo_read(models, uid, "stock.quant", [qid], ["quantity"])[0]
        current_qty = float(q.get("quantity") or 0.0)
        new_qty = current_qty + float(qty_delta)

        # Escribir inventory_quantity y aplicar
        odoo_write(models, uid, "stock.quant", [qid], {"inventory_quantity": new_qty})
        # En Odoo moderno, esto aplica el ajuste
        odoo_call(models, uid, "stock.quant", "action_apply_inventory", [qid])
        odoo_ref = f"stock.quant:{qid}"
    else:
        # Crear un quant primero (si no existe), luego ajustar
        # Nota: create en stock.quant puede requerir company_id; intentamos mínimo.
        qid = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.quant",
            "create",
            [
                {
                    "product_id": product_id,
                    "location_id": location_id,
                    "inventory_quantity": float(qty_delta),
                }
            ],
        )
        odoo_call(models, uid, "stock.quant", "action_apply_inventory", [qid])
        odoo_ref = f"stock.quant:{qid}"

    # 4) Marcar aplicado en bridge.db
    con.execute(
        """
        UPDATE inbound_stock_deltas
        SET applied_to_odoo=1,
            applied_at=?,
            odoo_ref=?
        WHERE delta_id=?
    """,
        (utc_now(), odoo_ref, delta_id),
    )
    con.commit()

    print("OK_APPLIED")
    print(f"delta_id={delta_id}")
    print(f"sku={sku} qty_delta={qty_delta} reason={reason}")
    print(f"odoo_ref={odoo_ref} location_id={location_id}")


if __name__ == "__main__":
    main()
