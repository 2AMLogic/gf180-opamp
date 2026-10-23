#!/usr/bin/env python3
"""Regression tests for the input-overdrive / output-saturation recovery
experiment (issue #183).

No simulator is needed: the extraction is checked against synthetic waveforms
with analytically known answers (exponential recovery with a known time
constant, underdamped ringing, transient tolerance crossings, slow tails,
hysteresis, latch/stuck outputs, non-saturating and non-following benches),
and the source guards are checked by mutating the committed testbench / DUT
text in memory (a stale or altered DUT is rejected before anything runs).

    python3 sim/overdrive-recovery/test_overdrive_recovery.py      # or: python3 -m unittest
"""

from __future__ import annotations

import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_overdrive_recovery as r  # noqa: E402
import harness  # noqa: E402

VDD = 3.3
VCM = VDD / 2
DT = 1e-9
TAU = 15e-9


# ---------------------------------------------------------------- fixtures


def exp_recovery(s):
    """Exponential v_sat -> v_ref recovery with time constant TAU; s = time
    since the removal edge."""
    s = np.clip(s, 0, None)
    return 1 - np.exp(-s / TAU)


def ringing_recovery(zeta, wn):
    """Underdamped second-order recovery from v_sat to v_ref."""
    wd = wn * math.sqrt(1 - zeta ** 2)
    phi = math.acos(zeta)

    def h(s):
        s = np.clip(s, 0, None)
        out = 1 - np.exp(-zeta * wn * s) / math.sqrt(1 - zeta ** 2) * np.sin(wd * s + phi)
        return np.where(s > 0, out, 0.0)

    return h


def overdrive(h=exp_recovery, *, vdd=VDD, dt=DT, vsat=None, extra=None, vout=None, offset=0.0,
              vout_off=0.0, h_lo=None):
    """A full bench waveform: input at VCM; from RISE_T overdriven high (input
    VCM + OD_UP_V) with the output saturated at `hi` until FALL_T; recovery h
    to v_ref; from OD_LO_T overdriven low with the output saturated at `lo`
    until RISE2_T; recovery h_lo (default h) to the end. `h` maps
    time-since-removal to the normalised v_sat -> v_ref response; `offset`
    shifts input AND output (a follower with offset -- still valid);
    `vout_off` shifts ONLY the output (a follower error -- invalid);
    `extra(t)` adds a disturbance; `vout` replaces the whole waveform."""
    t = np.arange(0, r.TSTOP + dt / 2, dt)
    hi = vsat if vsat is not None else vdd - 0.15  # saturated near the positive rail
    lo = vsat if vsat is not None else 0.15  # saturated near ground
    vin = np.full_like(t, VCM + offset)
    vin = np.where((t >= r.RISE_T) & (t < r.FALL_T), VCM + r.OD_UP_V, vin)
    vin = np.where((t >= r.OD_LO_T) & (t < r.RISE2_T), VCM - r.OD_DN_V, vin)
    if vout is None:
        vref = VCM + offset + vout_off
        vout = np.where(t < r.RISE_T, vref, hi + (vref - hi) * h(t - r.FALL_T))
        low = np.where(t < r.RISE2_T, lo, lo + (vref - lo) * (h_lo or h)(t - r.RISE2_T))
        vout = np.where(t >= r.OD_LO_T, low, vout)
    if extra is not None:
        vout = vout + extra(t)
    return {"time": t, "v(vout)": vout, "v(vinp)": vin}


def ring_vec():
    """A valid bench whose output recovers but then rings +-15 mV forever
    (5 MHz): inside LEVEL_TOL (20 mV) so both polarities stay valid, outside
    the ~16.5 mV recovery band much of the time, and drifting far more than
    DRIFT_FRAC bands across any final reference window -> not recovered. The
    ring starts at each removal edge, so the saturation holds stay flat."""
    def ring(t):
        s1 = np.clip(t - r.FALL_T, 0, None)
        s2 = np.clip(t - r.RISE2_T, 0, None)
        return 0.015 * (np.sin(2 * math.pi * 5e6 * s1) * ((t >= r.FALL_T) & (t < r.OD_LO_T))
                        + np.sin(2 * math.pi * 5e6 * s2) * (t >= r.RISE2_T))
    return overdrive(extra=ring)


def high_phase_of(vec):
    """(t, vo, vi) restricted helper: run the high-polarity extraction alone."""
    m = r.extract_recovery(vec)
    return m


def brute_recovery(h, band_v, travel, horizon=2e-6, dt=1e-12):
    """Independent reference: last time |h - 1| > band (normalised) on a 1 ps grid."""
    s = np.arange(dt, horizon, dt)
    out = np.nonzero(np.abs(h(s) - 1) > band_v / abs(travel))[0]
    return float(s[out[-1]]) if out.size else 0.0


# ------------------------------------------------------------------ tests


class ExponentialTests(unittest.TestCase):
    def test_exponential_recovery_has_known_time(self):
        m = r.extract_recovery(overdrive())
        self.assertTrue(m.valid, m.reason)
        travel_hi = m.high.v_ref - m.high.v_sat
        travel_lo = m.low.v_ref - m.low.v_sat
        band_hi = r.BAND_FRAC * abs(m.high.v_ref)
        band_lo = r.BAND_FRAC * abs(m.low.v_ref)
        # high polarity recovers downward (travel < 0), low polarity upward
        self.assertLess(travel_hi, 0)
        self.assertGreater(travel_lo, 0)
        for ph, travel, band in ((m.high, travel_hi, band_hi), (m.low, travel_lo, band_lo)):
            with self.subTest(phase=ph.name):
                self.assertFalse(ph.latched)
                self.assertAlmostEqual(ph.rec_s, TAU * math.log(abs(travel) / band), delta=1.0e-9)
                self.assertAlmostEqual(ph.overshoot_mv, 0.0, places=6)
                self.assertAlmostEqual(ph.preshoot_mv, 0.0, places=6)
        self.assertAlmostEqual(m.recovery, max(m.high.rec_s, m.low.rec_s), places=9)

    def test_band_is_one_percent_of_the_linear_output(self):
        m = r.extract_recovery(overdrive())
        self.assertTrue(m.valid, m.reason)
        for ph in m.phases():
            self.assertAlmostEqual(ph.band_v, 0.01 * abs(ph.v_ref), places=9)
            self.assertAlmostEqual(ph.band_v, 0.01 * VCM, places=9)  # v_ref = VCM here

    def test_worse_polarity_is_reported(self):
        def slow(s):
            return np.where(s <= 0, 0.0, 1 - np.exp(-np.clip(s, 0, None) / (2 * TAU)))

        m = r.extract_recovery(overdrive(h_lo=slow))
        self.assertTrue(m.valid, m.reason)
        self.assertGreater(m.low.rec_s, m.high.rec_s)
        self.assertEqual(m.recovery, m.low.rec_s)
        self.assertFalse(m.latched)


class RingingTests(unittest.TestCase):
    def test_ringing_overshoot_and_last_exit_recovery(self):
        for zeta in (0.3, 0.5):
            with self.subTest(zeta=zeta):
                wn = 2 * math.pi * 12e6
                h = ringing_recovery(zeta, wn)
                m = r.extract_recovery(overdrive(h=h))
                self.assertTrue(m.valid, m.reason)
                expect = 100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta ** 2))  # percent of travel
                for ph in m.phases():
                    travel = abs(ph.v_ref - ph.v_sat)
                    self.assertAlmostEqual(ph.overshoot_mv, expect / 100 * travel * 1e3,
                                           delta=0.05 * expect / 100 * travel * 1e3, msg=ph.name)
                    self.assertFalse(ph.latched, ph.name)
                    self.assertAlmostEqual(ph.rec_s, brute_recovery(h, ph.band_v, ph.v_ref - ph.v_sat),
                                           delta=2.0e-9, msg=ph.name)

    def test_transient_crossing_does_not_count_as_recovered(self):
        # the output enters the band, leaves it again for a ring lobe, and
        # finally holds: the LAST exit is the recovery time
        h = ringing_recovery(0.5, 2 * math.pi * 8e6)
        m = r.extract_recovery(overdrive(h=h))
        self.assertTrue(m.valid, m.reason)
        ph = m.high
        self.assertFalse(ph.latched)
        self.assertAlmostEqual(ph.rec_s, brute_recovery(h, ph.band_v, ph.v_ref - ph.v_sat), delta=2.0e-9)
        # the last exit really is a re-entry: the band is left at least once
        s = np.arange(1e-12, 2e-6, 1e-12)
        err = np.abs(h(s) - 1) * abs(ph.v_ref - ph.v_sat)
        inside = np.nonzero(err <= ph.band_v)[0]
        outside_after = np.nonzero(err[inside[0]:] > ph.band_v)[0]
        self.assertGreater(outside_after.size, 0)  # it does leave the band again
        self.assertGreater(ph.rec_s, s[inside[0]])  # and the recovery is measured at the LAST exit


class LatchTests(unittest.TestCase):
    def test_latch_timeout_is_not_recovered(self):
        # the output starts back only 2.0 us after removal -- outside the
        # 1.59 us recovery window (a latch timeout); checked on the phase
        # extraction, which is what the record reports
        base = overdrive(h=lambda s: 1 - np.exp(-np.clip(s, 0, None) / (TAU / 10)))
        t, vo, vi = base["time"], base["v(vout)"], base["v(vinp)"]
        seg = (t >= r.FALL_T) & (t < r.FALL_T + 2.0e-6)
        vo = vo.copy()
        vo[seg] = VDD - 0.15  # still at the rail 2 us after removal
        ph, why = r.extract_phase(t, vo, vi, "high", r.RISE_T, r.FALL_T, r.OD_LO_T)
        self.assertEqual(why, "")
        self.assertIsNotNone(ph)
        self.assertTrue(ph.latched)
        self.assertTrue(math.isinf(ph.rec_s))
        self.assertEqual(r.fmt_rec(ph.rec_s), "not recovered")

    def test_hard_latch_is_invalid_at_the_next_polarity(self):
        # latched high for good: the LOW overdrive then finds the output far
        # from its linear value and the point is INVALID with the reason named
        t = overdrive()["time"]
        vec = overdrive(vout=np.where(t < r.RISE_T, VCM, VDD - 0.15))
        m = r.extract_recovery(vec)
        self.assertFalse(m.valid)
        self.assertIn("low", m.reason)
        self.assertIn("not at the input", m.reason)

    def test_wrong_final_level_hysteresis_is_not_recovered(self):
        # recovers, but to v_ref + 60 mV (one band is ~16.5 mV): y never
        # returns to 1 within the recovery window
        def hysteresis(t):
            s = np.clip(t - r.FALL_T, 0, None)
            return 0.06 * (1 - np.exp(-s / TAU)) * (t >= r.FALL_T)

        base = overdrive(extra=hysteresis)
        ph, why = r.extract_phase(base["time"], base["v(vout)"], base["v(vinp)"],
                                  "high", r.RISE_T, r.FALL_T, r.OD_LO_T)
        self.assertEqual(why, "")
        self.assertIsNotNone(ph)
        self.assertTrue(ph.latched, "a recovery to a level one band away from v_ref must be a latch")
        self.assertGreater(abs(ph.v_final - ph.v_ref), ph.band_v)

    def test_sustained_small_ring_is_not_recovered(self):
        # +-15 mV sustained ring: inside LEVEL_TOL (both polarities valid) but
        # outside the recovery band much of the time -> not recovered, while a
        # window-mean-only check would have called it recovered
        m = r.extract_recovery(ring_vec())
        self.assertTrue(m.valid, m.reason)  # the bench is valid; the output just never settles
        for ph in m.phases():
            self.assertTrue(ph.latched, ph.name)
            self.assertTrue(math.isinf(ph.rec_s), ph.name)
        self.assertTrue(m.latched)
        self.assertEqual(r.fmt_rec(m.recovery), "not recovered")

    def test_slow_tail_hidden_in_the_window_mean_is_not_recovered(self):
        # a slow linear creep back toward the rail, never leaving the band
        # (max 7.7 mV of a 16.5 mV band): the last band exit is t=0, but the
        # output drifts 7.7 mV across the final reference window -- far more
        # than DRIFT_FRAC bands -- so the recovery is not claimed
        gate_end = r.OD_LO_T - r.SAMPLE_BEFORE - r.REF_WINDOW

        def creep(t):
            return (5e-3 / 1e-6) * np.clip(t - r.FALL_T, 0, None) * (t <= gate_end)

        m = r.extract_recovery(overdrive(extra=creep))
        self.assertTrue(m.valid, m.reason)
        self.assertTrue(m.high.latched)
        self.assertGreater(m.high.drift, r.DRIFT_FRAC)
        self.assertFalse(m.low.latched)  # its window is clean

    def test_removal_edge_measured_from_the_ramp_start(self):
        # shifting the whole recovery 5 ns earlier must move the reported time by 5 ns
        m = r.extract_recovery(overdrive())
        self.assertTrue(m.valid, m.reason)
        dt = 5e-9
        vec = overdrive()
        vec["v(vout)"] = np.interp(vec["time"] - dt, vec["time"], overdrive()["v(vout)"])
        m2 = r.extract_recovery(vec)
        self.assertTrue(m2.valid, m2.reason)
        self.assertAlmostEqual(m2.high.rec_s, m.high.rec_s + dt, delta=2e-9)


class InvalidTests(unittest.TestCase):
    def test_travel_too_small_is_invalid(self):
        # the output stops half a volt from VCM on BOTH polarities... the high
        # phase is checked first and named in the reason
        m = r.extract_recovery(overdrive(vsat=VCM + 0.35))
        self.assertFalse(m.valid)
        self.assertIn("high: output did not saturate", m.reason)
        self.assertIn("travelled 0.350 V", m.reason)

    def test_low_travel_too_small_is_invalid(self):
        # the real low clip limit sits ~0.5 V above ground: a stop at 1.3 V has
        # travelled only ~0.35 V -- not pinned
        t = overdrive()["time"]
        vout = overdrive()["v(vout)"].copy()
        vout[(t >= r.OD_LO_T) & (t < r.RISE2_T)] = 1.3
        m = r.extract_recovery(overdrive(vout=vout))
        self.assertFalse(m.valid)
        self.assertIn("low: output did not saturate", m.reason)

    def test_still_slewing_at_removal_is_invalid(self):
        # deep travel but still moving at ~5 V/us across the end of the hold:
        # the output is still slewing toward the demand, not at its limit
        def ramp(t):
            return -5e6 * np.clip(t - r.RISE_T, 0, None) * (t < r.FALL_T)  # 5 V/us

        m = r.extract_recovery(overdrive(extra=ramp))
        self.assertFalse(m.valid)
        self.assertIn("high: output did not saturate", m.reason)
        self.assertIn("still moving", m.reason)

    def test_residual_creep_at_the_limit_is_saturated(self):
        # the soft landing: ~0.1 V/us of residual creep (the observed grid
        # range) stays within SAT_MAX_RATE_V_US
        def creep(t):
            return -0.1e6 * np.clip(t - r.RISE_T, 0, None) * (t < r.FALL_T)  # 0.1 V/us

        m = r.extract_recovery(overdrive(extra=creep))
        self.assertTrue(m.valid, m.reason)

    def test_not_following_before_the_overdrive_is_invalid(self):
        m = r.extract_recovery(overdrive(vout_off=0.05))  # output rests 50 mV off the input
        self.assertFalse(m.valid)
        self.assertIn("not at the input", m.reason)

    def test_malformed_inputs_are_invalid(self):
        vec = overdrive()
        self.assertFalse(r.extract_recovery({k: v[:50] for k, v in vec.items()}).valid)
        self.assertFalse(r.extract_recovery({k: v[: len(v) // 2] for k, v in vec.items()}).valid)
        self.assertFalse(r.extract_recovery({"time": vec["time"]}).valid)
        self.assertFalse(r.extract_recovery({k: v[::-1] for k, v in vec.items()}).valid)


class SummaryTests(unittest.TestCase):
    def _grid(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        base = r.extract_recovery(overdrive())
        return keys, {k: base for k in keys}

    def test_grid_is_45_unique_points(self):
        keys, _ = self._grid()
        self.assertEqual(len(set(keys)), 45)

    def test_worst_points_and_latched_rank_worst(self):
        keys, res = self._grid()
        res = dict(res)
        ring = r.extract_recovery(overdrive(h=ringing_recovery(0.5, 2 * math.pi * 8e6)))
        res[keys[7]] = ring
        latched = r.extract_recovery(ring_vec())
        res[keys[9]] = latched
        res[keys[11]] = r.Metrics(valid=False, reason="x")
        s = r.summarise(res)
        self.assertEqual(s.worst_rec[1], keys[9])
        self.assertTrue(math.isinf(s.worst_rec[0]))
        self.assertEqual(s.worst_rec_phase["high"][1], keys[9])
        self.assertEqual(s.worst_rec_phase["low"][1], keys[9])
        self.assertEqual(s.latched, [keys[9]])
        self.assertEqual(s.not_recovered["high"], [keys[9]])
        self.assertEqual(s.n_invalid, 1)
        self.assertEqual(s.worst_os[1], keys[7])

    def test_invalid_points_are_excluded_from_the_worst(self):
        keys, res = self._grid()
        res[keys[3]] = r.Metrics(valid=False, reason="x")
        s = r.summarise(res)
        self.assertEqual(s.n_invalid, 1)
        self.assertNotIn(keys[3], s.latched)


class ControlTests(unittest.TestCase):
    def test_control_logic(self):
        nom = r.ControlRun("nominal", "", r.extract_recovery(overdrive()))
        big = r.ControlRun("cl-x5", "", r.extract_recovery(overdrive(h=lambda s: 1 - np.exp(-np.clip(s, 0, None) / (4 * TAU)))))
        dead = r.ControlRun("ibias-zero", "", r.Metrics(valid=False, reason="dead"))
        self.assertEqual(r.control_failures([nom, big, dead]), [])
        same = r.ControlRun("cl-x5", "", nom.metrics)
        self.assertTrue(r.control_failures([nom, same, dead]))
        alive = r.ControlRun("ibias-zero", "", nom.metrics)
        self.assertTrue(r.control_failures([nom, big, alive]))
        latched = r.ControlRun("cl-x5", "", r.extract_recovery(ring_vec()))
        self.assertEqual(r.control_failures([nom, latched, dead]), [])  # not recovered ranks worse: allowed
        self.assertTrue(r.control_failures([r.ControlRun("nominal", "", None, error="boom"), big, dead]))


class DataTests(unittest.TestCase):
    def test_committed_data_rederive_the_same_figures(self):
        with tempfile.TemporaryDirectory() as d:
            for vec in (overdrive(), overdrive(h=ringing_recovery(0.4, 2 * math.pi * 10e6))):
                m = r.extract_recovery(vec)
                p = Path(d) / "pt.dat"
                r.save_point_data(m, p)
                m2 = r.rederive(p)
                self.assertTrue(r.same_result(m, m2), p)
            other = r.extract_recovery(overdrive(h=lambda s: 1 - np.exp(-np.clip(s, 0, None) / (2 * TAU))))
            self.assertFalse(r.same_result(m2, other))

    def test_latched_point_round_trips(self):
        m = r.extract_recovery(ring_vec())
        self.assertTrue(m.valid)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pt.dat"
            r.save_point_data(m, p)
            m2 = r.rederive(p)
            self.assertTrue(r.same_result(m, m2))
            self.assertTrue(m2.high.latched)

    def test_invalid_point_keeps_its_data_and_reason(self):
        vec = overdrive(vout_off=0.05)
        m = r.extract_recovery(vec)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pt.dat"
            r.save_point_data(m, p)
            self.assertIn("# INVALID: ", p.read_text())
            self.assertFalse(r.rederive(p).valid)

    def test_file_without_header_cannot_rederive(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pt.dat"
            np.savetxt(p, np.zeros((5, 3)), header="t v")
            with self.assertRaises(ValueError):
                r.rederive(p)


def _raw(vec) -> str:
    names = ["time", "v(vinp)", "v(vout)"]
    n = vec["time"].size
    rows = []
    for i in range(n):
        rows.append(f" {i}\t{vec['time'][i]:.12e}")
        rows.append(f"\t{vec['v(vinp)'][i]:.12e}")
        rows.append(f"\t{vec['v(vout)'][i]:.12e}")
    head = (f"Title: t\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 3\nNo. Points: {n}\nVariables:\n"
            + "".join(f"\t{i}\t{nm}\tx\n" for i, nm in enumerate(names)) + "Values:\n")
    return head + "\n".join(rows) + "\n"


class ReportTests(unittest.TestCase):
    def _corner(self, raw_path, meas):
        return {"process": "typical", "temperature_c": 27.0, "supply_v": {"vdd": 3.3, "vcm": 1.65},
                "diagnostics": [], "measurements": [{"name": k, "value": v} for k, v in meas.items()],
                "artifacts": {"raw": str(raw_path)}}

    def test_missing_or_failed_points_are_problems(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        res, _, problems = r.analyse_report({"corners": []}, keys)
        self.assertEqual((len(problems), res), (45, {}))
        c = self._corner("/nonexistent/waveform.raw", {})
        _, _, problems = r.analyse_report({"corners": [c]}, [r.NOMINAL])
        self.assertTrue(any("simulation failed" in p for p in problems))

    def test_synthetic_report_and_crosscheck(self):
        vec = overdrive()
        m = r.extract_recovery(vec)
        t, vo = vec["time"], vec["v(vout)"]
        good = {"vpre_v": m.high.v_ref,
                "vsat_hi_v": float(vo[(t >= r.RISE_T) & (t <= r.FALL_T - r.SAMPLE_BEFORE)].max()),
                "vsat_lo_v": float(vo[(t >= r.OD_LO_T) & (t <= r.RISE2_T - r.SAMPLE_BEFORE)].min()),
                "vfinal_v": float(np.interp(r.TSTOP - r.SAMPLE_BEFORE, t, vo))}
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "w.raw"
            raw.write_text(_raw(vec))
            res, _, problems = r.analyse_report({"corners": [self._corner(raw, good)]}, [r.NOMINAL])
            self.assertEqual(problems, [])
            self.assertTrue(res[r.NOMINAL].valid)
            self.assertAlmostEqual(res[r.NOMINAL].recovery, m.recovery, places=6)
            bad = dict(good, vsat_hi_v=good["vsat_hi_v"] + 50e-3)  # ngspice disagrees by 50 mV
            _, _, problems = r.analyse_report({"corners": [self._corner(raw, bad)]}, [r.NOMINAL])
            self.assertTrue(any("vsat_hi_v" in p for p in problems))


class GuardTests(unittest.TestCase):
    TEXT = r.TESTBENCH.read_text()

    def test_committed_testbench_passes(self):
        self.assertEqual(r.g.guard_testbench(self.TEXT), [])

    def test_removing_the_dut_include_fails(self):
        self.assertTrue(r.g.guard_testbench(self.TEXT.replace(".include 'opamp_two_stage.dut.spice'", "")))

    def test_hand_declared_devices_fail(self):
        self.assertTrue(r.g.guard_testbench(self.TEXT + "\nM1 a b c d nfet_03v3 W=1u L=1u\n"))
        self.assertTrue(r.g.guard_testbench(self.TEXT + "\nXq a b c d nfet_03v3 W=1u L=1u\n"))
        self.assertTrue(r.g.guard_testbench(self.TEXT + "\n.end\n"))
        self.assertTrue(r.g.guard_testbench(self.TEXT + "\n.temp 27\n"))

    def test_bench_conditions_match_the_configuration(self):
        t = self.TEXT
        self.assertIn("Ibias vdd ibias dc 10u", t)
        self.assertIn("CL vout 0 2p", t)  # CL = 2 pF [DR-1]
        self.assertIn("Xdut vdd 0 vinp vout vout ibias opamp_two_stage", t)  # unity-gain follower
        up = re.search(r"^Vup n1 vcm dc 0 pulse\((\S+) (\S+) (\S+) (\S+) (\S+) (\S+) \S+\)", t, re.M)
        dn = re.search(r"^Vdn vinp n1 dc 0 pulse\((\S+) (\S+) (\S+) (\S+) (\S+) (\S+) \S+\)", t, re.M)
        self.assertIsNotNone(up)
        self.assertIsNotNone(dn)
        sec = {"p": 1e-12, "n": 1e-9, "u": 1e-6}
        to_sec = lambda x: float(x[:-1]) * sec[x[-1]]  # noqa: E731
        u = [float(up.group(1)), float(up.group(2))] + [to_sec(x) for x in up.groups()[2:]]
        d = [float(dn.group(1)), float(dn.group(2))] + [to_sec(x) for x in dn.groups()[2:]]
        self.assertEqual(u[1], r.OD_UP_V)  # input driven above the positive rail
        self.assertEqual(d[1], -r.OD_DN_V)  # input driven below ground
        self.assertAlmostEqual(u[2], r.RISE_T)
        self.assertAlmostEqual(u[3], r.EDGE_T)
        self.assertAlmostEqual(u[4], r.EDGE_T)
        self.assertAlmostEqual(u[2] + u[3] + u[5], r.FALL_T)  # removal edge starts at FALL_T
        self.assertAlmostEqual(d[2], r.OD_LO_T)
        self.assertAlmostEqual(d[2] + d[3] + d[5], r.RISE2_T)
        # beyond the rails at every grid supply (VCM = VDD/2)
        for vdd in r.SUPPLIES_V:
            self.assertGreater(vdd / 2 + r.OD_UP_V, vdd, vdd)
            self.assertLess(vdd / 2 - r.OD_DN_V, 0.0, vdd)

    def test_window_spans_many_time_constants(self):
        tau_max = 1 / (2 * math.pi * r.GBW_MIN_HZ)
        self.assertGreaterEqual((r.FALL_T - r.RISE_T) / tau_max, 30)  # saturation hold
        self.assertGreaterEqual((r.OD_LO_T - r.SAMPLE_BEFORE - r.FALL_T) / tau_max, 80)  # recovery high
        self.assertGreaterEqual((r.TSTOP - r.RISE2_T) / tau_max, 80)  # recovery low
        self.assertLessEqual(r.TSTEP, tau_max / 3)  # 5 ns of 15.9 ns


def fake_pdk(d: Path) -> r.Pdk:
    ng = d / "pdk" / "libs.tech" / "ngspice"
    ng.mkdir(parents=True)
    (ng / "design.ngspice").write_text("* stub\n")
    return r.Pdk(d / "pdk", "gf180mcuD", "test")


class StaleDutTests(unittest.TestCase):
    """The bench must measure the committed export: an altered, stale or
    hand-edited DUT is rejected before anything is written or simulated."""

    def test_committed_dut_materialises_and_only_includes_change(self):
        with tempfile.TemporaryDirectory() as d:
            pdk = fake_pdk(Path(d))
            tb = r.materialise(Path(d) / "w", pdk)
            got, want = tb.read_text().splitlines(), r.TESTBENCH.read_text().splitlines()
            diff = [(a, b) for a, b in zip(want, got) if a != b]
            self.assertEqual(len(diff), 2)
            self.assertTrue(all(a.startswith(".include") for a, _ in diff))
            self.assertEqual((Path(d) / "w" / r.g.DUT_INCLUDE_NAME).read_text(), r.load_dut_text())
            tb2 = r.materialise(Path(d) / "w2", pdk, cl_f=20e-12, ibias_a=0.0)
            self.assertIn("CL vout 0 2e-11", tb2.read_text())
            self.assertIn("Ibias vdd ibias dc 0", tb2.read_text())

    def test_altered_device_in_the_dut_is_rejected(self):
        dut = r.load_dut_text()
        mm = re.search(r"\bW=(\S+)", dut)
        self.assertIsNotNone(mm)
        stale = dut[: mm.start()] + "W=" + mm.group(1) + "0" + dut[mm.end():]  # a resized device
        self.assertTrue(r.g.guard_dut(stale))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                r.materialise(Path(d) / "w", fake_pdk(Path(d)), dut_text=stale)
            self.assertFalse((Path(d) / "w" / r.g.DUT_INCLUDE_NAME).exists())

    def test_dropped_or_duplicated_device_is_rejected(self):
        dut = r.load_dut_text()
        self.assertTrue(r.g.guard_dut(r.g.strip_instance(dut, "XM6")))
        first_x = next(ln for ln in dut.splitlines() if ln[:1] and ln[0] in "xX")
        self.assertTrue(r.g.guard_dut(dut.replace(first_x, first_x + "\n" + first_x, 1)))

    def test_committed_dut_passes(self):
        self.assertEqual(r.g.guard_dut(r.load_dut_text()), [])


class FingerprintTests(unittest.TestCase):
    PDK = r.Pdk(Path("/x/gf180mcuD"), "gf180mcuD", "test")

    def test_runner_constants_are_the_fingerprinted_ones(self):
        for n in ("OD_UP_V", "OD_DN_V", "RISE_T", "FALL_T", "OD_LO_T", "RISE2_T", "TSTOP", "TSTEP", "EDGE_T",
                  "SAMPLE_BEFORE", "REF_WINDOW", "MIN_TRAVEL_V", "SAT_MAX_RATE_V_US", "BAND_FRAC", "DRIFT_FRAC", "IBIAS_A", "CL_F",
                  "GBW_MIN_HZ"):
            self.assertEqual(getattr(r, n), getattr(r.mc, n), n)
        self.assertEqual(r.BAND_FRAC, 0.01)
        self.assertIs(r.CORNERS, r.mc.CORNERS)
        self.assertTrue(r.mc.TESTBENCH_REL.endswith(r.TESTBENCH.name))

    def test_request_is_what_the_fingerprint_hashes(self):
        inp = r.mc.inputs(r.TESTBENCH.read_text())
        req = r.make_request(Path("/x/tb.spice"), self.PDK, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(req["analysis"], {"kind": inp["analysis"]["kind"], "args": inp["analysis"]["args"]})
        self.assertEqual(req["measurements"], inp["analysis"]["measurements"])
        self.assertEqual(req["models"]["lib"], inp["models"]["lib"])
        self.assertEqual(len(req["corners"]["process"]) * len(req["corners"]["temperature_c"])
                         * len(req["corners"]["supply_v"]["vdd"]), 45)
        self.assertEqual(req["corners"]["supply_v"]["vcm"], [round(v / 2, 6) for v in r.SUPPLIES_V])

    def test_fingerprint_changes_with_the_bench(self):
        a = r.mc.fingerprint(r.TESTBENCH.read_text())
        b = r.mc.fingerprint(r.TESTBENCH.read_text().replace("CL vout 0 2p", "CL vout 0 3p"))
        self.assertNotEqual(a, b)


class RecordTests(unittest.TestCase):
    def test_record_names_worst_recovery_and_latch_and_judges_nothing(self):
        from datetime import datetime, timezone

        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        base = r.extract_recovery(overdrive())
        res = {k: base for k in keys}
        res[keys[5]] = r.extract_recovery(overdrive(h=lambda s: np.where(s <= 0, 0.0, 1 - np.exp(-np.clip(s, 0, None) / (3 * TAU)))))
        res[keys[6]] = r.extract_recovery(ring_vec())
        ctrls = [r.ControlRun("nominal", "n", base),
                 r.ControlRun("ibias-zero", "z", r.Metrics(valid=False, reason="dead"))]
        md = r.build_record(
            record="rid", stamp=datetime(2026, 1, 1, tzinfo=timezone.utc), pdk=r.Pdk(Path("/x"), "gf180mcuD", "test"),
            ngspice="ng", klt_ver="k", backend_desc="local", report={}, results=res, ctrls=ctrls, ctrl_bad=[],
            plots=[], dut_sha="0" * 64, xchk_count=(45, 45), rederived=(45, 45, []),
        )
        self.assertIn("no pass or fail verdict", md)
        self.assertIn("input-overdrive / output-saturation recovery study -- record rid", md)
        self.assertIn("| Recovery, worse polarity | not recovered | " + r.g.fmt_key(keys[6]) + " |", md)
        self.assertIn(f"**LATCHED at 1 point(s)**: {r.g.fmt_key(keys[6])}", md)
        self.assertIn("| Latched points | 1/45 |", md)
        self.assertIn("recovery band", md)
        self.assertEqual(sum(1 for ln in md.splitlines() if ln.startswith("| `") and "|" in ln[3:]) - len(ctrls), 45)
        for word in ("PASS", "FAIL"):
            self.assertNotIn(f"**{word}**", md)

    def test_no_latch_is_stated_explicitly(self):
        from datetime import datetime, timezone

        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        res = {k: r.extract_recovery(overdrive()) for k in keys}
        md = r.build_record(
            record="rid", stamp=datetime(2026, 1, 1, tzinfo=timezone.utc), pdk=r.Pdk(Path("/x"), "gf180mcuD", "test"),
            ngspice="ng", klt_ver="k", backend_desc="local", report={}, results=res, ctrls=[], ctrl_bad=[],
            plots=[], dut_sha="0" * 64, xchk_count=(45, 45), rederived=(45, 45, []),
        )
        self.assertIn("**LATCHED: none**", md)
        self.assertIn("| Latched points | 0 (none) |", md)


class AppendOnlyTests(unittest.TestCase):
    def test_existing_record_paths_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            harness.claim_record_paths(base, "rid-1")
            (base / "records").mkdir()
            (base / "records" / "rid-1.md").write_text("x")
            with self.assertRaises(FileExistsError):
                harness.claim_record_paths(base, "rid-1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
