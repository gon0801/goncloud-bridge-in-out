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

# 1) Leer credenciales desde bridge_settings
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT key,value FROM bridge_settings WHERE key LIKE 'amazon_%'"
).fetchall()
settings = {r["key"]: (r["value"] or "") for r in rows}

client_id = settings.get("amazon_client_id", "")
client_secret = settings.get("amazon_client_secret", "")
refresh_token = settings.get("amazon_refresh_token", "")

if not (client_id and client_secret and refresh_token):
    print("ERROR: Missing Amazon credentials in bridge_settings")
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

# 3) Obtener orden
url = f"{BASE}/orders/v0/orders/{ORDER_ID}"
headers = {
    "Authorization": f"Bearer {access_token}",
    "x-amz-access-token": access_token,
    "Accept": "application/json",
}

resp = httpx.get(url, headers=headers, timeout=30)
print("HTTP", resp.status_code)
resp.raise_for_status()

payload = resp.json()
order = payload.get("payload", {})

print("\n=== KEY FIELDS ===")
keys = [
    "AmazonOrderId",
    "MarketplaceId",
    "SalesChannel",
    "OrderStatus",
    "OrderType",
    "FulfillmentChannel",
    "ShipServiceLevel",
    "ShipmentServiceLevelCategory",
    "PaymentMethod",
    "PaymentMethodDetails",
    "IsPrime",
    "IsPremiumOrder",
    "EarliestShipDate",
    "LatestShipDate",
    "PurchaseDate",
    "LastUpdateDate",
    "EasyShipShipmentStatus",
    "IsBusinessOrder",
    "IsSoldByAB",
    "IsGlobalExpressEnabled",
    "NumberOfItemsUnshipped",
    "NumberOfItemsShipped",
]

for k in keys:
    if k in order:
        print(f"{k}: {order[k]}")

print("\n=== FULL JSON SAVED ===")
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = f"/data/amz_order_{ORDER_ID}_{ts}.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)

print(out)
