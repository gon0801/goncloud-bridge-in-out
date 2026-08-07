#!/usr/bin/env python3
"""
Rev4 Schema Migrator — GONCLOUD Bridge
Idempotente: crea tablas Rev4 si faltan + añade columnas faltantes con ALTER TABLE.
Seguro para correr en cada arranque.
"""

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("BRIDGE_DB") or os.getenv("BRIDGE_DB_PATH") or "/data/bridge.db"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cols = set()
    for r in conn.execute(f"PRAGMA table_info({table});").fetchall():
        # PRAGMA: cid, name, type, notnull, dflt_value, pk
        cols.add(str(r[1]))
    return cols


def ensure_table(conn: sqlite3.Connection, ddl: str):
    conn.execute(ddl)


def ensure_column(conn: sqlite3.Connection, table: str, col: str, col_def: str):
    cols = get_columns(conn, table)
    if col in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")


def main():
    print(f"[migrate_rev4_schema] start db={DB_PATH} ts={utc_now()}", flush=True)
    conn = connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE;")

        # 1) inbound_job_payloads (nuevo)
        ensure_table(
            conn,
            """
        CREATE TABLE IF NOT EXISTS inbound_job_payloads (
            dedupe_key   TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT
        );
        """,
        )

        # 2) inbound_processing_locks (nuevo)
        ensure_table(
            conn,
            """
        CREATE TABLE IF NOT EXISTS inbound_processing_locks (
            dedupe_key    TEXT PRIMARY KEY,
            claimed_at    TEXT NOT NULL,
            worker_id     TEXT NOT NULL,
            payload_hash  TEXT,
            heartbeat_at  TEXT
        );
        """,
        )

        # 3) processed_inbound_events (existe legacy, migrar columnas)
        ensure_table(
            conn,
            """
        CREATE TABLE IF NOT EXISTS processed_inbound_events (
            dedupe_key TEXT PRIMARY KEY,
            processed_at TEXT,
            result TEXT,
            detail_json TEXT
        );
        """,
        )
        # columnas nuevas rev4
        ensure_column(conn, "processed_inbound_events", "worker_id", "TEXT")
        ensure_column(conn, "processed_inbound_events", "processing_time_ms", "INTEGER")

        # 4) inbound_orders_state (existe legacy, migrar columnas)
        ensure_table(
            conn,
            """
        CREATE TABLE IF NOT EXISTS inbound_orders_state (
            order_id TEXT PRIMARY KEY,
            last_state TEXT,
            last_updated_at TEXT,
            last_seen_at TEXT,
            pack_id TEXT
        );
        """,
        )
        ensure_column(conn, "inbound_orders_state", "site", "TEXT")
        ensure_column(conn, "inbound_orders_state", "logistic_type", "TEXT")

        conn.execute("COMMIT;")
        print("[migrate_rev4_schema] ok", flush=True)
    except Exception as e:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        print(f"[migrate_rev4_schema] FAIL: {e}", flush=True)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
