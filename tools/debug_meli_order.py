#!/usr/bin/env python3
"""
debug_meli_order.py — Diagnóstico completo de una orden MeLi en el bridge.

Uso:
    python3 debug_meli_order.py <ORDER_ID>

Ejemplo:
    python3 debug_meli_order.py 2000011653786489
"""

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

if len(sys.argv) < 2:
    print("Uso: python3 debug_meli_order.py <ORDER_ID>")
    sys.exit(1)

ORDER_ID = sys.argv[1].strip()
DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
TOKEN_FILE = os.getenv("MELI_TOKEN_FILE", "/data/.meli_tokens.json")
ML_API = "https://api.mercadolibre.com"


def sep(title=""):
    print(f"\n{'=' * 60}")
    if title:
        print(f"  {title}")
        print(f"{'=' * 60}")


def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_token():
    try:
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        return t.get("access_token", "")
    except Exception:
        return None


def ml_get(path, token):
    url = f"{ML_API}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.getcode(), json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:500]
        return e.code, {"error": body}
    except Exception as e:
        return 0, {"error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 1. WEBHOOK RECIBIDO
# ──────────────────────────────────────────────────────────────────────
sep(f"1. WEBHOOK RECIBIDO — order_id contiene '{ORDER_ID}'")
try:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, received_at, topic, resource, status, dedupe_key
            FROM inbound_events
            WHERE payload_json LIKE ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (f"%{ORDER_ID}%",),
        ).fetchall()

    if not rows:
        print("  ❌ NO hay ningún evento en inbound_events con este order_id")
        print("  → El webhook nunca llegó al bridge, o llegó con otra forma del ID")
    else:
        for r in rows:
            print(f"  ✅ evento id={r['id']} received_at={r['received_at']}")
            print(f"     topic={r['topic']}  resource={r['resource']}")
            print(f"     status={r['status']}  dedupe_key={r['dedupe_key']}")
except Exception as e:
    print(f"  ERROR consultando inbound_events: {e}")

# ──────────────────────────────────────────────────────────────────────
# 2. ESTADO EN processed_inbound_events
# ──────────────────────────────────────────────────────────────────────
sep("2. PROCESADO — processed_inbound_events")
try:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT dedupe_key, processed_at, result, detail_json
            FROM processed_inbound_events
            WHERE dedupe_key LIKE ?
            ORDER BY processed_at DESC
            LIMIT 10
            """,
            (f"%{ORDER_ID}%",),
        ).fetchall()

    if not rows:
        print("  ❌ NO hay registro en processed_inbound_events")
        print("  → El worker aún no lo procesó, o se perdió en cola Redis")
    else:
        for r in rows:
            print(f"  dedupe_key : {r['dedupe_key']}")
            print(f"  processed_at: {r['processed_at']}")
            print(f"  result      : {r['result']}")
            try:
                detail = json.loads(r["detail_json"] or "{}")
                print(
                    f"  detail      : {json.dumps(detail, ensure_ascii=False, indent=4)}"
                )
            except Exception:
                print(f"  detail_json : {r['detail_json']}")
            print()
except Exception as e:
    print(f"  ERROR consultando processed_inbound_events: {e}")

# ──────────────────────────────────────────────────────────────────────
# 3. ESTADO LOCAL DE LA ORDEN
# ──────────────────────────────────────────────────────────────────────
sep("3. ESTADO LOCAL — inbound_orders_state")
try:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT order_id, last_state, last_updated_at, last_seen_at, pack_id
            FROM inbound_orders_state
            WHERE order_id=?
            LIMIT 1
            """,
            (ORDER_ID,),
        ).fetchone()

    if not row:
        print("  ❌ No hay entrada en inbound_orders_state")
    else:
        print(f"  order_id      : {row['order_id']}")
        print(f"  last_state    : {row['last_state']}")
        print(f"  last_updated  : {row['last_updated_at']}")
        print(f"  last_seen     : {row['last_seen_at']}")
        print(f"  pack_id       : {row['pack_id']}")
except Exception as e:
    print(f"  ERROR consultando inbound_orders_state: {e}")

# ──────────────────────────────────────────────────────────────────────
# 4. PLAN SO
# ──────────────────────────────────────────────────────────────────────
sep("4. PLAN SO — inbound_sales_orders")
try:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT dedupe_key, ml_order_id, site, status, odoo_so_id, odoo_name, created_at, updated_at
            FROM inbound_sales_orders
            WHERE ml_order_id=?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (ORDER_ID,),
        ).fetchall()

    if not rows:
        print("  ❌ No hay plan SO para esta orden")
    else:
        for r in rows:
            print(f"  dedupe_key  : {r['dedupe_key']}")
            print(f"  site        : {r['site']}")
            print(f"  status      : {r['status']}")
            print(f"  odoo_so_id  : {r['odoo_so_id']}")
            print(f"  odoo_name   : {r['odoo_name']}")
            print(f"  created_at  : {r['created_at']}")
            print()
except Exception as e:
    print(f"  ERROR consultando inbound_sales_orders (tabla puede no existir): {e}")

# ──────────────────────────────────────────────────────────────────────
# 5. COLA REDIS (cuántos jobs pendientes)
# ──────────────────────────────────────────────────────────────────────
sep("5. COLA REDIS — ml_orders_jobs")
try:
    import redis

    redis_url = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    qlen = r.llen("ml_orders_jobs")
    print(f"  Jobs pendientes en cola: {qlen}")
    if qlen > 0 and qlen <= 20:
        print("  Contenido:")
        jobs = r.lrange("ml_orders_jobs", 0, -1)
        for j in jobs:
            try:
                parsed = json.loads(j)
                print(
                    f"    → dedupe={parsed.get('dedupe_key')}  resource={parsed.get('resource')}"
                )
            except Exception:
                print(f"    → {j[:120]}")
except Exception as e:
    print(f"  ERROR consultando Redis: {e}")

# ──────────────────────────────────────────────────────────────────────
# 6. ESTADO ACTUAL EN MELI API
# ──────────────────────────────────────────────────────────────────────
sep("6. ESTADO ACTUAL EN MELI API (live)")
token = load_token()
if not token:
    print("  ❌ No se pudo cargar el token de MeLi")
else:
    status_code, order = ml_get(f"/orders/{ORDER_ID}", token)
    print(f"  HTTP: {status_code}")
    if status_code == 200:
        print(f"  status       : {order.get('status')}")
        print(f"  pack_id      : {order.get('pack_id')}")
        print(f"  logistic_type: {order.get('logistic_type')}")
        print(f"  site_id      : {order.get('site_id')}")
        print(f"  last_updated : {order.get('last_updated')}")
        buyer = order.get("buyer") or {}
        print(f"  buyer        : {buyer.get('nickname')} (id={buyer.get('id')})")
        items = order.get("order_items") or []
        print(f"  order_items  ({len(items)} items):")
        for it in items:
            item_d = it.get("item") or {}
            sku = item_d.get("seller_sku") or "[sin sku]"
            item_id = item_d.get("id")
            var_id = item_d.get("variation_id") or it.get("variation_id")
            qty = it.get("quantity")
            price = it.get("unit_price") or it.get("sale_fee") or ""
            print(
                f"    sku={sku}  item_id={item_id}  var_id={var_id}  qty={qty}  price={price}"
            )

        # Checar si hay mapeo en la DB
        sep("6b. MAPEO SKU en bridge DB")
        try:
            with db_conn() as conn:
                for it in items:
                    item_d = it.get("item") or {}
                    item_id = str(item_d.get("id") or "")
                    var_id = str(
                        item_d.get("variation_id") or it.get("variation_id") or ""
                    )
                    row = conn.execute(
                        "SELECT sku FROM sku_mapping WHERE channel='meli' AND remote_item_id=? AND remote_variation_id=?",
                        (item_id, var_id),
                    ).fetchone()
                    if row:
                        print(
                            f"  ✅ item_id={item_id} var_id={var_id} → sku={row['sku']}"
                        )
                    else:
                        print(
                            f"  ❌ item_id={item_id} var_id={var_id} → SIN MAPEO en sku_mapping"
                        )
        except Exception as e:
            print(f"  ERROR consultando sku_mapping: {e}")
    else:
        print(f"  Respuesta: {order}")

# ──────────────────────────────────────────────────────────────────────
# 7. SETTINGS RELEVANTES
# ──────────────────────────────────────────────────────────────────────
sep("7. FEATURE FLAGS RELEVANTES")
keys_to_check = [
    "meli_inbound_enabled",
    "meli_webhook_enabled",
    "meli_inbound_fbm_paid_enabled",
    "meli_inbound_full_paid_enabled",
    "meli_inbound_fbm_refunds_enabled",
    "meli_inbound_full_refunds_enabled",
]
try:
    with db_conn() as conn:
        for k in keys_to_check:
            row = conn.execute(
                "SELECT value FROM bridge_settings WHERE key=?", (k,)
            ).fetchone()
            val = row["value"] if row else "(no configurado)"
            flag = "✅" if val == "1" else "❌"
            print(f"  {flag} {k} = {val}")
except Exception as e:
    print(f"  ERROR consultando bridge_settings: {e}")

sep("FIN DEL DIAGNÓSTICO")
print(f"  Order ID analizado: {ORDER_ID}")
print(f"  DB: {DB_PATH}")
print(f"  Fecha: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
print()
