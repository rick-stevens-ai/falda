# FALDA Access Proxy

Controlled public front-end that lets **external demo agents** use FALDA
shared-memory pools **without** exposing the unauthenticated FALDA gateway.

## The problem it solves

The FALDA gateway (`:8078`) trusts the `tenant` field in the request body —
there is no inbound auth. On a loopback/tailnet that's fine. But a demo partner
(e.g. Crush, Somm) needs a reachable endpoint, and we cannot hand out an
endpoint where the caller picks their own identity and can read/write anyone's
private store.

## The design

```
external agent ──HTTPS + Bearer token──▶ proxy (public :8444)
                                           │  token → tenant map
                                           │  tenant CLAMP (anti-spoof)
                                           │  route + pool allowlist
                                           ▼
                                 FALDA gateway (tailnet 100.87.221.1:8078)
```

Runs on a public VPS that is also on our tailnet (e.g. `chicago-2`,
`103.101.203.226`, tailnet `100.72.226.1`), so it reaches FALDA privately.

### Controls enforced by the proxy

1. **TLS** termination (self-signed; clients use `-k`, same as the CELS model
   front-end already on the box).
2. **Bearer-token auth** — unknown/missing token → `401`.
3. **Tenant clamp** — the caller's `tenant` field is discarded and replaced with
   the tenant bound to their token. Impersonation is impossible.
4. **Route allowlist** — only `/stream/add`, `/stream/search`, `/atoms/upsert`,
   `/atoms/search`, `/atoms/query`, `/pools/mine`, `/health`. Pool admin
   (`/pools/declare`, `/pools/grant`) and core/scenes stay internal-only.
5. **Pool allowlist per token** — a token may address only `self` or pools in
   its allowed set.

## Files

- `falda_access_proxy.py` — the proxy (stdlib only, Python 3.8+).
- `falda_proxy_tokens.example.json` — token-map template. **Real token file is
  git-ignored** (`falda_proxy_tokens.json`).
- `deploy_chicago2.sh` — provisions cert + service on chicago-2.

## Setup

```bash
# 1. cert (self-signed)
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout falda_proxy.key -out falda_proxy.crt -subj "/CN=falda-proxy"

# 2. tokens
cp falda_proxy_tokens.example.json falda_proxy_tokens.json
#   -> fill in opaque tokens (e.g. `openssl rand -hex 24`) and tenant/pools

# 3. run
FALDA_PROXY_PORT=8444 python3 falda_access_proxy.py
```

## Client usage (what a demo agent runs)

```bash
BASE=https://103.101.203.226:8444
TOKEN=<issued-token>
POOL=external-demo

# write an atom (synchronous, immediately searchable)
# NOTE: the field is `content` (NOT `text`). The gateway's /atoms/upsert schema is
#   {id?, type?, content, background?}. Sending `text` leaves content undefined and
#   the embed call fails with `embeddings 422: Field required body.input`.
curl -sk $BASE/atoms/upsert -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"pool\":\"$POOL\",\"content\":\"hello from the demo agent\",\"type\":\"episodic\"}"

# semantic search
curl -sk $BASE/atoms/search -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"pool\":\"$POOL\",\"query\":\"hello\",\"limit\":5}"

# ingest a conversation turn into the stream (async, distilled later)
curl -sk $BASE/stream/add -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"pool\":\"$POOL\",\"session_id\":\"demo1\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"

# list pools this token can reach
curl -sk $BASE/pools/mine -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{}'
```

Allowed routes for external tokens: `/atoms/upsert`, `/atoms/search`,
`/atoms/query`, `/stream/add`, `/stream/search`, `/pools/mine`, `/health`.

Note: the client never sends a `tenant` field — even if it does, the proxy
overrides it. The token *is* the identity.
