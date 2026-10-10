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


RAW_POWER = """Title: t
Plotname: DC transfer characteristic
Flags: real
No. Variables: 4
No. Points: 1
Variables:
\t0\ti(i-sweep)\tcurrent
\t1\tv(vdd)\tvoltage
\t2\tv(vout)\tvoltage
\t3\ti(vdd)\tcurrent
Values:
 0\t1e-05
\t{vdd}
\t1.0
\t{negidd}
"""

class PassiveCornerTests(unittest.TestCase):
    """Opt-in RZ x CC passive-corner side study (issue #97): offline request
    construction, deck validation, extraction/verdict and record rendering."""

    PDK = r.Pdk(Path("/x/gf180mcuD"), "gf180mcuD", "test")

    def _req(self, fig="slew"):
        return r.passive_request(fig, Path("/x/tb.spice"), self.PDK)

    def test_one_matrix_with_exactly_the_27_study_cells_per_figure(self):
        for fig in r.FIGURES:
            req = self._req(fig)
            axis = req["corners"]["process"]
            self.assertEqual(len(axis), 27)
            by = {a["name"]: a["sections"] for a in axis}
            self.assertEqual(by[r.pc.passive_name("ss", "worst", "best")], ["ss", "res_ss", "mimcap_ff"])
            self.assertEqual(by[r.pc.passive_name("fs", "best", "worst")], ["fs", "res_ff", "mimcap_ss"])
            sup = req["corners"]["supply_v"]
            cells = set()
            for a in axis:
                for vdd in sup["vdd"]:
                    for t in req["corners"]["temperature_c"]:
                        e = {"process": a["name"], "temperature_c": t, "supply_v": {"vdd": vdd}}
                        if e not in req["exclude"]:
                            cells.add((a["name"], float(t), float(vdd)))
            self.assertEqual(cells, set(r.pc.passive_expected_keys()))
            self.assertEqual(len(cells), 27)
            # the figure's own analysis/measurements are kept (not the ac request's)
            self.assertEqual(req["analysis"], r.mc.analysis_for(fig, r.IBIAS_A))

    def test_request_expansion_matches_klt(self):
        try:
            from klayout_tools import sim as ksim
            expand = ksim._expand_corners
        except Exception:
            self.skipTest("klt expander not importable")
        req = self._req()
        pts = expand(req["corners"], req["exclude"])
        got = {(p.process if isinstance(p.process, str) else p.process["name"], p.temperature_c, p.supply_v["vdd"])
               for p in pts}
        self.assertEqual(got, set(r.pc.passive_expected_keys()))

    def test_default_grid_request_is_unchanged(self):
        req = r.make_request("slew", Path("/x/tb.spice"), self.PDK, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertNotIn("exclude", req)
        self.assertEqual(len(req["corners"]["process"]), 5)

    def test_helpers_are_shared_not_copied(self):
        import inspect
        import passive_corners as shared
        gain = r.g
        for name in ("passive_sections", "passive_name", "passive_combos", "passive_process_axis",
                     "passive_expected_keys", "split_passive_key", "apply_passive_matrix"):
            self.assertIs(getattr(gain, name), getattr(shared, name), name)
            self.assertIs(getattr(r.pc, name), getattr(shared, name), name)
        src = inspect.getsource(r)
        for name in ("passive_sections", "passive_name", "passive_combos", "split_passive_key"):
            self.assertNotIn(f"def {name}(", src)

    def test_deck_section_validation(self):
        k = (r.pc.passive_name("ss", "worst", "best"), 125.0, 2.97)
        lib = "/opt/pdk/gf180mcuD/libs.tech/ngspice/sm141064.ngspice"
        good = f".lib {lib} ss\n.lib {lib} res_ss\n.lib {lib} mimcap_ff\n.lib /x/design.ngspice typical\n"
        self.assertEqual(r.pc.deck_section_problems(k, good), [])
        bad = good.replace("res_ss", "res_typical")
        self.assertTrue(r.pc.deck_section_problems(k, bad))
        self.assertTrue(r.pc.deck_section_problems(k, ""))

    def _fake_report(self, tmp, drop=None, extra=False, vals=None):
        """A klt-style report whose rawfiles are synthetic power waveforms."""
        corners = []
        for k in r.pc.passive_expected_keys():
            if k == drop:
                continue
            d = Path(tmp) / r.g.point_stem(k)
            d.mkdir(exist_ok=True)
            mos, rl, cl = r.pc.split_passive_key(k)
            lib = "/p/sm141064.ngspice"
            (d / "corner.cir").write_text("\n".join(f".lib {lib} {s}" for s in [mos, *r.pc.passive_sections(rl, cl)]))
            (d / "raw").write_text(RAW_POWER.format(negidd=-(vals or {}).get(k, 100e-6), vdd=k[2]))
            corners.append({
                "process": k[0], "temperature_c": k[1], "supply_v": {"vdd": k[2], "vcm": k[2] / 2},
                "measurements": [], "diagnostics": [],
                "artifacts": {"raw": str(d / "raw"), "log": None, "deck": str(d / "corner.cir")},
            })
        if extra:
            corners.append({**corners[0], "process": "typical__r-typical__c-typical", "temperature_c": -40.0})
        return {"corners": corners}

    def test_extraction_over_a_synthetic_report(self):
        with tempfile.TemporaryDirectory() as t:
            rep = self._fake_report(t)
            res, arts, problems = r.analyse_passive_report("power", rep, r.pc.passive_expected_keys())
            self.assertEqual(problems, [])
            self.assertEqual(len(res), 27)
            self.assertTrue(all(m.valid for m in res.values()))

    def test_missing_extra_and_wrong_section_cells_are_rejected(self):
        keys = r.pc.passive_expected_keys()
        with tempfile.TemporaryDirectory() as t:
            _, _, p = r.analyse_passive_report("power", self._fake_report(t, drop=keys[0]), keys)
            self.assertTrue(any("missing result" in x for x in p))
        with tempfile.TemporaryDirectory() as t:
            _, _, p = r.analyse_passive_report("power", self._fake_report(t, extra=True), keys)
            self.assertTrue(p)
        with tempfile.TemporaryDirectory() as t:
            rep = self._fake_report(t)
            deck = Path(rep["corners"][3]["artifacts"]["deck"])
            deck.write_text(deck.read_text().replace("res_", "resX_"))
            _, _, p = r.analyse_passive_report("power", rep, keys)
            self.assertTrue(any("deck loads sections" in x for x in p))

    def test_summary_verdict_and_record_report_measured_values(self):
        keys = r.pc.passive_expected_keys()
        res = {}
        for k in keys:
            mos, rl, cl = r.pc.split_passive_key(k)
            res[k] = r.Metrics(fig="slew", valid=True, slew_vus=14.0 / r.pc.MIM_FACTOR[cl])
        # one cell below the ratified bound -> FAIL, with that cell binding
        worst = (r.pc.passive_name("ss", "worst", "worst"), 125.0, 2.97)
        res[worst] = r.Metrics(fig="slew", valid=True, slew_vus=9.5)
        summ = r.passive_summary(res, "slew")
        base = summ[("ss", 125.0, 2.97)][("typical", "typical")]
        self.assertAlmostEqual(base[1], 0.0)
        self.assertTrue(base[2])
        self.assertFalse(summ[("ss", 125.0, 2.97)][("worst", "worst")][2])
        v = r.judge_figure("slew", res)
        self.assertEqual((v.verdict, v.n_pass, v.n_total, v.binding), ("FAIL", 26, 27, worst))
        verdicts = {f: r.judge_figure(f, res if f == "slew" else None, requested=f == "slew") for f in r.FIGURES}
        md = r.build_passive_record(
            record="20261010-000000-abcdef0", stamp=__import__("datetime").datetime(2026, 10, 10),
            pdk=self.PDK, ngspice="n", klt_version="k", backend_descs={"slew": "b"}, all_results={"slew": res},
            verdicts=verdicts, ctrls=[], ctrl_bad=[], dut_sha="0" * 64, not_run={}, figs=("slew",),
        )
        self.assertTrue(md.startswith(r.PASSIVE_TITLE))
        self.assertIn("passes at 26/27 cells", md)
        self.assertIn("SIDE STUDY", md)
        self.assertIn("No spec row or bound is edited", md)
        self.assertIn("9.50", md)

    def test_aggregate_report_ignores_the_side_study(self):
        sys.path.insert(0, str(HERE.parents[1] / "sim" / "report"))
        import characterization_report as cr
        self.assertTrue(r.PASSIVE_TITLE.startswith(cr.STUDY_TITLES["slew-swing-power"][0]))


if __name__ == "__main__":
    unittest.main()
