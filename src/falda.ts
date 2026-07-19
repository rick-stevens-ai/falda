/**
 * FALDA core store — clustered hierarchical memory for scientific agents.
 *
 * Four tiers, layered like atmospheric strata:
 *   T0  Stream    — raw conversation / observation log
 *   T1  Atoms     — distilled atomic memories (facts, preferences, rules)
 *   T2  Scenes    — synthesized episodic scene blocks
 *   T3  Core      — long-lived persona / project core
 *
 * Storage is fully local and open:
 *   - SQLite + sqlite-vec   dense vector recall (cosine)
 *   - SQLite FTS5           BM25 lexical recall
 *   - local filesystem      scene (T2) + core (T3) blobs
 *
 * Recall fuses dense + lexical via reciprocal-rank fusion.
 */
import Database from "better-sqlite3";
import * as sqliteVec from "sqlite-vec";
import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

export type Embedder = (text: string) => Promise<number[]>;

export interface FaldaOptions {
  /** SQLite file path (':memory:' for ephemeral). */
  dbPath: string;
  /** Directory for scene (T2) + core (T3) blob files. */
  blobDir: string;
  /** Embedding function (OpenAI-compatible; see ./embedder). */
  embed: Embedder;
  /** Embedding dimensionality; must match the model. */
  dim?: number;
}

export interface StreamItem { id?: string; role: string; content: string; timestamp?: string; }
export interface StreamHit extends Required<StreamItem> { score: number; }
export interface Atom {
  id: string; type: string; content: string; background?: string | null;
  created_at: string; updated_at: string;
}
export interface AtomHit extends Atom { score: number; }
export interface SceneEntry { path: string; created_at: string; updated_at: string; }

const RRF_K = 60;

/**
 * Sanitize an arbitrary user string into a safe FTS5 MATCH expression.
 * Raw input passed straight to MATCH throws on bare operators/punctuation
 * (AND, OR, NOT, *, -, ", (, ...), surfacing as a 500. We tokenize on
 * non-word chars and wrap each token as a quoted FTS5 string literal so the
 * whole query is treated as literal terms (implicit AND). Empty input yields
 * a query that matches nothing rather than erroring.
 */
function toFtsQuery(raw: string): string {
  const tokens = (raw || "")
    .split(/[^0-9A-Za-z\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF]+/)
    .filter((t) => t.length > 0)
    .map((t) => `"${t.replace(/"/g, '""')}"`);
  // Empty input -> a quoted literal that is valid FTS5 syntax but won't match
  // real tokens, yielding zero rows instead of a parse error.
  return tokens.length ? tokens.join(" ") : '"__falda_no_match__"';
}

export class Falda {
  private db: Database.Database;
  private embed: Embedder;
  private blobDir: string;
  private dim: number;

  constructor(opts: FaldaOptions) {
    this.embed = opts.embed;
    this.blobDir = opts.blobDir;
    this.dim = opts.dim ?? 768;
    fs.mkdirSync(this.blobDir, { recursive: true });
    this.db = new Database(opts.dbPath);
    sqliteVec.load(this.db);
    this.db.pragma("journal_mode = WAL");
    this.initSchema();
  }

  /** sqlite-vec float[N] columns require the vector as a raw float32 Buffer.
   *  better-sqlite3@12 no longer accepts a bare Float32Array as a bind param
   *  (throws "SQLite3 can only bind numbers, strings, bigints, buffers, and null"),
   *  so encode to a Buffer over the Float32Array's backing bytes. */
  private vecBuf(a: number[]): Buffer {
    const f = new Float32Array(a);
    return Buffer.from(f.buffer, f.byteOffset, f.byteLength);
  }

  private initSchema() {
    const d = this.dim;
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS stream (
        id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, ts TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_stream_session ON stream(session_id);
      CREATE VIRTUAL TABLE IF NOT EXISTS stream_fts
        USING fts5(content, id UNINDEXED, tokenize='porter unicode61');
      CREATE VIRTUAL TABLE IF NOT EXISTS stream_vec
        USING vec0(id TEXT PRIMARY KEY, embedding float[${d}]);

      CREATE TABLE IF NOT EXISTS atoms (
        id TEXT PRIMARY KEY, type TEXT, content TEXT, background TEXT,
        created_at TEXT, updated_at TEXT
      );
      CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts
        USING fts5(content, id UNINDEXED, tokenize='porter unicode61');
      CREATE VIRTUAL TABLE IF NOT EXISTS atoms_vec
        USING vec0(id TEXT PRIMARY KEY, embedding float[${d}]);
    `);
  }

  // ─── T0 Stream ────────────────────────────────────────────────────────────
  async addStream(sessionId: string, items: StreamItem[]): Promise<string[]> {
    const ids: string[] = [];
    const ins = this.db.prepare("INSERT INTO stream(id,session_id,role,content,ts) VALUES(?,?,?,?,?)");
    const insF = this.db.prepare("INSERT INTO stream_fts(content,id) VALUES(?,?)");
    const insV = this.db.prepare("INSERT INTO stream_vec(id,embedding) VALUES(?,?)");
    for (const m of items) {
      const id = m.id ?? randomUUID();
      const ts = m.timestamp ?? new Date().toISOString();
      ins.run(id, sessionId, m.role, m.content, ts);
      insF.run(m.content, id);
      insV.run(id, this.vecBuf(await this.embed(m.content)));
      ids.push(id);
    }
    return ids;
  }

  queryStream(p: { session_id?: string; limit?: number; offset?: number; time_start?: string; time_end?: string } = {}) {
    const where: string[] = []; const args: any[] = [];
    if (p.session_id) { where.push("session_id=?"); args.push(p.session_id); }
    if (p.time_start) { where.push("ts>=?"); args.push(p.time_start); }
    if (p.time_end)   { where.push("ts<=?"); args.push(p.time_end); }
    const w = where.length ? "WHERE " + where.join(" AND ") : "";
    const total = (this.db.prepare(`SELECT COUNT(*) c FROM stream ${w}`).get(...args) as any).c;
    const messages = this.db.prepare(
      `SELECT id,role,content,ts AS timestamp FROM stream ${w} ORDER BY ts DESC LIMIT ? OFFSET ?`
    ).all(...args, p.limit ?? 50, p.offset ?? 0);
    return { messages, total };
  }

  async searchStream(query: string, limit = 10): Promise<StreamHit[]> {
    return this.hybrid("stream", query, limit) as Promise<StreamHit[]>;
  }

  deleteStream(p: { ids?: string[]; session_id?: string }): number {
    let n = 0;
    if (p.ids?.length) {
      const del = this.db.prepare("DELETE FROM stream WHERE id=?");
      for (const id of p.ids) n += del.run(id).changes;
    } else if (p.session_id) {
      n = this.db.prepare("DELETE FROM stream WHERE session_id=?").run(p.session_id).changes;
    }
    return n;
  }

  // ─── T1 Atoms ─────────────────────────────────────────────────────────────
  async upsertAtom(a: { id?: string; type?: string; content: string; background?: unknown }): Promise<Atom> {
    const now = new Date().toISOString();
    const id = a.id ?? randomUUID();
    // Coerce background to a bindable value: SQLite only accepts
    // number/string/bigint/buffer/null. Callers sometimes send booleans,
    // numbers, or objects; normalize so a stray type can't throw a 500.
    const bg: string | null =
      a.background == null ? null
      : typeof a.background === "string" ? a.background
      : typeof a.background === "object" ? JSON.stringify(a.background)
      : String(a.background);
    const exists = this.db.prepare("SELECT created_at FROM atoms WHERE id=?").get(id) as any;
    const created = exists?.created_at ?? now;
    if (exists) {
      this.db.prepare("UPDATE atoms SET type=?,content=?,background=?,updated_at=? WHERE id=?")
        .run(a.type ?? "fact", a.content, bg, now, id);
      this.db.prepare("DELETE FROM atoms_fts WHERE id=?").run(id);
      this.db.prepare("DELETE FROM atoms_vec WHERE id=?").run(id);
    } else {
      this.db.prepare("INSERT INTO atoms(id,type,content,background,created_at,updated_at) VALUES(?,?,?,?,?,?)")
        .run(id, a.type ?? "fact", a.content, bg, created, now);
    }
    this.db.prepare("INSERT INTO atoms_fts(content,id) VALUES(?,?)").run(a.content, id);
    this.db.prepare("INSERT INTO atoms_vec(id,embedding) VALUES(?,?)")
      .run(id, this.vecBuf(await this.embed(a.content)));
    return { id, type: a.type ?? "fact", content: a.content, background: bg, created_at: created, updated_at: now };
  }

  queryAtoms(p: { type?: string; limit?: number; offset?: number; time_start?: string; time_end?: string } = {}) {
    const where: string[] = []; const args: any[] = [];
    if (p.type)       { where.push("type=?"); args.push(p.type); }
    if (p.time_start) { where.push("updated_at>=?"); args.push(p.time_start); }
    if (p.time_end)   { where.push("updated_at<=?"); args.push(p.time_end); }
    const w = where.length ? "WHERE " + where.join(" AND ") : "";
    const total = (this.db.prepare(`SELECT COUNT(*) c FROM atoms ${w}`).get(...args) as any).c;
    const items = this.db.prepare(
      `SELECT id,type,content,background,created_at,updated_at FROM atoms ${w} ORDER BY updated_at DESC LIMIT ? OFFSET ?`
    ).all(...args, p.limit ?? 50, p.offset ?? 0);
    return { items, total };
  }

  async searchAtoms(query: string, limit = 10): Promise<AtomHit[]> {
    return this.hybrid("atoms", query, limit) as Promise<AtomHit[]>;
  }

  deleteAtoms(ids: string[]): number {
    let n = 0;
    const del = this.db.prepare("DELETE FROM atoms WHERE id=?");
    for (const id of ids) n += del.run(id).changes;
    return n;
  }

  // ─── T2 Scenes (local FS) ─────────────────────────────────────────────────
  private sceneBase() { const b = path.join(this.blobDir, "scenes"); fs.mkdirSync(b, { recursive: true }); return b; }

  listScenes(prefix = ""): { entries: SceneEntry[]; total: number } {
    const base = this.sceneBase();
    const walk = (dir: string): string[] =>
      fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
        const fp = path.join(dir, e.name);
        return e.isDirectory() ? walk(fp) : [fp];
      });
    const entries = walk(base).map((fp) => path.relative(base, fp))
      .filter((rel) => rel.startsWith(prefix))
      .map((rel) => {
        const st = fs.statSync(path.join(base, rel));
        return { path: rel, created_at: st.birthtime.toISOString(), updated_at: st.mtime.toISOString() };
      });
    return { entries, total: entries.length };
  }

  readScene(p: string): string | null {
    const fp = path.join(this.sceneBase(), p);
    return fs.existsSync(fp) ? fs.readFileSync(fp, "utf8") : null;
  }

  writeScene(p: string, content: string): void {
    const fp = path.join(this.sceneBase(), p);
    fs.mkdirSync(path.dirname(fp), { recursive: true });
    fs.writeFileSync(fp, content, "utf8");
  }

  removeScene(p: string): void {
    const fp = path.join(this.sceneBase(), p);
    if (fs.existsSync(fp)) fs.unlinkSync(fp);
  }

  // ─── T3 Core (local FS) ───────────────────────────────────────────────────
  readCore(): string {
    const fp = path.join(this.blobDir, "core.md");
    return fs.existsSync(fp) ? fs.readFileSync(fp, "utf8") : "";
  }

  writeCore(content: string): void {
    fs.writeFileSync(path.join(this.blobDir, "core.md"), content, "utf8");
  }

  // ─── Hybrid recall: dense (sqlite-vec) + lexical (FTS5 BM25), RRF-fused ────
  private async hybrid(kind: "stream" | "atoms", query: string, limit: number) {
    const qvec = this.vecBuf(await this.embed(query));
    const vecRows = this.db.prepare(
      `SELECT id, distance FROM ${kind}_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?`
    ).all(qvec, limit * 2) as Array<{ id: string }>;
    const ftsRows = this.db.prepare(
      `SELECT id, bm25(${kind}_fts) AS rank FROM ${kind}_fts WHERE ${kind}_fts MATCH ? ORDER BY rank LIMIT ?`
    ).all(toFtsQuery(query), limit * 2) as Array<{ id: string }>;
    const score = new Map<string, number>();
    vecRows.forEach((r, i) => score.set(r.id, (score.get(r.id) ?? 0) + 1 / (RRF_K + i)));
    ftsRows.forEach((r, i) => score.set(r.id, (score.get(r.id) ?? 0) + 1 / (RRF_K + i)));
    const top = [...score.entries()].sort((a, z) => z[1] - a[1]).slice(0, limit);
    const cols = kind === "stream"
      ? "id,role,content,ts AS timestamp"
      : "id,type,content,background,created_at,updated_at";
    return top.map(([id, s]) => {
      const row = this.db.prepare(`SELECT ${cols} FROM ${kind} WHERE id=?`).get(id) as any;
      return { ...row, score: s };
    });
  }

  close() { this.db.close(); }
}
