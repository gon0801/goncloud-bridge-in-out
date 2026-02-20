#!/usr/bin/env python3
"""
AMAZON FBA REFUND AND CANCEL — GONCLOUD BRIDGE
Crea Credit Note y cancela SO para órdenes FBA canceladas.

ENV requeridas:
- ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS
- CLIENT_ORDER_REF (ej: AMZFBA:A1AM78C64UM0Y8:123-456-789)
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
CLIENT_ORDER_REF = os.getenv("CLIENT_ORDER_REF") or ""

def die(msg, code=2):
    print(f"[FBA_REFUND] ERROR {msg}", file=sys.stderr)
    sys.exit(code)

for k, v in [("ODOO_URL", ODOO_URL), ("ODOO_DB", DB), ("ODOO_USER", USER), ("ODOO_PASS", PW), ("CLIENT_ORDER_REF", CLIENT_ORDER_REF)]:
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

print(f"[FBA_REFUND] ref={CLIENT_ORDER_REF}")

# =========================
# Authenticate
# =========================
uid = jcall("common", "authenticate", [DB, USER, PW, {}])
if not uid:
    die("authentication_failed")

# =========================
# Find SO
# =========================
sos = exec_kw(uid, "sale.order", "search_read",
    [[["client_order_ref", "=", CLIENT_ORDER_REF]]],
    {"fields": ["id", "name", "state"], "limit": 1})

if not sos:
    print(f"[FBA_REFUND] SO not found, nothing to cancel")
    sys.exit(2)  # RC=2 means SO doesn't exist yet

so = sos[0]
print(f"[FBA_REFUND] found SO: {so['name']} state={so['state']}")

if so["state"] == "cancel":
    print(f"[FBA_REFUND] SO already cancelled")
    sys.exit(0)

# =========================
# Find and reverse invoices
# =========================
invoices = exec_kw(uid, "account.move", "search_read",
    [[["invoice_origin", "=", so["name"]], ["move_type", "=", "out_invoice"], ["state", "=", "posted"]]],
    {"fields": ["id", "name", "state", "payment_state", "journal_id"]})

for inv in invoices:
    print(f"[FBA_REFUND] processing invoice: {inv['name']}")
    
    # Check if already has reversal
    reversals = exec_kw(uid, "account.move", "search_read",
        [[["reversed_entry_id", "=", inv["id"]], ["state", "=", "posted"]]],
        {"fields": ["id", "name"], "limit": 1})
    
    if reversals:
        print(f"[FBA_REFUND] invoice already reversed: {reversals[0]['name']}")
        continue
    
    # Create reversal
    try:
        ctx = {"active_model": "account.move", "active_ids": [inv["id"]]}
        wiz_id = exec_kw(uid, "account.move.reversal", "create",
            [{"reason": "Amazon FBA refund/cancel", "journal_id": inv["journal_id"][0] if inv.get("journal_id") else 1}],
            {"context": ctx})
        
        result = exec_kw(uid, "account.move.reversal", "refund_moves", [[wiz_id]], {"context": ctx})
        print(f"[FBA_REFUND] credit note created for {inv['name']}")
        
        # Post the credit note
        if result and "res_id" in result:
            cn_id = result["res_id"]
            cn = exec_kw(uid, "account.move", "read", [[cn_id], ["state", "name"]])[0]
            if cn["state"] == "draft":
                exec_kw(uid, "account.move", "action_post", [[cn_id]])
                print(f"[FBA_REFUND] credit note posted: {cn['name']}")
    except Exception as e:
        print(f"[FBA_REFUND] WARN reversal failed for {inv['name']}: {e}")

# =========================
# Cancel SO via wizard
# =========================
if so["state"] != "cancel":
    try:
        action = exec_kw(uid, "sale.order", "action_cancel", [[so["id"]]])
        ctx = action.get("context") if isinstance(action, dict) else {}
        ctx = ctx or {}
        order_id_ctx = int(ctx.get("default_order_id") or so["id"])
        wiz_id = exec_kw(uid, "sale.order.cancel", "create", [{"order_id": order_id_ctx}], {"context": ctx} if ctx else None)
        exec_kw(uid, "sale.order.cancel", "action_cancel", [[wiz_id]], {"context": ctx} if ctx else None)
        so = exec_kw(uid, "sale.order", "read", [[so["id"]], ["state"]])[0]
        print(f"[FBA_REFUND] SO cancelled: {so['state']}")
    except Exception as e:
        print(f"[FBA_REFUND] WARN SO cancel failed: {e}")

print(f"[FBA_REFUND] OK_DONE ref={CLIENT_ORDER_REF}")
