#!/usr/bin/env python3
"""Run one family member's authenticated memory exercise and emit a hash-only receipt."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from demo.family.common import AGENTS, FAMILY_POOL, make_receipt
from demo.family.http_client import request


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("agent", choices=AGENTS); p.add_argument("--run-id", required=True)
    p.add_argument("--private-fact", required=True); p.add_argument("--paraphrase", required=True,
        help="A semantically equivalent query that does not repeat the private fact verbatim")
    p.add_argument("--shared-fact")
    p.add_argument("--proxy", default="http://127.0.0.1:18079")
    p.add_argument("--token", default=os.getenv("FALDA_FAMILY_TOKEN")); p.add_argument("--receipt", type=Path)
    args = p.parse_args()
    if not args.token: p.error("--token or FALDA_FAMILY_TOKEN is required")
    checks = []
    status, who, _ = request(args.proxy, "GET", "/whoami", token=args.token)
    checks.append({"name": "whoami", "passed": status == 200 and who.get("tenant") == args.agent, "status": status})
    private_id = f"participant-{args.run_id}-{args.agent}-private"
    status, _, _ = request(args.proxy, "POST", "/atoms/upsert", {"id": private_id, "type": "participant-fact", "content": args.private_fact}, args.token)
    checks.append({"name": "plant-private", "passed": status == 200, "status": status})
    if args.shared_fact:
        shared_id = f"participant-{args.run_id}-{args.agent}-shared"
        status, _, _ = request(args.proxy, "POST", "/atoms/upsert", {"pool": FAMILY_POOL, "id": shared_id, "type": "participant-fact", "content": args.shared_fact}, args.token)
        checks.append({"name": "plant-shared", "passed": status == 200, "status": status})
    status, found, _ = request(args.proxy, "POST", "/atoms/search", {"query": args.paraphrase, "limit": 20}, args.token)
    checks.append({"name": "paraphrase-recall", "passed": status == 200 and any(x.get("id") == private_id for x in found.get("items", [])), "status": status,
                   "score": next((x.get("score") for x in found.get("items", []) if x.get("id") == private_id), None),
                   "provenance": "unavailable: API_OBSERVABILITY_GAP"})
    other = AGENTS[(AGENTS.index(args.agent) + 1) % len(AGENTS)]
    status, spoofed, _ = request(args.proxy, "POST", "/atoms/search", {"tenant": other, "query": args.private_fact, "limit": 20}, args.token)
    checks.append({"name": "spoof-clamped", "passed": status == 200 and any(x.get("id") == private_id for x in spoofed.get("items", [])), "status": status})
    receipt = make_receipt(args.agent, args.run_id, args.private_fact, args.shared_fact, args.token, checks)
    output = args.receipt or Path(f"receipt-{args.agent}-{args.run_id}.json")
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(output), "passed": all(c["passed"] for c in checks)}))
    return 0 if all(c["passed"] for c in checks) else 1


if __name__ == "__main__": raise SystemExit(main())
