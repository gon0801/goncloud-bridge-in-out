#!/usr/bin/env python3
import os
import sys
import argparse
import requests


def die(msg: str, code: int = 2):
    print(f"[FULL_CONFIRM_NOPICK] ERROR {msg}", file=sys.stderr)
    sys.exit(code)


def env_required(k: str) -> str:
    v = os.getenv(k)
    if not v:
        die(f"missing env {k}")
    return v


def odoo_call(url, db, uid, password, model, method, args=None, kwargs=None):
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
            "args": [db, uid, password, model, method, args, kwargs],
        },
        "id": 1,
    }
    r = requests.post(url.rstrip("/") + "/jsonrpc", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        die(f"odoo error: {data['error']}")
    return data.get("result")


def odoo_login(url, db, user, password):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "authenticate",
            "args": [db, user, password, {}],
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


def find_sale_order_id(url, db, uid, password, client_order_ref=None, so_id=None):
    if so_id:
        return so_id
    if not client_order_ref:
        die("provide --so-id or --client-order-ref")
    ids = odoo_call(
        url,
        db,
        uid,
        password,
        "sale.order",
        "search",
        args=[[["client_order_ref", "=", client_order_ref]]],
        kwargs={"limit": 1},
    )
    if not ids:
        die(f"sale.order not found for client_order_ref={client_order_ref}")
    return ids[0]


def read_sale_order(url, db, uid, password, so_id):
    recs = odoo_call(
        url,
        db,
        uid,
        password,
        "sale.order",
        "read",
        args=[[so_id], ["id", "name", "state", "client_order_ref"]],
    )
    if not recs:
        die(f"sale.order read failed id={so_id}")
    return recs[0]


def cancel_pickings_for_sale(url, db, uid, password, so_id):
    picking_ids = odoo_call(
        url,
        db,
        uid,
        password,
        "stock.picking",
        "search",
        args=[[["sale_id", "=", so_id]]],
    )
    if not picking_ids:
        print("[FULL_CONFIRM_NOPICK] OK no_pickings_found")
        return 0

    odoo_call(
        url,
        db,
        uid,
        password,
        "stock.picking",
        "action_cancel",
        args=[picking_ids],
    )
    print(
        f"[FULL_CONFIRM_NOPICK] OK pickings_cancelled count={len(picking_ids)} ids={picking_ids}"
    )
    return len(picking_ids)


def force_sale_state_no_procurement(url, db, uid, password, so_id):
    # NO usamos action_confirm(): dispara procurement/stock en tu Odoo
    ok = odoo_call(
        url,
        db,
        uid,
        password,
        "sale.order",
        "write",
        args=[[so_id], {"state": "sale"}],
    )
    if not ok:
        die("sale.order.write returned false")
    print("[FULL_CONFIRM_NOPICK] OK forced_state_sale (no procurement)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-order-ref", help="e.g. MLFULL:MLM:SIM-FULL-4")
    ap.add_argument("--so-id", type=int, help="sale.order id")
    args = ap.parse_args()

    url = env_required("ODOO_URL")
    db = env_required("ODOO_DB")
    user = env_required("ODOO_USER")
    pw = env_required("ODOO_PASSWORD")

    uid = odoo_login(url, db, user, pw)
    so_id = find_sale_order_id(
        url, db, uid, pw, client_order_ref=args.client_order_ref, so_id=args.so_id
    )

    before = read_sale_order(url, db, uid, pw, so_id)
    print(
        f"[FULL_CONFIRM_NOPICK] INFO before so_id={so_id} name={before.get('name')} "
        f"state={before.get('state')} ref={before.get('client_order_ref')}"
    )

    force_sale_state_no_procurement(url, db, uid, pw, so_id)
    cancel_pickings_for_sale(url, db, uid, pw, so_id)

    after = read_sale_order(url, db, uid, pw, so_id)
    print(
        f"[FULL_CONFIRM_NOPICK] INFO after so_id={so_id} name={after.get('name')} "
        f"state={after.get('state')} ref={after.get('client_order_ref')}"
    )

    if after.get("state") != "sale":
        die(f"expected state 'sale' but got {after.get('state')}", code=3)

    print("[FULL_CONFIRM_NOPICK] OK sale_in_orders_no_stock")


if __name__ == "__main__":
    main()
