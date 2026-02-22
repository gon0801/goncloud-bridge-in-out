import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import hashlib
from datetime import datetime, timezone
from typing import List, Optional

import redis
import requests
from fastapi import FastAPI, Request, Header
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")

ENABLE_MISSING_ZERO_CHANNELS = {
    c.strip()
    for c in os.getenv("ENABLE_MISSING_ZERO_CHANNELS", "").split(",")
    if c.strip()
}

# SKU canónico: MAYÚSCULAS, NÚMEROS y GUIONES
SKU_REGEX = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*$")

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="Stock Bridge", version="0.3")
logger = logging.getLogger(__name__)

# =========================================================
# MERCADOLIBRE OAUTH — AUTHORIZATION CODE FLOW (SERVER SIDE)
# =========================================================

MELI_CLIENT_ID = "2932799975062215"
MELI_CLIENT_SECRET = "GAhxyksH714JoOXz0JGw9sffs7PTXO3J"
MELI_REDIRECT_URI = "https://meli.goncloud.cc/oauth/callback"

TOKEN_FILE = "/data/.meli_tokens.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_iso_seconds() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    conn = db_conn()
    try:
        # Eventos crudos (auditoría)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              channel TEXT NOT NULL,
              item_count INTEGER NOT NULL,
              payload TEXT NOT NULL
            )
            """
        )

        # Idempotencia (worker)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
              event_id TEXT PRIMARY KEY,
              channel TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT,
              updated_at TEXT
            )
            """
        )

        # Métricas
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_metrics (
              key TEXT PRIMARY KEY,
              value INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Catálogo observado (NO fuente de verdad)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS known_skus (
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
              last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY(channel, sku)
            )
            """
        )

        # Snapshot por SKU (auditoría)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_items (
              event_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              qty INTEGER NOT NULL,
              derived_zero INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(event_id, channel, sku)
            )
            """
        )

        # Auditoría de SKUs rechazados (hardening)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rejected_skus (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              event_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              reason TEXT NOT NULL
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


def _save_tokens(data: dict):
    data["obtained_at"] = int(time.time())
    data["expires_at"] = data["obtained_at"] + int(data.get("expires_in", 0))
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


@app.get("/oauth/start")
def meli_oauth_start():
    params = {
        "response_type": "code",
        "client_id": MELI_CLIENT_ID,
        "redirect_uri": MELI_REDIRECT_URI,
        # IMPORTANTE: pide refresh token (offline access)
        "scope": "offline_access",
    }
    url = "https://auth.mercadolibre.com.mx/authorization?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@app.get("/oauth/callback")
def meli_oauth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "missing code"}, status_code=400)

    # Si el navegador/Cloudflare pega 2 veces el mismo callback, NO vuelvas a canjear el code
    last_code_file = "/data/.meli_last_code.txt"
    try:
        if os.path.exists(last_code_file):
            last = open(last_code_file, "r").read().strip()
            if last == code:
                # Ya lo procesamos, responde OK sin reintentar (evita invalid_grant por doble hit)
                return {
                    "ok": True,
                    "message": "Callback recibido (code duplicado). Ya fue procesado.",
                }
    except Exception:
        pass

    resp = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": MELI_CLIENT_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "code": code,
            "redirect_uri": MELI_REDIRECT_URI,
        },
        timeout=20,
    )

    if resp.status_code != 200:
        return JSONResponse(
            {"error": "token_exchange_failed", "details": resp.text},
            status_code=500,
        )

    data = resp.json()
    _save_tokens(data)

    # marca el code como ya consumido para que un refresh/doble carga no lo intente otra vez
    try:
        with open(last_code_file, "w") as f:
            f.write(code)
    except Exception:
        pass

    return {
        "ok": True,
        "message": "Tokens guardados correctamente en /data/.meli_tokens.json",
        "has_refresh_token": bool(data.get("refresh_token")),
        "expires_in": data.get("expires_in"),
        "user_id": data.get("user_id"),
        "warning": None if data.get("refresh_token") else "NO llegó refresh_token. Aun así se guardó access_token.",
    }


@app.post("/oauth/refresh")
def meli_oauth_refresh():
    if not os.path.exists(TOKEN_FILE):
        return JSONResponse({"error": "token_file_not_found"}, status_code=404)

    with open(TOKEN_FILE) as f:
        tokens = json.load(f)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return JSONResponse({"error": "no_refresh_token"}, status_code=400)

    resp = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": MELI_CLIENT_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=20,
    )

    if resp.status_code != 200:
        return JSONResponse({"error": "refresh_failed", "details": resp.text}, status_code=500)

    data = resp.json()
    _save_tokens(data)
    return {"ok": True, "refreshed": True, "expires_in": data.get("expires_in")}


# =========================================================
# INBOUND ML WEBHOOK (SAFE) — SOLO AUDITA + ENCOLA
# =========================================================

def _get_setting(key: str, default: str = "0") -> str:
    """
    Lee bridge_settings(key,value). Si no existe la tabla o falla, regresa default.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,))
        row = cur.fetchone()
        con.close()
        if row and row[0] is not None:
            return str(row[0])
        return default
    except Exception:
        return default


@app.post("/webhooks/meli/orders/{secret}")
@app.post("/webhooks/meli/orders")
async def meli_orders_webhook(
    request: Request,
    secret: str | None = None,
    x_goncloud_secret: Optional[str] = Header(default=None),
):
    """
    SAFE:
    - NO toca Odoo
    - NO toca outbound
    - Solo:
      1) valida gates + secret
      2) inserta inbound_events (auditoría)
      3) encola ml_orders_jobs en Redis
    """

    # Gates
    if _get_setting("meli_inbound_enabled", "0") != "1":
        return JSONResponse(
            {"ok": True, "ignored": "meli_inbound_enabled=0"},
            status_code=200,
        )

    if _get_setting("meli_webhook_enabled", "0") != "1":
        return JSONResponse(
            {"ok": True, "ignored": "meli_webhook_enabled=0"},
            status_code=200,
        )

    # Secret validation (HEADER o QUERYSTRING)
    expected = _get_setting("meli_webhook_secret", "")
    qsecret = request.query_params.get("secret")
    psecret = secret

    if not expected:
        return JSONResponse(
            {"ok": False, "error": "secret_not_configured"},
            status_code=500,
        )

    if (x_goncloud_secret or "") != expected and (qsecret or "") != expected and (psecret or "") != expected:
        return JSONResponse(
            {"ok": False, "error": "unauthorized"},
            status_code=401,
        )

    # Payload
    raw = await request.body()
    raw_text = raw.decode("utf-8", errors="replace")
    sha = hashlib.sha256(raw).hexdigest()
    received_at = _utc_now_iso_seconds()

    # Parse best-effort (la verdad se obtiene luego con GET /orders/{id})
    topic = "orders"
    resource = ""
    user_id = None
    try:
        data = json.loads(raw_text) if raw_text else {}
        if isinstance(data, dict):
            topic = str(data.get("topic") or data.get("type") or topic)
            resource = str(
                data.get("resource")
                or data.get("path")
                or data.get("resource_path")
                or ""
            )
            user_id = (
                data.get("user_id")
                or data.get("seller")
                or data.get("account_id")
            )
    except Exception:
        data = {}

    dedupe_key = f"rawsha:{sha}"

    # Insert inbound_events (auditoría)
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO inbound_events
            (received_at, topic, resource, user_id, payload_json, dedupe_key, status)
            VALUES (?, ?, ?, ?, ?, ?, 'queued')
            """,
            (
                received_at,
                topic,
                resource,
                str(user_id) if user_id is not None else None,
                raw_text,
                dedupe_key,
            ),
        )
        con.commit()
        con.close()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"db_insert_failed: {e}"},
            status_code=500,
        )

    # Enqueue Redis job
    try:
        job = {
            "topic": topic,
            "resource": resource,
            "received_at": received_at,
            "dedupe_key": dedupe_key,
            "rawsha": sha,
        }
        r.rpush(
            "ml_orders_jobs",
            json.dumps(job, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"redis_enqueue_failed: {e}"},
            status_code=500,
        )

    return JSONResponse({"ok": True, "queued": True}, status_code=200)


# =========================================================
# API EXISTENTE (SNAPSHOT OUTBOUND)
# =========================================================

class StockItem(BaseModel):
    sku: str = Field(min_length=1, max_length=120)
    qty: int = Field(ge=0, le=10_000_000)


class StockSnapshot(BaseModel):
    channel: str = Field(min_length=1, max_length=50)  # amazon_fbm / meli
    complete: bool = False
    items: List[StockItem]
    generated_at: Optional[str] = None


@app.get("/v1/health")
def health():
    return {"ok": True, "time": utc_now_iso()}


@app.get("/v1/debug/rejected-skus")
def debug_rejected_skus(limit: int = 20):
    # clamp para evitar abusos
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    conn = db_conn()
    try:
        rows = conn.execute(
            """
            SELECT created_at, event_id, channel, sku, reason
            FROM rejected_skus
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        items = [
            {
                "created_at": r[0],
                "event_id": r[1],
                "channel": r[2],
                "sku": r[3],
                "reason": r[4],
            }
            for r in rows
        ]
        return {"ok": True, "limit": limit, "items": items}
    finally:
        conn.close()


@app.post("/v1/stock/snapshot")
def stock_snapshot(snapshot: StockSnapshot):
    created_at = utc_now_iso()
    payload = snapshot.model_dump()
    payload_json = json.dumps(payload, ensure_ascii=False)

    conn = db_conn()
    try:
        cur = conn.execute(
            "INSERT INTO events (created_at, channel, item_count, payload) VALUES (?, ?, ?, ?)",
            (created_at, snapshot.channel, len(snapshot.items), payload_json),
        )
        event_id = cur.lastrowid

        # Hardening de SKUs (modo observación, no bloquea snapshot)
        valid_items: List[StockItem] = []
        rejected_count = 0

        for it in snapshot.items:
            raw = (it.sku or "").strip()

            if not raw:
                rejected_count += 1
                conn.execute(
                    "INSERT INTO rejected_skus(created_at, event_id, channel, sku, reason) VALUES (?,?,?,?,?)",
                    (created_at, str(event_id), snapshot.channel, "", "empty_sku"),
                )
                continue

            if not SKU_REGEX.match(raw):
                rejected_count += 1
                conn.execute(
                    "INSERT INTO rejected_skus(created_at, event_id, channel, sku, reason) VALUES (?,?,?,?,?)",
                    (created_at, str(event_id), snapshot.channel, raw, "regex_mismatch"),
                )
                continue

            valid_items.append(StockItem(sku=raw, qty=int(it.qty)))

        # métricas: total y por canal (solo si hubo rechazos)
        if rejected_count:
            conn.execute(
                """
                INSERT INTO bridge_metrics(key, value, updated_at)
                VALUES ('skus_rejected_total', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                  value = value + excluded.value,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (rejected_count,),
            )

            channel_key = f"skus_rejected_total_{snapshot.channel}"
            conn.execute(
                """
                INSERT INTO bridge_metrics(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                  value = value + excluded.value,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (channel_key, rejected_count),
            )

        missing_zero_enabled = False  # missing=0 DISABLED (complete snapshots ruido)
          # Upsert known_skus catalog (missing=0 disabled)
        if missing_zero_enabled:
            for it in valid_items:
                sku = it.sku.strip()
                if not sku:
                    continue
                conn.execute(
                    """
                    INSERT INTO known_skus(channel, sku) VALUES(?, ?)
                    ON CONFLICT(channel, sku) DO UPDATE SET last_seen_at=datetime('now')
                    """,
                    (snapshot.channel, sku),
                )

        # Si complete=true, expandir faltantes con qty=0 (missing=0)
        expanded_items: List[StockItem] = list(valid_items)
        derived_zero_skus = set()

        if missing_zero_enabled:
            present = {it.sku for it in valid_items}
            cur2 = conn.execute(
                "SELECT sku FROM known_skus WHERE channel=?", (snapshot.channel,)
            )
            known = {row[0] for row in cur2.fetchall()}
            missing = sorted(list(known - present))
            for sku in missing:
                expanded_items.append(StockItem(sku=sku, qty=0))
                derived_zero_skus.add(sku)

        # Guardar snapshot_items (auditoría por SKU)
        for it in expanded_items:
            sku = it.sku.strip()
            if not sku:
                continue
            qty = int(it.qty)
            if qty < 0:
                qty = 0

            conn.execute(
                """
                INSERT OR REPLACE INTO snapshot_items(event_id, channel, sku, qty, derived_zero)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    str(event_id),
                    snapshot.channel,
                    sku,
                    qty,
                    1 if sku in derived_zero_skus else 0,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    # Encolar jobs en Redis (uno por SKU) — usando items expandidos
    enqueued = 0
    for it in expanded_items:
        job = {
            "event_id": event_id,
            "channel": snapshot.channel,
            "sku": it.sku,
            "qty": it.qty,
            "created_at": created_at,
        }
        r.rpush("stock_jobs", json.dumps(job, ensure_ascii=False))
        enqueued += 1

    return {
        "ok": True,
        "event_id": event_id,
        "enqueued": enqueued,
        "expanded": True if missing_zero_enabled else False,
        "derived_zero": len(derived_zero_skus),
    }

# ============================================================
# AMAZON WEBHOOK (SNS Notifications)
# ============================================================

@app.post("/webhooks/amazon/orders/{secret}")
@app.post("/webhooks/amazon/orders")
async def amazon_orders_webhook(
    request: Request,
    secret: str | None = None,
    x_goncloud_secret: Optional[str] = Header(default=None),
):
    """Amazon SNS webhook endpoint."""
    if _get_setting("amazon_inbound_enabled", "0") != "1":
        return JSONResponse({"ok": True, "ignored": "amazon_inbound_enabled=0"}, status_code=200)
    if _get_setting("amazon_webhook_enabled", "0") != "1":
        return JSONResponse({"ok": True, "ignored": "amazon_webhook_enabled=0"}, status_code=200)
    expected = _get_setting("amazon_webhook_secret", "")
    qsecret = request.query_params.get("secret")
    psecret = secret
    if not expected:
        return JSONResponse({"ok": False, "error": "secret_not_configured"}, status_code=500)
    if (x_goncloud_secret or "") != expected and (qsecret or "") != expected and (psecret or "") != expected:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    raw = await request.body()
    raw_text = raw.decode("utf-8", errors="replace")
    sha = hashlib.sha256(raw).hexdigest()
    received_at = _utc_now_iso_seconds()
    try:
        data = json.loads(raw_text) if raw_text else {}
    except Exception:
        data = {}
    msg_type = request.headers.get("x-amz-sns-message-type", "")
    if msg_type == "SubscriptionConfirmation":
        subscribe_url = data.get("SubscribeURL")
        if subscribe_url:
            import httpx
            httpx.get(subscribe_url, timeout=10)
            return {"ok": True, "confirmed": True}
    order_id = ""
    event_type = "notification"
    message: dict = {}
    if isinstance(data, dict):
        msg_raw = data.get("Message", "{}")
        if isinstance(msg_raw, str):
            try:
                message = json.loads(msg_raw)
            except Exception:
                message = {}
        elif isinstance(msg_raw, dict):
            message = msg_raw
        if isinstance(message, dict):
            order_id = message.get("AmazonOrderId", "")
            event_type = message.get("NotificationType", "notification")
    # Use deterministic dedupe key matching the poll-script format when full
    # order context is available, so webhook and poll don't double-process.
    _mp = message.get("MarketplaceId", "") if message else ""
    _st = message.get("OrderStatus", "") if message else ""
    if order_id and _mp and _st:
        dedupe_key = f"amz:{_mp}:{order_id}:{_st}"
    else:
        dedupe_key = f"amz-webhook:{sha[:16]}"
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO amazon_inbound_events (received_at, event_type, order_id, payload_json, dedupe_key, status) VALUES (?, ?, ?, ?, ?, 'pending')", (received_at, event_type, order_id, raw_text, dedupe_key))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[amazon_webhook] DB error: {e}")
    # order_json must be the parsed SNS Message body (has AmazonOrderId, OrderStatus,
    # FulfillmentChannel, MarketplaceId), NOT the raw SNS envelope.
    # The worker will fetch OrderItems from SP-API when they are absent.
    job = {
        "dedupe_key": dedupe_key,
        "order_json": message if message else data,
        "source": "webhook",
        "received_at": received_at,
    }
    r.rpush("amazon_orders_jobs", json.dumps(job, ensure_ascii=False))
    return {"ok": True, "dedupe_key": dedupe_key}

@app.get("/v1/settings")
def get_settings():
    """
    Internal read-only endpoint. Bridge is not publicly exposed.
    Returns current feature flags from bridge_settings.
    """
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value, updated_at FROM bridge_settings ORDER BY key")
        rows = cur.fetchall()

    settings = [{"key": k, "value": v, "updated_at": ts} for (k, v, ts) in rows]
    return {"settings": settings}

# =========================================================
# AMAZON SKU MAPPER - WEB UI
# =========================================================
from fastapi.responses import HTMLResponse


@app.get("/mapper", response_class=HTMLResponse)
async def sku_mapper_ui():
    html = open("/app/sku_mapper.html").read()
    return HTMLResponse(content=html)

@app.get("/amazon/mapper", response_class=HTMLResponse)
async def amazon_mapper_ui():
    html = open("/app/amazon_mapper.html").read()
    return HTMLResponse(content=html)

@app.get("/api/amazon/odoo-skus")
async def get_odoo_skus():
    import xmlrpc.client
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.getenv("ODOO_PASSWORD", "bloqnum1")
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, pwd, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        prods = models.execute_kw(db, uid, pwd, "product.product", "search_read",
            [[("sell_on_amazon_fbm", "=", True), ("active", "=", True), ("default_code", "!=", False)]],
            {"fields": ["default_code", "name", "display_name", "qty_available"]})
        return {"ok": True, "skus": [{"sku": p["default_code"], "name": p.get("display_name", "") or p.get("name", ""), "qty": p.get("qty_available", 0)} for p in prods]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/amazon/amazon-skus")
async def get_amazon_skus():
    """Get Amazon SKUs from cache - enriched with Odoo display names"""
    import xmlrpc.client
    
    # Get Amazon inventory from cache
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT seller_sku, asin, product_name, qty FROM amazon_inventory_cache ORDER BY seller_sku").fetchall()
    conn.close()
    
    if not rows:
        # Cache empty, try to refresh
        result = await refresh_amazon_inventory()
        if result.get("ok"):
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute("SELECT seller_sku, asin, product_name, qty FROM amazon_inventory_cache ORDER BY seller_sku").fetchall()
            conn.close()
    
    # Build base list
    skus_list = [{"sku": r[0], "asin": r[1], "name": r[2] or "", "qty": r[3]} for r in rows]
    
    # Enrich with Odoo display names (for mapped SKUs)
    try:
        url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
        db = os.getenv("ODOO_DB", "EHV")
        user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
        pwd = os.getenv("ODOO_PASSWORD", "bloqnum1")
        
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, pwd, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        
        # Get all amazon_sku -> odoo_sku mappings
        conn = sqlite3.connect(DB_PATH)
        mappings = conn.execute("SELECT seller_sku, odoo_default_code FROM amazon_sku_mapping").fetchall()
        conn.close()
        
        # Build lookup: amazon_sku -> odoo_sku
        mapping_dict = {m[0]: m[1] for m in mappings}
        
        # Get Odoo product info for mapped SKUs
        odoo_skus = list(set(mapping_dict.values()))
        if odoo_skus:
            odoo_prods = models.execute_kw(db, uid, pwd, "product.product", "search_read",
                [[("default_code", "in", odoo_skus), ("active", "=", True)]],
                {"fields": ["default_code", "display_name"]})
            
            # Build lookup: odoo_sku -> display_name
            odoo_lookup = {p["default_code"]: p["display_name"] for p in odoo_prods}
            
            # Enrich Amazon SKUs with Odoo display names
            for item in skus_list:
                amazon_sku = item["sku"]
                if amazon_sku in mapping_dict:
                    odoo_sku = mapping_dict[amazon_sku]
                    if odoo_sku in odoo_lookup:
                        item["name"] = odoo_lookup[odoo_sku]
    except Exception as e:
        logger.error(f"Error enriching Amazon SKUs with Odoo names: {e}")
        # Continue with Amazon names if Odoo lookup fails
    
    return {"ok": True, "skus": skus_list}

@app.post("/api/amazon/refresh-inventory")

async def refresh_amazon_inventory():
    """Refresh Amazon inventory cache from Listings Items API (FBA + FBM)"""
    conn = sqlite3.connect(DB_PATH)
    def get_setting(k):
        row = conn.execute("SELECT value FROM bridge_settings WHERE key=?", (k,)).fetchone()
        return row[0] if row else ""
    creds = {
        "refresh_token": get_setting("amazon_sp_api_refresh_token"),
        "client_id": get_setting("amazon_sp_api_client_id"),
        "client_secret": get_setting("amazon_sp_api_client_secret"),
        "marketplace": get_setting("amazon_marketplace_id") or "A1AM78C64UM0Y8",
        "seller_id": get_setting("amazon_seller_id") or "A29XRL07YRN0L"
    }
    if not creds["refresh_token"]:
        conn.close()
        return {"ok": False, "error": "No Amazon credentials"}
    try:
        token_resp = requests.post(
            "https://api.amazon.com/auth/o2/token",
            data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
                  "client_id": creds["client_id"], "client_secret": creds["client_secret"]},
            timeout=30
        )
        access_token = token_resp.json()["access_token"]
        skus = []
        next_token = None
        page = 0
        while True:
            page += 1
            url = (f"https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items"
                   f"/{creds['seller_id']}"
                   f"?marketplaceIds={creds['marketplace']}&status=BUYABLE&pageSize=20")
            if next_token:
                url += f"&pageToken={requests.utils.quote(next_token)}"
            resp = requests.get(url, headers={"x-amz-access-token": access_token}, timeout=30)
            data = resp.json()
            for item in data.get("items", []):
                sku = item.get("sku", "")
                summary = item.get("summaries", [{}])[0]
                asin = summary.get("asin", "")
                name = summary.get("itemName", "")
                skus.append((sku, asin, name, 0))
            logger.info(f"Amazon listings page {page}: {len(data.get('items', []))} items (total: {len(skus)})")
            next_token = data.get("pagination", {}).get("nextToken")
            if not next_token:
                break
        conn.execute("DELETE FROM amazon_inventory_cache")
        conn.executemany("INSERT INTO amazon_inventory_cache (seller_sku, asin, product_name, qty, updated_at) VALUES (?, ?, ?, ?, datetime('now'))", skus)
        conn.commit()
        conn.close()
        logger.info(f"Amazon inventory cache updated: {len(skus)} listings")
        return {"ok": True, "count": len(skus)}
    except Exception as e:
        conn.close()
        logger.error(f"Error refreshing Amazon inventory: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/api/amazon/mappings")
async def get_mappings():
    import xmlrpc.client
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT seller_sku, odoo_default_code, notes, created_at FROM amazon_sku_mapping").fetchall()
    conn.close()
    
    # Check sell_on_amazon_fbm in Odoo
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.getenv("ODOO_PASSWORD", "bloqnum1")
    
    sell_on_amazon_skus = set()
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, pwd, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        prods = models.execute_kw(db, uid, pwd, "product.product", "search_read",
            [[("sell_on_amazon_fbm", "=", True), ("active", "=", True)]],
            {"fields": ["default_code"]})
        sell_on_amazon_skus = {p["default_code"] for p in prods if p.get("default_code")}
    except:
        pass
    
    return {"ok": True, "mappings": [{
        "amazon_sku": r[0], 
        "odoo_sku": r[1], 
        "notes": r[2], 
        "created_at": r[3],
        "has_sell_on_amazon": r[1] in sell_on_amazon_skus
    } for r in rows]}

@app.post("/api/amazon/mappings")
async def save_mapping(request: Request):
    data = await request.json()
    odoo_sku = data.get("odoo_sku", "").strip()
    amazon_sku = data.get("amazon_sku", "").strip()
    if not odoo_sku or not amazon_sku:
        return {"ok": False, "error": "SKUs requeridos"}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO amazon_sku_mapping (seller_sku, odoo_default_code, notes, created_at) VALUES (?, ?, 'Web UI', datetime('now'))", (amazon_sku, odoo_sku))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/amazon/mappings/{odoo_sku}")
async def delete_mapping(odoo_sku: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM amazon_sku_mapping WHERE odoo_default_code = ?", (odoo_sku,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════
# MELI SKU MAPPER ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/meli/odoo-skus")
async def get_meli_odoo_skus():
    import xmlrpc.client
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.getenv("ODOO_PASSWORD", "bloqnum1")
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, pwd, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        prods = models.execute_kw(db, uid, pwd, "product.product", "search_read",
            [[("sell_on_meli", "=", True), ("active", "=", True), ("default_code", "!=", False)]],
            {"fields": ["default_code", "name", "display_name", "qty_available"]})
        return {"ok": True, "skus": [{"sku": p["default_code"], "name": p.get("display_name", "") or p.get("name", ""), "qty": p.get("qty_available", 0)} for p in prods]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/meli/meli-skus")
async def get_meli_skus():
    """Get SKUs from MercadoLibre listings"""
    conn = sqlite3.connect(DB_PATH)
    # Get from sku_mapping (consolidated table)
    rows = conn.execute("SELECT DISTINCT sku FROM sku_mapping WHERE channel='meli' AND sku IS NOT NULL AND sku != ''").fetchall()
    skus = [{"sku": r[0], "name": "", "qty": 0} for r in rows]
    conn.close()
    return {"ok": True, "skus": skus}

@app.get("/api/meli/mappings")
async def get_meli_mappings():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT seller_sku, odoo_default_code, notes, created_at FROM meli_sku_mapping").fetchall()
    conn.close()
    return {"ok": True, "mappings": [{"meli_sku": r[0], "odoo_sku": r[1], "notes": r[2], "created_at": r[3]} for r in rows]}

@app.post("/api/meli/mappings")
async def save_meli_mapping(request: Request):
    data = await request.json()
    odoo_sku = data.get("odoo_sku", "").strip()
    meli_sku = data.get("meli_sku", "").strip()
    if not odoo_sku or not meli_sku:
        return {"ok": False, "error": "SKUs requeridos"}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO meli_sku_mapping (seller_sku, odoo_default_code, notes, created_at) VALUES (?, ?, 'Web UI', datetime('now'))", (meli_sku, odoo_sku))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/meli/mappings/{odoo_sku}")
async def delete_meli_mapping(odoo_sku: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM meli_sku_mapping WHERE odoo_default_code = ?", (odoo_sku,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/meli/listings")
async def get_meli_listings():
    """Get MeLi listings from cache (fast)"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    cur.execute("""
        SELECT item_id, variation_id, title, var_name, sku, qty, price, status, updated_at
        FROM meli_listings_cache
        ORDER BY item_id, variation_id
    """)
    
    listings = []
    for row in cur.fetchall():
        listings.append({
            "item_id": row["item_id"],
            "variation_id": row["variation_id"],
            "title": row["title"],
            "var_name": row["var_name"],
            "sku": row["sku"],
            "qty": row["qty"]
        })
    
    conn.close()
    
    return {
        "ok": True,
        "listings": listings,
        "cached": True
    }

@app.post("/api/meli/listings/refresh")
async def refresh_meli_listings():
    """Trigger manual refresh of MeLi listings cache"""
    import subprocess
    try:
        subprocess.Popen([
            "python3", 
            "/data/sync_meli_listings.py"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        return {
            "ok": True,
            "message": "Refresh iniciado en background"
        }
    except Exception as e:
        logger.error(f"Error triggering refresh: {e}")
        return {
            "ok": False,
            "error": str(e)
        }

@app.get("/api/meli/sku-mappings")
async def get_meli_sku_mappings():
    """Get sku_mapping entries for meli channel with sell_on_meli status"""
    import xmlrpc.client
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT sku, remote_item_id, remote_variation_id, last_seen_at FROM sku_mapping WHERE channel='meli'").fetchall()
    conn.close()
    
    # Check sell_on_meli in Odoo
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.getenv("ODOO_PASSWORD", "bloqnum1")
    
    sell_on_meli_skus = set()
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, pwd, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        prods = models.execute_kw(db, uid, pwd, "product.product", "search_read",
            [[("sell_on_meli", "=", True), ("active", "=", True)]],
            {"fields": ["default_code"]})
        sell_on_meli_skus = {p["default_code"] for p in prods if p.get("default_code")}
    except:
        pass
    
    return {"ok": True, "mappings": [{
        "sku": r[0], 
        "remote_item_id": r[1], 
        "remote_variation_id": r[2], 
        "last_seen_at": r[3],
        "has_sell_on_meli": r[0] in sell_on_meli_skus
    } for r in rows]}

@app.post("/api/meli/sku-mappings")
async def save_meli_sku_mapping(request: Request):
    data = await request.json()
    odoo_sku = data.get("odoo_sku", "").strip()
    item_id = data.get("item_id", "").strip()
    variation_id = data.get("variation_id", "").strip()
    if not odoo_sku or not item_id:
        return {"ok": False, "error": "SKU e Item ID requeridos"}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO sku_mapping (channel, sku, remote_item_id, remote_variation_id, last_seen_at)
        VALUES ('meli', ?, ?, ?, datetime('now'))
    """, (odoo_sku, item_id, variation_id or ""))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/meli/sku-mappings/{item_id}/{variation_id}")
async def delete_meli_sku_mapping(item_id: str, variation_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM sku_mapping WHERE channel='meli' AND remote_item_id = ? AND remote_variation_id = ?", (item_id, variation_id))
    conn.commit()
    conn.close()
    return {"ok": True}

# =========================================================
# SETUP WIZARD — GONCLOUD SaaS
# =========================================================
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import xmlrpc.client

# Servir archivos estáticos
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

@app.get("/setup")
async def setup_wizard():
    """Sirve el wizard de setup (solo si no está configurado)"""
    conn = sqlite3.connect(DB_PATH)
    
    # Verificar si ya hay configuración existente
    checks = [
        "SELECT value FROM bridge_settings WHERE key='setup_completed' AND value='1'",
        "SELECT 1 FROM sku_mapping LIMIT 1",
        "SELECT 1 FROM amazon_sku_mapping WHERE odoo_default_code IS NOT NULL LIMIT 1"
    ]
    
    already_configured = False
    for check in checks:
        try:
            row = conn.execute(check).fetchone()
            if row:
                already_configured = True
                break
        except:
            pass
    
    conn.close()
    
    if already_configured:
        return JSONResponse({
            "error": "Sistema ya configurado",
            "message": "GONCLOUD ya está en producción. Usa /mapper para SKUs o /status para ver estado.",
            "links": {
                "mapper": "/mapper",
                "status": "/api/health"
            }
        }, status_code=403)
    
    return FileResponse("/app/static/setup.html")

@app.get("/setup/force")
async def setup_wizard_force(secret: str = ""):
    """Forzar wizard (requiere secret)"""
    if secret != "goncloud2026":
        return JSONResponse({"error": "Secret inválido"}, status_code=403)
    return FileResponse("/app/static/setup.html")

@app.post("/setup/api/test-odoo")
async def test_odoo_connection(request: Request):
    """Valida credenciales de Odoo"""
    data = await request.json()
    url = data.get("url", "").strip().rstrip("/")
    db = data.get("db", "").strip()
    user = data.get("user", "").strip()
    password = data.get("password", "")
    
    if not all([url, db, user, password]):
        return {"ok": False, "error": "Todos los campos son requeridos"}
    
    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, user, password, {})
        
        if not uid:
            return {"ok": False, "error": "Credenciales inválidas"}
        
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
        
        # Contar productos
        product_count = models.execute_kw(db, uid, password, 
            "product.product", "search_count", [[("active", "=", True)]])
        
        # Contar almacenes
        warehouse_count = models.execute_kw(db, uid, password,
            "stock.warehouse", "search_count", [[]])
        
        return {
            "ok": True,
            "products": product_count,
            "warehouses": warehouse_count,
            "uid": uid
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/setup/api/status")
async def setup_status():
    """Retorna estado actual del sistema"""
    meli_connected = False
    meli_user = None
    amazon_connected = False
    
    # Check MeLi token
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                tokens = json.load(f)
                if tokens.get("access_token"):
                    meli_connected = True
                    meli_user = tokens.get("user_id")
    except:
        pass
    
    # Check Amazon (simplified - check if credentials exist)
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM bridge_settings WHERE key='amazon_refresh_token'").fetchone()
        conn.close()
        amazon_connected = bool(row and row[0])
    except:
        pass
    
    return {
        "meli_connected": meli_connected,
        "meli_user": meli_user,
        "amazon_connected": amazon_connected
    }

@app.post("/setup/api/auto-map")
async def setup_auto_map(request: Request):
    """Ejecuta mapeo automático de SKUs"""
    data = await request.json()
    result = {"ok": True}
    
    conn = sqlite3.connect(DB_PATH)
    
    # Get Odoo config
    odoo_url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    odoo_db = os.getenv("ODOO_DB", "EHV")
    odoo_user = os.getenv("ODOO_USER", "")
    odoo_pwd = os.getenv("ODOO_PASSWORD", "")
    
    # Try to get from bridge_settings if env not set
    if not odoo_user:
        row = conn.execute("SELECT value FROM bridge_settings WHERE key='odoo_user'").fetchone()
        odoo_user = row[0] if row else ""
    if not odoo_pwd:
        row = conn.execute("SELECT value FROM bridge_settings WHERE key='odoo_password'").fetchone()
        odoo_pwd = row[0] if row else ""
    
    # Get Odoo SKUs
    odoo_skus = set()
    try:
        common = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(odoo_db, odoo_user, odoo_pwd, {})
        models = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/object", allow_none=True)
        
        products = models.execute_kw(odoo_db, uid, odoo_pwd, "product.product", "search_read",
            [[("active", "=", True), ("default_code", "!=", False)]],
            {"fields": ["default_code"]})
        odoo_skus = {p["default_code"] for p in products if p.get("default_code")}
    except Exception as e:
        return {"ok": False, "error": f"Odoo error: {e}"}
    
    # MeLi auto-map
    if data.get("meli"):
        try:
            mapped = 0
            pending = 0
            
            # Get MeLi listings with SKU
            with open(TOKEN_FILE) as f:
                tokens = json.load(f)
            access_token = tokens.get("access_token")
            user_id = tokens.get("user_id")
            
            if access_token and user_id:
                # Get active items
                resp = requests.get(
                    f"https://api.mercadolibre.com/users/{user_id}/items/search",
                    params={"status": "active", "limit": 100},
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                items = resp.json().get("results", [])
                
                for item_id in items:
                    # Get item details with variations
                    item_resp = requests.get(
                        f"https://api.mercadolibre.com/items/{item_id}",
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    item = item_resp.json()
                    
                    for var in item.get("variations", [{}]) or [{}]:
                        var_id = var.get("id", "")
                        
                        # Get SKU from variation attributes
                        sku = ""
                        if var_id:
                            var_resp = requests.get(
                                f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
                                headers={"Authorization": f"Bearer {access_token}"}
                            )
                            var_data = var_resp.json()
                            for attr in var_data.get("attributes", []):
                                if attr.get("id") == "SELLER_SKU":
                                    sku = attr.get("value_name", "")
                                    break
                        
                        if not sku:
                            # Try item-level seller_custom_field
                            sku = item.get("seller_custom_field") or ""
                        
                        if sku and sku in odoo_skus:
                            # Check if already mapped
                            existing = conn.execute(
                                "SELECT 1 FROM sku_mapping WHERE channel='meli' AND remote_item_id=? AND remote_variation_id=?",
                                (item_id, str(var_id) if var_id else "")
                            ).fetchone()
                            
                            if not existing:
                                conn.execute("""
                                    INSERT INTO sku_mapping (channel, sku, remote_item_id, remote_variation_id, last_seen_at)
                                    VALUES ('meli', ?, ?, ?, datetime('now'))
                                """, (sku, item_id, str(var_id) if var_id else ""))
                                mapped += 1
                        elif sku:
                            pending += 1
                
                conn.commit()
            
            result["meli"] = {"mapped": mapped, "pending": pending}
        except Exception as e:
            result["meli"] = {"mapped": 0, "pending": 0, "error": str(e)}
    
    # Amazon auto-map
    if data.get("amazon"):
        try:
            mapped = 0
            pending = 0
            
            # Get Amazon SKUs from inventory (if available)
            amazon_skus = conn.execute(
                "SELECT DISTINCT seller_sku FROM amazon_sku_mapping"
            ).fetchall()
            
            for row in amazon_skus:
                seller_sku = row[0]
                if seller_sku in odoo_skus:
                    # Check if mapping exists
                    existing = conn.execute(
                        "SELECT 1 FROM amazon_sku_mapping WHERE seller_sku=? AND odoo_default_code IS NOT NULL",
                        (seller_sku,)
                    ).fetchone()
                    
                    if not existing:
                        conn.execute(
                            "UPDATE amazon_sku_mapping SET odoo_default_code=? WHERE seller_sku=?",
                            (seller_sku, seller_sku)
                        )
                        mapped += 1
                else:
                    pending += 1
            
            conn.commit()
            result["amazon"] = {"mapped": mapped, "pending": pending}
        except Exception as e:
            result["amazon"] = {"mapped": 0, "pending": 0, "error": str(e)}
    
    conn.close()
    return result

@app.post("/setup/api/activate")
async def setup_activate(request: Request):
    """Guarda configuración y activa GONCLOUD"""
    data = await request.json()
    
    conn = sqlite3.connect(DB_PATH)
    
    def save_setting(key, value):
        conn.execute("""
            INSERT INTO bridge_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
    
    try:
        # Odoo settings
        odoo = data.get("odoo", {})
        if odoo.get("url"):
            save_setting("odoo_url", odoo["url"])
        if odoo.get("db"):
            save_setting("odoo_db", odoo["db"])
        if odoo.get("user"):
            save_setting("odoo_user", odoo["user"])
        if odoo.get("password"):
            save_setting("odoo_password", odoo["password"])
        
        # Amazon settings
        amazon = data.get("amazon", {})
        if amazon.get("enabled"):
            save_setting("amazon_inbound_enabled", "1" if amazon.get("inbound") else "0")
            save_setting("amazon_inbound_fbm_paid_enabled", "1" if amazon.get("fbm") else "0")
            save_setting("amazon_inbound_fba_paid_enabled", "1" if amazon.get("fba") else "0")
            save_setting("amazon_outbound_enabled", "1" if amazon.get("outbound") else "0")
            save_setting("amazon_outbound_interval", amazon.get("interval", "10"))
        else:
            save_setting("amazon_inbound_enabled", "0")
            save_setting("amazon_outbound_enabled", "0")
        
        # MeLi settings
        meli = data.get("meli", {})
        if meli.get("enabled"):
            save_setting("meli_inbound_enabled", "1" if meli.get("inbound") else "0")
            save_setting("meli_inbound_fbm_paid_enabled", "1" if meli.get("fbm") else "0")
            save_setting("meli_inbound_full_paid_enabled", "1" if meli.get("full") else "0")
            save_setting("meli_outbound_enabled", "1" if meli.get("outbound") else "0")
            save_setting("meli_adapter_enabled", "1" if meli.get("outbound") else "0")
            save_setting("meli_outbound_interval", meli.get("interval", "10"))
        else:
            save_setting("meli_inbound_enabled", "0")
            save_setting("meli_outbound_enabled", "0")
            save_setting("meli_adapter_enabled", "0")
        
        # Mark setup complete
        save_setting("setup_completed", "1")
        save_setting("setup_completed_at", utc_now_iso())
        
        conn.commit()
        conn.close()
        
        return {"ok": True}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}
