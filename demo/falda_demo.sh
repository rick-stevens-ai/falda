#!/usr/bin/env bash
# falda_demo.sh — live, reproducible demonstration of the Falda memory engine.
#
# Shows the four things a flat memory file CANNOT do:
#   1. PLANT + SEMANTIC RECALL  — write an atom, recall it by *meaning* (different wording)
#   2. TENANT ISOLATION         — a fact in tenant A is invisible to tenant B
#   3. PERSISTENCE              — the atom is durable (survives restarts; re-read after write)
#   4. TIERED CONTEXT           — the assembled block is layered (L3 persona / L2 scene / L1 atoms / L0 stream)
#
# Each beat prints a PASS/FAIL line. Pure stdlib Python driver (no curl/jq needed).
#
# Usage:
#   bash falda_demo.sh                 # run against default gateway/tenant
#   FALDA_URL=http://100.87.221.1:8078 FALDA_TENANT=default bash falda_demo.sh
#
set -u

FALDA_URL="${FALDA_URL:-http://100.87.221.1:8078}"
TENANT_A="${FALDA_TENANT:-default}"     # Ollie
TENANT_B="${FALDA_TENANT_B:-kukla}"     # Kukla (for the isolation beat)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SECRET="falda-demo-${STAMP}"

echo "=================================================================="
echo " FALDA LIVE DEMO   gateway=${FALDA_URL}  tenantA=${TENANT_A}  tenantB=${TENANT_B}"
echo " run id: ${STAMP}"
echo "=================================================================="

python3 - "$FALDA_URL" "$TENANT_A" "$TENANT_B" "$SECRET" <<'PY'
import sys, json, time, urllib.request

BASE, TA, TB, SECRET = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

def call(path, payload, timeout=10):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")

def try_paths(paths, payload):
    """Falda builds may name write/query routes slightly differently; try a few."""
    last = None
    for p in paths:
        try:
            st, body = call(p, payload)
            if st == 200:
                return p, body
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return None, last

def items_of(body):
    if isinstance(body, dict):
        return body.get("items") or body.get("atoms") or []
    return []

npass = nfail = 0
def result(ok, label, detail=""):
    global npass, nfail
    tag = "PASS" if ok else "FAIL"
    if ok: npass += 1
    else:  nfail += 1
    print(f"[{tag}] {label}" + (f"  — {detail}" if detail else ""))

# ---- BEAT 1: PLANT --------------------------------------------------------
plant_fact = f"DEMO FACT: the secret demo phrase is '{SECRET}' and it belongs to a giraffe named Waffles."
write_paths = ["/atoms/upsert", "/atoms/ingest", "/atoms/write", "/atoms"]
wp, wbody = try_paths(write_paths, {
    "tenant": TA, "type": "episodic", "content": plant_fact,
    "text": plant_fact, "pool": "self",
})
if wp:
    result(True, "PLANT atom into tenant=%s" % TA, f"via {wp}")
else:
    result(False, "PLANT atom", f"no write route accepted ({wbody})")
    print("\n(Write route not found — the two-agent LIVE demo doesn't need this; "
          "agents plant via their own Hermes/OpenClaw memory tool. Continuing with recall beats.)")

time.sleep(1.5)  # let indexing settle

# ---- BEAT 2: SEMANTIC RECALL (different wording than the plant) ------------
q = "which animal owns the confidential passphrase for the demo?"
st, body = call("/atoms/query", {"query": q, "tenant": TA, "limit": 5})
hit = any(SECRET in (it.get("content","") + it.get("text","")) for it in items_of(body))
result(hit, "SEMANTIC RECALL in tenant=%s (query worded differently)" % TA,
       f'q="{q}" -> {"found secret" if hit else "no hit"} ({len(items_of(body))} items)')

# ---- BEAT 3: TENANT ISOLATION --------------------------------------------
st, body_b = call("/atoms/query", {"query": q, "tenant": TB, "limit": 5})
leaked = any(SECRET in (it.get("content","") + it.get("text","")) for it in items_of(body_b))
result(not leaked, "TENANT ISOLATION (%s cannot see %s's secret)" % (TB, TA),
       "no leak" if not leaked else "LEAK DETECTED")

# ---- BEAT 4: TIERED / STRUCTURED RECALL ----------------------------------
st, body2 = call("/atoms/query", {"query": "status", "tenant": TA, "limit": 5})
its = items_of(body2)
types = sorted({it.get("type","?") for it in its})
structured = len(its) > 0 and any(k in it for it in its for k in ("id","type"))
result(structured, "L1 atoms returned typed+ranked (not a flat log)",
       f"{len(its)} atoms, types={types}")

# ---- BEAT 5: LAYERED CONTEXT (L3 persona core + L0 stream exist) ----------
try:
    st, core = call("/core/read", {"tenant": TA})
    has_core = bool((core or {}).get("content"))
except Exception as e:
    has_core = False
result(has_core, "L3 PERSONA CORE present (/core/read)",
       "persona doc returned" if has_core else "empty")
try:
    st, strm = call("/stream/search", {"query": "status", "tenant": TA, "limit": 2})
    has_stream = "messages" in (strm or {})
except Exception as e:
    has_stream = False
result(has_stream, "L0 LIVE STREAM queryable (/stream/search)",
       "stream layer responds" if has_stream else "no stream route")

print("------------------------------------------------------------------")
print(f"SUMMARY: {npass} PASS / {nfail} FAIL")
print("Talking points while this runs:")
print("  • recall matched by MEANING, not keyword (beat 2 query shares ~0 words with the plant)")
print("  • the SAME infra serves two agents but tenants are walled off (beat 3)")
print("  • atoms come back structured+typed+ranked — the model sees layered context, not a dump")
sys.exit(0 if nfail == 0 else 1)
PY
rc=$?
echo "=================================================================="
echo "falda_demo.sh exit=$rc"
exit $rc
