#!/usr/bin/env python3
import os
import sys
import json
import argparse
import sqlite3
import datetime
import requests

DB_PATH = "/mnt/data/appdata/bridge/data/bridge.db"
TIMEOUT = 30
TOL = 1e-6

def utc_now():
    # ISO UTC con Z
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def get_env(name):
    v = os.getenv(name)
    if not v:
        die(f"FALTA ENV {name}. Define {name} en tu entorno antes de correr apply.")
    return v

def short(s, n=900):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"

def get_flag(key):
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    row = con.execute("SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,)).fetchone()
    con.close()
    return str(row[0]) if row and row[0] is not None else "0"

def read_bridge_delta(delta_id):
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    row = con.execute(
        "SELECT delta_id, sku, qty_delta, applied_to_odoo FROM inbound_stock_deltas WHERE delta_id=? LIMIT 1",
        (delta_id,),
    ).fetchone()
    con.close()
    return row

def mark_applied(delta_id, odoo_ref):
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute(
        "UPDATE inbound_stock_deltas SET applied_to_odoo=1, applied_at=?, odoo_ref=? WHERE delta_id=?",
        (utc_now(), odoo_ref, delta_id),
    )
    con.commit()
    con.close()

def odoo_auth(session: requests.Session, base_url: str, db: str, user: str, password: str) -> int:
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
        raise RuntimeError(f"auth_error: {short(json.dumps(data['error'], ensure_ascii=False), 1200)}")
    uid = (data.get("result") or {}).get("uid")
    if not uid:
        raise RuntimeError(f"auth_failed_no_uid body={short(json.dumps(data, ensure_ascii=False), 1200)}")
    return int(uid)

def call_kw(session: requests.Session, base_url: str, model: str, method: str, args=None, kwargs=None):
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
            f"odoo_error model={model} method={method} "
            f"err={short(json.dumps(data['error'], ensure_ascii=False), 1800)}"
        )

    # Odoo a veces responde void: {"jsonrpc":"2.0","id":1}
    if "result" not in data:
        print(
            f"[WARN] odoo_no_result_key model={model} method={method} "
            f"body={short(json.dumps(data, ensure_ascii=False), 600)}",
            flush=True,
        )
        return None

    return data["result"]

def sum_qty_for_product_location(session, ODOO_URL, product_id: int, location_id: int) -> float:
    q_ids = call_kw(
        session, ODOO_URL,
        "stock.quant", "search",
        args=[[["product_id", "=", product_id], ["location_id", "=", location_id]]],
        kwargs={},
    ) or []
    if not q_ids:
        return 0.0

    rows = call_kw(
        session, ODOO_URL,
        "stock.quant", "read",
        args=[q_ids, ["quantity"]],
        kwargs={},
    ) or []
    return float(sum((r.get("quantity") or 0.0) for r in rows))

def _get_quants_detail(session, ODOO_URL, product_id: int, location_id: int):
    q_ids = call_kw(
        session, ODOO_URL,
        "stock.quant", "search",
        args=[[["product_id", "=", product_id], ["location_id", "=", location_id]]],
        kwargs={},
    ) or []
    if not q_ids:
        return [], []

    fields = ["id", "quantity", "reserved_quantity", "inventory_quantity", "inventory_quantity_set", "in_date"]
    rows = call_kw(
        session, ODOO_URL,
        "stock.quant", "read",
        args=[q_ids, fields],
        kwargs={},
    ) or []
    return q_ids, rows

def _classify_quants(rows):
    """
    Queremos:
    - 1 quant "real": quantity != None (y normalmente >0) y reserved=0
    - N quants "basura segura": quantity is None, reserved=0, inventory_quantity=0, inventory_quantity_set=False
    """
    real = []
    junk = []
    unsafe = []

    for r in rows:
        qid = int(r.get("id"))
        qty = r.get("quantity", None)
        res = float(r.get("reserved_quantity") or 0.0)
        invq = float(r.get("inventory_quantity") or 0.0)
        invset = bool(r.get("inventory_quantity_set") or False)

        # Criterio de basura "segura"
        if qty is None and abs(res) < TOL and abs(invq) < TOL and (invset is False):
            junk.append(qid)
            continue

        # Quant real (tiene quantity)
        if qty is not None and abs(res) < TOL:
            real.append(qid)
            continue

        unsafe.append(qid)

    return real, junk, unsafe

def ensure_single_quant_or_manual_review(session, ODOO_URL, sku: str, product_id: int, location_id: int):
    """
    Regla canónica:
    - Si hay múltiples quants pero solo 1 es real y el resto es basura segura => borra basura y ok.
    - Si hay 0 reales, o >1 reales, o hay quants unsafe => MANUAL_REVIEW.
    """
    q_ids, rows = _get_quants_detail(session, ODOO_URL, product_id, location_id)
    if not q_ids:
        return None  # no hay quants; se puede crear 1 quant limpio

    real, junk, unsafe = _classify_quants(rows)

    if unsafe:
        die(f"MANUAL_REVIEW: hay quants UNSAFE para sku={sku} location_id={location_id}. unsafe={unsafe}")

    if len(real) == 1:
        # Borra basura si existe
        if junk:
            _ = call_kw(session, ODOO_URL, "stock.quant", "unlink", args=[junk], kwargs={})
            print(f"[OK] Limpieza quants basura: borrados={len(junk)} ids={junk}", flush=True)
        return real[0]

    if len(real) == 0 and junk:
        # Solo hay basura (quantity NULL). Lo más seguro: borrarla y seguir como si no hubiera quants.
        _ = call_kw(session, ODOO_URL, "stock.quant", "unlink", args=[junk], kwargs={})
        print(f"[OK] Limpieza: solo basura encontrada, borrados={len(junk)} ids={junk}", flush=True)
        return None

    # >1 real => ambigüedad (lotes/paquetes/etc). No tocamos.
    die(
        f"MANUAL_REVIEW: hay {len(real)} stock.quants reales para sku={sku} location_id={location_id}. "
        f"No es seguro aplicar delta con 1 solo quant. (real={real})"
    )

def main():
    ap = argparse.ArgumentParser(
        description="Apply ONE inbound_stock_deltas row to Odoo via /web/dataset/call_kw (safe + verify + auto-clean junk quants)."
    )
    ap.add_argument("--delta-id", required=True)
    ap.add_argument("--location-id", type=int, default=0, help="Force internal stock.location id (0 = auto first internal).")
    args = ap.parse_args()

    if get_flag("meli_inbound_apply_stock_enabled") != "1":
        die("BLOQUEADO: meli_inbound_apply_stock_enabled != 1 (botón rojo sigue apagado)")

    row = read_bridge_delta(args.delta_id)
    if not row:
        die("Delta no existe en bridge.db")
    delta_id, sku, qty_delta, applied = row
    if int(applied) == 1:
        die("Delta ya aplicado (applied_to_odoo=1)")

    ODOO_URL = get_env("ODOO_URL")
    ODOO_DB = get_env("ODOO_DB")
    ODOO_USER = get_env("ODOO_USER")
    ODOO_PASSWORD = get_env("ODOO_PASSWORD")

    session = requests.Session()
    _uid = odoo_auth(session, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)

    # producto por default_code
    prod_ids = call_kw(
        session, ODOO_URL,
        "product.product", "search",
        args=[[["default_code", "=", sku]]],
        kwargs={"limit": 1},
    )
    if not prod_ids:
        die(f"SKU no existe en Odoo: {sku}")
    product_id = int(prod_ids[0])

    # location internal
    if args.location_id and args.location_id > 0:
        location_id = int(args.location_id)
    else:
        loc_ids = call_kw(
            session, ODOO_URL,
            "stock.location", "search",
            args=[[["usage", "=", "internal"]]],
            kwargs={"limit": 1},
        )
        if not loc_ids:
            die("No encontré ninguna stock.location internal")
        location_id = int(loc_ids[0])

    # A) asegurar 1 quant (o ninguno => se crea 1)
    quant_id = ensure_single_quant_or_manual_review(session, ODOO_URL, sku, product_id, location_id)

    # B) calcular target absoluto
    current_qty = sum_qty_for_product_location(session, ODOO_URL, product_id, location_id)
    target_qty = float(current_qty) + float(qty_delta)

    # Si no hay quant, creamos uno limpio
    if quant_id is None:
        qid = call_kw(
            session, ODOO_URL,
            "stock.quant", "create",
            args=[{"product_id": product_id, "location_id": location_id}],
            kwargs={},
        )
        if not qid:
            die("No se pudo crear stock.quant (qid vacío)")
        quant_id = int(qid)

    # C) escribir inventario en el quant elegido (target absoluto)
    _ = call_kw(
        session, ODOO_URL,
        "stock.quant", "write",
        args=[[int(quant_id)], {
            "inventory_quantity": target_qty,
            "inventory_quantity_set": True,
        }],
        kwargs={},
    )

    # D) aplicar
    _ = call_kw(
        session, ODOO_URL,
        "stock.quant", "action_apply_inventory",
        args=[[int(quant_id)]],
        kwargs={},
    )

    # E) postcheck
    after_qty = sum_qty_for_product_location(session, ODOO_URL, product_id, location_id)
    if abs(after_qty - target_qty) > TOL:
        die(
            f"POSTCHECK FAIL: after_qty={after_qty} != target_qty={target_qty} "
            f"(quant_id={quant_id}). NO marco applied."
        )

    mark_applied(delta_id, f"quant:{quant_id}")
    print(
        f"OK_APPLIED delta_id={delta_id} sku={sku} qty_delta={qty_delta} "
        f"current_qty={current_qty} target_qty={target_qty} after_qty={after_qty} "
        f"location_id={location_id} odoo_ref=quant:{quant_id}"
    )

if __name__ == "__main__":
    main()
