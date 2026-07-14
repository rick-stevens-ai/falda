/**
 * FALDA HTTP gateway — exposes the four-tier memory store over a small JSON API.
 *
 * Multi-tenant + opt-in shared pools (see docs/POOLS.md):
 *   Every data route accepts optional {tenant, pool} addressing.
 *     tenant : agent identity. Defaults to FALDA_DEFAULT_TENANT (or "default").
 *     pool   : "self" (private, default) or a declared shared-pool name.
 *   With neither field set, behavior is single-tenant single-store parity against the
 *   default tenant's private store.
 *
 * Data routes (all POST, JSON in/out) — each also accepts {tenant?, pool?}:
 *   /stream/add      {session_id, messages[]}            -> {accepted_ids, total_count}
 *   /stream/query    {session_id?, limit?, ...}          -> {messages, total}
 *   /stream/search   {query, limit?}                     -> {messages: hits}
 *   /stream/delete   {ids?|session_id}                   -> {deleted_count}
 *   /atoms/upsert    {id?, type?, content, background?}  -> Atom
 *   /atoms/query     {type?, limit?, ...}                -> {items, total}
 *   /atoms/search    {query, limit?}                     -> {items: hits}
 *   /atoms/delete    {ids[]}                             -> {deleted_count}
 *   /scenes/ls       {prefix?}                           -> {entries, total}
 *   /scenes/read     {path}                              -> {path, content}
 *   /scenes/write    {path, content}                     -> {path}
 *   /scenes/rm       {path}                              -> {path}
 *   /core/read       {}                                  -> {content}
 *   /core/write      {content}                           -> {ok}
 *
 * Pool admin routes (POST):
 *   /pools/declare   {name, members:{tenant:access}, description?}  -> PoolDecl
 *   /pools/update    {name, members?, description?}                 -> PoolDecl
 *   /pools/grant     {name, tenant, access}                        -> PoolDecl
 *   /pools/get       {name}                                        -> {pool}
 *   /pools/list      {}                                            -> {pools}
 *   /pools/mine      {tenant}                                      -> {pools}  (reachable by tenant)
 *
 *   /healthz         (GET)                               -> {ok, tiers}
 */
import { createServer } from "node:http";
import { readFileSync as _lockReadFile, writeFileSync as _lockWriteFile } from "node:fs";
import { join as _lockJoin } from "node:path";
import { PoolManager, PoolError } from "./pools.js";
import { makeEmbedder, makeLocalEmbedder } from "./embedder.js";

const PORT = Number(process.env.FALDA_PORT ?? 8077);
// Bind loopback by default. The gateway has no inbound auth (tenant is read from
// the request body), so binding all interfaces would let any reachable caller
// name any tenant. Opt into a wider bind explicitly via FALDA_HOST=0.0.0.0, and
// only behind an authenticating front-end.
const HOST = process.env.FALDA_HOST ?? "127.0.0.1";
const DIM = Number(process.env.FALDA_DIM ?? 768);
const ROOT = process.env.FALDA_ROOT ?? "./falda-data";
const DEFAULT_TENANT = process.env.FALDA_DEFAULT_TENANT ?? "default";

// Embedder selection:
//   FALDA_EMBED=local                  -> deterministic offline embedder (no network)
//   FALDA_EMBED=remote                 -> require a configured /v1/embeddings endpoint
//   (unset) + FALDA_EMBED_BASE_URL set -> remote
//   (unset) + no base URL                -> local offline default (so it just works)
function selectEmbedder() {
  const mode = (process.env.FALDA_EMBED ?? "").toLowerCase();
  const hasRemote = !!process.env.FALDA_EMBED_BASE_URL;
  if (mode === "local") { console.log("FALDA embedder: local (offline, deterministic)"); return makeLocalEmbedder(DIM); }
  if (mode === "remote" || hasRemote) { console.log(`FALDA embedder: remote (${process.env.FALDA_EMBED_BASE_URL ?? "http://localhost:11434/v1"})`); return makeEmbedder(); }
  console.log("FALDA embedder: local (offline default; set FALDA_EMBED_BASE_URL for dense recall)");
  return makeLocalEmbedder(DIM);
}

// Embedding lock: verify running embed model+dim against the store's locked
// manifest (EMBEDDING.json) at boot. Prevents silent recall corruption from a
// same-dim/different-model swap, and broken inserts from a dim change.
function enforceEmbeddingLock() {
  const path = _lockJoin(ROOT, "EMBEDDING.json");
  const model = process.env.FALDA_EMBED_MODEL ?? "";
  let locked: any;
  try {
    locked = JSON.parse(_lockReadFile(path, "utf8"));
  } catch {
    // First boot / no manifest: write the current config as the lock.
    locked = { model, dim: DIM, locked: true, locked_at: new Date().toISOString().slice(0, 10) };
    try { _lockWriteFile(path, JSON.stringify(locked, null, 2)); } catch {}
    console.log(`FALDA embedding lock: initialized manifest model=${model} dim=${DIM}`);
    return;
  }
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

const pools = new PoolManager({ root: ROOT, embed: selectEmbedder(), dim: DIM });

/** Routes that mutate the addressed store (need readwrite on a shared pool). */
const WRITE_ROUTES = new Set([
  "/stream/add", "/stream/delete", "/atoms/upsert", "/atoms/delete",
  "/scenes/write", "/scenes/rm", "/core/write",
]);

async function handleData(route: string, b: any) {
  const tenant = b.tenant ?? DEFAULT_TENANT;
  const pool = b.pool; // undefined => "self"
  const store = pools.resolve(tenant, pool, WRITE_ROUTES.has(route));
  switch (route) {
    case "/stream/add":    return { accepted_ids: await store.addStream(b.session_id, b.messages ?? []), total_count: (b.messages ?? []).length };
    case "/stream/query":  return store.queryStream(b);
    case "/stream/search": return { messages: await store.searchStream(b.query, b.limit) };
    case "/stream/delete": return { deleted_count: store.deleteStream(b) };
    case "/atoms/upsert":  return await store.upsertAtom(b);
    case "/atoms/query":   return store.queryAtoms(b);
    case "/atoms/search":  return { items: await store.searchAtoms(b.query, b.limit) };
    case "/atoms/delete":  return { deleted_count: store.deleteAtoms(b.ids ?? []) };
    case "/scenes/ls":     return store.listScenes(b.prefix ?? "");
    case "/scenes/read":   return { path: b.path, content: store.readScene(b.path) };
    case "/scenes/write":  return (store.writeScene(b.path, b.content ?? ""), { path: b.path });
    case "/scenes/rm":     return (store.removeScene(b.path), { path: b.path });
    case "/core/read":     return { content: store.readCore() };
    case "/core/write":    return (store.writeCore(b.content ?? ""), { ok: true });
    default: return undefined;
  }
}

function handlePool(route: string, b: any) {
  switch (route) {
    case "/pools/declare": return pools.declarePool(b.name, b.members ?? {}, b.description ?? "");
    case "/pools/update":  return pools.updatePool(b.name, { members: b.members, description: b.description });
    case "/pools/grant":   return pools.grant(b.name, b.tenant, b.access);
    case "/pools/get":     return { pool: pools.getPool(b.name) };
    case "/pools/list":    return { pools: pools.listPools() };
    case "/pools/mine":    return { pools: pools.poolsForTenant(b.tenant ?? "default") };
    default: return undefined;
  }
}

async function handle(route: string, b: any): Promise<{ status: number; body: any }> {
  try {
    if (route.startsWith("/pools/")) {
      const out = handlePool(route, b);
      if (out === undefined) return { status: 404, body: { error: "unknown route" } };
      return { status: 200, body: out };
    }
    const out = await handleData(route, b);
    if (out === undefined) return { status: 404, body: { error: "unknown route" } };
    return { status: 200, body: out };
  } catch (e: any) {
    if (e instanceof PoolError) {
      // 404 for missing pool, 403 for access denial, 400 for malformed input.
      const status = e.code === "no_such_pool" ? 404
        : (e.code === "not_a_member" || e.code === "read_only") ? 403
        : 400;
      return { status, body: { error: e.message, code: e.code } };
    }
    return { status: 500, body: { error: String(e?.message ?? e) } };
  }
}

createServer((req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({ ok: true, tiers: ["stream", "atoms", "scenes", "core"], pools: true }));
  }
  if (req.method !== "POST") { res.writeHead(405); return res.end(); }
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    try {
      const parsed = body ? JSON.parse(body) : {};
      const { status, body: out } = await handle(req.url ?? "", parsed);
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(out));
    } catch (e: any) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: String(e?.message ?? e) }));
    }
  });
}).listen(PORT, HOST, () => console.log(`FALDA gateway listening on ${HOST}:${PORT} (root=${ROOT}, default-tenant=${DEFAULT_TENANT})`));
