#!/usr/bin/env python3
"""
FALDA distiller — closes the day-1 distillation gap.

FALDA ships storage primitives (addStream / upsertAtom / writeScene / writeCore)
but NO process that promotes T0 stream -> T1 atoms -> T2 scenes -> T3 core.
The shadow tap mirrors raw turns into T0 only, so without this the dual-run
validates CAPTURE but not DISTILLATION. This sidecar runs the promotion loop,
mirroring a standard L0->L1->L2->L3 distillation cadence.

Design constraints:
  - Sidecar only. Calls the documented FALDA HTTP API (:8077) + Argo. Never
    edits the FALDA TS repo (that is Ollie's maintenance lane) and never
    touches the authoritative source store. Fully reversible: stop the process, the dual-run reverts to
    capture-only.
  - FALDA stays SHADOW: distilled atoms/scenes/core live in FALDA's own DB,
    never injected into the agent loop. A bug here cannot degrade either agent.

Tiers / cadence:
  T0->T1  every L1_EVERY_N new stream turns -> LLM extracts typed atoms
          (persona / episodic / instruction), upserted via /atoms/upsert.
  T1->T2  every L2_INTERVAL_S, synthesize recent atoms into a dated scene block
          -> /scenes/write.
  T2->T3  every L3_INTERVAL_S, synthesize scenes into a persona/core doc
          -> /core/write.

Checkpoint: ~/.falda/distiller_state.json (last distilled stream ts + counters).
Idempotent: atom IDs are content-hash-derived so re-running never duplicates.
"""
import json, os, sys, time, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone

HOME      = os.path.expanduser("~")
FALDA   = os.environ.get("FALDA_URL", "http://localhost:8078")
# Multi-tenant addressing: every gateway call is scoped to this tenant. The
# distiller only ever touches ONE tenant's store per process — run a separate
# instance per tenant. Defaults to "default" for backward compatibility.
FALDA_TENANT = os.environ.get("FALDA_TENANT", "default")
FALDA_POOL   = os.environ.get("FALDA_POOL", "")  # empty = private "self" store
# Any OpenAI-compatible chat-completions endpoint. Point at your own proxy.
ARGO_URL  = os.environ.get("LLM_BASE_URL") or os.environ.get("ARGO_BASE_URL", "http://localhost:8000/v1")
# Required: no default. Set LLM_API_KEY (or ARGO_API_KEY) in the environment.
ARGO_KEY  = os.environ.get("LLM_API_KEY") or os.environ.get("ARGO_API_KEY", "")
ARGO_MODEL= os.environ.get("DISTILLER_MODEL", "gpt-4o-mini")
# Per-tenant checkpoint so multiple distiller instances don't clobber each other.
_state_suffix = "" if FALDA_TENANT == "default" else f"-{FALDA_TENANT}"
STATE     = os.path.join(HOME, ".falda", f"distiller_state{_state_suffix}.json")
LOG       = os.path.join(HOME, ".falda", f"distiller{_state_suffix}.log")

L1_EVERY_N    = int(os.environ.get("L1_EVERY_N", "10"))      # new turns -> trigger atom extraction
L2_INTERVAL_S = int(os.environ.get("L2_INTERVAL_S", "3600")) # scene synthesis cadence
L3_INTERVAL_S = int(os.environ.get("L3_INTERVAL_S", "21600"))# core synthesis cadence
POLL_S        = int(os.environ.get("DISTILLER_POLL_S", "120"))
VALID_TYPES   = {"persona", "episodic", "instruction"}


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_json(url, payload, headers=None, timeout=120):
    data = json.dumps(payload).encode()
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def falda(route, payload):
    # Thread multi-tenant addressing into every gateway call so this distiller
    # only ever reads/writes the tenant it is scoped to. Without this the
    # gateway falls back to FALDA_DEFAULT_TENANT and the distiller would
    # silently operate on the wrong store.
    p = dict(payload)
    p.setdefault("tenant", FALDA_TENANT)
    if FALDA_POOL:
        p.setdefault("pool", FALDA_POOL)
    return http_json(f"{FALDA}{route}", p)


def argo_chat(system, user, max_tokens=2000, timeout=180):
    payload = {
        "model": ARGO_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    out = http_json(f"{ARGO_URL}/chat/completions", payload,
                    headers={"Authorization": f"Bearer {ARGO_KEY}"}, timeout=timeout)
    return out["choices"][0]["message"]["content"]


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"last_ts": "", "turns_since_l1": 0, "last_l2": 0.0, "last_l3": 0.0,
            "atoms_made": 0, "scenes_made": 0, "cores_made": 0}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def parse_json_loose(text):
    """3-strategy JSON parse: direct, fenced-block, first-bracket-span."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    if "```" in text:
        seg = text.split("```")[1]
        seg = seg[4:].strip() if seg.lower().startswith("json") else seg.strip()
        try:
            return json.loads(seg)
        except Exception:
            pass
    for op, cl in (("[", "]"), ("{", "}")):
        i, j = text.find(op), text.rfind(cl)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                pass
    return None


# ─── T0 -> T1 : extract typed atoms from new stream turns ───────────────────
L1_SYS = (
    "You extract durable agent-memory atoms from a short conversation transcript. "
    "Return ONLY a JSON array, nothing else — output must start with [ and be valid JSON. "
    "Each element: "
    '{"type": "persona|episodic|instruction", "content": "<one self-contained declarative fact>", '
    '"priority": 0-100}. '
    "persona = stable facts about the user/agent identity, preferences, environment, infrastructure. "
    "episodic = specific events/outcomes/decisions/fixes worth recalling. "
    "instruction = explicit standing rules/directives the user gave. "
    "Write each content as a declarative fact, not an instruction to yourself. "
    "Skip trivia, pleasantries, transient status chatter, and anything already obvious. "
    "If nothing durable, return []."
)

L1_CHUNK_TURNS = int(os.environ.get("L1_CHUNK_TURNS", "12"))


def _extract_chunk(turns):
    """Run one extraction pass over a small turn window; return count upserted."""
    transcript = "\n".join(f"[{m['role']}] {m['content'][:1200]}" for m in turns)
    if not transcript.strip():
        return 0
    try:
        raw = argo_chat(L1_SYS, transcript, max_tokens=2500)
    except Exception as e:
        log(f"L1 argo error: {e}")
        return 0
    atoms = parse_json_loose(raw) or []
    made = 0
    for a in atoms:
        if not isinstance(a, dict):
            continue
        t = (a.get("type") or "episodic").lower()
        if t not in VALID_TYPES:
            t = "episodic"
        content = (a.get("content") or "").strip()
        if not content:
            continue
        aid = "l1-" + hashlib.sha256((t + "|" + content).encode()).hexdigest()[:24]
        try:
            falda("/atoms/upsert", {
                "id": aid, "type": t, "content": content,
                "background": f"priority={a.get('priority','')};src=distiller;at={datetime.now(timezone.utc).isoformat()}",
            })
            made += 1
        except Exception as e:
            log(f"L1 upsert error: {e}")
    return made


def run_l1(state):
    q = falda("/stream/query", {"limit": 500})
    msgs = list(reversed(q.get("messages", [])))  # oldest-first
    last_ts = state["last_ts"]
    new = [m for m in msgs if m.get("timestamp", "") > last_ts] if last_ts else msgs
    if not new:
        return 0
    # Chunk into small windows so substantive turns get their own extraction pass
    # instead of being diluted inside one giant transcript (which makes the model
    # under-extract). Standard every-N-conversations distillation cadence.
    total_made = 0
    n = L1_CHUNK_TURNS
    for i in range(0, len(new), n):
        chunk = new[i:i + n]
        total_made += _extract_chunk(chunk)
    state["last_ts"] = new[-1].get("timestamp", last_ts)
    state["atoms_made"] += total_made
    log(f"L1: {len(new)} new turns in {(len(new)+n-1)//n} chunks -> {total_made} atoms (total {state['atoms_made']})")
    return total_made


# ─── T1 -> T2 : synthesize recent atoms into a scene block ──────────────────
L2_SYS = (
    "You write a concise episodic SCENE BLOCK summarizing a batch of agent-memory "
    "atoms into a coherent narrative of what happened and what matters. "
    "Markdown. 150-400 words. Lead with a one-line topic. No preamble."
)


def run_l2(state):
    a = falda("/atoms/query", {"limit": 60})
    items = a.get("items", [])
    if len(items) < 5:
        return 0
    body = "\n".join(f"- ({it['type']}) {it['content']}" for it in items)
    try:
        scene = argo_chat(L2_SYS, body, max_tokens=1200)
    except Exception as e:
        log(f"L2 argo error: {e}")
        return 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"auto/{day}.md"
    try:
        falda("/scenes/write", {"path": path, "content": scene})
        state["scenes_made"] += 1
        state["last_l2"] = time.time()
        log(f"L2: scene written -> {path} ({len(scene)} chars, total {state['scenes_made']})")
        return 1
    except Exception as e:
        log(f"L2 write error: {e}")
        return 0


# ─── T2 -> T3 : synthesize scenes into persona/core ─────────────────────────
L3_SYS = (
    "You synthesize a long-lived PERSONA/CORE profile from a set of scene blocks "
    "and existing core. Capture stable identity, preferences, environment, and "
    "standing directives. Markdown. Supersede stale facts with current ones. "
    "Be compact and durable. No preamble."
)


def run_l3(state):
    sc = falda("/scenes/ls", {"prefix": ""})
    entries = sc.get("entries", [])
    if not entries:
        return 0
    blocks = []
    for e in entries[-12:]:
        try:
            r = falda("/scenes/read", {"path": e["path"]})
            if r.get("content"):
                blocks.append(f"## {e['path']}\n{r['content']}")
        except Exception:
            pass
    if not blocks:
        return 0
    existing = ""
    try:
        existing = falda("/core/read", {}).get("content", "")
    except Exception:
        pass
    user = (("EXISTING CORE:\n" + existing + "\n\n") if existing else "") + \
           "SCENE BLOCKS:\n" + "\n\n".join(blocks)
    if len(user) > 24000:
        user = user[-24000:]
    try:
        core = argo_chat(L3_SYS, user, max_tokens=3000)
    except Exception as e:
        log(f"L3 argo error: {e}")
        return 0
    try:
        falda("/core/write", {"content": core})
        state["cores_made"] += 1
        state["last_l3"] = time.time()
        log(f"L3: core synthesized ({len(core)} chars, total {state['cores_made']})")
        return 1
    except Exception as e:
        log(f"L3 write error: {e}")
        return 0


def loop():
    log(f"distiller start: falda={FALDA} tenant={FALDA_TENANT}"
        f"{('/'+FALDA_POOL) if FALDA_POOL else ''} model={ARGO_MODEL} "
        f"L1_EVERY_N={L1_EVERY_N} L2={L2_INTERVAL_S}s L3={L3_INTERVAL_S}s poll={POLL_S}s")
    while True:
        s = load_state()
        try:
            q = falda("/stream/query", {"limit": 1})
            total = q.get("total", 0)
            last_ts = s["last_ts"]
            new_count = 0
            if total:
                allq = falda("/stream/query", {"limit": 500})
                new_count = sum(1 for m in allq.get("messages", [])
                                if m.get("timestamp", "") > last_ts) if last_ts else total
            if new_count >= L1_EVERY_N or (new_count > 0 and not last_ts):
                run_l1(s)
            now = time.time()
            if now - s.get("last_l2", 0) >= L2_INTERVAL_S:
                run_l2(s)
            if now - s.get("last_l3", 0) >= L3_INTERVAL_S:
                run_l3(s)
            save_state(s)
        except urllib.error.URLError as e:
            log(f"falda unreachable: {e}")
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    if "--once" in sys.argv:
        s = load_state()
        run_l1(s)
        run_l2(s)
        run_l3(s)
        save_state(s)
        log("once: done")
    else:
        loop()
