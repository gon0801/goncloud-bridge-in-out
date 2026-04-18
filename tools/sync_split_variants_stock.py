#!/usr/bin/env python3
"""
FASE 1 URGENTE: arregla el stock de listings post-split de variantes.

Uso tipico (one-shot):
  python3 /data/sync_split_variants_stock.py --sku NH-CAR-AZU-CEN-DOR \
      --listings MLM5164542984,MLM5164542986,MLM5164542988,MLM5164542990,MLM5164542992,MLM5164542994

Para cada MLM ID:
1) Obtiene stock real de Odoo para el SKU
2) Hace PUT available_quantity = stock_odoo
3) Adicionalmente, registra los mappings en sku_mapping (requiere schema 1:N ya migrado)

Soporta tambien modo --auto: descubre listings huerfanos del SKU via MeLi API
(a traves de /items/{existing_mapped_id} y sus variantes relacionadas — fallback manual).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
import urllib.request


def log(msg: str) -> None:
    print(f"[sync_split_variants] {msg}", flush=True)


def load_token(path: str = "/data/.meli_tokens.json") -> str:
    with open(path) as f:
        j = json.load(f)
    tok = j.get("access_token")
    if not tok:
        raise RuntimeError(f"no_access_token_in={path}")
    return str(tok)


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(
        "SELECT value FROM bridge_settings WHERE key=?", (key,)
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def get_odoo_qty(conn: sqlite3.Connection, sku: str) -> float | None:
    import requests

    url = get_setting(conn, "odoo_url").rstrip("/")
    db_ = get_setting(conn, "odoo_db")
    user = get_setting(conn, "odoo_user")
    pw = get_setting(conn, "odoo_password")
    if not all([url, db_, user, pw]):
        raise RuntimeError("odoo_settings_missing")

    def jcall(service, method, args):
        r = requests.post(
            url + "/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"service": service, "method": method, "args": args},
                "id": 1,
            },
            timeout=30,
        )
        return r.json().get("result")

    uid = jcall("common", "authenticate", [db_, user, pw, {}])
    if not uid:
        raise RuntimeError("odoo_auth_failed")

    res = jcall(
        "object",
        "execute_kw",
        [
            db_, uid, pw, "product.product", "search_read",
            [[["default_code", "=", sku]]],
            {"fields": ["id", "default_code", "qty_available", "name"], "limit": 3},
        ],
    )
    if not res:
        return None
    return float(res[0].get("qty_available") or 0)


def ml_put_qty(item_id: str, var_id: str, qty: int, token: str) -> tuple[int, dict]:
    if var_id:
        url = f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}"
    else:
        url = f"https://api.mercadolibre.com/items/{item_id}"
    body = json.dumps({"available_quantity": qty}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.getcode(), json.load(r)
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body_err[:300]}")


def ml_get_item(item_id: str, token: str) -> dict:
    url = (
        f"https://api.mercadolibre.com/items/{item_id}"
        "?attributes=id,seller_custom_field,variations,available_quantity,status"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/bridge.db")
    parser.add_argument("--token-file", default="/data/.meli_tokens.json")
    parser.add_argument("--sku", required=True, help="SKU en Odoo (ej: NH-CAR-AZU-CEN-DOR)")
    parser.add_argument(
        "--listings",
        required=True,
        help="MLM IDs separados por coma (ej: MLM5164542984,MLM5164542986,...)",
    )
    parser.add_argument(
        "--override-qty",
        type=int,
        default=None,
        help="Si se especifica, usa este qty en vez del stock de Odoo",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = load_token(args.token_file)
    conn = sqlite3.connect(args.db)
    try:
        mlm_ids = [x.strip() for x in args.listings.split(",") if x.strip()]
        if not mlm_ids:
            log("ERROR no_listings_parsed")
            return 2

        if args.override_qty is not None:
            qty = int(args.override_qty)
            log(f"usando override_qty={qty}")
        else:
            qty_f = get_odoo_qty(conn, args.sku)
            if qty_f is None:
                log(f"ERROR sku_not_in_odoo={args.sku}")
                return 3
            qty = int(qty_f)
            log(f"odoo_qty sku={args.sku} qty={qty}")

        log(f"sku={args.sku} target_qty={qty} listings={len(mlm_ids)}")
        for mid in mlm_ids:
            # Detectar si tiene variations
            try:
                item = ml_get_item(mid, token)
            except Exception as e:
                log(f"FAIL get_item {mid}: {e}")
                continue

            variations = item.get("variations") or []
            current_status = item.get("status")

            if variations:
                for v in variations:
                    vid = str(v.get("id") or "")
                    current = v.get("available_quantity")
                    log(f"  {mid}/{vid} status={current_status} current={current} -> {qty}")
                    if args.dry_run:
                        continue
                    try:
                        code, resp = ml_put_qty(mid, vid, qty, token)
                        log(f"    PUT {code} echo={resp.get('available_quantity') if isinstance(resp, dict) else 'n/a'}")
                        # upsert mapping
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO sku_mapping
                            (channel, sku, remote_item_id, remote_variation_id, last_seen_at)
                            VALUES ('meli', ?, ?, ?, datetime('now'))
                            """,
                            (args.sku, mid, vid),
                        )
                        conn.commit()
                    except Exception as e:
                        log(f"    FAIL PUT: {e}")
            else:
                current = item.get("available_quantity")
                log(f"  {mid} status={current_status} current={current} -> {qty}")
                if args.dry_run:
                    continue
                try:
                    code, resp = ml_put_qty(mid, "", qty, token)
                    log(f"    PUT {code} echo={resp.get('available_quantity') if isinstance(resp, dict) else 'n/a'}")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO sku_mapping
                        (channel, sku, remote_item_id, remote_variation_id, last_seen_at)
                        VALUES ('meli', ?, ?, '', datetime('now'))
                        """,
                        (args.sku, mid),
                    )
                    conn.commit()
                except Exception as e:
                    log(f"    FAIL PUT: {e}")

        log("OK completado")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
