#!/usr/bin/env python3
import os, sys, json, requests
from datetime import datetime, timezone

ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/")
DB       = os.getenv("ODOO_DB") or ""
USER     = os.getenv("ODOO_USER") or ""
PW       = os.getenv("ODOO_PASS") or os.getenv("ODOO_PASSWORD") or ""
CLIENT_ORDER_REF = (os.getenv("CLIENT_ORDER_REF") or "").strip()

AUDIT_DIR = os.getenv("AUDIT_DIR") or "/mnt/data/appdata/bridge/audit/fbm_refund"

def die(msg, code=2):
    print(f"[FBM_REFUND] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k,v in [("ODOO_URL",ODOO_URL),("ODOO_DB",DB),("ODOO_USER",USER),("ODOO_PASS",PW),("CLIENT_ORDER_REF",CLIENT_ORDER_REF)]:
    if not v:
        die(f"missing env {k}")

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

audit = {
    "ts_start_utc": utc_now_iso(),
    "client_order_ref": CLIENT_ORDER_REF,
    "steps": [],
    "result": None,
}

def step(step_name, **data):
    audit["steps"].append({"step": step_name, "ts_utc": utc_now_iso(), **data})
    print(f"[FBM_REFUND] STEP={step_name} {data}", flush=True)

def jcall(service, method, args):
    payload = {"jsonrpc":"2.0","method":"call","params":{"service":service,"method":method,"args":args},"id":1}
    r = requests.post(ODOO_URL + "/jsonrpc", json=payload, timeout=45)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(json.dumps(d["error"], ensure_ascii=False)[:2000])
    return d.get("result")

def exec_kw(uid, model, method, args=None, kwargs=None, context=None):
    args = args or []
    kwargs = kwargs or {}
    if context:
        kwargs = dict(kwargs)
        kwargs["context"] = context
    return jcall("object","execute_kw",[DB, uid, PW, model, method, args, kwargs])

uid = jcall("common","authenticate",[DB, USER, PW, {}])
if not uid:
    die("AUTH_FAIL uid=null")
print(f"[FBM_REFUND] AUTH_OK uid={uid}", flush=True)
order_id = CLIENT_ORDER_REF.rsplit(":", 1)[-1]
step("input", client_order_ref=CLIENT_ORDER_REF, order_id=order_id)

# 1) Buscar SO
so_ids = exec_kw(uid, "sale.order", "search", [[["client_order_ref","=like",f"{order_id}%"]]], {"limit": 2})
if not so_ids:
    die("SO_NOT_FOUND (order not yet created in Odoo)")
if len(so_ids) > 1:
    die("SO_MULTIPLE_FOUND")
so_id = int(so_ids[0])

so = exec_kw(uid, "sale.order", "read", [[so_id], ["id","name","state","picking_ids","invoice_ids","invoice_status","locked"]])[0]
step("so_found", so_id=so_id, so_name=so["name"], so_state=so["state"], pickings=len(so.get("picking_ids") or []))

# 2) Encontrar invoice posted (out_invoice) por invoice_origin=SO.name
inv_ids = exec_kw(uid, "account.move", "search", [[["invoice_origin","=",so["name"]],["move_type","=","out_invoice"]]], {"limit": 10})
posted = []
for iid in inv_ids:
    m = exec_kw(uid, "account.move", "read", [[iid], ["id","name","state","payment_state","amount_total","amount_residual","move_type"]])[0]
    if m["state"] == "posted":
        posted.append(m)

if not posted:
    die("INVOICE_POSTED_NOT_FOUND (need posted out_invoice)")

invoice = posted[0]
invoice_id = int(invoice["id"])
step("invoice_found", invoice_id=invoice_id, invoice_name=invoice["name"], payment_state=invoice["payment_state"], amount_total=invoice["amount_total"])

# 3) Buscar credit note existente (out_refund) por reversed_entry_id
existing_cn = exec_kw(
    uid, "account.move", "search",
    [[["move_type","=","out_refund"],["reversed_entry_id","=",invoice_id]]],
    {"limit": 2}
)
if len(existing_cn) > 1:
    die("MULTIPLE_CREDIT_NOTES_FOUND")
credit_id = int(existing_cn[0]) if existing_cn else 0

if credit_id:
    credit = exec_kw(uid, "account.move", "read", [[credit_id], ["id","name","state","payment_state","amount_total","amount_residual","reversed_entry_id"]])[0]
    step("credit_note_ready", status="found_existing", credit_note_id=credit_id, state=credit["state"])
else:
    # 4) Crear reversal wizard y reverse_moves()
    # Journal sale
    j = exec_kw(uid, "account.journal", "search_read", [[["type","=","sale"]]], {"fields":["id","name","type","company_id"], "limit": 1})
    if not j:
        die("NO_SALE_JOURNAL_FOUND")
    sale_journal_id = int(j[0]["id"])
    step("journals", sale_journal_id=sale_journal_id, sale_journal_name=j[0]["name"])

    ctx = {"active_model":"account.move","active_ids":[invoice_id],"active_id":invoice_id}
    wiz_id = exec_kw(uid, "account.move.reversal", "create", [{
        "journal_id": sale_journal_id,
        "reason": "ML FBM refund/cancel",
    }], context=ctx)
    step("reversal_wizard_created", wiz_id=wiz_id)

    action = exec_kw(uid, "account.move.reversal", "reverse_moves", [[wiz_id]], context=ctx)
    res_id = int(action.get("res_id") or 0) if isinstance(action, dict) else 0
    if not res_id:
        die(f"COULD_NOT_INFER_CREDIT_NOTE_FROM_ACTION action={action}")
    credit_id = res_id
    credit = exec_kw(uid, "account.move", "read", [[credit_id], ["id","name","state","payment_state","amount_total","amount_residual","reversed_entry_id","move_type"]])[0]
    step("credit_note_created", credit_note_id=credit_id, credit_note_name=credit["name"], state=credit["state"])

# 5) Postear credit note si está draft
credit = exec_kw(uid, "account.move", "read", [[credit_id], ["id","name","state","payment_state","amount_total","amount_residual"]])[0]
if credit["state"] == "draft":
    exec_kw(uid, "account.move", "action_post", [[credit_id]])
    credit = exec_kw(uid, "account.move", "read", [[credit_id], ["id","state","payment_state","amount_total","amount_residual"]])[0]
    step("credit_note_post", status="posted_now", state=credit["state"])
else:
    step("credit_note_post", status="already_posted", state=credit["state"])

# 6) Pagar credit note si residual > 0
credit = exec_kw(uid, "account.move", "read", [[credit_id], ["id","state","payment_state","amount_total","amount_residual"]])[0]
if float(credit.get("amount_residual") or 0.0) <= 0.0:
    step("credit_note_pay", status="already_paid", payment_state=credit["payment_state"], residual=str(credit["amount_residual"]))
else:
    jb = exec_kw(uid, "account.journal", "search_read", [[["type","=","bank"]]], {"fields":["id","name","type"], "limit": 1})
    if not jb:
        die("NO_BANK_JOURNAL_FOUND")
    bank_journal_id = int(jb[0]["id"])
    step("bank_journal", bank_journal_id=bank_journal_id, bank_journal_name=jb[0]["name"])

    ctx = {"active_model":"account.move","active_ids":[credit_id],"active_id":credit_id}
    pay_wiz = exec_kw(uid, "account.payment.register", "create", [{"journal_id": bank_journal_id}], context=ctx)
    step("payment_wizard_created", wiz_id=pay_wiz)
    exec_kw(uid, "account.payment.register", "action_create_payments", [[pay_wiz]], context=ctx)

    credit = exec_kw(uid, "account.move", "read", [[credit_id], ["id","payment_state","amount_residual","state"]])[0]
    step("credit_note_pay", status="paid_now", payment_state=credit["payment_state"], residual=str(credit["amount_residual"]))

# 7) Cancelar SO via wizard sale.order.cancel (action_cancel) — 1 SOLO step
so = exec_kw(
    uid,
    "sale.order",
    "read",
    [[so_id], ["id", "name", "state", "invoice_status", "picking_ids"]],
)[0]

if so["state"] == "cancel":
    step("so_cancel", status="already_cancelled", so_state=so["state"])
else:
    action = exec_kw(uid, "sale.order", "action_cancel", [[so_id]])
    ctx = action.get("context") if isinstance(action, dict) else {}
    ctx = ctx or {}

    order_id_ctx = int(ctx.get("default_order_id") or so_id)
    wiz_id = exec_kw(
        uid,
        "sale.order.cancel",
        "create",
        [{"order_id": order_id_ctx}],
        context=ctx if ctx else None,
    )
    exec_kw(
        uid,
        "sale.order.cancel",
        "action_cancel",
        [[wiz_id]],
        context=ctx if ctx else None,
    )

    so = exec_kw(
        uid,
        "sale.order",
        "read",
        [[so_id], ["id", "name", "state", "invoice_status", "picking_ids"]],
    )[0]
    if so["state"] != "cancel":
        die("SO_CANCEL_FAILED")

    step(
        "so_cancel",
        status="cancelled_now",
        so_state=so["state"],
        invoice_status=so.get("invoice_status"),
        pickings=len(so.get("picking_ids") or []),
    )

# Asegurar que so tenga name para el resumen final
# Releer SO con name incluido para resumen final
so = exec_kw(
    uid,
    "sale.order",
    "read",
    [[so_id], ["id", "name", "state", "invoice_status", "picking_ids"]],
)[0]

if so["state"] != "cancel":
    die("SO_CANCEL_FAILED")

final = {
    "client_order_ref": CLIENT_ORDER_REF,
    "so_id": so_id,
    "so_name": so["name"],
    "so_state": so["state"],
    "invoice_id": invoice_id,
    "invoice_name": invoice["name"],
    "credit_note_id": credit_id,
    "credit_note_state": credit["state"],
    "credit_note_payment_state": credit["payment_state"],
}

audit["result"] = {"status":"ok", "summary": final}
audit["ts_end_utc"] = utc_now_iso()

os.makedirs(AUDIT_DIR, exist_ok=True)
audit_path = os.path.join(AUDIT_DIR, f"{CLIENT_ORDER_REF}.json")
with open(audit_path, "w", encoding="utf-8") as f:
    json.dump(audit, f, ensure_ascii=False, indent=2)

print("[FBM_REFUND] AUDIT_WRITE_OK path=" + audit_path, flush=True)
print("[FBM_REFUND] FINAL_SUMMARY")
print(json.dumps(final, ensure_ascii=False, indent=2))
print("[FBM_REFUND] OK_DONE")
