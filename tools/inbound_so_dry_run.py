#!/usr/bin/env python3
"""
inbound_so_dry_run.py

DRY RUN: arma un "plan" de Sales Order (SO) para una orden de MercadoLibre,
sin crear nada en Odoo. Solo valida SKUs contra Odoo, detecta si es KIT
(phantom BOM activo) y deja evidencia en bridge.db.

CANÓNICO:
- NO descuenta stock directo en kits phantom.
- Recomendación operativa: crear SO en Odoo para que Odoo explote BOM y descuente componentes.
- Este script NO crea SO. Solo planea + valida + guarda auditoría.

Uso:
  python3 /mnt/data/appdata/bridge/tools/inbound_so_dry_run.py --ml-order-id 2000014950669108

Requisitos:
- Token ML en: /mnt/data/appdata/bridge/data/.meli_tokens.json
- Odoo DB en contenedor: odoo-db-1 (psql)
- DB: EHV, user: odoo
- Bridge DB: /mnt/data/appdata/bridge/data/bridge.db
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

import requests
import subprocess

# -----------------------------
# CONFIG
# -----------------------------
BRIDGE_DB = "/mnt/data/appdata/bridge/data/bridge.db"
TOK_PATH = "/mnt/data/appdata/bridge/data/.meli_tokens.json"

ODOO_DB_CONTAINER = "odoo-db-1"
ODOO_DB_USER = "odoo"
ODOO_DB_NAME = "EHV"

ML_API = "https://api.mercadolibre.com"
TIMEOUT = 30


# -----------------------------
# UTILS
# -----------------------------
def utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_token() -> str:
    if not os.path.exists(TOK_PATH):
        die(f"ERROR: no existe token en {TOK_PATH}")
    try:
        d = json.load(open(TOK_PATH, "r", encoding="utf-8"))
    except Exception as e:
        die(f"ERROR: token corrupto en {TOK_PATH}: {e}")
    tok = d.get("access_token")
    if not tok:
        die("ERROR: token sin access_token")
    return str(tok)


def ml_get_order(ml_order_id: str) -> Dict[str, Any]:
    tok = load_token()
    h = {"Authorization": f"Bearer {tok}"}
    url = f"{ML_API}/orders/{ml_order_id}"
    r = requests.get(url, headers=h, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(
            f"ml_get_order_failed status={r.status_code} body={r.text[:500]}"
        )
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("ml_get_order_bad_json")
    return data


def resolve_site(order: Dict[str, Any]) -> str:
    """
    FIX CANÓNICO:
    - order.site_id puede venir None (como ya viste).
    - fallback a prefijo de item_id: "MLM216..." => "MLM"
    """
    site = str(order.get("site_id") or "").strip()
    if site:
        return site

    try:
        items = order.get("order_items") or order.get("order_items_v2") or []
        if items and isinstance(items, list):
            it0 = items[0] if isinstance(items[0], dict) else {}
            item = it0.get("item") or {}
            item_id = str(item.get("id") or "").strip()
            if len(item_id) >= 3:
                return item_id[:3]
    except Exception:
        pass

    return "UNKNOWN"


def parse_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = order.get("order_items") or order.get("order_items_v2") or []
    if not isinstance(items, list):
        return []

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item = it.get("item") or {}
        if not isinstance(item, dict):
            continue

        sku = item.get("seller_sku")
        try:
            qty = int(it.get("quantity", 0))
        except Exception:
            qty = 0

        if not sku or qty <= 0:
            continue

        out.append(
            {
                "sku": str(sku).strip(),
                "qty": qty,
                "item_id": item.get("id"),
                "variation_id": item.get("variation_id"),
            }
        )
    return out


def psql(sql: str) -> str:
    """
    Ejecuta SQL en PostgreSQL (Odoo) SIN shell quoting (evita el bug de \\n y comillas).
    Devuelve stdout strip.
    """
    cmd = [
        "sudo",
        "docker",
        "exec",
        "-i",
        ODOO_DB_CONTAINER,
        "psql",
        "-U",
        ODOO_DB_USER,
        "-d",
        ODOO_DB_NAME,
        "-At",
        "-c",
        sql,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
        return out
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"psql_failed:\n{e.output}") from e


def odoo_product_info(sku: str) -> Dict[str, Any]:
    """
    Regresa:
      product_id, tmpl_id, default_code, sell_on_meli, name, detailed_type, phantom_active
    """
    sku_esc = sku.replace("'", "''")
    sql = f"""
WITH p AS (
  SELECT
    pp.id AS product_id,
    pt.id AS tmpl_id,
    pp.default_code,
    COALESCE(pp.sell_on_meli,false) AS sell_on_meli,
    pt.name::text AS name,
    pt.detailed_type AS detailed_type
  FROM product_product pp
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pp.default_code = '{sku_esc}'
  LIMIT 1
),
b AS (
  SELECT COUNT(*) AS phantom_active
  FROM mrp_bom b
  WHERE b.product_tmpl_id = (SELECT tmpl_id FROM p)
    AND b.type='phantom'
    AND b.active = true
)
SELECT
  COALESCE((SELECT product_id FROM p), 0)::text,
  COALESCE((SELECT tmpl_id FROM p), 0)::text,
  COALESCE((SELECT default_code FROM p), '')::text,
  COALESCE((SELECT sell_on_meli FROM p), false)::text,
  COALESCE((SELECT name FROM p), '')::text,
  COALESCE((SELECT detailed_type FROM p), '')::text,
  COALESCE((SELECT phantom_active FROM b), 0)::text;
""".strip()

    out = psql(sql)
    # Si no hay filas, psql devuelve "".
    if not out:
        return {
            "exists": False,
            "product_id": 0,
            "tmpl_id": 0,
            "default_code": sku,
            "sell_on_meli": False,
            "name": "",
            "detailed_type": "",
            "phantom_bom_count": 0,
            "is_kit_phantom": False,
        }

    parts = out.split("|")
    if len(parts) < 7:
        raise RuntimeError(f"bad_psql_row_for_sku={sku}: {out}")

    product_id = int(parts[0] or "0")
    tmpl_id = int(parts[1] or "0")
    default_code = parts[2] or sku
    sell_on_meli = (parts[3] or "").strip().lower() in {"t", "true", "1", "yes"}
    name = parts[4] or ""
    detailed_type = parts[5] or ""
    phantom_cnt = int(parts[6] or "0")

    return {
        "exists": product_id > 0,
        "product_id": product_id,
        "tmpl_id": tmpl_id,
        "default_code": default_code,
        "sell_on_meli": sell_on_meli,
        "name": name,
        "detailed_type": detailed_type,
        "phantom_bom_count": phantom_cnt,
        "is_kit_phantom": phantom_cnt > 0,
    }


def ensure_bridge_tables() -> None:
    con = sqlite3.connect(BRIDGE_DB, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
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
);
""")
    con.execute("""
CREATE INDEX IF NOT EXISTS idx_inbound_sales_orders_ml_order_id
  ON inbound_sales_orders(ml_order_id);
""")
    con.commit()
    con.close()


def upsert_inbound_sales_orders(
    dedupe_key: str, ml_order_id: str, site: str, status: str
) -> None:
    con = sqlite3.connect(BRIDGE_DB, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute(
        """
INSERT INTO inbound_sales_orders(dedupe_key, ml_order_id, site, status, updated_at)
VALUES(?, ?, ?, ?, ?)
ON CONFLICT(dedupe_key) DO UPDATE SET
  ml_order_id=excluded.ml_order_id,
  site=excluded.site,
  status=excluded.status,
  updated_at=excluded.updated_at;
""",
        (dedupe_key, ml_order_id, site, status, utc_now_iso()),
    )
    con.commit()
    con.close()


# -----------------------------
# MAIN
# -----------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Dry-run: plan SO en Odoo para una orden ML (NO crea SO)."
    )
    ap.add_argument("--ml-order-id", required=True)
    args = ap.parse_args()

    ensure_bridge_tables()

    ml_order_id = str(args.ml_order_id).strip()
    if not ml_order_id:
        die("ml_order_id vacío")

    # 1) GET orden ML
    try:
        order = ml_get_order(ml_order_id)
    except Exception as e:
        die(f"ERROR: no pude obtener la orden ML {ml_order_id}: {e}")

    site = resolve_site(order)
    dedupe_key = f"so-plan:{site}:{ml_order_id}"

    items = parse_items(order)

    # 2) Consolidar SKUs (por si viene repetido)
    sku_qty: Dict[str, int] = {}
    for it in items:
        sku = it["sku"]
        sku_qty[sku] = sku_qty.get(sku, 0) + int(it["qty"])

    # 3) Validar cada SKU contra Odoo
    so_lines_ok: List[Dict[str, Any]] = []
    blocked_items: List[Dict[str, Any]] = []

    for sku, qty in sorted(sku_qty.items(), key=lambda x: x[0]):
        info = odoo_product_info(sku)

        if not info["exists"]:
            blocked_items.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "reason": "sku_not_in_odoo",
                }
            )
            continue

        # CANÓNICO: si sell_on_meli = false => bloquea (permiso/intención)
        if not info["sell_on_meli"]:
            blocked_items.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "reason": "sku_not_allowed_for_meli",
                    "product_id": info["product_id"],
                    "name": info["name"],
                }
            )
            continue

        so_lines_ok.append(
            {
                "sku": sku,
                "qty": qty,
                "product_id": info["product_id"],
                "name": info["name"],
                "is_kit_phantom": bool(info["is_kit_phantom"]),
                "phantom_bom_count": int(info["phantom_bom_count"]),
            }
        )

    # 4) Recomendación
    # Si hay al menos un kit phantom => SO recomendado sí o sí.
    # (Puedes decidir “SO para todo” para uniformidad: aquí lo dejamos como recomendado si hay kits.)
    so_recommended = any(line.get("is_kit_phantom") for line in so_lines_ok)

    notes = [
        "Este script NO crea SO en Odoo. Solo arma el plan y valida SKUs.",
        "Decisión canónica: NO aplicar delta directo a kits phantom; se recomienda SO (Odoo explota BOM).",
        "Para consistencia, también puedes ir por SO para productos simples (decisión operativa).",
    ]

    out = {
        "ts_utc": utc_now_iso(),
        "dedupe_key": dedupe_key,
        "ml_order_id": ml_order_id,
        "site": site,
        "status": "planned",
        "so_recommended": bool(so_recommended),
        "notes": notes,
        "so_lines_ok": so_lines_ok,
        "blocked_items": blocked_items,
        "counts": {
            "unique_skus_in_order": len(sku_qty),
            "lines_ok": len(so_lines_ok),
            "blocked": len(blocked_items),
        },
    }

    # 5) Persistir evidencia
    upsert_inbound_sales_orders(dedupe_key, ml_order_id, site, "planned")

    # 6) Print final
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print()
    print(f"DB: inbound_sales_orders.dedupe_key={dedupe_key} status=planned")


if __name__ == "__main__":
    main()
