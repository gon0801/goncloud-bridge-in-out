#!/usr/bin/env python3
"""
inbound_full_so_pay_and_reconcile.py

CANÓNICO FULL (CONTABILIDAD):
- Encuentra la Sale Order por client_order_ref (MLFULL:...)
- Encuentra la invoice (account.move) asociada a esa SO
- Registra pago usando el wizard oficial: account.payment.register
- Deja la invoice en paid (residual=0) y la reconciliación hecha

NO toca stock, NO pickings.

Requiere ENV:
  ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD

Uso:
  python3 inbound_full_so_pay_and_reconcile.py --client-order-ref MLFULL:MLM:SIM-FULL-TEST

Opcional:
  --journal-id 6                 (default: 6 = "Bank" en tu DB)
  --payment-date YYYY-MM-DD      (default: hoy UTC)
  --method-line-id <ID>          (si quieres forzar payment_method_line_id)
"""

import os
import sys
import json
import argparse
import datetime
import requests


def die(msg: str, code: int = 2):
    print(f"[FULL_PAY] ERROR {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def env_required(k: str) -> str:
    v = os.getenv(k)
    if not v:
        die(f"missing env {k}")
    return v


def odoo_call(url, db, uid, pw, model, method, args=None, kwargs=None, context=None):
    if args is None:
        args = []
    if kwargs is None:
        kwargs = {}
    if context is not None:
        kwargs = dict(kwargs)
        kwargs["context"] = context

    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [db, uid, pw, model, method, args, kwargs],
        },
        "id": 1,
    }
    r = requests.post(url.rstrip("/") + "/jsonrpc", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        # recorta para que no sea infinito
        die(json.dumps(data["error"])[:2000])
    return data.get("result")


def odoo_login(url, db, user, pw):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "authenticate",
            "args": [db, user, pw, {}],
        },
        "id": 1,
    }
    r = requests.post(url.rstrip("/") + "/jsonrpc", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        die(f"odoo auth error: {json.dumps(data['error'])[:1200]}")
    uid = data.get("result")
    if not uid:
        die("authentication failed (uid is null)")
    return uid

import datetime

def today_utc_date():
    return datetime.datetime.now(datetime.UTC).date().isoformat()

def find_sale_order(url, db, uid, pw, client_order_ref: str):
    ids = odoo_call(
        url, db, uid, pw,
        "sale.order", "search",
        args=[[["client_order_ref", "=", client_order_ref]]],
        kwargs={"limit": 1},
    )
    if not ids:
        die(f"sale.order not found for client_order_ref={client_order_ref}")
    so_id = ids[0]
    so = odoo_call(
        url, db, uid, pw,
        "sale.order", "read",
        args=[[so_id], ["id", "name", "state", "client_order_ref"]],
    )[0]
    return so_id, so


def find_invoice_for_sale(url, db, uid, pw, sale_name: str):
    # en tus queries previas: invoice_origin = S00xxx
    inv_ids = odoo_call(
        url, db, uid, pw,
        "account.move", "search",
        args=[[["move_type", "=", "out_invoice"], ["invoice_origin", "=", sale_name]]],
        kwargs={"limit": 10, "order": "id desc"},
    )
    if not inv_ids:
        die(f"no invoice found for sale.order name={sale_name} (invoice_origin)")
    inv_id = inv_ids[0]
    inv = odoo_call(
        url, db, uid, pw,
        "account.move", "read",
        args=[[inv_id], ["id", "name", "state", "move_type", "amount_total", "amount_residual", "payment_state"]],
    )[0]
    return inv_id, inv


def register_payment_for_invoice(url, db, uid, pw, inv_id: int, inv: dict, journal_id: int, payment_date: str, method_line_id=None, communication=None):
    residual = float(inv.get("amount_residual") or 0.0)
    if residual <= 0.00001:
        print(f"[FULL_PAY] OK already_paid invoice={inv.get('name')} residual={residual}", flush=True)
        return True

    ctx = {
        "active_model": "account.move",
        "active_ids": [inv_id],
        "active_id": inv_id,
    }

    vals = {
        "payment_date": payment_date,
        "amount": residual,
        "journal_id": journal_id,
    }
    if communication:
        vals["communication"] = communication
    if method_line_id:
        vals["payment_method_line_id"] = method_line_id

    wiz_id = odoo_call(
        url, db, uid, pw,
        "account.payment.register", "create",
        args=[vals],
        context=ctx,
    )

    # Ejecuta el wizard oficial: crea pago(s) y reconcilia
    odoo_call(
        url, db, uid, pw,
        "account.payment.register", "action_create_payments",
        args=[[wiz_id]],
        context=ctx,
    )

    # Relee invoice para verificar que quedó pagada
    inv2 = odoo_call(
        url, db, uid, pw,
        "account.move", "read",
        args=[[inv_id], ["id", "name", "amount_residual", "payment_state", "state"]],
    )[0]
    residual2 = float(inv2.get("amount_residual") or 0.0)
    print(f"[FULL_PAY] INFO after invoice={inv2.get('name')} state={inv2.get('state')} payment_state={inv2.get('payment_state')} residual={residual2}", flush=True)

    if residual2 > 0.00001:
        die(f"payment did not reconcile fully (residual={residual2})", code=3)

    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-order-ref", required=True, help="e.g. MLFULL:MLM:SIM-FULL-TEST")
    ap.add_argument("--journal-id", type=int, default=6, help="bank/cash journal id (default 6=Bank)")
    ap.add_argument("--payment-date", default=None, help="YYYY-MM-DD (default today UTC)")
    ap.add_argument("--method-line-id", type=int, default=None, help="optional account.payment.method.line id")
    args = ap.parse_args()

    url = env_required("ODOO_URL").rstrip("/")
    db = env_required("ODOO_DB")
    user = env_required("ODOO_USER")
    pw = env_required("ODOO_PASSWORD")

    pay_date = args.payment_date or today_utc_date()

    uid = odoo_login(url, db, user, pw)

    so_id, so = find_sale_order(url, db, uid, pw, args.client_order_ref)
    print(f"[FULL_PAY] INFO so_id={so_id} name={so.get('name')} ref={so.get('client_order_ref')} state={so.get('state')}", flush=True)

    inv_id, inv = find_invoice_for_sale(url, db, uid, pw, so.get("name"))
    print(f"[FULL_PAY] INFO invoice={inv.get('name')} state={inv.get('state')} payment_state={inv.get('payment_state')} residual={inv.get('amount_residual')}", flush=True)

    communication = f"PAY:{args.client_order_ref}"

    register_payment_for_invoice(
        url, db, uid, pw,
        inv_id, inv,
        journal_id=args.journal_id,
        payment_date=pay_date,
        method_line_id=args.method_line_id,
        communication=communication,
    )

    print("[FULL_PAY] OK paid_and_reconciled", flush=True)


if __name__ == "__main__":
    main()
