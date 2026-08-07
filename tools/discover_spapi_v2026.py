#!/usr/bin/env python3
"""
Discovery script: Amazon SP-API v2026-01-01 raw response dump.

Llama a la nueva API con includedData completo y vuelca el JSON crudo
de órdenes recientes para mapear los campos antes de migrar.

Uso:
  python3 discover_spapi_v2026.py [--days N] [--order ORDER_ID]
  python3 discover_spapi_v2026.py --order 702-XXXX-XXXX   # orden específica
  python3 discover_spapi_v2026.py --days 3               # últimas 3 días
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

DB_PATH = os.getenv("BRIDGE_DB_PATH") or os.getenv("BRIDGE_DB") or "/data/bridge.db"
BASE = "https://sellingpartnerapi-na.amazon.com"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"

MARKETPLACES = {
    "MX": "A1AM78C64UM0Y8",
    "US": "ATVPDKIKX0DER",
}

# Todos los includedData disponibles — queremos ver TODO
INCLUDED_DATA = "BUYER,PROCEEDS,FULFILLMENT,PACKAGES"


def get_setting(key: str) -> str:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,)
    ).fetchone()
    conn.close()
    return str(row["value"]) if row else ""


def get_token() -> str:
    creds = {
        "client_id": get_setting("amazon_sp_api_client_id"),
        "client_secret": get_setting("amazon_sp_api_client_secret"),
        "refresh_token": get_setting("amazon_sp_api_refresh_token"),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(
            f"ERROR: missing credentials in bridge_settings: {missing}", file=sys.stderr
        )
        sys.exit(1)
    resp = httpx.post(
        TOKEN_URL, data={"grant_type": "refresh_token", **creds}, timeout=30
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def spapi_get(token: str, url: str, params: dict = None) -> dict:
    for attempt in range(4):
        resp = httpx.get(
            url, params=params, headers={"x-amz-access-token": token}, timeout=30
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2**attempt
            print(f"  HTTP {resp.status_code} — retry en {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            print(
                f"  ERROR HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr
            )
            return {}
        return resp.json()
    return {}


def dump_order_fields(order: dict, label: str = ""):
    """Imprime un mapa plano de todos los campos y sub-campos del order."""
    print(f"\n{'=' * 70}")
    print(f"ORDER: {label}")
    print(f"{'=' * 70}")

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(obj, list):
            if not obj:
                print(f"  {prefix}: []")
            else:
                # Para listas, mostrar primer elemento con tipo
                print(f"  {prefix}: [list len={len(obj)}]")
                if isinstance(obj[0], dict):
                    walk(obj[0], f"{prefix}[0]")
                else:
                    print(f"  {prefix}[0]: {obj[0]!r}")
        else:
            print(f"  {prefix}: {obj!r}")

    walk(order)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", type=int, default=2, help="Días hacia atrás (default: 2)"
    )
    parser.add_argument("--marketplace", choices=["MX", "US", "BOTH"], default="BOTH")
    parser.add_argument("--order", help="Fetch una orden específica por ID")
    parser.add_argument(
        "--raw", action="store_true", help="Dump JSON crudo además del mapa de campos"
    )
    parser.add_argument(
        "--max",
        type=int,
        default=3,
        help="Máximo de órdenes a mostrar por marketplace (default: 3)",
    )
    args = parser.parse_args()

    print("Conectando a SP-API v2026-01-01...")
    print(f"DB: {DB_PATH}")
    token = get_token()
    print(f"Token OK ({len(token)} chars)\n")

    # Modo: orden específica
    if args.order:
        print(f"Fetching orden individual: {args.order}")
        url = f"{BASE}/orders/2026-01-01/orders/{args.order}"
        data = spapi_get(token, url, params={"includedData": INCLUDED_DATA})
        if not data:
            print("ERROR: no response")
            sys.exit(1)
        order = data.get("order") or data.get("payload") or data
        dump_order_fields(order, args.order)
        if args.raw:
            print("\n--- RAW JSON ---")
            print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    # Modo: listar órdenes recientes
    last_updated_after = (
        datetime.now(timezone.utc) - timedelta(days=args.days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Buscando órdenes actualizadas desde: {last_updated_after}")
    print(f"includedData: {INCLUDED_DATA}\n")

    marketplaces = (
        list(MARKETPLACES.items())
        if args.marketplace == "BOTH"
        else [(args.marketplace, MARKETPLACES[args.marketplace])]
    )

    for mp_name, mp_id in marketplaces:
        print(f"\n{'#' * 70}")
        print(f"# MARKETPLACE: {mp_name} ({mp_id})")
        print(f"{'#' * 70}")

        url = f"{BASE}/orders/2026-01-01/orders"
        params = {
            "marketplaceIds": mp_id,
            "lastUpdatedAfter": last_updated_after,
            "includedData": INCLUDED_DATA,
        }
        data = spapi_get(token, url, params=params)
        if not data:
            print("  ERROR: no response")
            continue

        # Mostrar estructura top-level del response
        print("\n--- TOP-LEVEL RESPONSE KEYS ---")
        print(f"  keys: {list(data.keys())}")
        if "paginationToken" in data:
            print("  paginationToken: presente")
        if "NextToken" in data:
            print("  NextToken: presente (aún usa nombre v0!)")

        # Buscar la lista de órdenes (puede estar en distintos keys)
        orders = (
            data.get("orders")
            or data.get("Orders")
            or (data.get("payload") or {}).get("Orders")
            or []
        )
        print(f"  órdenes encontradas: {len(orders)}")

        if not orders:
            print("  Sin órdenes en este marketplace")
            if args.raw:
                print("\n--- RAW RESPONSE ---")
                print(json.dumps(data, indent=2, ensure_ascii=False))
            continue

        # Mostrar hasta --max órdenes
        shown = 0
        # Intentar mostrar una de cada perfil si es posible
        for order in orders[: args.max * 5]:
            if shown >= args.max:
                break
            order_id = order.get("orderId") or order.get("AmazonOrderId") or "UNKNOWN"
            dump_order_fields(order, f"{mp_name}:{order_id}")
            if args.raw:
                print("\n--- RAW JSON ---")
                print(json.dumps(order, indent=2, ensure_ascii=False))
            shown += 1

        print(f"\n[mostrando {shown} de {len(orders)} órdenes — usa --max N para más]")


if __name__ == "__main__":
    main()
