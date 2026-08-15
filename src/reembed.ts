/**
 * `falda reembed` — rebuild dense-vector indexes after switching embedding
 * model/dimension.
 *
 * Backported from rbross-hpc/falda (src/reembed.ts), adapted to our schema:
 * uses Falda.reembedAll() (stream + atoms only; no scenes tier) and our
 * embedder-selection logic, not the fork's boot.ts/runtime.ts stack.
 *
 * Why this exists: enforceEmbeddingLock (src/gateway.ts) refuses to boot a
 * server whose FALDA_EMBED_MODEL/FALDA_DIM no longer match a store's locked
 * EMBEDDING.json, and its error message says "re-embed the store and update
 * the manifest" — this is that command.
 *
 * Per store (self tenant or declared pool):
 *   1. Opens the store with the currently configured embedder (same
 *      FALDA_EMBED / FALDA_DIM env as any other FALDA process).
 *   2. Calls Falda.reembedAll() — drops/recreates stream_vec + atoms_vec at
 *      the new dim (vec0 dimension is baked into the schema) and re-embeds
 *      every stream turn + atom's existing content, unchanged.
 *   3. Rewrites root EMBEDDING.json to the new model/dim so a subsequent
 *      `falda serve` boots clean.
 *
 * MUST be run with the gateway (and any other writer) stopped.
 *
 *   falda reembed [--root=DIR] [--tenant=T | --pool=P] [--dry-run] [--yes]
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { Falda } from "./falda.js";
import { makeEmbedder, makeLocalEmbedder } from "./embedder.js";
import { listAllStores, type StoreRef } from "./stats.js";

const DIM = Number(process.env.FALDA_DIM ?? 768);

function selectEmbedder() {
  const mode = (process.env.FALDA_EMBED ?? "").toLowerCase();
  const hasRemote = !!process.env.FALDA_EMBED_BASE_URL;
  if (mode === "local") return makeLocalEmbedder(DIM);
  if (mode === "remote" || hasRemote) return makeEmbedder();
  return makeLocalEmbedder(DIM);
}

function selectStores(root: string, opts: { tenant?: string; pool?: string }): StoreRef[] {
  let stores = listAllStores(root);
  if (opts.tenant) stores = stores.filter((s) => s.scope === "self" && s.name === opts.tenant);
  if (opts.pool)   stores = stores.filter((s) => s.scope === "pool" && s.name === opts.pool);
  return stores;
}

async function reembedStore(store: StoreRef): Promise<{ store: StoreRef; stream: number; atoms: number; dim: number }> {
  const embed = selectEmbedder();
  const falda = new Falda({ dbPath: store.dbPath, blobDir: store.blobDir, embed, dim: DIM });
  try {
    const r = await falda.reembedAll((tier, done, total) => {
      if (done === total || done % 25 === 0) console.log(`  [${store.label}] ${tier}: ${done}/${total}`);
    });
    return { store, ...r };
  } finally { falda.close(); }
}

/** Unconditionally rewrite the root manifest — this command's whole purpose
 *  is to update the lock after an intentional model/dim change. */
function writeEmbeddingManifest(root: string): void {
  const model = process.env.FALDA_EMBED_MODEL ?? "";
  const manifest = { model, dim: DIM, locked: true, locked_at: new Date().toISOString().slice(0, 10) };
  fs.writeFileSync(path.join(root, "EMBEDDING.json"), JSON.stringify(manifest, null, 2));
}

function parseArgs(argv: string[]) {
  const o: { root?: string; tenant?: string; pool?: string; dryRun: boolean; yes: boolean; help: boolean } =
    { dryRun: false, yes: false, help: false };
  for (const a of argv) {
    if (a === "--dry-run") o.dryRun = true;
    else if (a === "--yes") o.yes = true;
    else if (a === "--help" || a === "-h") o.help = true;
    else if (a.startsWith("--root=")) o.root = a.slice("--root=".length);
    else if (a.startsWith("--tenant=")) o.tenant = a.slice("--tenant=".length);
    else if (a.startsWith("--pool=")) o.pool = a.slice("--pool=".length);
    else { console.error(`falda reembed: unknown argument '${a}' (see --help)`); process.exit(1); }
  }
  return o;
}

const HELP = `Usage: falda reembed [--root=DIR] [--tenant=T | --pool=P] [--dry-run] [--yes]

Rebuilds dense-vector indexes (stream_vec + atoms_vec) for every store under
FALDA_ROOT (or the one selected by --tenant/--pool) using the currently
configured embedder (FALDA_EMBED / FALDA_DIM), then updates EMBEDDING.json.

Run with the gateway STOPPED. Source tables (stream, atoms) are never touched.`;

async function main() {
  const o = parseArgs(process.argv.slice(2));
  if (o.help) { console.log(HELP); return; }
  const root = o.root ?? process.env.FALDA_ROOT ?? "./falda-data";
  const stores = selectStores(root, o);
  if (!stores.length) { console.error(`falda reembed: no stores found under ${root}`); process.exit(1); }

  console.log(`falda reembed: ${stores.length} store(s) under ${root}, target dim=${DIM}, model=${process.env.FALDA_EMBED_MODEL ?? "(unset)"}`);
  for (const s of stores) console.log(`  - ${s.label}  (${s.dbPath})`);

  if (o.dryRun) { console.log("\n--dry-run: no changes made."); return; }
  if (!o.yes) {
    console.error("\nRefusing to run without --yes. Re-run with --yes once the gateway is stopped.");
    process.exit(1);
  }

  let totalStream = 0, totalAtoms = 0;
  for (const s of stores) {
    console.log(`\nre-embedding ${s.label} ...`);
    const r = await reembedStore(s);
    totalStream += r.stream; totalAtoms += r.atoms;
    console.log(`  done: ${r.stream} stream + ${r.atoms} atoms @ dim ${r.dim}`);
  }
  writeEmbeddingManifest(root);
  console.log(`\nreembed complete: ${totalStream} stream + ${totalAtoms} atoms across ${stores.length} store(s). Manifest updated to dim=${DIM}.`);
}

main().catch((e) => { console.error("falda reembed failed:", e?.message ?? e); process.exit(1); });
