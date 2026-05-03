#!/usr/bin/env bash
# Bug #1 mitigation — verificador de drift entre tools/ y data/.
# Uso: bash tools/check_tools_data_drift.sh
#
# Hasta que decidamos un canónico (data/ es lo que corre via docker exec,
# tools/ es lo que el repo versiona), ambos deben estar sincronizados.
# Este script reporta archivos que difieren para que el operador decida
# cuál lado promover.
#
# El audit Bug #1 recomendaba symlink, pero data/ está bind-mounted en
# bridge-amazon-inbound-worker y bridge-inbound-worker; convertirlo a
# symlink requiere coordinar con docker compose y verificar que el
# bind-mount sigue resolviendo correctamente.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

drift=0
for f in tools/*.py tools/*.sh; do
  [ -f "$f" ] || continue
  base=$(basename "$f")
  if [ -f "data/$base" ]; then
    if ! diff -q "$f" "data/$base" >/dev/null 2>&1; then
      echo "DRIFT: $base"
      drift=$((drift + 1))
    fi
  fi
done

if [ "$drift" -eq 0 ]; then
  echo "OK: tools/ y data/ están sincronizados."
  exit 0
fi
echo
echo "$drift archivo(s) con drift. Acción sugerida:"
echo "  1) Revisar diff: diff tools/<file> data/<file>"
echo "  2) Decidir cuál lado tiene la versión correcta (típico: data/)"
echo "  3) cp data/<file> tools/<file>  (o viceversa)"
echo "  4) git add tools/<file> && commit"
exit 1
