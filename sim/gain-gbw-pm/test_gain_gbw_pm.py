#!/usr/bin/env python3
"""Regression tests for the gain/GBW/PM experiment (issue #38).

No simulator is needed: extraction is checked against synthetic responses with
analytically known crossovers, and the source guards are checked by mutating
the committed testbench / DUT text in memory.

    python3 sim/gain-gbw-pm/test_gain_gbw_pm.py          # or: python3 -m unittest
"""

from __future__ import annotations

import contextlib
import io
import math
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_gain_gbw_pm as r  # noqa: E402
import harness  # noqa: E402  (on sys.path via the driver)

FREQ = np.logspace(math.log10(r.AC_FSTART), math.log10(r.AC_FSTOP), 201)


def poles_response(a0: float, poles_hz: list[float], zeros_hz: list[float] = ()) -> np.ndarray:
    s = 1j * FREQ
    h = a0 * np.ones_like(s)
    for p in poles_hz:
        h = h / (1 + s / p)
    for z in zeros_hz:
        h = h * (1 + s / z)
    return h


def analytic_crossing(a0, poles_hz, zeros_hz=()):
    """GBW and PM of a pole/zero response by bisection on the exact function."""
    def mag(f):
        h = a0
        for p in poles_hz:
            h /= abs(1 + 1j * f / p)
        for z in zeros_hz:
            h *= abs(1 + 1j * f / z)
        return h

    lo, hi = 1e-3, 1e12
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if mag(mid) > 1 else (lo, mid)
    f = math.sqrt(lo * hi)
    phase = -sum(math.degrees(math.atan(f / p)) for p in poles_hz) + sum(
        math.degrees(math.atan(f / z)) for z in zeros_hz
    )
    return f, 180.0 + phase


class ExtractionTests(unittest.TestCase):
    def test_two_pole_matches_analytic_crossover_and_phase(self):
        a0, poles = 10 ** (90 / 20), [800.0, 40e6]
        m = r.extract_metrics(FREQ, poles_response(a0, poles))
        gbw, pm = analytic_crossing(a0, poles)
        self.assertTrue(m.valid, m.reason)
        self.assertAlmostEqual(m.dc_gain_db, 90.0, delta=0.01)
        self.assertAlmostEqual(m.gbw_hz / gbw, 1.0, delta=0.01)
        self.assertAlmostEqual(m.pm_deg, pm, delta=0.2)

    def test_phase_wrapping_is_unwrapped_not_folded(self):
        # Three poles: phase passes -180 deg before/at crossover -> negative PM.
        a0, poles = 10 ** (90 / 20), [500.0, 2e6, 4e6, 6e6]
        gbw, pm = analytic_crossing(a0, poles)
        self.assertLess(pm, 0, "fixture must actually cross -180 deg")
        m = r.extract_metrics(FREQ, poles_response(a0, poles))
        self.assertTrue(m.valid, m.reason)
        self.assertAlmostEqual(m.pm_deg, pm, delta=0.5)
        self.assertLess(m.pm_deg, 0)
        # And a negative PM can never satisfy the ratified bound.
        self.assertFalse(r.point_passes(m, "pm_deg", r.PM_MIN_DEG))

    def test_absent_crossing_is_invalid_and_never_passes(self):
        m = r.extract_metrics(FREQ, poles_response(1e12, [1e9 * 10]))
        self.assertFalse(m.valid)
        self.assertIn("no 0 dB crossing", m.reason)
        for _, _, attr, bound, _ in r.ROWS:
            self.assertFalse(r.point_passes(m, attr, bound))

    def test_peaking_multiple_crossings_is_invalid(self):
        h = poles_response(10 ** (80 / 20), [1e3])
        bump = (FREQ > 5e7) & (FREQ < 1e8)
        h = np.where(bump, h * 1e3, h)  # gain returns above 0 dB after crossing
        m = r.extract_metrics(FREQ, h)
        self.assertFalse(m.valid)
        self.assertIn("crossings", m.reason)

    def test_rising_gain_is_not_a_plateau(self):
        # High-pass-like low-frequency behaviour (the feedback-loading artifact).
        h = poles_response(1e5, [1e3, 3e7], zeros_hz=[]) * (1j * FREQ / 50.0) / (1 + 1j * FREQ / 50.0)
        m = r.extract_metrics(FREQ, h)
        self.assertFalse(m.valid)
        self.assertTrue("plateau" in m.reason or "polarity" in m.reason, m.reason)

    def test_wrong_polarity_is_invalid(self):
        m = r.extract_metrics(FREQ, -poles_response(1e4, [1e3, 3e7]))
        self.assertFalse(m.valid)
        self.assertIn("polarity", m.reason)

    def test_peak_is_not_used_as_dc_gain(self):
        # A gentle low-frequency bump above the plateau must not become the DC gain.
        h = poles_response(10 ** (60 / 20), [3e3, 5e7])
        h = h * (1 + 0.5 * np.exp(-((np.log10(FREQ) - 2.5) ** 2) / 0.1))
        m = r.extract_metrics(FREQ, h)
        if m.valid:
            self.assertAlmostEqual(m.dc_gain_db, 60.0, delta=0.2)
            self.assertLess(m.dc_gain_db, float(np.max(m.db)) - 1.0)

    def test_gain_below_zero_db_at_start_is_invalid(self):
        m = r.extract_metrics(FREQ, poles_response(0.5, [1e3]))
        self.assertFalse(m.valid)

    def test_malformed_inputs_are_invalid(self):
        h = poles_response(1e4, [1e3, 3e7])
        bad = h.copy()
        bad[10] = np.nan
        self.assertFalse(r.extract_metrics(FREQ, bad).valid)
        self.assertFalse(r.extract_metrics(FREQ[:5], h[:5]).valid)
        self.assertFalse(r.extract_metrics(FREQ[::-1], h[::-1]).valid)
        zero = h.copy()
        zero[3] = 0
        self.assertFalse(r.extract_metrics(FREQ, zero).valid)

    def test_interpolation_is_exact_for_piecewise_linear_logf_db(self):
        # 7 points/decade so neither 1e6 Hz nor the 1e3 Hz kink is a sample.
        f = np.logspace(-1, 9, 10 * 7 + 1)
        lf = np.log10(f)
        db = np.where(lf < 3, 60.0, 60.0 - 20.0 * (lf - 3))  # 0 dB exactly at 1e6 Hz
        phase = np.where(lf < 0, 0.0, -10.0 * lf)  # flat ~0 deg plateau, then linear in log f
        h = 10 ** (db / 20) * np.exp(1j * np.radians(phase))
        m = r.extract_metrics(f, h)
        self.assertTrue(m.valid, m.reason)
        self.assertAlmostEqual(m.gbw_hz / 1e6, 1.0, delta=1e-6)
        self.assertAlmostEqual(m.dc_gain_db, 60.0, delta=1e-9)
        self.assertAlmostEqual(m.pm_deg, 180.0 - 60.0, delta=1e-6)  # -10*log10(1e6) = -60


class RawParserTests(unittest.TestCase):
    TEXT = (
        "Title: t\nPlotname: AC Analysis\nFlags: complex\nNo. Variables: 4\nNo. Points: 2\n"
        "Variables:\n\t0\tfrequency\tfrequency grid=3\n\t1\tv(vout)\tvoltage\n"
        "\t2\tv(vinp)\tvoltage\n\t3\tv(vinn)\tvoltage\nValues:\n"
        " 0\t1.0e0,0.0e0\n\t2.0e0,1.0e0\n\t1.0e0,0.0e0\n\t1.0e-6,0.0e0\n\n"
        " 1\t1.0e1,0.0e0\n\t1.0e0,-1.0e0\n\t1.0e0,0.0e0\n\t0.0e0,0.0e0\n"
    )

    def test_parse_and_differential_gain(self):
        vec = r.parse_ascii_complex_raw(self.TEXT)
        freq, h, vdiff = r.differential_gain(vec)
        np.testing.assert_allclose(freq, [1.0, 10.0])
        np.testing.assert_allclose(vdiff, [1.0 - 1e-6, 1.0])
        np.testing.assert_allclose(h[1], (1 - 1j) / 1.0)

    def test_malformed_raw_fails_loudly(self):
        with self.assertRaises(ValueError):
            r.parse_ascii_complex_raw(self.TEXT.replace("Flags: complex", "Flags: real"))
        with self.assertRaises(ValueError):
            r.parse_ascii_complex_raw(self.TEXT.replace("No. Points: 2", "No. Points: 3"))
        with self.assertRaises(ValueError):
            r.parse_ascii_complex_raw(self.TEXT.replace("2.0e0,1.0e0", "nan,1.0e0"))

    def test_zero_differential_input_rejected(self):
        vec = r.parse_ascii_complex_raw(self.TEXT.replace("1.0e0,0.0e0\n\t1.0e-6", "0.0e0,0.0e0\n\t0.0e0"))
        with self.assertRaises(ValueError):
            r.differential_gain(vec)


class VerdictTests(unittest.TestCase):
    def _m(self, gain, gbw, pm, valid=True):
        return r.Metrics(valid=valid, reason="" if valid else "x", dc_gain_db=gain, gbw_hz=gbw, pm_deg=pm)

    def test_bounds_are_the_ratified_values(self):
        self.assertEqual((r.GAIN_MIN_DB, r.GAIN_STRETCH_DB, r.GBW_MIN_HZ, r.PM_MIN_DEG), (60.0, 70.0, 10e6, 60.0))

    def test_binding_corner_per_metric_and_independent_rows(self):
        a, b, c = ("ss", -40.0, 2.97), ("ff", 125.0, 3.63), ("typical", 27.0, 3.30)
        res = {a: self._m(65, 12e6, 70), b: self._m(75, 30e6, 55), c: self._m(72, 9e6, 65)}
        v = {x.row: x for x in r.judge(res)}
        self.assertEqual(v["gain"].verdict, "PASS")
        self.assertEqual(v["gain"].binding, a)
        self.assertEqual(v["gain"].stretch_pass, 2)  # stretch labelled separately
        self.assertEqual(v["gbw"].verdict, "FAIL")
        self.assertEqual(v["gbw"].binding, c)
        self.assertEqual(v["pm"].verdict, "FAIL")
        self.assertEqual(v["pm"].binding, b)
        self.assertEqual((v["pm"].n_pass, v["pm"].n_total), (2, 3))

    def test_invalid_point_fails_every_row(self):
        k = ("typical", 27.0, 3.3)
        v = r.judge({k: self._m(float("nan"), float("nan"), float("nan"), valid=False)})
        self.assertTrue(all(x.verdict == "FAIL" for x in v))
        self.assertTrue(all(x.n_invalid == 1 for x in v))

    def test_bound_is_inclusive(self):
        self.assertTrue(r.point_passes(self._m(60.0, 10e6, 60.0), "pm_deg", 60.0))
        self.assertFalse(r.point_passes(self._m(60.0, 10e6, 59.999), "pm_deg", 60.0))

    def test_grid_is_the_full_45_unique_points(self):
        keys = r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(len(keys), 45)
        self.assertEqual(len(set(keys)), 45)
        self.assertEqual(len({r.point_stem(k) for k in keys}), 45)  # filenames survive
        self.assertEqual(sorted({k[2] for k in keys}), [2.97, 3.30, 3.63])

    def test_report_with_missing_or_failed_point_is_a_problem(self):
        want = r.expected_keys(["typical"], [27.0], [3.30])
        _, _, problems = r.analyse_ac_report({"corners": []}, want)
        self.assertTrue(any("missing result" in p for p in problems))
        rep = {"corners": [{
            "process": "typical", "temperature_c": 27, "supply_v": {"vdd": 3.3, "vcm": 1.65},
            "status": "error", "artifacts": {"raw": None}, "measurements": [],
            "diagnostics": [{"severity": "error", "message": "boom"}],
        }]}
        _, _, problems = r.analyse_ac_report(rep, want)
        self.assertTrue(any("simulation failed" in p and "boom" in p for p in problems))


class GuardTests(unittest.TestCase):
    TB = r.TESTBENCH.read_text()

    def test_committed_testbench_passes(self):
        self.assertEqual(r.guard_testbench(self.TB), [])

    def test_removing_or_changing_the_dut_include_fails(self):
        gone = self.TB.replace(".include 'opamp_two_stage.dut.spice'", "")
        self.assertTrue(r.guard_testbench(gone))
        other = self.TB.replace("opamp_two_stage.dut.spice", "provisional.spice")
        self.assertTrue(any("does not `.include" in e for e in r.guard_testbench(other)))

    def test_hand_declared_transistors_fail(self):
        for extra in (
            "M1 d g s b nfet_03v3 W=1u L=1u",
            "XM9 d g s b nfet_03v3 W=1u L=1u",
            "XQ1 a b c pfet_03v3 W=1u L=1u",
        ):
            self.assertTrue(r.guard_testbench(self.TB + "\n" + extra + "\n"), extra)

    def test_duplicate_or_foreign_dut_instance_fails(self):
        dup = self.TB + "\nXdut2 vdd 0 vinp vinn vout ibias opamp_two_stage\n"
        self.assertTrue(r.guard_testbench(dup))
        twice = self.TB + "\nXdut vdd 0 vinp vinn vout ibias opamp_two_stage\n"
        self.assertTrue(any("exactly one Xdut" in e for e in r.guard_testbench(twice)))
        foreign = self.TB.replace("opamp_two_stage\nLfb", "some_other_amp\nLfb")
        self.assertTrue(r.guard_testbench(foreign))

    def test_testbench_must_be_a_circuit_body(self):
        for card in (".control", ".end", ".temp 27", ".lib 'x' typical"):
            self.assertTrue(r.guard_testbench(self.TB + "\n" + card + "\n"), card)

    def test_dut_is_the_wrapper_normalised_export(self):
        dut = r.load_dut_text()
        self.assertEqual(r.guard_dut(dut), [])
        # wrapper conversion preserves every device body line of the export
        body = [
            ln for ln in r.DUT_EXPORT.read_text().splitlines()
            if ln.strip() and not ln.startswith(("**", ".end"))
        ]
        for ln in body:
            self.assertIn(ln.rstrip(), [x.rstrip() for x in dut.splitlines()])
        self.assertRegex(dut, r"(?m)^\.subckt opamp_two_stage vdd vss vinp vinn vout ibias")
        self.assertNotRegex(dut, r"(?m)^\.end\s*$")

    def test_altered_or_duplicated_dut_devices_fail(self):
        dut = r.load_dut_text()
        self.assertTrue(r.guard_dut(dut.replace("W=6u", "W=7u", 1)))
        dup = dut.replace(".ends", "XM5 tail ibias vss vss nfet_03v3 L=2u W=6u\n.ends")
        self.assertTrue(any("duplicate" in e for e in r.guard_dut(dup)))
        self.assertTrue(r.guard_dut(dut + "\n" + dut))

    def test_negative_control_removal_is_explicit_only(self):
        stripped = r.strip_instance(r.load_dut_text(), "XCC")
        self.assertTrue(r.guard_dut(stripped))  # silently dropping CC is a guard failure
        self.assertEqual(r.guard_dut(stripped, allow_missing=("xcc",)), [])
        self.assertNotIn("cap_mim", stripped)

    def test_committed_testbench_has_no_transistor_text(self):
        for ln in self.TB.splitlines():
            if ln.startswith("*") or not ln.strip():
                continue
            self.assertNotRegex(ln, r"(?i)\b(nfet|pfet)")

    def test_materialise_rewrites_only_includes_and_requested_overrides(self):
        pdk = r.find_pdk()
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            tb = r.materialise(work, pdk, lfb=1e10, ibias_a=0.0).read_text()
            self.assertIn(f"'{work / 'opamp_two_stage.dut.spice'}'", tb)
            self.assertIn(".param lfb=1e+10 cfb=1e+10", tb)
            self.assertIn("Ibias vdd ibias dc 0", tb)
            self.assertTrue((work / "design.ngspice").is_file())
            self.assertEqual((work / "opamp_two_stage.dut.spice").read_text(), r.load_dut_text())


class AppendOnlyTests(unittest.TestCase):
    def test_existing_record_paths_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first = r.claim_record_paths(root, "20261009-000000-abcdef0")
            first["corners"].mkdir(parents=True)
            first["record"].parent.mkdir(parents=True, exist_ok=True)
            first["record"].write_text("old")
            with self.assertRaises(FileExistsError):
                r.claim_record_paths(root, "20261009-000000-abcdef0")
            self.assertEqual(first["record"].read_text(), "old")

    def test_prior_provisional_records_are_retained(self):
        recs = {p.name for p in (HERE / "records").glob("*.md")}
        self.assertIn("20260915-221407-1bb9a74.md", recs)
        self.assertIn("20261003-030340-7bd8071.md", recs)

    def test_record_paths_without_plots(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with_plots = r.claim_record_paths(root, "rid")
            self.assertEqual(list(with_plots), ["record", "plots", "snapshot", "corners"])
            self.assertEqual(list(harness.claim_record_paths(root, "rid", plots=False)), ["record", "snapshot", "corners"])


class SharedKltWrapperTests(unittest.TestCase):
    """The `klt sim` wrapper lives once in sim/harness.py (issue #58)."""

    def setUp(self):
        self._run_klt = harness.run_klt
        self._sleep = harness.time.sleep
        self._subrun = harness.subprocess.run
        harness.time.sleep = lambda s: None

    def tearDown(self):
        harness.run_klt = self._run_klt
        harness.time.sleep = self._sleep
        harness.subprocess.run = self._subrun

    def _scripted(self, errors):
        calls = []

        def fake(request, outdir, backend, workdir):
            calls.append(backend)
            if len(calls) <= len(errors):
                raise harness.KltError(errors[len(calls) - 1])
            return {"corners": []}

        harness.run_klt = fake
        return calls

    def test_driver_uses_the_shared_helpers(self):
        for name in ("KltError", "run_klt", "run_klt_retrying", "claim_record_paths", "klt_version",
                     "batch_block", "sanitise_report", "load_dut_text"):
            self.assertIs(getattr(r, name), getattr(harness, name), name)

    def test_retry_only_on_capacity_refusal_and_same_backend(self):
        calls = self._scripted(["klt sim error: batch_no_capacity", "BATCH_MAX_CONCURRENT_INSTANCES reached"])
        with contextlib.redirect_stdout(io.StringIO()):
            rep = harness.run_klt_retrying({}, Path("o"), "batch", Path("w"), retries=2, wait_s=0)
        self.assertEqual(rep, {"corners": []})
        self.assertEqual(calls, ["batch", "batch", "batch"])

    def test_other_errors_and_exhausted_retries_propagate(self):
        calls = self._scripted(["klt sim error: bad netlist"])
        with self.assertRaises(harness.KltError):
            harness.run_klt_retrying({}, Path("o"), "batch", Path("w"), retries=3, wait_s=0)
        self.assertEqual(len(calls), 1)
        calls = self._scripted(["no capacity"] * 5)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(harness.KltError):
            harness.run_klt_retrying({}, Path("o"), "batch", Path("w"), retries=1, wait_s=0)
        self.assertEqual(len(calls), 2)

    def test_run_klt_passes_env_and_backend(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, env=kw.get("env"))
            return types.SimpleNamespace(returncode=0, stdout='{"corners": []}', stderr="")

        harness.subprocess.run = fake_run
        with tempfile.TemporaryDirectory() as d:
            rep = harness.run_klt({"x": 1}, Path(d) / "out", "local", Path(d), env={"HOME": d})
            self.assertTrue((Path(d) / "out.request.json").is_file())
        self.assertEqual(seen["env"], {"HOME": d})
        self.assertEqual(seen["cmd"][-2:], ["--backend", "local"])
        self.assertEqual(rep["_exit_code"], 0)

    def test_sanitise_and_remote(self):
        rep = {"_exit_code": 0, "corners": [{"artifacts": {"log": "/abs/host/p/corner.log", "raw": None}}],
               "environment": {"remote": {"provider": "batch"}}}
        clean = harness.sanitise_report(rep)
        self.assertNotIn("_exit_code", clean)
        self.assertEqual(clean["corners"][0]["artifacts"], {"log": "corner.log", "raw": None})
        self.assertEqual(rep["corners"][0]["artifacts"]["log"], "/abs/host/p/corner.log")  # input untouched
        self.assertEqual(harness.remote_of(rep), {"provider": "batch"})
        self.assertEqual(harness.remote_of({}), {})


class PassiveCornerTests(unittest.TestCase):
    """Opt-in RZ x CC passive-corner axis (issue #70); default grid untouched."""

    def test_default_grid_unchanged(self):
        self.assertEqual(len(r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)), 45)
        self.assertEqual(r.PASSIVE_SECTIONS, ("res_typical", "mimcap_typical"))
        ax = r.process_axis(["fs"])
        self.assertEqual(ax, [{"name": "fs", "sections": ["fs", "res_typical", "mimcap_typical"]}])

    def test_section_names_exist_in_pdk(self):
        try:
            lib = (harness.find_pdk().path / r.MODEL_LIB).read_text()
        except Exception:
            self.skipTest("PDK not installed")
        for res in r.PASSIVE_LEVELS:
            for mim in r.PASSIVE_LEVELS:
                for sec in r.passive_sections(res, mim):
                    self.assertRegex(lib, rf"(?im)^\.lib {sec}\s*$")

    def test_sections_map_independently(self):
        self.assertEqual(r.passive_sections("best", "worst"), ("res_ff", "mimcap_ss"))
        self.assertEqual(r.passive_sections("worst", "typical"), ("res_ss", "mimcap_typical"))
        self.assertEqual(len(r.passive_combos()), 9)

    def test_request_is_one_matrix_with_exactly_the_study_points(self):
        req = r.passive_ac_request(Path("/x/tb.spice"), types.SimpleNamespace(variant="gf180mcuD"))
        axis = req["corners"]["process"]
        self.assertEqual(len(axis), 27)
        by = {a["name"]: a["sections"] for a in axis}
        self.assertEqual(by[r.passive_name("ss", "worst", "best")], ["ss", "res_ss", "mimcap_ff"])
        # emulate klt: cross product minus exclude entries
        sup = req["corners"]["supply_v"]
        cells = set()
        for a in axis:
            for vdd in sup["vdd"]:
                for t in req["corners"]["temperature_c"]:
                    e = {"process": a["name"], "temperature_c": t, "supply_v": {"vdd": vdd}}
                    if e not in req["exclude"]:
                        cells.add((a["name"], float(t), float(vdd)))
        self.assertEqual(cells, set(r.passive_expected_keys()))
        self.assertEqual(len(cells), 27)

    def test_request_expansion_matches_klt(self):
        try:
            from klayout_tools import sim as ksim
            expand = ksim._expand_corners
        except Exception:
            self.skipTest("klt expander not importable")
        req = r.passive_ac_request(Path("/x/tb.spice"), types.SimpleNamespace(variant="gf180mcuD"))
        pts = expand(req["corners"], req["exclude"])
        got = {(p.process if isinstance(p.process, str) else p.process["name"], p.temperature_c, p.supply_v["vdd"])
               for p in pts}
        self.assertEqual(got, set(r.passive_expected_keys()))

    def test_summary_and_record_report_sensitivity(self):
        def m(pm, gbw):
            return r.Metrics(valid=True, dc_gain_db=90.0, gbw_hz=gbw, pm_deg=pm)

        results = {}
        for (mos, t, v) in r.PASSIVE_POINTS:
            for rl, cl in r.passive_combos():
                pm = 60.0 + (5.0 if rl == "worst" else -3.0 if rl == "best" else 0.0)
                gbw = 12e6 / r.MIM_FACTOR[cl]
                results[(r.passive_name(mos, rl, cl), t, v)] = m(pm, gbw)
        summ = r.passive_summary(results)
        row = summ[("fs", 125.0, 2.97)][("worst", "typical")]
        self.assertAlmostEqual(row["d_pm"], 5.0)
        self.assertAlmostEqual(row["slew_rel"], 1.0)
        self.assertAlmostEqual(summ[("fs", 125.0, 2.97)][("typical", "worst")]["slew_rel"], 1 / 1.1)
        self.assertAlmostEqual(summ[("fs", 125.0, 2.97)][("typical", "worst")]["d_gbw_pct"], 100 * (1 / 1.1 - 1))
        from datetime import datetime, timezone
        md = r.build_passive_record(
            record="X", stamp=datetime.now(timezone.utc), pdk=types.SimpleNamespace(path="/p", version="v"),
            ngspice="n", klt_version="k", backend_desc="b", report={}, results=results, dut_sha="0", base_record="B")
        self.assertIn("Passive-section policy (swept)", md)
        self.assertIn("res_ss", md)
        self.assertIn("**FAIL**", md)  # PM 57 at RZ best misses the unchanged 60 deg bound

    def test_verdict_counts_are_pass_counts_labelled_as_such(self):
        """Issue #70 review: the header printed the PASS count after FAIL."""
        keys = r.passive_expected_keys()
        results = {}
        for i, k in enumerate(keys):  # 7 cells meet PM, 3 cells miss GBW
            results[k] = r.Metrics(valid=True, dc_gain_db=90.0,
                                   pm_deg=61.0 if i < 7 else 55.0,
                                   gbw_hz=9.5e6 if i >= 24 else 11e6)
        pm, gbw = r.passive_verdict_lines(results)
        self.assertIn("**FAIL** -- passes at 7/27 study cells (fails at 20/27)", pm)
        self.assertIn("**FAIL** -- passes at 24/27 study cells (fails at 3/27)", gbw)
        self.assertNotRegex(pm + gbw, r"FAIL\*\* at \d")
        # all cells meeting the bound -> PASS, zero failures
        ok = {k: r.Metrics(valid=True, pm_deg=65.0, gbw_hz=12e6) for k in keys}
        pm, gbw = r.passive_verdict_lines(ok)
        self.assertIn("**PASS** -- passes at 27/27 study cells (fails at 0/27)", pm)
        # an INVALID cell counts as failing
        ok[keys[0]] = r.Metrics(valid=False, reason="x")
        pm, _ = r.passive_verdict_lines(ok)
        self.assertIn("**FAIL** -- passes at 26/27 study cells (fails at 1/27)", pm)

    def test_recompute_from_committed_record(self):
        """`--recompute-passive` re-derives the committed record without a simulator:
        tables/findings byte-identical, header counts agree with the table columns."""
        src = "20261009-233341-95dfc2a"
        if not (r.HERE / "corners" / src / "klt-report.json").is_file():
            self.skipTest("committed passive-corner data not present")
        from datetime import datetime, timezone
        rid, md = r.recompute_passive(src, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        old = (r.HERE / "records" / f"{src}.md").read_text()
        body = lambda t: t.split("## Conditions", 1)[1].split("## Artifacts", 1)[0]  # noqa: E731
        self.assertEqual(body(md), body(old))
        self.assertIn(f"- **Supersedes**: `{src}`", md)
        self.assertIn("passes at 7/27 study cells (fails at 20/27)", md)
        self.assertIn("passes at 24/27 study cells (fails at 3/27)", md)
        # the header counts must match the per-cell PASS/FAIL columns
        rows = [ln.split("|") for ln in md.splitlines() if re.match(r"^\| (typical|best|worst) \|", ln)]
        self.assertEqual(len(rows), 27)
        self.assertEqual(sum(c[8].strip() == "PASS" for c in rows), 7)
        self.assertEqual(sum(c[9].strip() == "FAIL" for c in rows), 3)
        self.assertIn(f"`sim/gain-gbw-pm/corners/{src}/`", md)
        self.assertIn(f"netlist-snapshots/{src}.spice", md)


class MeasurementFingerprint(unittest.TestCase):
    """Issue #85: the fingerprint is stable under non-semantic change and
    moves with load, bias, stimulus and analysis settings."""

    TB = r.TESTBENCH.read_text()

    def fp(self, text=None):
        return r.mc.fingerprint(self.TB if text is None else text)

    def test_driver_uses_the_fingerprinted_constants(self):
        self.assertIs(r.CORNERS, r.mc.CORNERS)
        self.assertEqual(r.ac_request.__globals__["AC_PPD"], r.mc.AC_PPD)

    def test_non_semantic_changes_keep_fingerprint(self):
        t = "* new comment\n\n" + self.TB.replace("'design.ngspice'", "'/tmp/x/work/design.ngspice'")
        t = t.replace("CL vout 0 2p", "CL  vout   0 2p ; load")
        self.assertEqual(self.fp(t), self.fp())

    def test_semantic_changes_move_fingerprint(self):
        for old, new in (("CL vout 0 2p", "CL vout 0 3p"), ("dc 10u", "dc 11u"),
                         ("dc 3.3", "dc 3.0"), ("ac 1", "ac 2"), ("lfb=1e9", "lfb=1e8")):
            with self.subTest(old):
                self.assertIn(old, self.TB)
                self.assertNotEqual(self.fp(self.TB.replace(old, new, 1)), self.fp())

    def test_inputs_are_json_and_record_embeds_them(self):
        import json
        lines = r.mc.fingerprint_lines(self.TB) + r.mc.inputs_section(self.TB)
        blob = "\n".join(lines)
        self.assertIn(self.fp(), blob)
        body = blob.split("```json\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(harness.measurement_fingerprint(json.loads(body)), self.fp())


class FixedVcmTests(unittest.TestCase):
    """Issue #125: explicit fixed-common-mode option; VDD/2 stays the default."""

    pdk = types.SimpleNamespace(variant="gf180mcuD")
    TB = r.TESTBENCH.read_text()

    def vcm_of(self, req):
        return req["corners"]["supply_v"]["vcm"]

    def test_fixed_vcm_is_1p20_at_every_supply_point(self):
        req = r.ac_request(Path("x"), self.pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V, 1.20)
        sv = req["corners"]["supply_v"]
        self.assertEqual(sv["vdd"], list(r.SUPPLIES_V))
        self.assertEqual(sv["vcm"], [1.20] * len(r.SUPPLIES_V))
        self.assertEqual(len(sv["vcm"]), len(sv["vdd"]))  # swept by index

    def test_default_still_tracks_half_supply(self):
        req = r.ac_request(Path("x"), self.pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(self.vcm_of(req), [1.485, 1.65, 1.815])
        self.assertEqual(req, r.ac_request(Path("x"), self.pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V, None))

    def test_op_requests_carry_the_same_policy(self):
        for fn in (r.op_request_grid, r.op_request_nominal):
            req = fn(Path("x"), self.pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V, 1.2)
            self.assertEqual(self.vcm_of(req), [1.2] * 3)
            req = fn(Path("x"), self.pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
            self.assertEqual(self.vcm_of(req), [1.485, 1.65, 1.815])

    def test_grid_is_still_45_points(self):
        self.assertEqual(len(r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)), 45)

    def test_invalid_requests_fail_before_submission(self):
        for bad in (0, -1.2, float("nan"), float("inf"), 2.97, 3.3, 5.0, "1.2", True):
            with self.subTest(bad):
                with self.assertRaises(r.VcmRequestError):
                    r.validate_vcm_fixed(bad)
                with self.assertRaises(r.VcmRequestError):
                    r.ac_request(Path("x"), self.pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V, bad)
        self.assertEqual(r.validate_vcm_fixed(1.2), 1.2)
        self.assertIsNone(r.validate_vcm_fixed(None))

    def test_cli_rejects_before_any_tool_or_submission(self):
        def boom(*a, **k):
            raise AssertionError("must not reach the PDK / klt")
        saved = (r.find_pdk, r.run_klt_retrying, r.run_klt)
        r.find_pdk = r.run_klt_retrying = r.run_klt = boom
        try:
            for argv in (["--vcm-fixed", "-1"], ["--vcm-fixed", "nan"], ["--vcm-fixed", "2.97"],
                         ["--vcm-fixed", "1.2", "--passive-corners"],
                         ["--vcm-fixed", "1.2", "--recompute-passive", "x"]):
                with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        r.main(argv)
                    self.assertEqual(cm.exception.code, 2)
        finally:
            r.find_pdk, r.run_klt_retrying, r.run_klt = saved

    def test_fixed_mode_runs_smoke_before_submission_and_aborts_on_failure(self):
        calls = []
        saved = (r.run_single, r.run_klt_retrying, r.allocate_record_id, r.claim_record_paths,
                 r.ngspice_version, r.klt_version)
        r.run_single = lambda *a, **k: (calls.append(("smoke", k.get("vcm_fixed"))),
                                        r.StudyRun("s", "d", None, [], error="boom"))[1]
        r.run_klt_retrying = lambda *a, **k: calls.append("submit")
        r.allocate_record_id = lambda root: ("20990101-000000-test", __import__("datetime").datetime(2099, 1, 1))
        r.claim_record_paths = lambda base, rec, plots=True: {}
        r.ngspice_version = r.klt_version = lambda: "x"
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc = r.run_fixed_vcm(types.SimpleNamespace(path="p", variant="v", version="1", source="s"),
                                     types.SimpleNamespace(), 1.2)
        finally:
            (r.run_single, r.run_klt_retrying, r.allocate_record_id, r.claim_record_paths,
             r.ngspice_version, r.klt_version) = saved
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [("smoke", 1.2)])  # grid never submitted

    def test_vcm_verification_flags_wrong_value(self):
        rep = {"corners": [{"process": "ff", "temperature_c": 27, "supply_v": {"vdd": 3.3, "vcm": 1.65}}]}
        self.assertTrue(r.vcm_mismatches(rep, 1.2))
        rep["corners"][0]["supply_v"]["vcm"] = 1.2
        self.assertFalse(r.vcm_mismatches(rep, 1.2))

    def test_fingerprint_default_unchanged_fixed_distinct_and_recorded(self):
        import json
        mc = r.mc
        self.assertEqual(mc.inputs(self.TB)["corners"]["vcm_rule"], "vdd/2")
        self.assertEqual(mc.fingerprint(self.TB), mc.fingerprint(self.TB, None))
        self.assertNotEqual(mc.fingerprint(self.TB), mc.fingerprint(self.TB, 1.2))
        self.assertNotEqual(mc.fingerprint(self.TB, 1.2), mc.fingerprint(self.TB, 1.25))
        self.assertEqual(mc.inputs(self.TB, 1.2)["corners"]["vcm_rule"], "fixed:1.2")
        blob = "\n".join(mc.fingerprint_lines(self.TB, 1.2) + mc.inputs_section(self.TB, 1.2))
        body = blob.split("```json\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(json.loads(body)["corners"]["vcm_rule"], "fixed:1.2")

    def test_fixed_records_live_outside_default_record_and_corner_dirs(self):
        self.assertNotEqual(r.FIXED_VCM_DIR, r.HERE)
        self.assertEqual(r.FIXED_VCM_DIR.parent, r.HERE)
        self.assertNotIn(r.FIXED_VCM_DIR.name, ("records", "corners"))

    # ---- retained fingerprint inputs (judge feedback on PR #126) ----

    @staticmethod
    def rehash_record(md: str) -> tuple[str, str, dict]:
        """(header fingerprint, sha256 of the retained JSON, parsed JSON)."""
        import json
        head = re.search(r"^- \*\*Measurement fingerprint\*\*: version \S+, sha256 `([0-9a-f]{64})`", md, re.M)
        assert head, "no fingerprint header"
        assert md.count("## Measurement fingerprint inputs") == 1, "no retained inputs section"
        body = md.split("## Measurement fingerprint inputs", 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
        inp = json.loads(body)
        return head.group(1), harness.measurement_fingerprint(inp), inp

    def _metrics(self, pm_fail_keys=()):
        want = r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        return want, {k: r.Metrics(valid=True, dc_gain_db=95.0, gbw_hz=12e6,
                                   pm_deg=58.0 if k in pm_fail_keys else 61.0) for k in want}

    def _record(self, **kw):
        from datetime import datetime
        want, res = self._metrics()
        args = dict(record="20990101-000000-test", stamp=datetime(2099, 1, 1),
                    pdk=types.SimpleNamespace(variant="gf180mcuD", version="v", source="s"),
                    ngspice="ng", klt_version="k", backend_desc="b", report={}, op_report_remote=None,
                    op_error=None, vcm=1.2, want=want, results=res, baseline={}, baseline_id="none",
                    op=None, nominal_op=None, problems=[], smoke_line="smoke", dut_sha="0" * 64, req={})
        args.update(kw)
        return r.build_fixed_vcm_record(**args)

    def test_generated_record_retains_inputs_that_rehash_to_the_header(self):
        head, rehash, inp = self.rehash_record(self._record())
        self.assertEqual(head, rehash)
        self.assertEqual(head, r.mc.fingerprint(self.TB, 1.2))
        self.assertEqual(inp["corners"]["vcm_rule"], "fixed:1.2")
        self.assertNotEqual(head, r.mc.fingerprint(self.TB))  # never the VDD/2 fingerprint

    def test_recompute_from_committed_record(self):
        """`--recompute-fixed-vcm` supersedes the committed record without a simulator:
        every table is byte-identical, the inputs block is added and rehashes to the
        fingerprint the superseded record printed."""
        src = "20261010-133217-43b32f6"
        if not (r.FIXED_VCM_DIR / "corners" / src / "klt-report.json").is_file():
            self.skipTest("committed fixed-VCM data not present")
        from datetime import datetime, timezone
        rid, md = r.recompute_fixed_vcm(src, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        old = (r.FIXED_VCM_DIR / "records" / f"{src}.md").read_text()
        self.assertNotIn("## Measurement fingerprint inputs", old)  # the defect being corrected
        old_fp = re.search(r"sha256 `([0-9a-f]{64})` over the canonical", old).group(1)
        head, rehash, _ = self.rehash_record(md)
        self.assertEqual((head, rehash), (old_fp, old_fp))
        body = lambda t: t.split("- **Issue**", 1)[1].split("## Interpretation limits", 1)[0]  # noqa: E731
        self.assertEqual(body(md), body(old))
        self.assertIn(f"- **Supersedes**: `{src}`", md)
        self.assertIn(f"`sim/gain-gbw-pm/fixed-vcm/corners/{src}/`", md)
        self.assertIn(f"netlist-snapshots/{src}.spice", md)

    def test_recompute_rejects_a_fingerprint_mismatch(self):
        src = "20261010-133217-43b32f6"
        if not (r.FIXED_VCM_DIR / "corners" / src / "klt-report.json").is_file():
            self.skipTest("committed fixed-VCM data not present")
        import shutil
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in ("records", "netlist-snapshots"):
                (root / sub).mkdir()
            (root / "corners").symlink_to(r.FIXED_VCM_DIR / "corners")
            shutil.copy(r.FIXED_VCM_DIR / "records" / f"{src}.md", root / "records")
            snap = (r.FIXED_VCM_DIR / "netlist-snapshots" / f"{src}.spice").read_text()
            (root / "netlist-snapshots" / f"{src}.spice").write_text(snap.replace("CL vout 0 2p", "CL vout 0 3p"))
            with self.assertRaisesRegex(ValueError, "does not rehash"):
                r.recompute_fixed_vcm(src, root=root, now=__import__("datetime").datetime(2026, 1, 1))

    # ---- --strict in fixed mode (judge feedback on PR #126) ----

    def _run_mocked_grid(self, strict: bool | None, pm_fail_keys) -> tuple[int, str]:
        """Drive run_fixed_vcm end to end with every tool/fleet call mocked."""
        want, res = self._metrics(pm_fail_keys)
        report = {"corners": [{"process": k[0], "temperature_c": k[1],
                               "supply_v": {"vdd": k[2], "vcm": 1.2}} for k in want]}
        names = ("run_single", "materialise", "batch_block", "run_klt_retrying", "analyse_ac_report",
                 "op_flags", "run_nominal_op", "load_dut_text", "latest_gain_dir", "allocate_record_id",
                 "claim_record_paths", "ngspice_version", "klt_version")
        saved = {n: getattr(r, n) for n in names}
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ok = r.Metrics(valid=True, dc_gain_db=95.0, gbw_hz=12e6, pm_deg=59.0)
            r.run_single = lambda *a, **k: r.StudyRun("s", "d", ok, [])
            r.materialise = lambda work, pdk, *a, **k: work / "tb.spice"
            r.batch_block = lambda args: {}
            r.run_klt_retrying = lambda *a, **k: report
            r.analyse_ac_report = lambda rep, w: (res, {}, [])
            r.op_flags = lambda rep, w: {}
            r.run_nominal_op = lambda *a, **k: None
            r.load_dut_text = lambda: "dut"
            r.latest_gain_dir = lambda: None
            r.allocate_record_id = lambda root: ("20990101-000000-test", __import__("datetime").datetime(2099, 1, 1))
            r.claim_record_paths = lambda b, rec, plots=True: {
                "record": base / "records" / f"{rec}.md", "snapshot": base / "snap" / f"{rec}.spice",
                "corners": base / "corners" / rec}
            r.ngspice_version = r.klt_version = lambda: "x"
            args = types.SimpleNamespace(backend=None, batch_submit_retries=0, batch_retry_wait_s=0.0)
            if strict is not None:
                args.strict = strict
            try:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    rc = r.run_fixed_vcm(types.SimpleNamespace(path="p", variant="gf180mcuD", version="1",
                                                               source="s"), args, 1.2)
            finally:
                for n, f in saved.items():
                    setattr(r, n, f)
            md = (base / "records" / "20990101-000000-test.md").read_text()  # evidence always retained
        return rc, md

    def test_default_fixed_mode_is_diagnostic_and_succeeds_on_bound_misses(self):
        want = r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        rc, md = self._run_mocked_grid(strict=False, pm_fail_keys=want[:30])
        self.assertEqual(rc, 0)
        self.assertIn("| Phase margin | >= 60 deg | 15/45 |", md)
        rc, _ = self._run_mocked_grid(strict=None, pm_fail_keys=want[:30])  # flag absent
        self.assertEqual(rc, 0)

    def test_strict_fixed_mode_exits_1_after_retaining_evidence(self):
        want = r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        rc, md = self._run_mocked_grid(strict=True, pm_fail_keys=want[:30])
        self.assertEqual(rc, 1)
        self.assertIn("| Phase margin | >= 60 deg | 15/45 |", md)
        head, rehash, _ = self.rehash_record(md)
        self.assertEqual(head, rehash)

    def test_strict_fixed_mode_passes_when_every_row_passes(self):
        rc, _ = self._run_mocked_grid(strict=True, pm_fail_keys=())
        self.assertEqual(rc, 0)

    def test_misses_count_invalid_and_missing_points(self):
        want, res = self._metrics()
        self.assertEqual(r.fixed_vcm_misses(res, want), [])
        res[want[0]] = r.Metrics(valid=False, reason="x")
        del res[want[1]]
        self.assertEqual(len(r.fixed_vcm_misses(res, want)), len(r.ROWS))


if __name__ == "__main__":
    unittest.main(verbosity=1)
