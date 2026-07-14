# Authenticating proxy for the FALDA gateway

The gateway trusts the `tenant` field in the request body:

```
// gateway.ts
const tenant = b.tenant ?? DEFAULT_TENANT;
```

On loopback that is fine — the only caller is the local agent. But the moment the
gateway is reachable by anything else (a shared multi-agent instance, a tailnet
peer, a container network), **any caller can name any tenant** and read or write
that tenant's store. There is no authentication and no binding of tenant identity
outside the payload.

Note that FALDA's tenant isolation is *physical*: each `(tenant, "self")` is a
separate SQLite file + blob dir, and each pool is its own file (see `pools.ts`).
That design defeats **accidental** cross-tenant leaks — there is no shared table
with a tenant column that a forgotten `WHERE` clause could spill. What it does not
defeat is a **spoofed** tenant field: name someone else's tenant and the resolver
opens their file directly.

`falda-authproxy.mjs` is an opt-in trust boundary that closes that gap without
changing the gateway.

## What it does

1. Requires `Authorization: Bearer <token>` on every data/pool route
   (`/stream/`, `/atoms/`, `/scenes/`, `/core/`, `/pools/`). Missing or unknown
   token → `401`.
2. Maps the token to a fixed tenant via a server-side table. The mapping stores
   only the SHA-256 hash of each token, never the raw secret.
3. **Overwrites `body.tenant`** with the token-bound tenant before forwarding.
   Whatever the client put in the field is discarded — this is what makes the
   tenant unspoofable.
4. Forwards to the loopback-only gateway. The gateway never faces the network;
   the proxy is the only thing bound outward.

`GET /healthz` passes through unauthenticated (no tenant, no data).

Token comparison uses `crypto.timingSafeEqual` over the hashes to avoid timing
side-channels. Pure Node stdlib — no dependencies.

## Minting tokens

```
node falda-authproxy-token.mjs <tenant>
```

Generates a 32-byte random token, prints the raw value to stdout **once**, and
persists only its SHA-256 hash → tenant into the token map (`0600`,
`~/.falda/authproxy.tokens.json` by default, override with `FALDA_AUTH_TOKENS`).
The raw secret is never written to disk, so it cannot leak from the file. If lost,
mint a new one.

## Running

```
# gateway stays loopback-only (the default)
FALDA_EMBED_BASE_URL=... node --import tsx src/gateway.ts    # 127.0.0.1:8077

# proxy is the only outward-facing listener
node falda-authproxy.mjs                                     # 127.0.0.1:8078
```

Environment:

| var                 | default                                   | meaning                     |
|---------------------|-------------------------------------------|-----------------------------|
| `FALDA_AUTH_PORT`   | `8078`                                    | proxy listen port           |
| `FALDA_AUTH_HOST`   | `127.0.0.1`                               | proxy bind address          |
| `FALDA_GATEWAY_URL` | `http://127.0.0.1:8077`                   | upstream gateway            |
| `FALDA_AUTH_TOKENS` | `~/.falda/authproxy.tokens.json`          | token→tenant map (hashes)   |

To expose the proxy to a trusted network, set `FALDA_AUTH_HOST` accordingly and
keep the gateway on `127.0.0.1`. Terminating TLS (or running behind a TLS-
terminating reverse proxy) is recommended for any non-loopback bind.

## Verifying isolation

Four properties, all checkable with `curl`:

1. No token → `401`.
2. A valid token performs operations as its bound tenant.
3. A token for tenant *A* that puts `"tenant":"B"` in the body still acts as *A* —
   it cannot read or write *B*'s store. (Spoof blocked.)
4. A token reading its own tenant's store works normally.

```sh
A=$(node falda-authproxy-token.mjs alice)
B=$(node falda-authproxy-token.mjs bob)
P=http://127.0.0.1:8078

# 1. no token
curl -s -o /dev/null -w '%{http_code}\n' -X POST $P/atoms/search \
  -H 'content-type: application/json' -d '{"query":"x"}'         # 401

# 2. alice plants a marker
curl -s -X POST $P/atoms/upsert -H "authorization: Bearer $A" \
  -H 'content-type: application/json' \
  -d '{"id":"canary","content":"planted by alice"}'

# 3. bob spoofs tenant=alice — must NOT see the canary
curl -s -X POST $P/atoms/search -H "authorization: Bearer $B" \
  -H 'content-type: application/json' \
  -d '{"tenant":"alice","query":"canary"}'                       # no hit

# 4. alice sees her own canary
curl -s -X POST $P/atoms/search -H "authorization: Bearer $A" \
  -H 'content-type: application/json' \
  -d '{"query":"canary"}'                                        # hit
```

## Scope / non-goals

- **Additive and non-breaking.** The gateway is unchanged; loopback deployments
  behave exactly as before. The proxy is opt-in for anyone exposing FALDA beyond
  loopback.
- Authentication and tenant-binding only. It does **not** add provenance fields,
  an append-only event log, tombstones/supersession, or export — those remain
  open items for the store layer.
- Bearer tokens are a pragmatic first factor. mTLS (client-cert → tenant) fits the
  same overwrite-the-tenant model if stronger caller identity is required.
