#!/usr/bin/env python3
"""Authenticated, tenant-clamping HTTP proxy for the isolated family demo."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DATA_ROUTES = frozenset({
    "/stream/add", "/stream/query", "/stream/search", "/stream/delete",
    "/atoms/upsert", "/atoms/query", "/atoms/search", "/atoms/delete",
    "/scenes/ls", "/scenes/read", "/scenes/write", "/scenes/rm",
    "/core/read", "/core/write",
})
ALLOWED_POST_ROUTES = DATA_ROUTES | {"/pools/mine"}


@dataclasses.dataclass(frozen=True)
class ProxyConfig:
    upstream: str
    tokens: dict[str, dict[str, Any]]
    bind: str = "127.0.0.1"
    port: int = 18079
    max_body: int = 1024 * 1024
    audit_path: str = "family-proxy-audit.ndjson"
    upstream_timeout: float = 30.0


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else "none"


def make_server(config: ProxyConfig) -> ThreadingHTTPServer:
    class FamilyServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    class Handler(BaseHTTPRequestHandler):
        server_version = "falda-family-proxy/1.0"

        def _send(self, status: int, value: Any) -> None:
            data = _json_bytes(value)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _audit(self, *, status: int, route: str, tenant: str | None = None,
                   token: str = "", outcome: str = "") -> None:
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "remote": self.client_address[0], "method": self.command,
                "route": route, "status": status, "tenant": tenant,
                "token_fingerprint": _token_fingerprint(token), "outcome": outcome,
            }
            path = Path(config.audit_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
            # One append call keeps records intact for normal small audit lines.
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)

        def _principal(self) -> tuple[str, dict[str, Any] | None]:
            auth = self.headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            principal = None
            # Avoid leaking timing differences between known token strings.
            for candidate, candidate_principal in config.tokens.items():
                if hmac.compare_digest(token, candidate):
                    principal = candidate_principal
            return token, principal

        def _forward(self, route: str, body: dict[str, Any]) -> tuple[int, bytes, str]:
            req = urllib.request.Request(
                config.upstream.rstrip("/") + route, data=_json_bytes(body), method="POST",
                headers={"content-type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=config.upstream_timeout) as response:
                    return response.status, response.read(), response.headers.get("content-type", "application/json")
            except urllib.error.HTTPError as error:
                return error.code, error.read(), error.headers.get("content-type", "application/json")

        def do_GET(self) -> None:
            route = self.path.split("?", 1)[0]
            if route == "/healthz":
                self._send(200, {"ok": True, "service": "falda-family-proxy"})
                return
            if route == "/readyz":
                try:
                    with urllib.request.urlopen(config.upstream.rstrip("/") + "/healthz", timeout=config.upstream_timeout) as response:
                        ready = response.status == 200
                except Exception:
                    ready = False
                self._send(200 if ready else 503, {"ok": ready, "dependency": "falda"})
                return
            token, principal = self._principal()
            if route != "/whoami":
                self._send(404, {"error": "not found"}); return
            if not principal:
                self._audit(status=401, route=route, token=token, outcome="unauthorized")
                self._send(401, {"error": "unauthorized"}); return
            tenant = str(principal["tenant"])
            self._audit(status=200, route=route, tenant=tenant, token=token, outcome="ok")
            self._send(200, {"tenant": tenant, "label": principal.get("label", tenant),
                             "pools": sorted(principal.get("pools", []))})

        def do_POST(self) -> None:
            route = self.path.split("?", 1)[0]
            token, principal = self._principal()
            if not principal:
                self._audit(status=401, route=route, token=token, outcome="unauthorized")
                self._send(401, {"error": "unauthorized"}); return
            tenant = str(principal["tenant"])
            if route not in ALLOWED_POST_ROUTES:
                self._audit(status=403, route=route, tenant=tenant, token=token, outcome="route_denied")
                self._send(403, {"error": f"route not permitted: {route}"}); return
            try:
                length = int(self.headers.get("content-length", "0"))
            except ValueError:
                self._send(400, {"error": "invalid content-length"}); return
            if length < 0 or length > config.max_body:
                self._audit(status=413, route=route, tenant=tenant, token=token, outcome="body_too_large")
                self._send(413, {"error": "body too large"}); return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("object required")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._send(400, {"error": "invalid JSON object"}); return
            if route in DATA_ROUTES:
                body["tenant"] = tenant
                pool = body.get("pool", "self")
                if pool != "self" and pool not in principal.get("pools", []):
                    self._audit(status=403, route=route, tenant=tenant, token=token, outcome="pool_denied")
                    self._send(403, {"error": f"pool not permitted: {pool}"}); return
                if route in {"/scenes/read", "/scenes/write", "/scenes/rm"}:
                    scene_path = body.get("path")
                    if (not isinstance(scene_path, str) or not scene_path or
                            "\\" in scene_path or "\x00" in scene_path or
                            scene_path.startswith("/") or ".." in scene_path.split("/")):
                        self._audit(status=400, route=route, tenant=tenant, token=token,
                                    outcome="invalid_scene_path")
                        self._send(400, {"error": "invalid scene path"}); return
            else:
                body = {"tenant": tenant}
            try:
                status, data, content_type = self._forward(route, body)
                self._audit(status=status, route=route, tenant=tenant, token=token,
                            outcome="upstream_ok" if status < 400 else "upstream_error")
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(data)))
                self.send_header("cache-control", "no-store")
                self.end_headers(); self.wfile.write(data)
            except Exception:
                self._audit(status=502, route=route, tenant=tenant, token=token, outcome="upstream_unavailable")
                self._send(502, {"error": "upstream unavailable"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return FamilyServer((config.bind, config.port), Handler)


def load_config(path: str) -> ProxyConfig:
    raw = json.loads(Path(path).read_text())
    return ProxyConfig(
        upstream=os.getenv("FALDA_FAMILY_UPSTREAM", raw.get("upstream", "http://127.0.0.1:18078")),
        tokens=raw["tokens"], bind=os.getenv("FALDA_FAMILY_PROXY_BIND", raw.get("bind", "127.0.0.1")),
        port=int(os.getenv("FALDA_FAMILY_PROXY_PORT", raw.get("port", 18079))),
        max_body=int(os.getenv("FALDA_FAMILY_MAX_BODY", raw.get("max_body", 1024 * 1024))),
        audit_path=os.getenv("FALDA_FAMILY_AUDIT", raw.get("audit_path", "family-proxy-audit.ndjson")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.getenv("FALDA_FAMILY_PROXY_CONFIG", "family-proxy.json"))
    args = parser.parse_args()
    config = load_config(args.config)
    server = make_server(config)
    print(f"family proxy http://{config.bind}:{server.server_port} -> {config.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
