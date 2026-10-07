#!/usr/bin/env python3
"""Generate mode-0600 proxy configuration without printing credentials."""
import argparse, hashlib, json, secrets
from pathlib import Path

AGENTS = ("yeil", "ikto", "tsisdu", "kukla", "ollie")
SHARED = {"yeil", "ikto", "tsisdu"}
p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, default=Path(__file__).with_name("runtime") / "family-proxy.json")
p.add_argument("--bind", default="127.0.0.1")
a = p.parse_args()
a.output.parent.mkdir(parents=True, exist_ok=True)
tokens = {secrets.token_urlsafe(32): {"tenant": x, "label": x.title(), "pools": ["family-room"] if x in SHARED else []} for x in AGENTS}
config = {"upstream": "http://127.0.0.1:18078", "bind": a.bind, "port": 18079,
          "max_body": 1048576, "audit_path": str(a.output.with_name("audit.ndjson")), "tokens": tokens}
a.output.write_text(json.dumps(config, indent=2) + "\n")
a.output.chmod(0o600)
print(json.dumps({"config": str(a.output), "mode": oct(a.output.stat().st_mode & 0o777),
                  "principals": {v["tenant"]: hashlib.sha256(k.encode()).hexdigest()[:12]
                                 for k, v in tokens.items()}}, sort_keys=True))
