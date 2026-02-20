#!/usr/bin/env python3
"""
inbound_full_so_invoice.py

CANÓNICO FULL (CONTABILIDAD):
- Crea y POSTEA (action_post) una invoice para una Sale Order FULL
- NO usa stock / NO usa picking
- Usa wizard: sale.advance.payment.inv con advance_payment_method='delivered'
- IMPORTANTÍSIMO: create_invoices() usa CONTEXT (active_model/active_ids), NO args extra

Uso:
  python3 inbound_full_so_invoice.py --so-id 274
  python3 inbound_full_so_invoice.py --client-order-ref MLFULL:MLM:SIM-FULL-TEST
"""

import os
import sys
import json
import argparse
import requests


def die(msg, code=2):
    print(f"[FULL_INVOICE] ERROR {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def env_required(k):
    v = os.getenv(k)
    if not v:
        die(f"missing env {k}")
    return v


def odoo_call(url, db, uid, pw, model, method, args=None, kwargs=None):
    if args is None:
        args = []
    if kwargs is None:
        kwargs = {}

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
        die(f"odoo error: {data['error']}")
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
        die(f"odoo auth error: {data['error']}")
    uid = data.get("result")
    if not uid:
        die("authentication failed (uid is null)")
    return uid


def find_so_id(url, db, uid, pw, so_id=None, client_order_ref=None):
    if so_id:
        return so_id
    if not client_order_ref:
        die("provide --so-id or --client-order-ref")

    ids = odoo_call(
        url, db, uid, pw,
        "sale.order", "search",
        args=[[["client_order_ref", "=", client_order_ref]]],
        kwargs={"limit": 1},
    )
    if not ids:
        die(f"sale.order not found for client_order_ref={client_order_ref}")
    return ids[0]


def extract_invoice_ids_from_action(action):
    """
    create_invoices suele devolver un action dict.
    Intentamos sacar invoice ids de:
    - res_id (int)
    - res_ids (list)
    - domain: [('id','in',[...])]
    """
    if action is None:
        return []

    # a veces Odoo devuelve bool True/False (raro)
    if isinstance(action, bool):
        return []

    # list directo
    if isinstance(action, list):
        # podría ser lista de ids o cosas
        if all(isinstance(x, int) for x in action):
            return action
        return []

    if not isinstance(action, dict):
        return []

    if isinstance(action.get("res_id"), int):
        return [action["res_id"]]

    if isinstance(action.get("res_ids"), list):
        ids = [x for x in action["res_ids"] if isinstance(x, int)]
        return ids

    dom = action.get("domain")
    if isinstance(dom, list):
        for term in dom:
            if (
                isinstance(term, (list, tuple))
                and len(term) == 3
                and term[0] == "id"
                and term[1] == "in"
                and isinstance(term[2], list)
            ):
                ids = [x for x in term[2] if isinstance(x, int)]
                return ids

    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--so-id", type=int)
    ap.add_argument("--client-order-ref")
    args = ap.parse_args()

    url = env_required("ODOO_URL").rstrip("/")
    db = env_required("ODOO_DB")
    user = env_required("ODOO_USER")
    pw = env_required("ODOO_PASSWORD")

    uid = odoo_login(url, db, user, pw)
    so_id = find_so_id(url, db, uid, pw, args.so_id, args.client_order_ref)

    so = odoo_call(
        url, db, uid, pw,
        "sale.order", "read",
        args=[[so_id], ["id", "name", "state", "client_order_ref"]],
    )[0]

    ref = so.get("client_order_ref") or ""
    print(f"[FULL_INVOICE] INFO so_id={so_id} name={so.get('name')} state={so.get('state')} ref={ref}")

    if so.get("state") != "sale":
        die("sale.order must be in state 'sale' before invoicing (run FULL confirm-no-picking first)")

    # Context CANÓNICO: wizard usa active_model/active_ids
    ctx = {
        "active_model": "sale.order",
        "active_ids": [so_id],
        "active_id": so_id,
    }

    # 1) Crear wizard
    wiz_id = odoo_call(
        url, db, uid, pw,
        "sale.advance.payment.inv", "create",
        args=[{"advance_payment_method": "delivered"}],
        kwargs={"context": ctx},
    )
    if not wiz_id:
        die("could not create sale.advance.payment.inv wizard")

    # 2) Ejecutar wizard (SIN args extra; solo recordset + context)
    action = odoo_call(
        url, db, uid, pw,
        "sale.advance.payment.inv", "create_invoices",
        args=[[wiz_id]],
        kwargs={"context": ctx},
    )

    invoice_ids = extract_invoice_ids_from_action(action)
    if not invoice_ids:
        # fallback: buscar invoices del SO por origin/ref
        # (sale.order.name suele quedar como invoice_origin)
        so_name = so.get("name")
        if so_name:
            inv_ids = odoo_call(
                url, db, uid, pw,
                "account.move", "search",
                args=[[["move_type", "=", "out_invoice"], ["invoice_origin", "=", so_name]]],
                kwargs={"limit": 10},
            )
            invoice_ids = inv_ids or []

    if not invoice_ids:
        die(f"no invoice ids found. action={json.dumps(action)[:400]}")

    # 3) Postear invoice(s)
    odoo_call(
        url, db, uid, pw,
        "account.move", "action_post",
        args=[invoice_ids],
    )

    print(f"[FULL_INVOICE] OK invoices_posted ids={invoice_ids}")


if __name__ == "__main__":
    main()
