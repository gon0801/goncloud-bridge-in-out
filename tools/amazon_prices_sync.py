#!/usr/bin/env python3
"""
Amazon SP-API → bridge.db: amazon_listing_prices
Solicita GET_MERCHANT_LISTINGS_ALL_DATA, espera resultado, parsea TSV, guarda en bridge.db.

Deploy:
    cp tools/amazon_prices_sync.py /mnt/data/appdata/bridge/tools/
    systemctl enable --now amazon-prices-sync.timer
"""

import csv
import gzip
import io
import json
import logging
import os
import sqlite3
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────
BRIDGE_DB  = os.getenv("BRIDGE_DB", "/mnt/data/appdata/bridge/data/bridge.db")
LWA_URL    = "https://api.amazon.com/auth/o2/token"
SP_API     = "https://sellingpartnerapi-na.amazon.com"

# Marketplaces a sincronizar.
# Si tienes cuenta Unified NA, ambos usan el mismo refresh_token.
# Comenta la línea de US si solo usas MX.
MARKETPLACES = [
    ("A1AM78C64UM0Y8", "amazon_mx"),
    ("ATVPDKIKX0DER",  "amazon_us"),
]

logging.basicConfig(
    level="INFO",
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(BRIDGE_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(db, key: str) -> str:
    row = db.execute("SELECT value FROM bridge_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def init_table(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS amazon_listing_prices (
        id                  INTEGER PRIMARY KEY,
        seller_sku          TEXT    NOT NULL,
        asin                TEXT,
        listing_id          TEXT,
        marketplace_id      TEXT    NOT NULL,   -- e.g. A1AM78C64UM0Y8
        marketplace_name    TEXT,               -- amazon_mx | amazon_us
        price               REAL,
        quantity            INTEGER,
        fulfillment_channel TEXT,               -- DEFAULT | AMAZON (FBA)
        item_name           TEXT,
        status              TEXT,               -- Active | Inactive
        fetched_at          TEXT    DEFAULT (datetime('now')),
        UNIQUE(seller_sku, marketplace_id)
    );

    -- FBA inventory (de report GET_AFN_INVENTORY_DATA)
    -- Merchant listings report deja quantity=NULL para FBA — ESTE report tiene el stock real.
    CREATE TABLE IF NOT EXISTS amazon_fba_inventory (
        id                  INTEGER PRIMARY KEY,
        seller_sku          TEXT    NOT NULL,
        fnsku               TEXT,
        asin                TEXT,
        marketplace_id      TEXT    NOT NULL,
        marketplace_name    TEXT,
        condition_type      TEXT,
        quantity_available  INTEGER,            -- sellable stock en FBA warehouse
        fetched_at          TEXT    DEFAULT (datetime('now')),
        UNIQUE(seller_sku, marketplace_id)
    );
    CREATE INDEX IF NOT EXISTS idx_fba_asin ON amazon_fba_inventory(asin, marketplace_id);
    """)
    db.commit()


# ── LWA Auth ──────────────────────────────────────────────────────────────────
def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(LWA_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     client_id,
        "client_secret": client_secret,
    }, timeout=15)
    resp.raise_for_status()
    token = resp.json()["access_token"]
    log.info("LWA access token obtenido")
    return token


# ── Reports API ───────────────────────────────────────────────────────────────
def request_report(token: str, marketplace_id: str) -> str:
    headers = {
        "x-amz-access-token": token,
        "Content-Type": "application/json",
    }
    body = {
        "reportType":     "GET_MERCHANT_LISTINGS_ALL_DATA",
        "marketplaceIds": [marketplace_id],
    }
    resp = requests.post(
        f"{SP_API}/reports/2021-06-30/reports",
        json=body, headers=headers, timeout=30
    )
    resp.raise_for_status()
    report_id = resp.json()["reportId"]
    log.info(f"Reporte solicitado: {report_id}")
    return report_id


def wait_for_report(token: str, report_id: str, timeout_sec: int = 900) -> str:
    """Polling hasta que el reporte esté DONE. Retorna reportDocumentId."""
    headers = {"x-amz-access-token": token}
    url = f"{SP_API}/reports/2021-06-30/reports/{report_id}"
    start = time.time()
    while time.time() - start < timeout_sec:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("processingStatus", "")
        log.info(f"Reporte {report_id}: {status}")
        if status == "DONE":
            return data["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            raise Exception(f"Reporte falló con estado: {status}")
        time.sleep(30)
    raise Exception(f"Reporte {report_id} no terminó en {timeout_sec}s")


def download_report(token: str, document_id: str) -> str:
    """Descarga el documento y retorna el contenido como string UTF-8."""
    headers = {"x-amz-access-token": token}
    resp = requests.get(
        f"{SP_API}/reports/2021-06-30/documents/{document_id}",
        headers=headers, timeout=15
    )
    resp.raise_for_status()
    doc         = resp.json()
    url         = doc["url"]
    compressed  = doc.get("compressionAlgorithm") == "GZIP"
    data_resp   = requests.get(url, timeout=120)
    data_resp.raise_for_status()
    content = gzip.decompress(data_resp.content) if compressed else data_resp.content
    log.info(f"Documento descargado: {len(content)} bytes")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


# ── Parse & Save ──────────────────────────────────────────────────────────────
def parse_and_save(tsv: str, db, marketplace_id: str, marketplace_name: str):
    reader  = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    count   = 0
    skipped = 0
    for row in reader:
        sku = (row.get("seller-sku") or "").strip()
        if not sku:
            skipped += 1
            continue

        price_str = (row.get("price") or "").strip()
        price     = float(price_str) if price_str else None

        qty_str = (row.get("quantity") or "").strip()
        qty     = int(qty_str) if qty_str else None

        db.execute("""
            INSERT INTO amazon_listing_prices
                (seller_sku, asin, listing_id, marketplace_id, marketplace_name,
                 price, quantity, fulfillment_channel, item_name, status, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(seller_sku, marketplace_id) DO UPDATE SET
                price               = excluded.price,
                quantity            = excluded.quantity,
                fulfillment_channel = excluded.fulfillment_channel,
                item_name           = excluded.item_name,
                status              = excluded.status,
                fetched_at          = excluded.fetched_at
        """, (
            sku,
            (row.get("asin1")               or "").strip(),
            (row.get("listing-id")          or "").strip(),
            marketplace_id,
            marketplace_name,
            price,
            qty,
            (row.get("fulfillment-channel") or "").strip(),
            (row.get("item-name")           or "").strip()[:500],
            (row.get("status")              or "").strip(),
        ))
        count += 1

    db.commit()
    log.info(f"[{marketplace_name}] {count} listings guardados, {skipped} sin SKU ignorados")


# ── FBA Inventory report ──────────────────────────────────────────────────────
def request_fba_inventory_report(token: str, marketplace_id: str) -> str:
    """Solicita GET_AFN_INVENTORY_DATA (FBA sellable inventory snapshot)."""
    headers = {"x-amz-access-token": token, "Content-Type": "application/json"}
    body = {
        "reportType":     "GET_AFN_INVENTORY_DATA",
        "marketplaceIds": [marketplace_id],
    }
    resp = requests.post(
        f"{SP_API}/reports/2021-06-30/reports",
        json=body, headers=headers, timeout=30
    )
    resp.raise_for_status()
    report_id = resp.json()["reportId"]
    log.info(f"FBA report solicitado: {report_id}")
    return report_id


def parse_and_save_fba(tsv: str, db, marketplace_id: str, marketplace_name: str):
    """GET_AFN_INVENTORY_DATA retorna columnas tab-separated:
    seller-sku | fulfillment-channel-sku | asin | condition-type | Warehouse-Condition-code |
    Quantity Available | ...
    Algunas columnas pueden variar por marketplace. Usamos DictReader para resiliencia.
    """
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    count = 0
    skipped = 0
    for row in reader:
        sku = (row.get("seller-sku") or "").strip()
        if not sku:
            skipped += 1
            continue

        # Amazon usa varios nombres de columna según marketplace:
        qty_str = (row.get("Quantity Available") or row.get("afn-fulfillable-quantity")
                   or row.get("quantity-available") or "").strip()
        qty = int(qty_str) if qty_str.isdigit() else 0

        db.execute("""
            INSERT INTO amazon_fba_inventory
                (seller_sku, fnsku, asin, marketplace_id, marketplace_name,
                 condition_type, quantity_available, fetched_at)
            VALUES (?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(seller_sku, marketplace_id) DO UPDATE SET
                fnsku              = excluded.fnsku,
                asin               = excluded.asin,
                condition_type     = excluded.condition_type,
                quantity_available = excluded.quantity_available,
                fetched_at         = excluded.fetched_at
        """, (
            sku,
            (row.get("fulfillment-channel-sku") or "").strip(),
            (row.get("asin") or "").strip(),
            marketplace_id,
            marketplace_name,
            (row.get("condition-type") or "").strip(),
            qty,
        ))
        count += 1

    db.commit()
    log.info(f"[{marketplace_name} FBA] {count} items guardados, {skipped} sin SKU ignorados")


def sync_fba_inventory(token: str, db, marketplace_id: str, marketplace_name: str):
    """Pipeline completo FBA: request → poll → download → parse → upsert."""
    try:
        report_id   = request_fba_inventory_report(token, marketplace_id)
        document_id = wait_for_report(token, report_id)
        tsv         = download_report(token, document_id)
        parse_and_save_fba(tsv, db, marketplace_id, marketplace_name)
    except Exception as e:
        log.error(f"FBA sync error {marketplace_name}: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    db = get_db()
    init_table(db)

    # Leer credenciales desde bridge_settings
    client_id     = get_setting(db, "amazon_sp_api_client_id")
    client_secret = get_setting(db, "amazon_sp_api_client_secret")
    refresh_token = get_setting(db, "amazon_sp_api_refresh_token")
    seller_id     = get_setting(db, "amazon_seller_id")

    missing = [k for k, v in {
        "amazon_sp_api_client_id":     client_id,
        "amazon_sp_api_client_secret": client_secret,
        "amazon_sp_api_refresh_token": refresh_token,
        "amazon_seller_id":            seller_id,
    }.items() if not v]

    if missing:
        log.error(f"Faltan en bridge_settings: {missing}")
        log.error("Ejecuta: SELECT key, value FROM bridge_settings WHERE key LIKE 'amazon%';")
        db.close()
        return

    token = get_access_token(client_id, client_secret, refresh_token)

    for marketplace_id, marketplace_name in MARKETPLACES:
        log.info(f"── Procesando {marketplace_name} ({marketplace_id}) — merchant listings ──")
        try:
            report_id   = request_report(token, marketplace_id)
            document_id = wait_for_report(token, report_id)
            tsv         = download_report(token, document_id)
            parse_and_save(tsv, db, marketplace_id, marketplace_name)
        except Exception as e:
            log.error(f"Error merchant {marketplace_name}: {e}")

        # FBA inventory — reporte separado, stock real de Amazon warehouses
        log.info(f"── Procesando {marketplace_name} ({marketplace_id}) — FBA inventory ──")
        sync_fba_inventory(token, db, marketplace_id, marketplace_name)

    db.close()
    log.info("Sync de precios + FBA inventory Amazon completado")


if __name__ == "__main__":
    main()
