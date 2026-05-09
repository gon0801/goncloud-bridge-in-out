import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import hashlib
import defusedxml.xmlrpc as _defusedxml_xmlrpc; _defusedxml_xmlrpc.monkey_patch()
from datetime import datetime, timezone
from contextlib import closing
from typing import List, Optional

import redis
import requests
from fastapi import FastAPI, Request, Header, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field

from auth_middleware import require_secret

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


# H-07/MI-21 CSRF — double-submit cookie pattern para requests que vienen
# vía Cloudflare Access cookie. Requests con X-Goncloud-Secret header son
# scripts/cron (no browser) y pasan sin CSRF. Webhooks (/webhooks/*) y
# oauth (/oauth/*) tienen sus propios mecanismos de validación. La cookie
# `csrf_token` se setea en cada respuesta; el JS frontend lee la cookie y
# manda `X-CSRF-Token` header en mutaciones (ver setup.html → csrf.js).
import secrets as _bridge_secrets

_BRIDGE_CSRF_COOKIE = "csrf_token"
_BRIDGE_CSRF_EXEMPT_PREFIX = ("/webhooks/", "/oauth/", "/health", "/static")


def _bridge_ensure_csrf(request, response):
    existing = request.cookies.get(_BRIDGE_CSRF_COOKIE)
    if existing and len(existing) == 64:
        return existing
    token = _bridge_secrets.token_hex(32)
    response.set_cookie(
        key=_BRIDGE_CSRF_COOKIE,
        value=token,
        max_age=86400 * 30,
        path="/",
        samesite="strict",
        secure=True,
        httponly=False,
    )
    return token


@app.middleware("http")
async def _bridge_csrf_middleware(request: Request, call_next):
    import hmac as _hmac_local
    path = request.url.path or ""
    method = request.method.upper()
    is_exempt_path = any(path.startswith(p) for p in _BRIDGE_CSRF_EXEMPT_PREFIX)
    is_mutation = method in ("POST", "PUT", "DELETE", "PATCH")
    # Solo enforce CSRF cuando es mutación de browser (Cf-Access header presente
    # implica request via Cloudflare Access SSO) y no es exempt path. Scripts
    # ops usan X-Goncloud-Secret y nunca llevan cookies del browser.
    if is_mutation and not is_exempt_path:
        has_cf_access = bool(request.headers.get("cf-access-authenticated-user-email"))
        has_secret_header = bool(request.headers.get("x-goncloud-secret"))
        if has_cf_access and not has_secret_header:
            cookie_token = request.cookies.get(_BRIDGE_CSRF_COOKIE, "")
            header_token = request.headers.get("x-csrf-token", "")
            if not cookie_token or not header_token or \
               not _hmac_local.compare_digest(cookie_token, header_token):
                return JSONResponse(status_code=403,
                    content={"error": "CSRF token missing or invalid"})
    response = await call_next(request)
    # Setear cookie en cualquier respuesta que no sea exempt path (incluido
    # GETs autenticados, así el setup wizard recibe token desde el primer load).
    if not is_exempt_path:
        _bridge_ensure_csrf(request, response)
    return response

_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
)

@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP_POLICY
    return response

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
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> None:
    conn = db_conn()
    try:
        # ── Outbound stock worker ─────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              channel TEXT NOT NULL,
              item_count INTEGER NOT NULL,
              payload TEXT NOT NULL
            )""")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_events (
              event_id TEXT PRIMARY KEY,
              channel TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT,
              updated_at TEXT
            )""")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bridge_metrics (
              key TEXT PRIMARY KEY,
              value INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS known_skus (
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
              last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY(channel, sku)
            )""")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_items (
              event_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              qty INTEGER NOT NULL,
              derived_zero INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(event_id, channel, sku)
            )""")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS rejected_skus (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              event_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              reason TEXT NOT NULL
            )""")

        # ── SKU mapping (1 SKU → N listings post split-variantes MeLi) ────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sku_mapping (
              channel TEXT NOT NULL,
              sku TEXT NOT NULL,
              remote_item_id TEXT NOT NULL,
              remote_variation_id TEXT NOT NULL DEFAULT '',
              site TEXT DEFAULT '',
              last_seen_at TEXT,
              PRIMARY KEY (channel, remote_item_id, remote_variation_id)
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sku_mapping_sku ON sku_mapping(channel, sku)"
        )

        # ── S4: tables used by main.py but previously absent from init_db ─────

        # Configuration key-value store (read by every component)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bridge_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
        # Auto-update timestamp on insert/update
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_bridge_settings_updated_insert
            AFTER INSERT ON bridge_settings FOR EACH ROW
            BEGIN
              UPDATE bridge_settings
                SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
              WHERE key = NEW.key;
            END""")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_bridge_settings_updated_update
            AFTER UPDATE OF value ON bridge_settings FOR EACH ROW
            BEGIN
              UPDATE bridge_settings
                SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
              WHERE key = NEW.key;
            END""")

        # Amazon SKU → Odoo product mapping (manual overrides)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_sku_mapping (
              seller_sku TEXT PRIMARY KEY,
              odoo_product_id INTEGER,
              odoo_default_code TEXT,
              asin TEXT,
              parent_asin TEXT,
              keepa_domain INTEGER DEFAULT 11,
              notes TEXT,
              created_at TEXT DEFAULT (datetime('now'))
            )""")

        # Amazon FBA/FBM inventory cache (populated by amazon_prices_sync)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_inventory_cache (
              seller_sku TEXT NOT NULL,
              asin TEXT,
              product_name TEXT,
              qty INTEGER NOT NULL DEFAULT 0,
              marketplace TEXT NOT NULL DEFAULT 'MX',
              updated_at TEXT,
              PRIMARY KEY (seller_sku, marketplace)
            )""")

        # MeLi listings cache (populated by sync_meli_listings / backfill)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meli_listings_cache (
              item_id TEXT NOT NULL,
              variation_id TEXT NOT NULL,
              title TEXT,
              var_name TEXT,
              sku TEXT,
              qty INTEGER DEFAULT 0,
              price REAL,
              status TEXT DEFAULT 'active',
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              raw_json TEXT,
              PRIMARY KEY (item_id, variation_id)
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meli_listings_sku ON meli_listings_cache(sku)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meli_listings_updated ON meli_listings_cache(updated_at)"
        )

        # MeLi SKU → Odoo product mapping (manual overrides, parallel to amazon_sku_mapping)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meli_sku_mapping (
              seller_sku TEXT PRIMARY KEY,
              odoo_product_id INTEGER,
              odoo_default_code TEXT,
              notes TEXT,
              created_at TEXT DEFAULT (datetime('now'))
            )""")

        # MeLi raw inbound webhook events (audit trail before queueing)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              received_at TEXT NOT NULL,
              topic TEXT NOT NULL,
              resource TEXT NOT NULL,
              user_id TEXT,
              payload_json TEXT NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'queued',
              rawsha TEXT
            )""")

        # Amazon raw inbound order events (audit trail before queueing)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_inbound_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              received_at TEXT NOT NULL DEFAULT (datetime('now')),
              event_type TEXT NOT NULL,
              order_id TEXT NOT NULL,
              payload_json TEXT,
              dedupe_key TEXT UNIQUE,
              status TEXT DEFAULT 'pending'
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_amazon_events_order ON amazon_inbound_events(order_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_amazon_events_status ON amazon_inbound_events(status)"
        )

        # MeLi SKUs allowed for inbound processing (synced from sku_mapping)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_allowed_skus (
              sku TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 1,
              note TEXT,
              created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")

        # D1.8: idempotent schema migrations for columns added after initial deploy
        for _migration in [
            "ALTER TABLE inbound_events ADD COLUMN rawsha TEXT",
            "ALTER TABLE processed_inbound_events ADD COLUMN status TEXT DEFAULT 'done'",
        ]:
            try:
                conn.execute(_migration)
            except Exception:
                pass  # Column already exists

        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


def _save_tokens(data: dict):
    data["obtained_at"] = int(time.time())
    data["expires_at"] = data["obtained_at"] + int(data.get("expires_in", 0))
    # Persist app credentials alongside tokens so workers can do inline refresh
    # without needing a separate env var or bridge_settings key.
    data["client_id"] = MELI_CLIENT_ID
    data["client_secret"] = MELI_CLIENT_SECRET
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


# AP-3 — OAuth state CSRF protection.
# Sin parámetro `state`, un atacante puede hacer landing en /oauth/callback con
# un `code` de su propia cuenta para que el bridge lo canjee y guarde tokens del
# atacante. Generamos un state único por start, lo persistimos en redis con TTL
# 10 min, y validamos en callback.
_OAUTH_STATE_TTL_SECONDS = 600


@app.get("/oauth/start")
def meli_oauth_start():
    import secrets as _secrets
    state = _secrets.token_urlsafe(32)
    try:
        r.setex(f"meli_oauth_state:{state}", _OAUTH_STATE_TTL_SECONDS, "1")
    except Exception as e:
        logger.error(f"OAuth state redis save failed: {e}")
        return JSONResponse({"error": "oauth_state_store_failed"}, status_code=500)
    params = {
        "response_type": "code",
        "client_id": MELI_CLIENT_ID,
        "redirect_uri": MELI_REDIRECT_URI,
        # IMPORTANTE: pide refresh token (offline access)
        "scope": "offline_access",
        "state": state,
    }
    url = "https://auth.mercadolibre.com.mx/authorization?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@app.get("/oauth/callback")
def meli_oauth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "missing code"}, status_code=400)

    # Si el navegador/Cloudflare pega 2 veces el mismo callback, NO vuelvas a canjear el code.
    # Esto se evalúa ANTES del state-check porque el state ya fue consumido (getdel) en el
    # primer hit y un retry legítimo del mismo code no debe fallar como CSRF.
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

    # AP-3: validar state. GETDEL atómico evita replay del mismo state en otra sesión.
    state = request.query_params.get("state", "")
    if not state:
        return JSONResponse({"error": "missing_state"}, status_code=400)
    try:
        consumed = r.getdel(f"meli_oauth_state:{state}")
    except Exception as e:
        logger.error(f"OAuth state redis check failed: {e}")
        return JSONResponse({"error": "oauth_state_check_failed"}, status_code=500)
    if not consumed:
        # No coincide ningún state vivo: callback no iniciado por nosotros (CSRF) o expirado.
        return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)

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

    # D7.6: reject if MeLi didn't return refresh_token — a session without it
    # expires in 6h and can never auto-renew, causing mass order failures.
    if not data.get("refresh_token"):
        logger.error("OAuth callback: MeLi did not return refresh_token — rejecting non-refreshable session")
        return JSONResponse({
            "error": "missing_refresh_token",
            "message": (
                "MeLi no devolvió refresh_token. Ocurre cuando la app ya fue autorizada "
                "y MeLi no reemite el token. Solución: en https://myaccount.mercadolibre.com.mx/apps/authorized "
                "revoca el acceso a esta app y vuelve a autorizar vía /oauth/start."
            ),
        }, status_code=422)

    _save_tokens(data)
    # Clear reauth sentinel if one exists from a previous invalid_grant
    try:
        reauth_sentinel = TOKEN_FILE.replace(".meli_tokens.json", ".meli_reauth_required")
        if os.path.exists(reauth_sentinel):
            os.remove(reauth_sentinel)
    except Exception:
        pass

    # marca el code como ya consumido para que un refresh/doble carga no lo intente otra vez
    try:
        with open(last_code_file, "w") as f:
            f.write(code)
    except Exception:
        pass

    return {
        "ok": True,
        "message": "Tokens guardados correctamente en /data/.meli_tokens.json",
        "has_refresh_token": True,
        "expires_in": data.get("expires_in"),
        "user_id": data.get("user_id"),
    }


@app.post("/oauth/refresh", dependencies=[Depends(require_secret)])
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
    """Lee bridge_settings(key,value). Si no existe la tabla o falla, regresa default.

    AP-17/AP-30 fix: usar db_conn() (WAL + busy_timeout vía PRAGMA) en lugar
    de sqlite3.connect() raw. Bursts de webhooks colisionaban con writers
    (worker + reaper) y `try/except: return default` enmascaraba `database is
    locked` devolviendo "0" → gates evaluados como off → webhook silenciosamente
    ignorado. Plus: log explícito en error en vez de tragar la excepción.
    """
    con = None
    try:
        con = db_conn()
        cur = con.execute("SELECT value FROM bridge_settings WHERE key=? LIMIT 1", (key,))
        row = cur.fetchone()
        if row and row[0] is not None:
            return str(row[0])
        return default
    except Exception as e:
        logger.warning(f"_get_setting({key!r}) failed: {e!r} → returning default {default!r}")
        return default
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


import hmac as _hmac

# AP-11 — SubscribeURL allowlist. SNS publica SOLO en sns.<region>.amazonaws.com.
# Bloqueamos cualquier intento de redirigir GET hacia otro host (SSRF interno).
_SNS_HOST_RE = re.compile(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com/", re.IGNORECASE)


def _is_valid_sns_subscribe_url(url: str) -> bool:
    return bool(_SNS_HOST_RE.match(url or ""))


def _check_webhook_secret(
    expected: str,
    header_secret: Optional[str],
    query_secret: Optional[str],
    path_secret: Optional[str],
) -> bool:
    """AP-6: comparación con hmac.compare_digest para evitar timing oracle.

    Acepta el secret por header (preferido), querystring o path (compat con
    suscripciones MeLi/SNS vivas). El día que se rote el secret y se actualicen
    las URLs en los paneles upstream, el path/query support puede retirarse (AP-5).
    """
    if not expected:
        return False
    expected_b = expected.encode("utf-8")
    for candidate in (header_secret, query_secret, path_secret):
        if not candidate:
            continue
        try:
            if _hmac.compare_digest(candidate.encode("utf-8"), expected_b):
                return True
        except Exception:
            continue
    return False


# AP-12 — SNS message signature verification
# https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
_SNS_CERT_CACHE: dict = {}  # url -> (loaded_cert, expires_at_epoch)
_SNS_CERT_TTL_SECONDS = 24 * 3600

# Campos canónicos por tipo de mensaje (orden alfabético, exactamente como
# AWS los firma). Subject solo se incluye si está presente en Notification.
_SNS_FIELDS_NOTIFICATION = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SNS_FIELDS_SUBSCRIPTION = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")


def _sns_canonical_string(payload: dict) -> Optional[bytes]:
    msg_type = payload.get("Type", "")
    if msg_type == "Notification":
        fields = _SNS_FIELDS_NOTIFICATION
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        fields = _SNS_FIELDS_SUBSCRIPTION
    else:
        return None
    parts = []
    for f in fields:
        if f == "Subject" and "Subject" not in payload:
            continue
        if f not in payload:
            return None  # firma no verificable: campo requerido ausente
        parts.append(f)
        parts.append(str(payload[f]))
    return ("\n".join(parts) + "\n").encode("utf-8")


def _sns_load_cert(cert_url: str):
    """Descarga + cachea el cert PKI de AWS. fail-closed si URL fuera de allowlist."""
    if not _is_valid_sns_subscribe_url(cert_url):
        # SigningCertURL DEBE ser un endpoint sns.<region>.amazonaws.com (mismo allowlist).
        return None
    now = time.time()
    cached = _SNS_CERT_CACHE.get(cert_url)
    if cached and cached[1] > now:
        return cached[0]
    try:
        resp = requests.get(cert_url, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"SNS cert download HTTP {resp.status_code} for {cert_url}")
            return None
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_pem_x509_certificate(resp.content, default_backend())
        _SNS_CERT_CACHE[cert_url] = (cert, now + _SNS_CERT_TTL_SECONDS)
        return cert
    except Exception as e:
        logger.warning(f"SNS cert load failed: {e}")
        return None


def _verify_sns_signature(payload: dict) -> bool:
    """Verifica firma RSA del SNS message (v1=SHA1, v2=SHA256). fail-closed."""
    try:
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        sig_b64 = payload.get("Signature")
        cert_url = payload.get("SigningCertURL") or payload.get("SigningCertUrl")
        sig_ver = str(payload.get("SignatureVersion", ""))
        if not sig_b64 or not cert_url:
            return False
        if sig_ver == "1":
            logger.warning("SNS signature version 1 (SHA1) received — configure topic to use SignatureVersion=2 (SHA256)")
            hash_algo = hashes.SHA1()
        elif sig_ver == "2":
            hash_algo = hashes.SHA256()
        else:
            return False
        canonical = _sns_canonical_string(payload)
        if canonical is None:
            return False
        cert = _sns_load_cert(cert_url)
        if cert is None:
            return False
        try:
            signature = base64.b64decode(sig_b64)
        except Exception:
            return False
        public_key = cert.public_key()
        public_key.verify(signature, canonical, padding.PKCS1v15(), hash_algo)
        return True
    except Exception as e:
        logger.warning(f"SNS signature verification failed: {e}")
        return False


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

    # Secret validation. Aceptamos HEADER (preferido), QUERYSTRING o PATH por
    # compatibilidad con suscripciones MeLi vivas registradas con el path-secret.
    # Comparamos con hmac.compare_digest para evitar timing oracle (AP-6).
    # NO loggeamos el secret-path/query — riesgo de leak en logs nginx/cloudflare (AP-5).
    expected = _get_setting("meli_webhook_secret", "")
    qsecret = request.query_params.get("secret")
    psecret = secret

    if not expected:
        return JSONResponse(
            {"ok": False, "error": "secret_not_configured"},
            status_code=500,
        )

    if not _check_webhook_secret(expected, x_goncloud_secret, qsecret, psecret):
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

    # Insert inbound_events (auditoría). AP-15: rowcount==0 significa que el
    # INSERT OR IGNORE detectó duplicado (mismo dedupe_key) → NO encolar otro
    # job al worker para evitar fetch redundante del order detail.
    # AP-17: usar db_conn() (WAL + busy_timeout) en lugar de sqlite3.connect raw
    # — bursts de webhooks colisionaban con writers internos.
    insert_ignored = False
    try:
        con = db_conn()
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
        insert_ignored = (cur.rowcount == 0)
        con.commit()
        con.close()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"db_insert_failed: {e}"},
            status_code=500,
        )

    if insert_ignored:
        return JSONResponse(
            {"ok": True, "queued": False, "duplicate": True},
            status_code=200,
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
    checks = {}
    ok = True

    # DB check
    try:
        with closing(db_conn()) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"
        ok = False

    # DB size check (D5.6) — warn at 4GB, error at 8GB
    try:
        db_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        db_mb = round(db_bytes / 1024 / 1024, 1)
        checks["db_size_mb"] = db_mb
        if db_bytes > 8 * 1024 ** 3:
            checks["db_size_warn"] = "exceeds_8gb"
            ok = False
        elif db_bytes > 4 * 1024 ** 3:
            checks["db_size_warn"] = "exceeds_4gb"
    except Exception as e:
        checks["db_size_mb"] = f"error: {e}"

    # Redis check
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://bridge-redis:6379/0"), socket_connect_timeout=2)
        _r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
        ok = False

    # D7.3: surface MeLi reauth requirement when inline refresh detected invalid_grant
    try:
        reauth_sentinel = TOKEN_FILE.replace(".meli_tokens.json", ".meli_reauth_required")
        if os.path.exists(reauth_sentinel):
            with open(reauth_sentinel) as _f:
                checks["meli_reauth_required"] = _f.read().strip()
            ok = False
    except Exception:
        pass

    status_code = 200 if ok else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"ok": ok, "time": utc_now_iso(), "checks": checks},
        status_code=status_code,
    )


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


@app.post("/v1/stock/snapshot", dependencies=[Depends(require_secret)])
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
    if not _check_webhook_secret(expected, x_goncloud_secret, qsecret, psecret):
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
    # AP-12: validar firma SNS antes de procesar/responder cualquier acción.
    # Sin firma válida no podemos confiar en SubscribeURL ni en Message body
    # — el shared-secret protege ENTRE Cloudflare y bridge, pero no ATA al
    # publisher SNS de Amazon.
    if msg_type in ("Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"):
        if not _verify_sns_signature(data if isinstance(data, dict) else {}):
            logger.warning(f"Rejected SNS message: signature verification failed (type={msg_type})")
            return JSONResponse(
                {"ok": False, "error": "invalid_sns_signature"},
                status_code=401,
            )
    if msg_type == "SubscriptionConfirmation":
        subscribe_url = data.get("SubscribeURL")
        if not subscribe_url or not _is_valid_sns_subscribe_url(subscribe_url):
            # AP-11: SSRF guard. SNS SubscribeURL DEBE ser https://sns.<region>.amazonaws.com/...
            logger.warning("Rejected SNS SubscribeURL (failed allowlist regex)")
            return JSONResponse(
                {"ok": False, "error": "invalid_subscribe_url"},
                status_code=400,
            )
        import httpx
        try:
            httpx.get(subscribe_url, timeout=10)
        except Exception as e:
            logger.error(f"SNS SubscribeURL GET failed: {e}")
            return JSONResponse(
                {"ok": False, "error": "subscribe_get_failed"},
                status_code=502,
            )
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
    elif order_id and _mp:
        # AP-13: si tenemos order_id+marketplace pero no status, usar key
        # determinística basada en (mp, order_id) en lugar de sha[:16] que
        # cambia con cada timestamp/MessageId del envelope SNS y produce
        # falsos negativos de dedupe. Status vacío al final coincide con el
        # fallback del worker (Bug #7) → mismo espacio de claves.
        dedupe_key = f"amz:{_mp}:{order_id}:"
    else:
        # Sin order_id ni marketplace: último recurso, sha del payload.
        dedupe_key = f"amz-webhook:{sha[:16]}"
    # AP-17: usar db_conn() (WAL + busy_timeout) en lugar de sqlite3.connect raw.
    try:
        con = db_conn()
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO amazon_inbound_events (received_at, event_type, order_id, payload_json, dedupe_key, status) VALUES (?, ?, ?, ?, ?, 'pending')", (received_at, event_type, order_id, raw_text, dedupe_key))
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"[amazon_webhook] DB error: {e}")
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

@app.get("/v1/settings", dependencies=[Depends(require_secret)])
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


@app.get("/mapper", response_class=HTMLResponse, dependencies=[Depends(require_secret)])
async def sku_mapper_ui():
    html = open("/app/sku_mapper.html").read()
    return HTMLResponse(content=html)

@app.get("/amazon/mapper", response_class=HTMLResponse, dependencies=[Depends(require_secret)])
async def amazon_mapper_ui():
    html = open("/app/amazon_mapper.html").read()
    return HTMLResponse(content=html)

@app.get("/api/amazon/odoo-skus", dependencies=[Depends(require_secret)])
async def get_odoo_skus():
    import xmlrpc.client
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.environ["ODOO_PASSWORD"]  # OP-2: KeyError si falta — sin fallback hardcoded
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

@app.get("/api/amazon/amazon-skus", dependencies=[Depends(require_secret)])
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
        pwd = os.environ["ODOO_PASSWORD"]  # OP-2: KeyError si falta — sin fallback hardcoded
        
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

@app.post("/api/amazon/refresh-inventory", dependencies=[Depends(require_secret)])
async def refresh_amazon_inventory():
    """Refresh Amazon inventory cache from Listings Items API (FBA + FBM).

    AP-22: lock SETNX en redis con TTL=10min para evitar 2 calls concurrentes
    que ambos hagan DELETE+INSERT y consuman doble rate limit SP-API.
    """
    lock_key = "refresh_amazon_inventory_lock"
    if not r.set(lock_key, "1", nx=True, ex=600):
        return {"ok": False, "error": "refresh_in_progress"}
    try:
        return await _refresh_amazon_inventory_locked()
    finally:
        try:
            r.delete(lock_key)
        except Exception:
            pass


async def _refresh_amazon_inventory_locked():
    """Cuerpo real del refresh — sin la lógica de lock."""
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
        # AP-20: swap atómico via tabla staging.
        # Antes: DELETE + INSERT en la MISMA tabla expone una ventana donde
        # consumidores concurrentes (ej. /api/amazon/amazon-skus) ven 0 filas.
        # Plus: si la SP-API devuelve lista vacía por glitch, NO sobrescribimos
        # el cache vivo. Requiere len(skus) > 0 para hacer el swap.
        if not skus:
            conn.close()
            logger.warning("Amazon listings API returned 0 items — keeping previous cache")
            return {"ok": False, "error": "empty_listings_response"}

        conn.execute(
            "CREATE TABLE IF NOT EXISTS amazon_inventory_cache_staging "
            "(seller_sku TEXT, asin TEXT, product_name TEXT, qty INTEGER, updated_at TEXT)"
        )
        conn.execute("DELETE FROM amazon_inventory_cache_staging")
        conn.executemany(
            "INSERT INTO amazon_inventory_cache_staging (seller_sku, asin, product_name, qty, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            skus,
        )
        # Swap atómico — todo dentro de una transacción explícita.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM amazon_inventory_cache")
            conn.execute(
                "INSERT INTO amazon_inventory_cache (seller_sku, asin, product_name, qty, updated_at) "
                "SELECT seller_sku, asin, product_name, qty, updated_at FROM amazon_inventory_cache_staging"
            )
            conn.execute("DELETE FROM amazon_inventory_cache_staging")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        conn.close()
        logger.info(f"Amazon inventory cache updated: {len(skus)} listings (atomic swap)")
        return {"ok": True, "count": len(skus)}
    except Exception as e:
        conn.close()
        logger.error(f"Error refreshing Amazon inventory: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/api/amazon/mappings", dependencies=[Depends(require_secret)])
async def get_mappings():
    import xmlrpc.client
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT seller_sku, odoo_default_code, notes, created_at FROM amazon_sku_mapping").fetchall()
    conn.close()
    
    # Check sell_on_amazon_fbm in Odoo
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.environ["ODOO_PASSWORD"]  # OP-2: KeyError si falta — sin fallback hardcoded
    
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

@app.post("/api/amazon/mappings", dependencies=[Depends(require_secret)])
async def save_mapping(request: Request):
    data = await request.json()
    odoo_sku = data.get("odoo_sku", "").strip()
    amazon_sku = data.get("amazon_sku", "").strip()
    confirm_overwrite = bool(data.get("confirm_overwrite", False))
    if not odoo_sku or not amazon_sku:
        return {"ok": False, "error": "SKUs requeridos"}

    # AP-23: si el seller_sku ya está mapeado a OTRO odoo_sku, requerir confirm
    # explícito para evitar reasignaciones silenciadas por INSERT OR REPLACE.
    # El UI debe re-llamar con confirm_overwrite=true tras mostrar diff al user.
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT odoo_default_code FROM amazon_sku_mapping WHERE seller_sku=?",
        (amazon_sku,),
    ).fetchone()
    if existing and existing[0] and existing[0] != odoo_sku and not confirm_overwrite:
        conn.close()
        return {
            "ok": False,
            "error": "overwrite_required",
            "current_odoo_sku": existing[0],
            "requested_odoo_sku": odoo_sku,
            "hint": "Re-enviar con confirm_overwrite=true para reemplazar.",
        }
    conn.execute(
        "INSERT OR REPLACE INTO amazon_sku_mapping (seller_sku, odoo_default_code, notes, created_at) "
        "VALUES (?, ?, 'Web UI', datetime('now'))",
        (amazon_sku, odoo_sku),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "overwrote": bool(existing and existing[0] and existing[0] != odoo_sku)}

@app.delete("/api/amazon/mappings/{odoo_sku}", dependencies=[Depends(require_secret)])
async def delete_mapping(odoo_sku: str, seller_sku: str = ""):
    """AP-24: borrar 1 mapping específico, no todos los que comparten odoo_sku.
    Un mismo odoo_default_code puede mapear a múltiples seller_sku (FBA-XXX,
    FBM-XXX, marketplace-prefix, etc). El UI ahora envía ambos para precisión.
    Backward-compat: si seller_sku no llega, conservamos el comportamiento
    histórico (delete-all by odoo_sku) — el wizard viejo y posibles scripts
    pueden depender de ello.
    """
    conn = sqlite3.connect(DB_PATH)
    if seller_sku:
        cur = conn.execute(
            "DELETE FROM amazon_sku_mapping WHERE odoo_default_code = ? AND seller_sku = ?",
            (odoo_sku, seller_sku),
        )
    else:
        cur = conn.execute(
            "DELETE FROM amazon_sku_mapping WHERE odoo_default_code = ?",
            (odoo_sku,),
        )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": deleted}

# ═══════════════════════════════════════════════════════════════════
# MELI SKU MAPPER ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/meli/odoo-skus", dependencies=[Depends(require_secret)])
async def get_meli_odoo_skus():
    import xmlrpc.client
    url = os.getenv("ODOO_URL", "http://odoo-odoo-1:8069")
    db = os.getenv("ODOO_DB", "EHV")
    user = os.getenv("ODOO_USER", "ehventasmx@gmail.com")
    pwd = os.environ["ODOO_PASSWORD"]  # OP-2: KeyError si falta — sin fallback hardcoded
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

@app.get("/api/meli/meli-skus", dependencies=[Depends(require_secret)])
async def get_meli_skus():
    """Get SKUs from MercadoLibre listings"""
    conn = sqlite3.connect(DB_PATH)
    # Get from sku_mapping (consolidated table)
    rows = conn.execute("SELECT DISTINCT sku FROM sku_mapping WHERE channel='meli' AND sku IS NOT NULL AND sku != ''").fetchall()
    skus = [{"sku": r[0], "name": "", "qty": 0} for r in rows]
    conn.close()
    return {"ok": True, "skus": skus}

@app.get("/api/meli/mappings", dependencies=[Depends(require_secret)])
async def get_meli_mappings():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT seller_sku, odoo_default_code, notes, created_at FROM meli_sku_mapping").fetchall()
    conn.close()
    return {"ok": True, "mappings": [{"meli_sku": r[0], "odoo_sku": r[1], "notes": r[2], "created_at": r[3]} for r in rows]}

@app.post("/api/meli/mappings", dependencies=[Depends(require_secret)])
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

@app.delete("/api/meli/mappings/{odoo_sku}", dependencies=[Depends(require_secret)])
async def delete_meli_mapping(odoo_sku: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM meli_sku_mapping WHERE odoo_default_code = ?", (odoo_sku,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/meli/listings", dependencies=[Depends(require_secret)])
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

@app.post("/api/meli/listings/refresh", dependencies=[Depends(require_secret)])
async def refresh_meli_listings():
    """Trigger manual refresh of MeLi listings cache"""
    import subprocess
    # F9.2: SETNX lock — prevent concurrent runs consuming double MeLi rate-limit.
    # TTL=600s acts as max expected runtime; no explicit release needed for Popen.
    lock_key = "bridge:lock:meli_listings_refresh"
    if not r.set(lock_key, "1", nx=True, ex=600):
        return {"ok": False, "message": "Refresh ya en progreso, espera unos minutos"}
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

@app.get("/api/meli/sku-mappings", dependencies=[Depends(require_secret)])
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
    pwd = os.environ["ODOO_PASSWORD"]  # OP-2: KeyError si falta — sin fallback hardcoded
    
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

@app.post("/api/meli/sku-mappings", dependencies=[Depends(require_secret)])
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

@app.delete("/api/meli/sku-mappings/{item_id}/{variation_id}", dependencies=[Depends(require_secret)])
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

@app.get("/setup", dependencies=[Depends(require_secret)])
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

@app.get("/setup/force", dependencies=[Depends(require_secret)])
async def setup_wizard_force():
    """AP-8: ex-secret hardcoded `goncloud2026` reemplazado por require_secret.
    Solo accesible con header X-Goncloud-Secret válido o tras Cloudflare Access.
    """
    return FileResponse("/app/static/setup.html")

@app.post("/setup/api/test-odoo", dependencies=[Depends(require_secret)])
async def test_odoo_connection(request: Request):
    """AP-9: valida credenciales de Odoo.
    Plus: URL allowlist contra env ODOO_URL para evitar SSRF (un atacante
    podría apuntar a un servidor XML-RPC interno y harvest credenciales).
    """
    data = await request.json()
    url = data.get("url", "").strip().rstrip("/")
    db = data.get("db", "").strip()
    user = data.get("user", "").strip()
    password = data.get("password", "")

    if not all([url, db, user, password]):
        return {"ok": False, "error": "Todos los campos son requeridos"}

    # F4.1: reject non-HTTP schemes regardless of env allowlist state.
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    # AP-9: URL DEBE coincidir con la que el operador ya configuró por env.
    # Esto evita que el wizard se use como SSRF probe contra hosts internos.
    expected_odoo_url = (os.getenv("ODOO_URL", "") or "").strip().rstrip("/")
    if expected_odoo_url and url != expected_odoo_url:
        return {
            "ok": False,
            "error": f"URL fuera de allowlist. Esperado: {expected_odoo_url}",
        }
    
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

@app.get("/setup/api/status", dependencies=[Depends(require_secret)])
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

@app.post("/setup/api/auto-map", dependencies=[Depends(require_secret)])
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
                # AP-29: timeout=20 en los 3 sitios para evitar que el wizard
                # de setup cuelgue indefinidamente cuando MeLi no responde
                # (combinado con Popen no-async desde HTTP request, bloqueaba
                # el worker FastAPI).
                resp = requests.get(
                    f"https://api.mercadolibre.com/users/{user_id}/items/search",
                    params={"status": "active", "limit": 100},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=20,
                )
                items = resp.json().get("results", [])

                for item_id in items:
                    # Get item details with variations
                    item_resp = requests.get(
                        f"https://api.mercadolibre.com/items/{item_id}",
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=20,
                    )
                    item = item_resp.json()

                    for var in item.get("variations", [{}]) or [{}]:
                        var_id = var.get("id", "")

                        # Get SKU from variation attributes (attributes[SELLER_SKU] is MeLi's current field)
                        sku = ""
                        if var_id:
                            var_resp = requests.get(
                                f"https://api.mercadolibre.com/items/{item_id}/variations/{var_id}",
                                headers={"Authorization": f"Bearer {access_token}"},
                                timeout=20,
                            )
                            var_data = var_resp.json()
                            for attr in var_data.get("attributes", []):
                                if attr.get("id") == "SELLER_SKU":
                                    sku = attr.get("value_name", "")
                                    break

                        if not sku:
                            # Fallback 1: attributes[SELLER_SKU] a nivel del listing (campo actual)
                            for attr in item.get("attributes", []) or []:
                                if attr.get("id") == "SELLER_SKU":
                                    sku = attr.get("value_name", "") or ""
                                    break

                        if not sku:
                            # Fallback 2: seller_custom_field (campo legacy, ultima opcion)
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
    
    # AP-21: Amazon auto-map deshabilitado.
    # La lógica original asumía `seller_sku == odoo_default_code` literal, lo
    # cual produce mappings silenciosamente erróneos cuando el operador usa
    # convenciones distintas (FBM-XXX vs XXX, prefix por marketplace, etc).
    # El UI de C5 hará match candidate-based con confirmación 1-a-1.
    if data.get("amazon"):
        result["amazon"] = {
            "mapped": 0,
            "pending": 0,
            "disabled": True,
            "reason": "auto-map Amazon deshabilitado (AP-21): match literal seller_sku=odoo_default_code "
                      "produce errores silenciosos. Usar /amazon/mapper para confirmar 1-a-1.",
        }

    conn.close()
    return result

@app.post("/setup/api/activate", dependencies=[Depends(require_secret)])
async def setup_activate(request: Request):
    """Guarda configuración y activa GONCLOUD.
    AP-10: idempotente — si setup_completed ya es '1', rechaza para evitar
    re-escritura accidental de credenciales / re-activación de flags por un
    POST repetido del wizard.
    """
    data = await request.json()

    conn = sqlite3.connect(DB_PATH)

    # AP-10: gate idempotencia. El wizard SOLO debe correr una vez por instalación.
    try:
        row = conn.execute(
            "SELECT value FROM bridge_settings WHERE key='setup_completed'"
        ).fetchone()
        if row and str(row[0]) == "1":
            conn.close()
            return JSONResponse(
                {
                    "ok": False,
                    "error": "setup_already_completed",
                    "hint": "Para re-configurar, borrar setup_completed de bridge_settings manualmente.",
                },
                status_code=409,
            )
    except Exception:
        pass

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
        # AP-33: NO persistir odoo_password en bridge_settings (SQLite sin
        # cifrado, dump del DB lo expone en plain text). Se lee de env
        # ODOO_PASSWORD; el wizard solo lo usa para el test de conexión y
        # debe quedar en .env por instalación. Si llega del wizard, ignoramos.
        if odoo.get("password"):
            logger.warning("odoo_password recibido en /setup/api/activate — NO persistido (debe estar en env ODOO_PASSWORD)")
        
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
