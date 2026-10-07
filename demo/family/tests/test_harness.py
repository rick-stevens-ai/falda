import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from demo.family.common import AGENTS, FAMILY_POOL, build_manifest, make_receipt, validate_isolated_url
from demo.family.orchestrator import Harness


class HarnessContractTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_has_twenty_distractors_per_scope(self):
        one = build_manifest("run-123", seed=19)
        two = build_manifest("run-123", seed=19)
        self.assertEqual(one, two)
        self.assertEqual(list(AGENTS), list(one["agents"]))
        for agent in AGENTS:
            self.assertEqual(20, len(one["distractors"][agent]))
        self.assertEqual(20, len(one["distractors"][FAMILY_POOL]))
        ids = [x["id"] for values in one["distractors"].values() for x in values]
        self.assertEqual(len(ids), len(set(ids)))

    def test_receipt_contains_hashes_not_token_or_private_fact(self):
        receipt = make_receipt("yeil", "run-123", "private secret", "shared fact", "token-value", [{"name": "exact", "passed": True}])
        encoded = json.dumps(receipt)
        self.assertNotIn("token-value", encoded)
        self.assertNotIn("private secret", encoded)
        self.assertEqual(hashlib.sha256(b"private secret").hexdigest(), receipt["facts"]["private_sha256"])
        self.assertEqual(64, len(receipt["receipt_sha256"]))

    def test_urls_must_be_loopback_for_isolated_admin(self):
        validate_isolated_url("http://127.0.0.1:18078")
        validate_isolated_url("http://localhost:18079")
        with self.assertRaises(ValueError):
            validate_isolated_url("https://production.example")

    def test_cleanup_fails_closed_on_api_failure(self):
        manifest = build_manifest("cleanup-fail", seed=19)
        h = Harness("http://127.0.0.1:18078", "http://127.0.0.1:18079",
                    {a: "token" for a in AGENTS}, manifest, Path(tempfile.mkdtemp()))
        h.api = lambda *args, **kwargs: (599, {"error": "down"}, 0.0)
        with self.assertRaises(RuntimeError):
            h.cleanup()
        self.assertNotIn("cleanup_checked", manifest)

    def test_cleanup_rejects_successful_noop_when_exact_ids_remain(self):
        manifest = build_manifest("cleanup-noop", seed=19)
        h = Harness("http://127.0.0.1:18078", "http://127.0.0.1:18079",
                    {a: "token" for a in AGENTS}, manifest, Path(tempfile.mkdtemp()))
        remaining = manifest["agents"]["yeil"]["private_atom_id"]
        def fake_api(route, body, agent=None, admin=False):
            if route == "/atoms/query":
                return 200, {"items": [{"id": remaining}], "total": 1}, 0.0
            if route == "/stream/query":
                return 200, {"messages": [], "total": 0}, 0.0
            if route == "/scenes/read":
                return 200, {"content": None}, 0.0
            if route == "/core/read":
                return 200, {"content": ""}, 0.0
            return 200, {"deleted_count": 0}, 0.0
        h.api = fake_api
        with self.assertRaises(RuntimeError):
            h.cleanup()
        self.assertNotIn("cleanup_checked", manifest)

    def test_all_atom_ids_paginates_past_ten_thousand(self):
        manifest = build_manifest("cleanup-pages", seed=19)
        h = Harness("http://127.0.0.1:18078", "http://127.0.0.1:18079",
                    {a: "token" for a in AGENTS}, manifest, Path(tempfile.mkdtemp()))
        rows = [{"id": f"filler-{i}"} for i in range(10001)] + [{"id": "late-target"}]
        def fake_api(route, body, agent=None, admin=False):
            offset, limit = body.get("offset", 0), body.get("limit", 1000)
            return 200, {"items": rows[offset:offset+limit], "total": len(rows)}, 0.0
        h.api = fake_api
        status, ids = h.all_atom_ids("yeil")
        self.assertEqual(200, status)
        self.assertEqual(len(rows), len(ids))
        self.assertIn("late-target", ids)

    def test_all_atom_ids_rejects_premature_empty_page(self):
        manifest = build_manifest("cleanup-short-page", seed=19)
        h = Harness("http://127.0.0.1:18078", "http://127.0.0.1:18079",
                    {a: "token" for a in AGENTS}, manifest, Path(tempfile.mkdtemp()))
        h.api = lambda *args, **kwargs: (200, {"items": [], "total": 10001}, 0.0)
        status, ids = h.all_atom_ids("yeil")
        self.assertEqual(598, status)
        self.assertEqual(set(), ids)

    def test_all_atom_ids_rejects_duplicate_pages_hiding_residue(self):
        manifest = build_manifest("cleanup-duplicate-pages", seed=19)
        h = Harness("http://127.0.0.1:18078", "http://127.0.0.1:18079",
                    {a: "token" for a in AGENTS}, manifest, Path(tempfile.mkdtemp()))
        repeated = [{"id": f"same-{i}"} for i in range(1000)]
        h.api = lambda *args, **kwargs: (200, {"items": repeated, "total": 10001}, 0.0)
        status, ids = h.all_atom_ids("yeil")
        self.assertEqual(598, status)
        self.assertEqual(1000, len(ids))

    def test_all_atom_ids_rejects_total_drift(self):
        manifest = build_manifest("cleanup-total-drift", seed=19)
        h = Harness("http://127.0.0.1:18078", "http://127.0.0.1:18079",
                    {a: "token" for a in AGENTS}, manifest, Path(tempfile.mkdtemp()))
        rows = [{"id": f"row-{i}"} for i in range(2000)]
        calls = 0
        def shrinking(route, body, agent=None, admin=False):
            nonlocal calls
            calls += 1
            offset, limit = body.get("offset", 0), body.get("limit", 1000)
            total = 2000 if calls == 1 else 1000
            return 200, {"items": rows[offset:offset+limit], "total": total}, 0.0
        h.api = shrinking
        status, ids = h.all_atom_ids("yeil")
        self.assertEqual(598, status)
        self.assertEqual(2000, len(ids))


if __name__ == "__main__":
    unittest.main()
