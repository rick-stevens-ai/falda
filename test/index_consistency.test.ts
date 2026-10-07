/** Regression: logical records span base, FTS, and vector indexes. */
import { Falda } from "../src/falda.js";
import { makeLocalEmbedder } from "../src/embedder.js";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import Database from "better-sqlite3";
import * as sqliteVec from "sqlite-vec";

function assert(ok: unknown, msg: string) { if (!ok) throw new Error(msg); }

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "falda-consistency-"));
  const s = new Falda({ dbPath: path.join(root, "falda.db"), blobDir: path.join(root, "blobs"), embed: makeLocalEmbedder(32), dim: 32 });
  try {
    const atom = await s.upsertAtom({ id: "atom-delete", type: "fact", content: "cedar telescope beacon" });
    assert((await s.searchAtoms("cedar telescope", 5)).some(x => x.id === atom.id), "atom was not searchable before delete");
    assert(s.deleteAtoms([atom.id, atom.id]) === 1, "duplicate atom ids must count one logical deletion");
    assert(s.queryAtoms({}).total === 0, "atom base row survived delete");
    assert(!(await s.searchAtoms("cedar telescope", 5)).some(x => x.id === atom.id), "atom ghost survived in search index");

    const ids = await s.addStream("session-delete", [
      { id: "stream-a", role: "user", content: "violet harbor compass" },
      { id: "stream-b", role: "assistant", content: "amber ocean sextant" },
    ]);
    assert(ids.length === 2, "stream insert failed");
    assert((await s.searchStream("violet harbor", 5)).some(x => x.id === "stream-a"), "stream was not searchable before delete");
    assert(s.deleteStream({ session_id: "session-delete" }) === 2, "session delete count wrong");
    assert(s.queryStream({ session_id: "session-delete" }).total === 0, "stream base rows survived delete");
    assert(!(await s.searchStream("violet harbor", 5)).some(x => x.id === "stream-a"), "stream ghost survived in search index");
    s.close();
    const db = new Database(path.join(root, "falda.db"));
    sqliteVec.load(db);
    for (const table of ["atoms", "atoms_fts", "atoms_vec", "stream", "stream_fts", "stream_vec"]) {
      const count = (db.prepare(`SELECT count(*) AS n FROM ${table}`).get() as { n: number }).n;
      assert(count === 0, `${table} retained ${count} rows after logical deletion`);
    }
    db.close();
    console.log("INDEX CONSISTENCY GREEN");
  } finally {
    try { s.close(); } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  }
}
main().catch(e => { console.error(e); process.exit(1); });
