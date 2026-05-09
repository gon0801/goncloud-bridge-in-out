#!/usr/bin/env bash
# D6.9: Sync bridge backups to offsite storage via rclone.
# Prerequisites:
#   1. Install rclone: curl https://rclone.org/install.sh | bash
#   2. Configure remote: rclone config  (name it "bridge-offsite")
#      Supports S3, Backblaze B2, Cloudflare R2, Google Drive, etc.
#   3. Set RCLONE_REMOTE below or export env var before calling.
#
# Suggested cron (daily at 03:30 UTC, after bridge_backup.sh at 03:00):
#   30 3 * * * /mnt/data/appdata/bridge/tools/bridge_backup_offsite.sh >> /mnt/data/appdata/bridge/data/backup_offsite.log 2>&1
set -euo pipefail

RCLONE_REMOTE="${RCLONE_REMOTE:-bridge-offsite}"
LOCAL_DIR="/mnt/data/backups/bridge"
REMOTE_PATH="${RCLONE_REMOTE}:goncloud-bridge/backups"
LOG=/mnt/data/appdata/bridge/data/backup_offsite.log

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

if ! command -v rclone &>/dev/null; then
  echo "$(ts) ERROR rclone not installed. Run: curl https://rclone.org/install.sh | bash"
  exit 1
fi

if [ ! -d "$LOCAL_DIR" ]; then
  echo "$(ts) ERROR local backup dir not found: $LOCAL_DIR"
  exit 1
fi

echo "$(ts) INFO starting offsite sync local=$LOCAL_DIR remote=$REMOTE_PATH"
rclone copy "$LOCAL_DIR" "$REMOTE_PATH" \
  --include "*.db" \
  --transfers 2 \
  --retries 3 \
  --log-level INFO

echo "$(ts) OK offsite sync complete"
