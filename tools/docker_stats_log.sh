#!/usr/bin/env bash
# D5.8: Log Docker container stats for bridge containers.
# Run from host (not inside container). Suggested cron: */5 * * * *
# Example cron entry:
#   */5 * * * * /mnt/data/appdata/bridge/tools/docker_stats_log.sh >> /mnt/data/appdata/bridge/data/docker_stats.log 2>&1
set -euo pipefail

LOG=/mnt/data/appdata/bridge/data/docker_stats.log
MAX_LINES=10000

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

docker stats --no-stream --format \
  "$(ts) {{.Name}}\tcpu={{.CPUPerc}}\tmem={{.MemUsage}}\tnet={{.NetIO}}\tblock={{.BlockIO}}" \
  bridge-api bridge-inbound-worker bridge-amazon-inbound-worker bridge-worker bridge-redis \
  2>/dev/null || true

# Rotate: keep last MAX_LINES lines
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LINES" ]; then
  tail -n "$MAX_LINES" "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
