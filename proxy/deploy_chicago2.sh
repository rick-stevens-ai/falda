#!/usr/bin/env bash
# Deploy the FALDA access proxy to chicago-2 (public VPS on our tailnet).
# Idempotent. Run from repo: proxy/deploy_chicago2.sh
set -euo pipefail

HOST="${FALDA_PROXY_HOST:-chicago-2}"
PORT="${FALDA_PROXY_PORT:-8444}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== ensuring remote dir on $HOST =="
ssh "$HOST" 'mkdir -p "$HOME/falda-proxy"'
REMOTE_DIR="$(ssh "$HOST" 'echo $HOME/falda-proxy')"

echo "== copying proxy + tokens =="
scp -q "$HERE/falda_access_proxy.py" "$HOST:$REMOTE_DIR/"
if [ -f "$HERE/falda_proxy_tokens.json" ]; then
  scp -q "$HERE/falda_proxy_tokens.json" "$HOST:$REMOTE_DIR/"
else
  echo "!! no local falda_proxy_tokens.json — create it first (see example)"; exit 1
fi

echo "== generating self-signed cert on remote if absent =="
ssh "$HOST" "cd $REMOTE_DIR && [ -f falda_proxy.crt ] || openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout falda_proxy.key -out falda_proxy.crt -subj '/CN=falda-proxy' 2>/dev/null; chmod 600 falda_proxy.key falda_proxy_tokens.json"

echo "== (re)starting proxy under nohup on :$PORT =="
ssh "$HOST" "cd $REMOTE_DIR && pkill -f falda_access_proxy.py 2>/dev/null || true; sleep 1; FALDA_PROXY_PORT=$PORT nohup python3 falda_access_proxy.py > proxy.log 2>&1 & sleep 1; echo started; tail -3 proxy.log"

echo "== health check =="
ssh "$HOST" "curl -sk https://127.0.0.1:$PORT/health"; echo
echo "DONE. Public: https://\$(ssh $HOST curl -s ifconfig.me):$PORT"
