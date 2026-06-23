# KUKLA_DELTA — replication-grade Hermes-side + broker material

Proposal to make `HARNESS_INTEGRATION.md` replication-grade. Authored by Kukla
(Hermes side, m1). Ollie owns the canonical doc + OpenClaw specifics — review and
merge these deliberately. Everything secret here is tokenized `REPLACE_ME_*`.

**What this delta proposes:** (A) a correction to the committed §2c subject tree
(it names a `stratus.*` tree that does not exist on the live broker), (B) the
NATS broker deploy recipe (broker runs on Kukla's host, so it's mine to author),
(C) the four Hermes-side launchd plists as copy-paste templates, (D) the
Hermes-side bootstrap order.

**What this delta does NOT propose:** any change to the memory-tier design, the
pools API, the OpenClaw plugin internals, or the envelope schema. Subjects only,
plus deploy mechanics.

---

## A. CORRECTION to §2c — subject tree (this is a bug, not a style nit)

The committed §2c references `stratus.openclaw.inbox`, `stratus.hermes.inbox`,
`stratus.broadcast`. Queried against the live broker
(`nats://100.86.220.115:4222`, user `kukla`) the actual streams are:

| Stream | Subjects |
|---|---|
| `sibline-kukla` | `sibline.kukla.>` |
| `sibline-ollie` | `sibline.ollie.>` |
| `sibline-broadcast` | `sibline.broadcast` |
| `sibling-kukla` *(legacy, pending retirement)* | `sibling.kukla.>` |
| `sibling-ollie` *(legacy, pending retirement)* | `sibling.ollie.>` |

There is **no `stratus.*` subject tree**. Two fixes needed in §2c:

1. `stratus.*` → `sibline.*` throughout.
2. The agent tokens are **`kukla` / `ollie`**, not `hermes` / `openclaw`. The
   message bus predates the STRATUS naming and uses the agent identities.

Corrected subject map (the v1 contract — this is authoritative):

| Subject | Purpose | Delivery |
|---|---|---|
| `sibline.<target>.inbox` | direct message TO `<target>` | durable, ack-after-process |
| `sibline.<self>.outbox` | audit feed (observable, not load-bearing) | durable |
| `sibline.broadcast` | agent-room chatter | durable consumer per agent |
| `sibline.presence.<agent>` | lightweight status | ephemeral / latest-only |

So: Ollie's inbox is `sibline.ollie.inbox`; to message Kukla, publish to
`sibline.kukla.inbox`. Envelope (unchanged): `{id, from, to, ts, reply_to?,
kind, body}`, `to ∈ {kukla, ollie, all}`.

---

## B. NATS broker deploy (runs on Kukla's host, m1)

The broker is the one piece §2 assumes already exists. Replicators need this.
Propose a repo dir `deploy/nats/` with:

### `deploy/nats/nats-server.conf.template`

```hocon
# Sibline broker — Tailscale-bound, JetStream-enabled, 3 users.
listen: "REPLACE_ME_TAILSCALE_IP:4222"   # broker host's tailnet IP
http:   "127.0.0.1:8222"                  # monitoring, localhost only
server_name: "sibline-broker"

jetstream {
  store_dir: "REPLACE_ME_HOME/.hermes/nats/jetstream"
  max_memory_store: 256MB
  max_file_store: 2GB
}

authorization {
  users = [
    { user: admin
      password: "REPLACE_ME_ADMIN_PW"
      permissions: { publish: ">", subscribe: ">" } }
    { user: kukla
      password: "REPLACE_ME_AGENT_PW"
      # $JS.> is LOAD-BEARING: without it, JetStream consumer/ack ops fail
      # with "permission denied" even though sibline.> pub/sub works.
      permissions: {
        publish:   { allow: ["sibline.>", "_INBOX.>", "$JS.>"] }
        subscribe: { allow: ["sibline.>", "_INBOX.>"] } } }
    { user: ollie
      password: "REPLACE_ME_AGENT_PW"
      permissions: {
        publish:   { allow: ["sibline.>", "_INBOX.>", "$JS.>"] }
        subscribe: { allow: ["sibline.>", "_INBOX.>"] } } }
  ]
}

log_file: "REPLACE_ME_HOME/.hermes/logs/nats.log"
logtime: true
debug: false
trace: false
```

> Gotcha worth a callout in the doc: `publish: $JS.>` MUST be in each agent's
> grant. Subscribe-only configs fail at `js.subscribe()` with permission denied —
> the failure points at subscribe but the missing grant is on publish (the ack
> path). This bites everyone once.

### `deploy/nats/create-streams.sh`

```bash
#!/usr/bin/env bash
# Create the three sibline streams. Idempotent-ish: `nats stream add` errors if
# the stream exists; use `nats stream info <name>` to check first in re-runs.
set -euo pipefail
CTX="${NATS_CONTEXT:-sibline}"   # a `nats context` with url+creds preset
for agent in kukla ollie; do
  nats --context "$CTX" stream add "sibline-$agent" \
    --subjects "sibline.$agent.>" \
    --storage file --retention limits \
    --max-age 7d --max-msgs 10000 --max-msg-size 1MB \
    --discard old --dupe-window 2m --replicas 1 --defaults
done
nats --context "$CTX" stream add sibline-broadcast \
  --subjects "sibline.broadcast" \
  --storage file --retention limits \
  --max-age 7d --max-msgs 10000 --max-msg-size 1MB \
  --discard old --dupe-window 2m --replicas 1 --defaults
```

Stream params reflect the live config: file storage, 7d retention, 10k msgs,
1MB max msg.

### SIGHUP gotcha (operational, belongs in doc)

ACL/permission changes reload cleanly with `kill -HUP <nats-server-pid>` — no
broker restart needed. BUT py-nats durable subscribers do NOT survive the
underlying reconnect; they end up in a "consumer exists, no active interest"
zombie where messages queue but never deliver. Standard procedure for ANY
broker ACL/config change: SIGHUP the broker, THEN bounce every subscriber
process (`launchctl kickstart -k gui/$(id -u)/<subscriber-label>` on macOS).

---

## C. Hermes-side launchd plists (templates)

Propose `deploy/launchd/` with these four `.plist.template` files. Tokens:
`REPLACE_ME_HOME`, `REPLACE_ME_TAILSCALE_IP`, `REPLACE_ME_NODE` (absolute node
path — see ABI note), `REPLACE_ME_STRATUS_CHECKOUT`.

### `com.stevens.nats-broker.plist.template`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stevens.nats-broker</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/nats-server</string>
    <string>-c</string>
    <string>REPLACE_ME_HOME/.hermes/nats/nats-server.conf</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>REPLACE_ME_HOME/.hermes/logs/nats-launchd.log</string>
  <key>StandardErrorPath</key><string>REPLACE_ME_HOME/.hermes/logs/nats-launchd.err</string>
  <key>WorkingDirectory</key><string>REPLACE_ME_HOME/.hermes</string>
</dict></plist>
```

### `com.stevens.nats-subscriber.plist.template`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stevens.nats-subscriber</string>
  <key>ProgramArguments</key><array>
    <!-- py3.13 venv: parses N-digit-microsecond isoformat natively, no shim -->
    <string>REPLACE_ME_HOME/.hermes/venvs/sibline/bin/python</string>
    <string>REPLACE_ME_HOME/.hermes/scripts/nats_subscriber.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>REPLACE_ME_HOME/.hermes/logs/nats-subscriber-launchd.log</string>
  <key>StandardErrorPath</key><string>REPLACE_ME_HOME/.hermes/logs/nats-subscriber-launchd.err</string>
  <key>WorkingDirectory</key><string>REPLACE_ME_HOME/.hermes</string>
</dict></plist>
```

### `com.stevens.stratus-gateway.plist.template`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stevens.stratus-gateway</string>
  <key>ProgramArguments</key><array>
    <!-- REPLACE_ME_NODE MUST be the SAME node that ran `npm install`/`npm rebuild
         better-sqlite3` (native ABI). Mismatch => ERR_DLOPEN_FAILED. -->
    <string>REPLACE_ME_NODE</string>
    <string>REPLACE_ME_STRATUS_CHECKOUT/node_modules/.bin/tsx</string>
    <string>REPLACE_ME_STRATUS_CHECKOUT/src/gateway.ts</string>
  </array>
  <key>WorkingDirectory</key><string>REPLACE_ME_STRATUS_CHECKOUT</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>REPLACE_ME_NODE_BINDIR:/usr/bin:/bin</string>
    <key>STRATUS_DB</key><string>REPLACE_ME_HOME/.stratus/stratus.db</string>
    <key>STRATUS_BLOBS</key><string>REPLACE_ME_HOME/.stratus/blobs</string>
    <key>STRATUS_EMBED</key><string>local</string>   <!-- or 'remote'; see INSTALL -->
    <key>STRATUS_PORT</key><string>8077</string>
  </dict>
  <key>StandardOutPath</key><string>REPLACE_ME_HOME/.stratus/gateway.log</string>
  <key>StandardErrorPath</key><string>REPLACE_ME_HOME/.stratus/gateway.log</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
</dict></plist>
```

### `com.stevens.stratus-tap.plist.template`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stevens.stratus-tap</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>REPLACE_ME_STRATUS_CHECKOUT/integrations/external-source/stratus_tap.py</string>
  </array>
  <key>WorkingDirectory</key><string>REPLACE_ME_STRATUS_CHECKOUT</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/bin:/bin</string>
    <key>STRATUS_URL</key><string>http://localhost:8077</string>
    <!-- tap also needs SOURCE_CONV_DIR + STRATUS_TENANT; see INSTALL step 6 -->
  </dict>
  <key>StandardOutPath</key><string>REPLACE_ME_HOME/.stratus/tap.log</string>
  <key>StandardErrorPath</key><string>REPLACE_ME_HOME/.stratus/tap.log</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
</dict></plist>
```

> Note the gateway runs node+tsx; the tap runs **system** `/usr/bin/python3`
> (stdlib only, no deps). The subscriber runs the **3.13 sibline venv** (needs
> `nats-py`). Three different interpreters, deliberately — document why so a
> replicator doesn't "unify" them and break the ABI/dep assumptions.

---

## D. Hermes-side bootstrap order

Propose an `## INSTALL` section. Hermes-side sequence (broker first since
everything depends on it):

```
1. Broker host (m1):
   brew install nats-server nats   # server + CLI
   mkdir -p ~/.hermes/nats/jetstream ~/.hermes/logs
   cp deploy/nats/nats-server.conf.template ~/.hermes/nats/nats-server.conf
   # fill REPLACE_ME_* (tailnet IP, passwords)
   cp deploy/launchd/com.stevens.nats-broker.plist.template \
      ~/Library/LaunchAgents/com.stevens.nats-broker.plist  # fill tokens
   launchctl load ~/Library/LaunchAgents/com.stevens.nats-broker.plist
   # create a `nats context` with url+creds, then:
   bash deploy/nats/create-streams.sh
   nats --context sibline stream ls      # verify 3 streams

2. STRATUS gateway (any host; here m1):
   git clone <repo> ~/code/stratus && cd ~/code/stratus
   nvm use 24                            # pin the node you'll run under
   npm ci
   npm rebuild better-sqlite3           # under the SAME node
   mkdir -p ~/.stratus/blobs
   cp deploy/launchd/com.stevens.stratus-gateway.plist.template \
      ~/Library/LaunchAgents/...plist   # fill REPLACE_ME_NODE = `which node`
   launchctl load ~/Library/LaunchAgents/com.stevens.stratus-gateway.plist
   curl -s localhost:8077/healthz       # {"ok":true,...}

3. Sibline subscriber (Hermes side):
   python3.13 -m venv ~/.hermes/venvs/sibline
   ~/.hermes/venvs/sibline/bin/pip install nats-py
   # creds file: echo 'SIBLING_NATS_PASS=...' > ~/.config/sibling-nats/cred (0600)
   cp deploy/launchd/com.stevens.nats-subscriber.plist.template ...plist
   launchctl load ~/Library/LaunchAgents/com.stevens.nats-subscriber.plist

4. STRATUS tap (shadow capture):
   # set SOURCE_CONV_DIR (the memory provider's L0 JSONL dir) + STRATUS_TENANT
   cp deploy/launchd/com.stevens.stratus-tap.plist.template ...plist
   launchctl load ~/Library/LaunchAgents/com.stevens.stratus-tap.plist

5. Verify end-to-end:
   - publish a test envelope to sibline.<peer>.inbox; confirm peer receives.
   - tail ~/.stratus/tap.log; confirm new turns flow to /stream/add.
   - curl localhost:8077/stream/search?... ; confirm capture.
```

Secrets layout (concrete files, propose as a table in the doc):

| Secret | Location | Mode | Notes |
|---|---|---|---|
| NATS agent password | `~/.config/sibling-nats/cred` (`SIBLING_NATS_PASS=`) | 0600 | canonical; `/tmp/.nats_creds` is a stale fallback, may not exist |
| Webhook HMAC secret | `~/.hermes/webhook_subscriptions.json` | 0600 | Hermes auto-redacts in echo; JSON parser sees raw |
| Argo API key (STRATUS remote embed/distill) | env / keychain | — | only if `STRATUS_EMBED=remote` |

---

End of delta. Merge what you agree with into the canonical doc + `deploy/` tree;
ping me on anything you want changed. The plists/conf above are the REAL m1
artifacts, tokenized — they're known-good.

Last updated: 2026-06-23 by kukla
