"""Unit tests for `ci_artifact_manifests` (issue #271). Stdlib only."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ci_artifact_manifests as C  # noqa: E402


def entry(data: bytes, name: str = "sim/x/corners/r/a.dat", uri: str | None = None) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    return {"name": name, "sha256": sha, "size": len(data), "kind": "evidence",
            "producer": {"tool": "klt"}, "uri": uri or f"artifact:evidence/x/{sha[:2]}/{sha}"}


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "manifests" / "artifacts").mkdir(parents=True)

    def tearDown(self):
        self._t.cleanup()

    def put(self, doc, name="b-r.manifest.json"):
        (self.root / "manifests" / "artifacts" / name).write_text(json.dumps(doc))

    def commit_file(self, rel: str, data: bytes):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        subprocess.run(["git", "-C", str(self.root), "add", rel], check=True)

    def test_clean_pass(self):
        self.put({"schema_version": 1, "entries": [entry(b"one")]})
        self.commit_file("sim/readme.txt", b"unrelated")
        self.assertEqual(C.run(self.root), [])

    def test_bad_schema(self):
        self.put({"schema_version": 2, "entries": [entry(b"one")]})
        self.assertTrue(C.run(self.root))

    def test_empty_manifest(self):
        self.put({"schema_version": 1, "entries": []})
        self.assertTrue(any("no entries" in p for p in C.run(self.root)))

    def test_non_artifact_uri(self):
        self.put({"schema_version": 1, "entries": [entry(b"one", uri="s3://bkt/k")]})
        probs = C.run(self.root)
        self.assertTrue(any("artifact:" in p for p in probs))
        self.assertTrue(any("s3://" in p for p in probs))

    def test_uri_not_naming_sha(self):
        self.put({"schema_version": 1, "entries": [entry(b"one", uri="artifact:evidence/x/" + "0" * 64)]})
        self.assertTrue(any("sha256" in p for p in C.run(self.root)))

    def test_duplicate_name(self):
        self.put({"schema_version": 1, "entries": [entry(b"one"), entry(b"two")]})
        self.assertTrue(any("duplicate" in p for p in C.run(self.root)))

    def test_committed_bytes_collision(self):
        self.put({"schema_version": 1, "entries": [entry(b"stored bytes")]})
        self.commit_file("sim/x/corners/old/a.dat", b"stored bytes")
        self.assertTrue(any("tracked in git" in p for p in C.run(self.root)))

    def test_same_size_different_bytes_is_clean(self):
        self.put({"schema_version": 1, "entries": [entry(b"aaaa")]})
        self.commit_file("sim/y.dat", b"bbbb")
        self.assertEqual(C.run(self.root), [])


if __name__ == "__main__":
    unittest.main()
