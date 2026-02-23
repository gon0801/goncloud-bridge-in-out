#!/usr/bin/env python3
"""
INBOUND WORKER — GONCLOUD BRIDGE
Procesa órdenes MercadoLibre → Odoo 17

Versión: 3.2 (FIX: pack_id como display_id)
Fecha: 2026-02-12
Estado: LIMPIO · FULL + FBM COMPLETO · PACK_ID FIX
"""

import json
import os
import re
import sqlite3
import time
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis
import requests

# ============================================================
# ENV / CONSTANTES
# ============================================================

DB_PATH = (
    os.getenv("BRIDGE_DB_PATH")
    or os.getenv("BRIDGE_DB")
    or "/data/bridge.db"
)

REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
TOKEN_FILE = os.getenv("MELI_TOKEN_FILE", "/data/.meli_tokens.json")

QUEUE = "ml_orders_jobs"
ORDER_ID_RE = re.compile(r"^/orders/([A-Za-z0-9\-]+)$")

ML_API = "https://api.mercadolibre.com"

# ============================================================
# UTILS
# ============================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def get_setting(key: str, default: str = "0") -> str:
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM bridge_settings WHERE key=? LIMIT 1",
                (key,),
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else default
    except Exception:
        return default


def get_display_id(order: Dict[str, Any], order_id: str) -> str:
    """
    Devuelve el ID que el usuario ve en la web de MercadoLibre.
    - Si existe pack_id -> es lo que muestra la web
    - Si no -> usa order_id
    
    IMPORTANTE: MeLi agrupa órdenes en "packs" y la web muestra el pack_id,
    pero la API envía webhooks con order_id individual.
    """
    pack_id = order.get("pack_id")
    if pack_id:
        return str(pack_id)
    return str(order_id)


# ============================================================
# AUDITORÍA / IDEMPOTENCIA
# ============================================================

def mark_processed(dedupe_key: str, result: str, detail: dict):
    """
    FUNCIÓN CRÍTICA:
    Todo job que sale de Redis DEBE pasar por aquí.
    """
    try:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_inbound_events
                (dedupe_key, processed_at, result, detail_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    dedupe_key,
                    utc_now_iso(),
                    result,
                    json.dumps(detail, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE inbound_events SET status='processed' WHERE dedupe_key=?",
                (dedupe_key,),
            )
            conn.commit()
        print(f"[inbound_worker] AUDIT OK dedupe={dedupe_key} result={result}", flush=True)
    except Exception as e:
        print(f"[inbound_worker] CRITICAL DB ERROR in mark_processed: {e}", flush=True)


def already_processed(dedupe_key: str) -> bool:
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_inbound_events WHERE dedupe_key=? LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        return row is not None
    except Exception as e:
        print(f"[inbound_worker] DB READ ERROR: {e}", flush=True)
        return False


# ============================================================
# MERCADOLIBRE
# ============================================================

def load_access_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(f"token_file_not_found: {TOKEN_FILE}")
    with open(TOKEN_FILE, "r") as f:
        t = json.load(f)
    tok = t.get("access_token")
    if not tok:
        raise RuntimeError("no_access_token_in_token_file")
    return str(tok)


def ml_get_json(path: str) -> Dict[str, Any]:
    token = load_access_token()
    url = f"{ML_API}{path}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"ml_get_failed path={path} status={resp.status_code} body={resp.text[:200]}"
        )
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("ml_get_bad_json")
    return data


def ml_get_order(order_id: str) -> Dict[str, Any]:
    return ml_get_json(f"/orders/{order_id}")


def ml_get_shipment(shipment_id: str) -> Dict[str, Any]:
    return ml_get_json(f"/shipments/{shipment_id}")


def detect_full(order: Dict[str, Any], shipment: Optional[Dict[str, Any]]) -> Tuple[Optional[bool], str]:
    """
    Regla SELLADA:
    FULL si logistic_type == "fulfillment"
    Prioridad:
      1) order.logistic_type
      2) shipment.logistic_type
      3) indeterminado => manual_review
    """
    lt = order.get("logistic_type")
    if lt is not None:
        return (str(lt) == "fulfillment", "order.logistic_type")

    if shipment and shipment.get("logistic_type") is not None:
        lt2 = shipment.get("logistic_type")
        return (str(lt2) == "fulfillment", "shipment.logistic_type")

    return (None, "unknown")


# ============================================================
# LÓGICA DE NEGOCIO
# ============================================================

def upsert_order_state(order_id: str, state: str, last_updated_at: str, pack_id: Optional[str]):
    try:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO inbound_orders_state
                  (order_id, last_state, last_updated_at, last_seen_at, pack_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                  last_state=excluded.last_state,
                  last_updated_at=excluded.last_updated_at,
                  last_seen_at=excluded.last_seen_at,
                  pack_id=excluded.pack_id
                """,
                (order_id, state, last_updated_at, utc_now_iso(), pack_id),
            )
            conn.commit()
    except Exception as e:
        print(f"[inbound_worker] STATE UPSERT ERROR: {e}", flush=True)


def upsert_inbound_sales_order_plan(site: str, order_id: str, status: str):
    """
    Crea/actualiza el plan de SO (idempotente por dedupe_key so-plan:<site>:<order_id>)
    """
    dedupe = f"so-plan:{site}:{order_id}"
    try:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO inbound_sales_orders
                  (dedupe_key, ml_order_id, site, status, odoo_so_id, odoo_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (dedupe, order_id, site, status, utc_now_iso(), utc_now_iso()),
            )
            conn.commit()
    except Exception as e:
        print(f"[inbound_worker] SO PLAN UPSERT ERROR: {e}", flush=True)


def insert_delta(delta_id: str, order_id: str, sku: str, qty_delta: int, reason: str) -> bool:
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO inbound_stock_deltas
            (delta_id, order_id, sku, qty_delta, reason, created_at, applied_to_odoo)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (delta_id, order_id, sku, int(qty_delta), reason, utc_now_iso()),
        )
        conn.commit()
        return cur.rowcount == 1


def is_allowed_sku(sku: str) -> bool:
    sku = (sku or "").strip()
    if not sku:
        return False
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM inbound_allowed_skus WHERE sku=? AND enabled=1 LIMIT 1",
                (sku,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def compute_reason_and_multiplier(state: str) -> Tuple[Optional[str], Optional[int]]:
    state = (state or "").strip().lower()
    if state in {"paid", "confirmed"}:
        return ("sale", -1)
    if state in {"cancelled", "canceled"}:
        return ("cancel", +1)
    return (None, None)

def resolve_site(order: Dict[str, Any]) -> str:
    site = str(order.get("site_id") or "").strip()
    if site:
        return site

    # fallback: infer from item_id prefix like "MLM..."
    try:
        oi = (order.get("order_items") or [])[0]
        item = oi.get("item") or {}
        item_id = str(item.get("id") or "")
        if len(item_id) >= 3:
            return item_id[:3]
    except Exception:
        pass

    return "UNKNOWN"


def build_so_note(order: Dict[str, Any], order_id: str, display_id: str, prefix: str = "ML") -> str:
    """
    Construye la nota para el SO.
    Formato estándar: {prefix} | ORDER={display_id} | {buyer_name}
    """
    buyer = order.get("buyer") or {}
    buyer_first = (buyer.get("nickname") or buyer.get("first_name") or "").strip()
    buyer_last = (buyer.get("last_name") or "").strip()
    buyer_name = (f"{buyer_first} {buyer_last}".strip() if buyer_last else buyer_first) or "N/A"

    return f"{prefix} | ORDER={display_id} | {buyer_name}"


# ============================================================
# SKU PARSING
# ============================================================

def extract_sku(item_dict: Dict[str, Any]) -> Optional[str]:
    sku = item_dict.get("seller_sku") or item_dict.get("SELLER_SKU")
    if sku:
        return str(sku).strip()

    for attr in item_dict.get("attributes") or []:
        if isinstance(attr, dict) and str(attr.get("id")).upper() == "SELLER_SKU":
            val = attr.get("value_name") or attr.get("value_id")
            if val:
                return str(val).strip()

    return None


def lookup_sku_mapping(item_id: str, variation_id: str, site: str) -> Optional[str]:
    item_id = (item_id or "").strip()
    variation_id = (variation_id or "").strip()
    site = (site or "MLM").strip() or "MLM"
    if not item_id or not variation_id:
        return None

    try:
        with db_conn() as conn:
            # site es extra guardrail (tu PK no lo requiere, pero ayuda si hay duplicados cross-site)
            row = conn.execute(
                """
                SELECT sku
                FROM sku_mapping
                WHERE channel='meli'
                  AND remote_item_id=?
                  AND remote_variation_id=?
                  AND (site=? OR site IS NULL OR site='')
                LIMIT 1
                """,
                (item_id, variation_id, site),
            ).fetchone()
        return str(row[0]).strip() if row and row[0] else None
    except Exception:
        return None


def parse_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = order.get("order_items") or order.get("order_items_v2") or []
    if not isinstance(items, list):
        return []

    site = str(order.get("site_id") or "").strip() or "MLM"

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        item_data = it.get("item") or {}
        if not isinstance(item_data, dict):
            continue

        # 1) Primero: mapping canónico v2 por (item_id, variation_id)
        item_id = str(item_data.get("id") or "").strip()
        var_id = (
            item_data.get("variation_id")
            or it.get("variation_id")
            or it.get("var_id")
            or it.get("variation")
        )
        var_id = str(var_id or "").strip()

        sku = None
        if item_id and var_id:
            sku = lookup_sku_mapping(item_id, var_id, site)

        # 2) Fallback: si no hay mapping, usa SKU directo del payload
        if not sku:
            sku = extract_sku(item_data)
        try:
            qty = int(it.get("quantity", 0))
        except Exception:
            qty = 0

        if sku and qty > 0:
            out.append({"sku": sku, "quantity": qty})

    return out

# ============================================================
# FULL REFUND / RETURN (NO STOCK) — tool canónica
# ============================================================

def maybe_run_full_refund(order: Dict[str, Any], order_id: str, display_id: str, site: str, state: str, dedupe_key: str):
    """
    FULL refund/cancel:
      - Llama tool canónica inbound_full_so_refund_and_cancel.py
      - Si la tool regresa RC=2 => SO todavía no existe => NO es error (deferred)
    Kill-switch:
      - bridge_settings: meli_inbound_full_refunds_enabled = "1"
    """
    enabled = (
        os.getenv("INBOUND_FULL_REFUNDS_ENABLED", "0") == "1"
        or get_setting("meli_inbound_full_refunds_enabled", "0") == "1"
    )
    if not enabled:
        return

    st = (state or "").strip().lower()
    if st not in {"cancelled", "canceled", "refunded"}:
        return

    client_order_ref = f"MLFULL:{site}:{display_id}"
    _refund_candidates = [
        "/data/inbound_full_so_refund_and_cancel.py",
        "/mnt/data/appdata/bridge/tools/inbound_full_so_refund_and_cancel.py",
    ]
    tool = next((p for p in _refund_candidates if os.path.exists(p)), _refund_candidates[-1])

    try:
        p = subprocess.run(
            ["python3", tool],
            env=dict(
                os.environ,
                CLIENT_ORDER_REF=client_order_ref,
                ODOO_URL=get_setting("odoo_url", ""),
                ODOO_DB=get_setting("odoo_db", ""),
                ODOO_USER=get_setting("odoo_user", ""),
                ODOO_PASSWORD=get_setting("odoo_password", ""),
            ),
            capture_output=True,
            text=True,
            timeout=180,
        )

        if p.returncode == 0:
            print(f"[inbound_worker] FULL_REFUND_OK dedupe={dedupe_key} ref={client_order_ref}", flush=True)
            return

        if p.returncode == 2:
            print(f"[inbound_worker] FULL_REFUND_DEFERRED_NO_SO dedupe={dedupe_key} ref={client_order_ref}", flush=True)
            return

        err_tail = (p.stderr or "")[-500:]
        print(
            f"[inbound_worker] FULL_REFUND_ERROR dedupe={dedupe_key} ref={client_order_ref} "
            f"rc={p.returncode} stderr_tail={err_tail}",
            flush=True,
        )

    except subprocess.TimeoutExpired:
        print(f"[inbound_worker] FULL_REFUND_TIMEOUT dedupe={dedupe_key} ref={client_order_ref}", flush=True)
    except Exception as e:
        print(f"[inbound_worker] FULL_REFUND_EXCEPTION dedupe={dedupe_key} ref={client_order_ref} err={e}", flush=True)


# ========================================================
# FULL PAID (NO STOCK) — contable puro
# ========================================================

def maybe_run_full_paid_no_stock(
    order: Dict[str, Any],
    order_id: str,
    display_id: str,
    site: str,
    state: str,
    dedupe_key: str,
):
    """
    FULL paid/approved/finalized:
      - Crea/Confirma SO contable + crea factura + paga
      - NO crea pickings / NO mueve stock
    Kill-switch:
      - bridge_settings: meli_inbound_full_paid_enabled = "1"
    """
    enabled = (
        os.getenv("INBOUND_FULL_PAID_ENABLED", "0") == "1"
        or get_setting("meli_inbound_full_paid_enabled", "0") == "1"
    )
    if not enabled:
        return

    st = (state or "").strip().lower()
    if st not in {"paid", "approved", "finalized"}:
        return

    client_order_ref = f"MLFULL:{site}:{display_id}"

    tool_candidates = [
        "/data/inbound_full_paid_one_shot_no_stock.py",
        "/mnt/data/appdata/bridge/tools/inbound_full_paid_one_shot_no_stock.py",
    ]
    tool = next((p for p in tool_candidates if os.path.exists(p)), None)

    if not tool:
        print(
            f"[inbound_worker] FULL_PAID_ERROR dedupe={dedupe_key} ref={client_order_ref} tool_not_found",
            flush=True,
        )
        return

    try:
        env = os.environ.copy()
        env["CLIENT_ORDER_REF"] = client_order_ref
        env["ODOO_URL"] = get_setting("odoo_url", "")
        env["ODOO_DB"] = get_setting("odoo_db", "")
        env["ODOO_USER"] = get_setting("odoo_user", "")
        env["ODOO_PASSWORD"] = get_setting("odoo_password", "")
        env["ORDER_JSON"] = json.dumps(order, ensure_ascii=False)
        env["SITE_ID"] = site

        env["SO_NOTE"] = build_so_note(order, order_id, display_id, "ML: FULL paid")

        p = subprocess.run(
            ["python3", tool],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )

        if p.returncode == 0:
            print(f"[inbound_worker] FULL_PAID_OK dedupe={dedupe_key} ref={client_order_ref}", flush=True)
        else:
            stderr_tail = (p.stderr or "")[-500:]
            print(
                f"[inbound_worker] FULL_PAID_ERROR dedupe={dedupe_key} ref={client_order_ref} rc={p.returncode} stderr_tail={stderr_tail}",
                flush=True,
            )

    except subprocess.TimeoutExpired:
        print(f"[inbound_worker] FULL_PAID_TIMEOUT dedupe={dedupe_key} ref={client_order_ref}", flush=True)
    except Exception as e:
        print(f"[inbound_worker] FULL_PAID_EXCEPTION dedupe={dedupe_key} ref={client_order_ref} err={e}", flush=True)


# ========================================================
# FBM PAID — SO + Picking (SIN validar picking)
# ========================================================

def maybe_run_fbm_paid(
    order: Dict[str, Any],
    order_id: str,
    display_id: str,
    site: str,
    state: str,
    dedupe_key: str,
) -> bool:
    """
    FBM paid/approved/finalized:
      - Crea SO + líneas + confirma (crea picking automáticamente)
      - Crea Invoice + paga
      - NO valida picking (queda para humano)
    Kill-switch:
      - bridge_settings: meli_inbound_fbm_paid_enabled = "1"
    Returns:
      - True si se procesó (éxito o error)
      - False si no está habilitado o no aplica
    """
    enabled = (
        os.getenv("INBOUND_FBM_PAID_ENABLED", "0") == "1"
        or get_setting("meli_inbound_fbm_paid_enabled", "0") == "1"
    )
    if not enabled:
        return False

    st = (state or "").strip().lower()
    if st not in {"paid", "approved", "finalized", "confirmed"}:
        return False

    client_order_ref = f"MLFBM:{site}:{display_id}"

    tool_candidates = [
        "/data/inbound_fbm_so_apply_paid_one_shot.py",
        "/mnt/data/appdata/bridge/tools/inbound_fbm_so_apply_paid_one_shot.py",
    ]
    tool = next((p for p in tool_candidates if os.path.exists(p)), None)

    if not tool:
        print(
            f"[inbound_worker] FBM_PAID_ERROR dedupe={dedupe_key} ref={client_order_ref} tool_not_found",
            flush=True,
        )
        return False

    try:
        env = os.environ.copy()
        env["ODOO_URL"] = get_setting("odoo_url", "http://odoo-odoo-1:8069")
        env["ODOO_DB"] = get_setting("odoo_db", "")
        env["ODOO_USER"] = get_setting("odoo_user", "")
        env["ODOO_PASS"] = get_setting("odoo_password", "")
        env["ORDER_JSON"] = json.dumps(order, ensure_ascii=False)

        env["SITE_ID"] = site
        env["CLIENT_ORDER_REF"] = client_order_ref

        env["SO_NOTE"] = build_so_note(order, order_id, display_id, "ML: FBM paid")

        p = subprocess.run(
            ["python3", tool],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )

        stdout_tail = (p.stdout or "")[-800:]
        stderr_tail = (p.stderr or "")[-800:]

        if p.returncode == 0:
            print(f"[inbound_worker] FBM_PAID_OK dedupe={dedupe_key} ref={client_order_ref}", flush=True)
            return True
        else:
            print(
                f"[inbound_worker] FBM_PAID_ERROR dedupe={dedupe_key} ref={client_order_ref} "
                f"rc={p.returncode} stderr_tail={stderr_tail}",
                flush=True,
            )
            return "manual_review"

    except subprocess.TimeoutExpired:
        print(f"[inbound_worker] FBM_PAID_TIMEOUT dedupe={dedupe_key} ref={client_order_ref}", flush=True)
        return "error"
    except Exception as e:
        print(f"[inbound_worker] FBM_PAID_EXCEPTION dedupe={dedupe_key} ref={client_order_ref} err={e}", flush=True)
        return "error"


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        print(f"[inbound_worker] START queue={QUEUE} db={DB_PATH}", flush=True)
    except Exception as e:
        print(f"[inbound_worker] FATAL redis error: {e}", flush=True)
        time.sleep(5)
        return

    while True:
        if get_setting("meli_inbound_enabled", "0") != "1":
            time.sleep(2)
            continue

        try:
            item = r.blpop(QUEUE, timeout=5)
            if not item:
                continue
            _, payload = item
        except Exception as e:
            print(f"[inbound_worker] REDIS ERROR: {e}", flush=True)
            time.sleep(2)
            continue

        job_hash = str(hash(payload))[-8:]
        print(f"[inbound_worker] JOB recv hash={job_hash}", flush=True)

        dedupe_key = f"unknown:{job_hash}"

        try:
            try:
                job = json.loads(payload)
                if not isinstance(job, dict):
                    raise ValueError("payload_not_dict")
            except Exception:
                mark_processed(
                    f"badjson:{job_hash}",
                    "skipped",
                    {"error": "bad_json", "payload": payload[:200]},
                )
                continue

            dedupe_key = str(job.get("dedupe_key") or "").strip()
            if not dedupe_key:
                dedupe_key = f"missingdedupe:{job_hash}"

            if already_processed(dedupe_key):
                print(f"[inbound_worker] SKIP dedupe={dedupe_key}", flush=True)
                continue

            resource = str(job.get("resource") or "")
            order_json = job.get("order_json")

            if isinstance(order_json, dict):
                order = order_json
                order_id = str(order.get("id") or f"TEST-{job_hash}")
            else:
                m = ORDER_ID_RE.match(resource)
                if not m:
                    mark_processed(dedupe_key, "manual_review", {"reason": "bad_resource"})
                    continue
                order_id = m.group(1)
                order = ml_get_order(order_id)

            # CRITICAL: display_id es lo que el usuario ve en MeLi web
            display_id = get_display_id(order, order_id)

            site = resolve_site(order)
            state = str(order.get("status") or "")

            print(
                f"[inbound_worker] STATE_PROBE order_id={order_id} display_id={display_id} site={site} status={state} logistic_type={order.get('logistic_type')}",
                flush=True,
            )

            last_updated = str(
                order.get("last_updated")
                or order.get("date_last_updated")
                or utc_now_iso()
            )

            upsert_order_state(
                order_id,
                state,
                last_updated,
                str(order.get("pack_id")) if order.get("pack_id") else None,
            )

            # ========= FULL DETECTION =========
            shipment = None

            shipment_json = job.get("shipment_json")
            if isinstance(shipment_json, dict):
                shipment = shipment_json
            else:
                ship_id = None
                try:
                    ship_id = (order.get("shipping") or {}).get("id")
                except Exception:
                    ship_id = None

                if ship_id:
                    try:
                        shipment = ml_get_shipment(str(ship_id))
                    except Exception as e:
                        print(f"[inbound_worker] WARN shipment fetch failed id={ship_id} err={e}", flush=True)

            is_full, src = detect_full(order, shipment)

            if is_full is None:
                mark_processed(
                    dedupe_key,
                    "manual_review",
                    {"order_id": order_id, "display_id": display_id, "site": site, "reason": "full_unknown", "src": src},
                )
                continue

            # ========= FULL PATH =========
            if is_full:
                upsert_inbound_sales_order_plan(site, order_id, "planned")
                maybe_run_full_refund(order, order_id, display_id, site, state, dedupe_key)
                maybe_run_full_paid_no_stock(order, order_id, display_id, site, state, dedupe_key)

                mark_processed(
                    dedupe_key,
                    "planned_full",
                    {
                        "order_id": order_id,
                        "display_id": display_id,
                        "site": site,
                        "state": state,
                        "full": True,
                        "full_detect_src": src,
                        "full_paid_enabled": get_setting("meli_inbound_full_paid_enabled", "0") == "1",
                        "full_refunds_enabled": get_setting("meli_inbound_full_refunds_enabled", "0") == "1",
                    },
                )
                continue

            # ========= FBM PATH =========
            st_lower = state.strip().lower()

            # FBM CANCEL/REFUND/RETURN
            if st_lower in ("cancelled", "canceled", "refunded", "returned"):
                if get_setting("meli_inbound_fbm_refunds_enabled", "0") != "1":
                    mark_processed(
                        dedupe_key,
                        "skipped_disabled",
                        {
                            "order_id": order_id,
                            "display_id": display_id,
                            "site": site,
                            "state": state,
                            "full": False,
                            "fbm_refund_cancel_enabled": False,
                        },
                    )
                    continue

                client_order_ref = f"MLFBM:{site}:{display_id}"
                env = os.environ.copy()
                env["ODOO_URL"] = get_setting("odoo_url", "http://odoo-odoo-1:8069")
                env["ODOO_DB"] = get_setting("odoo_db", "")
                env["ODOO_USER"] = get_setting("odoo_user", "")
                env["ODOO_PASS"] = get_setting("odoo_password", "")
                env["CLIENT_ORDER_REF"] = client_order_ref

                tool = "/data/inbound_fbm_so_refund_and_cancel.py"

                try:
                    p = subprocess.run(
                        ["python3", tool],
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=180,
                    )
                    rc = int(p.returncode)
                    stderr_tail = (p.stderr or "")[-1200:]

                    if rc != 0:
                        print(
                            f"[inbound_worker] FBM_REFUND_ERROR dedupe={dedupe_key} ref={client_order_ref} rc={rc} stderr_tail={stderr_tail}",
                            flush=True,
                        )
                        mark_processed(
                            dedupe_key,
                            "manual_review",
                            {
                                "order_id": order_id,
                                "display_id": display_id,
                                "site": site,
                                "state": state,
                                "client_order_ref": client_order_ref,
                                "error": "fbm_refund_failed",
                                "rc": rc,
                            },
                        )
                        continue

                    print(f"[inbound_worker] FBM_REFUND_OK dedupe={dedupe_key} ref={client_order_ref}", flush=True)
                    mark_processed(
                        dedupe_key,
                        "fbm_refund_ok",
                        {
                            "order_id": order_id,
                            "display_id": display_id,
                            "site": site,
                            "state": state,
                            "client_order_ref": client_order_ref,
                        },
                    )
                    continue

                except Exception as e:
                    print(f"[inbound_worker] FBM_REFUND_EXCEPTION dedupe={dedupe_key} err={e}", flush=True)
                    mark_processed(
                        dedupe_key,
                        "error",
                        {"order_id": order_id, "display_id": display_id, "error": "fbm_refund_exception", "msg": str(e)},
                    )
                    continue

            # FBM PAID - Crear SO + Picking
            if st_lower in ("paid", "approved", "finalized", "confirmed"):
                fbm_paid_processed = maybe_run_fbm_paid(order, order_id, display_id, site, state, dedupe_key)
                if fbm_paid_processed == True:
                    mark_processed(
                        dedupe_key,
                        "fbm_paid_ok",
                        {
                            "order_id": order_id,
                            "display_id": display_id,
                            "site": site,
                            "state": state,
                            "full": False,
                            "fbm_paid_enabled": True,
                        },
                    )
                    continue
                elif fbm_paid_processed in ("manual_review", "error"):
                    mark_processed(dedupe_key, fbm_paid_processed, {"order_id": order_id, "display_id": display_id, "site": site, "state": state, "reason": "fbm_paid_failed"})
                    continue

            # FBM FALLBACK: Deltas (legacy)
            reason, mult = compute_reason_and_multiplier(state)
            if reason is None:
                mark_processed(
                    dedupe_key,
                    "manual_review",
                    {"order_id": order_id, "display_id": display_id, "state": state, "reason": "unknown_state"},
                )
                continue

            items = parse_items(order)
            if not items:
                mark_processed(
                    dedupe_key,
                    "manual_review",
                    {"order_id": order_id, "display_id": display_id, "reason": "no_items"},
                )
                continue

            inserted = 0
            blocked = 0

            for it in items:
                sku = it["sku"]
                qty = it["quantity"]

                if not is_allowed_sku(sku):
                    blocked += 1
                    continue

                delta_id = f"ml-delta:{site}:{order_id}:{sku}:{reason}:{last_updated}"
                if insert_delta(delta_id, order_id, sku, mult * qty, reason):
                    inserted += 1

            detail = {
                "order_id": order_id,
                "display_id": display_id,
                "site": site,
                "state": state,
                "full": False,
                "inserted_deltas": inserted,
                "blocked_items": blocked,
            }

            if inserted == 0:
                mark_processed(dedupe_key, "manual_review", detail)
            else:
                mark_processed(dedupe_key, "applied", detail)

        except Exception as e:
            print(f"[inbound_worker] UNHANDLED EXCEPTION: {e}", flush=True)
            try:
                mark_processed(
                    dedupe_key,
                    "error",
                    {"error": "unhandled_exception", "msg": str(e)},
                )
            except Exception:
                print("[inbound_worker] CRITICAL: could not write error", flush=True)


if __name__ == "__main__":
    main()
