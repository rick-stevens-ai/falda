#!/usr/bin/env python3
"""
FALDA Access Proxy — controlled public front-end for external demo agents.

Why this exists
---------------
The FALDA gateway (:8078) has NO inbound authentication: it reads the caller's
`tenant` straight from the request JSON body (`tenant = body.tenant ?? "default"`).
Anyone who can reach :8078 can therefore impersonate ANY tenant (ollie, kukla,
default, ...) and read/write that tenant's private store. That is fine on a
trusted loopback/tailnet, but unacceptable for external demo partners.

This proxy is the control layer. It runs on a public VPS that is ALSO on our
tailnet, so it can reach FALDA privately while exposing only a hardened surface:

  external agent  --HTTPS + Bearer token-->  proxy (public:8444)
                                               |  (token -> tenant map)
                                               |  (tenant CLAMP + route allowlist)
                                               v
                                     FALDA gateway (tailnet 100.87.221.1:8078)

Controls enforced here (NOT in FALDA core):
  1. TLS termination (self-signed; clients use -k, same pattern as the CELS
     model front-end already on this box).
  2. Bearer-token auth. Unknown/missing token -> 401.
  3. Tenant CLAMP: whatever tenant the caller puts in the body is DISCARDED and
     replaced with the tenant bound to their token. Spoofing is impossible.
  4. Route allowlist. Only read/append + own-pool-listing routes are exposed.
     Pool administration (/pools/declare, /pools/grant) stays internal-only.
  5. Pool allowlist per token: a caller can only address "self" or pools in
     their token's allowed set.

Config: JSON file (default ./falda_proxy_tokens.json), hot-read per request:
  {
    "upstream": "http://100.87.221.1:8078",
    "tokens": {
      "<opaque-token-string>": {
        "tenant": "crush",
        "pools": ["external-demo"],        # pools this token may address (besides "self")
        "label": "Crush (Brian Spears' agent)"
      }
    }
  }

Env:
  FALDA_PROXY_PORT      (default 8444)
  FALDA_PROXY_BIND      (default 0.0.0.0)
  FALDA_PROXY_TOKENS    (default ./falda_proxy_tokens.json)
  FALDA_PROXY_CERT      (default ./falda_proxy.crt)
  FALDA_PROXY_KEY       (default ./falda_proxy.key)
  FALDA_PROXY_UPSTREAM  (overrides upstream in tokens file)

Stdlib only. Python 3.8+.
"""
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Routes an external token is allowed to hit. Everything else -> 403.
ALLOWED_ROUTES = {
    "/stream/add",
    "/stream/search",
    "/atoms/upsert",
    "/atoms/search",
    "/atoms/query",
    "/pools/mine",
    "/health",
}
# Routes that carry a {tenant, pool} body we must clamp/validate.
DATA_ROUTES = {"/stream/add", "/stream/search", "/atoms/upsert", "/atoms/search", "/atoms/query"}

TOKENS_PATH = os.environ.get("FALDA_PROXY_TOKENS", "falda_proxy_tokens.json")
PORT = int(os.environ.get("FALDA_PROXY_PORT", "8444"))
BIND = os.environ.get("FALDA_PROXY_BIND", "0.0.0.0")
CERT = os.environ.get("FALDA_PROXY_CERT", "falda_proxy.crt")
KEY = os.environ.get("FALDA_PROXY_KEY", "falda_proxy.key")
MAX_BODY = 4 * 1024 * 1024  # 4 MiB


def load_cfg():
    with open(TOKENS_PATH, "r") as f:
        cfg = json.load(f)
    up = os.environ.get("FALDA_PROXY_UPSTREAM") or cfg.get("upstream") or "http://100.87.221.1:8078"
    return up.rstrip("/"), cfg.get("tokens", {})


def log(*a):
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}]", *a, flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "falda-access-proxy/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code, msg):
        self._send(code, {"error": msg})

    def do_GET(self):
        # Only /health is allowed unauthenticated as a liveness ping.
        if self.path == "/health":
            return self._send(200, {"ok": True, "proxy": "falda-access-proxy"})
        return self._deny(404, "not found")

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route not in ALLOWED_ROUTES:
            log("DENY route", route)
            return self._deny(403, f"route not permitted: {route}")

        # --- auth ---
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        try:
            upstream, tokens = load_cfg()
        except Exception as e:
            log("CONFIG ERROR", e)
            return self._deny(500, "proxy misconfigured")
        princ = tokens.get(token)
        if not princ:
            log("DENY auth", route, "token-prefix", (token[:6] + "…") if token else "(none)")
            return self._deny(401, "unauthorized")
        tenant = princ["tenant"]
        allowed_pools = set(princ.get("pools", []))

        # --- body ---
        n = int(self.headers.get("content-length", 0) or 0)
        if n > MAX_BODY:
            return self._deny(413, "body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._deny(400, "invalid json body")

        # --- tenant clamp + pool allowlist ---
        if route in DATA_ROUTES:
            body["tenant"] = tenant  # HARD override — spoofing impossible
            pool = body.get("pool")
            if pool not in (None, "self") and pool not in allowed_pools:
                log("DENY pool", tenant, "->", pool)
                return self._deny(403, f"tenant {tenant} may not address pool {pool}")
        elif route == "/pools/mine":
            body = {"tenant": tenant}  # can only ever list own pools

        # --- forward to FALDA over tailnet ---
        out = json.dumps(body).encode()
        req = urllib.request.Request(
            upstream + route, data=out,
            headers={"content-type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                log("OK", tenant, route, r.status)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            log("UPSTREAM-ERR", tenant, route, e.code)
        except Exception as e:
            log("UPSTREAM-FAIL", tenant, route, repr(e))
            return self._deny(502, "upstream error")

    def log_message(self, *a):
        pass  # we do our own logging


def main():
    try:
        upstream, tokens = load_cfg()
    except Exception as e:
        print(f"FATAL: cannot load {TOKENS_PATH}: {e}", file=sys.stderr)
        sys.exit(1)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    log(f"FALDA access proxy on https://{BIND}:{PORT} -> {upstream}  ({len(tokens)} token(s))")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
