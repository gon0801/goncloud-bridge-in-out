#!/usr/bin/env bash
set -euo pipefail

DB="/mnt/data/appdata/bridge/data/bridge.db"
LOG="/mnt/data/logs/maintenance/bridge-cleanup.log"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

mkdir -p /mnt/data/logs/maintenance

echo "[$(ts)] START bridge cleanup" >> "$LOG"

# Retenciones
KEEP_DAYS_EVENTS=90
KEEP_DAYS_PROCESSED=90
KEEP_DAYS_REJECTED=180

sqlite3 "$DB" >>"$LOG" 2>&1 <<SQL
PRAGMA busy_timeout=10000;

DELETE FROM events
WHERE created_at < datetime('now', '-${KEEP_DAYS_EVENTS} days');

DELETE FROM processed_events
WHERE processed_at < datetime('now', '-${KEEP_DAYS_PROCESSED} days');

DELETE FROM rejected_skus
WHERE created_at < datetime('now', '-${KEEP_DAYS_REJECTED} days');

VACUUM;
SQL

echo "[$(ts)] DONE bridge cleanup" >> "$LOG"
