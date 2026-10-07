/** Regressions: scene containment and startup orphan repair. */
import { Falda } from "../src/falda.js";
import { makeLocalEmbedder } from "../src/embedder.js";
import Database from "better-sqlite3";
import * as sqliteVec from "sqlite-vec";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

function assert(ok: unknown, msg: string) { if (!ok) throw new Error(msg); }
function mustThrow(fn: () => unknown, msg: string) { let threw = false; try { fn(); } catch { threw = true; } assert(threw, msg); }

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "falda-sec-"));
  const dbPath = path.join(root, "falda.db");
  const blobs = path.join(root, "blobs");
  let s = new Falda({ dbPath, blobDir: blobs, embed: makeLocalEmbedder(32), dim: 32 });
  try {
    for (const bad of ["../escape", "../../escape", "/tmp/escape", "ok/../../../escape", "..\\escape"])
      for (const op of [() => s.readScene(bad), () => s.writeScene(bad, "x"), () => s.removeScene(bad)])
        mustThrow(op, `scene traversal accepted: ${bad}`);
    s.writeScene("safe/note.md", "safe");
    assert(s.readScene("safe/note.md") === "safe", "valid nested scene failed");
    const outside = path.join(root, "outside");
    fs.mkdirSync(outside);
    fs.symlinkSync(outside, path.join(blobs, "scenes", "link"));
    mustThrow(() => s.writeScene("link/escape.md", "x"), "scene symlink escape accepted");
    const danglingTarget = path.join(outside, "created-by-escape.md");
    fs.symlinkSync(danglingTarget, path.join(blobs, "scenes", "dangling.md"));
    mustThrow(() => s.writeScene("dangling.md", "x"), "dangling final symlink escape accepted");
    assert(!fs.existsSync(danglingTarget), "dangling final symlink created outside file");
    assert(!s.listScenes().entries.some((x) => x.path === "link" || x.path === "dangling.md"),
      "scene listing exposed symlink entries");
    const coreOutside = path.join(outside, "core-outside.md");
    fs.writeFileSync(coreOutside, "KEEP");
    fs.symlinkSync(coreOutside, path.join(blobs, "core.md"));
    mustThrow(() => s.readCore(), "core read followed existing symlink");
    mustThrow(() => s.writeCore("PWN"), "core write followed existing symlink");
    assert(fs.readFileSync(coreOutside, "utf8") === "KEEP", "core existing symlink modified outside file");
    fs.unlinkSync(path.join(blobs, "core.md"));
    const danglingCore = path.join(outside, "core-created.md");
    fs.symlinkSync(danglingCore, path.join(blobs, "core.md"));
    mustThrow(() => s.writeCore("PWN"), "core write followed dangling symlink");
    assert(!fs.existsSync(danglingCore), "core dangling symlink created outside file");
    s.close();

    const db = new Database(dbPath); sqliteVec.load(db);
    const v = Buffer.from(new Float32Array(32).buffer);
    db.prepare("INSERT INTO atoms_fts(content,id) VALUES(?,?)").run("ghost atom", "ghost-a");
    db.prepare("INSERT INTO atoms_vec(id,embedding) VALUES(?,?)").run("ghost-a", v);
    db.prepare("INSERT INTO stream_fts(content,id) VALUES(?,?)").run("ghost stream", "ghost-s");
    db.prepare("INSERT INTO stream_vec(id,embedding) VALUES(?,?)").run("ghost-s", v);
    db.close();

    s = new Falda({ dbPath, blobDir: blobs, embed: makeLocalEmbedder(32), dim: 32 });
    s.close();
    const check = new Database(dbPath); sqliteVec.load(check);
    for (const table of ["atoms_fts", "atoms_vec", "stream_fts", "stream_vec"])
      assert((check.prepare(`SELECT count(*) n FROM ${table}`).get() as {n:number}).n === 0, `${table} startup orphan survived`);
    check.close();
    console.log("SECURITY AND ORPHAN REPAIR GREEN");
  } finally {
    try { s.close(); } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  }
}
main().catch(e => { console.error(e); process.exit(1); });
