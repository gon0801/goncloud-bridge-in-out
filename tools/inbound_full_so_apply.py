#!/usr/bin/env python3
"""
inbound_full_so_apply.py

CANÓNICO (FULL):
- Crea (o reutiliza) un sale.order CONTABLE para órdenes MercadoLibre FULL (fulfillment)
- NO confirma con action_confirm()
- NO crea picking / NO mueve stock / NO deltas
- Idempotente por client_order_ref = "MLFULL:<SITE>:<ML_ORDER_ID>"
- Gating duro: SKU debe existir en Odoo (default_code), active=true, sell_on_meli=true

Requisitos:
- ENV: ODOO_URL ODOO_DB ODOO_USER ODOO_PASSWORD
- (Opcional) ENV: INBOUND_MELI_PARTNER_ID (default 1)
- Tokens ML: /mnt/data/appdata/bridge/data/.meli_tokens.json

Uso:
  python3 inbound_full_so_apply.py --ml-order-id 2000...
  python3 inbound_full_so_apply.py --ml-order-id SIM-FULL-1 --order-json-file /tmp/sim_full_order.json
"""

import os
import sys
import json
import argparse
import sqlite3
import datetime
import requests
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = "/mnt/data/appdata/bridge/data/bridge.db"
TOK_PATH = "/mnt/data/appdata/bridge/data/.meli_tokens.json"
ML_API = "https://api.mercadolibre.com"
TIMEOUT = 30

# Flags FULL (sellado)
FULL_REQUIRED_FLAGS = {
    "meli_inbound_create_so_enabled": "1",
    "meli_inbound_confirm_so_enabled": "0",  # FULL NO usa action_confirm
    "meli_inbound_validate_picking_enabled": "0",  # FULL NO valida picking
    "meli_inbound_apply_stock_enabled": "0",  # FULL NO aplica stock por deltas
}


def die(msg: str, code: int = 2):
    print(f"[FULL_SO_APPLY] ERROR {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def utc_now():
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def short(s: Any, n: int = 900) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def get_env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        if default is not None:
            return default
        die(f"missing env {name}")
    return v


def db_flag_get(key: str) -> str:
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute("PRAGMA busy_timeout=5000;")
        row = con.execute(
            "SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,)
        ).fetchone()
        con.close()
        if not row or row[0] is None:
            return "0"
        return str(row[0])
    except Exception:
        # Si DB no está disponible, no “adivinamos”: fallamos claro.
        die(f"could not read bridge_settings from {DB_PATH}")


def enforce_full_flags():
    bad = []
    for k, want in FULL_REQUIRED_FLAGS.items():
        got = db_flag_get(k)
        if got != want:
            bad.append((k, got, want))
    if bad:
        lines = ", ".join([f"{k}={g} (want {w})" for (k, g, w) in bad])
        die(f"FULL flags mismatch: {lines}")


def load_ml_token() -> str:
    if not os.path.exists(TOK_PATH):
        die(f"missing token file {TOK_PATH}")
    try:
        with open(TOK_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        die(f"token file not valid json: {TOK_PATH} err={e!r}")
    tok = d.get("access_token")
    if not tok:
        die("MELI_TOKEN_MISSING: access_token not found")
    return str(tok)


def ml_get(path: str, tok: str) -> Dict[str, Any]:
    url = ML_API.rstrip("/") + path
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
    # ML devuelve JSON con error, pero también hay casos con HTML; protegemos.
    if r.status_code != 200:
        body = r.text.strip()
        # Mensaje útil si token expiró/invalidado
        if r.status_code in (401, 403):
            raise RuntimeError(f"HTTP {r.status_code} {r.reason} :: {body[:300]}")
        raise RuntimeError(f"HTTP {r.status_code} {r.reason} :: {body[:300]}")
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"HTTP 200 but non-json body: {r.text[:200]}")


def is_numeric_order_id(s: str) -> bool:
    return s.isdigit()


def load_order_json(ml_order_id: str, order_json_file: Optional[str]) -> Dict[str, Any]:
    if order_json_file:
        if not os.path.exists(order_json_file):
            die(f"order-json-file does not exist: {order_json_file}")
        try:
            with open(order_json_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            die(f"order-json-file invalid json: {order_json_file} err={e!r}")

    if not is_numeric_order_id(ml_order_id):
        die("SIM ids require --order-json-file (ml-order-id is not numeric)")

    tok = load_ml_token()
    try:
        return ml_get(f"/orders/{ml_order_id}", tok)
    except Exception as e:
        die(f"could not GET /orders/{ml_order_id}: {e!r}")


def detect_site(order: Dict[str, Any]) -> str:
    site = order.get("site_id") or order.get("site") or order.get("siteId")
    if site:
        return str(site).strip() or "UNKNOWN"
    return "UNKNOWN"


def detect_full(order: Dict[str, Any]) -> Tuple[bool, str]:
    """
    FULL si:
    - order.logistic_type == 'fulfillment'
    - o shipping/logistic_type == 'fulfillment' (si viene)
    - o shipment.logistic_type == 'fulfillment' (si tenemos shipping.id y lo podemos consultar)
    """
    lt = (order.get("logistic_type") or "").strip().lower()
    if lt == "fulfillment":
        return True, "order.logistic_type"

    shipping = order.get("shipping") or {}
    if isinstance(shipping, dict):
        lt2 = (shipping.get("logistic_type") or "").strip().lower()
        if lt2 == "fulfillment":
            return True, "shipping.logistic_type"

    # intento con shipment si hay shipping.id y el order vino de API real
    sid = None
    if isinstance(shipping, dict):
        sid = shipping.get("id")
    if sid and str(sid).isdigit():
        tok = load_ml_token()
        try:
            sh = ml_get(f"/shipments/{sid}", tok)
            lt3 = (sh.get("logistic_type") or "").strip().lower()
            if lt3 == "fulfillment":
                return True, "shipment.logistic_type"
        except Exception:
            # No reventamos por esto; FULL puede venir en el order, si no vino, lo tratamos como no FULL.
            pass

    return False, "not_fulfillment"


def extract_items(order: Dict[str, Any]) -> List[Tuple[str, int]]:
    items = order.get("order_items") or order.get("order_items_v2") or []
    if not isinstance(items, list) or not items:
        die("no order_items found")

    out: List[Tuple[str, int]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = it.get("quantity")
        try:
            qty_i = int(qty)
        except Exception:
            qty_i = 0
        item = it.get("item") or {}
        if not isinstance(item, dict):
            item = {}

        # Estrategia sellada: seller_sku directo si viene (orders trae item.seller_sku a veces)
        sku = (item.get("seller_sku") or "").strip()
        if not sku:
            # fallback (no inventar): algunas cargas traen seller_sku directo en it
            sku = (it.get("seller_sku") or "").strip()

        if not sku:
            die("missing seller_sku in order_items (FULL requires seller_sku)")

        if qty_i <= 0:
            die(f"bad quantity for sku={sku}: {qty!r}")

        out.append((sku, qty_i))

    # compactar por sku
    merged: Dict[str, int] = {}
    for sku, q in out:
        merged[sku] = merged.get(sku, 0) + int(q)
    return sorted(merged.items(), key=lambda x: x[0])


def odoo_auth(
    session: requests.Session, base_url: str, db: str, user: str, password: str
) -> int:
    url = base_url.rstrip("/") + "/web/session/authenticate"
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"db": db, "login": user, "password": password},
        "id": 1,
    }
    r = session.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(
            f"auth_error: {short(json.dumps(data['error'], ensure_ascii=False), 1200)}"
        )
    uid = (data.get("result") or {}).get("uid")
    if not uid:
        raise RuntimeError(
            f"auth_failed_no_uid body={short(json.dumps(data, ensure_ascii=False), 1200)}"
        )
    return int(uid)


def call_kw(
    session: requests.Session,
    base_url: str,
    model: str,
    method: str,
    args=None,
    kwargs=None,
):
    args = args or []
    kwargs = kwargs or {}
    url = base_url.rstrip("/") + f"/web/dataset/call_kw/{model}/{method}"
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs},
        "id": 1,
    }
    r = session.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(
            f"odoo_error model={model} method={method} err={short(json.dumps(data['error'], ensure_ascii=False), 1800)}"
        )
    return data.get("result")


def find_existing_so(
    session: requests.Session, ODOO_URL: str, ref: str
) -> Optional[Tuple[int, str, str]]:
    ids = (
        call_kw(
            session,
            ODOO_URL,
            "sale.order",
            "search",
            args=[[["client_order_ref", "=", ref]]],
            kwargs={"limit": 1},
        )
        or []
    )
    if not ids:
        return None
    so_id = int(ids[0])
    rows = (
        call_kw(
            session,
            ODOO_URL,
            "sale.order",
            "read",
            args=[[so_id], ["name", "state", "client_order_ref"]],
            kwargs={},
        )
        or []
    )
    if not rows:
        return (so_id, "", "")
    r0 = rows[0]
    return (so_id, str(r0.get("name") or ""), str(r0.get("state") or ""))


def product_gate(session: requests.Session, ODOO_URL: str, sku: str) -> Tuple[int, str]:
    rows = (
        call_kw(
            session,
            ODOO_URL,
            "product.product",
            "search_read",
            args=[[["default_code", "=", sku], ["active", "=", True]]],
            kwargs={
                "fields": ["id", "default_code", "sell_on_meli", "name"],
                "limit": 1,
            },
        )
        or []
    )
    if not rows:
        die(f"sku_not_in_odoo sku={sku}")
    p = rows[0]
    if not bool(p.get("sell_on_meli", False)):
        die(f"sku_not_allowed_for_meli sku={sku}")
    pid = int(p["id"])
    nm = p.get("name")
    return pid, (nm if isinstance(nm, str) else json.dumps(nm, ensure_ascii=False))


def create_so_draft(
    session: requests.Session,
    ODOO_URL: str,
    partner_id: int,
    ref: str,
    lines: List[Tuple[int, int]],
    note: str,
) -> Tuple[int, str]:
    so_id = call_kw(
        session,
        ODOO_URL,
        "sale.order",
        "create",
        args=[
            {
                "partner_id": partner_id,
                "client_order_ref": ref,
                "origin": ref,
                "note": note,
            }
        ],
        kwargs={},
    )
    if not so_id:
        die("odoo create sale.order returned empty")
    so_id = int(so_id)

    # crear líneas con price_unit placeholder
    for product_id, qty in lines:
        _ = call_kw(
            session,
            ODOO_URL,
            "sale.order.line",
            "create",
            args=[
                {
                    "order_id": so_id,
                    "product_id": int(product_id),
                    "product_uom_qty": float(qty),
                    "price_unit": 1.0,
                }
            ],
            kwargs={},
        )

    # leer nombre
    rows = (
        call_kw(
            session,
            ODOO_URL,
            "sale.order",
            "read",
            args=[[so_id], ["name"]],
            kwargs={},
        )
        or []
    )
    so_name = ""
    if rows:
        so_name = str(rows[0].get("name") or "")
    return so_id, so_name


def try_update_inbound_sales_orders(
    site: str, ml_order_id: str, status: str, so_id: int, so_name: str
):
    """
    No rompemos si la tabla no existe. Es “best effort” para auditoría.
    """
    dedupe_key = f"so-plan:{site}:{ml_order_id}"
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute("PRAGMA busy_timeout=5000;")
        # Si no existe tabla, esto falla y salimos silencioso.
        con.execute(
            """
            INSERT OR IGNORE INTO inbound_sales_orders(dedupe_key, ml_order_id, site, status, odoo_so_id, odoo_name, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
        """,
            (
                dedupe_key,
                str(ml_order_id),
                str(site),
                str(status),
                int(so_id),
                str(so_name),
                utc_now(),
                utc_now(),
            ),
        )
        con.execute(
            """
            UPDATE inbound_sales_orders
               SET site=?,
                   status=?,
                   odoo_so_id=?,
                   odoo_name=?,
                   updated_at=?
             WHERE dedupe_key=?
        """,
            (str(site), str(status), int(so_id), str(so_name), utc_now(), dedupe_key),
        )
        con.commit()
        con.close()
    except Exception:
        try:
            con.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="FULL: create contable SO (draft) without stock/picking/procurement."
    )
    ap.add_argument(
        "--ml-order-id", required=True, help="numeric ML order id or SIM id"
    )
    ap.add_argument("--order-json-file", help="SIM only: order JSON file path")
    args = ap.parse_args()

    enforce_full_flags()

    ODOO_URL = get_env("ODOO_URL")
    ODOO_DB = get_env("ODOO_DB")
    ODOO_USER = get_env("ODOO_USER")
    ODOO_PASSWORD = get_env("ODOO_PASSWORD")
    partner_id = int(get_env("INBOUND_MELI_PARTNER_ID", "1"))

    # Load order JSON (real or sim)
    order = load_order_json(args.ml_order_id, args.order_json_file)
    site = detect_site(order)
    ml_order_id = str(order.get("id") or args.ml_order_id)

    is_full, how = detect_full(order)
    if not is_full:
        die(f"order is not FULL (fulfillment). detected={how}")

    # Extract items (seller_sku + qty)
    sku_qty = extract_items(order)

    ref = f"MLFULL:{site}:{ml_order_id}"

    # Odoo session
    session = requests.Session()
    try:
        _uid = odoo_auth(session, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)
    except Exception as e:
        die(f"odoo auth failed: {e!r}")

    # Idempotency: already exists?
    ex = None
    try:
        ex = find_existing_so(session, ODOO_URL, ref)
    except Exception as e:
        die(f"odoo lookup existing SO failed: {e!r}")

    if ex:
        so_id, so_name, so_state = ex
        # Actualiza auditoría best-effort
        try_update_inbound_sales_orders(
            site, ml_order_id, "created_full", so_id, so_name
        )
        print(
            f"[FULL_SO_APPLY] OK already_created so_id={so_id} name={so_name} ref={ref} state={so_state}",
            flush=True,
        )
        return

    # Gate SKUs + prepare lines
    lines: List[Tuple[int, int]] = []
    for sku, qty in sku_qty:
        pid, _nm = product_gate(session, ODOO_URL, sku)
        lines.append((pid, int(qty)))

    note = (
        "ML FULL (fulfillment) — SO CONTABLE SIN STOCK/SIN PICKING.\n"
        f"REF={ref}\n"
        "CANÓNICO: NO action_confirm(); NO procurement; NO deltas."
    )

    try:
        so_id, so_name = create_so_draft(
            session, ODOO_URL, partner_id, ref, lines, note
        )
    except Exception as e:
        die(f"odoo create draft SO failed: {e!r}")

    try_update_inbound_sales_orders(site, ml_order_id, "created_full", so_id, so_name)

    print(
        f"[FULL_SO_APPLY] OK created so_id={so_id} name={so_name} ref={ref} lines_ok={len(lines)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
