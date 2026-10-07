#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${FALDA_FAMILY_PROXY_CONFIG:-$HERE/runtime/family-proxy.json}"
[ -f "$CONFIG" ] || { echo "missing $CONFIG; run ./generate_config.py" >&2; exit 2; }
export FALDA_FAMILY_PROXY_BIND="${FALDA_FAMILY_PROXY_BIND:-127.0.0.1}"
export FALDA_FAMILY_PROXY_PORT="${FALDA_FAMILY_PROXY_PORT:-18079}"
export FALDA_FAMILY_UPSTREAM="${FALDA_FAMILY_UPSTREAM:-http://127.0.0.1:18078}"
export FALDA_FAMILY_AUDIT="${FALDA_FAMILY_AUDIT:-$HERE/runtime/audit.ndjson}"
exec python3 "$HERE/proxy.py" --config "$CONFIG"
