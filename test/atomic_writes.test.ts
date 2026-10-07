import { Falda } from "../src/falda.js";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

let pass = 0;
let fail = 0;
function check(name: string, ok: boolean) {
  if (ok) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.error(`  FAIL ${name}`); }
}

async function main() {
  const blobDir = fs.mkdtempSync(path.join(os.tmpdir(), "falda-atomic-"));
  let calls = 0;
  let failOn = 1;
  const embed = async (_text: string) => {
    calls++;
    if (calls === failOn) throw new Error("synthetic embedder outage");
    return new Array(8).fill(0.125);
  };
  const s = new Falda({ dbPath: ":memory:", blobDir, embed, dim: 8 });

  calls = 0; failOn = 1;
  let atomFailed = false;
  try {
    await s.upsertAtom({ id: "atomic-atom", type: "fact", content: "must not persist" });
  } catch { atomFailed = true; }
  check("atom write reports embedder failure", atomFailed);
  check("failed atom write leaves no SQL/FTS record", s.queryAtoms({}).total === 0);

  calls = 0; failOn = 2;
  let streamFailed = false;
  try {
    await s.addStream("atomic-session", [
      { id: "stream-1", role: "user", content: "first" },
      { id: "stream-2", role: "assistant", content: "second fails" },
    ]);
  } catch { streamFailed = true; }
  check("stream batch reports embedder failure", streamFailed);
  check("failed stream batch leaves no partial records", s.queryStream({ session_id: "atomic-session" }).total === 0);

  calls = 0; failOn = 999;
  const first = await s.upsertAtom({ id: "update-me", type: "fact", content: "original" });
  calls = 0; failOn = 1;
  let updateFailed = false;
  try {
    await s.upsertAtom({ id: first.id, type: "fact", content: "replacement must not persist" });
  } catch { updateFailed = true; }
  const rows = s.queryAtoms({ type: "fact" }).items as Array<{ id: string; content: string }>;
  check("failed update reports embedder failure", updateFailed);
  check("failed update preserves previous atom", rows.some((r) => r.id === first.id && r.content === "original"));

  s.close();
  fs.rmSync(blobDir, { recursive: true, force: true });
  console.log(`\nFALDA atomic writes: ${pass} passed, ${fail} failed`);
  if (fail) process.exit(1);
}

main().catch((e) => { console.error(e); process.exit(1); });
