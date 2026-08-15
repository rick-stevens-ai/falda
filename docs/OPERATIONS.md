# FALDA operations: `falda stats` and `falda reembed`

Two offline operator tools, back-ported (adapted) from
[rbross-hpc/falda](https://github.com/rbross-hpc/falda) to our 2-tier
(T0 stream + T1 atoms) schema.

## `falda stats` — read-only deployment report

Safe against a **live** deployment: never constructs a `PoolManager`/`Falda`,
never touches the embedder or a token. Walks `FALDA_ROOT` directly and opens
each SQLite read-only.

```bash
falda stats                     # human-readable, every store
falda stats --json              # machine-readable
falda stats --tenant=kukla      # scope to one tenant's self store
falda stats --pool=lucid        # scope to one shared pool
FALDA_ROOT=/data falda stats    # point at a specific root
```

Reports per store: T0 stream count, T1 atoms count, on-disk size, and
per-store warnings. The most valuable warnings:

- **`stream_vec N < stream M — missing embeddings`** — the exact HTTP-500
  fault class we chase: rows landed in the source table but their embeddings
  never made it into the vector index. Fix with `falda reembed`.
- **`embedding-lock mismatch: manifest dim X != FALDA_DIM Y`** — the running
  config disagrees with the store's locked `EMBEDDING.json`. Serving would
  corrupt recall.

**Exit code:** `0` clean, `1` if any error-level warning is present — so it
works as a health check in a cron/monitor.

## `falda reembed` — rebuild vector indexes after an embedding change

The migration path that `enforceEmbeddingLock`'s error message promises.
`vec0`'s dimension is baked into the table schema, so a model/dim change can't
be a row-level rewrite — the `*_vec` tables must be dropped and recreated.

```bash
# Run with the gateway STOPPED.
FALDA_DIM=1024 FALDA_EMBED_MODEL=new-model falda reembed --dry-run
FALDA_DIM=1024 FALDA_EMBED_MODEL=new-model falda reembed --yes
```

Per store it: reads every stream turn + atom's existing content, drops and
recreates `stream_vec`/`atoms_vec` at the new dim, re-embeds, then rewrites
root `EMBEDDING.json` so a subsequent `falda serve` boots clean against the
lock. **Source tables (`stream`, `atoms`) are never touched** — no content is
lost even if interrupted.

Requires `--yes` to make changes (`--dry-run` lists the targeted stores only).
Scope with `--tenant=T` / `--pool=P`; default is every store under the root.

## Boot-time embedder safeguards (`falda serve`)

Two additions to the existing embedding lock (also from rbross-hpc/falda):

- **`probeEmbedder`** — on boot with a remote embedder, calls it once and
  asserts the returned vector length == `FALDA_DIM`. Catches a down endpoint
  or one silently serving a different model/dim — *at boot*, instead of as an
  HTTP 500 on the first real write. Warns by default.
- **`FALDA_EMBED_STRICT=1`** — makes a probe failure (or an accidentally-unset
  remote embedder falling back to the offline fake) a boot FATAL instead of a
  warning. Use in production where a silent downgrade to the fake embedder is
  never acceptable.

## What was NOT back-ported

The fork's `stats`/`reembed` also cover a T2 scenes tier, a
`recall_traces.db`, and the fork's evolved atom schema — none of which exist
in this repo. Adopting those is a separate, larger decision (see the fork's
`docs/MODEL.md`). These two tools are the schema-independent subset.
