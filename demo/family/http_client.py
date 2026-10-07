"""Tiny stdlib JSON HTTP client used by family demo CLIs."""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any


def request(base: str, method: str, route: str, body: dict | None = None,
            token: str | None = None, timeout: float = 60.0) -> tuple[int, Any, float]:
    import time
    headers = {"accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base.rstrip("/") + route, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"{}"), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            value = {"error": raw.decode(errors="replace")}
        return error.code, value, (time.perf_counter() - started) * 1000
