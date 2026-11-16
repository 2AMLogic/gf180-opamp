"""Unit tests for the shared sim-harness helpers.

Started for `harness.stage_workdir` (issue #94) with no simulator and no PDK:
a scratch directory stands in for the PDK (only `design.ngspice` is copied),
and a fixed DUT text stands in for the committed export, so these run
anywhere the stdlib does. Later arrivals: the source-guard, submit-request,
MC-snapshot, PDK-search and record-path helpers, and the frequency-domain
extraction helpers hoisted from the CMRR driver (issue #337).

This module also hosts `StaleDutMixin` and `fake_pdk` (issue #347): the ONE
copy of the stale-DUT negative tests every bench suite mixes in next to its
runner-specific checks, kept here next to the `guard_dut_core` /
`stage_workdir` code they exercise instead of being hand-copied per bench.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

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


class MismatchSwitchGuardTests(unittest.TestCase):
    """`harness.guard_mc_mismatch_switch` (issue #226): the semantic contract
    shared by the four MC runners. Pure text; no simulator, no PDK."""

    INC = ".include 'design.ngspice'"
    DUT = ".include 'opamp_two_stage.dut.spice'"

    def check(self, *lines: str) -> list[str]:
        return harness.guard_mc_mismatch_switch(harness.logical_lines("\n".join(lines)))

    def test_valid_formats_pass(self):
        sw = ".param sw_stat_mismatch=1"
        for name, lines in {
            "standalone": (self.INC, sw),
            "standalone zero (control run)": (self.INC, ".param sw_stat_mismatch=0"),
            "multi-key, switch last": (self.INC, ".param acp=0.5 rsv=1e9 sw_stat_mismatch=1"),
            "multi-key, switch first": (self.INC, ".param sw_stat_mismatch=1 acp=0.5"),
            "spaces around =": (self.INC, ".param sw_stat_mismatch = 1"),
            "uppercase": (self.INC, ".PARAM SW_STAT_MISMATCH=1"),
            "continuation": (self.INC, ".param acp=0.5", "+ sw_stat_mismatch=1"),
            "continuation, indented": (self.INC, ".param acp=0.5", "   + sw_stat_mismatch=1"),
            "comment lines": ("* sw_stat_mismatch=0 sw_stat_global", self.INC, "* note", sw),
            "inline comment mention": (self.INC, ".param acp=0.5 sw_stat_mismatch=1 ; not sw_stat_mismatch=0"),
            "double-quoted include": ('.include "design.ngspice"', sw),
            "unquoted include": (".include design.ngspice", sw),
            ".inc spelling": (".inc 'design.ngspice'", sw),
            "path-prefixed include": (".include '/tmp/work/design.ngspice'", sw),
            "windows path include": (".include 'C:\\work\\design.ngspice'", sw),
            "other lines around": (self.DUT, self.INC, "Rl a b 1k", sw, "Rx c d 1k"),
        }.items():
            with self.subTest(name):
                self.assertEqual(self.check(*lines), [])

    def test_missing_switch_is_rejected(self):
        for name, lines in {
            "absent": (self.INC, ".param acp=0.5"),
            "key without assignment": (self.INC, ".param sw_stat_mismatch"),
            "only in a comment": (self.INC, "* .param sw_stat_mismatch=1"),
            "only in an inline comment": (self.INC, ".param acp=1 ; sw_stat_mismatch=1"),
            "not on a .param line": (self.INC, ".option sw_stat_mismatch=1"),
            "longer key": (self.INC, ".param xsw_stat_mismatch=1"),
        }.items():
            with self.subTest(name):
                self.assertTrue(any("exactly one" in e for e in self.check(*lines)), name)

    def test_duplicate_switch_is_rejected(self):
        for name, lines in {
            "two lines": (self.INC, ".param sw_stat_mismatch=1", ".param sw_stat_mismatch=0"),
            "same line twice": (self.INC, ".param sw_stat_mismatch=1 sw_stat_mismatch=0"),
            "multi-key plus standalone": (self.INC, ".param a=1 sw_stat_mismatch=1", ".param sw_stat_mismatch=1"),
            "continuation duplicate": (self.INC, ".param sw_stat_mismatch=1", "+ sw_stat_mismatch=0"),
        }.items():
            with self.subTest(name):
                self.assertTrue(any("exactly one" in e for e in self.check(*lines)), name)

    def test_misordered_switch_is_rejected(self):
        sw = ".param sw_stat_mismatch=1"
        for name, lines in {
            "before include": (sw, self.INC),
            "multi-key before include": (".param a=1 sw_stat_mismatch=1", self.INC),
            "before path-prefixed include": (sw, ".include '/tmp/w/design.ngspice'"),
            "between two design includes": (self.INC, sw, ".include 'x/design.ngspice'"),
        }.items():
            with self.subTest(name):
                self.assertTrue(any("precedes" in e for e in self.check(*lines)), name)

    def test_switch_without_any_design_include_is_rejected(self):
        errs = self.check(".param sw_stat_mismatch=1", self.DUT)
        self.assertTrue(any("no design.ngspice include" in e for e in errs), errs)
        # a look-alike include is not the PDK design file
        errs = self.check(".include 'mydesign.ngspice'", ".param sw_stat_mismatch=1")
        self.assertTrue(any("no design.ngspice include" in e for e in errs), errs)

    def test_global_variation_is_rejected(self):
        sw = ".param sw_stat_mismatch=1"
        for name, lines in {
            "own .param line": (self.INC, sw, ".param sw_stat_global=1"),
            "same line as the switch": (self.INC, ".param sw_stat_mismatch=1 sw_stat_global=1"),
            "off value": (self.INC, sw, ".param sw_stat_global=0"),
            "not on a .param line": (self.INC, sw, ".option sw_stat_global=1"),
            "continuation": (self.INC, sw, ".param a=1", "+ sw_stat_global=1"),
        }.items():
            with self.subTest(name):
                self.assertTrue(any("sw_stat_global" in e for e in self.check(*lines)), name)

    def test_helper_does_not_police_experiment_specific_content(self):
        """Value, other keys and the circuit body are the runner's job."""
        self.assertEqual(self.check(self.INC, ".param sw_stat_mismatch=7 bogus=1", "Rweird a b 1"), [])

    def test_mismatch_switch_lines_indexes_each_assignment(self):
        code = harness.logical_lines(
            "\n".join([self.INC, ".param a=1", "+ sw_stat_mismatch=1", "R a b 1", ".param sw_stat_mismatch=0"])
        )
        self.assertEqual(harness.mismatch_switch_lines(code), [1, 3])


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


class RunKltRetryingStagesModelsTests(unittest.TestCase):
    def test_every_batch_submit_ships_the_client_models(self):
        """Drivers that build their own request (sharded grids, controls) and call
        `run_klt_retrying` directly must still stage the client's model files."""
        sent = {}

        def fake_run_klt(req, outdir, backend, workdir, env=None):
            sent["req"] = req
            return {}

        orig = harness.run_klt
        harness.run_klt = fake_run_klt
        try:
            harness.run_klt_retrying({"k": 1}, Path("/o"), "batch", Path("/w"), retries=0, wait_s=0.0)
        finally:
            harness.run_klt = orig
        self.assertIs(sent["req"]["options"]["stage_model_inputs"], True)


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


class ClaimRecordPathsTests(unittest.TestCase):
    """`claim_record_paths` — the append-only guard over `sim/` evidence.

    One copy of this contract lives here, in the module that tests
    `sim/harness.py`, rather than being restated per bench (issue #324). The
    guard refuses a re-claim when ANY of the paths it hands out already
    exists, so each guarded kind is exercised under both `plots` settings.
    """

    RECORD = "20261009-000000-abcdef0"
    #: Every path kind `claim_record_paths` guards (`sim/harness.py`).
    KINDS = ("record", "plots", "snapshot", "corners")
    #: The kinds that are directories; the rest are files.
    DIR_KINDS = ("plots", "corners")

    def _base(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _occupy(self, target: Path, kind: str) -> None:
        if kind in self.DIR_KINDS:
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old")

    def test_a_fresh_claim_is_accepted_and_writes_nothing(self):
        for plots in (True, False):
            with self.subTest(plots=plots):
                base = self._base()
                paths = harness.claim_record_paths(base, self.RECORD, plots=plots)
                self.assertEqual("plots" in paths, plots)
                self.assertFalse(any(p.exists() for p in paths.values()))
                # A claim is a reservation, not a write: it stays repeatable
                # until something is actually committed underneath it.
                harness.claim_record_paths(base, self.RECORD, plots=plots)

    def test_any_existing_guarded_path_refuses_the_reclaim(self):
        for plots in (True, False):
            for kind in self.KINDS:
                if kind == "plots" and not plots:
                    continue  # not handed out, so there is nothing to guard
                with self.subTest(plots=plots, kind=kind):
                    base = self._base()
                    target = harness.claim_record_paths(base, self.RECORD, plots=plots)[kind]
                    self._occupy(target, kind)
                    with self.assertRaises(FileExistsError) as cm:
                        harness.claim_record_paths(base, self.RECORD, plots=plots)
                    self.assertIn(str(target), str(cm.exception))
                    self.assertIn("evidence is append-only", str(cm.exception))
                    if kind not in self.DIR_KINDS:
                        # The refusal leaves the existing evidence untouched.
                        self.assertEqual(target.read_text(), "old")

    def test_a_fresh_record_id_is_still_accepted(self):
        base = self._base()
        taken = harness.claim_record_paths(base, self.RECORD)
        self._occupy(taken["record"], "record")
        with self.assertRaises(FileExistsError):
            harness.claim_record_paths(base, self.RECORD)
        fresh = harness.claim_record_paths(base, "20261009-000001-abcdef0")
        self.assertNotEqual(fresh["record"], taken["record"])
        self.assertEqual(taken["record"].read_text(), "old")


class BatchArgsDestContractTests(unittest.TestCase):
    """`add_batch_args` dests — the contract `batch_block`/`submit_request` read.

    #348 hoisted the five fleet flags into one helper, but nothing connected
    it to the consumers reading its dests by attribute, so a renamed dest
    passed the whole local suite and surfaced only on a real fleet submit as
    an `AttributeError` at `batch_block()` time, after the grid was staged.
    These tests parse the helper's own argparse parser (never a hand-written
    namespace) and call the real `batch_block`, so dest or default drift
    fails here instead of in a staged grid (issue #350).
    """

    #: Every dest the helper declares; `batch_block` and `submit_request`
    #: read all five of these by attribute.
    DESTS = frozenset({"backend", "batch_runner_version_check",
                       "batch_capacity_wait_s", "batch_submit_retries",
                       "batch_retry_wait_s"})

    def _parser(self, add, **kwargs) -> argparse.ArgumentParser:
        ap = argparse.ArgumentParser()
        add(ap, **kwargs)
        return ap

    def test_defaults_produce_an_empty_batch_block_and_the_documented_reads(self):
        ns = self._parser(harness.add_batch_args, backend_help="x").parse_args([])
        self.assertEqual(harness.batch_block(ns), {})
        # What `submit_request` reads by attribute (`sim/harness.py`).
        self.assertIsNone(ns.backend)
        self.assertEqual(ns.batch_submit_retries, 0)
        self.assertEqual(ns.batch_retry_wait_s, 120.0)

    def test_flags_forward_into_the_batch_block(self):
        ns = self._parser(harness.add_batch_args, backend_help="x").parse_args(
            ["--batch-runner-version-check", "warn", "--batch-capacity-wait-s", "600"])
        self.assertEqual(harness.batch_block(ns),
                         {"runner_version_check": "warn", "capacity_wait_s": 600.0})
        # The `type=float` coercion of the CLI string is part of the contract.
        self.assertIsInstance(ns.batch_capacity_wait_s, float)

    def test_a_zero_capacity_wait_still_forwards(self):
        # `batch_block` tests `is not None`, not truthiness: a 0 s wait is a
        # deliberate request, and a truthiness regression would drop it.
        ns = self._parser(harness.add_batch_args, backend_help="x").parse_args(
            ["--batch-capacity-wait-s", "0"])
        self.assertEqual(harness.batch_block(ns), {"capacity_wait_s": 0.0})

    def test_runner_version_check_outside_the_choices_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self._parser(harness.add_batch_args, backend_help="x").parse_args(
                ["--batch-runner-version-check", "maybe"])
        self.assertEqual(cm.exception.code, 2)

    def test_grid_and_common_parsers_are_supersets_of_the_batch_dests(self):
        for add, kwargs in ((harness.add_grid_args, {"grid_help": "x"}),
                            (harness.add_common_args, {})):
            with self.subTest(helper=add.__name__):
                ap = self._parser(add, **kwargs)
                self.assertGreaterEqual(set(vars(ap.parse_args([]))), self.DESTS)


class ExtractionHelpersTests(unittest.TestCase):
    """The hoisted frequency-domain extraction helpers (issue #337).

    The same invariants the cmrr/psrr extraction tests exercised through the
    CMRR driver, asserted against the shared home so the next relocation of
    these bodies has its own guard."""

    F = np.array([0.1, 1.0, 10.0, 100.0])

    def test_check_axis_accepts_a_strictly_increasing_positive_axis(self):
        harness.check_axis(self.F, 4)  # must not raise
        harness.check_axis(self.F)  # no expected size: shape/monotonicity only

    def test_check_axis_rejects_wrong_size_and_bad_axes(self):
        for bad, why in [
            (np.array([1.0, 2.0]), "wrong size"),
            (np.array([[1.0, 2.0]]), "not 1-D"),
            (np.array([1.0, -1.0, 10.0]), "non-positive"),
            (np.array([1.0, 3.0, 2.0]), "not increasing"),
            (np.array([1.0, np.nan, 2.0]), "non-finite"),
        ]:
            with self.subTest(why=why), self.assertRaises(harness.ExtractionError):
                harness.check_axis(bad, 3)

    def test_same_axis_rejects_different_axes(self):
        harness.same_axis(self.F, self.F * 1.0)  # must not raise
        with self.assertRaises(harness.ExtractionError):
            harness.same_axis(self.F, self.F * 1.1)
        with self.assertRaises(harness.ExtractionError):
            harness.same_axis(self.F, self.F[:-1])

    def test_finite_rejects_non_finite_phasors(self):
        harness.finite(np.ones(4), np.ones(4))  # must not raise
        with self.assertRaises(harness.ExtractionError):
            harness.finite(np.ones(4), np.array([1.0, np.inf, 1.0, 1.0]))

    def test_db_is_twenty_log10_of_the_magnitude(self):
        np.testing.assert_allclose(harness.db(10.0), 20.0)
        np.testing.assert_allclose(harness.db(1j), 0.0, atol=1e-12)
        np.testing.assert_allclose(harness.db(np.array([1.0, 10.0])), [0.0, 20.0])

    def test_interp_db_is_log_linear_and_bounded(self):
        y = 40.0 * np.log10(self.F)  # exactly linear in (log10 f, dB)
        self.assertAlmostEqual(harness.interp_db(self.F, y, 1.0), 0.0)
        self.assertAlmostEqual(harness.interp_db(self.F, y, 10.0), 40.0)
        with self.assertRaises(harness.ExtractionError):
            harness.interp_db(self.F, y, 1e3)


class RunGridModesTests(unittest.TestCase):
    """The parameterised grid runner (issue #337): fakes stand in for every
    bench collaborator, proving the shared copy reads no bench module global."""

    class Args:
        backend = "local"
        batch_submit_retries = 0
        batch_retry_wait_s = 0.0
        batch_runner_version_check = None
        batch_capacity_wait_s = None

    def setUp(self):
        self.calls = []

        def materialise(path, pdk, params, *, testbench, guard):
            self.calls.append(("materialise", str(path), params, testbench, guard))
            return path / "tb.spice"

        def with_servo(mp, **over):
            return {**mp, **over}

        def ac_request(tb, pdk, corners, temps, supplies):
            self.calls.append(("ac_request", tuple(corners), tuple(temps), tuple(supplies)))
            return {"netlist": str(tb), "analysis": {}}

        self.materialise, self.with_servo, self.ac_request = materialise, with_servo, ac_request
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_one_request_per_mode_with_the_callers_axes_and_collaborators(self):
        fake_report = {"environment": {"remote": {"job_id": "j1"}}}
        with mock.patch.object(harness, "run_klt_retrying", return_value=fake_report) as rk:
            reports, reqs, walls = harness.run_grid_modes(
                None, Path(self._tmp.name), self.Args(), {"dm": {"acp": 1}, "cm": {"acp": 2}},
                corners=["typical", "ff"], temps=[27.0], supplies=[3.3],
                materialise=self.materialise, with_servo=self.with_servo, ac_request=self.ac_request,
                testbench="TB", guard="guard")
        self.assertEqual(sorted(reports), ["cm", "dm"])
        self.assertEqual(reqs["dm"]["batch"], rk.call_args_list[0].args[0]["batch"])
        # one request per mode, each with its own workdir
        self.assertEqual([c.args[3] for c in rk.call_args_list],
                         [Path(self._tmp.name) / "dm", Path(self._tmp.name) / "cm"])
        modes = [(c[0], c[2]) for c in self.calls if c[0] == "materialise"]
        self.assertEqual(modes, [("materialise", {"acp": 1}), ("materialise", {"acp": 2})])
        ac = [c for c in self.calls if c[0] == "ac_request"]
        self.assertEqual([c[1:] for c in ac], [(("typical", "ff"), (27.0,), (3.3,))] * 2)
        self.assertTrue(all(w >= 0 for w in walls.values()))


class RequirePlottingTests(unittest.TestCase):
    def test_returns_none_when_matplotlib_imports(self):
        from types import SimpleNamespace

        fake_mpl = SimpleNamespace(use=lambda backend: None)
        with mock.patch.dict(sys.modules, {"matplotlib": fake_mpl, "matplotlib.pyplot": object()}):
            self.assertIsNone(harness.require_plotting())

    def test_reports_the_environment_error_when_it_cannot_import(self):
        with mock.patch.dict(sys.modules, {"matplotlib": None}):
            msg = harness.require_plotting()
        self.assertIn("matplotlib is not importable (", msg)
        self.assertIn("python3 -s", msg)


class WorkDirTests(unittest.TestCase):
    class Args:
        keep_work = None

    def test_defaults_to_a_temporary_directory(self):
        with harness.work_dir(self.Args(), "prefix-") as scratch:
            self.assertTrue(Path(scratch).is_dir())
        self.assertFalse(Path(scratch).exists())  # cleaned up on exit

    def test_keep_work_creates_and_returns_the_kept_directory(self):
        with tempfile.TemporaryDirectory() as root:
            kept = Path(root) / "kept"

            class NullArgs:
                keep_work = kept

            with harness.work_dir(NullArgs(), "prefix-") as scratch:
                self.assertEqual(Path(scratch), kept.resolve())
                self.assertTrue(kept.is_dir())
            self.assertTrue(kept.is_dir())  # survives the exit


class RunTransientGridTests(unittest.TestCase):
    """`run_transient_grid` (issue #358): the run flow hoisted out of the
    step-response / overdrive-recovery runners. Exercised once here with fake
    bench pieces and a faked klt grid -- the orchestration, the two
    no-record failure exits and the exit-code mapping."""

    RECORD = "20260101-000000-abcdef0"

    def _fake_paths(self, root: Path) -> dict:
        return {
            "corners": root / "corners" / self.RECORD,
            "record": root / "records" / f"{self.RECORD}.md",
            "snapshot": root / "netlist-snapshots" / f"{self.RECORD}.spice",
            "plots": root / "records" / f"{self.RECORD}-plots",
        }

    def _run(self, root: Path, *, problems=(), klt_error=None, rederive_bad=(), control_bad=(),
             n_invalid=0) -> SimpleNamespace:
        (root / "tb.spice").write_text("* bench\n")
        args = argparse.Namespace(smoke=False, backend=None, batch_submit_retries=0,
                                  batch_retry_wait_s=0.0, batch_runner_version_check=None,
                                  batch_capacity_wait_s=None)
        report = {"corners": []}
        results = {"typical_27c_3.30v": object()}
        arts = {"typical_27c_3.30v": {"klt_meas": {"m1": 1.0}, "deck": None}}
        built: list[dict] = []
        printed: list = []
        finalized: list = []
        calls = {"persist_points": 0, "check_rederive": 0}
        paths = self._fake_paths(root)
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def build_record(**kw):
            built.append(kw)
            return "# record md\n"

        def fake_persist(sub, res, art, rep, **kw):
            calls["persist_points"] += 1

        def fake_check_rederive(sub, res, *, rederive, same, suffix, label, rbad):
            calls["check_rederive"] += 1
            rbad.extend(rederive_bad)
            return len(res), len(res)

        def fake_run_klt(*a, **kw):
            if klt_error is not None:
                raise klt_error
            return report

        with mock.patch.object(harness, "allocate_record_id", return_value=(self.RECORD, stamp)), \
             mock.patch.object(harness, "claim_run_paths", return_value=paths), \
             mock.patch.object(harness, "ngspice_version", return_value="ngspice-42"), \
             mock.patch.object(harness, "klt_version", return_value="klt-test"), \
             mock.patch.object(harness, "run_klt_retrying", side_effect=fake_run_klt), \
             mock.patch.object(harness, "load_dut_text", return_value="* fake dut"), \
             mock.patch.object(harness, "persist_points", side_effect=fake_persist), \
             mock.patch.object(harness, "check_rederive", side_effect=fake_check_rederive), \
             mock.patch.object(harness, "finalize_run_evidence", side_effect=finalized.append):
            rc = harness.run_transient_grid(
                pdk=harness.Pdk(root / "pdk", "gf180mcuD", "test"), args=args,
                here=root / "bench", prefix="t-", issue=42,
                tb_rel="sim/t/testbench/tb.spice", tb_path=root / "tb.spice",
                nominal="typical_27c_3.30v", corners="c", temps="t", supplies="s",
                want=["typical_27c_3.30v"],
                materialise=lambda work, pdk: work / "tb.spice",
                make_request=lambda tb, pdk, c, t, s: {"netlist": str(tb), "corners": ["c1"]},
                analyse_report=lambda rep, want: (results, arts, list(problems)),
                meas_names=["m1"],
                run_controls=lambda pdk, work: [],
                control_failures=lambda ctrls: list(control_bad),
                save_point_data=lambda m, path: None,
                rederive=lambda path: None,
                same_result=lambda a, b: True,
                build_plots=lambda res, plot_dir: [],
                build_record=build_record,
                summarise=lambda res: SimpleNamespace(n_invalid=n_invalid),
                print_summary=printed.append,
            )
        return SimpleNamespace(rc=rc, built=built, printed=printed, finalized=finalized,
                               paths=paths, calls=calls, report=report)

    def test_happy_path_writes_the_record_and_returns_0(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(Path(d))
            self.assertEqual(out.rc, 0)
            self.assertEqual(out.paths["record"].read_text(), "# record md\n")
            snap = out.paths["snapshot"].read_text()
            self.assertIn("issue #42", snap)
            self.assertIn("sim/t/testbench/tb.spice", snap)
            kw = out.built[0]
            self.assertEqual(kw["record"], self.RECORD)
            self.assertEqual(kw["ngspice"], "ngspice-42")
            self.assertEqual(kw["klt_ver"], "klt-test")
            self.assertEqual(kw["rederived"], (1, 1, []))
            self.assertEqual(kw["xchk_count"], (1, 1))
            self.assertEqual(kw["dut_sha"], harness.sha256_text("* fake dut"))
            self.assertEqual(kw["backend_desc"], harness.describe_backend(out.report))
            self.assertEqual(out.finalized, [out.paths])
            self.assertEqual(out.calls, {"persist_points": 1, "check_rederive": 1})
            self.assertEqual(len(out.printed), 1)

    def test_a_failed_grid_submit_returns_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(Path(d), klt_error=harness.KltError("batch_no_capacity"))
            self.assertEqual(out.rc, 2)
            self.assertFalse(out.paths["record"].exists())
            self.assertFalse(out.paths["snapshot"].exists())
            self.assertEqual(out.built, [])
            self.assertEqual(out.finalized, [])

    def test_an_incomplete_grid_returns_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(Path(d), problems=("missing result for x",))
            self.assertEqual(out.rc, 2)
            self.assertFalse(out.paths["record"].exists())
            self.assertFalse(out.paths["snapshot"].exists())
            self.assertEqual(out.built, [])
            self.assertEqual(out.finalized, [])

    def test_rederivation_mismatch_control_problems_and_invalid_points_fail_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(Path(d), rederive_bad=["x.dat differs"], control_bad=["ctl drifted"],
                            n_invalid=2)
            self.assertEqual(out.rc, 1)
            # The run still commits its evidence; the failures are reported, not hidden.
            self.assertEqual(out.paths["record"].read_text(), "# record md\n")
            self.assertEqual(out.finalized, [out.paths])


def fake_pdk(d: Path) -> harness.Pdk:
    """A scratch PDK root with only the `design.ngspice` `stage_workdir` copies."""
    ng = d / "pdk" / "libs.tech" / "ngspice"
    ng.mkdir(parents=True)
    (ng / "design.ngspice").write_text("* stub\n")
    return harness.Pdk(d / "pdk", "gf180mcuD", "test")


class StaleDutMixin:
    """The bench must measure the committed export: an altered, stale or
    hand-edited DUT is rejected before anything is written or simulated.

    One copy of these negative tests lives here, in the module that tests
    the shared guard code they exercise (issue #347); each bench suite
    subclasses this next to `unittest.TestCase` and points the hooks at its
    runner. Deliberately NOT a `unittest.TestCase` itself: this module runs
    standalone with no runner attached (`python3 sim/test_harness.py`), and
    an unbound TestCase would be collected and fail on its unset hooks.

    Hooks a subclass must provide:

    * ``RUNNER`` -- the bench's runner module. It must expose
      ``load_dut_text()``, ``materialise(work, pdk, ...)`` and the guards
      module ``g`` (``guard_dut``, ``strip_instance``, ``DUT_INCLUDE_NAME``).
    * ``MATERIALISE_ARGS`` -- extra positional arguments passed to
      ``materialise`` before the keyword ones (e.g. the enable-acquisition
      schedule; ``()`` for the plain benches).
    """

    RUNNER = None
    MATERIALISE_ARGS: tuple = ()

    def test_altered_device_in_the_dut_is_rejected(self):
        r = self.RUNNER
        dut = r.load_dut_text()
        mm = re.search(r"\bW=(\S+)", dut)
        self.assertIsNotNone(mm)
        stale = dut[: mm.start()] + "W=" + mm.group(1) + "0" + dut[mm.end():]  # a resized device
        self.assertTrue(r.g.guard_dut(stale))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                r.materialise(Path(d) / "w", fake_pdk(Path(d)), *self.MATERIALISE_ARGS, dut_text=stale)
            self.assertFalse((Path(d) / "w" / r.g.DUT_INCLUDE_NAME).exists())

    def test_dropped_or_duplicated_device_is_rejected(self):
        r = self.RUNNER
        dut = r.load_dut_text()
        self.assertTrue(r.g.guard_dut(r.g.strip_instance(dut, "XM6")))
        first_x = next(ln for ln in dut.splitlines() if ln[:1] and ln[0] in "xX")
        self.assertTrue(r.g.guard_dut(dut.replace(first_x, first_x + "\n" + first_x, 1)))

    def test_committed_dut_passes(self):
        self.assertEqual(self.RUNNER.g.guard_dut(self.RUNNER.load_dut_text()), [])


if __name__ == "__main__":
    unittest.main()
