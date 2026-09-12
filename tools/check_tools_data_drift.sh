#!/usr/bin/env bash
# Verificador de drift entre tools/ y data/.
#
# La logica vive en check_tools_data_drift.py, que compara por AST en vez de
# por texto. Este wrapper existe porque la ruta .sh ya esta citada en CLAUDE.md
# y en los runbooks.
#
# Uso: bash tools/check_tools_data_drift.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/tools/check_tools_data_drift.py" "$ROOT"
