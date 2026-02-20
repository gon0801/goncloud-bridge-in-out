#!/usr/bin/env bash
set -euo pipefail

DB="/mnt/data/appdata/bridge/data/bridge.db"
LOG="/mnt/data/appdata/bridge/data/bridge_cleanup.log"

# Retención (AJUSTABLE)
KEEP_DAYS=90

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[$TS] START cleanup KEEP_DAYS=$KEEP_DAYS" >> "$LOG"

# 1) Limpieza SQLite (solo tablas de auditoría)
# Nota: NO tocar sku_mapping / bridge_settings / known_skus aquí.
sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;

-- Borrar events viejos (por created_at ISO)
DELETE FROM events
WHERE channel='meli'
  AND created_at < datetime('now', '-${KEEP_DAYS} days');

-- Borrar snapshot_items viejos (por event_id que ya no existe en events)
DELETE FROM snapshot_items
WHERE event_id NOT IN (SELECT CAST(id AS TEXT) FROM events);

-- Borrar processed_events viejos si tu tabla tiene timestamps (si NO los tiene, lo quitamos)
-- Si tu processed_events NO tiene processed_at, avísame y lo cambiamos a ROWID por ventana.
DELETE FROM processed_events
WHERE channel='meli'
  AND processed_at IS NOT NULL
  AND processed_at < datetime('now', '-${KEEP_DAYS} days');

SQL

# 2) Rotación simple del log meli_sync.log (si existe)
SYNC_LOG="/mnt/data/appdata/bridge/data/meli_sync.log"
if [[ -f "$SYNC_LOG" ]]; then
  # conserva 5000 líneas
  tail -n 5000 "$SYNC_LOG" > "${SYNC_LOG}.tmp" && mv "${SYNC_LOG}.tmp" "$SYNC_LOG"
fi

echo "[$TS] DONE cleanup" >> "$LOG"
