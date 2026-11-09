#!/bin/bash

set -e

BRIDGE_PATH="${BRIDGE_PATH:-/mnt/data/appdata/bridge}"
BACKUP_PATH="${BACKUP_PATH:-/mnt/data/backups/bridge}"
KEEP="${BRIDGE_BACKUP_KEEP:-3}"
DATE=$(date -u +%Y%m%dT%H%M%SZ)

BRIDGE_PARENT=$(dirname "$BRIDGE_PATH")
BRIDGE_NAME=$(basename "$BRIDGE_PATH")

mkdir -p "$BACKUP_PATH"

echo "=== BRIDGE BACKUP $DATE ==="

# Conserva las $KEEP copias más nuevas que empiezan por <prefijo> y nunca borra
# <recien>, aunque por un empate de fecha quede fuera de las primeras.
rotate() {
  local prefix="$1" recien="$2"
  local -a files=()
  local f
  for f in "$BACKUP_PATH"/${prefix}*; do
    [ -e "$f" ] || continue
    files+=("$f")
  done
  [ "${#files[@]}" -gt "$KEEP" ] || return 0
  ls -1t "${files[@]}" | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
    if [ "$old" != "$recien" ]; then
      rm -f -- "$old"
    fi
  done
}

# 1) Verificar integridad DB antes de copiar
echo "Checking SQLite integrity..."
sqlite3 "$BRIDGE_PATH/data/bridge.db" "PRAGMA integrity_check;" | grep -q "ok"

# 2) Backup DB (online-safe: sqlite3 .backup aplica el WAL antes de copiar,
#    a diferencia de cp plano que omite las páginas pendientes en bridge.db-wal)
DB_FILE="$BACKUP_PATH/bridge_db_$DATE.db"
sqlite3 "$BRIDGE_PATH/data/bridge.db" ".backup '$DB_FILE'"

# 2b) Backup tokens y credenciales (permisos 600 en destino)
for f in .meli_tokens.json amazon_credentials.json .env.meli; do
  src="$BRIDGE_PATH/data/$f"
  if [ -f "$src" ]; then
    install -m 600 "$src" "$BACKUP_PATH/${f//\//_}_$DATE"
  fi
done

# 3) Backup código limpio (sin secretos ni runtime). --ignore-failed-read solo
#    lo entiende GNU tar; en bsdtar (macOS) se omite para poder probar local.
TAR_OPTS=()
if tar --help 2>/dev/null | grep -q -- "--ignore-failed-read"; then
  TAR_OPTS+=("--ignore-failed-read")
fi
CODE_FILE="$BACKUP_PATH/bridge_CODE_$DATE.tar.gz"
tar -czf "$CODE_FILE" "${TAR_OPTS[@]}" \
  --exclude "$BRIDGE_NAME/redis" \
  --exclude "$BRIDGE_NAME/data" \
  --exclude "$BRIDGE_NAME/.env*" \
  --exclude '**/.env*' \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  -C "$BRIDGE_PARENT" "$BRIDGE_NAME"

# 4) Rotación: conservar las $KEEP copias más nuevas de cada prefijo
rotate "bridge_db_" "$DB_FILE"
rotate "bridge_CODE_" "$CODE_FILE"
for f in .meli_tokens.json amazon_credentials.json .env.meli; do
  rotate "$f" "$BACKUP_PATH/${f//\//_}_$DATE"
done

echo "Backup completed successfully."
