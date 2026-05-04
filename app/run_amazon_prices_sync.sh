#!/bin/bash
# AMAZON PRICES + FBA INVENTORY SYNC — Runner script
# Ejecuta amazon_prices_sync.py dentro de bridge-amazon-inbound-worker
# (tiene la dep `requests` y monta /data con bridge.db).

set -e

LOG_PREFIX="[amazon-prices-sync]"

echo "$LOG_PREFIX Starting at $(date -Iseconds)"

docker exec bridge-amazon-inbound-worker python3 /data/amazon_prices_sync.py

echo "$LOG_PREFIX Completed at $(date -Iseconds)"
