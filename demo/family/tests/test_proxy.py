import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from demo.family.proxy import ProxyConfig, make_server


class Upstream(BaseHTTPRequestHandler):
    requests = []
    healthy = True

    def do_GET(self):
        if self.path == "/healthz" and self.healthy:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(503)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.requests.append((self.path, body))
        data = json.dumps({"route": self.path, "body": body}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class ProxyTests(unittest.TestCase):
    def setUp(self):
        Upstream.requests = []
        Upstream.healthy = True
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.ndjson"
        cfg = ProxyConfig(
            upstream=f"http://127.0.0.1:{self.upstream.server_port}",
            bind="127.0.0.1",
            port=0,
            max_body=128,
            audit_path=str(self.audit),
            tokens={"secret-token": {"tenant": "yeil", "pools": ["family-room"], "label": "Yeil"}},
        )
        self.proxy = make_server(cfg)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close()
        self.upstream.shutdown(); self.upstream.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token=None):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        if token:
            headers["authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_whoami_authenticates_without_disclosing_token(self):
        status, body = self.request("GET", "/whoami", token="secret-token")
        self.assertEqual(200, status)
        self.assertEqual({"tenant": "yeil", "label": "Yeil", "pools": ["family-room"]}, body)
        self.assertNotIn("secret-token", self.audit.read_text())

    def test_clamps_spoofed_tenant_and_allows_named_pool(self):
        status, response = self.request("POST", "/atoms/search", {"tenant": "ollie", "pool": "family-room", "query": "x"}, "secret-token")
        self.assertEqual(200, status)
        self.assertEqual("yeil", response["body"]["tenant"])

    def test_denies_unlisted_pool_and_pool_admin(self):
        self.assertEqual(403, self.request("POST", "/atoms/search", {"pool": "other", "query": "x"}, "secret-token")[0])
        self.assertEqual(403, self.request("POST", "/pools/declare", {}, "secret-token")[0])
        self.assertEqual([], Upstream.requests)

    def test_allows_tier_routes_but_rejects_oversize_and_bad_auth(self):
        route_bodies = {
            "/scenes/ls": {}, "/scenes/read": {"path": "safe.md"},
            "/scenes/write": {"path": "safe.md", "content": "x"},
            "/scenes/rm": {"path": "safe.md"},
        }
        for route in ("/stream/add", "/stream/query", "/stream/search", "/stream/delete",
                      "/atoms/upsert", "/atoms/query", "/atoms/search", "/atoms/delete",
                      "/scenes/ls", "/scenes/read", "/scenes/write", "/scenes/rm",
                      "/core/read", "/core/write", "/pools/mine"):
            self.assertEqual(200, self.request("POST", route, route_bodies.get(route, {}), "secret-token")[0], route)
        self.assertEqual(401, self.request("POST", "/atoms/search", {}, "wrong")[0])
        self.assertEqual(413, self.request("POST", "/atoms/upsert", {"content": "x" * 200}, "secret-token")[0])

    def test_readyz_checks_upstream(self):
        self.assertEqual(200, self.request("GET", "/readyz")[0])
        Upstream.healthy = False
        self.assertEqual(503, self.request("GET", "/readyz")[0])

    def test_rejects_scene_path_traversal_before_forwarding(self):
        for route in ("/scenes/read", "/scenes/write", "/scenes/rm"):
            for bad in ("../escape", "../../escape", "/tmp/escape", "ok/../../../escape", "..\\escape"):
                Upstream.requests = []
                status, _ = self.request("POST", route, {"path": bad, "content": "x"}, "secret-token")
                self.assertEqual(400, status, (route, bad))
                self.assertEqual([], Upstream.requests)

    def test_audit_is_ndjson_and_redacts_payload(self):
        self.request("POST", "/atoms/upsert", {"content": "TOP SECRET", "tenant": "ollie"}, "secret-token")
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertTrue(records)
        serialized = json.dumps(records)
        self.assertNotIn("TOP SECRET", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertEqual("yeil", records[-1]["tenant"])


if __name__ == "__main__":
    unittest.main()
