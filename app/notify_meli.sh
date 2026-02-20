#!/usr/bin/env bash
set -euo pipefail

MSG="${1:-"(sin mensaje)"}"

# Usa tu notifier canónico
echo "$MSG" | /mnt/data/logs/alerts/telegram-notifier.sh
