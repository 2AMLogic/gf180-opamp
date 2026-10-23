"""Unit tests for `evidence_store` and the harness run-claim/finalize hook (issue #271).

No simulator, no broker: `klt` is a stub callable and the broker probe is a
stub. Covers the fail-closed contract: broker unset, put failing, success.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import evidence_store as ES  # noqa: E402
import harness  # noqa: E402


def fake_run(fail_on: str | None = None):
    """A `subprocess.run` stand-in that behaves like `klt artifact put`."""

    def run(cmd, **kw):
        if cmd[1:4] == ["artifact", "put", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["artifact", "put"]:
            path, name, manifest = cmd[3], cmd[cmd.index("--name") + 1], cmd[cmd.index("--manifest") + 1]
            if fail_on and fail_on in name:
                return subprocess.CompletedProcess(cmd, 1, "", '{"error": {"code": "broker_refused", "message": "no"}}')
            data = Path(path).read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            try:
                doc = json.loads(Path(manifest).read_text())
            except OSError:
                doc = {"schema_version": 1, "entries": []}
            doc["entries"].append({"name": name, "sha256": sha, "size": len(data), "kind": "evidence",
                                   "producer": {"tool": "klt"}, "uri": f"artifact:evidence/x/{sha[:2]}/{sha}"})
            Path(manifest).write_text(json.dumps(doc))
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        raise AssertionError(cmd)

    return run


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.stage = self.root / "stage"
        (self.stage / "sub").mkdir(parents=True)
        (self.stage / "a.dat").write_text("alpha")
        (self.stage / "sub" / "b.log").write_text("beta")
        self.record = self.root / "rec.md"
        self.record.write_text("# record\n")

    def tearDown(self):
        self._t.cleanup()

    def test_preflight_broker_unset(self):
        with self.assertRaisesRegex(ES.StoreError, "KLT_ARTIFACT_BROKER"):
            ES.preflight(self.root, env={}, which=lambda _: "/bin/klt", run=fake_run(), probe=lambda *a: None)

    def test_preflight_no_klt(self):
        with self.assertRaisesRegex(ES.StoreError, "not on PATH"):
            ES.preflight(self.root, env={ES.ENV_BROKER: "http://b", ES.ENV_REPO: "o/r"},
                         which=lambda _: None, run=fake_run(), probe=lambda *a: None)

    def test_preflight_ok(self):
        ES.preflight(self.root, env={ES.ENV_BROKER: "http://b", ES.ENV_REPO: "o/r"},
                     which=lambda _: "/bin/klt", run=fake_run(), probe=lambda *a: None)

    def test_put_failure_leaves_nothing(self):
        with mock.patch.object(ES.subprocess, "run", fake_run(fail_on="b.log")):
            with self.assertRaises(ES.StoreError):
                ES.store_run(self.stage, "sim/x/corners/r1", repo_root=self.root, bench="x", rid="r1",
                             record_path=self.record)
        self.assertFalse((self.root / "manifests").exists())
        self.assertEqual(self.record.read_text(), "# record\n")

    def test_success_writes_manifest_and_section(self):
        with mock.patch.object(ES.subprocess, "run", fake_run()):
            m = ES.store_run(self.stage, "sim/x/corners/r1", repo_root=self.root, bench="x", rid="r1",
                             record_path=self.record)
        doc = json.loads(m.read_text())
        self.assertEqual([e["name"] for e in doc["entries"]],
                         ["sim/x/corners/r1/a.dat", "sim/x/corners/r1/sub/b.log"])
        self.assertTrue(all(e["uri"].startswith("artifact:") for e in doc["entries"]))
        self.assertIn("## Stored evidence", self.record.read_text())
        with mock.patch.object(ES.subprocess, "run", fake_run()):
            with self.assertRaises(ES.StoreError):  # manifests are never overwritten
                ES.store_run(self.stage, "sim/x/corners/r1", repo_root=self.root, bench="x", rid="r1",
                             record_path=self.record)

    def test_non_artifact_uri_rejected(self):
        run = fake_run()

        def legacy(cmd, **kw):
            p = run(cmd, **kw)
            if cmd[1:3] == ["artifact", "put"]:
                m = cmd[cmd.index("--manifest") + 1]
                d = json.loads(Path(m).read_text())
                d["entries"][-1]["uri"] = "s3://bucket/" + d["entries"][-1]["sha256"]
                Path(m).write_text(json.dumps(d))
            return p

        with mock.patch.object(ES.subprocess, "run", legacy):
            with self.assertRaisesRegex(ES.StoreError, "artifact:"):
                ES.store_run(self.stage, "p", repo_root=self.root, bench="x", rid="r1", record_path=self.record)


class HarnessHookTests(unittest.TestCase):
    def test_claim_run_fails_closed_without_broker(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(ES.ENV_BROKER, None)
            with self.assertRaises(ES.StoreError):
                harness.claim_run_paths(Path(d), "20990101-000000-abcdef0")
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_claim_run_routes_corners_to_scratch(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ES, "preflight", lambda *a, **k: None):
            base = Path(d)
            paths = harness.claim_run_paths(base, "20990101-000000-abcdef0")
            self.assertEqual(paths["corners"].parent, harness.REPO_ROOT / "sim" / "build")
            self.assertEqual(paths["record"], base / "records" / "20990101-000000-abcdef0.md")
            (base / "corners" / "20990101-000001-abcdef0").mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                harness.claim_run_paths(base, "20990101-000001-abcdef0")


if __name__ == "__main__":
    unittest.main()
