#!/usr/bin/env python3
"""
Escanea TODOS los listings activos del seller en MeLi y pobla sku_mapping.

Despues de que MeLi separa variantes, un mismo SKU (seller_custom_field) queda
en N listings distintos. Este script descubre esos listings y los registra en
sku_mapping. Requiere que el schema ya este migrado (PK = (channel, remote_item_id,
remote_variation_id)).

Uso:
  python3 /data/backfill_meli_mappings.py [--db /data/bridge.db]
                                           [--token-file /data/.meli_tokens.json]
                                           [--status active,paused]
                                           [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request


def log(msg: str) -> None:
    print(f"[backfill_meli_mappings] {msg}", flush=True)


def load_token(path: str) -> str:
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


def ml_get_json(url: str, token: str, retries: int = 3) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"ml_get_failed url={url} err={last}")


def list_seller_items(seller_id: str, token: str, status_filter: str) -> list[str]:
    ids: list[str] = []
    offset = 0
    limit = 50
    # MeLi limita scan por scroll_id para seller grandes; usamos offset paginado simple
    while True:
        params = {"status": status_filter, "limit": str(limit), "offset": str(offset)}
        url = (
            f"https://api.mercadolibre.com/users/{seller_id}/items/search?"
            + urllib.parse.urlencode(params)
        )
        data = ml_get_json(url, token)
        batch = data.get("results") or []
        if not batch:
            break
        ids.extend(batch)
        total = int(data.get("paging", {}).get("total") or 0)
        offset += limit
        if offset >= total or offset >= 1000:  # MeLi cap offset=1000 en search normal
            break
    return ids


def list_seller_items_scan(seller_id: str, token: str, status_filter: str) -> list[str]:
    """Usa scan mode (scroll) para seller grandes. Fallback de la funcion anterior."""
    ids: list[str] = []
    scroll_id = None
    while True:
        params = {"status": status_filter, "search_type": "scan", "limit": "100"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        url = (
            f"https://api.mercadolibre.com/users/{seller_id}/items/search?"
            + urllib.parse.urlencode(params)
        )
        data = ml_get_json(url, token)
        batch = data.get("results") or []
        if not batch:
            break
        ids.extend(batch)
        scroll_id = data.get("scroll_id")
        if not scroll_id:
            break
    return ids


def get_item_details(item_id: str, token: str) -> dict | None:
    # IMPORTANTE: incluir `attributes` (lista) ademas de `seller_custom_field` (legacy).
    # MeLi moderno guarda el SKU en attributes[id=SELLER_SKU], no en seller_custom_field
    # (que esta deprecated). Leer solo seller_custom_field produce mappings incorrectos
    # tras separacion de variantes — cada listing post-split puede tener SKU distinto
    # en attributes[SELLER_SKU] pero el mismo (o ninguno) en seller_custom_field.
    url = (
        f"https://api.mercadolibre.com/items/{item_id}"
        "?attributes=id,seller_custom_field,attributes,variations,status"
    )
    try:
        return ml_get_json(url, token)
    except Exception as e:
        log(f"WARN get_item_failed id={item_id} err={e}")
        return None


def extract_item_sku(item_data: dict) -> str:
    """Lee SKU con precedencia: attributes[SELLER_SKU] (fuente actual) -> seller_custom_field (legacy)."""
    for a in item_data.get("attributes") or []:
        if a.get("id") == "SELLER_SKU":
            val = str(a.get("value_name") or "").strip()
            if val:
                return val
    return str(item_data.get("seller_custom_field") or "").strip()


def extract_variation_sku(variation: dict, fallback: str = "") -> str:
    """Lee SKU de una variacion con precedencia:
    1) attribute_combinations[SELLER_SKU]
    2) variation.seller_custom_field (legacy)
    3) "" (sin fallback al SKU del padre — BFM-3)

    NO usamos el SKU del listing padre como fallback: en MeLi cada variación
    es un producto distinto (color/talle/etc) y heredar el SKU del padre
    creaba mappings 1:N falsos donde 1 SKU Odoo apuntaba a múltiples
    variation_id distintos. Si la variación no tiene SELLER_SKU propio, el
    caller la trata como huérfana y la skipea (PENDIENTES.md task 5).
    El parámetro `fallback` se conserva por compat de signature pero ya no
    se usa para emitir un mapping; queda disponible para logging del caller.
    """
    for a in variation.get("attribute_combinations") or []:
        if a.get("id") == "SELLER_SKU":
            val = str(a.get("value_name") or "").strip()
            if val:
                return val
    legacy = str(variation.get("seller_custom_field") or "").strip()
    if legacy:
        return legacy
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/bridge.db")
    parser.add_argument("--token-file", default="/data/.meli_tokens.json")
    parser.add_argument("--status", default="active")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = load_token(args.token_file)
    conn = sqlite3.connect(args.db)
    try:
        seller_id = get_setting(conn, "meli_seller_id") or get_setting(conn, "ml_user_id")
        if not seller_id:
            log("ERROR seller_id_missing (busque meli_seller_id o ml_user_id en bridge_settings)")
            return 2

        log(f"seller_id={seller_id} status={args.status}")
        log("obteniendo lista de items (scan mode) ...")
        try:
            item_ids = list_seller_items_scan(seller_id, token, args.status)
        except Exception as e:
            log(f"scan_mode_failed={e} -> fallback a paginado normal")
            item_ids = list_seller_items(seller_id, token, args.status)
        log(f"items_encontrados={len(item_ids)}")

        if not item_ids:
            log("no hay items -> nada que backfillear")
            return 0

        rows_to_upsert: list[tuple] = []
        for i, iid in enumerate(item_ids, 1):
            if i % 50 == 0:
                log(f"progreso {i}/{len(item_ids)}")
            d = get_item_details(iid, token)
            if not d:
                continue

            sku = extract_item_sku(d)
            variations = d.get("variations") or []

            if variations:
                # listing con variaciones internas (aun con una sola)
                for v in variations:
                    vsku = extract_variation_sku(v, fallback=sku)
                    if not vsku:
                        # BFM-3: variación sin SELLER_SKU propio. NO usamos el
                        # SKU del padre — el operador debe asignarlo en MeLi.
                        log(f"WARN orphan_variation item={iid} variation={v.get('id')} parent_sku={sku!r}")
                        continue
                    rows_to_upsert.append(
                        ("meli", vsku, iid, str(v.get("id") or ""))
                    )
            else:
                # listing plano (variante separada, o listing sin variaciones)
                if not sku:
                    continue
                rows_to_upsert.append(("meli", sku, iid, ""))

        log(f"mappings_a_upsert={len(rows_to_upsert)}")
        if args.dry_run:
            for r in rows_to_upsert[:20]:
                log(f"DRY {r}")
            log("DRY RUN -> no se escribe")
            return 0

        with conn:
            for channel, sku, iid, vid in rows_to_upsert:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sku_mapping
                    (channel, sku, remote_item_id, remote_variation_id, last_seen_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    """,
                    (channel, sku, iid, vid),
                )

        # Reporte post-backfill
        rep = conn.execute(
            """
            SELECT sku, COUNT(*) as n
            FROM sku_mapping WHERE channel='meli'
            GROUP BY sku HAVING n > 1
            ORDER BY n DESC LIMIT 20
            """
        ).fetchall()
        if rep:
            log("skus_con_multiples_listings (top 20):")
            for sku, n in rep:
                log(f"  {sku}: {n} listings")

        total = conn.execute(
            "SELECT COUNT(*) FROM sku_mapping WHERE channel='meli'"
        ).fetchone()[0]
        log(f"OK total_mappings_meli={total}")
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
