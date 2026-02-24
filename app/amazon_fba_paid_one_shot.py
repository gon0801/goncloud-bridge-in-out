#!/usr/bin/env python3
"""
AMAZON FBA PAID ONE SHOT — GONCLOUD BRIDGE
Crea SO contable + Invoice + Pago para órdenes FBA (Amazon envía)

Similar a FULL de MercadoLibre: NO crea picking, NO mueve stock.
Amazon ya tiene la mercancía y la envía.

ENV requeridas:
- ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS (o ODOO_PASSWORD)
- ORDER_JSON (json de la orden Amazon)
- CLIENT_ORDER_REF (ej: AMZFBA:A1AM78C64UM0Y8:123-456-789)
"""

import os
import sys
import json
import sqlite3
import requests

from datetime import datetime, timezone

# =========================
# ENV
# =========================
ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/")
DB = os.getenv("ODOO_DB") or ""
USER = os.getenv("ODOO_USER") or ""
PW = os.getenv("ODOO_PASS") or os.getenv("ODOO_PASSWORD") or ""
ORDER_JSON_RAW = os.getenv("ORDER_JSON") or ""
CLIENT_ORDER_REF = os.getenv("CLIENT_ORDER_REF") or ""
CHANNEL_LABEL = (os.getenv("CHANNEL_LABEL") or "Amazon FBA").strip()
BUYER_NAME = (os.getenv("BUYER_NAME") or "").strip()

# DB bridge (para mapeo amazon_sku_mapping)
BRIDGE_DB = os.getenv("BRIDGE_DB") or "/data/bridge.db"

def die(msg, code=2):
    print(f"[FBA_PAID] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k, v in [
    ("ODOO_URL", ODOO_URL),
    ("ODOO_DB", DB),
    ("ODOO_USER", USER),
    ("ODOO_PASS", PW),
    ("ORDER_JSON", ORDER_JSON_RAW),
    ("CLIENT_ORDER_REF", CLIENT_ORDER_REF),
]:
    if not v:
        die(f"missing env {k}")

# =========================
# JSON-RPC helpers
# =========================
def jcall(service, method, args):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1,
    }
    r = requests.post(ODOO_URL + "/jsonrpc", json=payload, timeout=45)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(json.dumps(d["error"], ensure_ascii=False))
    return d.get("result")

def exec_kw(uid, model, method, args=None, kwargs=None):
    args = args or []
    kwargs = kwargs or {}
    return jcall("object", "execute_kw", [DB, uid, PW, model, method, args, kwargs])

# =========================
# Bridge DB helper (SKU mapping)
# =========================
def map_amazon_sku_to_odoo_default_code(seller_sku: str) -> str:
    """
    Si existe mapeo en amazon_sku_mapping: seller_sku -> odoo_default_code, lo usa.
    Si no existe, regresa seller_sku tal cual.
    """
    seller_sku = (seller_sku or "").strip()
    if not seller_sku:
        return ""

    try:
        con = sqlite3.connect(BRIDGE_DB)
        cur = con.cursor()
        cur.execute(
            "SELECT odoo_default_code FROM amazon_sku_mapping WHERE seller_sku=? LIMIT 1",
            (seller_sku,),
        )
        row = cur.fetchone()
        con.close()
        if row and row[0]:
            return str(row[0]).strip()
    except Exception as e:
        # No tronamos el flujo por mapeo; solo avisamos.
        print(f"[FBA_PAID] WARN sku mapping failed for {seller_sku}: {e}", file=sys.stderr)

    return seller_sku

# =========================
# Parse order
# =========================
try:
    order = json.loads(ORDER_JSON_RAW)
    if not isinstance(order, dict):
        raise ValueError("not_dict")
except Exception as e:
    die(f"bad ORDER_JSON: {e}")

order_id = str(order.get("AmazonOrderId") or "").strip()
if not order_id:
    die("ORDER_JSON missing AmazonOrderId")

def parse_items(order_dict):
    """Extrae items de orden Amazon + mapea SellerSKU -> odoo_default_code usando bridge.db"""
    import sqlite3

    db_path = os.getenv("BRIDGE_DB") or "/data/bridge.db"
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
    except Exception as e:
        die(f"cannot open BRIDGE_DB at {db_path}: {e}")

    items = order_dict.get("OrderItems", [])
    if not isinstance(items, list):
        return []

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue

        seller_sku = (it.get("SellerSKU") or "").strip()
        if not seller_sku:
            continue

        # === MAPEO AMAZON SKU → ODOO DEFAULT_CODE ===
        try:
            row = db.execute(
                "SELECT odoo_default_code FROM amazon_sku_mapping WHERE seller_sku=?",
                (seller_sku,)
            ).fetchone()
            if row and row["odoo_default_code"]:
                mapped = str(row["odoo_default_code"]).strip()
                if mapped and mapped != seller_sku:
                    print(f"[FBA_PAID] MAPPED Amazon SKU → Odoo: {seller_sku} -> {mapped}")
                    seller_sku = mapped
        except Exception as e:
            die(f"sku mapping query failed for {seller_sku}: {e}")

        try:
            qty = int(it.get("QuantityOrdered", 0))
        except Exception:
            qty = 0
        if qty <= 0:
            continue

        price = 0.0
        item_price = it.get("ItemPrice", {})
        if isinstance(item_price, dict):
            try:
                price = float(item_price.get("Amount", 0) or 0)
            except Exception:
                price = 0.0

        unit_price = round(price / qty, 6) if qty else 0.0
        out.append({"sku": seller_sku, "qty": qty, "unit_price": unit_price})

    return out

items = parse_items(order)
if not items:
    die("no valid items in order")

print(f"[FBA_PAID] order_id={order_id} ref={CLIENT_ORDER_REF} items={len(items)}")

# =========================
# Authenticate
# =========================
uid = jcall("common", "authenticate", [DB, USER, PW, {}])
if not uid:
    die("authentication_failed")
print(f"[FBA_PAID] authenticated uid={uid}")

# =========================
# USD → MXN conversion (solo órdenes US)
# =========================
def get_usd_to_mxn_rate():
    """Lee el tipo de cambio USD/MXN desde Odoo (res.currency.rate).
    Retorna MXN por 1 USD, ej. 17.5. None si no se puede obtener.

    En Odoo 17, res.currency.rate.rate = inverse_company_rate = USD por 1 MXN
    (ej. rate=0.05714 si 1 USD = 17.5 MXN).
    Por eso mxn_per_usd = 1 / rate.

    SANITY CHECK: si el resultado es < 5 o > 500, el tipo de cambio no está
    configurado correctamente en Odoo (ej. rate=1.0 por default) → falla con
    code=1 (manual_review) para evitar registrar precios incorrectos.
    """
    try:
        rates = exec_kw(uid, "res.currency.rate", "search_read",
            [[["currency_id.name", "=", "USD"]]],
            {"fields": ["rate", "name"], "order": "name desc", "limit": 1})
        if rates and rates[0].get("rate"):
            r = float(rates[0]["rate"])
            if r > 0:
                mxn_per_usd = round(1.0 / r, 6)
                # Sanity check: 1 USD no puede valer menos de 5 MXN ni más de 500
                if mxn_per_usd < 5.0 or mxn_per_usd > 500.0:
                    print(f"[FBA_PAID] ERROR tipo de cambio sospechoso: {mxn_per_usd} MXN/USD "
                          f"(raw rate={r}). Configura el tipo de cambio USD en Odoo correctamente.",
                          file=sys.stderr)
                    return None
                print(f"[FBA_PAID] USD/MXN rate={mxn_per_usd} (fecha={rates[0].get('name','?')})")
                return mxn_per_usd
    except Exception as e:
        print(f"[FBA_PAID] WARN no se pudo leer tipo de cambio: {e}", file=sys.stderr)
    return None

if IS_USD_ORDER:
    usd_to_mxn = get_usd_to_mxn_rate()
    if not usd_to_mxn:
        die("No se pudo obtener tipo de cambio USD/MXN de Odoo. Verifica que el tipo de cambio USD esté configurado en Odoo (Contabilidad → Divisas → USD).", code=1)
    for item in items:
        item["unit_price"] = round(item["unit_price"] * usd_to_mxn, 2)
    print(f"[FBA_PAID] precios convertidos USD→MXN (rate={usd_to_mxn})")

# =========================
# Check existing SO
# =========================
display_ref = f"{order_id} | {BUYER_NAME}" if BUYER_NAME else order_id
existing = exec_kw(
    uid, "sale.order", "search_read",
    [[["client_order_ref", "=", display_ref]]],
    {"fields": ["id", "name", "state"], "limit": 1},
)

if existing:
    so = existing[0]
    so_id = so["id"]
    print(f"[FBA_PAID] SO exists: {so['name']} state={so['state']}")
else:
    so_id = None

# =========================
# Get Amazon partner (create if missing)
# =========================
partner = exec_kw(
    uid, "res.partner", "search_read",
    [[["name", "=", CHANNEL_LABEL]]],
    {"fields": ["id"], "limit": 1},
)

if partner:
    partner_id = partner[0]["id"]
else:
    partner_id = exec_kw(uid, "res.partner", "create", [{"name": CHANNEL_LABEL, "customer_rank": 1}])
    print(f"[FBA_PAID] created partner id={partner_id}")

# =========================
# Resolve SKUs to products
# =========================
skus = [it["sku"] for it in items]
sku_to_pid = {}

for sku in skus:
    prods = exec_kw(
        uid, "product.product", "search_read",
        [[["default_code", "=", sku], ["sale_ok", "=", True]]],
        {"fields": ["id", "name"], "limit": 1},
    )
    if prods:
        sku_to_pid[sku] = prods[0]["id"]
        continue

missing = [s for s in skus if s not in sku_to_pid]
if missing:
    die(f"missing products (sale_ok=true) for SKUs: {missing}")

print(f"[FBA_PAID] products resolved: {len(sku_to_pid)}")

# =========================
# Create SO if needed
# =========================
if not so_id:
    so_id = exec_kw(uid, "sale.order", "create", [{
        "partner_id": partner_id,
        "client_order_ref": display_ref,
        "note": f"{CHANNEL_LABEL} | #{order_id}" + (f" | {BUYER_NAME}" if BUYER_NAME else ""),
    }])
    print(f"[FBA_PAID] SO created id={so_id}")

# =========================
# Create SO lines
# =========================
existing_lines = exec_kw(
    uid, "sale.order.line", "search_read",
    [[["order_id", "=", so_id]]],
    {"fields": ["id"], "limit": 1},
)

if not existing_lines:
    for it in items:
        # name: ponemos el SKU Odoo (mapped) y si difiere, anotamos el Amazon original.
        line_name = it["sku"]
        if it.get("amazon_sku") and it["amazon_sku"] != it["sku"]:
            line_name = f"{it['sku']} (amz:{it['amazon_sku']})"

        exec_kw(uid, "sale.order.line", "create", [{
            "order_id": so_id,
            "product_id": sku_to_pid[it["sku"]],
            "product_uom_qty": it["qty"],
            "price_unit": it["unit_price"],
            "name": line_name,
        }])
    print(f"[FBA_PAID] SO lines created: {len(items)}")

# =========================
# Confirm SO
# =========================
so = exec_kw(uid, "sale.order", "read", [[so_id], ["id", "name", "state"]])[0]
if so["state"] in ("draft", "sent"):
    exec_kw(uid, "sale.order", "action_confirm", [[so_id]])
    so = exec_kw(uid, "sale.order", "read", [[so_id], ["id", "name", "state"]])[0]
    print(f"[FBA_PAID] SO confirmed: {so['name']} state={so['state']}")

# =========================
# Cancel pickings (FBA = no stock movement)
# =========================
pickings = exec_kw(
    uid, "stock.picking", "search_read",
    [[["origin", "=", so["name"]], ["state", "not in", ["done", "cancel"]]]],
    {"fields": ["id", "name", "state"]},
)

for pick in pickings:
    try:
        exec_kw(uid, "stock.picking", "action_cancel", [[pick["id"]]])
        print(f"[FBA_PAID] picking cancelled: {pick['name']}")
    except Exception as e:
        print(f"[FBA_PAID] WARN picking cancel failed: {pick['name']} err={e}")

# =========================
# Create Invoice
# =========================
inv_ids = exec_kw(
    uid, "account.move", "search",
    [[["invoice_origin", "=", so["name"]], ["move_type", "=", "out_invoice"]]],
    {"limit": 5},
)

invoice = None
for iid in inv_ids:
    m = exec_kw(uid, "account.move", "read", [[iid], ["id", "state", "payment_state", "name"]])[0]
    if m["state"] == "posted":
        invoice = m
        break

if not invoice:
    ctx = {"active_model": "sale.order", "active_ids": [so_id], "active_id": so_id}
    wiz_id = exec_kw(
        uid, "sale.advance.payment.inv", "create",
        [{"advance_payment_method": "delivered"}],
        {"context": ctx},
    )
    exec_kw(uid, "sale.advance.payment.inv", "create_invoices", [[wiz_id]], {"context": ctx})

    inv_ids = exec_kw(
        uid, "account.move", "search",
        [[["invoice_origin", "=", so["name"]], ["move_type", "=", "out_invoice"]]],
        {"limit": 1},
    )

    if inv_ids:
        invoice = exec_kw(uid, "account.move", "read", [[inv_ids[0]], ["id", "state", "name"]])[0]
        print(f"[FBA_PAID] invoice created: {invoice['name']}")

        if invoice["state"] == "draft":
            exec_kw(uid, "account.move", "action_post", [[invoice["id"]]])
            invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id", "state", "payment_state", "name"]])[0]
            print(f"[FBA_PAID] invoice posted: {invoice['name']}")

# =========================
# Pay Invoice
# =========================
if invoice and invoice.get("payment_state") != "paid":
    invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id", "state", "payment_state", "amount_residual"]])[0]

    if invoice["payment_state"] != "paid" and invoice["state"] == "posted":
        journals = exec_kw(
            uid, "account.journal", "search_read",
            [[["type", "=", "bank"]]],
            {"fields": ["id", "name"], "limit": 1},
        )

        if journals:
            ctx = {"active_model": "account.move", "active_ids": [invoice["id"]], "active_id": invoice["id"]}
            pay_wiz = exec_kw(
                uid, "account.payment.register", "create",
                [{"journal_id": journals[0]["id"]}],
                {"context": ctx},
            )
            exec_kw(uid, "account.payment.register", "action_create_payments", [[pay_wiz]], {"context": ctx})

            invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id", "payment_state"]])[0]
            print(f"[FBA_PAID] invoice paid: payment_state={invoice['payment_state']}")

print(f"[FBA_PAID] OK_DONE ref={CLIENT_ORDER_REF}")
