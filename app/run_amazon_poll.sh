#!/bin/bash
# AMAZON ORDERS POLL — Runner script
# Ejecuta el polling dentro del contenedor bridge-amazon-inbound-worker

set -e

LOG_PREFIX="[amazon-poll]"

echo "$LOG_PREFIX Starting Amazon orders poll at $(date -Iseconds)"

# Ejecutar dentro del contenedor
docker exec bridge-amazon-inbound-worker python3 /data/amazon_orders_poll.py --days 2 --marketplace BOTH

echo "$LOG_PREFIX Completed at $(date -Iseconds)"
