#!/usr/bin/env bash
# Refresca amazon_inventory_cache llamando al endpoint del bridge.
#
# Por que existe
# --------------
# `amazon_inventory_cache` alimenta /api/amazon/amazon-skus, la lista de SKUs
# del mapper UI. Solo se escribia a mano, via POST /api/amazon/refresh-inventory.
# Nadie lo llamaba desde el 2026-08-10.
#
# El problema no era el mapper desactualizado — era que /v1/health mide la edad
# de esa tabla y con >168h pone `ok: false`. Resultado: el health quedo en rojo
# permanente desde el 11-ago por algo cosmetico, y cuando el 24-ago se cayo el
# ingress de MercadoLibre (19 dias, ~138 entregas rechazadas por dia) el
# semaforo ya llevaba dos semanas en rojo y nadie lo miro.
#
# Autenticacion
# -------------
# El endpoint usa `require_secret`, que acepta tres factores. Este script entra
# por el segundo: la IP de origen. Llamando al puerto publicado en la interfaz
# WireGuard, el app ve el gateway de docker (172.18.0.1) y autoriza sin secreto.
# Por eso aca no hay credenciales que rotar ni que fugar en el log.
#
# Instalacion (ver tools/systemd/amazon-inventory-refresh.{service,timer})
#   sudo cp tools/amazon_inventory_refresh.sh /mnt/data/appdata/bridge/tools/
#   sudo chmod +x /mnt/data/appdata/bridge/tools/amazon_inventory_refresh.sh
#   sudo cp tools/systemd/amazon-inventory-refresh.* /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now amazon-inventory-refresh.timer

set -euo pipefail

BRIDGE_BASE="${BRIDGE_BASE:-/mnt/data/appdata/bridge}"
BRIDGE_HOST="${BRIDGE_HOST:-10.13.13.1}"
BRIDGE_PORT="${BRIDGE_PORT:-8099}"
LOG_FILE="${LOG_FILE:-${BRIDGE_BASE}/data/amazon_inventory_refresh.log}"
TIMEOUT="${TIMEOUT:-180}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

HTTP=$(curl -s -o "$BODY" -w "%{http_code}" \
  --max-time "$TIMEOUT" \
  -X POST "http://${BRIDGE_HOST}:${BRIDGE_PORT}/api/amazon/refresh-inventory" \
  || echo "000")

# El endpoint devuelve 200 con {"ok": false} cuando la SP-API responde vacia:
# en ese caso NO pisa el cache vivo (es deliberado). Hay que mirar el cuerpo,
# no solo el codigo, o un fallo real pasa por exitoso.
if [ "$HTTP" != "200" ]; then
  echo "$(ts) ERROR http=$HTTP body=$(head -c 200 "$BODY")" >>"$LOG_FILE"
  exit 1
fi

if grep -q '"ok"[[:space:]]*:[[:space:]]*false' "$BODY"; then
  echo "$(ts) ERROR endpoint_ok_false body=$(head -c 200 "$BODY")" >>"$LOG_FILE"
  exit 1
fi

echo "$(ts) OK $(head -c 200 "$BODY")" >>"$LOG_FILE"
