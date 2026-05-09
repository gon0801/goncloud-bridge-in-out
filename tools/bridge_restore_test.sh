#!/usr/bin/env bash
# D6.10: Monthly restore test — verifies latest bridge.db backup is valid.
# Checks: file exists, non-empty, SQLite integrity, table count sanity.
#
# Suggested cron (1st of each month at 04:00 UTC):
#   0 4 1 * * /mnt/data/appdata/bridge/tools/bridge_restore_test.sh >> /mnt/data/appdata/bridge/data/restore_test.log 2>&1
set -euo pipefail

BACKUP_DIR="/mnt/data/backups/bridge"
RESTORE_TMP="/tmp/bridge_restore_test_$$.db"
LOG=/mnt/data/appdata/bridge/data/restore_test.log

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
fail() { echo "$(ts) FAIL $*"; exit 1; }
ok()   { echo "$(ts) OK   $*"; }

echo "$(ts) INFO === bridge restore test start ==="

# Find latest backup
LATEST=$(find "$BACKUP_DIR" -name "*.db" -newer /dev/null 2>/dev/null | sort | tail -1)
[ -z "$LATEST" ] && fail "no backup found in $BACKUP_DIR"
ok "latest backup: $LATEST ($(du -h "$LATEST" | cut -f1))"

# Copy to tmp
cp "$LATEST" "$RESTORE_TMP"
trap 'rm -f "$RESTORE_TMP"' EXIT

# Integrity check
INTEGRITY=$(sqlite3 "$RESTORE_TMP" "PRAGMA integrity_check;" 2>&1)
[ "$INTEGRITY" != "ok" ] && fail "integrity_check failed: $INTEGRITY"
ok "integrity_check: ok"

# Table count sanity (production has 37+)
TABLE_COUNT=$(sqlite3 "$RESTORE_TMP" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
[ "$TABLE_COUNT" -lt 15 ] && fail "suspicious table count: $TABLE_COUNT (expected >=15)"
ok "tables: $TABLE_COUNT"

# Key table row counts
for TABLE in processed_inbound_events amazon_processed_events bridge_settings sku_mapping; do
  ROWS=$(sqlite3 "$RESTORE_TMP" "SELECT COUNT(*) FROM $TABLE;" 2>/dev/null || echo "N/A")
  ok "  $TABLE rows=$ROWS"
done

echo "$(ts) OK === restore test PASSED ==="
