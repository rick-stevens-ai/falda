#!/usr/bin/env python3
"""Freeze a deterministic, token-free FALDA demo evidence bundle."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import statistics
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "password", "passwd", "secret", "token", "tokens"}
SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def redact(value: Any, key: str = "") -> Any:
    normalized = key.lower().replace("-", "_")
    if normalized in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def assert_no_secret_text(text: str, source: str, forbidden_values: tuple[str, ...] = ()) -> None:
    for value in forbidden_values:
        if value and value in text:
            raise ValueError(f"configured secret value remains in {source}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"secret-like value remains in {source}")


def copy_redacted_json(source: Path, destination: Path, ndjson: bool = False,
                       forbidden_values: tuple[str, ...] = ()) -> None:
    if ndjson:
        records = [redact(json.loads(line)) for line in source.read_text().splitlines() if line.strip()]
        text = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    else:
        text = json.dumps(redact(json.loads(source.read_text())), indent=2, sort_keys=True) + "\n"
    assert_no_secret_text(text, source.name, forbidden_values)
    destination.write_text(text)


def freeze(runtime: Path, output: Path, commit: str, tree: str, freeze_utc: str) -> dict[str, Any]:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    evidence = runtime / "evidence"
    forbidden_values: tuple[str, ...] = ()
    config = runtime / "family-proxy.json"
    if config.exists():
        raw_config = json.loads(config.read_text())
        forbidden_values = tuple(str(value) for value in raw_config.get("tokens", {}).keys())
    with (evidence / "results.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("results.csv is empty")
    latencies = [float(row["latency_ms"]) for row in rows]
    by_test: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_test.setdefault(row["test"], {"n": 0, "pass": 0, "latency_ms": []})
        item["n"] += 1
        item["pass"] += row["passed"] == "True"
        item["latency_ms"].append(float(row["latency_ms"]))
    summary = {
        "schema": "falda-family-summary/v1", "commit": commit, "tree": tree,
        "rows": len(rows), "passes": sum(row["passed"] == "True" for row in rows),
        "failures": sum(row["passed"] != "True" for row in rows),
        "phases": sorted({row["phase"] for row in rows}),
        "latency_ms": {"min": min(latencies), "median": statistics.median(latencies), "max": max(latencies)},
        "by_test": {name: {"n": item["n"], "passes": item["pass"],
                            "median_latency_ms": statistics.median(item["latency_ms"])}
                    for name, item in sorted(by_test.items())},
        "limitations": [
            "Hybrid API exposes fused score but not dense-vs-lexical channel attribution.",
            "Synthetic corpus and single-host service are not production-scale evidence.",
        ],
        "production_status": "not certified by this isolated experiment",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    for name in ("results.json", "results.csv"):
        data = (evidence / name).read_text()
        assert_no_secret_text(data, name, forbidden_values)
        (output / name).write_text(data)
    for name in ("manifest.json", "pre.out", "post.out", "cleanup.out", "backend.log", "proxy.log", "audit.ndjson"):
        source = runtime / name
        if not source.exists():
            continue
        destination = output / name
        if name == "manifest.json":
            copy_redacted_json(source, destination, forbidden_values=forbidden_values)
        elif name == "audit.ndjson":
            copy_redacted_json(source, destination, ndjson=True, forbidden_values=forbidden_values)
        else:
            text = source.read_text()
            assert_no_secret_text(text, name, forbidden_values)
            destination.write_text(text)
    identity = {
        "schema": "falda-family-freeze/v1", "commit": commit, "tree": tree,
        "freeze_utc": freeze_utc, "token_files_included": False,
    }
    (output / "identity.json").write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha(path)}  {path.relative_to(output).as_posix()}\n" for path in files)
    )
    return {"files": len(files), "sha256sums_sha256": sha(output / "SHA256SUMS"),
            "rows": len(rows), "passes": summary["passes"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--freeze-utc", required=True,
                        help="Explicit RFC3339 timestamp; identical inputs and timestamp produce identical bytes")
    args = parser.parse_args()
    print(json.dumps(freeze(args.runtime, args.output, args.commit, args.tree, args.freeze_utc), sort_keys=True))


if __name__ == "__main__":
    main()
