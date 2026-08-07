#!/usr/bin/env python3
"""
Test rápido: llama SP-API v2026 para órdenes conocidas de cada perfil
y muestra el resultado de normalize_to_v0() sin tocar Odoo.

Uso:
  python3 test_v2026_normalize.py
"""

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

DB = os.getenv("BRIDGE_DB", "/data/bridge.db")
BASE = "https://sellingpartnerapi-na.amazon.com"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
INCLUDED_DATA = "BUYER,PROCEEDS,FULFILLMENT,PACKAGES"

# ── Órdenes conocidas para probar cada perfil ──────────────────────────────
TEST_ORDERS = [
    # (order_id,          descripcion)
    ("702-3596162-5550625", "FLEX_MX  (AFN + MX  + Shipped)"),
    ("701-8809471-1320260", "FBM_MX   (MFN + MX  + Shipped)"),
    ("113-7084185-9541054", "FBM_US   (MFN + US  + Unshipped)"),
    ("112-7100755-5535413", "FBA_US   (AFN + US  + Shipped)"),
]

# ── Normalizer (copia inline) ───────────────────────────────────────────────
_STATUS_MAP = {
    "PENDING": "Pending",
    "PENDING_AVAILABILITY": "Pending",
    "UNSHIPPED": "Unshipped",
    "PARTIALLY_SHIPPED": "PartiallyShipped",
    "SHIPPED": "Shipped",
    "INVOICE_UNCONFIRMED": "InvoiceUnconfirmed",
    "CANCELLED": "Canceled",
    "UNFULFILLABLE": "Unfulfillable",
}


def _norm_item(item):
    product = item.get("product") or {}
    prices = {
        "ITEM": "0",
        "SHIPPING": "0",
        "DISCOUNT": "0",
        "ITEM_TAX": "0",
        "SHIPPING_TAX": "0",
    }
    currency = ""
    for bd in (item.get("proceeds") or {}).get("breakdowns") or []:
        bt = bd.get("type", "")
        m = bd.get("subtotal") or {}
        a, c = str(m.get("amount", "0")), m.get("currencyCode", "")
        if c:
            currency = c
        if bt in ("ITEM", "SHIPPING", "DISCOUNT"):
            prices[bt] = a
        elif bt == "TAX":
            for dbd in bd.get("detailedBreakdowns") or []:
                dt = dbd.get("subtype", "")
                dm = dbd.get("value") or {}
                if dt == "ITEM":
                    prices["ITEM_TAX"] = str(dm.get("amount", "0"))
                elif dt == "SHIPPING":
                    prices["SHIPPING_TAX"] = str(dm.get("amount", "0"))
    total = sum(
        float(prices[k]) for k in ("ITEM", "SHIPPING", "ITEM_TAX", "SHIPPING_TAX")
    )
    return {
        "SellerSKU": product.get("sellerSku", ""),
        "qty": item.get("quantityOrdered", 0),
        "currency": currency,
        "ItemPrice": prices["ITEM"],
        "ShipPrice": prices["SHIPPING"],
        "ItemTax": prices["ITEM_TAX"],
        "ShipTax": prices["SHIPPING_TAX"],
        "Discount": prices["DISCOUNT"],
        "TOTAL_LINE": round(total, 2),
    }


def normalize(order):
    f = order.get("fulfillment") or {}
    sc = order.get("salesChannel") or {}
    fc = "AFN" if f.get("fulfilledBy") == "AMAZON" else "MFN"
    fs = f.get("fulfillmentStatus", "")
    pr = order.get("proceeds") or {}
    gt = pr.get("grandTotal") or {}
    programs = order.get("programs") or []
    items = [_norm_item(i) for i in (order.get("orderItems") or [])]
    return {
        "orderId": order.get("orderId"),
        "status": _STATUS_MAP.get(fs, fs),
        "fc": fc,
        "marketplace": sc.get("marketplaceId"),
        "easy_ship": "AMAZON_EASY_SHIP" in programs,
        "programs": programs,
        "currency": gt.get("currencyCode"),
        "grandTotal": gt.get("amount"),
        "items": items,
        "items_total": round(sum(i["TOTAL_LINE"] for i in items), 2),
    }


# ── SP-API helpers ──────────────────────────────────────────────────────────
def get_setting(conn, key):
    row = conn.execute(
        "SELECT value FROM bridge_settings WHERE key=?", (key,)
    ).fetchone()
    return str(row[0]) if row else ""


def get_token(conn):
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": get_setting(conn, "amazon_sp_api_refresh_token"),
            "client_id": get_setting(conn, "amazon_sp_api_client_id"),
            "client_secret": get_setting(conn, "amazon_sp_api_client_secret"),
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def spapi_get(token, order_id):
    url = f"{BASE}/orders/2026-01-01/orders/{order_id}?includedData={INCLUDED_DATA}"
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"x-amz-access-token": token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2**attempt)
                continue
            print(f"  HTTP {e.code}: {e.read()[:200]}")
            return None
    return None


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    conn = sqlite3.connect(DB)
    print("Obteniendo token SP-API v2026-01-01...")
    token = get_token(conn)
    print(f"Token OK ({len(token)} chars)\n")

    for order_id, desc in TEST_ORDERS:
        print(f"{'=' * 65}")
        print(f"ORDEN: {order_id}  [{desc}]")
        print(f"{'=' * 65}")

        raw = spapi_get(token, order_id)
        if not raw:
            print("  ERROR: sin respuesta de la API\n")
            continue

        order_raw = raw.get("order") or {}
        if not order_raw:
            print(f"  ERROR: clave 'order' vacía. Keys: {list(raw.keys())}\n")
            continue

        n = normalize(order_raw)
        print(f"  orderId    : {n['orderId']}")
        print(f"  status     : {n['status']}")
        print(
            f"  fc         : {n['fc']}  ({'FBA path' if n['fc'] == 'AFN' else 'FBM path'})"
        )
        print(f"  marketplace: {n['marketplace']}")
        print(f"  easy_ship  : {n['easy_ship']}  programs={n['programs']}")
        print(f"  currency   : {n['currency']}")
        print(f"  grandTotal : {n['grandTotal']}")
        print(f"  items ({len(n['items'])}):")
        for i, it in enumerate(n["items"]):
            print(
                f"    [{i}] SKU={it['SellerSKU']} qty={it['qty']} curr={it['currency']}"
            )
            print(f"         ItemPrice={it['ItemPrice']}  ItemTax={it['ItemTax']}")
            print(f"         ShipPrice={it['ShipPrice']}  ShipTax={it['ShipTax']}")
            print(
                f"         Discount={it['Discount']}  → LINE_TOTAL={it['TOTAL_LINE']}"
            )
        print(f"  ITEMS_TOTAL: {n['items_total']} {n['currency']}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
