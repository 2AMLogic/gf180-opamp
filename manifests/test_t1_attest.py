#!/usr/bin/env python3
"""Offline tests for manifests/t1_attest.py (issue #160). Runs on a scratch copy."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "manifests"))
import t1_attest  # noqa: E402

SKIP = ("/records/", "/corners/", "/netlist-snapshots/", "/probes/", ".loom/", ".agents/", ".claude/")


def scratch() -> Path:
    tmp = Path(tempfile.mkdtemp())
    files = subprocess.check_output(["git", "-C", str(REPO), "ls-files"], text=True).splitlines()
    for rel in files:
        if any(s in "/" + rel for s in SKIP) or not (REPO / rel).is_file():
            continue
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
    # working-tree (possibly uncommitted) generated files
    for rel in ("manifests/attest",):
        shutil.copytree(REPO / rel, tmp / rel, dirs_exist_ok=True)
    return tmp


class T1Attest(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_committed_state_is_current(self):
        self.assertEqual(t1_attest.main(["--root", str(self.root), "--check"]), 0)

    def test_testbench_edit_is_stale(self):
        tb = self.root / "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice"
        tb.write_text(tb.read_text() + "* edited\n")
        self.assertEqual(t1_attest.main(["--root", str(self.root), "--check"]), 1)

    def test_missing_testbench_refuses_to_attest(self):
        (self.root / "sim/noise/testbench/tb_noise.spice").unlink()
        self.assertEqual(t1_attest.main(["--root", str(self.root)]), 2)

    def test_missing_license_refuses_to_attest(self):
        (self.root / "LICENSE").unlink()
        self.assertEqual(t1_attest.main(["--root", str(self.root)]), 2)

    def test_sch_path_comment_is_ignored_for_netlist_hash(self):
        n = self.root / "design/netlist/opamp_two_stage.spice"
        lines = n.read_text().splitlines(keepends=True)
        lines[0] = "** sch_path: /somewhere/else/opamp_two_stage.sch\n"
        n.write_text("".join(lines))
        self.assertEqual(t1_attest.main(["--root", str(self.root), "--check"]), 0)

    def test_manifest_pin_drift_is_caught(self):
        m = self.root / "manifests/gf180-opamp.json"
        m.write_text(m.read_text().replace("sha256:", "sha256:0", 1))
        self.assertEqual(t1_attest.main(["--root", str(self.root), "--check"]), 1)


if __name__ == "__main__":
    unittest.main()
