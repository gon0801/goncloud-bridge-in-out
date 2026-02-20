#!/usr/bin/env python3
"""
AMAZON FBM PAID ONE SHOT — GONCLOUD BRIDGE
Crea SO + Picking + Invoice + Pago para órdenes FBM (Tú envías)

Similar a FBM de MercadoLibre:
- Crea SO y lo confirma (genera picking automáticamente)
- Crea Invoice y la paga
- NO valida el picking (queda para humano)

ENV requeridas:
- ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS
- ORDER_JSON (json de la orden Amazon)
- CLIENT_ORDER_REF (ej: AMZFBM:A1AM78C64UM0Y8:123-456-789)
"""

import os
import sys
import json
import requests

# =========================
# ENV
# =========================
ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/")
DB = os.getenv("ODOO_DB") or ""
USER = os.getenv("ODOO_USER") or ""
PW = os.getenv("ODOO_PASS") or os.getenv("ODOO_PASSWORD") or ""
ORDER_JSON_RAW = os.getenv("ORDER_JSON") or ""
CLIENT_ORDER_REF = os.getenv("CLIENT_ORDER_REF") or ""
CHANNEL_LABEL = (os.getenv("CHANNEL_LABEL") or "Amazon FBM").strip()
BUYER_NAME = (os.getenv("BUYER_NAME") or "").strip()

def die(msg, code=2):
    print(f"[FBM_PAID] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k, v in [("ODOO_URL", ODOO_URL), ("ODOO_DB", DB), ("ODOO_USER", USER), ("ODOO_PASS", PW), ("ORDER_JSON", ORDER_JSON_RAW), ("CLIENT_ORDER_REF", CLIENT_ORDER_REF)]:
    if not v:
        die(f"missing env {k}")

# =========================
# JSON-RPC helpers
# =========================
def jcall(service, method, args):
    payload = {"jsonrpc": "2.0", "method": "call", "params": {"service": service, "method": method, "args": args}, "id": 1}
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
    """Extrae items de orden Amazon"""
    items = order_dict.get("OrderItems", [])
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sku = (it.get("SellerSKU") or "").strip()
        if not sku:
            continue
        try:
            qty = int(it.get("QuantityOrdered", 0))
        except:
            qty = 0
        if qty <= 0:
            continue
        price = 0.0
        item_price = it.get("ItemPrice", {})
        if isinstance(item_price, dict):
            try:
                price = float(item_price.get("Amount", 0))
            except:
                price = 0.0
        out.append({"sku": sku, "qty": qty, "unit_price": price})
    return out

items = parse_items(order)
if not items:
    die("no valid items in order")

print(f"[FBM_PAID] order_id={order_id} ref={CLIENT_ORDER_REF} items={len(items)}")

# =========================
# Authenticate
# =========================
uid = jcall("common", "authenticate", [DB, USER, PW, {}])
if not uid:
    die("authentication_failed")
print(f"[FBM_PAID] authenticated uid={uid}")

# =========================
# Check existing SO
# =========================
existing = exec_kw(uid, "sale.order", "search_read",
    [[["client_order_ref", "=", CLIENT_ORDER_REF]]],
    {"fields": ["id", "name", "state"], "limit": 1})

if existing:
    so = existing[0]
    so_id = so["id"]
    print(f"[FBM_PAID] SO exists: {so['name']} state={so['state']} — continuing to check invoice/payment")
else:
    so_id = None

# =========================
# Get Amazon partner (create if missing)
# =========================
partner = exec_kw(uid, "res.partner", "search_read",
    [[["name", "=", CHANNEL_LABEL]]],
    {"fields": ["id"], "limit": 1})

if partner:
    partner_id = partner[0]["id"]
else:
    partner_id = exec_kw(uid, "res.partner", "create", [{"name": CHANNEL_LABEL, "customer_rank": 1}])
    print(f"[FBM_PAID] created partner '{CHANNEL_LABEL}' id={partner_id}")

# =========================
# Resolve SKUs to products
# =========================
skus = [it["sku"] for it in items]
sku_to_pid = {}

for sku in skus:
    # Gate 1: default_code con sale_ok=True (estricto)
    prods = exec_kw(uid, "product.product", "search_read",
        [[["default_code", "=", sku], ["sale_ok", "=", True]]],
        {"fields": ["id", "name"], "limit": 1})

    if prods:
        sku_to_pid[sku] = prods[0]["id"]
        continue

    # Gate 2: fallback — default_code sin restricción de sale_ok
    prods = exec_kw(uid, "product.product", "search_read",
        [[["default_code", "=", sku]]],
        {"fields": ["id", "name"], "limit": 1})

    if prods:
        print(f"[FBM_PAID] WARN SKU {sku} found but sale_ok=False, using anyway")
        sku_to_pid[sku] = prods[0]["id"]
        continue

missing = [s for s in skus if s not in sku_to_pid]
if missing:
    die(f"missing products (sale_ok=true) for SKUs: {missing}")

print(f"[FBM_PAID] products resolved: {len(sku_to_pid)}")

# =========================
# Create SO if needed
# =========================
if not so_id:
    so_id = exec_kw(uid, "sale.order", "create", [{
        "partner_id": partner_id,
        "client_order_ref": CLIENT_ORDER_REF,
        "note": f"{CHANNEL_LABEL} | ORDER={order_id}" + (f" | {BUYER_NAME}" if BUYER_NAME else ""),
    }])
    print(f"[FBM_PAID] SO created id={so_id}")

# =========================
# Create SO lines
# =========================
existing_lines = exec_kw(uid, "sale.order.line", "search_read",
    [[["order_id", "=", so_id]]],
    {"fields": ["id"], "limit": 1})

if not existing_lines:
    for it in items:
        exec_kw(uid, "sale.order.line", "create", [{
            "order_id": so_id,
            "product_id": sku_to_pid[it["sku"]],
            "product_uom_qty": it["qty"],
            "price_unit": it["unit_price"],
            "name": it["sku"],
        }])
    print(f"[FBM_PAID] SO lines created: {len(items)}")
else:
    # Si las líneas existen con precio $0 y ahora llegan precios reales → actualizar
    current_lines = exec_kw(uid, "sale.order.line", "search_read",
        [[["order_id", "=", so_id]]],
        {"fields": ["id", "name", "price_unit"]})
    sku_price = {it["sku"]: it["unit_price"] for it in items if it["unit_price"] > 0}
    for line in current_lines:
        if line["price_unit"] == 0 and line["name"] in sku_price:
            exec_kw(uid, "sale.order.line", "write",
                [[line["id"]], {"price_unit": sku_price[line["name"]]}])
            print(f"[FBM_PAID] line price updated: {line['name']} $0 → ${sku_price[line['name']]}")

# =========================
# Confirm SO (creates picking automatically)
# =========================
so = exec_kw(uid, "sale.order", "read", [[so_id], ["id", "name", "state", "picking_ids"]])[0]
if so["state"] in ("draft", "sent"):
    exec_kw(uid, "sale.order", "action_confirm", [[so_id]])
    so = exec_kw(uid, "sale.order", "read", [[so_id], ["id", "name", "state", "picking_ids"]])[0]
    print(f"[FBM_PAID] SO confirmed: {so['name']} state={so['state']} pickings={len(so.get('picking_ids', []))}")

# =========================
# Check picking exists (do NOT validate)
# =========================
pickings = so.get("picking_ids", [])
print(f"[FBM_PAID] pickings created: {len(pickings)} (NOT validated - human must validate)")

# =========================
# Si todos los precios son $0 → SO + picking listos, factura pendiente hasta que llegue precio
# =========================
current_line_prices = exec_kw(uid, "sale.order.line", "search_read",
    [[["order_id", "=", so_id]]],
    {"fields": ["price_unit"]})
if all(l["price_unit"] == 0 for l in current_line_prices):
    print(f"[FBM_PAID] SO+picking listos, precios $0 — factura diferida hasta que llegue precio")
    print(f"[FBM_PAID] OK_DONE ref={CLIENT_ORDER_REF}")
    import sys; sys.exit(0)

# =========================
# Create Invoice (manual - vinculada a SO lines)
# =========================
inv_ids = exec_kw(uid, "account.move", "search",
    [[["invoice_origin", "=", so["name"]], ["move_type", "=", "out_invoice"], ["state", "=", "posted"]]],
    {"limit": 1})

invoice = None
if inv_ids:
    invoice = exec_kw(uid, "account.move", "read", [[inv_ids[0]], ["id", "state", "payment_state", "name"]])[0]
    print(f"[FBM_PAID] invoice found: {invoice['name']}")
else:
    # Obtener líneas de la SO CON ID para vincular
    so_lines = exec_kw(uid, "sale.order.line", "search_read",
        [[["order_id", "=", so_id]]],
        {"fields": ["id", "product_id", "product_uom_qty", "price_unit", "name"]})
    
    invoice_lines = []
    for line in so_lines:
        invoice_lines.append((0, 0, {
            "product_id": line["product_id"][0],
            "quantity": line["product_uom_qty"],
            "price_unit": line["price_unit"],
            "name": line["name"],
            "sale_line_ids": [(6, 0, [line["id"]])],  # Vínculo crítico
        }))
    
    from datetime import datetime
    inv_id = exec_kw(uid, "account.move", "create", [{
        "move_type": "out_invoice",
        "partner_id": partner_id,
        "invoice_origin": so["name"],
        "invoice_date": datetime.now().strftime("%Y-%m-%d"),
        "invoice_line_ids": invoice_lines,
    }])
    
    invoice = exec_kw(uid, "account.move", "read", [[inv_id], ["id", "state", "name"]])[0]
    print(f"[FBM_PAID] invoice created: {invoice['name']}")
    
    if invoice["state"] == "draft":
        exec_kw(uid, "account.move", "action_post", [[inv_id]])
        invoice = exec_kw(uid, "account.move", "read", [[inv_id], ["id", "state", "payment_state", "name"]])[0]
        print(f"[FBM_PAID] invoice posted: {invoice['name']}")

# =========================
# Pay Invoice
# =========================
if invoice and invoice.get("payment_state") != "paid":
    invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id", "state", "payment_state", "amount_residual"]])[0]
    
    if invoice["payment_state"] != "paid" and invoice["state"] == "posted":
        journals = exec_kw(uid, "account.journal", "search_read",
            [[["type", "=", "bank"]]],
            {"fields": ["id", "name"], "limit": 1})
        
        if journals:
            ctx = {"active_model": "account.move", "active_ids": [invoice["id"]], "active_id": invoice["id"]}
            pay_wiz = exec_kw(uid, "account.payment.register", "create",
                [{"journal_id": journals[0]["id"]}], {"context": ctx})
            exec_kw(uid, "account.payment.register", "action_create_payments", [[pay_wiz]], {"context": ctx})
            
            invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id", "payment_state"]])[0]
            print(f"[FBM_PAID] invoice paid: payment_state={invoice['payment_state']}")

print(f"[FBM_PAID] OK_DONE ref={CLIENT_ORDER_REF}")
