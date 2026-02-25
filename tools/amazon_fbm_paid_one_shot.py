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
import sqlite3
import requests

BRIDGE_DB = os.getenv("BRIDGE_DB") or "/data/bridge.db"

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
IS_USD_ORDER = os.getenv("IS_USD_ORDER", "0") == "1"

def die(msg, code=2):
    print(f"[FBM_PAID] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k, v in [("ODOO_URL", ODOO_URL), ("ODOO_DB", DB), ("ODOO_USER", USER), ("ODOO_PASS", PW), ("ORDER_JSON", ORDER_JSON_RAW), ("CLIENT_ORDER_REF", CLIENT_ORDER_REF)]:
    if not v:
        die(f"missing env {k}", code=1)

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
    die(f"bad ORDER_JSON: {e}", code=1)

order_id = str(order.get("AmazonOrderId") or "").strip()
if not order_id:
    die("ORDER_JSON missing AmazonOrderId", code=1)

# Fuente de verdad: CurrencyCode del pedido (override del env flag)
# Si OrderTotal no está presente (ej. webhook sin enrich) → usa IS_USD_ORDER del env
_order_currency = (order.get("OrderTotal") or {}).get("CurrencyCode", "").strip().upper()
if _order_currency:
    IS_USD_ORDER = (_order_currency == "USD")
    print(f"[FBM_PAID] order currency={_order_currency} IS_USD_ORDER={IS_USD_ORDER}")
    if _order_currency not in ("MXN", "USD"):
        die(f"Moneda no soportada: {_order_currency}", code=1)

def map_sku(seller_sku):
    """Busca seller_sku en amazon_sku_mapping → odoo_default_code. Retorna seller_sku si no hay mapeo."""
    try:
        con = sqlite3.connect(BRIDGE_DB)
        row = con.execute(
            "SELECT odoo_default_code FROM amazon_sku_mapping WHERE seller_sku=? LIMIT 1",
            (seller_sku,)
        ).fetchone()
        con.close()
        if row and row[0]:
            mapped = str(row[0]).strip()
            if mapped and mapped != seller_sku:
                print(f"[FBM_PAID] SKU mapped (legacy): {seller_sku} -> {mapped}")
                return mapped
    except Exception as e:
        print(f"[FBM_PAID] WARN sku mapping lookup failed for {seller_sku}: {e}", file=sys.stderr)
    return seller_sku

def parse_amount(field):
    """Extrae Amount de un campo Amazon (ItemPrice, ItemTax, ShippingPrice, etc.). Retorna 0.0 si no existe."""
    if not isinstance(field, dict):
        return 0.0
    try:
        return float(field.get("Amount") or 0)
    except (ValueError, TypeError):
        return 0.0

def parse_items(order_dict):
    """Extrae items de orden Amazon, aplicando amazon_sku_mapping para SKUs legacy.

    unit_price = Sales Proceeds / qty
    Sales Proceeds = ItemPrice + ItemTax + ShippingPrice + ShippingTax + GiftWrapPrice + GiftWrapTax
    Esto es el monto total que Amazon cobró al cliente, que es lo que debe entrar a Odoo.
    """
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
        sku = map_sku(sku)
        try:
            qty = int(it.get("QuantityOrdered", 0))
        except:
            qty = 0
        if qty <= 0:
            continue

        # Sales Proceeds: suma de todos los componentes que Amazon cobró al cliente
        item_price   = parse_amount(it.get("ItemPrice"))
        item_tax     = parse_amount(it.get("ItemTax"))
        ship_price   = parse_amount(it.get("ShippingPrice"))
        ship_tax     = parse_amount(it.get("ShippingTax"))
        giftwrap     = parse_amount(it.get("GiftWrapPrice"))
        giftwrap_tax = parse_amount(it.get("GiftWrapTax"))

        total = item_price + item_tax + ship_price + ship_tax + giftwrap + giftwrap_tax
        unit_price = round(total / qty, 6) if qty else 0.0

        print(f"[FBM_PAID] item {sku} qty={qty} product={item_price} tax={item_tax} "
              f"ship={ship_price} ship_tax={ship_tax} → total={total} unit_price={unit_price}")

        out.append({"sku": sku, "qty": qty, "unit_price": unit_price})
    return out

items = parse_items(order)
if not items:
    die("no valid items in order", code=1)

print(f"[FBM_PAID] order_id={order_id} ref={CLIENT_ORDER_REF} items={len(items)}")

# =========================
# Authenticate
# =========================
uid = jcall("common", "authenticate", [DB, USER, PW, {}])
if not uid:
    die("authentication_failed")
print(f"[FBM_PAID] authenticated uid={uid}")

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
                # En Odoo, rate = inverse_company_rate = 1/MXN_por_USD
                mxn_per_usd = round(1.0 / r, 6)
                # Sanity check: 1 USD no puede valer menos de 5 MXN ni más de 500
                if mxn_per_usd < 5.0 or mxn_per_usd > 500.0:
                    print(f"[FBM_PAID] ERROR tipo de cambio sospechoso: {mxn_per_usd} MXN/USD "
                          f"(raw rate={r}). Configura el tipo de cambio USD en Odoo correctamente.",
                          file=sys.stderr)
                    return None
                print(f"[FBM_PAID] USD/MXN rate={mxn_per_usd} (fecha={rates[0].get('name','?')})")
                return mxn_per_usd
    except Exception as e:
        print(f"[FBM_PAID] WARN no se pudo leer tipo de cambio de res.currency.rate: {e}", file=sys.stderr)
    return None

if IS_USD_ORDER:
    usd_to_mxn = get_usd_to_mxn_rate()
    if not usd_to_mxn:
        die("No se pudo obtener tipo de cambio USD/MXN de Odoo. Verifica que el tipo de cambio USD esté configurado en Odoo (Contabilidad → Divisas → USD).", code=1)
    for item in items:
        item["unit_price"] = round(item["unit_price"] * usd_to_mxn, 2)
    print(f"[FBM_PAID] precios convertidos USD→MXN (rate={usd_to_mxn})")

# =========================
# Check existing SO
# =========================
display_ref = f"{order_id} | {BUYER_NAME}" if BUYER_NAME else order_id

# Search by display_ref OR order_id-only.
# Flex MX: on Pending status Amazon does not return buyer info → SO is created with
# client_order_ref = order_id.  When the order transitions to Unshipped the buyer
# name becomes available and display_ref changes to "order_id | buyer", so a plain
# equality check would miss the existing SO and create a duplicate.
existing = exec_kw(uid, "sale.order", "search_read",
    [["|",
      ["client_order_ref", "=", display_ref],
      ["client_order_ref", "=", order_id]]],
    {"fields": ["id", "name", "state", "client_order_ref"], "limit": 1})

if existing:
    so = existing[0]
    so_id = so["id"]
    print(f"[FBM_PAID] SO exists: {so['name']} state={so['state']} — continuing to check invoice/payment")
    # If SO was created without buyer_name (Flex MX Pending) and we now have it, update the ref
    if so.get("client_order_ref") == order_id and BUYER_NAME and display_ref != order_id:
        exec_kw(uid, "sale.order", "write", [[so_id], {"client_order_ref": display_ref}])
        print(f"[FBM_PAID] client_order_ref updated: {order_id} → {display_ref}")
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
    die(f"missing products (sale_ok=true) for SKUs: {missing}", code=1)

print(f"[FBM_PAID] products resolved: {len(sku_to_pid)}")

# =========================
# Create SO if needed
# =========================
if not so_id:
    so_id = exec_kw(uid, "sale.order", "create", [{
        "partner_id": partner_id,
        "client_order_ref": display_ref,
        "note": f"{CHANNEL_LABEL} | ORDER={order_id} | {BUYER_NAME or 'N/A'}",
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
    # Si las líneas existen con precio distinto al calculado → actualizar
    # Esto cubre: $0 (Flex MX Pending), USD sin convertir (órdenes US), etc.
    current_lines = exec_kw(uid, "sale.order.line", "search_read",
        [[["order_id", "=", so_id]]],
        {"fields": ["id", "name", "price_unit"]})
    sku_price = {it["sku"]: it["unit_price"] for it in items if it["unit_price"] > 0}
    for line in current_lines:
        line_sku = line.get("name", "")
        if line_sku in sku_price and abs(line["price_unit"] - sku_price[line_sku]) > 0.01:
            exec_kw(uid, "sale.order.line", "write",
                [[line["id"]], {"price_unit": sku_price[line_sku]}])
            print(f"[FBM_PAID] line price updated: {line_sku} ${line['price_unit']} → ${sku_price[line_sku]}")

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
