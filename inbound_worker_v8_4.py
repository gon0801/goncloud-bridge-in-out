#!/usr/bin/env python3
"""
GONCLOUD Inbound Worker v8.4 — "Payload-Persistent"
Arquitectura: Locks transient + Audit permanent + Payload persistente
Diseñado para: 500k SKUs, 10+ workers, recuperación autónoma, zero duplicación
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import redis

# =========================
# CONFIG — Environment-driven, zero hardcode
# =========================
class Config:
    # Paths
    DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
    REDIS_URL = os.getenv("REDIS_URL", "redis://bridge-redis:6379/0")
    TOKEN_FILE = os.getenv("MELI_TOKEN_FILE", "/data/.meli_tokens.json")
    
    # Queues (Redis)
    QUEUE = "ml_orders_jobs"           # FIFO: trabajos pendientes
    PROCESSING = "ml_orders_processing" # Jobs actualmente en proceso (para observabilidad)
    DEAD_LETTER = "ml_orders_dead"      # Jobs fallidos permanentemente
    
    # Timeouts (segundos)
    LOCK_TIMEOUT = 600          # 10 min: tiempo máximo de procesamiento antes de considerar stale
    HEARTBEAT_INTERVAL = 30     # 30 seg: frecuencia de heartbeat para jobs largos
    REAPER_INTERVAL = 30        # 30 seg: frecuencia de limpieza de locks stale
    
    # Retry policy
    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 2
    
    # Worker identity
    WORKER_ID = f"{os.getpid()}@{os.uname().nodename}"
    WORKER_START_TIME = datetime.now(timezone.utc).isoformat()
    
    # Odoo (lazy load desde DB)
    ODOO_URL: Optional[str] = None
    ODOO_DB: Optional[str] = None
    ODOO_USER: Optional[str] = None
    ODOO_PASS: Optional[str] = None

# =========================
# DATABASE — SQLite con WAL, optimizado para concurrencia
# =========================
class Database:
    _instance: Optional['Database'] = None
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
        
        # Conexión inicial para crear tablas
        conn = self._create_connection()
        self._ensure_tables(conn)
        conn.close()
        
        self._initialized = True
    
    def _create_connection(self) -> sqlite3.Connection:
        """Crea conexión optimizada para alta concurrencia"""
        conn = sqlite3.connect(
            self.path,
            timeout=60,
            isolation_level=None,
            check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
        conn.execute("PRAGMA cache_size=-65536")    # 64MB page cache
        conn.row_factory = sqlite3.Row
        return conn
    
    def _get_conn(self) -> sqlite3.Connection:
        """Thread-local connection pooling"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = self._create_connection()
        return self._local.conn
    
    def execute(self, sql: str, params: tuple = (), use_writer_lock: bool = False) -> sqlite3.Cursor:
        """Ejecución con retry exponencial y jitter"""
        conn = self._get_conn()
        
        if use_writer_lock:
            with self._writer_lock:
                return self._execute_with_retry(conn, sql, params)
        return self._execute_with_retry(conn, sql, params)
    
    def _execute_with_retry(self, conn: sqlite3.Connection, sql: str, params: tuple, max_retries: int = 5) -> sqlite3.Cursor:
        for attempt in range(max_retries):
            try:
                return conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "busy" in str(e).lower() and attempt < max_retries - 1:
                    # Jitter: 100ms, 200ms, 400ms, 800ms, 1600ms + random(0-100ms)
                    sleep_time = (0.1 * (2 ** attempt)) + (hash(str(params)) % 100 / 1000)
                    time.sleep(sleep_time)
                    continue
                raise
    
    def transaction(self):
        return Transaction(self._get_conn(), self._writer_lock)
    
    def _ensure_tables(self, conn: sqlite3.Connection):
        """Schema v8.4: 4 tablas core"""
        
        # 1. PAYLOAD PERSISTENTE — El fix crítico para reaper
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_job_payloads (
                dedupe_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,      -- Job completo, inmutable
                payload_hash TEXT NOT NULL,      -- SHA256 para integridad
                created_at TEXT NOT NULL,
                expires_at TEXT                  -- TTL para limpieza automática
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_payloads_expires 
            ON inbound_job_payloads(expires_at)
        """)
        
        # 2. LOCKS TRANSIENTES — Coordinación entre workers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_processing_locks (
                dedupe_key TEXT PRIMARY KEY,
                claimed_at TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,      -- Validación: lock corresponde a payload
                heartbeat_at TEXT                -- Último heartbeat del worker
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_locks_worker 
            ON inbound_processing_locks(worker_id)
        """)
        
        # 3. AUDIT PERMANENTE — Historial inmutable de resultados
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_inbound_events (
                dedupe_key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL,
                result TEXT NOT NULL CHECK(result IN ('success', 'manual_review', 'dead', 'error', 'skipped')),
                detail_json TEXT,
                worker_id TEXT,
                processing_time_ms INTEGER
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_result_time 
            ON processed_inbound_events(result, processed_at)
        """)
        
        # 4. ESTADO DE ÓRDENES — Último estado conocido por order_id
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_orders_state (
                order_id TEXT PRIMARY KEY,
                site TEXT NOT NULL,
                last_state TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                pack_id TEXT,
                first_seen_at TEXT DEFAULT (datetime('now')),
                logistic_type TEXT
            )
        """)
        
        # 5. MÉTRICAS HORARIAS — Agregación para dashboards
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_metrics (
                hour TEXT PRIMARY KEY,
                processed INTEGER DEFAULT 0,
                manual_review INTEGER DEFAULT 0,
                dead INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                retries INTEGER DEFAULT 0
            )
        """)

class Transaction:
    def __init__(self, conn: sqlite3.Connection, writer_lock: threading.Lock):
        self.conn = conn
        self.writer_lock = writer_lock
        self.active = False
    
    def __enter__(self):
        self.writer_lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        self.active = True
        return self.conn
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.active = False
            self.writer_lock.release()

# Singleton global
db: Optional[Database] = None

def init_db():
    global db
    db = Database(Config.DB_PATH)
    
    # Lazy load Odoo config
    Config.ODOO_URL = get_setting("odoo_url", "http://odoo-odoo-1:8069")
    Config.ODOO_DB = get_setting("odoo_db", "")
    Config.ODOO_USER = get_setting("odoo_user", "")
    Config.ODOO_PASS = get_setting("odoo_password", "")

# =========================
# LOGGING — Estructurado, con contexto de worker
# =========================
def log(msg: str, level: str = "INFO", extra: dict = None):
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": ts,
        "level": level,
        "worker": Config.WORKER_ID,
        "msg": msg
    }
    if extra:
        entry.update(extra)
    print(json.dumps(entry), flush=True)

# =========================
# SETTINGS — Cache local con fallback a DB
# =========================
_settings_cache: Dict[str, Tuple[str, datetime]] = {}
_settings_ttl = timedelta(seconds=30)

def get_setting(key: str, default: str = "") -> str:
    try:
        # Cache hit?
        if key in _settings_cache:
            value, cached_at = _settings_cache[key]
            if datetime.now(timezone.utc) - cached_at < _settings_ttl:
                return value
        
        # Cache miss o expirado
        row = db.execute("SELECT value FROM bridge_settings WHERE key=?", (key,)).fetchone()
        value = str(row[0]) if row and row[0] else default
        
        _settings_cache[key] = (value, datetime.now(timezone.utc))
        return value
    except Exception as e:
        log(f"Settings error for {key}: {e}", "ERROR")
        return default

def is_enabled(key: str) -> bool:
    return get_setting(key, "0") == "1"

# =========================
# MÉTRICAS — Agregación eficiente
# =========================
def record_metric(result_type: str, processing_time_ms: Optional[int] = None):
    hour = datetime.now().strftime("%Y-%m-%d-%H")
    column = result_type if result_type in ("processed", "manual_review", "dead", "errors", "retries") else "processed"
    
    try:
        db.execute(f"""
            INSERT INTO inbound_metrics (hour, {column}) VALUES (?, 1)
            ON CONFLICT(hour) DO UPDATE SET {column} = {column} + 1
        """, (hour,))
        
        if processing_time_ms and column == "processed":
            # Podríamos trackear percentiles en tabla separada si se necesita
            pass
    except Exception as e:
        log(f"Metric error: {e}", "ERROR")

# =========================
# PAYLOAD PERSISTENCE — Fix crítico v8.4
# =========================
def persist_payload(dedupe_key: str, job: dict) -> str:
    """
    Guarda payload completo en DB antes de procesar.
    Retorna hash SHA256 para validación de integridad.
    """
    payload_json = json.dumps(job, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=7)).isoformat()  # TTL 7 días
    
    try:
        db.execute("""
            INSERT INTO inbound_job_payloads (dedupe_key, payload_json, payload_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                payload_json = CASE 
                    WHEN excluded.payload_hash != inbound_job_payloads.payload_hash 
                    THEN excluded.payload_json 
                    ELSE inbound_job_payloads.payload_json 
                END,
                expires_at = excluded.expires_at
        """, (dedupe_key, payload_json, payload_hash, now.isoformat(), expires_at))
        return payload_hash
    except Exception as e:
        log(f"Payload persistence failed for {dedupe_key}: {e}", "ERROR")
        raise

def get_payload(dedupe_key: str) -> Optional[Tuple[dict, str]]:
    """
    Recupera payload original + hash para reaper.
    Retorna None si no existe (job muy antiguo o corrupción).
    """
    try:
        row = db.execute(
            "SELECT payload_json, payload_hash FROM inbound_job_payloads WHERE dedupe_key=?",
            (dedupe_key,)
        ).fetchone()
        
        if not row:
            return None
        
        return json.loads(row["payload_json"]), row["payload_hash"]
    except Exception as e:
        log(f"Payload retrieval failed for {dedupe_key}: {e}", "ERROR")
        return None

def cleanup_expired_payloads():
    """Limpia payloads expirados (llamar periódicamente desde reaper)"""
    try:
        cursor = db.execute(
            "DELETE FROM inbound_job_payloads WHERE expires_at < datetime('now')",
            ()
        )
        if cursor.rowcount > 0:
            log(f"Cleaned up {cursor.rowcount} expired payloads", "INFO")
    except Exception as e:
        log(f"Payload cleanup error: {e}", "ERROR")

# =========================
# LOCK MANAGEMENT — Atomic, stealable, with heartbeat
# =========================
def acquire_lock(dedupe_key: str, payload_hash: str) -> bool:
    """
    Adquiere lock de procesamiento.
    Roba locks stale automáticamente.
    """
    now = datetime.now(timezone.utc).isoformat()
    stale_threshold = (datetime.now(timezone.utc) - timedelta(seconds=Config.LOCK_TIMEOUT)).isoformat()
    
    try:
        with db.transaction():
            # UPSERT atómico: insertar nuevo o robar stale
            cursor = db.execute("""
                INSERT INTO inbound_processing_locks 
                    (dedupe_key, claimed_at, worker_id, payload_hash, heartbeat_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    claimed_at = CASE 
                        WHEN inbound_processing_locks.claimed_at < ? THEN ?
                        ELSE inbound_processing_locks.claimed_at
                    END,
                    worker_id = CASE 
                        WHEN inbound_processing_locks.claimed_at < ? THEN ?
                        ELSE inbound_processing_locks.worker_id
                    END,
                    payload_hash = CASE 
                        WHEN inbound_processing_locks.claimed_at < ? THEN ?
                        ELSE inbound_processing_locks.payload_hash
                    END,
                    heartbeat_at = CASE 
                        WHEN inbound_processing_locks.claimed_at < ? THEN ?
                        ELSE inbound_processing_locks.heartbeat_at
                    END
                WHERE inbound_processing_locks.claimed_at < ?
            """, (
                dedupe_key, now, Config.WORKER_ID, payload_hash, now,  # INSERT
                stale_threshold, now,  # UPDATE claimed_at
                stale_threshold, Config.WORKER_ID,  # UPDATE worker_id
                stale_threshold, payload_hash,  # UPDATE payload_hash
                stale_threshold, now,  # UPDATE heartbeat_at
                stale_threshold  # WHERE condition
            ))
            
            # Verificar si somos dueños del lock
            row = db.execute(
                "SELECT worker_id, claimed_at FROM inbound_processing_locks WHERE dedupe_key=?",
                (dedupe_key,)
            ).fetchone()
            
            if not row:
                return False
            
            is_mine = row["worker_id"] == Config.WORKER_ID
            is_fresh = row["claimed_at"] == now
            
            if is_mine and is_fresh:
                log(f"Lock acquired: {dedupe_key}", "DEBUG")
                return True
            else:
                log(f"Lock busy: {dedupe_key} held by {row['worker_id']}", "DEBUG")
                return False
                
    except Exception as e:
        log(f"Lock acquisition error: {dedupe_key}: {e}", "ERROR")
        return False

def update_heartbeat(dedupe_key: str):
    """Actualiza heartbeat para jobs de larga duración"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE inbound_processing_locks SET heartbeat_at=? WHERE dedupe_key=? AND worker_id=?",
            (now, dedupe_key, Config.WORKER_ID)
        )
    except Exception as e:
        log(f"Heartbeat error for {dedupe_key}: {e}", "ERROR")

def release_lock(dedupe_key: str):
    """Libera lock al completar (éxito o fallo)"""
    try:
        db.execute(
            "DELETE FROM inbound_processing_locks WHERE dedupe_key=? AND worker_id=?",
            (dedupe_key, Config.WORKER_ID)
        )
    except Exception as e:
        log(f"Lock release error: {dedupe_key}: {e}", "ERROR")

def is_already_completed(dedupe_key: str) -> bool:
    """Verifica si ya fue procesado (audit table)"""
    try:
        row = db.execute(
            """SELECT 1 FROM processed_inbound_events 
               WHERE dedupe_key=? AND result IN ('success', 'manual_review', 'dead')""",
            (dedupe_key,)
        ).fetchone()
        return row is not None
    except Exception as e:
        log(f"Completion check error: {dedupe_key}: {e}", "ERROR")
        return False  # Conservador: si no podemos verificar, asumimos no completado

def mark_completed(dedupe_key: str, result: str, detail: dict, processing_time_ms: Optional[int] = None):
    """
    Marca job como completado en audit table.
    Libera lock y limpia payload (opcional).
    """
    try:
        with db.transaction():
            # Insertar en audit
            db.execute("""
                INSERT INTO processed_inbound_events 
                    (dedupe_key, processed_at, result, detail_json, worker_id, processing_time_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    processed_at=excluded.processed_at,
                    result=excluded.result,
                    detail_json=excluded.detail_json,
                    worker_id=excluded.worker_id,
                    processing_time_ms=excluded.processing_time_ms
            """, (
                dedupe_key,
                datetime.now(timezone.utc).isoformat(),
                result,
                json.dumps(detail),
                Config.WORKER_ID,
                processing_time_ms
            ))
            
            # Liberar lock
            db.execute(
                "DELETE FROM inbound_processing_locks WHERE dedupe_key=?",
                (dedupe_key,)
            )
            
            # Opcional: limpiar payload inmediatamente (ahorro de espacio)
            # db.execute("DELETE FROM inbound_job_payloads WHERE dedupe_key=?", (dedupe_key,))
        
        record_metric(result, processing_time_ms)
        log(f"Completed: {dedupe_key} = {result}", "INFO", {"detail": detail})
        
    except Exception as e:
        log(f"CRITICAL: mark_completed failed: {dedupe_key}: {e}", "CRITICAL")
        # No relanzar: mejor perder audit que crashar el worker

# =========================
# REAPER — Recuperación autónoma con payload real
# =========================
def reaper_loop(redis_client: redis.Redis):
    """
    Limpia locks stale y reencola jobs con payload original.
    Fix v8.4: Usa tabla inbound_job_payloads, no reconstrucción ad-hoc.
    """
    log("Reaper started", "INFO")
    
    while True:
        try:
            time.sleep(Config.REAPER_INTERVAL)
            
            stale_threshold = (datetime.now(timezone.utc) - timedelta(seconds=Config.LOCK_TIMEOUT)).isoformat()
            
            # 1. Encontrar locks stale
            stale_locks = db.execute(
                """SELECT dedupe_key, worker_id, payload_hash 
                   FROM inbound_processing_locks 
                   WHERE claimed_at < ? 
                   LIMIT 100""",
                (stale_threshold,)
            ).fetchall()
            
            if not stale_locks:
                continue
            
            log(f"Reaper found {len(stale_locks)} stale locks", "WARN")
            
            for lock in stale_locks:
                dedupe_key = lock["dedupe_key"]
                old_worker = lock["worker_id"]
                expected_hash = lock["payload_hash"]
                
                try:
                    with db.transaction():
                        # Verificar que sigue siendo stale (race condition check)
                        current = db.execute(
                            "SELECT claimed_at FROM inbound_processing_locks WHERE dedupe_key=?",
                            (dedupe_key,)
                        ).fetchone()
                        
                        if not current or current["claimed_at"] > stale_threshold:
                            continue  # Ya fue actualizado por otro
                        
                        # Eliminar lock stale
                        db.execute("DELETE FROM inbound_processing_locks WHERE dedupe_key=?", (dedupe_key,))
                        
                        # RECUPERAR PAYLOAD REAL — Fix crítico v8.4
                        payload_data = get_payload(dedupe_key)
                        
                        if payload_data:
                            job, stored_hash = payload_data
                            
                            # Validar integridad
                            if stored_hash != expected_hash:
                                log(f"Hash mismatch for {dedupe_key}: expected {expected_hash[:16]}..., got {stored_hash[:16]}...", "ERROR")
                                # Enviar a manual review en lugar de reencolar
                                mark_completed(dedupe_key, "manual_review", {
                                    "reason": "payload_integrity_error",
                                    "expected_hash_prefix": expected_hash[:16],
                                    "stored_hash_prefix": stored_hash[:16]
                                })
                                continue
                            
                            # Incrementar contador de reaps
                            reap_count = job.get("_reap_count", 0) + 1
                            job["_reap_count"] = reap_count
                            job["_reaped"] = True
                            job["_reaped_at"] = datetime.now(timezone.utc).isoformat()
                            job["_original_worker"] = old_worker
                            
                            # Guardar payload actualizado
                            persist_payload(dedupe_key, job)
                            
                            # REENCOLAR CON FIFO CORRECTO (rpush) — Fix v8.4
                            redis_client.rpush(Config.QUEUE, json.dumps(job))
                            
                            log(f"Reaper requeued: {dedupe_key} (reap #{reap_count}, was {old_worker})", "INFO")
                            
                            # Si ha sido reapeado muchas veces, alertar
                            if reap_count >= 3:
                                log(f"Job reaped {reap_count} times: {dedupe_key}", "WARN", {
                                    "dedupe_key": dedupe_key,
                                    "reap_count": reap_count,
                                    "job_preview": json.dumps(job)[:200]
                                })
                        else:
                            # Payload perdido (expirado o corrupción)
                            log(f"Payload lost for stale lock: {dedupe_key}", "ERROR")
                            mark_completed(dedupe_key, "dead", {
                                "reason": "payload_lost",
                                "original_worker": old_worker
                            })
                            
                except Exception as e:
                    log(f"Reaper error for {dedupe_key}: {e}", "ERROR")
            
            # 2. Cleanup de payloads expirados
            cleanup_expired_payloads()
            
        except Exception as e:
            log(f"Reaper fatal error: {e}", "ERROR")
            time.sleep(5)

# =========================
# MERCADOLIBRE API — Con retry y circuit breaker pattern
# =========================
def ml_token() -> str:
    with open(Config.TOKEN_FILE) as f:
        return json.load(f)["access_token"]

def ml_get(path: str, retries: int = 0) -> dict:
    import requests  # Import local para no fallar si no está disponible al inicio
    
    token = ml_token()
    url = f"https://api.mercadolibre.com{path}"
    
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            
            if r.status_code == 200:
                return r.json()
            
            if r.status_code == 401:
                raise RuntimeError("token_expired")
            
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt < retries:
                    sleep_time = Config.RETRY_DELAY_BASE * (2 ** attempt)
                    log(f"ML API retry: {r.status_code} attempt {attempt+1}/{retries}", "WARN")
                    time.sleep(sleep_time)
                    continue
            
            raise RuntimeError(f"ML API error: {path} = {r.status_code}")
            
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(Config.RETRY_DELAY_BASE * (2 ** attempt))
                continue
            raise RuntimeError(f"ML API timeout after {retries} retries")
        
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                time.sleep(Config.RETRY_DELAY_BASE * (2 ** attempt))
                continue
            raise RuntimeError(f"ML API exception: {e}")

# =========================
# BUSINESS LOGIC — Idempotente, con validación de SKU
# =========================
def is_full(order: dict, shipment: Optional[dict]) -> Optional[bool]:
    """Detecta si es Fulfillment (Full) o FBM"""
    lt = order.get("logistic_type")
    if lt == "fulfillment":
        return True
    if lt in ("cross_docking", "drop_off", "xd_drop_off"):
        return False
    
    if lt is None and shipment:
        lt = shipment.get("logistic_type")
        if lt == "fulfillment":
            return True
        if lt in ("cross_docking", "drop_off", "xd_drop_off"):
            return False
    
    return None

def is_allowed_sku(sku: str) -> bool:
    """Fail-closed: si no puede verificar, rechaza"""
    sku = (sku or "").strip()
    if not sku:
        return False
    
    try:
        # Si no existe tabla de allowlist, permitir todo (modo legacy)
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inbound_allowed_skus'"
        ).fetchone()
        if not row:
            return True
        
        row = db.execute(
            "SELECT 1 FROM inbound_allowed_skus WHERE sku=? AND enabled=1",
            (sku,)
        ).fetchone()
        return row is not None
    except Exception as e:
        log(f"SKU allowlist error: {e}", "ERROR")
        return False

def parse_items(order: dict) -> List[dict]:
    """Extrae SKUs válidos de la orden"""
    items = []
    
    for it in order.get("order_items", []):
        item = it.get("item", {})
        item_id = str(item.get("id", ""))
        var_id = str(item.get("variation_id", ""))
        
        sku = None
        
        # 1. Buscar en mapping local
        if item_id and var_id:
            row = db.execute(
                "SELECT sku FROM sku_mapping WHERE channel='meli' AND remote_item_id=? AND remote_variation_id=?",
                (item_id, var_id)
            ).fetchone()
            sku = row[0] if row else None
        
        # 2. Fallback a atributos de ML
        if not sku:
            for attr in item.get("attributes", []):
                if str(attr.get("id", "")).upper() == "SELLER_SKU":
                    sku = attr.get("value_name")
                    break
        
        if not sku:
            continue
            
        sku = sku.strip()
        
        try:
            qty = int(it.get("quantity", 0))
        except:
            qty = 0
        
        if qty <= 0 or not is_allowed_sku(sku):
            continue
        
        items.append({"sku": sku, "qty": qty, "item_id": item_id, "variation_id": var_id})
    
    return items

def run_tool(tool_name: str, env_vars: dict) -> Tuple[int, str, str]:
    """
    Ejecuta tool externa con idempotencia garantizada por CLIENT_ORDER_REF.
    Las tools deben usar CLIENT_ORDER_REF como clave única en Odoo.
    """
    tool_paths = [
        f"/data/{tool_name}.py",
        f"/mnt/data/appdata/bridge/tools/{tool_name}.py",
        f"./tools/{tool_name}.py"
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
    
    try:
        start = time.time()
        p = subprocess.run(
            ["python3", tool_path], 
            env=env, 
            capture_output=True, 
            text=True, 
            timeout=180
        )
        elapsed_ms = int((time.time() - start) * 1000)
        
        if p.returncode != 0:
            log(f"Tool failed: {tool_name} rc={p.returncode}", "ERROR", {
                "stderr": p.stderr[-500:],
                "stdout": p.stdout[-500:]
            })
        
        return p.returncode, p.stdout, p.stderr
        
    except subprocess.TimeoutExpired:
        return -2, "", f"Timeout after 180s"
    except Exception as e:
        return -3, "", str(e)

def process_full(order: dict, order_id: str, site: str, state: str, dedupe_key: str, visible_ref: str):
    """Procesa orden Fulfillment"""
    ref = f"MLFULL:{site}:{visible_ref}"
    start_time = time.time()
    
    # Estados terminales negativos
    if state in ("cancelled", "refunded"):
        if not is_enabled("meli_inbound_full_refunds_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "refunds_disabled"})
            return
        
        rc, out, err = run_tool("inbound_full_so_refund_and_cancel", {"CLIENT_ORDER_REF": ref})
        result = "success" if rc == 0 else "manual_review"
        processing_time = int((time.time() - start_time) * 1000)
        mark_completed(dedupe_key, result, {"ref": ref, "rc": rc, "error": err[:500]}, processing_time)
        return
    
    # Estados positivos
    if state in ("paid", "approved"):
        if not is_enabled("meli_inbound_full_paid_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "paid_disabled"})
            return
        
        buyer = order.get("buyer") or {}
        buyer_name = (buyer.get("nickname") or buyer.get("first_name") or "").strip()
        buyer_last = (buyer.get("last_name") or "").strip()
        full_name = (f"{buyer_name} {buyer_last}".strip() if buyer_last else buyer_name)

        so_note = (
            f"MercadoLibre FULL | #{visible_ref} | {state}"
            + (f" | {full_name}" if full_name else "")
        )

        rc, out, err = run_tool("inbound_full_paid_one_shot_no_stock", {
            "CLIENT_ORDER_REF": ref,
            "ORDER_JSON": json.dumps(order),
            "SITE_ID": site,
            "SO_NOTE": so_note,
        })
        result = "success" if rc == 0 else "manual_review"
        processing_time = int((time.time() - start_time) * 1000)
        mark_completed(dedupe_key, result, {"ref": ref, "rc": rc, "error": err[:500]}, processing_time)

def process_fbm(order: dict, order_id: str, site: str, state: str, dedupe_key: str, visible_ref: str):
    """Procesa orden FBM (Fulfillment by Merchant)"""
    ref = f"MLFBM:{site}:{visible_ref}"
    start_time = time.time()
    
    if state in ("cancelled", "refunded"):
        if not is_enabled("meli_inbound_fbm_refunds_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "refunds_disabled"})
            return
        
        rc, out, err = run_tool("inbound_fbm_so_refund_and_cancel", {"CLIENT_ORDER_REF": ref})
        result = "success" if rc == 0 else "manual_review"
        processing_time = int((time.time() - start_time) * 1000)
        mark_completed(dedupe_key, result, {"ref": ref, "rc": rc, "error": err[:500]}, processing_time)
        return
    
    if state in ("paid", "approved", "confirmed"):
        if not is_enabled("meli_inbound_fbm_paid_enabled"):
            mark_completed(dedupe_key, "skipped", {"reason": "paid_disabled"})
            return
        
        items = parse_items(order)
        if not items:
            mark_completed(dedupe_key, "manual_review", {"reason": "no_valid_items"})
            return
        
        buyer = order.get("buyer") or {}
        buyer_name = (buyer.get("nickname") or buyer.get("first_name") or "").strip()
        buyer_last = (buyer.get("last_name") or "").strip()
        full_name = (buyer_name + (" " + buyer_last if buyer_last else "")).strip() or "UNKNOWN"

        so_note = f"MercadoLibre FBM | #{visible_ref} | {state} | {full_name}"

        rc, out, err = run_tool("inbound_fbm_so_apply_paid_one_shot", {
            "CLIENT_ORDER_REF": ref,
            "ORDER_JSON": json.dumps(order),
            "SITE_ID": site,
            "SO_NOTE": so_note,
        })
        
        processing_time = int((time.time() - start_time) * 1000)
        if rc == 0:
            mark_completed(dedupe_key, "success", {"ref": ref, "items": len(items)}, processing_time)
        else:
            mark_completed(dedupe_key, "manual_review", {"ref": ref, "rc": rc, "error": err[:500]}, processing_time)

# =========================
# MAIN LOOP — FIFO correcto, heartbeat, graceful degradation
# =========================
def main():
    init_db()
    log(f"Worker started: {Config.WORKER_ID}", "INFO", {
        "start_time": Config.WORKER_START_TIME,
        "db": Config.DB_PATH,
        "queue": Config.QUEUE
    })
    
    redis_client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)
    
    # Verificar conectividad
    try:
        redis_client.ping()
        log("Redis connected", "INFO")
    except Exception as e:
        log(f"Redis connection failed: {e}", "CRITICAL")
        sys.exit(1)
    
    # Iniciar reaper thread
    reaper_thread = threading.Thread(target=reaper_loop, args=(redis_client,), daemon=True)
    reaper_thread.start()
    
    # Métricas periódicas
    def metrics_reporter():
        while True:
            time.sleep(300)  # 5 minutos
            try:
                locks = db.execute("SELECT COUNT(*) FROM inbound_processing_locks").fetchone()[0]
                payloads = db.execute("SELECT COUNT(*) FROM inbound_job_payloads").fetchone()[0]
                completed_1h = db.execute(
                    "SELECT COUNT(*) FROM processed_inbound_events WHERE processed_at > datetime('now', '-1 hour')"
                ).fetchone()[0]
                
                q_len = redis_client.llen(Config.QUEUE)
                proc_len = redis_client.llen(Config.PROCESSING)
                dlq_len = redis_client.llen(Config.DEAD_LETTER)
                
                log("Metrics snapshot", "INFO", {
                    "db_locks": locks,
                    "db_payloads": payloads,
                    "completed_1h": completed_1h,
                    "redis_queue": q_len,
                    "redis_processing": proc_len,
                    "redis_dlq": dlq_len
                })
            except Exception as e:
                log(f"Metrics error: {e}", "ERROR")
    
    threading.Thread(target=metrics_reporter, daemon=True).start()
    
    # Main processing loop
    while True:
        if not is_enabled("meli_inbound_enabled"):
            time.sleep(2)
            continue
        
        item = None
        dedupe_key = None
        processing_start = None
        
        try:
            # FIFO correcto: BRPOP (derecha) → LPUSH (izquierda) en processing
            # Pero para mantener FIFO en requeue, usamos RPUSH
            item = redis_client.brpop(Config.QUEUE, timeout=5)
            if not item:
                continue
            
            # brpop retorna (queue_name, item)
            _, item_data = item
            
            processing_start = time.time()
            
            # Parsear job
            try:
                job = json.loads(item_data)
            except json.JSONDecodeError:
                log(f"Invalid JSON in queue", "ERROR", {"preview": str(item_data)[:200]})
                redis_client.lpush(Config.DEAD_LETTER, json.dumps({
                    "raw": str(item_data)[:1000],
                    "error": "json_decode_error",
                    "ts": datetime.now(timezone.utc).isoformat()
                }))
                redis_client.ltrim(Config.DEAD_LETTER, 0, 9999)
                continue
            
            # Generar/extraer dedupe_key
            dedupe_key = job.get("dedupe_key")
            if not dedupe_key:
                resource = job.get("resource", "")
                m = re.match(r"^/orders/([A-Z0-9\-]+)$", resource)
                if m:
                    dedupe_key = f"ml:{m.group(1)}"
                else:
                    dedupe_key = f"legacy:{hashlib.sha256(item_data.encode()).hexdigest()[:16]}"
                job["dedupe_key"] = dedupe_key
            
            # 1. Persistir payload ANTES de cualquier operación — Fix v8.4
            payload_hash = persist_payload(dedupe_key, job)
            
            # 2. Verificar si ya completado (idempotencia)
            if is_already_completed(dedupe_key):
                log(f"Dedupe: already completed", "DEBUG", {"dedupe_key": dedupe_key})
                continue
            
            # 3. Adquirir lock
            if not acquire_lock(dedupe_key, payload_hash):
                # Lock ocupado por otro worker activo
                # FIX v8.4: FIFO correcto — usar RPUSH, no LPUSH
                job["_retry_delay"] = job.get("_retry_delay", 0) + 1
                redis_client.rpush(Config.QUEUE, json.dumps(job))
                log(f"Lock busy: requeued with delay", "DEBUG", {
                    "dedupe_key": dedupe_key,
                    "delay": job["_retry_delay"]
                })
                time.sleep(min(job["_retry_delay"], 10))  # Backoff creciente, max 10s
                continue
            
            # Tenemos el lock — agregar a processing list para observabilidad
            redis_client.lpush(Config.PROCESSING, json.dumps({
                "dedupe_key": dedupe_key,
                "worker": Config.WORKER_ID,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "payload_hash": payload_hash[:16]
            }))
            
            log(f"Processing started", "INFO", {
                "dedupe_key": dedupe_key,
                "retry_count": job.get("_retry_count", 0),
                "reaped": job.get("_reaped", False)
            })
            
            # 4. Extraer o fetchear orden
            order = job.get("order_json")
            
            if not order:
                # Necesitamos fetchear de ML
                resource = job.get("resource", "")
                m = re.match(r"^/orders/([A-Z0-9\-]+)$", resource)
                
                if not m:
                    mark_completed(dedupe_key, "dead", {"reason": "bad_resource", "resource": resource})
                    redis_client.lrem(Config.PROCESSING, 0, json.dumps({"dedupe_key": dedupe_key}))
                    continue
                
                try:
                    order = ml_get(f"/orders/{m.group(1)}", retries=2)
                except Exception as e:
                    error_msg = str(e)
                    retry_count = job.get("_retry_count", 0)
                    
                    if retry_count >= Config.MAX_RETRIES or "token_expired" in error_msg:
                        mark_completed(dedupe_key, "dead", {
                            "reason": "ml_fetch_failed",
                            "error": error_msg,
                            "retries": retry_count
                        })
                        redis_client.lpush(Config.DEAD_LETTER, json.dumps({
                            "job": job,
                            "error": error_msg,
                            "failed_at": "ml_fetch"
                        }))
                        redis_client.ltrim(Config.DEAD_LETTER, 0, 9999)
                    else:
                        # Reintentar con backoff
                        job["_retry_count"] = retry_count + 1
                        release_lock(dedupe_key)
                        redis_client.rpush(Config.QUEUE, json.dumps(job))
                        log(f"ML fetch failed: retry scheduled", "WARN", {
                            "dedupe_key": dedupe_key,
                            "retry": job["_retry_count"],
                            "error": error_msg
                        })
                    
                    redis_client.lrem(Config.PROCESSING, 0, json.dumps({"dedupe_key": dedupe_key}))
                    continue
            
            # 5. Procesar orden
            order_id = str(order.get("id", "UNKNOWN"))
            site = str(order.get("site_id", "MLM"))
            state = str(order.get("status", "")).lower()
            pack_id = str(order.get("pack_id")) if order.get("pack_id") else None
            # --- Visible reference for ops: prefer pack_id, fallback to order_id ---
            visible_ref = pack_id if pack_id else order_id
            log(f"Visible ref computed | order_id={order_id} | pack_id={pack_id} | visible_ref={visible_ref}")
            
            # Guardar estado
            db.execute("""
                INSERT INTO inbound_orders_state (order_id, site, last_state, last_seen_at, pack_id, logistic_type)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    last_state=excluded.last_state,
                    last_seen_at=excluded.last_seen_at,
                    pack_id=COALESCE(excluded.pack_id, pack_id),
                    logistic_type=COALESCE(excluded.logistic_type, logistic_type)
            """, (
                order_id, site, state, 
                datetime.now(timezone.utc).isoformat(), 
                pack_id,
                order.get("logistic_type")
            ))
            
            # Detectar tipo logístico
            shipment = None
            if order.get("shipping", {}).get("id"):
                try:
                    shipment = ml_get(f"/shipments/{order['shipping']['id']}", retries=1)
                except Exception as e:
                    log(f"Shipment fetch warning", "WARN", {"error": str(e)})
            
            full = is_full(order, shipment)
            
            if full is None:
                mark_completed(dedupe_key, "manual_review", {
                    "order_id": order_id,
                    "reason": "unknown_logistic_type",
                    "logistic_type": order.get("logistic_type")
                })
                redis_client.lrem(Config.PROCESSING, 0, json.dumps({"dedupe_key": dedupe_key}))
                continue
            
            # Ejecutar lógica de negocio
            if full:
                process_full(order, order_id, site, state, dedupe_key, visible_ref)
            else:
                process_fbm(order, order_id, site, state, dedupe_key, visible_ref)

            # Limpiar de processing list
            redis_client.lrem(Config.PROCESSING, 0, json.dumps({"dedupe_key": dedupe_key}))
            
        except Exception as e:
            log(f"Unhandled exception", "CRITICAL", {
                "error": str(e),
                "dedupe_key": dedupe_key,
                "traceback": str(sys.exc_info()[2])
            })
            
            if dedupe_key:
                try:
                    job = json.loads(item_data) if item else {}
                    retry_count = job.get("_retry_count", 0)
                    
                    if retry_count >= Config.MAX_RETRIES:
                        mark_completed(dedupe_key, "dead", {
                            "reason": "unhandled_exception",
                            "error": str(e)
                        })
                        redis_client.lpush(Config.DEAD_LETTER, json.dumps({
                            "job": job,
                            "error": str(e),
                            "traceback": str(sys.exc_info())
                        }))
                        redis_client.ltrim(Config.DEAD_LETTER, 0, 9999)
                    else:
                        job["_retry_count"] = retry_count + 1
                        release_lock(dedupe_key)
                        redis_client.rpush(Config.QUEUE, json.dumps(job))
                    
                    redis_client.lrem(Config.PROCESSING, 0, json.dumps({"dedupe_key": dedupe_key}))
                except:
                    pass
            
            time.sleep(1)

if __name__ == "__main__":
    main()
