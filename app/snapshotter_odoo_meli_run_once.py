#!/usr/bin/env python3
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone
import defusedxml.xmlrpc as _defusedxml_xmlrpc

# monkey_patch() DEBE correr antes de importar xmlrpc.client: parcha el parser
# XML de la stdlib contra entidades maliciosas. Como es una sentencia a nivel
# de modulo, ruff marca E402 en TODO import posterior; por eso los imports que
# siguen llevan `noqa: E402`. No los muevas arriba: romperias la mitigacion.
_defusedxml_xmlrpc.monkey_patch()
import xmlrpc.client  # noqa: E402

# =========================
# ENV OBLIGATORIA
# =========================
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("[FATAL] ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD no definidos")
    sys.exit(1)

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
CHANNEL = "meli"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def die(msg: str, code: int = 1):
    print(msg)
    sys.exit(code)


# =========================
# Conexión Odoo (XML-RPC) - allow_none=True
# =========================
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    die("[FATAL] Autenticación Odoo falló")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

# =========================
# SQLite
# =========================
conn = sqlite3.connect(DB_PATH, timeout=10)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
cur = conn.cursor()

# =========================
# Cargar set de SKUs mapeados (FUENTE DE VERDAD PARA QUÉ SE SINCRONIZA)
# =========================
mapped = set()
try:
    rows = cur.execute(
        "SELECT sku FROM sku_mapping WHERE channel=?", (CHANNEL,)
    ).fetchall()
    mapped = {r[0] for r in rows if r and r[0]}
except Exception as e:
    conn.close()
    die(f"[FATAL] No pude leer sku_mapping: {e!r}")

if not mapped:
    # Esto es intencionalmente fatal: sin mapping, NO sincronizamos nada.
    conn.close()
    die(
        "[FATAL] sku_mapping está vacío para canal=meli. No hay nada seguro que sincronizar."
    )

# =========================
# Crear evento (payload requerido + item_count NOT NULL)
# =========================
created_at = utc_now_iso()
payload = {
    "channel": CHANNEL,
    "mode": "mapped_only",
    "mapped_skus": len(mapped),
}
payload_json = json.dumps(payload, ensure_ascii=False)

cur.execute(
    "INSERT INTO events(created_at, channel, item_count, payload) VALUES (?,?,?,?)",
    (created_at, CHANNEL, 0, payload_json),
)
event_id = cur.lastrowid

# =========================
# Leer productos desde Odoo
# =========================
domain = [
    ("active", "=", True),
    ("sell_on_meli", "=", True),
    ("default_code", "!=", False),
]
fields = ["default_code", "qty_available"]

products = models.execute_kw(
    ODOO_DB,
    uid,
    ODOO_PASSWORD,
    "product.product",
    "search_read",
    [domain],
    {"fields": fields},
)

items = 0
skipped_not_mapped = 0

for p in products:
    sku = (p.get("default_code") or "").strip()
    if not sku:
        continue

    # SOLO lo que está mapeado en sku_mapping (esto evita NO_MAPPING en producción)
    if sku not in mapped:
        skipped_not_mapped += 1
        continue

    qty = int(p.get("qty_available") or 0)

    cur.execute(
        "INSERT INTO snapshot_items(event_id, channel, sku, qty) VALUES (?,?,?,?)",
        (str(event_id), CHANNEL, sku, qty),
    )
    items += 1

# Actualizar contador final
cur.execute(
    "UPDATE events SET item_count=? WHERE id=?",
    (items, int(event_id)),
)

conn.commit()
conn.close()

print(
    json.dumps(
        {
            "SNAPSHOT_OK": True,
            "channel": CHANNEL,
            "event_id": str(event_id),
            "items": items,
            "skipped_not_mapped": skipped_not_mapped,
        },
        ensure_ascii=False,
    )
)
