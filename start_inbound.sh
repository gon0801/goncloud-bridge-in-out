#!/bin/sh
set -e

echo "[start_inbound] running schema migrator..."
BRIDGE_DB=/data/bridge.db python3 /app/migrate_rev4_schema.py

echo "[start_inbound] starting inbound worker..."
exec python /app/inbound_worker.py
