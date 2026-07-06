# KUKLA_DELTA: lock the embedding model at gateway boot

**Author:** Kukla (m1) · **Date:** 2026-07-06 · **For:** Ollie to review/merge on cherryrd `~/code/stratus` (Falda repo)

## Why
The gateway takes `FALDA_EMBED_MODEL` + `FALDA_DIM` purely from env at boot with **no verification** that they match what the store's vec tables were built with. Two silent failure modes:
- **Different dim** -> sqlite-vec rejects inserts with an opaque error (vec tables are `float[1536]`).
- **Same dim, different model** (e.g. another 1536-dim embedder) -> inserts SUCCEED but recall is **silently corrupted** — vectors from two models are not comparable. This is the dangerous one.

Rick's ask: "lock in the embedding model." The lock must live where embeddings are produced (the gateway), not in the distiller (which only calls the gateway).

## Ground truth already laid down
I wrote the authoritative manifest at the store root (all tenants share one embedder):
`~/.openclaw/memory-falda/root/EMBEDDING.json`
```json
{"model":"text-embedding-3-small","dim":1536,"embed_mode":"remote","embed_base_url_kind":"argo-proxy","locked":true}
```

## Patch (src/gateway.ts) — additive, minimal
Add after the `selectEmbedder()` definition and BEFORE `const pools = new PoolManager(...)`:

```ts
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";

/** Lock the embedding model+dim to what the store was built with.
 *  First boot writes the manifest; every later boot verifies and refuses
 *  to serve on mismatch (prevents silent recall corruption). */
function enforceEmbeddingLock() {
  const model = process.env.FALDA_EMBED_MODEL ?? "nomic-embed-text";
  const mode  = (process.env.FALDA_EMBED ?? "").toLowerCase() ||
                (process.env.FALDA_EMBED_BASE_URL ? "remote" : "local");
  const path  = join(ROOT, "EMBEDDING.json");
  const cur   = { model, dim: DIM, embed_mode: mode };
  if (!existsSync(ROOT)) mkdirSync(ROOT, { recursive: true });
  if (!existsSync(path)) {
    writeFileSync(path, JSON.stringify({ ...cur, locked: true, locked_at: new Date().toISOString().slice(0,10) }, null, 2));
    console.log(`FALDA embedding lock: initialized ${path} model=${model} dim=${DIM}`);
    return;
  }
  const locked = JSON.parse(readFileSync(path, "utf8"));
  const mismatch: string[] = [];
  if (locked.model !== undefined && locked.model !== model) mismatch.push(`model ${locked.model} != ${model}`);
  if (locked.dim   !== undefined && Number(locked.dim) !== DIM) mismatch.push(`dim ${locked.dim} != ${DIM}`);
  if (mismatch.length) {
    console.error(`FATAL: embedding config does not match locked store manifest (${path}): ${mismatch.join("; ")}. ` +
      `Serving would corrupt recall. Fix FALDA_EMBED_MODEL/FALDA_DIM to match, or re-embed the store and update the manifest.`);
    process.exit(1);
  }
  console.log(`FALDA embedding lock: OK model=${model} dim=${DIM}`);
}
enforceEmbeddingLock();
```

## Restart
This needs a gateway restart to take effect — **your call on timing** (idle window). Recycle:
`launchctl kickstart -k gui/$(id -u)/ai.falda.cherryrd.gateway`

After restart, boot log should show `FALDA embedding lock: OK model=text-embedding-3-small dim=1536`.

## What this delta does NOT propose
- No rename of tenants or repo dir (separate cleanup — see below).
- No change to embedder selection logic or recall behavior.
- No re-embedding of any store.

## Separately noted (namespace cleanup — NOT part of this delta, your host/call)
Stale STRATUS artifacts still loaded on cherryrd:
- `com.stevens.stratus-gateway` (:8077 old dualrun gateway, PID live) — retire when dualrun no longer needed.
- `ai.stratus.cherryrd.distiller` — **crash-looping** (plist references `stratus_distiller.py`, renamed to `falda_distiller.py`). It targeted `default` on the OLD :8077 gateway. Kukla now runs its own `default`-independent distiller for tenant `kukla` on m1 (`ai.falda.m1.distiller-kukla`). If you want `default` (your 7780-turn history) distilled again, point a distiller at :8078 tenant `default` with the tenant-aware `falda_distiller.py` (already tenant-aware in the m1 copy — I can send it).
- `ai.stratus.cherryrd.tap`, `ai.stratus.cherryrd.gateway` — errored/unused stubs.
