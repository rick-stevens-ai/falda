# Isolated FALDA family demo

This harness exercises five authenticated principals (`yeil`, `ikto`, `tsisdu`,
`kukla`, `ollie`) against private stores and the explicitly shared
`family-room` pool. Everything is confined to `demo/family/runtime`; launchers
refuse a store outside this directory and use loopback only. **Do not point the
orchestrator's admin URL at a production gateway.** It rejects non-loopback
URLs.

## Prerequisite: real embeddings

Start LM Studio's OpenAI-compatible server on `127.0.0.1:1234` with model
`text-embedding-nomic-embed-text-v1.5`, returning 768-dimensional embeddings.
The backend launcher intentionally sets `FALDA_EMBED=remote`; there is no local
fake semantic fallback.

Verify it before proceeding:

```bash
curl -fsS http://127.0.0.1:1234/v1/embeddings \
  -H 'content-type: application/json' \
  -d '{"model":"text-embedding-nomic-embed-text-v1.5","input":"probe"}'
```

## Launch

From the repository root, use three terminals:

```bash
python3 demo/family/generate_config.py
./demo/family/launch_backend.sh
./demo/family/launch_proxy.sh
curl -fsS http://127.0.0.1:18079/readyz
```

The generated config is mode 0600 and ignored by git. The proxy is stdlib-only,
binds `127.0.0.1:18079` by default, clamps each bearer token to one tenant,
allows only the four tier route families plus `/pools/mine`, checks pool
allowlists, caps request bodies, and never exposes pool administration. Its
NDJSON audit records token fingerprints and metadata only—not tokens or bodies.
`/healthz` is liveness; `/readyz` checks the backend dependency; authenticated
`/whoami` reports the clamped identity.

## Full run and cleanup guarantee

```bash
python3 demo/family/orchestrator.py run --run-id rehearsal-1
```

`run` creates a deterministic manifest, declares the shared pool through the
loopback-only admin endpoint, plants one target plus exactly 20 distractors in
each private tenant and 20 in `family-room`, runs all checks, writes
`evidence/results.{json,csv}`, then cleans planted records in a `finally` block.
It tests exact and paraphrase search, cross-tenant negative recall, spoof clamp,
shared recall, T0/T1/T2/T3, deleting a nonexistent atom, and latency.
The manifest is byte-stable for the same explicit `--run-id` and `--seed`;
without `--run-id`, a unique timestamped run ID is generated to avoid collisions.

For a real restart boundary:

```bash
python3 demo/family/orchestrator.py pre-restart
# restart backend and proxy, preserving demo/family/runtime/store
python3 demo/family/orchestrator.py post-restart
```

`pre-restart` deliberately retains test data after a successful phase;
`post-restart` verifies persistence and then cleans it. Failed/interrupted phases
run cleanup in `finally`; if services were unavailable during that attempt, rerun
`python3 demo/family/orchestrator.py cleanup` with the same manifest. The shared pool registry and
empty isolated store directories may remain under `runtime/`; remove the whole
ignored runtime directory only after both services stop.

The harness never uses `/atoms/query` to claim semantic success. Semantic checks
use `/atoms/search` and preserve the returned fused `score`. Current FALDA API
responses do not identify whether dense or lexical retrieval contributed to a
hit. Evidence labels this **`API_OBSERVABILITY_GAP`** and records no invented
channel provenance.

## Participant exercise

Each participant gets only their token (printed by config generation):

```bash
FALDA_FAMILY_TOKEN='...' python3 demo/family/participant.py yeil \
  --run-id family-live --private-fact '...' --paraphrase '...' --shared-fact '...'
```

The CLI authenticates with `/whoami`, plants private and optional shared facts,
tries paraphrase recall and a spoofed-tenant request, and emits a receipt. The
receipt contains SHA-256 fact hashes, a short token fingerprint, check outcomes,
and a canonical payload hash—never the secret token or fact plaintext. This is
integrity evidence (“signed-ish”), not a cryptographic signature.

## Tests

```bash
python3 -m unittest discover -s demo/family/tests -v
npm run build
```
