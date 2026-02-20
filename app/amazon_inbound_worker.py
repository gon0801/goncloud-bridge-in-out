#!/usr/bin/env python3
"""
GONCLOUD Amazon Inbound Worker v2.7 — "Polish Pack" (FULL)

Includes v2.6 fixes + optimizations:
✅ Reaper race-free delete (DELETE condicional + rowcount confirm)
✅ acquire_lock steal uses last_activity = COALESCE(heartbeat_at, claimed_at)
✅ Settings cache bounded + TTL purge (anti leak)
✅ Heartbeat updates heartbeat_at + extends Redis TTL key
✅ Graceful shutdown: signals _shutdown, cleans current job, waits threads briefly (cleaner)
✅ Metrics: skipped separado (no se mezcla con processed)
✅ SQLite indexes: composite + partial indexes for reaper branches
✅ Subprocess: kill process group on timeout
✅ Retry lock-busy: exponential backoff + jitter (reduce presión en colas grandes)

NOTAS:
- 1 job a la vez por proceso. Escala: N procesos (workers).
- DLQ sigue en Redis (cap 10k). Si quieres auditoría completa: se hace DLQ en SQLite (v3/tier).
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import hashlib
import threading
import signal
import random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import redis

# =========================
# CONFIG
# =========================
class Config:
    DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
    REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")

    QUEUE = "amazon_orders_jobs"
    DEAD_LETTER = "amazon_orders_dead"
    PROCESSING_PREFIX = "amazon:processing"

    LOCK_TIMEOUT = 600          # 10 min
    HEARTBEAT_INTERVAL = 30     # 30s
    REAPER_INTERVAL = 30
    METRICS_INTERVAL = 300
    MAX_RETRIES = 3
    MAX_DEFERRED = 5        # RC=2 retries antes de enviar a DLQ


    DLQ_MAX = 9999

    WORKER_ID = f"{os.getpid()}@{os.uname().nodename}"

    SETTINGS_TTL_SHORT = timedelta(seconds=5)
    SETTINGS_TTL_LONG = timedelta(seconds=30)
    SETTINGS_CACHE_MAX = 256
    SETTINGS_PURGE_EVERY = 64

    ODOO_URL: Optional[str] = None
    ODOO_DB: Optional[str] = None
    ODOO_USER: Optional[str] = None
    ODOO_PASS: Optional[str] = None


# =========================
# STATE GLOBAL
# =========================
db: Optional[Any] = None
current_job_dedupe_key: Optional[str] = None
current_job_processing_key: Optional[str] = None
job_lock = threading.Lock()

_shutdown = threading.Event()


# =========================
# DATABASE
# =========================
class Database:
    _instance: Optional["Database"] = None
    _lock = threading.Lock()

    def __new__(cls, path: str):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, path: str):
        if self._initialized:
            return
        self.path = path
        self._local = threading.local()
        self._writer_lock = threading.Lock()

        conn = self._create_connection()
        self._ensure_tables(conn)
        self._migrate_if_needed(conn)
        self._ensure_indexes(conn)
        conn.close()
        self._initialized = True

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=60, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.row_factory = sqlite3.Row
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._create_connection()
        return self._local.conn

    def execute(self, sql: str, params: tuple = (), use_writer_lock: bool = False):
        conn = self._get_conn()
        if use_writer_lock:
            with self._writer_lock:
                return self._execute_with_retry(conn, sql, params)
        return self._execute_with_retry(conn, sql, params)

    def _execute_with_retry(self, conn, sql, params, max_retries=5):
        for attempt in range(max_retries):
            try:
                return conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "busy" in str(e).lower() and attempt < max_retries - 1:
                    sleep_time = (0.1 * (2 ** attempt)) + (hash(str(params)) % 100 / 1000)
                    time.sleep(sleep_time)
                    continue
                raise

    def transaction(self):
        return Transaction(self._get_conn(), self._writer_lock)

    def _ensure_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_job_payloads (
                dedupe_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_processing_locks (
                dedupe_key TEXT PRIMARY KEY,
                claimed_at TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                heartbeat_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_processed_events (
                dedupe_key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL,
                result TEXT NOT NULL,
                detail_json TEXT,
                worker_id TEXT,
                processing_time_ms INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_orders_state (
                order_id TEXT PRIMARY KEY,
                marketplace TEXT,
                last_state TEXT,
                last_seen_at TEXT,
                fulfillment_channel TEXT,
                buyer_email TEXT,
                buyer_name TEXT,
                ship_city TEXT,
                ship_state TEXT,
                ship_country TEXT,
                first_seen_at TEXT DEFAULT (datetime('now'))
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_metrics (
                hour TEXT PRIMARY KEY,
                processed INTEGER DEFAULT 0,
                deferred INTEGER DEFAULT 0,
                manual_review INTEGER DEFAULT 0,
                dead INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                retries INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0
            )
        """)

    def _col_exists(self, conn: sqlite3.Connection, table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)

    def _migrate_if_needed(self, conn: sqlite3.Connection):
        if not self._col_exists(conn, "amazon_processing_locks", "heartbeat_at"):
            try:
                conn.execute("ALTER TABLE amazon_processing_locks ADD COLUMN heartbeat_at TEXT")
            except Exception:
                pass

        if not self._col_exists(conn, "amazon_metrics", "skipped"):
            try:
                conn.execute("ALTER TABLE amazon_metrics ADD COLUMN skipped INTEGER DEFAULT 0")
            except Exception:
                pass

    def _ensure_indexes(self, conn: sqlite3.Connection):
        # Base helpful indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_amz_processed_at ON amazon_processed_events(processed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_amz_payload_expires ON amazon_job_payloads(expires_at)")

        # Reaper query: (heartbeat_at IS NULL AND claimed_at < ?) OR (heartbeat_at < ?)
        # Composite helps second branch sometimes, but first branch benefits strongly from partial index.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_amz_locks_hb_claimed ON amazon_processing_locks(heartbeat_at, claimed_at)")

        # ✅ Partial indexes to avoid table scan on the IS NULL branch (big tables)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_amz_locks_claimed_hbnull
            ON amazon_processing_locks(claimed_at)
            WHERE heartbeat_at IS NULL
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_amz_locks_heartbeat_notnull
            ON amazon_processing_locks(heartbeat_at)
            WHERE heartbeat_at IS NOT NULL
        """)


class Transaction:
    def __init__(self, conn, writer_lock):
        self.conn = conn
        self.writer_lock = writer_lock

    def __enter__(self):
        self.writer_lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self.writer_lock.release()


# =========================
# LOGGING
# =========================
def log(msg: str, level: str = "INFO", extra: dict = None):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "worker": Config.WORKER_ID,
        "channel": "amazon",
        "msg": msg,
    }
    if extra:
        entry.update(extra)
    print(json.dumps(entry), flush=True)


# =========================
# SETTINGS (bounded cache)
# =========================
_settings_cache: Dict[str, Tuple[str, datetime, timedelta]] = {}
_settings_gets = 0

def _purge_settings_cache():
    now = datetime.now(timezone.utc)
    expired = []
    for k, (v, cached_at, ttl) in _settings_cache.items():
        if now - cached_at >= ttl:
            expired.append(k)
    for k in expired:
        _settings_cache.pop(k, None)

    if len(_settings_cache) > Config.SETTINGS_CACHE_MAX:
        items = sorted(_settings_cache.items(), key=lambda kv: kv[1][1])  # by cached_at
        for k, _ in items[: len(_settings_cache) - Config.SETTINGS_CACHE_MAX]:
            _settings_cache.pop(k, None)

def get_setting(key: str, default: str = "", use_short_ttl: bool = False) -> str:
    global _settings_gets
    ttl = Config.SETTINGS_TTL_SHORT if use_short_ttl else Config.SETTINGS_TTL_LONG

    _settings_gets += 1
    if _settings_gets % Config.SETTINGS_PURGE_EVERY == 0:
        _purge_settings_cache()

    try:
        if key in _settings_cache:
            value, cached_at, cached_ttl = _settings_cache[key]
            if datetime.now(timezone.utc) - cached_at < cached_ttl:
                return value

        row = db.execute("SELECT value FROM bridge_settings WHERE key=?", (key,)).fetchone()
        value = str(row[0]) if row and row[0] else default
        _settings_cache[key] = (value, datetime.now(timezone.utc), ttl)
        return value
    except Exception as e:
        log("Settings error", "ERROR", {"key": key, "error": str(e)})
        return default

def is_enabled(key: str) -> bool:
    return get_setting(key, "0", use_short_ttl=True) == "1"


# =========================
# INIT DB
# =========================
def init_db():
    global db
    db = Database(Config.DB_PATH)
    Config.ODOO_URL = get_setting("odoo_url", "http://odoo-odoo-1:8069")
    Config.ODOO_DB = get_setting("odoo_db", "")
    Config.ODOO_USER = get_setting("odoo_user", "")
    Config.ODOO_PASS = get_setting("odoo_password", "")


# =========================
# METRICS
# =========================
def record_metric(result_type: str):
    hour = datetime.now().strftime("%Y-%m-%d-%H")
    column_map = {
        "success": "processed",
        "deferred": "deferred",
        "manual_review": "manual_review",
        "dead": "dead",
        "error": "errors",
        "skipped": "skipped",
    }
    column = column_map.get(result_type, "processed")

    try:
        db.execute(f"""
            INSERT INTO amazon_metrics (hour, {column}) VALUES (?, 1)
            ON CONFLICT(hour) DO UPDATE SET {column} = {column} + 1
        """, (hour,))
    except Exception as e:
        log("Metric error", "ERROR", {"error": str(e)})


# =========================
# PAYLOAD
# =========================
def persist_payload(dedupe_key: str, job: dict) -> str:
    payload_json = json.dumps(job, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=7)).isoformat()

    db.execute("""
        INSERT INTO amazon_job_payloads (dedupe_key, payload_json, payload_hash, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(dedupe_key) DO UPDATE SET
            payload_json = CASE
                WHEN excluded.payload_hash != amazon_job_payloads.payload_hash
                THEN excluded.payload_json
                ELSE amazon_job_payloads.payload_json
            END,
            expires_at = excluded.expires_at
    """, (dedupe_key, payload_json, payload_hash, now.isoformat(), expires_at))
    return payload_hash

def get_payload(dedupe_key: str) -> Optional[Tuple[dict, str]]:
    try:
        row = db.execute(
            "SELECT payload_json, payload_hash FROM amazon_job_payloads WHERE dedupe_key=?",
            (dedupe_key,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"]), row["payload_hash"]
    except Exception as e:
        log("Payload retrieval failed", "ERROR", {"dedupe_key": dedupe_key, "error": str(e)})
        return None


# =========================
# LOCKS
# =========================
def acquire_lock(dedupe_key: str, payload_hash: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    stale_threshold = (datetime.now(timezone.utc) - timedelta(seconds=Config.LOCK_TIMEOUT)).isoformat()

    try:
        with db.transaction():
            cur = db.execute(
                """UPDATE amazon_processing_locks
                   SET claimed_at=?, worker_id=?, payload_hash=?, heartbeat_at=?
                   WHERE dedupe_key=?
                     AND COALESCE(heartbeat_at, claimed_at) < ?""",
                (now, Config.WORKER_ID, payload_hash, now, dedupe_key, stale_threshold),
            )
            if cur.rowcount == 1:
                log("Lock stolen", "DEBUG", {"dedupe_key": dedupe_key})
                return True

            try:
                db.execute(
                    """INSERT INTO amazon_processing_locks
                       (dedupe_key, claimed_at, worker_id, payload_hash, heartbeat_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (dedupe_key, now, Config.WORKER_ID, payload_hash, now),
                )
                log("Lock acquired", "DEBUG", {"dedupe_key": dedupe_key})
                return True
            except sqlite3.IntegrityError:
                log("Lock busy (race)", "DEBUG", {"dedupe_key": dedupe_key})
                return False

    except Exception as e:
        log("Lock acquisition error", "ERROR", {"dedupe_key": dedupe_key, "error": str(e)})
        return False

def release_lock(dedupe_key: str):
    try:
        db.execute(
            "DELETE FROM amazon_processing_locks WHERE dedupe_key=? AND worker_id=?",
            (dedupe_key, Config.WORKER_ID),
        )
    except Exception as e:
        log("Lock release error", "ERROR", {"dedupe_key": dedupe_key, "error": str(e)})

def update_heartbeat(dedupe_key: str):
    try:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE amazon_processing_locks SET heartbeat_at=? WHERE dedupe_key=? AND worker_id=?",
            (now, dedupe_key, Config.WORKER_ID),
        )
    except Exception as e:
        log("Heartbeat update error", "ERROR", {"dedupe_key": dedupe_key, "error": str(e)})

def is_already_completed(dedupe_key: str) -> bool:
    """Checks for terminal states only. 'deferred' is NOT terminal — it must be retried."""
    try:
        row = db.execute(
            """SELECT 1 FROM amazon_processed_events
               WHERE dedupe_key=? AND result IN ('success','manual_review','dead')""",
            (dedupe_key,),
        ).fetchone()
        return row is not None
    except Exception as e:
        log("Completion check error", "ERROR", {"dedupe_key": dedupe_key, "error": str(e)})
        return False

def mark_completed(dedupe_key: str, result: str, detail: dict, processing_time_ms: Optional[int] = None):
    try:
        with db.transaction():
            db.execute(
                """
                INSERT INTO amazon_processed_events
                    (dedupe_key, processed_at, result, detail_json, worker_id, processing_time_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    processed_at=excluded.processed_at,
                    result=excluded.result,
                    detail_json=excluded.detail_json,
                    worker_id=excluded.worker_id,
                    processing_time_ms=excluded.processing_time_ms
                """,
                (
                    dedupe_key,
                    datetime.now(timezone.utc).isoformat(),
                    result,
                    json.dumps(detail),
                    Config.WORKER_ID,
                    processing_time_ms,
                ),
            )
            db.execute("DELETE FROM amazon_processing_locks WHERE dedupe_key=?", (dedupe_key,))
        record_metric(result)
        log("Completed", "INFO", {"dedupe_key": dedupe_key, "result": result, "detail": detail})
    except Exception as e:
        log("CRITICAL: mark_completed failed", "CRITICAL", {"dedupe_key": dedupe_key, "error": str(e)})


# =========================
# REAPER (race-free delete)
# =========================
def reaper_loop(redis_client: redis.Redis):
    log("Reaper started (v2.7)", "INFO")

    while not _shutdown.is_set():
        try:
            time.sleep(Config.REAPER_INTERVAL)

            stale_threshold = (datetime.now(timezone.utc) - timedelta(seconds=Config.LOCK_TIMEOUT)).isoformat()

            candidates = db.execute(
                """SELECT dedupe_key, worker_id, payload_hash
                   FROM amazon_processing_locks
                   WHERE (heartbeat_at IS NULL AND claimed_at < ?)
                      OR (heartbeat_at IS NOT NULL AND heartbeat_at < ?)
                   LIMIT 100""",
                (stale_threshold, stale_threshold),
            ).fetchall()

            if not candidates:
                continue

            log("Found stale lock candidates", "WARN", {"count": len(candidates)})

            for row in candidates:
                if _shutdown.is_set():
                    break

                dedupe_key = row["dedupe_key"]
                old_worker = row["worker_id"]
                expected_hash = row["payload_hash"]

                try:
                    with db.transaction():
                        del_cur = db.execute(
                            """DELETE FROM amazon_processing_locks
                               WHERE dedupe_key=?
                                 AND (
                                   (heartbeat_at IS NULL AND claimed_at < ?)
                                   OR (heartbeat_at IS NOT NULL AND heartbeat_at < ?)
                                 )""",
                            (dedupe_key, stale_threshold, stale_threshold),
                        )

                        if del_cur.rowcount != 1:
                            log("Reaper: lock still alive (delete lost race)", "DEBUG", {
                                "dedupe_key": dedupe_key,
                                "rowcount": del_cur.rowcount,
                            })
                            continue

                        payload_data = get_payload(dedupe_key)
                        if payload_data:
                            job, stored_hash = payload_data
                            if stored_hash != expected_hash:
                                log("Hash mismatch on reap", "ERROR", {
                                    "dedupe_key": dedupe_key,
                                    "expected": expected_hash[:16],
                                    "stored": stored_hash[:16],
                                })
                                mark_completed(dedupe_key, "manual_review", {"reason": "payload_integrity_error"})
                                continue

                            reap_count = job.get("_reap_count", 0) + 1
                            job["_reap_count"] = reap_count
                            job["_reaped"] = True
                            job["_reaped_at"] = datetime.now(timezone.utc).isoformat()
                            job["_original_worker"] = old_worker

                            persist_payload(dedupe_key, job)
                            redis_client.rpush(Config.QUEUE, json.dumps(job))

                            log("Reaper: job requeued", "INFO", {
                                "dedupe_key": dedupe_key,
                                "reap_count": reap_count,
                                "original_worker": old_worker,
                            })

                            if reap_count >= 3:
                                log("Reaper: job reaped multiple times - possible issue", "WARN", {
                                    "dedupe_key": dedupe_key,
                                    "reap_count": reap_count,
                                })
                        else:
                            log("Reaper: payload lost", "ERROR", {"dedupe_key": dedupe_key, "original_worker": old_worker})
                            mark_completed(dedupe_key, "dead", {"reason": "payload_lost", "original_worker": old_worker})

                except Exception as e:
                    log("Reaper error", "ERROR", {"dedupe_key": dedupe_key, "error": str(e)})

        except Exception as e:
            log("Reaper fatal error", "ERROR", {"error": str(e)})
            time.sleep(5)


# =========================
# HEARTBEAT
# =========================
def heartbeat_thread():
    local_redis = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)

    while not _shutdown.is_set():
        time.sleep(Config.HEARTBEAT_INTERVAL)
        try:
            with job_lock:
                dedupe = current_job_dedupe_key
                pkey = current_job_processing_key

            if dedupe:
                update_heartbeat(dedupe)
                if pkey:
                    try:
                        local_redis.expire(pkey, Config.LOCK_TIMEOUT + 60)
                    except Exception as redis_err:
                        log("Heartbeat Redis TTL error (non-critical)", "DEBUG", {"error": str(redis_err)})
                log("Heartbeat sent", "DEBUG", {"dedupe_key": dedupe})

        except Exception as e:
            log("Heartbeat error", "ERROR", {"error": str(e)})


# =========================
# METRICS REPORTER
# =========================
def metrics_reporter(redis_client: redis.Redis):
    while not _shutdown.is_set():
        time.sleep(Config.METRICS_INTERVAL)
        try:
            locks = db.execute("SELECT COUNT(*) FROM amazon_processing_locks").fetchone()[0]
            payloads = db.execute("SELECT COUNT(*) FROM amazon_job_payloads").fetchone()[0]
            completed_1h = db.execute(
                "SELECT COUNT(*) FROM amazon_processed_events WHERE processed_at > datetime('now', '-1 hour')"
            ).fetchone()[0]
            q_len = redis_client.llen(Config.QUEUE)
            dlq_len = redis_client.llen(Config.DEAD_LETTER)

            log("Metrics snapshot", "INFO", {
                "db_locks": locks,
                "db_payloads": payloads,
                "completed_1h": completed_1h,
                "redis_queue": q_len,
                "redis_dlq": dlq_len,
            })
        except Exception as e:
            log("Metrics error", "ERROR", {"error": str(e)})


# =========================
# BUSINESS LOGIC
# =========================
def extract_buyer_info(order: dict) -> Dict[str, str]:
    buyer_info = order.get("BuyerInfo", {}) or {}
    shipping = order.get("ShippingAddress", {}) or {}
    return {
        "buyer_email": buyer_info.get("BuyerEmail", ""),
        "buyer_name": buyer_info.get("BuyerName", "") or shipping.get("Name", ""),
        "ship_city": shipping.get("City", ""),
        "ship_state": shipping.get("StateOrRegion", ""),
        "ship_country": shipping.get("CountryCode", ""),
    }

AMZ_MX_MARKETPLACE = "A1AM78C64UM0Y8"

# profile → (channel_type, label)
# channel_type "FBA" = Amazon ships (no picking), "FBM" = seller ships (with picking)
_PROFILE_MAP = {
    "FLEX_MX": ("FBM", "Amazon Flex MX"),
    "FBA_US":  ("FBA", "Amazon FBA US"),
    "FBA_MX":  ("FBA", "Amazon FBA MX"),
    "EASY_MX": ("FBM", "Amazon Easy MX"),
    "FBM_MX":  ("FBM", "Amazon FBM MX"),
    "FBM_US":  ("FBM", "Amazon FBM US"),
}

def detect_order_profile(order: Dict[str, Any]) -> Tuple[str, str, str]:
    """Returns (channel_type, label, profile) e.g. ('FBM', 'Amazon Flex MX', 'FLEX_MX')."""
    channel = (order.get("FulfillmentChannel", "") or "").upper()
    marketplace = (order.get("MarketplaceId", "") or "").upper()
    is_mx = marketplace == AMZ_MX_MARKETPLACE

    if channel == "AFN":
        profile = "FLEX_MX" if is_mx else "FBA_US"
    elif channel == "MFN":
        ess = order.get("EasyShipShipmentStatus")
        if ess is not None:
            profile = "EASY_MX"
        else:
            profile = "FBM_MX" if is_mx else "FBM_US"
    else:
        profile = "FBM_MX" if is_mx else "FBM_US"

    channel_type, label = _PROFILE_MAP[profile]
    return (channel_type, label, profile)


def detect_fulfillment_channel(order: Dict[str, Any]) -> Tuple[str, str]:
    """Legacy shim — use detect_order_profile() for new code."""
    channel_type, _label, _profile = detect_order_profile(order)
    return (channel_type, "order.FulfillmentChannel")

def map_order_status(status: str) -> Optional[str]:
    status = (status or "").strip()
    if status in ("Unshipped", "PartiallyShipped"):
        return "paid"
    if status == "Shipped":
        return "shipped"
    if status in ("Canceled", "Cancelled"):
        return "cancelled"
    if status == "Pending":
        return None
    return None


# =========================
# RUN TOOL (kill process group)
# =========================
def run_tool(tool_name: str, env_vars: dict) -> Tuple[int, str, str]:
    tool_paths = [
        f"/data/{tool_name}.py",
        f"/mnt/data/appdata/bridge/tools/{tool_name}.py",
        f"./tools/{tool_name}.py",
    ]
    tool_path = next((p for p in tool_paths if os.path.exists(p)), None)
    if not tool_path:
        return -1, "", f"Tool not found: {tool_name}"

    env = os.environ.copy()
    env.update({
        "ODOO_URL": Config.ODOO_URL or "",
        "ODOO_DB": Config.ODOO_DB or "",
        "ODOO_USER": Config.ODOO_USER or "",
        "ODOO_PASS": Config.ODOO_PASS or "",
    })
    env.update(env_vars)

    p = subprocess.Popen(
        ["python3", tool_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=180)
        rc = p.returncode if p.returncode is not None else 0
        return rc, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        return -2, out or "", "Timeout after 180s (killed process group)"
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        return -3, "", str(e)

def map_tool_result(rc: int, stdout: str, stderr: str) -> Tuple[str, dict]:
    if rc == 0:
        return "success", {"rc": rc, "stdout_preview": (stdout or "")[-200:]}
    if rc == 2:
        return "deferred", {"rc": rc, "reason": "deferred_by_tool", "stderr_preview": (stderr or "")[-200:]}
    return "manual_review", {"rc": rc, "error": (stderr or "")[-500:], "stdout_preview": (stdout or "")[-200:]}

def _finalize(dedupe_key: str, result: str, detail: dict, ms: int) -> bool:
    """Marks job as completed. Returns True if the job needs to be re-queued (deferred)."""
    if result == "deferred":
        # Deferred is NOT terminal: release lock and signal caller to re-queue.
        release_lock(dedupe_key)
        record_metric("deferred")
        log("Deferred: will requeue", "INFO", {"dedupe_key": dedupe_key, **detail})
        return True
    mark_completed(dedupe_key, result, detail, ms)
    return False


def process_fba(order: dict, order_id: str, marketplace: str, action: str, dedupe_key: str,
               channel_label: str = "Amazon FBA", buyer_name: str = "") -> bool:
    """Returns True if the job must be re-queued (deferred)."""
    ref = f"AMZFBA:{marketplace}:{order_id}"
    start = time.time()

    if action == "cancelled":
        if not is_enabled("amazon_inbound_fba_refunds_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "refunds_disabled"})
            return False
        rc, out, err = run_tool("amazon_fba_refund_and_cancel", {"CLIENT_ORDER_REF": ref})
        result, detail = map_tool_result(rc, out, err)
        ms = int((time.time() - start) * 1000)
        return _finalize(dedupe_key, result, {"ref": ref, **detail}, ms)

    if action in ("paid", "shipped"):
        if not is_enabled("amazon_inbound_fba_paid_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "paid_disabled"})
            return False
        rc, out, err = run_tool("amazon_fba_paid_one_shot", {
            "CLIENT_ORDER_REF": ref,
            "ORDER_JSON": json.dumps(order),
            "CHANNEL_LABEL": channel_label,
            "BUYER_NAME": buyer_name,
        })
        result, detail = map_tool_result(rc, out, err)
        ms = int((time.time() - start) * 1000)
        return _finalize(dedupe_key, result, {"ref": ref, **detail}, ms)

    return False


def process_fbm(order: dict, order_id: str, marketplace: str, action: str, dedupe_key: str,
               channel_label: str = "Amazon FBM", buyer_name: str = "") -> bool:
    """Returns True if the job must be re-queued (deferred)."""
    ref = f"AMZFBM:{marketplace}:{order_id}"
    start = time.time()

    if action == "cancelled":
        if not is_enabled("amazon_inbound_fbm_refunds_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "refunds_disabled"})
            return False
        rc, out, err = run_tool("amazon_fbm_refund_and_cancel", {"CLIENT_ORDER_REF": ref})
        result, detail = map_tool_result(rc, out, err)
        ms = int((time.time() - start) * 1000)
        return _finalize(dedupe_key, result, {"ref": ref, **detail}, ms)

    if action in ("paid", "shipped"):
        if not is_enabled("amazon_inbound_fbm_paid_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "paid_disabled"})
            return False
        rc, out, err = run_tool("amazon_fbm_paid_one_shot", {
            "CLIENT_ORDER_REF": ref,
            "ORDER_JSON": json.dumps(order),
            "CHANNEL_LABEL": channel_label,
            "BUYER_NAME": buyer_name,
        })
        result, detail = map_tool_result(rc, out, err)
        ms = int((time.time() - start) * 1000)
        return _finalize(dedupe_key, result, {"ref": ref, **detail}, ms)

    return False


# =========================
# MAIN
# =========================
def main():
    global current_job_dedupe_key, current_job_processing_key

    init_db()
    log("Worker started", "INFO", {"version": "2.7", "db": Config.DB_PATH, "queue": Config.QUEUE})

    redis_client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)
    try:
        redis_client.ping()
        log("Redis connected", "INFO")
    except Exception as e:
        log("Redis connection failed", "CRITICAL", {"error": str(e)})
        sys.exit(1)

    threads = []

    def graceful_exit(signum, frame):
        global current_job_dedupe_key, current_job_processing_key
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log("Shutdown signal received", "WARN", {"signal": sig_name})
        _shutdown.set()

        # Cleanup best-effort
        with job_lock:
            dk = current_job_dedupe_key
            pk = current_job_processing_key
            current_job_dedupe_key = None
            current_job_processing_key = None

        try:
            if pk:
                redis_client.delete(pk)
        except Exception:
            pass

        try:
            if dk:
                release_lock(dk)
        except Exception:
            pass

        # Wait a bit for threads to stop cleanly
        for t in threads:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass

        # Small grace for stdout flush + sqlite settle
        time.sleep(0.2)
        sys.exit(0)

    signal.signal(signal.SIGTERM, graceful_exit)
    signal.signal(signal.SIGINT, graceful_exit)

    # Start threads (non-daemon so we can join)
    t_reaper = threading.Thread(target=reaper_loop, args=(redis_client,), daemon=False)
    t_hb = threading.Thread(target=heartbeat_thread, daemon=False)
    t_metrics = threading.Thread(target=metrics_reporter, args=(redis_client,), daemon=False)

    t_reaper.start()
    t_hb.start()
    t_metrics.start()
    threads.extend([t_reaper, t_hb, t_metrics])

    while True:
        if _shutdown.is_set():
            return

        if not is_enabled("amazon_inbound_enabled"):
            time.sleep(2)
            continue

        item = None
        dedupe_key = None
        processing_key = None

        try:
            item = redis_client.brpop(Config.QUEUE, timeout=5)
            if not item:
                continue
            _, item_data = item

            try:
                job = json.loads(item_data)
            except json.JSONDecodeError:
                log("Invalid JSON", "ERROR", {"preview": str(item_data)[:200]})
                redis_client.lpush(Config.DEAD_LETTER, json.dumps({
                    "raw": str(item_data)[:1000],
                    "error": "json_decode_error",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }))
                redis_client.ltrim(Config.DEAD_LETTER, 0, Config.DLQ_MAX)
                continue

            dedupe_key = job.get("dedupe_key")
            if not dedupe_key:
                order_id = str(job.get("order_json", {}).get("AmazonOrderId", ""))
                marketplace = str(job.get("order_json", {}).get("MarketplaceId", "UNKNOWN"))
                dedupe_key = f"amz:{marketplace}:{order_id}" if order_id else f"legacy:{hashlib.sha256(item_data.encode()).hexdigest()[:16]}"
                job["dedupe_key"] = dedupe_key

            payload_hash = persist_payload(dedupe_key, job)

            if is_already_completed(dedupe_key):
                log("Dedupe: already completed", "DEBUG", {"dedupe_key": dedupe_key})
                continue

            if not acquire_lock(dedupe_key, payload_hash):
                # ✅ Exponential backoff + jitter on lock contention
                lock_busy = int(job.get("_lock_busy", 0)) + 1
                job["_lock_busy"] = lock_busy

                # backoff = 1,2,4,8,10.. with jitter
                base = min(2 ** min(lock_busy, 4), 10)  # cap ~10s
                jitter = random.uniform(0.0, 0.25)
                delay = min(base + jitter, 10.0)

                redis_client.rpush(Config.QUEUE, json.dumps(job))
                log("Lock busy: requeued", "DEBUG", {"dedupe_key": dedupe_key, "lock_busy": lock_busy, "sleep_s": round(delay, 3)})
                time.sleep(delay)
                continue

            processing_key = f"{Config.PROCESSING_PREFIX}:{dedupe_key}"
            redis_client.setex(
                processing_key,
                Config.LOCK_TIMEOUT + 60,
                json.dumps({"dedupe_key": dedupe_key, "worker": Config.WORKER_ID, "started_at": datetime.now(timezone.utc).isoformat()}),
            )

            with job_lock:
                current_job_dedupe_key = dedupe_key
                current_job_processing_key = processing_key

            order = job.get("order_json", {})
            if not isinstance(order, dict) or not order:
                mark_completed(dedupe_key, "manual_review", {"reason": "no_order_json"})
                redis_client.delete(processing_key)
                with job_lock:
                    current_job_dedupe_key = None
                    current_job_processing_key = None
                continue

            order_id = str(order.get("AmazonOrderId", "")).strip()
            if not order_id:
                mark_completed(dedupe_key, "manual_review", {"reason": "no_order_id"})
                redis_client.delete(processing_key)
                with job_lock:
                    current_job_dedupe_key = None
                    current_job_processing_key = None
                continue

            marketplace = str(order.get("MarketplaceId", "")).strip() or get_setting("amazon_marketplace_id", "A1AM78C64UM0Y8")
            status = str(order.get("OrderStatus", "")).strip()
            action = map_order_status(status)

            # Map Amazon legacy SellerSKU -> Odoo default_code (amazon_sku_mapping)
            try:
                items = order.get("OrderItems") or []
                for it in items:
                    raw_sku = (it.get("SellerSKU") or it.get("sku") or "").strip()
                    if not raw_sku:
                        continue
                    row = db.execute(
                        "SELECT odoo_default_code FROM amazon_sku_mapping WHERE seller_sku=? LIMIT 1",
                        (raw_sku,)
                    ).fetchone()
                    if row and row[0]:
                        mapped = str(row[0]).strip()
                        if mapped and mapped != raw_sku:
                            it["amazon_seller_sku_raw"] = raw_sku
                            it["SellerSKU"] = mapped
                            it["sku"] = mapped
                            order["amazon_sku_mapped"] = True
            except Exception:
                pass

            channel_type, channel_label, profile = detect_order_profile(order)

            # Flex MX: cualquier Pending activa el tool.
            # El tool crea SO + picking siempre; solo factura si ya hay precio.
            if profile == "FLEX_MX" and action is None and status == "Pending":
                action = "paid"

            buyer = extract_buyer_info(order)
            buyer_name = buyer["buyer_name"] or ""

            db.execute("""
                INSERT INTO amazon_orders_state
                    (order_id, marketplace, last_state, last_seen_at, fulfillment_channel,
                     buyer_email, buyer_name, ship_city, ship_state, ship_country)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    last_state=excluded.last_state,
                    last_seen_at=excluded.last_seen_at,
                    fulfillment_channel=excluded.fulfillment_channel,
                    buyer_email=COALESCE(excluded.buyer_email, buyer_email),
                    buyer_name=COALESCE(excluded.buyer_name, buyer_name),
                    ship_city=COALESCE(excluded.ship_city, ship_city),
                    ship_state=COALESCE(excluded.ship_state, ship_state),
                    ship_country=COALESCE(excluded.ship_country, ship_country)
            """, (
                order_id, marketplace, status, datetime.now(timezone.utc).isoformat(), profile,
                buyer["buyer_email"], buyer_name, buyer["ship_city"], buyer["ship_state"], buyer["ship_country"],
            ))

            log("State probe", "INFO", {
                "order_id": order_id,
                "marketplace": marketplace,
                "status": status,
                "action": action,
                "profile": profile,
                "label": channel_label,
                "buyer_name": buyer_name[:30] if buyer_name else "N/A",
                "ship_city": buyer["ship_city"] or "N/A",
            })

            if action is None:
                mark_completed(dedupe_key, "skipped", {"order_id": order_id, "status": status, "reason": "pending_or_unknown"})
                redis_client.delete(processing_key)
                with job_lock:
                    current_job_dedupe_key = None
                    current_job_processing_key = None
                continue

            if channel_type == "FBA":
                needs_requeue = process_fba(order, order_id, marketplace, action, dedupe_key, channel_label, buyer_name)
            else:
                needs_requeue = process_fbm(order, order_id, marketplace, action, dedupe_key, channel_label, buyer_name)

            if needs_requeue:
                deferred_count = int(job.get("_deferred_count", 0)) + 1
                job["_deferred_count"] = deferred_count
                if deferred_count >= Config.MAX_DEFERRED:
                    mark_completed(dedupe_key, "dead", {
                        "reason": "max_deferred_exceeded",
                        "deferred_count": deferred_count,
                    })
                    redis_client.lpush(Config.DEAD_LETTER, json.dumps({
                        "job": job,
                        "error": "max_deferred_exceeded",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }))
                    redis_client.ltrim(Config.DEAD_LETTER, 0, Config.DLQ_MAX)
                    log("Deferred: max retries exhausted → DLQ", "ERROR", {
                        "dedupe_key": dedupe_key,
                        "deferred_count": deferred_count,
                    })
                else:
                    redis_client.rpush(Config.QUEUE, json.dumps(job))
                    log("Deferred: requeued", "INFO", {
                        "dedupe_key": dedupe_key,
                        "deferred_count": deferred_count,
                    })

            redis_client.delete(processing_key)
            with job_lock:
                current_job_dedupe_key = None
                current_job_processing_key = None

        except Exception as e:
            log("Unhandled exception", "CRITICAL", {"error": str(e), "dedupe_key": dedupe_key})

            if dedupe_key:
                try:
                    job = json.loads(item_data) if item else {}
                    retry_count = int(job.get("_retry_count", 0))

                    if retry_count >= Config.MAX_RETRIES:
                        mark_completed(dedupe_key, "dead", {"reason": "unhandled_exception", "error": str(e)})
                        redis_client.lpush(Config.DEAD_LETTER, json.dumps({"job": job, "error": str(e)}))
                        redis_client.ltrim(Config.DEAD_LETTER, 0, Config.DLQ_MAX)
                    else:
                        job["_retry_count"] = retry_count + 1
                        release_lock(dedupe_key)
                        redis_client.rpush(Config.QUEUE, json.dumps(job))
                        # Metrics: retries (optional)
                        try:
                            hour = datetime.now().strftime("%Y-%m-%d-%H")
                            db.execute("""
                                INSERT INTO amazon_metrics (hour, retries) VALUES (?, 1)
                                ON CONFLICT(hour) DO UPDATE SET retries = retries + 1
                            """, (hour,))
                        except Exception:
                            pass

                    if processing_key:
                        redis_client.delete(processing_key)
                except Exception:
                    pass

            with job_lock:
                current_job_dedupe_key = None
                current_job_processing_key = None

            time.sleep(1)


if __name__ == "__main__":
    main()
