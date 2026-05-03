#!/usr/bin/env python3
import os, sys, json, requests, re, time
from datetime import date, datetime, timezone
from pathlib import Path

# ================= CONFIG (ENV) =================
URL  = os.getenv("ODOO_URL","").rstrip("/")
DB   = os.getenv("ODOO_DB","")
USER = os.getenv("ODOO_USER","")
PW   = os.getenv("ODOO_PASSWORD","")

CLIENT_ORDER_REF = os.getenv("CLIENT_ORDER_REF","").strip()
AUDIT_DIR = os.getenv("AUDIT_DIR", "/mnt/data/appdata/bridge/audit/full_refund").strip()
# ===============================================

LOG_PREFIX = "[FULL_REFUND]"

def log(msg, **kv):
    parts = [LOG_PREFIX, msg]
    if kv:
        parts.append(" ".join([f"{k}={kv[k]}" for k in sorted(kv.keys())]))
    print(" ".join(parts), flush=True)

def die(m, code=2, **kv):
    log("ERROR " + m, **kv)
    write_audit(audit)
    sys.exit(code)

def sanitize_filename(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[^A-Za-z0-9._:-]+", "_", s)
    return s[:200]

def write_audit(payload: dict):
    try:
        d = Path(AUDIT_DIR)
        d.mkdir(parents=True, exist_ok=True)
        fn = sanitize_filename(CLIENT_ORDER_REF) or f"full_refund_{int(time.time())}"
        path = d / f"{fn}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        log("AUDIT_WRITE_OK", path=str(path))
    except Exception as e:
        log("AUDIT_WRITE_FAILED", err=str(e))

def jcall(service, method, args):
    payload = {
        "jsonrpc":"2.0",
        "method":"call",
        "params":{"service":service,"method":method,"args":args},
        "id":1
    }
    r = requests.post(URL + "/jsonrpc", json=payload, timeout=30)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        die("rpc_error", detail=json.dumps(d["error"], ensure_ascii=False)[:8000])
    return d["result"]

def exec_kw(uid, model, method, args=None, kwargs=None):
    return jcall("object","execute_kw",[DB, uid, PW, model, method, args or [], kwargs or {}])

# ================= START =================
for k,v in {
    "ODOO_URL":URL,"ODOO_DB":DB,"ODOO_USER":USER,
    "ODOO_PASSWORD":PW,"CLIENT_ORDER_REF":CLIENT_ORDER_REF
}.items():
    if not v:
        die("missing_env", key=k)

uid = jcall("common","authenticate",[DB, USER, PW, {}])
log("AUTH_OK", uid=uid)

audit = {
    "ts_start_utc": datetime.now(timezone.utc).isoformat(),
    "client_order_ref": CLIENT_ORDER_REF,
    "steps": [],
    "result": None,
}

def step(name, **data):
    audit["steps"].append({"step":name,"ts":datetime.now(timezone.utc).isoformat(),**data})
    log(f"STEP={name}", **data)

# ============== HELPERS =================

_order_id_for_search = CLIENT_ORDER_REF.rsplit(":", 1)[-1]

def get_so():
    rows = exec_kw(uid,"sale.order","search_read",
        [[["client_order_ref","=like",f"{_order_id_for_search}%"]]],
        {"fields":["id","name","state","invoice_ids","picking_ids"],"limit":2}
    )
    if not rows:
        die("so_not_found")
    if len(rows) > 1:
        die(f"so_multiple_match: {[r['name'] for r in rows]}")
    return rows[0]

def get_posted_invoice(so):
    invs = exec_kw(uid,"account.move","read",[so["invoice_ids"]],
        {"fields":["id","name","state","payment_state","amount_residual","reversed_entry_id"]}
    )
    posted = [i for i in invs if i["state"]=="posted"]
    if not posted:
        die("no_posted_invoice")
    return posted[-1]

def get_or_create_credit(invoice_id):
    rows = exec_kw(uid,"account.move","search_read",
        [[["move_type","=","out_refund"],["reversed_entry_id","=",invoice_id]]],
        {"limit":1}
    )
    if rows:
        return rows[0], "existing"

    wiz = exec_kw(uid,"account.move.reversal","create",[{
        "reason":"ML FULL refund",
        "date":str(date.today())
    }],{"context":{"active_model":"account.move","active_ids":[invoice_id]}})

    res = exec_kw(uid,"account.move.reversal","reverse_moves",[[wiz]],
        {"context":{"active_model":"account.move","active_ids":[invoice_id]}}
    )
    cid = res.get("res_id")
    cn = exec_kw(uid,"account.move","read",[[cid]],
        {"fields":["id","name","state","payment_state","amount_residual"]}
    )[0]
    return cn, "created"

def post_credit(cn):
    """Postear credit note. Bug #6: si falla → unlink draft (no consume secuencia)."""
    if cn["state"] != "draft":
        return
    try:
        exec_kw(uid,"account.move","action_post",[[cn["id"]]])
    except Exception as e:
        try:
            _check = exec_kw(uid,"account.move","read",[[cn["id"]],["state"]])[0]
            if _check["state"] == "draft":
                exec_kw(uid,"account.move","unlink",[[cn["id"]]])
                step("compensation_credit_unlinked", credit_id=cn["id"])
        except Exception as ce:
            step("compensation_failed", credit_id=cn["id"], error=str(ce)[:200])
        die(f"credit note post failed: {e}")

def pay_credit(cn):
    """Pagar credit note. Bug #6: si falla, credit YA posted → die manual,
    no la cancelamos (rompería secuencia). Operador aplica el pago en Odoo UI."""
    if float(cn["amount_residual"]) == 0.0:
        return
    try:
        wiz = exec_kw(uid,"account.payment.register","create",[{}],
            {"context":{"active_model":"account.move","active_ids":[cn["id"]]}})
        exec_kw(uid,"account.payment.register","action_create_payments",[[wiz]],
            {"context":{"active_model":"account.move","active_ids":[cn["id"]]}})
    except Exception as e:
        die(f"payment register failed (credit note {cn['name']} POSTED, register payment manualmente en Odoo): {e}")

def cancel_so(so):
    so_id = so["id"]
    ret = exec_kw(uid,"sale.order","action_cancel",[[so_id]])
    so2 = exec_kw(uid,"sale.order","read",[[so_id]],{"fields":["state"]})[0]
    if so2["state"]!="cancel":
        wiz = exec_kw(uid,"sale.order.cancel","create",[{"order_id":so_id}])
        exec_kw(uid,"sale.order.cancel","action_cancel",[[wiz]])
    so3 = exec_kw(uid,"sale.order","read",[[so_id]],{"fields":["state"]})[0]
    if so3["state"]!="cancel":
        die("so_cancel_failed")

# ============== FLOW ====================
# Bug #6: removido el wrap try/except global porque borraba la distinción entre
# tipos de errores. Cada paso ahora muere con mensaje específico via die() (los
# helpers tienen su propia compensation cuando aplica). El wrap final solo captura
# excepciones que NO son SystemExit (errores de programación como AttributeError).

try:
    step("input")

    so = get_so()
    step("so_found", so_id=so["id"], so_state=so["state"])

    if so["picking_ids"]:
        die("has_pickings")

    inv = get_posted_invoice(so)
    step("invoice_found", invoice_id=inv["id"], payment_state=inv["payment_state"])

    cn, src = get_or_create_credit(inv["id"])
    step("credit_note_ready", credit_note_id=cn["id"], source=src)

    post_credit(cn)
    cn = exec_kw(uid,"account.move","read",[[cn["id"]]],
        {"fields":["id","name","state","payment_state","amount_residual"]})[0]
    step("credit_note_posted", state=cn["state"])

    pay_credit(cn)
    step("credit_note_paid")

    cancel_so(so)
    step("so_cancelled")

    audit["result"] = "ok"
    write_audit(audit)
    log("OK_DONE")

except SystemExit:
    raise
except Exception as e:
    die("unhandled_exception", err=str(e))
