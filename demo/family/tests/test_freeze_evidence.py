import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from demo.family.freeze_evidence import freeze


class FreezeEvidenceTests(unittest.TestCase):
    def fixture(self, root: Path, leak: bool = True) -> Path:
        runtime = root / "runtime"
        evidence = runtime / "evidence"
        evidence.mkdir(parents=True)
        (evidence / "results.csv").write_text(
            "phase,agent,test,passed,status,latency_ms,score,detail\n"
            "pre,yeil,exact,True,200,1.25,0.5,ok\n"
        )
        (evidence / "results.json").write_text(json.dumps({"results": [{"passed": True}]}) + "\n")
        (runtime / "manifest.json").write_text(json.dumps({"run_id": "r", "token": "DUMMY_SUPER_SECRET_TOKEN"}) + "\n")
        (runtime / "audit.ndjson").write_text(json.dumps({"authorization": "Bearer DUMMY_SUPER_SECRET_TOKEN", "status": 200}) + "\n")
        (runtime / "family-proxy.json").write_text(json.dumps({"tokens": {"DUMMY_SUPER_SECRET_TOKEN": {"tenant": "yeil"}}}) + "\n")
        (evidence / "results.json").write_text(json.dumps({"results": [{"detail": "DUMMY_SUPER_SECRET_TOKEN" if leak else "safe"}]}) + "\n")
        (runtime / "pre.out").write_text("ok\n")
        return runtime

    def test_freeze_redacts_structured_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); runtime = self.fixture(root); out = root / "out"
            with self.assertRaises(ValueError):
                freeze(runtime, out, "c" * 40, "t" * 40, "2026-10-07T06:00:00Z")

    def test_freeze_is_byte_deterministic_for_identical_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); runtime = self.fixture(root, leak=False); a = root / "a"; b = root / "b"
            args = (runtime, a, "c" * 40, "t" * 40, "2026-10-07T06:00:00Z")
            freeze(*args)
            freeze(runtime, b, "c" * 40, "t" * 40, "2026-10-07T06:00:00Z")
            def digest_map(folder):
                return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in folder.rglob("*") if p.is_file()}
            self.assertEqual(digest_map(a), digest_map(b))


if __name__ == "__main__":
    unittest.main()
