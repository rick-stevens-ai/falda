# Falda Demo

A re-runnable, empirical proof of what the **Falda memory engine** does that a flat
memory file cannot: tiered, tenant-scoped, semantic recall that survives restarts and
feeds the agent's context automatically.

## Quick start

```bash
bash demo/falda_demo.sh
# override target:
FALDA_URL=http://100.87.221.1:8078 FALDA_TENANT=default FALDA_TENANT_B=kukla bash demo/falda_demo.sh
```

Expected: `SUMMARY: 6 PASS / 0 FAIL`.

## What it proves (each beat prints a PASS/FAIL witness line)

1. **PLANT** — write an atom via `POST /atoms/upsert`.
2. **SEMANTIC RECALL** — plant "the secret phrase belongs to a giraffe named Waffles,"
   then query *"which animal owns the confidential passphrase?"* (≈0 shared keywords) →
   Falda finds it **by meaning**, not string match.
3. **TENANT ISOLATION** — the same query against a different tenant returns nothing.
   One physical backend, walled-off memory. This is the money shot.
4. **L1 atoms** — recall returns typed + ranked atoms, not a flat log.
5. **L3 persona core** — `POST /core/read` returns the synthesized persona doc.
6. **L0 live stream** — `POST /stream/search` returns the raw-turn stream layer.

## Gateway route map (live-verified against :8078; all POST-only, GET = 405)

| Purpose | Route | Returns |
|---|---|---|
| Write atom (L1) | `POST /atoms/upsert` | `{id,type,content,created_at,...}` |
| Append stream (L0) | `POST /stream/add` | `{accepted_ids,total_count}` |
| Semantic recall (L1) | `POST /atoms/query` | `{items:[{id,type,content,...}]}` |
| Keyword/hybrid (L1) | `POST /atoms/search` | `{items:[...]}` |
| Stream search (L0) | `POST /stream/search` | `{messages:[{score,...}]}` |
| Persona core (L3) | `POST /core/read` | `{content:"# PERSONA/CORE ..."}` |

Non-routes (404, do not probe): `/atoms/ingest`, `/atoms/write`, `/atoms`, `/ingest`, `/stream/ingest`.

## Demo arc to narrate (5 min)

1. Run the script cold → **6/6 green** (sets the stage in 30s).
2. Live-plant a fresh fact:
   ```bash
   python3 - <<'PY'
   import json,urllib.request
   fact="the passphrase is 'heron-42', guarded by a lighthouse keeper named Pilar."
   d=json.dumps({"tenant":"default","pool":"self","type":"episodic","content":fact}).encode()
   r=urllib.request.urlopen(urllib.request.Request("http://100.87.221.1:8078/atoms/upsert",d,{"Content-Type":"application/json"}))
   print(r.read().decode())
   PY
   ```
3. Recall it with a **re-worded** query (*"who protects the secret code word?"*) → semantic hit.
4. **Cross-session amnesia test (headline):** open a fresh agent session and ask for the
   passphrase — it recalls with zero conversation history. That's the beat that lands with
   any audience.

## Two-agent version (strongest technical story)

Run live from both bots (Ollie tenant=`default`, Kukla tenant=`kukla`) on the same backend:

1. Rick plants a fact on Ollie's tenant → Kukla proves she can't recall it.
2. Rick plants a fact on Kukla's tenant → Ollie proves he can't recall it.
3. Each agent recalls **its own** fact via a re-worded query → semantic hit + isolation, live.

## Restart-survival beat

On the box that owns the gateway (m1 for Kukla's Hermes): plant → bounce the gateway →
re-recall. The atom is still there and still auto-injected into context. (Left as a
host-local step since only the gateway owner can restart it.)
