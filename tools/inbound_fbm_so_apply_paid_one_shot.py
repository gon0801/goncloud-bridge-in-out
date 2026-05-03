#!/usr/bin/env python3
import os, sys, json, time, requests
from datetime import datetime, timezone

# =========================
# ENV
# =========================
ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/")
DB      = os.getenv("ODOO_DB") or ""
USER    = os.getenv("ODOO_USER") or ""
PW      = os.getenv("ODOO_PASS") or os.getenv("ODOO_PASSWORD") or ""  # compat
ORDER_JSON_RAW = os.getenv("ORDER_JSON") or ""

AUDIT_DIR = os.getenv("AUDIT_DIR") or "/mnt/data/appdata/bridge/audit/fbm_paid"

def die(msg, code=2):
    print(f"[FBM_PAID] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k,v in [("ODOO_URL",ODOO_URL),("ODOO_DB",DB),("ODOO_USER",USER),("ODOO_PASS",PW),("ORDER_JSON",ORDER_JSON_RAW)]:
    if not v:
        die(f"missing env {k}")

# =========================
# JSON-RPC helpers
# =========================
def jcall(service, method, args):
    payload = {"jsonrpc":"2.0","method":"call","params":{"service":service,"method":method,"args":args},"id":1}
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
    return jcall("object","execute_kw",[DB, uid, PW, model, method, args, kwargs])

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

# =========================
# Parse order JSON
# =========================
try:
    order = json.loads(ORDER_JSON_RAW)
    if not isinstance(order, dict):
        raise ValueError("order_json_not_dict")
except Exception as e:
    die(f"bad ORDER_JSON: {e!r}")

order_id = str(order.get("id") or "").strip()
if not order_id:
    die("ORDER_JSON missing id")

# Preferir valores enviados por el worker (fuente única de verdad)
SITE_ID_ENV = (os.getenv("SITE_ID") or "").strip()
COR_ENV = (os.getenv("CLIENT_ORDER_REF") or "").strip()

site_id = SITE_ID_ENV or str(order.get("site_id") or "").strip() or "UNKNOWN"
CLIENT_ORDER_REF = COR_ENV or f"MLFBM:{site_id}:{order_id}"

import sqlite3

BRIDGE_DB = os.getenv("BRIDGE_DB") or "/mnt/data/appdata/bridge/data/bridge.db"

def lookup_sku_mapping(item_id: str, variation_id: str, site: str) -> str:
    item_id = (item_id or "").strip()
    variation_id = (variation_id or "").strip()
    site = (site or "").strip() or "MLM"
    if not item_id or not variation_id:
        return ""
    try:
        con = sqlite3.connect(BRIDGE_DB)
        row = con.execute(
            """
            SELECT sku
            FROM sku_mapping
            WHERE channel='meli'
              AND remote_item_id=?
              AND remote_variation_id=?
              AND COALESCE(site,'MLM')=?
            LIMIT 1
            """,
            (item_id, variation_id, site),
        ).fetchone()
        con.close()
        return (str(row[0]).strip() if row and row[0] else "")
    except Exception:
        return ""

def parse_items(order_dict):
    items = order_dict.get("order_items") or []
    if not isinstance(items, list):
        return []

    site = (SITE_ID_ENV or str(order_dict.get("site_id") or "").strip() or "MLM").strip()

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            qty = int(it.get("quantity", 0))
        except Exception:
            qty = 0
        if qty <= 0:
            continue

        item = it.get("item") or {}
        if not isinstance(item, dict):
            continue

        sku = (item.get("seller_sku") or item.get("SELLER_SKU") or "").strip()

        # Fallback v2: si no viene seller_sku, resuelve por (item_id, variation_id) en sku_mapping
        if not sku:
            item_id = str(item.get("id") or "").strip()
            var_id = str(item.get("variation_id") or it.get("variation_id") or "").strip()
            sku = lookup_sku_mapping(item_id, var_id, site)

        if not sku:
            continue

        unit_price = it.get("unit_price")
        if unit_price is None:
            unit_price = it.get("price")
        try:
            unit_price = float(unit_price) if unit_price is not None else 0.0
        except Exception:
            unit_price = 0.0

        out.append({"sku": sku, "qty": qty, "unit_price": unit_price})
    return out

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
    print(f"[FBM_PAID] STEP={step_name} {data}", flush=True)

# =========================
# Main
# =========================
uid = jcall("common","authenticate",[DB, USER, PW, {}])
if not uid:
    die("AUTH_FAIL uid=null")
print(f"[FBM_PAID] AUTH_OK uid={uid}", flush=True)

items = parse_items(order)
if not items:
    die("no valid items (missing SKUs or zero qty)")
step("input", items_count=len(items), client_order_ref=CLIENT_ORDER_REF)

# 1) Partner "MercadoLibre FBM"
PARTNER_NAME = "MercadoLibre FBM"
partner_ids = exec_kw(uid, "res.partner", "search", [[["name","=",PARTNER_NAME],["active","=",True]]], {"limit": 1})
if partner_ids:
    partner_id = int(partner_ids[0])
    step("partner_found", partner_id=partner_id, partner_name=PARTNER_NAME)
else:
    partner_id = exec_kw(uid, "res.partner", "create", [{
        "name": PARTNER_NAME,
        "is_company": True,
        "company_type": "company",
        "active": True,
    }])
    step("partner_created", partner_id=partner_id, partner_name=PARTNER_NAME)

# 2) Find existing SO by client_order_ref
buyer_nick = ((order.get("buyer") or {}).get("nickname") or (order.get("buyer") or {}).get("first_name") or "").strip()
display_ref = f"{order_id} | {buyer_nick}" if buyer_nick else order_id
so_ids = exec_kw(uid, "sale.order", "search", [[["client_order_ref","=",display_ref]]], {"limit": 2})
if len(so_ids) > 1:
    die(f"multiple SO found for client_order_ref={display_ref}")
so_id = int(so_ids[0]) if so_ids else 0

# 3) Resolve products for all SKUs (sale_ok=True) + GATE sell_on_meli
skus = [x["sku"] for x in items]

prod_rows = exec_kw(
    uid, "product.product", "search_read",
    [[["default_code","in",skus],["sale_ok","=",True]]],
    {"fields":["id","default_code","display_name","sale_ok","type","sell_on_meli"], "limit": 2000}
) or []

sku_to_pid = {}
sku_to_sell = {}
for r in prod_rows:
    code = str(r.get("default_code") or "").strip()
    if not code:
        continue
    sku_to_pid[code] = int(r["id"])
    sku_to_sell[code] = bool(r.get("sell_on_meli"))

missing = [s for s in skus if s not in sku_to_pid]
if missing:
    die(f"missing products (sale_ok=true) for SKUs: {missing}")

blocked = sorted({s for s in skus if not sku_to_sell.get(s, False)})
if blocked:
    die(f"sku_not_allowed_for_meli (sell_on_meli=false) blocked_skus={blocked}")

step("products_ok", unique_skus=len(set(skus)), sell_on_meli_ok=True)

# 4) Create SO if missing
if not so_id:
    so_id = exec_kw(uid, "sale.order", "create", [{
        "partner_id": partner_id,
        "client_order_ref": display_ref,
        "note": (os.getenv("SO_NOTE") or f"ML:FBM | STATE=paid | ORDER={order_id}"),
    }])
    step("so_created", so_id=so_id)
else:
    step("so_exists", so_id=so_id)
# Update SO note if provided (pack_id-visible ref, buyer name, etc.)
so_note_env = (os.getenv("SO_NOTE") or "").strip()
if so_note_env:
    exec_kw(uid, "sale.order", "write", [[so_id], {"note": so_note_env}])
    step("so_note_updated", so_id=so_id)

# 5) Ensure SO lines (idempotent strict mode)
existing_lines = exec_kw(
    uid, "sale.order.line", "search_read",
    [[["order_id","=",so_id]]],
    {"fields":["id","product_id","product_uom_qty","price_unit"], "limit": 2000}
) or []

if existing_lines:
    sums = {}
    for ln in existing_lines:
        pid = int((ln.get("product_id") or [0])[0]) if isinstance(ln.get("product_id"), list) else int(ln.get("product_id") or 0)
        sums.setdefault(pid, {"qty":0, "price_units": set()})
        try:
            sums[pid]["qty"] += float(ln.get("product_uom_qty") or 0)
        except Exception:
            pass
        sums[pid]["price_units"].add(float(ln.get("price_unit") or 0.0))

    for it in items:
        pid = sku_to_pid[it["sku"]]
        want_qty = float(it["qty"])
        want_price = float(it["unit_price"])
        if pid not in sums or abs(sums[pid]["qty"] - want_qty) > 1e-9:
            die(f"SO already has lines but qty mismatch for sku={it['sku']}: have={sums.get(pid)} want_qty={want_qty}")
        if want_price != 0.0 and want_price not in sums[pid]["price_units"]:
            die(f"SO already has lines but price mismatch for sku={it['sku']}: have_prices={sums[pid]['price_units']} want_price={want_price}")

    step("so_lines_verified", lines=len(existing_lines))
else:
    line_vals = []
    for it in items:
        pid = sku_to_pid[it["sku"]]
        line_vals.append({
            "order_id": so_id,
            "product_id": pid,
            "product_uom_qty": int(it["qty"]),
            "price_unit": float(it["unit_price"] or 0.0),
            "name": f"{it['sku']}",
        })
    for lv in line_vals:
        exec_kw(uid, "sale.order.line", "create", [lv])
    step("so_lines_created", lines=len(line_vals))

# 6) Confirm SO if needed
so = exec_kw(uid, "sale.order", "read", [[so_id], ["id","name","state","picking_ids","invoice_ids","amount_total"]])[0]
step("so_read", so_name=so["name"], state=so["state"])

if so["state"] in ("draft","sent"):
    # Bug #6: try/except — SO en draft retry seguro, no hay state que rollback.
    try:
        exec_kw(uid, "sale.order", "action_confirm", [[so_id]])
        so = exec_kw(uid, "sale.order", "read", [[so_id], ["id","name","state","picking_ids","invoice_ids","amount_total"]])[0]
        step("so_confirmed", so_name=so["name"], state=so["state"])
    except Exception as e:
        die(f"SO confirm failed (state=draft, safe to retry): {e}")
elif so["state"] == "sale":
    step("so_already_sale")
else:
    die(f"SO state not supported for paid flow: {so['state']}")

# 7) Ensure at least one picking exists (do NOT validate)
pickings = so.get("picking_ids") or []
step("picking_check", picking_count=len(pickings))

# 8) Find posted invoice if already exists
inv_ids = exec_kw(uid, "account.move", "search", [[["invoice_origin","=",so["name"]],["move_type","=","out_invoice"]]], {"limit": 5})
posted = []
for iid in inv_ids:
    m = exec_kw(uid, "account.move", "read", [[iid], ["id","state","payment_state","name","amount_total","ref","invoice_origin"]])[0]
    if m["state"] == "posted":
        posted.append(m)

if posted:
    invoice = posted[0]
    step("invoice_found_posted", invoice_id=invoice["id"], invoice_name=invoice["name"], payment_state=invoice["payment_state"])
else:
    # 9) Create invoice via wizard - use 'delivered' to create regular invoice
    # Bug #6: wizard_create + wizard_execute + post envueltos en try/except.
    # Compensation: si creó draft pero post falló → unlink draft (no consume secuencia).
    ctx = {"active_model": "sale.order", "active_ids": [so_id], "active_id": so_id}
    inv_id = None
    try:
        wiz_id = exec_kw(uid, "sale.advance.payment.inv", "create", [{
            "advance_payment_method": "delivered",
        }], context=ctx)
        step("invoice_wizard_created", wiz_id=wiz_id, method="delivered")

        exec_kw(uid, "sale.advance.payment.inv", "create_invoices", [[wiz_id]], context=ctx)
        step("invoice_wizard_executed")

        # Find the created invoice
        inv_ids = exec_kw(
            uid, "account.move", "search",
            [[["invoice_origin", "=", so["name"]], ["move_type", "=", "out_invoice"], ["state", "=", "draft"]]],
            {"order": "id desc", "limit": 1},
        ) or []

        if not inv_ids:
            die(f"invoice_create_failed: no draft invoice found for origin={so['name']}")

        inv_id = int(inv_ids[0])
        invoice = exec_kw(
            uid, "account.move", "read",
            [[inv_id], ["id", "state", "payment_state", "name", "amount_total", "ref", "invoice_origin", "invoice_line_ids"]],
        )[0]
        step("invoice_created", invoice_id=invoice["id"], invoice_name=invoice["name"], state=invoice["state"], line_count=len(invoice.get("invoice_line_ids") or []))

        if not invoice.get("invoice_line_ids"):
            die(f"invoice_has_no_lines invoice_id={inv_id} origin={so['name']}")

        if invoice["state"] == "draft":
            exec_kw(uid, "account.move", "action_post", [[inv_id]])
            invoice = exec_kw(uid, "account.move", "read", [[inv_id], ["id", "state", "payment_state", "name", "amount_total", "ref", "invoice_origin", "amount_residual"]])[0]
            step("invoice_posted", invoice_id=inv_id, state=invoice["state"])
    except SystemExit:
        # die() levanta SystemExit — no hacer compensation (die() es decisión del propio bloque).
        raise
    except Exception as e:
        if inv_id:
            try:
                _check = exec_kw(uid, "account.move", "read", [[inv_id], ["state"]])[0]
                if _check["state"] == "draft":
                    exec_kw(uid, "account.move", "unlink", [[inv_id]])
                    step("compensation_invoice_unlinked", invoice_id=inv_id)
                else:
                    step("compensation_skipped", invoice_id=inv_id, state=_check["state"])
            except Exception as ce:
                step("compensation_failed", invoice_id=inv_id, error=str(ce)[:200])
        die(f"invoice create+post failed: {e}")

# 10) Pay invoice if not paid
invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id","state","payment_state","name","amount_total","amount_residual"]])[0]
if invoice["payment_state"] == "paid":
    step("invoice_already_paid", invoice_id=invoice["id"])
else:
    j = exec_kw(uid, "account.journal", "search_read", [[["type","=","bank"]]], {"fields":["id","name","type"], "limit": 1})
    if not j:
        die("no bank journal found")
    bank_journal_id = int(j[0]["id"])
    step("bank_journal", bank_journal_id=bank_journal_id, journal_name=j[0]["name"])

    # Bug #6: payment register sin try/except dejaba la invoice posted huérfana
    # cuando el wizard fallaba. Ahora morimos limpio: invoice queda posted (es válida,
    # no la cancelamos = no rompemos secuencia) y operador la paga manual desde Odoo UI.
    try:
        ctx = {"active_model":"account.move", "active_ids":[invoice["id"]], "active_id": invoice["id"]}
        pay_wiz = exec_kw(uid, "account.payment.register", "create", [{
            "journal_id": bank_journal_id,
        }], context=ctx)
        step("payment_wizard_created", wiz_id=pay_wiz)

        exec_kw(uid, "account.payment.register", "action_create_payments", [[pay_wiz]], context=ctx)
        invoice = exec_kw(uid, "account.move", "read", [[invoice["id"]], ["id","payment_state","amount_residual","state"]])[0]
        step("invoice_paid", payment_state=invoice["payment_state"], residual=str(invoice["amount_residual"]))
    except Exception as e:
        die(f"payment register failed (invoice {invoice['name']} POSTED, register payment manualmente en Odoo): {e}")

# 11) Final summary
final = {
    "client_order_ref": CLIENT_ORDER_REF,
    "so_id": so_id,
    "so_name": so["name"],
    "invoice_id": invoice["id"],
    "invoice_state": invoice["state"],
    "invoice_payment_state": invoice["payment_state"],
    "picking_count": len(so.get("picking_ids") or []),
}
audit["result"] = {"status":"ok", "summary": final}
audit["ts_end_utc"] = utc_now_iso()

os.makedirs(AUDIT_DIR, exist_ok=True)
audit_path = os.path.join(AUDIT_DIR, f"{CLIENT_ORDER_REF}.json")
with open(audit_path, "w", encoding="utf-8") as f:
    json.dump(audit, f, ensure_ascii=False, indent=2)

print("[FBM_PAID] AUDIT_WRITE_OK path=" + audit_path, flush=True)
print("[FBM_PAID] FINAL_SUMMARY")
print(json.dumps(final, ensure_ascii=False, indent=2))
print("[FBM_PAID] OK_DONE")
