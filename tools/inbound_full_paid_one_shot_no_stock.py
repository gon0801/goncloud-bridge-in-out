#!/usr/bin/env python3
import os, sys, json, requests
from datetime import datetime, timezone

# =========================
# ENV
# =========================
ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/")
DB      = os.getenv("ODOO_DB") or ""
USER    = os.getenv("ODOO_USER") or ""
PW      = os.getenv("ODOO_PASSWORD") or os.getenv("ODOO_PASS") or ""  # compat
CLIENT_ORDER_REF = (os.getenv("CLIENT_ORDER_REF") or "").strip()
ORDER_JSON_RAW = os.getenv("ORDER_JSON") or ""
WAREHOUSE_NAME = (os.getenv("WAREHOUSE_NAME") or "").strip()

AUDIT_DIR = os.getenv("AUDIT_DIR") or "/mnt/data/appdata/bridge/audit/full_paid"

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def die(msg, code=2):
    print(f"[FULL_PAID] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k, v in [
    ("ODOO_URL", ODOO_URL),
    ("ODOO_DB", DB),
    ("ODOO_USER", USER),
    ("ODOO_PASSWORD", PW),
    ("CLIENT_ORDER_REF", CLIENT_ORDER_REF),
    ("ORDER_JSON", ORDER_JSON_RAW),
]:
    if not v:
        die(f"missing env {k}", 2)

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

def exec_kw(uid, model, method, args=None, kwargs=None, context=None):
    args = args or []
    kwargs = kwargs or {}
    if context:
        kwargs = dict(kwargs)
        kwargs["context"] = context
    return jcall("object", "execute_kw", [DB, uid, PW, model, method, args, kwargs])

# =========================
# Parse order JSON
# =========================
try:
    order = json.loads(ORDER_JSON_RAW)
    if not isinstance(order, dict):
        raise ValueError("order_json_not_dict")
except Exception as e:
    die(f"bad ORDER_JSON: {e!r}", 2)

order_id = str(order.get("id") or "").strip()
site_id  = str(order.get("site_id") or "").strip() or "UNKNOWN"
if not order_id:
    die("ORDER_JSON missing id", 2)

def parse_items(order_dict):
    """Bug M3: items sin seller_sku ya no se silencian; die con rc=1 →
    worker → manual_review. El operador asigna el SKU en MeLi o en
    sku_mapping antes de reprocesar."""
    items = order_dict.get("order_items") or []
    if not isinstance(items, list):
        return []
    out = []
    skipped = []
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = it.get("quantity", 0)
        try:
            qty = int(qty)
        except Exception:
            qty = 0
        if qty <= 0:
            continue

        item = it.get("item") or {}
        if not isinstance(item, dict):
            continue

        sku = (item.get("seller_sku") or item.get("SELLER_SKU") or "").strip()
        if not sku:
            skipped.append({
                "item_id": str(item.get("id") or ""),
                "variation_id": str(item.get("variation_id") or it.get("variation_id") or ""),
                "title": str(item.get("title") or ""),
            })
            continue

        unit_price = it.get("unit_price")
        if unit_price is None:
            unit_price = it.get("price")
        try:
            unit_price = float(unit_price) if unit_price is not None else 0.0
        except Exception:
            unit_price = 0.0

        out.append({"sku": sku, "qty": qty, "unit_price": unit_price})

    if skipped:
        die(f"unmapped_items: {len(skipped)} item(s) sin SKU resoluble: {skipped}", code=1)

    return out

items = parse_items(order)
if not items:
    die("ORDER_JSON has no usable items (need order_items with item.seller_sku and quantity)", 2)

# =========================
# Audit scaffold
# =========================
audit = {
    "ts_start_utc": utc_now_iso(),
    "client_order_ref": CLIENT_ORDER_REF,
    "order_id": order_id,
    "site_id": site_id,
    "steps": [],
    "result": None,
}
def step(step_name, **data):
    audit["steps"].append({"step": step_name, "ts_utc": utc_now_iso(), **data})
    print(f"[FULL_PAID] STEP={step_name} {data}", flush=True)

# =========================
# Main
# =========================
uid = jcall("common", "authenticate", [DB, USER, PW, {}])
if not uid:
    die("AUTH_FAIL uid=null", 2)
print(f"[FULL_PAID] AUTH_OK uid={uid}", flush=True)
step("input", items_count=len(items), client_order_ref=CLIENT_ORDER_REF)

def get_warehouse_id(name: str):
    if not name:
        return None
    whs = exec_kw(uid, "stock.warehouse", "search_read",
        [[["name", "=", name]]],
        {"fields": ["id", "name"], "limit": 1})
    if whs:
        print(f"[FULL_PAID] warehouse: '{whs[0]['name']}' id={whs[0]['id']}", flush=True)
        return whs[0]["id"]
    print(f"[FULL_PAID] WARN warehouse '{name}' not found in Odoo, using default", flush=True)
    return None

warehouse_id = get_warehouse_id(WAREHOUSE_NAME)

# 1) Partner "MercadoLibre FULL"
PARTNER_NAME = "MercadoLibre FULL"
partner_ids = exec_kw(uid, "res.partner", "search", [[["name", "=", PARTNER_NAME], ["active", "=", True]]], {"limit": 1})
if partner_ids:
    partner_id = int(partner_ids[0])
    step("partner_found", partner_id=partner_id, partner_name=PARTNER_NAME)
else:
    die('partner_not_found name="MercadoLibre FULL" (create it manually once)', 2)

# 2) Find existing SO by client_order_ref
buyer_nick = ((order.get("buyer") or {}).get("nickname") or (order.get("buyer") or {}).get("first_name") or "").strip()
display_id = CLIENT_ORDER_REF.rsplit(":", 1)[-1]
display_ref = f"{display_id} | {buyer_nick}" if buyer_nick else display_id
so_ids = exec_kw(uid, "sale.order", "search", [[["client_order_ref", "=", display_ref]]], {"limit": 2})
if len(so_ids) > 1:
    die(f"multiple SO found for client_order_ref={display_ref}", 2)
so_id = int(so_ids[0]) if so_ids else 0

# 3) Resolve products (sale_ok=True)
skus = [x["sku"] for x in items]
prod_rows = exec_kw(
    uid, "product.product", "search_read",
    [[["default_code", "in", skus], ["sale_ok", "=", True]]],
    {"fields": ["id", "default_code", "display_name", "sale_ok", "type"], "limit": 2000}
) or []

sku_to_pid = {}
for r in prod_rows:
    sku_to_pid[str(r.get("default_code") or "")] = int(r["id"])

missing = [s for s in skus if s not in sku_to_pid]
if missing:
    die(f"missing products (sale_ok=true) for SKUs: {missing}", 2)

step("products_ok", unique_skus=len(set(skus)))

# 4) Create SO if missing
if not so_id:
    so_vals = {
        "partner_id": partner_id,
        "client_order_ref": display_ref,
        "note": f"ML:FULL | STATE=paid | ORDER={order_id}",
    }
    if warehouse_id:
        so_vals["warehouse_id"] = warehouse_id
    so_id = exec_kw(uid, "sale.order", "create", [so_vals])
    step("so_created", so_id=so_id, warehouse_id=warehouse_id or "default")
    # TODO(Task-3-picking): una vez decidida Task 1.5 (phantom BOM), actualizar
    # lógica de picking. Actualmente el SO se fuerza a state='sale' sin pickings
    # (MeLi FULL = MeLi gestiona el stock físico). Con warehouse Meli-Full puede
    # ser necesario generar y validar un picking para decrementar el stock correcto.
else:
    step("so_exists", so_id=so_id)

# 5) Ensure SO lines (idempotent strict mode)
existing_lines = exec_kw(
    uid, "sale.order.line", "search_read",
    [[["order_id", "=", so_id]]],
    {"fields": ["id", "product_id", "product_uom_qty", "price_unit"], "limit": 2000}
) or []

if existing_lines:
    # verify each SKU qty matches
    sums = {}
    for ln in existing_lines:
        pid = int((ln.get("product_id") or [0])[0]) if isinstance(ln.get("product_id"), list) else int(ln.get("product_id") or 0)
        sums.setdefault(pid, 0.0)
        try:
            sums[pid] += float(ln.get("product_uom_qty") or 0.0)
        except Exception:
            pass

    for it in items:
        pid = sku_to_pid[it["sku"]]
        want_qty = float(it["qty"])
        if pid not in sums or abs(sums[pid] - want_qty) > 1e-9:
            die(f"SO already has lines but qty mismatch for sku={it['sku']}: have={sums.get(pid)} want_qty={want_qty}", 2)

    step("so_lines_verified", lines=len(existing_lines))
else:
    for it in items:
        pid = sku_to_pid[it["sku"]]
        exec_kw(uid, "sale.order.line", "create", [{
            "order_id": so_id,
            "product_id": pid,
            "product_uom_qty": int(it["qty"]),
            "price_unit": float(it["unit_price"] or 0.0),
            "name": f"{it['sku']}",
        }])
    step("so_lines_created", lines=len(items))

# 6) FORCE "confirmed" contable SIN logística: state="sale" (NO pickings)
so = exec_kw(uid, "sale.order", "read", [[so_id], ["id","name","state","client_order_ref","picking_ids","invoice_ids","invoice_status","amount_total"]])[0]
if so.get("picking_ids"):
    die("FULL must be NO pickings. Found pickings on SO -> stop.", 2)

if so["state"] != "sale":
    exec_kw(uid, "sale.order", "write", [[so_id], {"state": "sale"}])
    so = exec_kw(uid, "sale.order", "read", [[so_id], ["id","name","state","client_order_ref","picking_ids","invoice_ids","invoice_status","amount_total"]])[0]
    step("so_forced_sale_no_stock", so_name=so["name"], state=so["state"])
else:
    step("so_already_sale", so_name=so["name"])

# 7) Invoice: si ya existe posted por origin/ref -> usar; si no -> wizard 100%
inv_ids = exec_kw(uid, "account.move", "search", [[
    ["move_type","=","out_invoice"],
    ["invoice_origin","=",so["name"]],
]], {"limit": 10}) or []

posted = []
for iid in inv_ids:
    m = exec_kw(uid, "account.move", "read", [[iid], ["id","state","payment_state","name","amount_total","amount_residual","invoice_origin","ref"]])[0]
    if m["state"] == "posted":
        posted.append(m)

if posted:
    invoice = posted[0]
    step("invoice_found_posted", invoice_id=invoice["id"], invoice_name=invoice["name"], payment_state=invoice["payment_state"])
else:
    # Wizard correcto que SÍ liga invoice_ids al SO
    ctx = {"active_model":"sale.order", "active_ids":[so_id], "active_id": so_id}
    wiz_id = exec_kw(uid, "sale.advance.payment.inv", "create", [{
        "advance_payment_method": "delivered",
    }], context=ctx)
    step("invoice_wizard_created", wiz_id=wiz_id)

    action = exec_kw(uid, "sale.advance.payment.inv", "create_invoices", [[wiz_id]], context=ctx)
    res_id = int((action or {}).get("res_id") or 0)
    if not res_id:
        die(f"could not infer invoice id from action: {action}", 2)

    invoice = exec_kw(uid, "account.move", "read", [[res_id], ["id","state","payment_state","name","amount_total","amount_residual","invoice_origin","ref"]])[0]
    step("invoice_created", invoice_id=invoice["id"], invoice_name=invoice["name"], state=invoice["state"])

    if invoice["state"] == "draft":
        exec_kw(uid, "account.move", "action_post", [[invoice["id"]]])
        invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id","state","payment_state","name","amount_total","amount_residual","invoice_origin","ref"]])[0]
        step("invoice_posted", invoice_id=invoice["id"], state=invoice["state"])

# 8) Pay invoice if not paid
invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id","state","payment_state","name","amount_total","amount_residual"]])[0]
if invoice["payment_state"] == "paid":
    step("invoice_already_paid", invoice_id=invoice["id"], payment_state=invoice["payment_state"], residual=str(invoice.get("amount_residual")))
else:
    j = exec_kw(uid, "account.journal", "search_read", [[["type","=","bank"]]], {"fields":["id","name","type"], "limit": 1})
    if not j:
        die("no bank journal found", 2)
    bank_journal_id = int(j[0]["id"])
    step("bank_journal", bank_journal_id=bank_journal_id, bank_journal_name=j[0]["name"])

    ctx = {"active_model":"account.move", "active_ids":[invoice["id"]], "active_id": invoice["id"]}
    pay_wiz = exec_kw(uid, "account.payment.register", "create", [{
        "journal_id": bank_journal_id,
    }], context=ctx)
    step("payment_wizard_created", wiz_id=pay_wiz)

    exec_kw(uid, "account.payment.register", "action_create_payments", [[pay_wiz]], context=ctx)
    invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id","payment_state","amount_residual","state","name"]])[0]
    step("invoice_paid", payment_state=invoice["payment_state"], residual=str(invoice.get("amount_residual")))

# Releer SO después del wizard para confirmar link invoice_ids
so_after = exec_kw(uid, "sale.order", "read", [[so_id], ["id","name","state","invoice_ids","invoice_status","picking_ids","amount_total"]])[0]
step("so_after_invoicing", invoice_status=so_after.get("invoice_status"), invoice_ids=so_after.get("invoice_ids") or [])

# 9) Final summary + audit
final = {
    "client_order_ref": CLIENT_ORDER_REF,
    "so_id": so_id,
    "so_name": so_after["name"],
    "so_state": so_after["state"],
    "so_picking_count": len(so_after.get("picking_ids") or []),
    "so_invoice_status": so_after.get("invoice_status"),
    "so_invoice_ids": so_after.get("invoice_ids") or [],
    "invoice_id": invoice["id"],
    "invoice_name": invoice.get("name"),
    "invoice_state": invoice["state"],
    "invoice_payment_state": invoice["payment_state"],
    "invoice_residual": str(invoice.get("amount_residual")),
}

audit["result"] = {"status": "ok", "summary": final}
audit["ts_end_utc"] = utc_now_iso()

os.makedirs(AUDIT_DIR, exist_ok=True)
audit_path = os.path.join(AUDIT_DIR, f"{CLIENT_ORDER_REF}.json")
with open(audit_path, "w", encoding="utf-8") as f:
    json.dump(audit, f, ensure_ascii=False, indent=2)

print("[FULL_PAID] AUDIT_WRITE_OK path=" + audit_path, flush=True)
print("[FULL_PAID] FINAL_SUMMARY")
print(json.dumps(final, ensure_ascii=False, indent=2))
print("[FULL_PAID] OK_DONE")
sys.exit(0)
