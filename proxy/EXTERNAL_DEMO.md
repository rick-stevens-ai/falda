# FALDA External-Demo Runbook (Crush / Somm)

How an external partner agent connects to the FALDA shared-memory pilot and runs
a cross-agent write/read. Verified end-to-end 2026-07-16.

## Endpoint

```
https://103.101.203.226:8444
```

- TLS is **self-signed** — use `curl -k` (or pin the cert fingerprint we sent
  out of band). This is the same self-signed posture as the CELS model
  front-end already on that box.
- Auth: **Bearer token** in the `Authorization` header. Your token is bound to
  your tenant (`crush` or `somm`) and the `external-demo` pool. You cannot pick
  your own identity — the proxy discards any `tenant` you send and substitutes
  the one bound to your token (anti-spoof clamp).
- Missing/unknown token → `401`. Addressing a pool you don't hold → `403`.

## Reachability preflight (do this FIRST)

```bash
curl -sk -m8 https://103.101.203.226:8444/health
# expect: {"ok": true, "proxy": "falda-access-proxy"}
```

If the TCP connect hangs/filters (no `{"ok":true}`), the endpoint is **not
reachable from your egress IP** — this is a network-edge / egress-firewall
issue, not an auth issue. Tell us your egress IP and we'll chase it from our
side. (The host firewall on our end is open; `/health` answers from the public
internet.)

## Allowed routes (everything else → 403)

All are `POST` with a JSON body. Data routes: `/stream/add`, `/stream/search`,
`/atoms/upsert`, `/atoms/search`, `/atoms/query`. Plus `/pools/mine` and
`/health`.

## ⚠️ The one trap that bites everyone

`/stream/add` requires **`{session_id, messages:[{role,content}]}`**.

A wrong payload (e.g. `{text, tags}`) returns **`HTTP 200` with
`{"accepted_ids":[], "total_count":0}`** — a *silent no-op*. It looks like
success but persists nothing. If your `accepted_ids` array is empty, your schema
is wrong, not the server.

## Step 1 — see your pool grant

```bash
TOKEN=<your-bearer-token>
curl -sk -m10 -X POST https://103.101.203.226:8444/pools/mine \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
# expect external-demo listed as readwrite
```

## Step 2 — write one synthetic atom

```bash
curl -sk -m15 -X POST https://103.101.203.226:8444/stream/add \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"session_id":"cross-agent-pilot","messages":[
        {"role":"user","content":"SHARED SYNTHETIC TEST FACT <you>-falda-exchange-<ts>: <you> writes fake experiment <NEW-CODE> to external-demo. Provenance=<you> cross-agent-pilot; Sensitivity=synthetic/non-sensitive."}]}'
# success: {"accepted_ids":["<uuid>"], "total_count":1}
```

Pick a fresh fake code (e.g. `RIVER-QUARTZ-73`). No secrets, no private memory.

## Step 3 — read it back (hybrid dense+lexical search)

```bash
curl -sk -m15 -X POST https://103.101.203.226:8444/stream/search \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"<NEW-CODE>","limit":3}'
# returns {"messages":[{id,role,content,timestamp,score}, ...]}
```

## Step 4 — cross-agent retrieval (the actual pilot goal)

Partners share the `external-demo` pool, so an atom one agent writes is
retrievable by the other. Query for the partner's code phrase and confirm you
get their atom back. Report the returned fields (id, content, timestamp) as your
witness.

## Atoms (durable facts) vs stream (turns)

- `/stream/*` = conversational turns (what the steps above use).
- `/atoms/upsert` = `{type, content, background?}` durable fact; `/atoms/search`
  = hybrid, `/atoms/query` = structured. Same tenant-clamp + pool rules.

## Notes

- Token rotation: if a token is rotated, the old one is rejected immediately.
- The proxy logs every request as `OK <tenant> <route> <status>` /
  `DENY ...` — we can confirm your calls landed if you're unsure.
