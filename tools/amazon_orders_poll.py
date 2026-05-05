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

INCLUDED_DATA = "BUYER,PROCEEDS,FULFILLMENT,PACKAGES"

_V2026_STATUS_MAP = {
    "PENDING":              "Pending",
    "PENDING_AVAILABILITY": "Pending",
    "UNSHIPPED":            "Unshipped",
    "PARTIALLY_SHIPPED":    "PartiallyShipped",
    "SHIPPED":              "Shipped",
    "INVOICE_UNCONFIRMED":  "InvoiceUnconfirmed",
    "CANCELLED":            "Canceled",
    "UNFULFILLABLE":        "Unfulfillable",
}


def _normalize_item_to_v0(item: dict) -> dict:
    """Convert a v2026-01-01 orderItem to v0 schema."""
    product = item.get("product") or {}
    ip = {"Amount": "0", "CurrencyCode": ""}
    itax = {"Amount": "0", "CurrencyCode": ""}
    sp = {"Amount": "0", "CurrencyCode": ""}
    stax = {"Amount": "0", "CurrencyCode": ""}
    gwp = {"Amount": "0", "CurrencyCode": ""}
    gwtax = {"Amount": "0", "CurrencyCode": ""}
    pdis = {"Amount": "0", "CurrencyCode": ""}

    for bd in item.get("breakdowns") or []:
        bt = bd.get("type", "")
        m = bd.get("amount") or {}
        a, c = str(m.get("amount", "0")), m.get("currencyCode", "")
        if bt == "ITEM":
            ip = {"Amount": a, "CurrencyCode": c}
        elif bt == "SHIPPING":
            sp = {"Amount": a, "CurrencyCode": c}
        elif bt == "DISCOUNT":
            pdis = {"Amount": a, "CurrencyCode": c}
        elif bt == "TAX":
            for dbd in bd.get("detailedBreakdowns") or []:
                dt = dbd.get("type", "")
                dm = dbd.get("amount") or {}
                da, dc = str(dm.get("amount", "0")), dm.get("currencyCode", c)
                if dt == "ITEM_TAX":
                    itax = {"Amount": da, "CurrencyCode": dc}
                elif dt == "SHIPPING_TAX":
                    stax = {"Amount": da, "CurrencyCode": dc}
                elif dt == "GIFT_WRAP_TAX":
                    gwtax = {"Amount": da, "CurrencyCode": dc}
                elif dt == "GIFT_WRAP":
                    gwp = {"Amount": da, "CurrencyCode": dc}

    return {
        "ASIN":             product.get("asin", ""),
        "SellerSKU":        product.get("sellerSku", ""),
        "OrderItemId":      item.get("orderItemId", ""),
        "Title":            product.get("productName", ""),
        "QuantityOrdered":  item.get("quantityOrdered", 0),
        "QuantityShipped":  item.get("quantityShipped", 0),
        "ItemPrice":        ip,
        "ItemTax":          itax,
        "ShippingPrice":    sp,
        "ShippingTax":      stax,
        "GiftWrapPrice":    gwp,
        "GiftWrapTax":      gwtax,
        "PromotionDiscount": pdis,
    }


def normalize_to_v0(order: dict) -> dict:
    """Convert a v2026-01-01 order object to v0 schema.

    Adapter so all downstream tools (amazon_fba_paid_one_shot.py, etc.)
    continue reading AmazonOrderId, FulfillmentChannel, OrderItems, etc.
    """
    fulfillment = order.get("fulfillment") or {}
    fc = "AFN" if fulfillment.get("fulfilledBy") == "AMAZON" else "MFN"

    sales_channel = order.get("salesChannel") or {}
    marketplace_id = sales_channel.get("marketplaceId", "")

    programs = order.get("programs") or []
    easy_ship = "AMAZON_EASY_SHIP" in programs

    fs = fulfillment.get("fulfillmentStatus", "")
    order_status = _V2026_STATUS_MAP.get(fs, fs.title() if fs else "")

    proceeds = order.get("proceeds") or {}
    grand_total = proceeds.get("grandTotal") or {}

    buyer = order.get("buyer") or {}
    buyer_info: dict = {}
    if buyer.get("buyerName"):
        buyer_info["BuyerName"] = buyer["buyerName"]
    if buyer.get("buyerEmail"):
        buyer_info["BuyerEmail"] = buyer["buyerEmail"]

    delivery = fulfillment.get("deliveryAddress") or {}
    shipping_address: dict = {}
    if delivery:
        shipping_address = {
            "Name":          delivery.get("name", ""),
            "City":          delivery.get("city", ""),
            "StateOrRegion": delivery.get("stateOrRegion", ""),
            "PostalCode":    delivery.get("postalCode", ""),
            "CountryCode":   delivery.get("countryCode", ""),
        }

    v0 = {
        "AmazonOrderId":    order.get("orderId", ""),
        "PurchaseDate":     order.get("purchaseDate", ""),
        "LastUpdateDate":   order.get("lastUpdatedTime", ""),
        "OrderStatus":      order_status,
        "FulfillmentChannel": fc,
        "MarketplaceId":    marketplace_id,
        "SalesChannel":     sales_channel.get("channelType", ""),
        "OrderTotal": {
            "CurrencyCode": grand_total.get("currencyCode", ""),
            "Amount":       str(grand_total.get("amount", "0")),
        },
        "BuyerInfo":        buyer_info,
        "ShippingAddress":  shipping_address,
        "OrderItems":       [_normalize_item_to_v0(i) for i in (order.get("orderItems") or [])],
    }
    if easy_ship:
        v0["EasyShipShipmentStatus"] = "PendingPickUp"
    return v0


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
    """Obtiene órdenes de Amazon SP-API v2026-01-01 con paginación.

    v2026 incluye items embebidos vía includedData, eliminando la necesidad
    de llamar getOrderItems por separado.
    """
    orders = []
    pagination_token = None

    while True:
        params = {
            "marketplaceIds":  marketplace_id,
            "lastUpdatedAfter": last_updated_after,
            "includedData":    INCLUDED_DATA,
        }
        if pagination_token:
            params["paginationToken"] = pagination_token

        resp = _sp_api_get(token, f"{AMAZON_API_BASE}/orders/2026-01-01/orders", params)

        if resp.status_code != 200:
            print(f"[poll] ERROR getting orders: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
            break

        data = resp.json()
        orders.extend(data.get("orders", []))

        pagination_token = data.get("paginationToken")
        if not pagination_token:
            break

    return orders


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
        
        for order_raw in orders:
            # Normalize to v0 first so all downstream fields/logic work unchanged
            order = normalize_to_v0(order_raw)
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

            items = order.get("OrderItems", [])
            fc = order.get("FulfillmentChannel", "?")
            print(f"[poll]   {order_id}: status={status} fc={fc} items={len(items)}")

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
