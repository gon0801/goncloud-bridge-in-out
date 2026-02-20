#!/usr/bin/env python3
import os, sys, json, requests
from datetime import datetime, timezone

# =========================
# ENV required
# =========================
ODOO_URL = os.getenv("ODOO_URL", "").rstrip("/")
DB       = os.getenv("ODOO_DB", "")
USER     = os.getenv("ODOO_USER", "")
PW       = os.getenv("ODOO_PASSWORD", "")
ORDER_ID = os.getenv("ORDER_ID", "").strip()
SITE     = os.getenv("SITE_ID", "MLM").strip() or "MLM"
SKU      = os.getenv("SKU", "").strip()
QTY      = int(os.getenv("QTY", "1"))
PRICE    = float(os.getenv("PRICE_UNIT", "0"))

PARTNER_NAME = os.getenv("FULL_PARTNER_NAME", "MercadoLibre FULL").strip()

def die(msg, code=2):
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(code)

for k,v in [("ODOO_URL",ODOO_URL),("ODOO_DB",DB),("ODOO_USER",USER),("ODOO_PASSWORD",PW),("ORDER_ID",ORDER_ID),("SKU",SKU)]:
    if not v:
        die(f"missing env {k}")

if QTY <= 0:
    die("QTY must be > 0")

CLIENT_ORDER_REF = f"MLFULL:{SITE}:{ORDER_ID}"

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def jcall(service, method, args):
    payload={"jsonrpc":"2.0","method":"call","params":{"service":service,"method":method,"args":args},"id":1}
    r = requests.post(ODOO_URL + "/jsonrpc", json=payload, timeout=30)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]

def exec_kw(uid, model, method, args=None, kwargs=None):
    if args is None: args=[]
    if kwargs is None: kwargs={}
    return jcall("object","execute_kw",[DB,uid,PW,model,method,args,kwargs])

uid = jcall("common","authenticate",[DB,USER,PW,{}])
if not uid:
    die("AUTH_FAIL uid=null")
print("AUTH_OK uid=", uid)
print("CLIENT_ORDER_REF =", CLIENT_ORDER_REF)

# =========================
# 1) Find partner
# =========================
partner_rows = exec_kw(uid, "res.partner", "search_read",
    [[["name","=",PARTNER_NAME]]],
    {"fields":["id","name"],"limit":2}
)
if not partner_rows:
    die(f'partner_not_found name="{PARTNER_NAME}" (create it manually once)')
if len(partner_rows) > 1:
    die(f'partner_not_unique name="{PARTNER_NAME}"')
partner_id = int(partner_rows[0]["id"])
print("partner_id =", partner_id, "|", partner_rows[0]["name"])

# =========================
# 2) Find product by SKU (default_code)
# =========================
prod_rows = exec_kw(uid, "product.product", "search_read",
    [[["default_code","=",SKU],["sale_ok","=",True]]],
    {"fields":["id","default_code","display_name","sale_ok"],"limit":2}
)
if not prod_rows:
    die(f'product_not_found_or_not_sale_ok sku="{SKU}"')
if len(prod_rows) > 1:
    die(f'product_not_unique sku="{SKU}"')
product_id = int(prod_rows[0]["id"])
print("product_id =", product_id, "|", prod_rows[0]["display_name"])

# =========================
# 3) Idempotency: reuse SO if exists
# =========================
so_rows = exec_kw(uid, "sale.order", "search_read",
    [[["client_order_ref","=",CLIENT_ORDER_REF]]],
    {"fields":["id","name","state","client_order_ref","picking_ids","invoice_ids"],"limit":2}
)
if len(so_rows) > 1:
    die("so_not_unique (client_order_ref)")
if so_rows:
    so = so_rows[0]
    print("\nSO_ALREADY_EXISTS")
    print(json.dumps(so, ensure_ascii=False, indent=2))
    print("OK_DONE")
    sys.exit(0)

# =========================
# 4) Create SO in DRAFT (Level1) with one line
# =========================
so_vals = {
    "partner_id": partner_id,
    "client_order_ref": CLIENT_ORDER_REF,
    "origin": CLIENT_ORDER_REF,
    "note": "AUTO FULL INBOUND (LEVEL1: SO ONLY)",
}

so_id = exec_kw(uid, "sale.order", "create", [so_vals])
print("\nSO_CREATED so_id =", so_id)

line_vals = {
    "order_id": so_id,
    "product_id": product_id,
    "product_uom_qty": QTY,
    "price_unit": PRICE,
    "name": SKU,
}
line_id = exec_kw(uid, "sale.order.line", "create", [line_vals])
print("SO_LINE_CREATED line_id =", line_id)

# Read back
so = exec_kw(uid, "sale.order", "read",
    [[so_id], ["id","name","state","client_order_ref","picking_ids","invoice_ids","amount_total"]]
)[0]

print("\nSO_AFTER")
print(json.dumps(so, ensure_ascii=False, indent=2))

# Hard assertion: no pickings created just by draft creation
if so.get("picking_ids"):
    die("unexpected_pickings_created (FULL must have 0 pickings)")

print("\nOK_DONE ts_utc=", utc_now_iso())
