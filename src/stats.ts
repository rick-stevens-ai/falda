/**
 * `falda stats` — offline, read-only deployment report.
 *
 * Backported from rbross-hpc/falda (src/stats.ts), trimmed to our 2-tier
 * (T0 stream + T1 atoms) schema — the fork also reports a scenes tier and a
 * recall_traces.db we do not have.
 *
 * Safe against a LIVE deployment: never constructs a PoolManager/Falda, never
 * touches the embedder or a token. It walks FALDA_ROOT directly (the same
 * on-disk contract PoolManager uses) and opens each SQLite read-only.
 *
 *   falda stats                       human-readable, every store
 *   falda stats --json                machine-readable
 *   falda stats --tenant=T            scope to one tenant's self store
 *   falda stats --pool=P              scope to one shared pool
 *   FALDA_ROOT=/data falda stats      point at a specific root
 *
 * Exits 1 if any error-level warning is found (usable as a health check).
 */
import * as fs from "node:fs";
import * as path from "node:path";
import Database from "better-sqlite3";

export interface StoreRef {
  scope: "self" | "pool";
  name: string;         // tenant name (self) or pool name (pool)
  label: string;        // "<name>:self" | "pool:<name>"
  dbPath: string;
  blobDir: string;
}

/** Enumerate every store under a FALDA_ROOT by walking the filesystem —
 *  self stores under tenants/<t>/self, pool stores from pools/<p>. */
export function listAllStores(root: string): StoreRef[] {
  const out: StoreRef[] = [];
  const tdir = path.join(root, "tenants");
  if (fs.existsSync(tdir)) {
    for (const t of fs.readdirSync(tdir)) {
      const db = path.join(tdir, t, "self", "falda.db");
      if (fs.existsSync(db)) out.push({ scope: "self", name: t, label: `${t}:self`, dbPath: db, blobDir: path.join(tdir, t, "self", "blobs") });
    }
  }
  const pdir = path.join(root, "pools");
  if (fs.existsSync(pdir)) {
    for (const p of fs.readdirSync(pdir)) {
      const db = path.join(pdir, p, "falda.db");
      if (fs.existsSync(db)) out.push({ scope: "pool", name: p, label: `pool:${p}`, dbPath: db, blobDir: path.join(pdir, p, "blobs") });
    }
  }
  return out;
}

interface StoreStats {
  label: string;
  stream: number;
  atoms: number;
  size_bytes: number;
  warnings: string[];
}

function tableCount(db: Database.Database, table: string): number {
  try { return (db.prepare(`SELECT COUNT(*) c FROM ${table}`).get() as any).c; }
  catch { return -1; }
}

function statStore(ref: StoreRef): StoreStats {
  const warnings: string[] = [];
  let stream = -1, atoms = -1, size = 0;
  try { size = fs.statSync(ref.dbPath).size; } catch {}
  const db = new Database(ref.dbPath, { readonly: true, fileMustExist: true });
  try {
    stream = tableCount(db, "stream");
    atoms = tableCount(db, "atoms");
    if (stream === 0 && atoms === 0) warnings.push("empty store (0 stream, 0 atoms)");
    // vector index parity — a *_vec count far below the table count means
    // embeds failed to land (the exact HTTP-500 fault class) — flag it.
    const sv = tableCount(db, "stream_vec");
    const av = tableCount(db, "atoms_vec");
    if (sv >= 0 && stream >= 0 && sv < stream) warnings.push(`stream_vec ${sv} < stream ${stream} — missing embeddings (run: falda reembed)`);
    if (av >= 0 && atoms >= 0 && av < atoms)  warnings.push(`atoms_vec ${av} < atoms ${atoms} — missing embeddings (run: falda reembed)`);
  } finally { db.close(); }
  return { label: ref.label, stream, atoms, size_bytes: size, warnings };
}

/** Root-level warnings not tied to a single store. */
function rootWarnings(root: string): string[] {
  const w: string[] = [];
  const manifest = path.join(root, "EMBEDDING.json");
  if (!fs.existsSync(manifest)) {
    w.push("no EMBEDDING.json manifest at root — embedding lock uninitialized");
  } else {
    try {
      const m = JSON.parse(fs.readFileSync(manifest, "utf8"));
      const envDim = Number(process.env.FALDA_DIM ?? 768);
      const envModel = process.env.FALDA_EMBED_MODEL ?? "";
      if (m.dim !== undefined && Number(m.dim) !== envDim) w.push(`embedding-lock mismatch: manifest dim ${m.dim} != FALDA_DIM ${envDim}`);
      if (m.model !== undefined && envModel && m.model !== envModel) w.push(`embedding-lock mismatch: manifest model ${m.model} != FALDA_EMBED_MODEL ${envModel}`);
    } catch { w.push("EMBEDDING.json is present but unparseable"); }
  }
  return w;
}

export function collectStats(root: string, filter: { tenant?: string; pool?: string }) {
  let stores = listAllStores(root);
  if (filter.tenant) stores = stores.filter((s) => s.scope === "self" && s.name === filter.tenant);
  if (filter.pool)   stores = stores.filter((s) => s.scope === "pool" && s.name === filter.pool);
  return {
    root,
    root_warnings: rootWarnings(root),
    stores: stores.map(statStore),
  };
}

function parseArgs(argv: string[]) {
  const o: { json: boolean; tenant?: string; pool?: string; help: boolean } = { json: false, help: false };
  for (const a of argv) {
    if (a === "--json") o.json = true;
    else if (a === "--help" || a === "-h") o.help = true;
    else if (a.startsWith("--tenant=")) o.tenant = a.slice("--tenant=".length);
    else if (a.startsWith("--pool=")) o.pool = a.slice("--pool=".length);
    else { console.error(`falda stats: unknown argument '${a}'`); process.exit(1); }
  }
  return o;
}

const HELP = `Usage: falda stats [--json] [--tenant=T | --pool=P]

Offline, read-only report of every store under FALDA_ROOT (default ./falda-data).
Never touches the live server, an embedder, or a token. Exits 1 on any
error-level warning (embedding-lock mismatch, missing embeddings).`;

function main() {
  const o = parseArgs(process.argv.slice(2));
  if (o.help) { console.log(HELP); return; }
  const root = process.env.FALDA_ROOT ?? "./falda-data";
  const report = collectStats(root, o);
  const anyError = report.root_warnings.length > 0 ||
    report.stores.some((s) => s.warnings.length > 0);

  if (o.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`FALDA stats — root: ${report.root}`);
    if (report.root_warnings.length) {
      console.log("\n  ROOT WARNINGS:");
      for (const w of report.root_warnings) console.log(`    ⚠ ${w}`);
    }
    console.log(`\n  ${"store".padEnd(28)} ${"stream".padStart(8)} ${"atoms".padStart(8)} ${"size".padStart(10)}`);
    for (const s of report.stores) {
      const sz = s.size_bytes >= 1e6 ? `${(s.size_bytes/1e6).toFixed(1)}MB` : `${(s.size_bytes/1e3).toFixed(0)}KB`;
      console.log(`  ${s.label.padEnd(28)} ${String(s.stream).padStart(8)} ${String(s.atoms).padStart(8)} ${sz.padStart(10)}`);
      for (const w of s.warnings) console.log(`      ⚠ ${w}`);
    }
    if (!report.stores.length) console.log("  (no stores found)");
  }
  if (anyError) process.exit(1);
}

// run only when invoked directly (not when imported by reembed.ts, etc.)
import { fileURLToPath } from "node:url";
if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
