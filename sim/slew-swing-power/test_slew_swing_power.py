#!/usr/bin/env python3
"""Regression tests for the slew/swing/power experiment (issue #44).

No simulator is needed: extraction is checked against synthetic waveforms with
analytically known answers, and the source guards are checked by mutating the
committed testbench text in memory.

    python3 sim/slew-swing-power/test_slew_swing_power.py      # or: python3 -m unittest
"""

from __future__ import annotations

import contextlib
import io
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_slew_swing_power as r  # noqa: E402


# ---------------------------------------------------------------- fixtures


def follower_vec(rise_slope_vus: float, fall_slope_vus: float, *, vcm=1.65, lo=None, hi=None, dt=1e-9):
    """Piecewise-linear follower output: ramps at the given slopes (V/us) from
    `lo` to `hi` (rising at SLEW_RISE_T) and back (falling at SLEW_FALL_T)."""
    lo = vcm - 0.5 if lo is None else lo
    hi = vcm + 0.5 if hi is None else hi
    t = np.arange(0, r.SLEW_TSTOP + dt / 2, dt)
    vin = np.where((t >= r.SLEW_RISE_T) & (t < r.SLEW_FALL_T), vcm + 0.5, vcm - 0.5)
    vo = np.full_like(t, lo)
    tr = (hi - lo) / (rise_slope_vus * 1e6)
    tf = (hi - lo) / (fall_slope_vus * 1e6)
    up = (t >= r.SLEW_RISE_T) & (t < r.SLEW_FALL_T)
    vo[up] = np.minimum(hi, lo + (t[up] - r.SLEW_RISE_T) / tr * (hi - lo))
    dn = t >= r.SLEW_FALL_T
    vo[dn] = np.maximum(lo, hi - (t[dn] - r.SLEW_FALL_T) / tf * (hi - lo))
    return {"time": t, "v(vout)": vo, "v(vinp)": vin}


def swing_vec(vdd=3.3, headroom_hi=0.2, headroom_lo=0.15, sat_loss_hi=None, sat_loss_lo=None):
    """Ideal inverting transfer clipped at [headroom_lo, vdd-headroom_hi] with a
    soft knee, plus saturation margins that go negative `sat_loss_*` before the
    clip (None = at the clip)."""
    vin = np.arange(0, r.SWING_VIN_STOP_V + 1e-9, r.SWING_VIN_STEP_V)
    ideal = vdd - vin
    top, bot = vdd - headroom_hi, headroom_lo
    vout = np.clip(ideal, bot, top)
    # M6 margin: positive while vout < top - sat_loss_hi; M7 margin: positive while vout > bot + sat_loss_lo
    sh = headroom_hi * 0 if sat_loss_hi is None else sat_loss_hi
    sl = 0.0 if sat_loss_lo is None else sat_loss_lo
    m6 = (top - sh) - ideal * 0 - vout
    m7 = vout - (bot + sl)
    if sat_loss_hi is None:
        m6 = np.full_like(vin, 1.0)
    if sat_loss_lo is None:
        m7 = np.full_like(vin, 1.0)
    vec = {
        "v(vin)": vin,
        "v(vout)": vout,
        "v(@m.xdut.xm6.m0[vds])": 1.0 + m6,
        "v(@m.xdut.xm6.m0[vdsat])": np.full_like(vin, 1.0),
        "v(@m.xdut.xm7.m0[vds])": 1.0 + m7,
        "v(@m.xdut.xm7.m0[vdsat])": np.full_like(vin, 1.0),
    }
    return vec


def power_vec(idd=80e-6, vdd=3.3, ibias=r.IBIAS_A):
    return {
        "i(i-sweep)": np.array([ibias, ibias + 1e-6]),
        "i(vdd)": np.array([-idd, -idd]),
        "v(vdd)": np.array([vdd, vdd]),
        "v(vout)": np.array([vdd / 2, vdd / 2]),
    }


# ------------------------------------------------------------------ tests


class PowerTests(unittest.TestCase):
    def test_power_is_supply_current_times_vdd(self):
        m = r.extract_power(power_vec(idd=80e-6, vdd=3.63), 3.63)
        self.assertTrue(m.valid, m.reason)
        self.assertAlmostEqual(m.power_uw, 80e-6 * 3.63 * 1e6, places=6)
        self.assertAlmostEqual(m.idd_ua, 80.0, places=6)

    def test_wrong_polarity_or_dead_circuit_is_invalid(self):
        v = power_vec()
        v["i(vdd)"] = np.array([+80e-6, +80e-6])
        self.assertFalse(r.extract_power(v, 3.3).valid)
        self.assertFalse(r.extract_power(power_vec(idd=5e-6), 3.3).valid)  # below the bias branch

    def test_wrong_supply_or_sweep_point_is_invalid(self):
        self.assertFalse(r.extract_power(power_vec(vdd=3.3), 3.63).valid)
        v = power_vec()
        v["i(i-sweep)"] = np.array([5e-6, 6e-6])
        self.assertFalse(r.extract_power(v, 3.3).valid)

    def test_bound_is_an_inclusive_upper_limit(self):
        m = r.extract_power(power_vec(idd=r.POWER_MAX_UW * 1e-6 / 3.3), 3.3)
        self.assertTrue(m.valid)
        self.assertAlmostEqual(m.power_uw, r.POWER_MAX_UW, places=6)
        self.assertTrue(r.point_passes(m, "power"))
        over = r.extract_power(power_vec(idd=r.POWER_MAX_UW * 1.001e-6 / 3.3), 3.3)
        self.assertFalse(r.point_passes(over, "power"))


class SlewTests(unittest.TestCase):
    def test_known_ramp_slopes_recovered_and_slower_edge_reported(self):
        m = r.extract_slew(follower_vec(16.0, 12.0))
        self.assertTrue(m.valid, m.reason)
        self.assertAlmostEqual(m.slew_rise_vus, 16.0, delta=0.05)
        self.assertAlmostEqual(m.slew_fall_vus, 12.0, delta=0.05)
        self.assertAlmostEqual(m.slew_vus, 12.0, delta=0.05)
        m2 = r.extract_slew(follower_vec(9.0, 20.0))
        self.assertAlmostEqual(m2.slew_vus, 9.0, delta=0.05)

    def test_rounded_top_uses_the_20_80_window_only(self):
        # Exponential approach: slope at 20-80 % is the chord, not the initial/peak slope.
        vec = follower_vec(16.0, 16.0)
        t, vin = vec["time"], vec["v(vinp)"]
        lo, hi = 1.15, 2.15
        tau = 20e-9
        vo = np.full_like(t, lo)
        up = (t >= r.SLEW_RISE_T) & (t < r.SLEW_FALL_T)
        vo[up] = hi - (hi - lo) * np.exp(-(t[up] - r.SLEW_RISE_T) / tau)
        dn = t >= r.SLEW_FALL_T
        vo[dn] = lo + (hi - lo) * np.exp(-(t[dn] - r.SLEW_FALL_T) / tau)
        m = r.extract_slew({"time": t, "v(vout)": vo, "v(vinp)": vin})
        self.assertTrue(m.valid, m.reason)
        # chord of 20-80 % of an exponential: 0.6*step / (tau*ln(0.8/0.2)) = 0.6/ (20ns*1.386)
        expect = 0.6 * 1.0 / (tau * math.log(4.0)) * 1e-6
        self.assertAlmostEqual(m.slew_vus, expect, delta=0.05 * expect)

    def test_output_that_never_settles_to_the_input_is_invalid(self):
        m = r.extract_slew(follower_vec(0.3, 0.3))  # 0.3 V/us: still ramping at the pre-fall instant
        self.assertFalse(m.valid)
        self.assertIn("settle", m.reason)
        self.assertFalse(r.point_passes(m, "slew"))

    def test_clipped_output_is_invalid_not_a_fast_slew(self):
        vec = follower_vec(16.0, 16.0)
        vec["v(vinp)"] = np.where(vec["v(vinp)"] > 1.65, 1.65 + 0.5, 1.65 - 0.5)
        clipped = follower_vec(16.0, 16.0, hi=1.65 + 0.05)  # output stops 0.05 V above lo+...: settles wrong vs input
        m = r.extract_slew({"time": clipped["time"], "v(vout)": clipped["v(vout)"], "v(vinp)": vec["v(vinp)"]})
        self.assertFalse(m.valid)

    def test_stuck_output_is_invalid(self):
        vec = follower_vec(16.0, 16.0)
        vec["v(vout)"] = np.full_like(vec["time"], 1.15)
        m = r.extract_slew(vec)
        self.assertFalse(m.valid)

    def test_malformed_inputs_are_invalid(self):
        vec = follower_vec(16.0, 16.0)
        short = {k: v[:50] for k, v in vec.items()}
        self.assertFalse(r.extract_slew(short).valid)
        cut = {k: v[: len(v) // 2] for k, v in vec.items()}
        self.assertFalse(r.extract_slew(cut).valid)
        self.assertFalse(r.extract_slew({"time": vec["time"]}).valid)
        rev = {k: v[::-1] for k, v in vec.items()}
        self.assertFalse(r.extract_slew(rev).valid)

    def test_bound_is_inclusive(self):
        m = r.extract_slew(follower_vec(r.SLEW_MIN_VUS, r.SLEW_MIN_VUS))
        self.assertTrue(m.valid, m.reason)
        m.slew_vus = r.SLEW_MIN_VUS
        self.assertTrue(r.point_passes(m, "slew"))
        m.slew_vus = r.SLEW_MIN_VUS - 0.01
        self.assertFalse(r.point_passes(m, "slew"))


class SwingTests(unittest.TestCase):
    def test_clip_levels_recovered_when_gain_collapse_binds(self):
        m = r.extract_swing(swing_vec(3.3, 0.2, 0.15), 1.65)
        self.assertTrue(m.valid, m.reason)
        self.assertAlmostEqual(m.vout_hi_v, 3.1, delta=0.02)
        self.assertAlmostEqual(m.vout_lo_v, 0.15, delta=0.02)
        self.assertAlmostEqual(m.swing_v, 2.95, delta=0.03)
        self.assertEqual((m.edge_hi, m.edge_lo), ("gain", "gain"))

    def test_saturation_loss_binds_before_gain_collapse(self):
        m = r.extract_swing(swing_vec(3.3, 0.2, 0.15, sat_loss_hi=0.3, sat_loss_lo=0.25), 1.65)
        self.assertTrue(m.valid, m.reason)
        self.assertEqual((m.edge_hi, m.edge_lo), ("M6", "M7"))
        self.assertAlmostEqual(m.vout_hi_v, 3.1 - 0.3, delta=0.02)
        self.assertAlmostEqual(m.vout_lo_v, 0.15 + 0.25, delta=0.02)
        self.assertLess(m.swing_v, m.sens["gain-only"])
        self.assertAlmostEqual(m.sens["sat-only"], m.swing_v, delta=0.02)

    def test_bound_applies_to_the_swing_in_vpp(self):
        ok = r.extract_swing(swing_vec(2.97, 0.2, 0.15), 1.485)
        self.assertTrue(ok.valid, ok.reason)
        self.assertGreaterEqual(ok.swing_v, r.SWING_MIN_V)
        self.assertTrue(r.point_passes(ok, "swing"))
        tight = r.extract_swing(swing_vec(2.97, 0.45, 0.4), 1.485)
        self.assertTrue(tight.valid, tight.reason)
        self.assertLess(tight.swing_v, r.SWING_MIN_V)
        self.assertFalse(r.point_passes(tight, "swing"))

    def test_sweep_that_ends_before_collapse_is_invalid_never_a_bound(self):
        vec = swing_vec(3.3, 0.2, 0.15)
        n = int(2.6 / r.SWING_VIN_STEP_V)  # stop at vin = 2.6: vout never reaches the lower clip
        vec = {k: v[:n] for k, v in vec.items()}
        m = r.extract_swing(vec, 1.65)
        self.assertFalse(m.valid)
        self.assertIn("unbounded", m.reason)

    def test_wrong_polarity_or_gain_is_invalid(self):
        vec = swing_vec()
        vec["v(vout)"] = 3.3 - vec["v(vout)"]  # non-inverting
        self.assertFalse(r.extract_swing(vec, 1.65).valid)
        vec = swing_vec()
        vec["v(vout)"] = 1.65 + 0.5 * (vec["v(vout)"] - 1.65)  # gain -0.5
        self.assertFalse(r.extract_swing(vec, 1.65).valid)

    def test_device_unsaturated_at_vcm_is_invalid(self):
        vec = swing_vec()
        vec["v(@m.xdut.xm6.m0[vds])"] = np.full_like(vec["v(vin)"], 0.1)
        m = r.extract_swing(vec, 1.65)
        self.assertFalse(m.valid)
        self.assertIn("not saturated", m.reason)

    def test_missing_or_malformed_data_is_invalid(self):
        vec = swing_vec()
        self.assertFalse(r.extract_swing({k: v for k, v in vec.items() if k != "v(vout)"}, 1.65).valid)
        self.assertFalse(r.extract_swing({k: v[:20] for k, v in vec.items()}, 1.65).valid)
        self.assertFalse(r.extract_swing({k: v[::-1] for k, v in vec.items()}, 1.65).valid)


class SwingDataTests(unittest.TestCase):
    """The committed swing/*.dat must carry what decides the verdict (PR #57 review)."""

    def _roundtrip(self, vec, vcm):
        m = r.extract_swing(vec, vcm)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pt.dat"
            r.save_point_data("swing", m, path, vcm=vcm)
            text = path.read_text()
            m2 = r.rederive_swing(path)
            back, vcm2 = r.load_swing_dat(path)
        return m, m2, text, back, vcm2

    def test_saturation_columns_are_saved_with_vcm(self):
        vec = swing_vec(3.3, 0.2, 0.15, sat_loss_hi=0.3, sat_loss_lo=0.25)
        m, _, text, back, vcm = self._roundtrip(vec, 1.65)
        header = [ln for ln in text.splitlines() if ln.startswith("#")]
        self.assertIn("# vcm_v=1.65", header)
        self.assertIn("# vin_v vout_v m6_vds_v m6_vdsat_v m7_vds_v m7_vdsat_v", header)
        self.assertEqual(vcm, 1.65)
        for name in ("v(vin)", "v(vout)", "v(@m.xdut.xm6.m0[vds])", "v(@m.xdut.xm6.m0[vdsat])",
                     "v(@m.xdut.xm7.m0[vds])", "v(@m.xdut.xm7.m0[vdsat])"):
            np.testing.assert_array_equal(back[name], vec[name], err_msg=name)  # full precision, every sample

    def test_saturation_bound_verdict_is_rederived_from_the_file(self):
        vec = swing_vec(2.97, 0.2, 0.15, sat_loss_hi=0.3, sat_loss_lo=0.25)
        m, m2, *_ = self._roundtrip(vec, 1.485)
        self.assertTrue(m.valid and m2.valid, (m.reason, m2.reason))
        self.assertEqual((m2.edge_hi, m2.edge_lo), ("M6", "M7"))
        self.assertTrue(r.same_swing(m, m2))
        self.assertEqual(m2.swing_v, m.swing_v)

    def test_gain_bound_verdict_is_rederived_from_the_file(self):
        m, m2, *_ = self._roundtrip(swing_vec(3.3, 0.2, 0.15), 1.65)
        self.assertEqual((m2.edge_hi, m2.edge_lo), ("gain", "gain"))
        self.assertTrue(r.same_swing(m, m2))

    def test_vin_vout_only_file_cannot_rederive(self):
        # the pre-fix format (vin_v vout_v only) must not silently re-derive a swing
        m = r.extract_swing(swing_vec(), 1.65)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.dat"
            np.savetxt(path, np.column_stack([m.x, m.y]), header="vin_v vout_v")
            with self.assertRaises(ValueError):
                r.rederive_swing(path)
            vec, vcm = r.load_swing_dat(path)
            self.assertFalse(r.extract_swing(vec, 1.65).valid)  # no saturation vectors -> INVALID, not a pass

    def test_same_swing_detects_a_changed_edge_or_level(self):
        m = r.extract_swing(swing_vec(3.3, 0.2, 0.15, sat_loss_hi=0.3, sat_loss_lo=0.25), 1.65)
        other = r.extract_swing(swing_vec(3.3, 0.2, 0.15), 1.65)
        self.assertFalse(r.same_swing(m, other))
        bad = r.Metrics(fig="swing", valid=False, reason="x")
        self.assertFalse(r.same_swing(m, bad))

    def test_invalid_point_keeps_its_data_and_reason(self):
        vec = swing_vec()
        vec["v(@m.xdut.xm6.m0[vds])"] = np.full_like(vec["v(vin)"], 0.1)
        m, m2, text, *_ = self._roundtrip(vec, 1.65)
        self.assertFalse(m.valid)
        self.assertIn("# INVALID: ", text)
        self.assertFalse(m2.valid)


class RawParserTests(unittest.TestCase):
    RAW = (
        "Title: t\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 2\nNo. Points: 2\n"
        "Variables:\n\t0\ttime\ttime\n\t1\tv(vout)\tvoltage\nValues:\n"
        " 0\t0.0\n\t1.5\n 1\t1e-9\n\t1.6\n"
    )

    def test_parse(self):
        v = r.parse_ascii_real_raw(self.RAW)
        self.assertEqual(list(v), ["time", "v(vout)"])
        np.testing.assert_allclose(v["v(vout)"], [1.5, 1.6])
        np.testing.assert_allclose(v["time"], [0.0, 1e-9])

    def test_malformed_raw_fails_loudly(self):
        with self.assertRaises(ValueError):
            r.parse_ascii_real_raw(self.RAW.replace("Flags: real", "Flags: complex"))
        with self.assertRaises(ValueError):
            r.parse_ascii_real_raw(self.RAW.replace("1.6\n", ""))
        with self.assertRaises(ValueError):
            r.parse_ascii_real_raw(self.RAW.replace("1.5", "nan"))
        with self.assertRaises(ValueError):
            r.parse_ascii_real_raw("garbage")


class VerdictTests(unittest.TestCase):
    def test_bounds_are_the_ratified_values(self):
        self.assertEqual(r.SLEW_MIN_VUS, 10.0)
        self.assertEqual(r.SWING_MIN_V, 2.3)
        self.assertEqual(r.POWER_MAX_UW, 350.0)
        self.assertEqual(r.SWING_STRETCH_V, 2.6)

    def _res(self, fig, values, valid=True):
        out = {}
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        for k, v in zip(keys, values):
            m = r.Metrics(fig=fig, valid=valid)
            setattr(m, r.ROWS[fig][1], v)
            out[k] = m
        return out, keys

    def test_binding_corner_direction(self):
        vals = [100.0] * 45
        vals[7] = 90.0
        vals[20] = 400.0
        res, keys = self._res("power", vals)
        v = r.judge_figure("power", res)
        self.assertEqual((v.verdict, v.worst_value, v.binding, v.n_pass), ("FAIL", 400.0, keys[20], 44))
        res, keys = self._res("slew", [20.0] * 44 + [9.0])
        v = r.judge_figure("slew", res)
        self.assertEqual((v.verdict, v.worst_value, v.binding, v.n_pass), ("FAIL", 9.0, keys[44], 44))

    def test_all_pass_and_stretch_count(self):
        res, _ = self._res("swing", [2.6] * 40 + [2.5] * 5)
        v = r.judge_figure("swing", res)
        self.assertEqual((v.verdict, v.n_pass, v.stretch_pass), ("PASS", 45, 40))

    def test_invalid_point_fails_the_row(self):
        res, keys = self._res("slew", [20.0] * 45)
        res[keys[3]] = r.Metrics(fig="slew", valid=False, reason="x")
        v = r.judge_figure("slew", res)
        self.assertEqual((v.verdict, v.binding, v.n_invalid), ("FAIL", keys[3], 1))

    def test_not_run_is_never_a_pass(self):
        self.assertEqual(r.judge_figure("slew", None).verdict, "NOT RUN")

    def test_not_requested_is_never_a_pass(self):
        v = r.judge_figure("power", None, requested=False)
        self.assertEqual((v.verdict, v.n_pass, v.n_total), ("NOT REQUESTED", 0, 0))
        res, _ = self._res("power", [100.0] * 45)
        self.assertEqual(r.judge_figure("power", res, requested=False).verdict, "NOT REQUESTED")

    def test_figures_option_rejects_unknown_or_empty(self):
        for bad in ("gain", "", "swing,bogus"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                r.main(["--figures", bad])

    def test_grid_is_the_full_45_unique_points(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(len(keys), 45)
        self.assertEqual(len(set(keys)), 45)

    def test_report_with_missing_or_failed_point_is_a_problem(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        for fig in r.FIGURES:
            res, arts, problems = r.analyse_report(fig, {"corners": []}, keys)
            self.assertEqual(len(problems), 45)
            self.assertEqual(res, {})
        c = {"process": "typical", "temperature_c": 27.0, "supply_v": {"vdd": 3.3, "vcm": 1.65},
             "diagnostics": [], "measurements": [], "artifacts": {"raw": "/nonexistent/waveform.raw"}}
        res, arts, problems = r.analyse_report("slew", {"corners": [c]}, [("typical", 27.0, 3.3)])
        self.assertTrue(any("simulation failed" in p for p in problems))


class GuardTests(unittest.TestCase):
    def test_committed_testbenches_pass(self):
        for fig, path in r.TESTBENCH.items():
            self.assertEqual(r.g.guard_testbench(path.read_text()), [], fig)

    def test_removing_the_dut_include_fails(self):
        for fig, path in r.TESTBENCH.items():
            text = path.read_text().replace(".include 'opamp_two_stage.dut.spice'", "")
            self.assertTrue(r.g.guard_testbench(text), fig)

    def test_hand_declared_transistors_fail(self):
        for fig, path in r.TESTBENCH.items():
            text = path.read_text() + "\nM1 a b c d nfet_03v3 W=1u L=1u\n"
            self.assertTrue(r.g.guard_testbench(text), fig)
            text = path.read_text() + "\nXq a b c d nfet_03v3 W=1u L=1u\n"
            self.assertTrue(r.g.guard_testbench(text), fig)

    def test_testbenches_are_circuit_bodies_with_one_dut(self):
        for fig, path in r.TESTBENCH.items():
            text = path.read_text()
            self.assertTrue(r.g.guard_testbench(text + "\n.end\n"), fig)
            self.assertTrue(r.g.guard_testbench(text + "\n.temp 27\n"), fig)
            self.assertEqual(sum(1 for ln in text.splitlines() if ln.startswith("Xdut ")), 1, fig)

    def test_bias_and_load_conditions(self):
        text = {f: p.read_text() for f, p in r.TESTBENCH.items()}
        for fig, t in text.items():
            self.assertIn("Ibias vdd ibias dc 10u", t, fig)
        self.assertNotIn("\nCL ", text["power"])  # no output load for the power bench
        self.assertNotIn("\nCL ", text["swing"])
        self.assertIn("CL vout 0 2p", text["slew"])  # CL = 2 pF [DR-1]

    def test_materialise_rewrites_only_includes_and_requested_override(self):
        pdk = r.find_pdk()
        with tempfile.TemporaryDirectory() as d:
            for fig, path in r.TESTBENCH.items():
                tb = r.materialise(fig, Path(d) / fig, pdk)
                got = tb.read_text().splitlines()
                want = path.read_text().splitlines()
                diff = [(a, b) for a, b in zip(want, got) if a != b]
                self.assertEqual(len(diff), 2, fig)
                self.assertTrue(all(a.startswith(".include") for a, _ in diff))
                tb2 = r.materialise(fig, Path(d) / (fig + "2"), pdk, ibias_a=5e-6)
                self.assertIn("Ibias vdd ibias dc 5e-06", tb2.read_text())


class AppendOnlyTests(unittest.TestCase):
    def test_existing_record_paths_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            r.g.claim_record_paths(base, "rid-1")
            (base / "records").mkdir()
            (base / "records" / "rid-1.md").write_text("x")
            with self.assertRaises(FileExistsError):
                r.g.claim_record_paths(base, "rid-1")


class MeasurementFingerprint(unittest.TestCase):
    """Issue #89: the runner and the fingerprint share ONE configuration source,
    per figure."""

    PDK = r.Pdk(Path("/x/gf180mcuD"), "gf180mcuD", "test")
    TEXTS = {f: r.TESTBENCH[f].read_text() for f in r.FIGURES}

    def test_runner_constants_are_the_fingerprinted_ones(self):
        for n in ("SLEW_RISE_T", "SLEW_FALL_T", "SLEW_TSTOP", "SLEW_TSTEP", "SLEW_STEP_V", "SLEW_LO_FRAC", "SLEW_HI_FRAC",
                  "SLEW_SAMPLE_BEFORE", "SWING_VIN_STOP_V", "SWING_VIN_STEP_V", "SWING_FRAC", "SWING_MID_BAND_V", "IBIAS_A"):
            self.assertEqual(getattr(r, n), getattr(r.mc, n), n)
        self.assertEqual(r.FIGURES, r.mc.FIGURES)
        self.assertIs(r.CORNERS, r.mc.CORNERS)
        self.assertEqual(sorted(r.mc.TESTBENCHES_REL), sorted(r.FIGURES))
        for f in r.FIGURES:
            self.assertTrue(r.mc.TESTBENCHES_REL[f].endswith(r.TESTBENCH[f].name))

    def test_each_figure_request_is_what_the_fingerprint_hashes(self):
        inp = r.mc.inputs(self.TEXTS)
        for f in r.FIGURES:
            with self.subTest(f):
                req = r.make_request(f, Path("/x/tb.spice"), self.PDK, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
                fi = inp["figures"][f]
                self.assertEqual(req["analysis"], {"kind": fi["analysis"]["kind"], "args": fi["analysis"]["args"]})
                self.assertEqual(req["measurements"], fi["analysis"]["measurements"])
                self.assertEqual(req["models"]["lib"], inp["models"]["lib"])

    def test_controls_are_the_fingerprinted_ones(self):
        self.assertEqual([c[2] for c in r.mc.CONTROLS][1:], [5e-6, 0.0])
        self.assertEqual(r.mc.inputs(self.TEXTS)["figures"]["power"]["controls"]["ibias_a"], [c[2] for c in r.mc.CONTROLS])

    def test_record_scope_is_the_measured_figures(self):
        texts, sel = r.fingerprint_scope(["swing"])
        self.assertEqual(sorted(r.mc.inputs(texts, sel)["figures"]), ["swing"])
        texts, sel = r.fingerprint_scope(list(r.FIGURES))
        self.assertEqual(sorted(r.mc.inputs(texts, sel)["figures"]), sorted(r.FIGURES))

    def test_record_embeds_header_and_inputs_for_selected_figures(self):
        texts, sel = r.fingerprint_scope(["power"])
        blob = "\n".join(r.mc.fingerprint_lines(texts, sel) + r.mc.inputs_section(texts, sel))
        self.assertIn(r.mc.fingerprint(texts, sel), blob)
        self.assertIn("power", blob)
        self.assertNotIn('"slew":', blob)


if __name__ == "__main__":
    unittest.main()
