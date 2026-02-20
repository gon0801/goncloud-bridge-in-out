#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# run_meli_sync.sh (CANÓNICO)
# - Carga ODOO_* desde /mnt/data/appdata/bridge/.env
# - Ejecuta snapshotters DENTRO de bridge-api (tiene deps + red docker)
# - Lock para evitar doble ejecución
# =========================================================

ROOT="/mnt/data/appdata/bridge"
ENV_FILE="$ROOT/.env"
LOG="$ROOT/data/meli_sync.log"

LOCK="/tmp/meli_sync.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SKIP: meli_sync ya estaba corriendo" >> "$LOG"
  exit 0
fi

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "[$(ts)] START meli_sync" >> "$LOG"

# 1) Cargar env de Odoo (host)
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[$(ts)] FATAL: no existe $ENV_FILE" >> "$LOG"
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# 2) Validación dura
: "${ODOO_URL:?}"
: "${ODOO_DB:?}"
: "${ODOO_USER:?}"
: "${ODOO_PASSWORD:?}"

# 3) Ejecutar snapshotters dentro de bridge-api (NO host python)
docker exec -i \
  -e ODOO_URL="$ODOO_URL" \
  -e ODOO_DB="$ODOO_DB" \
  -e ODOO_USER="$ODOO_USER" \
  -e ODOO_PASSWORD="$ODOO_PASSWORD" \
  bridge-api python3 /app/snapshotter_odoo_meli_run_once.py >> "$LOG" 2>&1

docker exec -i bridge-api python3 /app/snapshotter_meli_run_once.py >> "$LOG" 2>&1

echo "[$(ts)] DONE meli_sync" >> "$LOG"
