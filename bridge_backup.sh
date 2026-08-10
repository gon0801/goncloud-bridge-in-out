#!/bin/bash

set -e

BRIDGE_PATH="/mnt/data/appdata/bridge"
BACKUP_PATH="/mnt/data/backups/bridge"
DATE=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$BACKUP_PATH"

echo "=== BRIDGE BACKUP $DATE ==="

# 1) Verificar integridad DB antes de copiar
echo "Checking SQLite integrity..."
sqlite3 "$BRIDGE_PATH/data/bridge.db" "PRAGMA integrity_check;" | grep -q "ok"

# 2) Backup DB (online-safe: sqlite3 .backup aplica el WAL antes de copiar,
#    a diferencia de cp plano que omite las páginas pendientes en bridge.db-wal)
sqlite3 "$BRIDGE_PATH/data/bridge.db" ".backup '$BACKUP_PATH/bridge_db_$DATE.db'"

# 2b) Backup tokens y credenciales (permisos 600 en destino)
for f in .meli_tokens.json amazon_credentials.json .env.meli; do
  src="$BRIDGE_PATH/data/$f"
  [ -f "$src" ] && install -m 600 "$src" "$BACKUP_PATH/${f//\//_}_$DATE"
done

# 3) Backup código limpio (sin secretos ni runtime)
tar -czf "$BACKUP_PATH/bridge_CODE_$DATE.tar.gz" \
  --ignore-failed-read \
  --exclude 'bridge/redis' \
  --exclude 'bridge/data' \
  --exclude 'bridge/.env*' \
  --exclude '**/.env*' \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  -C /mnt/data/appdata bridge

# 4) Rotación (mantener últimos 7)
ls -1t "$BACKUP_PATH"/bridge_db_*.db | tail -n +8 | xargs -r rm --
ls -1t "$BACKUP_PATH"/bridge_CODE_*.tar.gz | tail -n +8 | xargs -r rm --

echo "Backup completed successfully."
