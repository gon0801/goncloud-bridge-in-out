#!/usr/bin/env bash
set -euo pipefail
umask 077

ENV_FILE="/mnt/data/appdata/bridge/.env.meli"
TOK_FILE="/mnt/data/appdata/bridge/data/.meli_tokens.json"
LOG_FILE="/mnt/data/appdata/bridge/data/meli_token_refresh.log"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

if [ ! -r "$ENV_FILE" ]; then
  echo "$(ts) ERROR env_file_not_readable=$ENV_FILE" >>"$LOG_FILE"
  exit 1
fi
source "$ENV_FILE"

if [ ! -r "$TOK_FILE" ]; then
  echo "$(ts) ERROR tokens_file_not_readable=$TOK_FILE" >>"$LOG_FILE"
  exit 1
fi

REFRESH="$(jq -r '.refresh_token // empty' "$TOK_FILE" 2>/dev/null || true)"
if [ -z "$REFRESH" ] || [ "$REFRESH" = "null" ]; then
  echo "$(ts) ERROR refresh_token_missing tok_file=$TOK_FILE" >>"$LOG_FILE"
  exit 1
fi

TMP="/tmp/refresh_resp.json"
curl -sS -X POST "https://api.mercadolibre.com/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "client_id=${MELI_CLIENT_ID}" \
  --data-urlencode "client_secret=${MELI_CLIENT_SECRET}" \
  --data-urlencode "refresh_token=${REFRESH}" \
  > "$TMP"

AT="$(jq -r '.access_token // empty' "$TMP" 2>/dev/null || true)"
RT="$(jq -r '.refresh_token // empty' "$TMP" 2>/dev/null || true)"
if [ -z "$AT" ]; then
  echo "$(ts) ERROR refresh_failed resp=$(cat "$TMP" | tr -d "\n" | head -c 220)" >>"$LOG_FILE"
  exit 1
fi

BK="${TOK_FILE}.BK.$(date -u +%Y%m%dT%H%M%SZ)"
cp -a "$TOK_FILE" "$BK"

cp -a "$TMP" "$TOK_FILE"
chown gon:gon "$TOK_FILE"
chmod 600 "$TOK_FILE"

echo "$(ts) OK access_prefix=${AT:0:12} refresh_prefix=${RT:0:12} backup=$(basename "$BK")" >>"$LOG_FILE"
