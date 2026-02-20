#!/usr/bin/env python3
import os
import sys
import json
import time
import argparse
import sqlite3
import datetime
from typing import Dict, Any, List, Tuple, Optional

import requests

# =========================
# CONFIG
# =========================
BRIDGE_DB = os.getenv("BRIDGE_DB", "/mnt/data/appdata/bridge/data/bridge.db")
MELI_TOKEN_FILE = os.getenv("MELI_TOKEN_FILE", "/mnt/data/appdata/bridge/data/.meli_tokens.json")

TIMEOUT = 30
SLEEP = 0.10

# Odoo envs
# OJO: Esto usa JSON-RPC /web/dataset/call_kw + session/authenticate
# Requiere:
#   export ODOO_URL="http://127.0.0.1:8082"
#   export ODOO_DB="EHV"
#   export ODOO_USER="..."
#   export ODOO_PASSWORD="..."
#
# Flags en bridge_settings:
#   meli_inbound_create_so_enabled = 1  (sin esto NO hace nada)
#   meli_inbound_confirm_so_enabled = 1 (opcional, default 0)
#   meli_inbound_validate_picking_enabled = 1 (opcional, default 0)  <-- RIESGOSO, default 0

# =========================
# UTILS
# =========================
def utc_now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def get_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        die(f"FALTA ENV {name}. Define {name} en tu entorno antes de correr este script.")
    return v

def short(s: Any, n: int = 1200) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"

def db_conn() -> sqlite3.Connection:
    con = sqlite3.connect(BRIDGE_DB, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    return con

def get_flag(key: str, default: str = "0") -> str:
    try:
        with db_conn() as con:
            row = con.execute("SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,)).fetchone()
        return str(row[0]) if row and row[0] is not None else default
    except Exception:
        return default

def ensure_inbound_sales_orders_table():
    with db_conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS inbound_sales_orders (
          dedupe_key TEXT PRIMARY KEY,
          ml_order_id TEXT NOT NULL,
          site TEXT,
          status TEXT NOT NULL DEFAULT 'draft',
          odoo_so_id INTEGER,
          odoo_name TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at TEXT
        )
        """)
        con.execute("""
        CREATE INDEX IF NOT EXISTS idx_inbound_sales_orders_ml_order_id
          ON inbound_sales_orders(ml_order_id)
        """)
        con.commit()

def upsert_inbound_so_row(dedupe_key: str, ml_order_id: str, site: str, status: str,
                          odoo_so_id: Optional[int] = None, odoo_name: Optional[str] = None):
    with db_conn() as con:
        con.execute("""
        INSERT INTO inbound_sales_orders(dedupe_key, ml_order_id, site, status, odoo_so_id, odoo_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(dedupe_key) DO UPDATE SET
          ml_order_id=excluded.ml_order_id,
          site=excluded.site,
          status=excluded.status,
          odoo_so_id=COALESCE(excluded.odoo_so_id, inbound_sales_orders.odoo_so_id),
          odoo_name=COALESCE(excluded.odoo_name, inbound_sales_orders.odoo_name),
          updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """, (dedupe_key, ml_order_id, site, status, odoo_so_id, odoo_name))
        con.commit()

def get_inbound_so_row(dedupe_key: str):
    with db_conn() as con:
        row = con.execute("""
        SELECT dedupe_key, ml_order_id, site, status,
               COALESCE(odoo_so_id,0) AS odoo_so_id,
               COALESCE(odoo_name,'') AS odoo_name,
               created_at, COALESCE(updated_at,'')
        FROM inbound_sales_orders
        WHERE dedupe_key=?
        LIMIT 1
        """, (dedupe_key,)).fetchone()
    return row

# =========================
# MERCADOLIBRE
# =========================
def load_meli_access_token() -> str:
    if not os.path.exists(MELI_TOKEN_FILE):
        raise RuntimeError(f"token_file_not_found: {MELI_TOKEN_FILE}")
    with open(MELI_TOKEN_FILE, "r") as f:
        d = json.load(f)
    tok = d.get("access_token")
    if not tok:
        raise RuntimeError("no_access_token_in_token_file")
    return str(tok)

def ml_get_order(order_id: str) -> Dict[str, Any]:
    tok = load_meli_access_token()
    url = f"https://api.mercadolibre.com/orders/{order_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"ml_get_order_failed status={r.status_code} body={r.text[:300]}")
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("ml_get_order_bad_json")
    return data

def infer_site_from_order(order: Dict[str, Any]) -> str:
    # 1) site_id explícito
    site = str(order.get("site_id") or "").strip()
    if site:
        return site
    # 2) del item_id (MLMxxxx)
    try:
        items = order.get("order_items") or order.get("order_items_v2") or []
        if items and isinstance(items, list):
            it0 = items[0] or {}
            item = it0.get("item") or {}
            item_id = str(item.get("id") or "").strip()
            if len(item_id) >= 3:
                return item_id[:3]
    except Exception:
        pass
    return "UNKNOWN"

def extract_sku(item: Dict[str, Any]) -> Optional[str]:
    # CANÓNICO: seller_sku directo (lo vimos en tu orden real)
    v = item.get("seller_sku")
    if v:
        return str(v).strip()
    # fallback: si un día viene como attributes SELLER_SKU (por si acaso)
    attrs = item.get("attributes") or []
    if isinstance(attrs, list):
        for a in attrs:
            if isinstance(a, dict) and str(a.get("id") or "").upper() == "SELLER_SKU":
                vv = a.get("value_name") or a.get("value_id")
                if vv:
                    return str(vv).strip()
    return None

def order_to_lines(order: Dict[str, Any]) -> List[Tuple[str, int]]:
    items = order.get("order_items") or order.get("order_items_v2") or []
    if not isinstance(items, list):
        return []
    acc: Dict[str, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = it.get("quantity", 0)
        try:
            qty_i = int(qty)
        except Exception:
            qty_i = 0
        item = it.get("item") or {}
        if not isinstance(item, dict):
            continue
        sku = extract_sku(item)
        if not sku or qty_i <= 0:
            continue
        acc[sku] = acc.get(sku, 0) + qty_i
    # return stable order
    return sorted(acc.items(), key=lambda x: x[0])

# =========================
# ODOO JSON-RPC
# =========================
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

    # algunos métodos void regresan solo {"jsonrpc":"2.0","id":1}
    if "result" not in data:
        # warning pero no falla
        print(f"[WARN] odoo_no_result_key model={model} method={method} body={short(json.dumps(data, ensure_ascii=False), 600)}", flush=True)
        return None

    return data["result"]

def odoo_product_by_sku(session, ODOO_URL: str, sku: str) -> Dict[str, Any]:
    prod_ids = call_kw(
        session, ODOO_URL,
        "product.product", "search",
        args=[[["default_code", "=", sku]]],
        kwargs={"limit": 1},
    ) or []
    if not prod_ids:
        return {"exists": False}

    pid = int(prod_ids[0])
    rows = call_kw(
        session, ODOO_URL,
        "product.product", "read",
        args=[[pid], ["id", "default_code", "product_tmpl_id", "sell_on_meli", "lst_price", "display_name"]],
        kwargs={},
    ) or []
    if not rows:
        return {"exists": False}
    r = rows[0]
    tmpl = r.get("product_tmpl_id")
    tmpl_id = int(tmpl[0]) if isinstance(tmpl, list) and tmpl else (int(tmpl) if tmpl else 0)
    sell_on_meli = bool(r.get("sell_on_meli") or False)
    lst_price = float(r.get("lst_price") or 0.0)
    return {
        "exists": True,
        "product_id": int(r.get("id")),
        "tmpl_id": tmpl_id,
        "sell_on_meli": sell_on_meli,
        "lst_price": lst_price,
        "display_name": r.get("display_name") or "",
    }

def odoo_phantom_bom_count(session, ODOO_URL: str, tmpl_id: int) -> int:
    if not tmpl_id:
        return 0
    bom_ids = call_kw(
        session, ODOO_URL,
        "mrp.bom", "search",
        args=[[["product_tmpl_id", "=", tmpl_id], ["type", "=", "phantom"], ["active", "=", True]]],
        kwargs={},
    ) or []
    return int(len(bom_ids))

def ensure_partner_meli(session, ODOO_URL: str) -> int:
    # buscamos partner "MercadoLibre"
    ids = call_kw(
        session, ODOO_URL,
        "res.partner", "search",
        args=[[["name", "=", "MercadoLibre"]]],
        kwargs={"limit": 1},
    ) or []
    if ids:
        return int(ids[0])
    # lo creamos (contacto genérico)
    pid = call_kw(
        session, ODOO_URL,
        "res.partner", "create",
        args=[{
            "name": "MercadoLibre",
            "company_type": "company",
        }],
        kwargs={},
    )
    if not pid:
        raise RuntimeError("no_pude_crear_partner_MercadoLibre")
    return int(pid)

def create_sale_order(session, ODOO_URL: str, partner_id: int, client_ref: str, note: str) -> int:
    so_id = call_kw(
        session, ODOO_URL,
        "sale.order", "create",
        args=[{
            "partner_id": partner_id,
            "client_order_ref": client_ref,
            "note": note,
        }],
        kwargs={},
    )
    if not so_id:
        raise RuntimeError("odoo_create_sale_order_failed")
    return int(so_id)

def add_so_lines(session, ODOO_URL: str, so_id: int, lines: List[Dict[str, Any]]):
    # crea líneas como sale.order.line separadas (simple y confiable)
    for ln in lines:
        vals = {
            "order_id": so_id,
            "product_id": ln["product_id"],
            "product_uom_qty": ln["qty"],
            "price_unit": ln["price_unit"],
            "name": ln.get("name") or "",
        }
        _ = call_kw(session, ODOO_URL, "sale.order.line", "create", args=[vals], kwargs={})
        time.sleep(SLEEP)

def read_so_name(session, ODOO_URL: str, so_id: int) -> str:
    rows = call_kw(
        session, ODOO_URL,
        "sale.order", "read",
        args=[[so_id], ["name"]],
        kwargs={},
    ) or []
    if not rows:
        return ""
    return str(rows[0].get("name") or "")

def so_action_confirm(session, ODOO_URL: str, so_id: int):
    _ = call_kw(session, ODOO_URL, "sale.order", "action_confirm", args=[[so_id]], kwargs={})

def validate_picking_for_so(session, ODOO_URL: str, so_name: str):
    # MUY RIESGOSO (lotes/series/backorders). Por eso default OFF.
    # Busca pickings cuyo origin = so_name y los intenta validar.
    pick_ids = call_kw(
        session, ODOO_URL,
        "stock.picking", "search",
        args=[[["origin", "=", so_name]]],
        kwargs={},
    ) or []
    for pid in pick_ids:
        pid = int(pid)
        # intenta assign primero
        _ = call_kw(session, ODOO_URL, "stock.picking", "action_assign", args=[[pid]], kwargs={})
        # luego validate
        _ = call_kw(session, ODOO_URL, "stock.picking", "button_validate", args=[[pid]], kwargs={})
        time.sleep(SLEEP)

# =========================
# MAIN
# =========================
def main():
    ap = argparse.ArgumentParser(description="INBOUND: Create SO in Odoo from ML order (safe, flag-gated).")
    ap.add_argument("--ml-order-id", required=True, help="MercadoLibre order id, e.g. 2000014950669108")
    ap.add_argument("--force", action="store_true", help="Ignore existing planned row and re-run (still idempotent by dedupe_key if exists).")
    args = ap.parse_args()

    ensure_inbound_sales_orders_table()

    # Gate duro
    if get_flag("meli_inbound_create_so_enabled") != "1":
        die("BLOQUEADO: meli_inbound_create_so_enabled != 1 (create SO apagado)")

    ml_order_id = str(args.ml_order_id).strip()
    if not ml_order_id:
        die("ml-order-id vacío")

    # 1) ML GET
    order = ml_get_order(ml_order_id)
    site = infer_site_from_order(order)

    # dedupe para SO (sellado)
    dedupe_key = f"so-plan:{site}:{ml_order_id}"

    # 2) DB row check
    row = get_inbound_so_row(dedupe_key)
    if row:
        _, _, _, status, odoo_so_id, odoo_name, *_ = row
        if int(odoo_so_id) > 0:
            print(f"YA_EXISTE: dedupe_key={dedupe_key} status={status} odoo_so_id={odoo_so_id} odoo_name={odoo_name}")
            return
        if (status or "").lower() in {"created", "confirmed", "done"} and not args.force:
            print(f"YA_PROCESADO: dedupe_key={dedupe_key} status={status}. Usa --force si quieres reintentar.")
            return

    # Creamos/actualizamos estado planned (evidencia)
    upsert_inbound_so_row(dedupe_key, ml_order_id, site, "planned")

    # 3) Construye líneas por SELLER_SKU
    sku_lines = order_to_lines(order)
    if not sku_lines:
        upsert_inbound_so_row(dedupe_key, ml_order_id, site, "manual_review", None, None)
        die("manual_review: no_items_or_no_skus_found")

    # 4) Odoo auth
    ODOO_URL = get_env("ODOO_URL")
    ODOO_DB = get_env("ODOO_DB")
    ODOO_USER = get_env("ODOO_USER")
    ODOO_PASSWORD = get_env("ODOO_PASSWORD")

    session = requests.Session()
    _uid = odoo_auth(session, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)

    # 5) Valida SKUs contra Odoo + sell_on_meli gate (SELLADO)
    lines_ok: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []

    for sku, qty in sku_lines:
        info = odoo_product_by_sku(session, ODOO_URL, sku)
        if not info.get("exists"):
            blocked.append({"sku": sku, "qty": qty, "reason": "sku_not_in_odoo"})
            continue
        if not info.get("sell_on_meli", False):
            blocked.append({"sku": sku, "qty": qty, "reason": "sku_not_allowed_for_meli"})
            continue

        tmpl_id = int(info.get("tmpl_id") or 0)
        phantom_count = odoo_phantom_bom_count(session, ODOO_URL, tmpl_id)
        is_kit = phantom_count > 0

        lines_ok.append({
            "sku": sku,
            "qty": int(qty),
            "product_id": int(info["product_id"]),
            "name": str(info.get("display_name") or sku),
            "price_unit": float(info.get("lst_price") or 0.0),
            "is_kit_phantom": bool(is_kit),
            "phantom_bom_count": int(phantom_count),
        })

    if not lines_ok:
        upsert_inbound_so_row(dedupe_key, ml_order_id, site, "manual_review", None, None)
        die(f"manual_review: all_items_blocked blocked={json.dumps(blocked, ensure_ascii=False)}")

    # 6) Decide: para consistencia, aquí SIEMPRE creamos SO (kits y simples)
    # (sellado en tu decisión reciente: NO descontar stock por delta; dejar que SO lo haga normal)
    partner_id = ensure_partner_meli(session, ODOO_URL)

    client_ref = f"ML:{site}:{ml_order_id}"
    note = (
        "GONCLOUD INBOUND (MercadoLibre) — SO creada por bridge.\n"
        f"ML order_id: {ml_order_id}\n"
        f"site: {site}\n"
        f"ts_utc: {utc_now_z()}\n"
        "Reglas selladas: SELLER_SKU de ML + sell_on_meli gate.\n"
        "NO ajustar stock directo de kits phantom; Odoo explota BOM al confirmar SO.\n"
    )

    so_id = create_sale_order(session, ODOO_URL, partner_id, client_ref, note)
    add_so_lines(session, ODOO_URL, so_id, lines_ok)
    so_name = read_so_name(session, ODOO_URL, so_id)

    # guarda en DB
    upsert_inbound_so_row(dedupe_key, ml_order_id, site, "created", so_id, so_name)

    print("=== SO CREATED ===")
    print(f"dedupe_key={dedupe_key}")
    print(f"ml_order_id={ml_order_id} site={site}")
    print(f"odoo_so_id={so_id} odoo_name={so_name}")
    print(f"lines_ok={len(lines_ok)} blocked={len(blocked)}")
    if blocked:
        print("blocked_items=" + json.dumps(blocked, ensure_ascii=False))

    # 7) Confirm / Validate picking (opcionales)
    if get_flag("meli_inbound_confirm_so_enabled") == "1":
        so_action_confirm(session, ODOO_URL, so_id)
        upsert_inbound_so_row(dedupe_key, ml_order_id, site, "confirmed", so_id, so_name)
        print("OK: SO confirmed (flag meli_inbound_confirm_so_enabled=1)")

        if get_flag("meli_inbound_validate_picking_enabled") == "1":
            if not so_name:
                so_name = read_so_name(session, ODOO_URL, so_id)
            validate_picking_for_so(session, ODOO_URL, so_name)
            upsert_inbound_so_row(dedupe_key, ml_order_id, site, "done", so_id, so_name)
            print("OK: picking validated (flag meli_inbound_validate_picking_enabled=1)")

if __name__ == "__main__":
    main()
