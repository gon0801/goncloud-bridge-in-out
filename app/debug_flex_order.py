import json
import sqlite3
import httpx
import sys
from datetime import datetime, timezone

if len(sys.argv) != 2:
    print("Usage: python3 debug_flex_order.py ORDER_ID")
    sys.exit(1)

ORDER_ID = sys.argv[1]
DB = "/data/bridge.db"
BASE = "https://sellingpartnerapi-na.amazon.com"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
INCLUDED_DATA = "BUYER,PROCEEDS,FULFILLMENT,PACKAGES"

# 1) Leer credenciales desde bridge_settings
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT key,value FROM bridge_settings WHERE key LIKE 'amazon_sp_api_%'"
).fetchall()
settings = {r["key"]: (r["value"] or "") for r in rows}

client_id = settings.get("amazon_sp_api_client_id", "")
client_secret = settings.get("amazon_sp_api_client_secret", "")
refresh_token = settings.get("amazon_sp_api_refresh_token", "")

if not (client_id and client_secret and refresh_token):
    print(
        "ERROR: Missing amazon_sp_api_client_id / amazon_sp_api_client_secret / amazon_sp_api_refresh_token in bridge_settings"
    )
    sys.exit(1)

# 2) Obtener access token
r = httpx.post(
    TOKEN_URL,
    data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    },
    timeout=30,
)
r.raise_for_status()
access_token = r.json()["access_token"]

# 3) Obtener orden (v2026-01-01 — items embebidos)
url = f"{BASE}/orders/2026-01-01/orders/{ORDER_ID}"
headers = {"x-amz-access-token": access_token, "Accept": "application/json"}

resp = httpx.get(
    url, headers=headers, params={"includedData": INCLUDED_DATA}, timeout=30
)
print("HTTP", resp.status_code)
resp.raise_for_status()

payload = resp.json()
raw_order = payload.get("order", {})

# 4) Normalizar a v0 para verificar que el adapter funciona
_V2026_STATUS_MAP = {
    "PENDING": "Pending",
    "PENDING_AVAILABILITY": "Pending",
    "UNSHIPPED": "Unshipped",
    "PARTIALLY_SHIPPED": "PartiallyShipped",
    "SHIPPED": "Shipped",
    "INVOICE_UNCONFIRMED": "InvoiceUnconfirmed",
    "CANCELLED": "Canceled",
    "UNFULFILLABLE": "Unfulfillable",
}


def normalize_order(order: dict) -> dict:
    fulfillment = order.get("fulfillment") or {}
    fc = "AFN" if fulfillment.get("fulfilledBy") == "AMAZON" else "MFN"
    sales_channel = order.get("salesChannel") or {}
    programs = order.get("programs") or []
    fs = fulfillment.get("fulfillmentStatus", "")
    proceeds = order.get("proceeds") or {}
    grand_total = proceeds.get("grandTotal") or {}
    v0 = {
        "AmazonOrderId": order.get("orderId", ""),
        "PurchaseDate": order.get("purchaseDate", ""),
        "LastUpdateDate": order.get("lastUpdatedTime", ""),
        "OrderStatus": _V2026_STATUS_MAP.get(fs, fs),
        "FulfillmentChannel": fc,
        "MarketplaceId": sales_channel.get("marketplaceId", ""),
        "SalesChannel": sales_channel.get("channelType", ""),
        "OrderTotal": {
            "CurrencyCode": grand_total.get("currencyCode", ""),
            "Amount": str(grand_total.get("amount", "0")),
        },
        "OrderItems": len(order.get("orderItems") or []),
    }
    if "AMAZON_EASY_SHIP" in programs:
        v0["EasyShipShipmentStatus"] = "PendingPickUp"
    return v0


print("\n=== KEY FIELDS (v2026 raw) ===")
raw_keys = [
    "orderId",
    "purchaseDate",
    "lastUpdatedTime",
    "programs",
    "salesChannel",
    "fulfillment",
    "proceeds",
]
for k in raw_keys:
    if k in raw_order:
        print(f"{k}: {raw_order[k]}")

print("\n=== NORMALIZED TO v0 ===")
norm = normalize_order(raw_order)
for k, v in norm.items():
    print(f"{k}: {v}")

print(f"\n=== ORDER ITEMS ({len(raw_order.get('orderItems') or [])} items) ===")
for i, item in enumerate(raw_order.get("orderItems") or []):
    prod = item.get("product") or {}
    print(
        f"  [{i}] SKU={prod.get('sellerSku')} qty={item.get('quantityOrdered')} breakdowns={[b.get('type') for b in item.get('breakdowns') or []]}"
    )

print("\n=== FULL JSON SAVED ===")
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = f"/data/amz_order_{ORDER_ID}_{ts}.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)

print(out)
