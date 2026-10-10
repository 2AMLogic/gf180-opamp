"""Unit tests for `harness.stage_workdir` (issue #94). No simulator, no PDK.

A scratch directory stands in for the PDK (only `design.ngspice` is copied),
and a fixed DUT text stands in for the committed export, so these run
anywhere the stdlib does.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402

TB = (
    "* bench\n"
    ".include 'design.ngspice'\n"
    ".include 'opamp_two_stage.dut.spice'\n"
    ".param x=1\n"
)
DUT = ".subckt opamp_two_stage a b\n.ends\n"


def ok(_text: str) -> list[str]:
    return []


class StageWorkdirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        pdk_dir = root / "pdk"
        (pdk_dir / "libs.tech" / "ngspice").mkdir(parents=True)
        (pdk_dir / "libs.tech" / "ngspice" / "design.ngspice").write_text("* design\n")
        self.pdk = harness.Pdk(path=pdk_dir, variant="fake", source="test")
        self.work = root / "work"

    def test_stages_files_and_rewrites_only_the_two_includes(self):
        tb = harness.stage_workdir(self.work, self.pdk, TB, guard_tb=ok, guard_dut=ok, dut_text=DUT)
        self.assertEqual((self.work / "design.ngspice").read_text(), "* design\n")
        self.assertEqual((self.work / harness.DUT_INCLUDE_NAME).read_text(), DUT)
        self.assertEqual(
            tb,
            "* bench\n"
            f".include '{self.work / 'design.ngspice'}'\n"
            f".include '{self.work / harness.DUT_INCLUDE_NAME}'\n"
            ".param x=1\n",
        )
        # The caller writes tb.spice, not the helper.
        self.assertFalse((self.work / "tb.spice").exists())

    def test_guards_receive_the_bench_and_dut_text(self):
        seen = {}
        harness.stage_workdir(
            self.work, self.pdk, TB,
            guard_tb=lambda t: seen.setdefault("tb", t) and [],
            guard_dut=lambda d: seen.setdefault("dut", d) and [],
            dut_text=DUT,
        )
        self.assertEqual(seen, {"tb": TB, "dut": DUT})

    def test_testbench_guard_failure_writes_nothing(self):
        with self.assertRaises(RuntimeError) as cm:
            harness.stage_workdir(self.work, self.pdk, TB, guard_tb=lambda t: ["a", "b"], guard_dut=ok,
                                  dut_text=DUT)
        self.assertEqual(str(cm.exception), "testbench guard failed:\n  a\n  b")
        self.assertFalse(self.work.exists())

    def test_testbench_guard_label(self):
        with self.assertRaises(RuntimeError) as cm:
            harness.stage_workdir(self.work, self.pdk, TB, guard_tb=lambda t: ["a"], guard_dut=ok,
                                  dut_text=DUT, tb_label="tb_slew.spice")
        self.assertEqual(str(cm.exception), "tb_slew.spice guard failed:\n  a")

    def test_dut_guard_failure_does_not_write_the_dut(self):
        with self.assertRaises(RuntimeError) as cm:
            harness.stage_workdir(self.work, self.pdk, TB, guard_tb=ok, guard_dut=lambda d: ["bad"],
                                  dut_text=DUT)
        self.assertEqual(str(cm.exception), "DUT guard failed:\n  bad")
        self.assertFalse((self.work / harness.DUT_INCLUDE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
