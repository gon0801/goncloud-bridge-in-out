#!/usr/bin/env python3
"""
AMAZON ORDERS POLL — GONCLOUD BRIDGE
Consulta órdenes de Amazon SP-API y las envía a Redis para procesar.

Uso: python3 amazon_orders_poll.py [--days N] [--marketplace MX|US|BOTH]

Este script:
1. Obtiene access token de Amazon
2. Consulta órdenes actualizadas en los últimos N días (default: 2)
   NOTA: usa LastUpdatedAfter (no CreatedAfter) para capturar órdenes creadas
   hace más de N días que acaban de cambiar de estado (ej. Pending→Shipped).
3. Para cada orden, obtiene los items
4. Envía a Redis cola amazon_orders_jobs
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import redis
import httpx

# ============================================================
# CONFIG
# ============================================================

DB_PATH = os.getenv("BRIDGE_DB_PATH") or os.getenv("BRIDGE_DB") or "/data/bridge.db"
REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
QUEUE = "amazon_orders_jobs"

# Amazon endpoints
AMAZON_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
AMAZON_API_BASE = "https://sellingpartnerapi-na.amazon.com"

# Marketplaces
MARKETPLACES = {
    "MX": "A1AM78C64UM0Y8",
    "US": "ATVPDKIKX0DER",
}

# ============================================================
# HELPERS
# ============================================================

def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(key: str, default: str = "") -> str:
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,)
            ).fetchone()
        return str(row["value"]) if row else default
    except Exception:
        return default


def get_credentials():
    return {
        "client_id": get_setting("amazon_sp_api_client_id"),
        "client_secret": get_setting("amazon_sp_api_client_secret"),
        "refresh_token": get_setting("amazon_sp_api_refresh_token"),
    }


def get_access_token(creds: dict) -> str:
    """Obtiene access token usando refresh token."""
    resp = httpx.post(
        AMAZON_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"No access_token in response: {data}")
    return data["access_token"]


def _sp_api_get(token: str, url: str, params: dict = None, max_retries: int = 4) -> httpx.Response:
    """GET a SP-API endpoint con retry exponencial en 429, 5xx y timeouts.

    Bug #9 fix: antes solo 429 disparaba retry; cualquier 5xx o timeout
    rompía la corrida del marketplace completo. Ahora reintentamos también
    503/504/timeouts con el mismo backoff. 4xx no-429 retornan inmediato
    (errores de cliente, no se resuelven con retry).
    """
    last_resp = None
    for attempt in range(max_retries):
        try:
            resp = httpx.get(
                url,
                params=params,
                headers={"x-amz-access-token": token},
                timeout=30,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            wait = 2 ** attempt
            print(f"[poll] WARN {type(e).__name__} on {url} — retry {attempt + 1}/{max_retries} in {wait}s")
            if attempt + 1 == max_retries:
                raise
            time.sleep(wait)
            continue

        last_resp = resp
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt  # 1s, 2s, 4s, 8s
            print(f"[poll] WARN HTTP {resp.status_code} on {url} — retry {attempt + 1}/{max_retries} in {wait}s")
            if attempt + 1 == max_retries:
                return resp
            time.sleep(wait)
            continue
        return resp
    return last_resp


def get_orders(token: str, marketplace_id: str, last_updated_after: str) -> list:
    """Obtiene órdenes de Amazon con paginación y retry en 429.

    Usa LastUpdatedAfter para capturar también órdenes antiguas que acaban
    de cambiar de estado (ej. Pending → Shipped después de varios días).
    """
    orders = []
    next_token = None

    while True:
        params = {
            "MarketplaceIds": marketplace_id,
            "LastUpdatedAfter": last_updated_after,
        }
        if next_token:
            params["NextToken"] = next_token

        resp = _sp_api_get(token, f"{AMAZON_API_BASE}/orders/v0/orders", params)

        if resp.status_code != 200:
            print(f"[poll] ERROR getting orders: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
            break

        data = resp.json()
        payload = data.get("payload", {})
        orders.extend(payload.get("Orders", []))

        next_token = payload.get("NextToken")
        if not next_token:
            break

    return orders


def get_order_items(token: str, order_id: str) -> list:
    """Obtiene items de una orden con retry en 429/5xx/timeouts y paginación NextToken.

    Bug #10 fix: antes solo leía la primera página. SP-API getOrderItems pagina
    igual que getOrders cuando una orden tiene >100 items (raro pero ocurre en
    Amazon Business). Sin paginación → factura sub-totalada, diferencia con
    Settlement.
    """
    items = []
    next_token = None
    while True:
        params = {"NextToken": next_token} if next_token else None
        resp = _sp_api_get(
            token,
            f"{AMAZON_API_BASE}/orders/v0/orders/{order_id}/orderItems",
            params=params,
        )
        if resp.status_code != 200:
            print(f"[poll] ERROR getting items for {order_id}: {resp.status_code}", file=sys.stderr)
            return items if items else []
        payload = resp.json().get("payload", {})
        items.extend(payload.get("OrderItems", []))
        next_token = payload.get("NextToken")
        if not next_token:
            break
    return items


def make_dedupe_key(order: dict, marketplace_id: str) -> str:
    """Genera clave única para deduplicación.

    Formato: amz:{marketplace_id}:{order_id}:{status}
    - Incluye marketplace_id para coincidir con el prefijo del worker.
    - Incluye status para que cada transición de estado sea un job independiente.
    - No incluye LastUpdateDate (puede variar entre polls sin cambio real de estado).
    """
    order_id = order.get("AmazonOrderId", "")
    status = order.get("OrderStatus", "")
    return f"amz:{marketplace_id}:{order_id}:{status}"


def already_processed(dedupe_key: str) -> bool:
    """Verifica si ya se procesó este evento en la tabla correcta del worker.

    Solo bloquea resultados terminales permanentes:
    - 'success'  → orden completamente procesada en Odoo, no reintentar
    - 'skipped'  → feature deshabilitada intencionalmente, no reintentar

    NO bloquea:
    - 'manual_review' → falló (ej. SKU no en Odoo): se reintenta en cada poll
      para que el sistema se auto-cure cuando se agregue el producto faltante
    - 'dead' → agotó reintentos: se reintenta para recuperación automática
    """
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM amazon_processed_events"
                " WHERE dedupe_key=? AND result IN ('success','skipped') LIMIT 1",
                (dedupe_key,)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def push_to_redis(r: redis.Redis, order: dict, dedupe_key: str) -> bool:
    """Envía orden a cola Redis."""
    job = {
        "dedupe_key": dedupe_key,
        "order_json": order,
        "source": "polling",
        "polled_at": datetime.now(timezone.utc).isoformat(),
    }
    r.rpush(QUEUE, json.dumps(job, ensure_ascii=False))
    return True


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Poll Amazon orders")
    parser.add_argument("--days", type=int, default=2, help="Days to look back via LastUpdatedAfter (default: 2)")
    parser.add_argument("--marketplace", choices=["MX", "US", "BOTH"], default="BOTH", help="Marketplace (default: BOTH)")
    parser.add_argument("--dry-run", action="store_true", help="Don't push to Redis, just show")
    args = parser.parse_args()
    
    # Check if enabled
    if get_setting("amazon_inbound_enabled", "0") != "1":
        print("[poll] Amazon inbound disabled, skipping")
        return 0
    
    # Get credentials
    creds = get_credentials()
    if not all(creds.values()):
        print("[poll] ERROR: Missing Amazon credentials in bridge_settings", file=sys.stderr)
        return 1
    
    # Get access token
    try:
        token = get_access_token(creds)
        print(f"[poll] Got access token ({len(token)} chars)")
    except Exception as e:
        print(f"[poll] ERROR getting token: {e}", file=sys.stderr)
        return 1
    
    # Connect Redis
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"[poll] ERROR connecting Redis: {e}", file=sys.stderr)
        return 1
    
    # Calculate date range — usar Z en vez de +00:00 (requerido por SP-API US)
    # LastUpdatedAfter captura órdenes que cambiaron de estado recientemente,
    # sin importar cuándo fueron creadas (cubre Pending de varios días atrás).
    last_updated_after = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"[poll] Looking for orders updated since {last_updated_after}")
    
    # Determine marketplaces
    if args.marketplace == "BOTH":
        marketplaces = list(MARKETPLACES.items())
    else:
        marketplaces = [(args.marketplace, MARKETPLACES[args.marketplace])]
    
    total_orders = 0
    total_pushed = 0
    total_skipped = 0
    
    for mp_name, mp_id in marketplaces:
        print(f"[poll] Checking {mp_name} ({mp_id})...")
        
        try:
            orders = get_orders(token, mp_id, last_updated_after)
            print(f"[poll] Found {len(orders)} orders in {mp_name}")
        except Exception as e:
            print(f"[poll] ERROR fetching {mp_name}: {e}", file=sys.stderr)
            continue
        
        for order in orders:
            total_orders += 1
            order_id = order.get("AmazonOrderId", "unknown")
            status = order.get("OrderStatus", "unknown")
            
            # Skip pending orders — EXCEPTO Flex MX (AFN + marketplace MX)
            # Flex MX necesita SO+picking aunque esté Pending (escáneo de paquetes)
            is_flex_mx = (mp_id == "A1AM78C64UM0Y8" and
                          order.get("FulfillmentChannel", "").upper() == "AFN")
            if status == "Pending" and not is_flex_mx:
                print(f"[poll]   {order_id}: status=Pending, skipping")
                total_skipped += 1
                continue
            if status == "Pending" and is_flex_mx:
                print(f"[poll]   {order_id}: status=Pending (Flex MX), encolando...")
            
            # Check dedupe
            dedupe_key = make_dedupe_key(order, mp_id)
            if already_processed(dedupe_key):
                print(f"[poll]   {order_id}: already processed, skipping")
                total_skipped += 1
                continue
            
            # Get order items
            try:
                items = get_order_items(token, order_id)
                order["OrderItems"] = items
                print(f"[poll]   {order_id}: {status}, {len(items)} items")
            except Exception as e:
                print(f"[poll]   {order_id}: ERROR getting items: {e}")
                order["OrderItems"] = []
            
            # Push to Redis
            if args.dry_run:
                print(f"[poll]   {order_id}: DRY RUN - would push")
            else:
                push_to_redis(r, order, dedupe_key)
                print(f"[poll]   {order_id}: pushed to Redis")
            
            total_pushed += 1
    
    print(f"[poll] Done. Total: {total_orders}, Pushed: {total_pushed}, Skipped: {total_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
