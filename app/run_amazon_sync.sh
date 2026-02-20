#!/bin/bash
# Amazon Outbound Stock Sync
# Ejecuta snapshotter Odoo -> Bridge -> Redis

set -e

echo "[$(date -Is)] Amazon sync started"

# 1) Snapshot Odoo -> Bridge (lee sell_on_amazon_fbm=True)
docker exec bridge-api python3 /app/snapshotter_odoo_amazon_run_once.py

# 2) Snapshot Bridge -> Redis (encola jobs)
docker exec bridge-api python3 /app/snapshotter_amazon_run_once.py

echo "[$(date -Is)] Amazon sync completed"
