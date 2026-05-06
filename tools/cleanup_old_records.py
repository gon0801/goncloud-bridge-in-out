#!/usr/bin/env python3
"""
GONCLOUD Bridge — Limpieza periódica de registros antiguos

Elimina filas viejas de las tablas de auditoría y payloads para mantener
la DB pequeña. NUNCA toca sku_mapping, bridge_settings, amazon_sku_mapping,
inbound_allowed_skus, ni las tablas de estado de órdenes.

Retenciones por defecto:
  - Eventos success                → 90 días
  - Eventos manual_review/dead/error → 30 días
  - Payloads expirados (expires_at) → inmediato
  - Payloads huérfanos             → 7 días
  - Locks stale                   → 1 día
  - Métricas                      → 90 días
  - events / snapshot_items       → 90 días

Uso:
  # Ver qué se borraría sin tocar nada:
  python3 tools/cleanup_old_records.py --dry-run

  # Ejecutar limpieza real:
  python3 tools/cleanup_old_records.py

  # Con retenciones personalizadas:
  python3 tools/cleanup_old_records.py --success-days 60 --stuck-days 14

  BRIDGE_DB=/mnt/data/appdata/bridge/data/bridge.db \\
  python3 tools/cleanup_old_records.py --dry-run
"""

import argparse
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("BRIDGE_DB", "/data/bridge.db")
LOG_PATH = os.getenv("CLEANUP_LOG", "/data/bridge_cleanup.log")


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str, logfile):
    line = f"[{ts()}] {msg}"
    print(line)
    logfile.write(line + "\n")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn


def count(conn, table: str, where: str, params: tuple) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()
    return row[0] if row else 0


def delete(conn, table: str, where: str, params: tuple, dry_run: bool, logfile) -> int:
    n = count(conn, table, where, params)
    if n == 0:
        return 0
    if dry_run:
        log(f"  [DRY-RUN] {table}: borraría {n} filas ({where[:60]})", logfile)
    else:
        conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        log(f"  {table}: {n} filas eliminadas", logfile)
    return n


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def run_cleanup(args, logfile):
    conn = open_db()
    total = 0

    log(f"DB: {DB_PATH}", logfile)
    log(f"dry_run={args.dry_run}  success_days={args.success_days}  stuck_days={args.stuck_days}", logfile)
    log("-" * 60, logfile)

    # ── 1. Audit MeLi ────────────────────────────────────────────
    if table_exists(conn, "processed_inbound_events"):
        log("processed_inbound_events:", logfile)
        total += delete(
            conn, "processed_inbound_events",
            "result = 'success' AND processed_at < datetime('now', ?)",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )
        total += delete(
            conn, "processed_inbound_events",
            "result IN ('manual_review','dead','error') AND processed_at < datetime('now', ?)",
            (f"-{args.stuck_days} days",), args.dry_run, logfile,
        )

    # ── 2. Audit Amazon ───────────────────────────────────────────
    if table_exists(conn, "amazon_processed_events"):
        log("amazon_processed_events:", logfile)
        total += delete(
            conn, "amazon_processed_events",
            "result = 'success' AND processed_at < datetime('now', ?)",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )
        total += delete(
            conn, "amazon_processed_events",
            "result IN ('manual_review','dead','error') AND processed_at < datetime('now', ?)",
            (f"-{args.stuck_days} days",), args.dry_run, logfile,
        )

    # ── 3. Payloads MeLi ─────────────────────────────────────────
    if table_exists(conn, "inbound_job_payloads"):
        log("inbound_job_payloads:", logfile)
        # Expirados por TTL explícito
        total += delete(
            conn, "inbound_job_payloads",
            "expires_at IS NOT NULL AND expires_at < datetime('now')",
            (), args.dry_run, logfile,
        )
        # Huérfanos (sin audit record) más viejos que 7 días
        total += delete(
            conn, "inbound_job_payloads",
            "created_at < datetime('now', '-7 days') "
            "AND dedupe_key NOT IN (SELECT dedupe_key FROM processed_inbound_events)",
            (), args.dry_run, logfile,
        )
        # Completados exitosamente y viejos (audit ya borrado arriba, o éxito viejo)
        total += delete(
            conn, "inbound_job_payloads",
            "created_at < datetime('now', ?)"
            " AND dedupe_key IN ("
            "  SELECT dedupe_key FROM processed_inbound_events WHERE result='success'"
            ")",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )

    # ── 4. Payloads Amazon ────────────────────────────────────────
    if table_exists(conn, "amazon_job_payloads"):
        log("amazon_job_payloads:", logfile)
        total += delete(
            conn, "amazon_job_payloads",
            "expires_at IS NOT NULL AND expires_at < datetime('now')",
            (), args.dry_run, logfile,
        )
        total += delete(
            conn, "amazon_job_payloads",
            "created_at < datetime('now', '-7 days') "
            "AND dedupe_key NOT IN (SELECT dedupe_key FROM amazon_processed_events)",
            (), args.dry_run, logfile,
        )
        total += delete(
            conn, "amazon_job_payloads",
            "created_at < datetime('now', ?)"
            " AND dedupe_key IN ("
            "  SELECT dedupe_key FROM amazon_processed_events WHERE result='success'"
            ")",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )

    # ── 5. Locks stale ────────────────────────────────────────────
    if table_exists(conn, "inbound_processing_locks"):
        log("inbound_processing_locks (stale):", logfile)
        total += delete(
            conn, "inbound_processing_locks",
            "claimed_at < datetime('now', '-1 day')",
            (), args.dry_run, logfile,
        )

    if table_exists(conn, "amazon_processing_locks"):
        log("amazon_processing_locks (stale):", logfile)
        total += delete(
            conn, "amazon_processing_locks",
            "claimed_at < datetime('now', '-1 day')",
            (), args.dry_run, logfile,
        )

    # ── 6. Métricas horarias ──────────────────────────────────────
    if table_exists(conn, "inbound_metrics"):
        log("inbound_metrics:", logfile)
        total += delete(
            conn, "inbound_metrics",
            "hour < datetime('now', ?)",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )

    if table_exists(conn, "amazon_metrics"):
        log("amazon_metrics:", logfile)
        total += delete(
            conn, "amazon_metrics",
            "hour < datetime('now', ?)",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )

    # ── 7. Events / snapshot_items (outbound legacy) ──────────────
    if table_exists(conn, "events"):
        log("events:", logfile)
        total += delete(
            conn, "events",
            "created_at < datetime('now', ?)",
            (f"-{args.success_days} days",), args.dry_run, logfile,
        )

    if table_exists(conn, "snapshot_items"):
        log("snapshot_items (huérfanos):", logfile)
        total += delete(
            conn, "snapshot_items",
            "event_id NOT IN (SELECT id FROM events)",
            (), args.dry_run, logfile,
        )

    # ── 8. Commit + VACUUM ────────────────────────────────────────
    if not args.dry_run:
        conn.commit()
        log("VACUUM...", logfile)
        conn.execute("VACUUM")
        log(f"DONE — {total} filas eliminadas en total.", logfile)
    else:
        log(f"[DRY-RUN] DONE — {total} filas se eliminarían.", logfile)

    conn.close()
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Limpieza periódica de registros antiguos en bridge.db"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Mostrar qué se borraría sin tocar nada (default: False)",
    )
    parser.add_argument(
        "--success-days", type=int, default=90,
        help="Retención de eventos 'success' en días (default: 90)",
    )
    parser.add_argument(
        "--stuck-days", type=int, default=30,
        help="Retención de eventos manual_review/dead/error en días (default: 30)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  GONCLOUD — Limpieza periódica de bridge.db")
    print("=" * 60)

    os.makedirs(os.path.dirname(LOG_PATH) if os.path.dirname(LOG_PATH) else ".", exist_ok=True)

    with open(LOG_PATH, "a") as logfile:
        logfile.write(f"\n{'='*60}\n")
        run_cleanup(args, logfile)

    print("=" * 60)


if __name__ == "__main__":
    main()
