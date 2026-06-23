
import { Stratus } from "../src/stratus.js";
import { makeLocalEmbedder } from "../src/embedder.js";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
const blobDir = fs.mkdtempSync(path.join(os.tmpdir(), "stratus-fts-"));
const s = new Stratus({ dbPath: ":memory:", blobDir, embed: makeLocalEmbedder(768), dim: 768 });
async function main() {
  await s.addStream("t", [
    { role: "user", content: "memory system dual run between SOURCE and STRATUS" },
    { role: "assistant", content: "the distiller promotes atoms and scenes" },
  ]);
  const probes = ["memory system dual run","SOURCE AND STRATUS",'distiller "scenes"',"atoms-and-scenes (promote)","OR NOT *",""];
  let fails = 0;
  for (const q of probes) {
    try { const r = await s.searchStream(q, 3); console.log("OK   [" + q + "] -> " + r.length + " hits"); }
    catch (e) { fails++; console.log("FAIL [" + q + "] -> " + e.message); }
  }
  s.close();
  fs.rmSync(blobDir, { recursive: true, force: true });
  console.log(fails === 0 ? "FTS PROBE GREEN" : "FTS PROBE FAILED: " + fails);
}
main();
