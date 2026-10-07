#!/usr/bin/env python3
"""Initialize and verify the isolated five-member FALDA family demonstration."""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from demo.family.common import AGENTS, FACTS, FAMILY_POOL, PARAPHRASES, SHARED_FACTS, SHARED_MEMBERS, build_manifest, validate_isolated_url, validate_proxy_url
from demo.family.http_client import request


def atom_ids(manifest: dict, agent: str, shared: bool = False) -> list[str]:
    key = "shared_atom_id" if shared else "private_atom_id"
    ids = [manifest["agents"][agent][key]]
    scope = FAMILY_POOL if shared else agent
    ids.extend(item["id"] for item in manifest["distractors"][scope])
    return ids


class Harness:
    def __init__(self, backend: str, proxy: str, tokens: dict[str, str], manifest: dict, evidence_dir: Path):
        validate_isolated_url(backend); validate_proxy_url(proxy)
        self.backend, self.proxy, self.tokens, self.m = backend, proxy, tokens, manifest
        self.out = evidence_dir; self.results: list[dict[str, Any]] = []

    def api(self, route: str, body: dict, agent: str | None = None, admin: bool = False):
        base = self.backend if admin else self.proxy
        try:
            return request(base, "POST", route, body, None if admin else self.tokens[agent or "yeil"])
        except Exception as exc:
            return 599, {"error": str(exc)}, 0.0

    def all_atom_ids(self, agent: str, pool: str = "self", page_size: int = 1000) -> tuple[int, set[str]]:
        ids: set[str] = set()
        offset = 0
        expected_total: int | None = None
        while True:
            body = {"limit": page_size, "offset": offset}
            if pool != "self": body["pool"] = pool
            status, result, _ = self.api("/atoms/query", body, agent)
            if status != 200: return status, ids
            page = result.get("items", [])
            before = len(ids)
            ids.update(x.get("id") for x in page if x.get("id"))
            total = int(result.get("total", len(page)))
            if expected_total is None: expected_total = total
            if total != expected_total: return 598, ids
            if not page and offset < expected_total: return 598, ids
            if page and len(ids) == before and offset + len(page) < expected_total: return 598, ids
            offset += len(page)
            if offset >= expected_total: return (status if len(ids) == expected_total else 598), ids

    def record(self, phase: str, agent: str, name: str, passed: bool, status: int,
               latency: float, detail: str = "", score: float | None = None) -> None:
        self.results.append({"phase": phase, "agent": agent, "test": name, "passed": passed,
                             "status": status, "latency_ms": round(latency, 3), "score": score,
                             "detail": detail})

    def initialize(self) -> None:
        status, body, _ = self.api("/pools/declare", {"name": FAMILY_POOL,
            "members": {a: "readwrite" for a in SHARED_MEMBERS}, "description": "isolated family demo"}, admin=True)
        if status == 400 and body.get("code") == "exists":
            status, body, _ = self.api("/pools/update", {"name": FAMILY_POOL,
                "members": {a: "readwrite" for a in SHARED_MEMBERS}, "description": "isolated family demo"}, admin=True)
        if status != 200:
            raise RuntimeError(f"pool initialization failed: {status} {body}")

    def plant(self) -> None:
        for agent in AGENTS:
            private = [{"id": self.m["agents"][agent]["private_atom_id"], "type": "fact", "content": FACTS[agent]}]
            private += self.m["distractors"][agent]
            status, body, _ = self.api("/atoms/upsert", {"atoms": private}, agent)
            if status != 200: raise RuntimeError(f"private plant {agent}: {status} {body}")
        shared = [{"id": self.m["agents"][a]["shared_atom_id"], "type": "fact", "content": SHARED_FACTS[a]} for a in SHARED_MEMBERS]
        shared += self.m["distractors"][FAMILY_POOL]
        status, body, _ = self.api("/atoms/upsert", {"pool": FAMILY_POOL, "atoms": shared}, "yeil")
        if status != 200: raise RuntimeError(f"shared plant: {status} {body}")

    def snapshot(self) -> None:
        """Capture mutable non-ID-addressed state so cleanup can restore it."""
        if "baseline_core" not in self.m:
            status, body, _ = self.api("/core/read", {}, "yeil")
            if status != 200: raise RuntimeError(f"core snapshot failed: {status} {body}")
            self.m["baseline_core"] = body.get("content", "")

    def run_tests(self, phase: str) -> None:
        for agent in AGENTS:
            target = self.m["agents"][agent]["private_atom_id"]
            for name, query_text in (("exact", FACTS[agent]), ("semantic", PARAPHRASES[agent])):
                status, body, ms = self.api("/atoms/search", {"query": query_text, "limit": 10}, agent)
                hits = body.get("items", [])
                hit = next((x for x in hits if x.get("id") == target), None)
                self.record(phase, agent, name, status == 200 and hit is not None, status, ms,
                            "hybrid search; channel provenance unavailable (API_OBSERVABILITY_GAP)",
                            hit.get("score") if hit else None)
            other = AGENTS[(AGENTS.index(agent) + 1) % len(AGENTS)]
            status, body, ms = self.api("/atoms/search", {"query": FACTS[other], "limit": 30}, agent)
            leaked = any(x.get("id") == self.m["agents"][other]["private_atom_id"] for x in body.get("items", []))
            self.record(phase, agent, "negative", status == 200 and not leaked, status, ms)
            status, body, ms = self.api("/atoms/search", {"tenant": other, "query": FACTS[agent], "limit": 10}, agent)
            own = any(x.get("id") == target for x in body.get("items", []))
            self.record(phase, agent, "spoof", status == 200 and own, status, ms, "spoofed tenant was clamped")
            shared_target = self.m["agents"]["yeil"]["shared_atom_id"]
            status, body, ms = self.api("/atoms/search", {"pool": FAMILY_POOL, "query": SHARED_FACTS["yeil"], "limit": 10}, agent)
            hit = next((x for x in body.get("items", []) if x.get("id") == shared_target), None)
            expected_shared = agent in SHARED_MEMBERS
            self.record(phase, agent, "shared", (status == 200 and hit is not None) if expected_shared else (status in (403, 404) and hit is None), status, ms,
                        "score recorded; channel provenance unavailable", hit.get("score") if hit else None)
        self._tier_tests(phase)

    def _tier_tests(self, phase: str) -> None:
        a, prefix = "yeil", f"family-{self.m['run_id']}"
        sid, stream_id = prefix + "-session", prefix + "-stream"
        if phase == "post-restart":
            status, body, ms = self.api("/stream/query", {"session_id": sid}, a)
            tier0_ok = any(x.get("id") == stream_id for x in body.get("messages", []))
        else:
            status, body, ms = self.api("/stream/add", {"session_id": sid, "messages": [{"id": stream_id, "role": "user", "content": "family T0 sentinel"}]}, a)
            tier0_ok = stream_id in body.get("accepted_ids", [])
        self.record(phase, a, "T0", status == 200 and tier0_ok, status, ms)
        target = self.m["agents"][a]["private_atom_id"]
        status, body, ms = self.api("/atoms/search", {"query": FACTS[a], "limit": 10}, a)
        self.record(phase, a, "T1", any(x.get("id") == target for x in body.get("items", [])), status, ms)
        scene = prefix + "/scene.md"
        if phase != "post-restart": self.api("/scenes/write", {"path": scene, "content": "family T2 sentinel"}, a)
        status, body, ms = self.api("/scenes/read", {"path": scene}, a)
        self.record(phase, a, "T2", status == 200 and body.get("content") == "family T2 sentinel", status, ms)
        if phase != "post-restart": self.api("/core/write", {"content": "family T3 sentinel " + self.m["run_id"]}, a)
        status, body, ms = self.api("/core/read", {}, a)
        self.record(phase, a, "T3", status == 200 and self.m["run_id"] in body.get("content", ""), status, ms)
        ghost = prefix + "-ghost"
        status, body, ms = self.api("/atoms/delete", {"ids": [ghost]}, a)
        self.record(phase, a, "delete-ghost", status == 200 and body.get("deleted_count") == 0, status, ms)
        status, _, ms = self.api("/atoms/search", {"query": "family", "limit": 1}, a)
        self.record(phase, a, "latency", status == 200, status, ms, "observational; no pass threshold")

    def cleanup(self) -> None:
        failures = []
        for agent in AGENTS:
            status, body, _ = self.api("/atoms/delete", {"ids": atom_ids(self.m, agent)}, agent)
            if status != 200: failures.append(f"{agent} atoms: {status} {body}")
        status, body, _ = self.api("/atoms/delete", {"pool": FAMILY_POOL, "ids": atom_ids(self.m, "yeil", True)}, "yeil")
        if status != 200: failures.append(f"pool atoms: {status} {body}")
        shared_extra = [self.m["agents"][a]["shared_atom_id"] for a in AGENTS if a != "yeil"]
        status, body, _ = self.api("/atoms/delete", {"pool": FAMILY_POOL, "ids": shared_extra}, "yeil")
        if status != 200: failures.append(f"pool extras: {status} {body}")
        status, body, _ = self.api("/stream/delete", {"session_id": f"family-{self.m['run_id']}-session"}, "yeil")
        if status != 200: failures.append(f"stream: {status} {body}")
        status, body, _ = self.api("/scenes/rm", {"path": f"family-{self.m['run_id']}/scene.md"}, "yeil")
        if status != 200: failures.append(f"scene: {status} {body}")
        if "baseline_core" in self.m:
            status, body, _ = self.api("/core/write", {"content": self.m["baseline_core"]}, "yeil")
            if status != 200: failures.append(f"core restore: {status} {body}")
        expected_private = {
            agent: {self.m["agents"][agent]["private_atom_id"],
                    *(x["id"] for x in self.m["distractors"][agent])}
            for agent in AGENTS
        }
        for agent in AGENTS:
            status, present = self.all_atom_ids(agent)
            if status != 200 or present & expected_private[agent]:
                failures.append(f"{agent} private residue")
        expected_shared = {self.m["agents"][a]["shared_atom_id"] for a in SHARED_MEMBERS}
        expected_shared.update(x["id"] for x in self.m["distractors"][FAMILY_POOL])
        status, present = self.all_atom_ids("yeil", FAMILY_POOL)
        if status != 200 or present & expected_shared: failures.append("shared pool residue")
        status, body, _ = self.api("/stream/query", {"session_id": f"family-{self.m['run_id']}-session", "limit": 10000}, "yeil")
        if status != 200 or body.get("total") != 0: failures.append("stream residue")
        status, body, _ = self.api("/scenes/read", {"path": f"family-{self.m['run_id']}/scene.md"}, "yeil")
        if status != 200 or body.get("content") is not None: failures.append("scene residue")
        status, body, _ = self.api("/core/read", {}, "yeil")
        if status != 200 or body.get("content", "") != self.m.get("baseline_core", ""): failures.append("core not restored")
        if failures: raise RuntimeError("cleanup failed: " + "; ".join(failures))
        self.m["cleanup_checked"] = True

    def write_evidence(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "results.json").write_text(json.dumps({"manifest": self.m, "results": self.results}, indent=2) + "\n")
        fields = ["phase", "agent", "test", "passed", "status", "latency_ms", "score", "detail"]
        with (self.out / "results.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(self.results)


def load_tokens(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text())["tokens"]
    return {principal["tenant"]: token for token, principal in raw.items()}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=("run", "pre-restart", "post-restart", "cleanup"), nargs="?", default="run")
    p.add_argument("--backend", default="http://127.0.0.1:18078"); p.add_argument("--proxy", default="http://100.86.220.115:18079")
    p.add_argument("--config", type=Path, default=Path(__file__).with_name("runtime") / "family-proxy.json")
    p.add_argument("--manifest", type=Path, default=Path(__file__).with_name("runtime") / "manifest.json")
    p.add_argument("--evidence", type=Path, default=Path(__file__).with_name("evidence")); p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--run-id", help="stable identifier; with --seed makes a byte-stable new manifest")
    args = p.parse_args()
    if args.manifest.exists(): manifest = json.loads(args.manifest.read_text())
    elif args.phase in ("post-restart", "cleanup"): p.error("manifest does not exist; run pre-restart first")
    else:
        run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)
        manifest = build_manifest(run_id, args.seed)
        args.manifest.parent.mkdir(parents=True, exist_ok=True); args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    harness = Harness(args.backend, args.proxy, load_tokens(args.config), manifest, args.evidence)
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    previous = args.evidence / "results.json"
    if args.phase == "post-restart" and previous.exists():
        old = json.loads(previous.read_text())
        if old.get("manifest", {}).get("run_id") == manifest["run_id"]:
            harness.results.extend(old.get("results", []))
    clean_on_exit = args.phase in ("run", "post-restart", "cleanup")
    completed = False
    try:
        if args.phase in ("run", "pre-restart"):
            harness.initialize(); harness.snapshot()
            args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            harness.plant()
        if args.phase != "cleanup": harness.run_tests(args.phase)
        completed = True
    finally:
        # A successful pre-restart intentionally preserves state. Every other
        # exit, including a failed/interrupted pre-restart, attempts cleanup.
        if clean_on_exit or not completed: harness.cleanup()
        if args.phase != "cleanup": harness.write_evidence()
    failed = [r for r in harness.results if not r["passed"]]
    print(json.dumps({"results": len(harness.results), "failed": len(failed), "evidence": str(args.evidence)}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
