#!/usr/bin/env bash
set -Eeuo pipefail
SETAPI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Instale Python 3 para executar o instalador.' >&2
  exit 1
fi
exec python3 "$SETAPI_DIR/scripts/install.py" "$@"
