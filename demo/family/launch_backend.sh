#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
RUNTIME="${FALDA_FAMILY_RUNTIME:-$HERE/runtime}"
mkdir -p "$RUNTIME/store"
STORE_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$RUNTIME/store")
HERE_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$HERE")
case "$STORE_REAL" in "$HERE_REAL"/*) ;; *) echo "refusing store outside demo/family: $RUNTIME/store" >&2; exit 2;; esac
export FALDA_ROOT="$RUNTIME/store"
export FALDA_PORT="${FALDA_PORT:-18078}"
export FALDA_BIND_HOST="${FALDA_BIND_HOST:-127.0.0.1}"
export FALDA_DEFAULT_TENANT=family-demo-default
export FALDA_EMBED=remote
export FALDA_EMBED_BASE_URL="${FALDA_EMBED_BASE_URL:-http://127.0.0.1:1234/v1}"
export FALDA_EMBED_API_KEY="${FALDA_EMBED_API_KEY:-x}"
export FALDA_EMBED_MODEL="${FALDA_EMBED_MODEL:-text-embedding-nomic-embed-text-v1.5}"
export FALDA_DIM=768
exec "$ROOT/node_modules/.bin/tsx" "$ROOT/src/gateway.ts"
