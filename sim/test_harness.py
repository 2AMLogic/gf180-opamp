"""Unit tests for `harness.stage_workdir` (issue #94). No simulator, no PDK.

A scratch directory stands in for the PDK (only `design.ngspice` is copied),
and a fixed DUT text stands in for the committed export, so these run
anywhere the stdlib does.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


GOOD_TB = (
    "* bench\n"
    ".include 'design.ngspice'\n"
    ".include 'opamp_two_stage.dut.spice'\n"
    "Xdut vdd 0 vinp vout vout ibias opamp_two_stage\n"
    "CL vout 0 2p\n"
)
# The same bench with every line that matters wrapped across `+` continuations.
WRAPPED_TB = (
    "* bench\n"
    ".include\n+ 'design.ngspice'\n"
    ".include 'opamp_two_stage.dut.spice'\n"
    "Xdut vdd 0 vinp\n+ vout vout ibias\n  + opamp_two_stage\n"
    "CL vout 0\n+ 2p\n"
)


class LogicalLinesTests(unittest.TestCase):
    def test_joins_continuations_and_drops_comments_and_blanks(self):
        text = "* c\n\nR1 a b\n+ 1k\n  + tc=1\n  C1 a 0 1p  \n"
        self.assertEqual(harness.logical_lines(text), ["R1 a b 1k tc=1", "C1 a 0 1p"])

    def test_canonical_bench_lines_is_unchanged_by_the_shared_splitter(self):
        text = (
            "* c\n.INCLUDE \"/x/design.ngspice\"\nR1 a b\n+ 1K $ tail\n\nC1 a 0 1p ; note\n"
        )
        self.assertEqual(
            harness.canonical_bench_lines(text),
            [".include 'design.ngspice'", "r1 a b 1k", "c1 a 0 1p"],
        )

    def test_continuation_after_a_dropped_comment_has_no_parent(self):
        # a `+` line opens a new logical line only when nothing precedes it
        self.assertEqual(harness.logical_lines("+ R1 a b 1k\n"), ["+ R1 a b 1k"])


class GuardCoreContinuationTests(unittest.TestCase):
    """A guard must see a wrapped bench exactly as it sees the unwrapped one."""

    def test_core_guard_is_equivalent_on_wrapped_bench(self):
        self.assertEqual(harness.guard_testbench_core(GOOD_TB), [])
        self.assertEqual(harness.guard_testbench_core(WRAPPED_TB), [])

    def test_core_guard_flags_a_wrapped_hand_declared_device(self):
        for tb in (GOOD_TB, WRAPPED_TB):
            bad = tb + "M1 a b\n+ c d nfet_03v3 W=1u L=1u\n"
            self.assertTrue(harness.guard_testbench_core(bad), tb)

    def test_core_guard_detects_missing_dut_and_second_xdut(self):
        self.assertTrue(harness.guard_testbench_core(GOOD_TB.replace("opamp_two_stage.dut.spice", "x.spice")))
        self.assertTrue(harness.guard_testbench_core(GOOD_TB + "Xdut vdd 0 vinp vout vout ibias opamp_two_stage\n"))
        self.assertTrue(harness.guard_testbench_core(GOOD_TB + ".temp 27\n"))

    def test_cmrr_code_lines_joins_continuations(self):
        try:
            cmrr = harness.load_sibling("cmrr_for_harness_test", "sim/cmrr/run_cmrr.py")
        except ModuleNotFoundError as exc:  # the cmrr driver imports numpy; this file is stdlib-only
            self.skipTest(str(exc))
        self.assertEqual(cmrr.code_lines(WRAPPED_TB)[-2:], cmrr.code_lines(GOOD_TB)[-2:])
        self.assertIn("CL vout 0 2p", cmrr.code_lines(WRAPPED_TB))


class ParsePrefixedScalarsTests(unittest.TestCase):
    def test_last_occurrence_wins_and_prefix_is_case_insensitive(self):
        log = "cmc_x = 1.0\ncmc_x   =  2.5e+00\nCMC_Y = 3\n"
        self.assertEqual(harness.parse_prefixed_scalars(log, "cmc_"), {"cmc_x": 2.5, "cmc_y": 3.0})

    def test_other_prefix_and_non_numeric_values_are_ignored(self):
        log = "pmc_z = nan\nfoo = 1\ncmc_w = 2\n"
        self.assertEqual(harness.parse_prefixed_scalars(log, "pmc_"), {})


class SubmitRequestTests(unittest.TestCase):
    def test_attaches_batch_block_and_forwards_retry_settings(self):
        from types import SimpleNamespace
        args = SimpleNamespace(backend="batch", batch_submit_retries=3, batch_retry_wait_s=7.0)
        seen = {}

        def fake(req, outdir, backend, workdir, *, retries, wait_s):
            seen.update(req=req, outdir=outdir, backend=backend, workdir=workdir, retries=retries, wait_s=wait_s)
            return {"environment": {"remote": {"job_id": "j1"}}}

        orig, orig_bb = harness.run_klt_retrying, harness.batch_block
        harness.run_klt_retrying, harness.batch_block = fake, lambda a: {"marker": True}
        try:
            rep, req, wall = harness.submit_request("t", {"k": 1}, args, Path("/w"), units=4)
        finally:
            harness.run_klt_retrying, harness.batch_block = orig, orig_bb
        self.assertEqual(req["batch"], {"marker": True})
        self.assertIs(req["options"]["stage_model_inputs"], True)
        self.assertEqual((seen["outdir"], seen["workdir"]), (Path("/w/t/out"), Path("/w/t")))
        self.assertEqual((seen["backend"], seen["retries"], seen["wait_s"]), ("batch", 3, 7.0))
        self.assertEqual(rep["environment"]["remote"]["job_id"], "j1")
        self.assertGreaterEqual(wall, 0.0)


class McSnapshotTests(unittest.TestCase):
    REQ = {"netlist": "/a/b/tb.spice", "models": {"pdk_root": "/x", "corner": "tt"}}

    def test_request_for_snapshot_sanitises_a_copy(self):
        r = harness.request_for_snapshot(self.REQ)
        self.assertEqual(r, {"netlist": "tb.spice", "models": {"corner": "tt"}})
        self.assertEqual(self.REQ["netlist"], "/a/b/tb.spice")
        self.assertIn("pdk_root", self.REQ["models"])

    def test_write_mc_snapshot_layout(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.spice"
            harness.write_mc_snapshot(p, title="X record r", tb_label="tb.spice", tb_text="t1", dut_text="d1",
                                      ctl_dut="XM3 a b\nXM1 c\n", control_mirror=("XM3", "W", "6u", "5.4u"),
                                      reqs={"tag": self.REQ})
            lines = p.read_text().splitlines()
        self.assertEqual(lines[0], "* netlist snapshot for X record r")
        self.assertEqual(lines[1], "* ---- committed testbench (tb.spice) ----")
        self.assertIn("* ---- negative-control DUT copy (XM3 W changed), sha256 ", lines[5])
        self.assertEqual(lines[6], "* | XM3 a b")
        self.assertIn("* ---- klt request `tag` ----", lines)
        self.assertNotIn("pdk_root", "\n".join(lines))


class FindPdkSearchOrderTests(unittest.TestCase):
    """`find_pdk` prefers ~/.ciel (the pinned tree) over ~/.volare (issue #240)."""

    def _home(self, tools: list[str]) -> Path:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        for tool in tools:
            ngspice = home / f".{tool}" / "gf180mcuD" / "libs.tech" / "ngspice"
            ngspice.mkdir(parents=True)
            (ngspice / "sm141064.ngspice").write_text("")
        return home

    def _find(self, home: Path) -> "harness.Pdk":
        env = {k: v for k, v in os.environ.items() if k not in ("GF180_PDK_PATH", "PDK_ROOT", "PDK")}
        env["HOME"] = str(home)
        with mock.patch.dict(os.environ, env, clear=True):
            return harness.find_pdk()

    def test_ciel_wins_over_volare(self) -> None:
        pdk = self._find(self._home(["volare", "ciel"]))
        self.assertEqual(pdk.source, "ciel-default")

    def test_volare_only_is_the_legacy_fallback(self) -> None:
        self.assertEqual(self._find(self._home(["volare"])).source, "volare-default")

    def test_neither_tree_names_the_ciel_install(self) -> None:
        with self.assertRaises(harness.PdkNotFound) as cm:
            self._find(self._home([]))
        self.assertIn("ciel enable --pdk-family gf180mcu", str(cm.exception))
        self.assertIn(harness.PINNED_PDK_REV, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
