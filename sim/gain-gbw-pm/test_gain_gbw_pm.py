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


if __name__ == "__main__":
    unittest.main(verbosity=1)
