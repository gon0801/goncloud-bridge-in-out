#!/usr/bin/env python3
"""
Sync MeLi listings to cache table
Runs periodically to keep meli_listings_cache fresh
"""

import sqlite3
import requests
import json
import sys
from datetime import datetime

DB_PATH = "/data/bridge.db"
TOKEN_FILE = "/data/.meli_tokens.json"
LOG_FILE = "/data/logs/sync_meli_listings.log"


def log(msg):
    timestamp = datetime.now().isoformat()
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_token():
    """Get MeLi access token"""
    try:
        with open(TOKEN_FILE) as f:
            tokens = json.load(f)
        return tokens.get("access_token"), tokens.get("user_id")
    except Exception as e:
        log(f"ERROR: Cannot read token file: {e}")
        return None, None


def fetch_all_items(token, user_id):
    """Fetch all active item IDs using scan"""
    headers = {"Authorization": f"Bearer {token}"}
    items = []
    url = f"https://api.mercadolibre.com/users/{user_id}/items/search?search_type=scan&limit=100&status=active"

    iterations = 0
    max_iterations = 50

    while url and iterations < max_iterations:
        iterations += 1
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                log(f"WARN: Items search returned {resp.status_code}")
                break

            data = resp.json()
            item_ids = data.get("results", [])
            if not item_ids:
                break

            items.extend(item_ids)
            log(f"Fetched {len(item_ids)} items (total: {len(items)})")

            scroll_id = data.get("scroll_id")
            if scroll_id:
                url = f"https://api.mercadolibre.com/users/{user_id}/items/search?search_type=scan&limit=100&scroll_id={scroll_id}&status=active"
            else:
                break
        except Exception as e:
            log(f"ERROR in items scan: {e}")
            break

    log(f"Total items found: {len(items)}")
    return items


def fetch_item_details(token, item_id):
    """Fetch full item details including variations"""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}", headers=headers, timeout=10
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            log(f"WARN: Item {item_id} returned {resp.status_code}")
            return None
    except Exception as e:
        log(f"ERROR fetching item {item_id}: {e}")
        return None


def fetch_variation_details(token, item_id, var_id):
    """Fetch variation details to get SELLER_SKU"""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def process_item(token, item_data):
    """Extract listings from item data - fetches variation details for SELLER_SKU"""
    listings = []

    item_id = item_data.get("id")
    title = item_data.get("title", "")[:100]
    item_sku = item_data.get("seller_custom_field") or ""
    price = item_data.get("price", 0)
    status = item_data.get("status", "unknown")
    variations = item_data.get("variations", [])

    if variations:
        for v in variations:
            var_id = str(v.get("id", ""))

            # Fetch variation details to get SELLER_SKU
            var_data = fetch_variation_details(token, item_id, var_id)

            var_sku = ""
            if var_data:
                # Extract SELLER_SKU from variation attributes
                for attr in var_data.get("attributes", []):
                    if attr.get("id") == "SELLER_SKU":
                        var_sku = attr.get("value_name", "")
                        break

            # Fallback to seller_custom_field if no SELLER_SKU
            if not var_sku:
                var_sku = v.get("seller_custom_field") or item_sku

            # Build variation name
            var_name = " / ".join(
                [
                    attr.get("value_name", "")
                    for attr in v.get("attribute_combinations", [])
                    if attr.get("id") not in ("SELLER_SKU", "GTIN")
                ]
            )[:100]

            listings.append(
                {
                    "item_id": item_id,
                    "variation_id": var_id,
                    "title": title,
                    "var_name": var_name,
                    "sku": var_sku,
                    "qty": v.get("available_quantity", 0),
                    "price": v.get("price", price),
                    "status": status,
                }
            )
    else:
        # Item without variations
        listings.append(
            {
                "item_id": item_id,
                "variation_id": "",
                "title": title,
                "var_name": "",
                "sku": item_sku,
                "qty": item_data.get("available_quantity", 0),
                "price": price,
                "status": status,
            }
        )

    return listings


def sync_to_cache(listings):
    """Write listings to cache table (idempotent)"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Clear cache (simple approach - or use DELETE + INSERT)
    cur.execute("DELETE FROM meli_listings_cache")

    # Insert all listings
    for listing in listings:
        cur.execute(
            """
            INSERT INTO meli_listings_cache
            (item_id, variation_id, title, var_name, sku, qty, price, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
            (
                listing["item_id"],
                listing["variation_id"],
                listing["title"],
                listing["var_name"],
                listing["sku"],
                listing["qty"],
                listing["price"],
                listing["status"],
            ),
        )

    conn.commit()
    conn.close()
    log(f"Synced {len(listings)} listings to cache")


def main():
    log("=== MeLi Listings Sync START ===")

    # Get token
    token, user_id = get_token()
    if not token or not user_id:
        log("ERROR: No valid token")
        sys.exit(1)

    # Fetch all items
    item_ids = fetch_all_items(token, user_id)
    if not item_ids:
        log("WARN: No items found")
        sys.exit(0)

    # Fetch details and build listings
    all_listings = []
    errors = 0

    for idx, item_id in enumerate(item_ids):
        if idx % 10 == 0:
            log(f"Processing items: {idx}/{len(item_ids)}")

        item_data = fetch_item_details(token, item_id)
        if item_data:
            listings = process_item(token, item_data)
            all_listings.extend(listings)
        else:
            errors += 1
            if errors > 10:
                log("ERROR: Too many errors, stopping")
                break

    # Sync to cache
    if all_listings:
        sync_to_cache(all_listings)

    log(
        f"=== MeLi Listings Sync COMPLETE === Total: {len(all_listings)}, Errors: {errors}"
    )


if __name__ == "__main__":
    main()
