#!/usr/bin/env python3
"""Regression tests for the supply / external-bias enable acquisition
experiment (issue #191).

No simulator is needed: the extraction is checked against synthetic waveforms
with analytically known answers (exponential acquisition with a known time
constant, recrossing, unresolved / slow-tail / single-crossing windows, invalid
references, powered starts, missing samples), and the source guards are
checked by mutating the committed testbench / DUT text in memory.

    python3 sim/enable-acquisition/test_enable_acquisition.py      # or: python3 -m unittest
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_enable_acquisition as r  # noqa: E402

VDD = 3.3
VCM = VDD / 2
TAU = 15e-9


# ---------------------------------------------------------------- fixtures


def ramp01(t, t0, dur):
    return np.clip((t - t0) / dur, 0.0, 1.0)


def envelopes(sched, t):
    """The two 0..1 envelopes exactly as the bench's pwl sources define them."""
    p = sched["params"]
    rs = ramp01(t, p["t_vdd"], p["tr_s"])
    rb = ramp01(t, p["t_ib"], p["tr_b"]) - ramp01(t, p["t_off"], p["tr_b"]) + ramp01(t, p["t_on2"], p["tr_b"])
    return rs, rb


def enable_vec(case=("simultaneous", "fast"), *, vdd=VDD, dt=None, h=None, v_off=0.0, extra=None, vout=None,
               rb_override=None, rail_scale=1.0, powered_start=False):
    """A full bench waveform. `h(s)` maps seconds since an event's ramps
    completed to the normalised remaining error (1 at completion, 0 acquired);
    before completion vout follows the ramped input. `v_off` shifts the final
    follower level (systematic offset). Returns (sched, vec)."""
    sched = r.mc.schedule(*case)
    dt = dt or sched["tstep"]
    t = np.arange(0, sched["tstop"] + dt / 2, dt)
    rs, rb = envelopes(sched, t)
    if powered_start:
        rs, rb = np.ones_like(t), np.ones_like(t)
    vcm = vdd / 2
    h = h or (lambda s: np.exp(-np.clip(s, 0, None) / TAU))
    vref = vcm + v_off
    A = 0.2  # error at the instant the ramps complete (V)
    vo = vcm * rs * 1.0 + v_off * rs  # tracks the ramped input before completion
    for name, t0, tc, t1 in sched["events"]:
        sel = (t >= tc) & (t <= t1)
        vo = np.where(sel, vref + A * h(t - tc), vo)
    if sched["t_off"] < sched["tstop"]:  # bias off: output collapses toward 0 while the bias is removed
        off = (t >= sched["t_off"]) & (t < sched["t_on2"] + sched["tr_b"])
        vo = np.where(off, vref * (1 - ramp01(t, sched["t_off"], sched["tr_b"])) + 0.0, vo)
        # re-enable window values are set above (t >= tc of the second event)
        for name, t0, tc, t1 in sched["events"]:
            sel = (t >= tc) & (t <= t1)
            vo = np.where(sel, vref + A * h(t - tc), vo)
    vec = {
        "time": t, "v(vdd)": vdd * rs * rail_scale, "v(rs)": rs, "v(rb)": rb if rb_override is None else rb_override,
        "v(ibias)": 0.9 * rb, "v(vinp)": vcm * rs, "v(vout)": vo if vout is None else vout,
        "v(xdut.n1)": 2.4 * rs, "v(xdut.n2)": 2.4 * rs, "v(xdut.nz)": 2.0 * rs, "v(xdut.tail)": 0.8 * rs,
    }
    if extra is not None:
        vec["v(vout)"] = vec["v(vout)"] + extra(t)
    return sched, vec


def stuck(sched, vec, level):
    """Output that starts at 0 and sits at `level` once the rail starts to ramp."""
    vec["v(vout)"] = np.where(vec["time"] >= sched["params"]["t_vdd"], level, 0.0)
    return vec


def run(case=("simultaneous", "fast"), *, vdd=VDD, v_ref=None, **kw):
    sched, vec = enable_vec(case, vdd=vdd, **kw)
    ref = vdd / 2 + kw.get("v_off", 0.0) if v_ref is None else v_ref
    return sched, r.extract_enable(vec, sched, vdd, ref)


# ------------------------------------------------------------------ tests


class AcquisitionTimingTests(unittest.TestCase):
    def test_exponential_acquisition_has_known_time(self):
        sched, m = run()
        self.assertTrue(m.valid, m.reason)
        band = r.BAND_FRAC * VCM
        want = TAU * math.log(0.2 / band)
        self.assertAlmostEqual(m.events[0].acq_s, want, delta=1.0e-9)  # 5 ns sampling, linear interpolation
        self.assertAlmostEqual(m.events[0].t_acq, sched["events"][0][2] + want, delta=1.0e-9)
        self.assertFalse(m.events[0].censored)

    def test_band_is_one_percent_of_the_steady_level_and_scales_with_supply(self):
        for vdd in r.SUPPLIES_V:
            _, m = run(vdd=vdd)
            self.assertAlmostEqual(m.events[0].acq_s, TAU * math.log(0.2 / (0.01 * vdd / 2)), delta=1.0e-9, msg=vdd)

    def test_slower_response_acquires_later(self):
        _, a = run()
        _, b = run(h=lambda s: np.exp(-np.clip(s, 0, None) / (4 * TAU)))
        self.assertGreater(b.events[0].acq_s, a.events[0].acq_s + 50e-9)

    def test_systematic_offset_does_not_matter_when_the_reference_includes_it(self):
        _, m = run(v_off=5e-3)
        self.assertTrue(m.valid, m.reason)
        self.assertFalse(m.events[0].censored)

    def test_acquisition_before_the_ramps_complete_is_negative_not_clamped(self):
        _, m = run(h=lambda s: np.zeros_like(s))  # already at v_ref at completion, and (tracking) within band before
        self.assertTrue(m.valid)
        self.assertLessEqual(m.events[0].acq_s, 0.0)

    def test_recrossing_counts_the_last_exit(self):
        # acquires at ~30 ns, then a 40 mV bump re-exits the band at +1.0 us for 100 ns
        def bump(t):
            return 0.04 * ((t >= 2.1e-6) & (t < 2.2e-6))

        sched, m = run(extra=bump)
        e = m.events[0]
        self.assertFalse(e.censored)
        self.assertAlmostEqual(e.t_acq, 2.2e-6, delta=r.mc.schedule(*("simultaneous", "fast"))["tstep"] * 1.1)
        self.assertGreater(e.acq_s, 1.0e-6)

    def test_tolerance_crossing_that_leaves_again_is_not_an_acquisition(self):
        # inside the band once at ~30 ns, then rings out of it again up to the very end of the window
        ring = lambda t: 0.03 * np.sin(2 * math.pi * 5e6 * t) * (t > 1.4e-6)  # noqa: E731
        _, m = run(extra=ring)
        self.assertTrue(m.valid)
        self.assertTrue(m.events[0].censored)
        self.assertTrue(math.isinf(m.events[0].acq_s))


class CensoredTests(unittest.TestCase):
    def test_never_acquired_is_not_acquired_and_not_finite(self):
        sched, vec = enable_vec()
        stuck(sched, vec, 0.5)
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(m.valid)
        self.assertTrue(m.events[0].censored)
        self.assertEqual(m.events[0].acq_s, math.inf)
        self.assertTrue(math.isnan(m.events[0].t_acq))
        self.assertTrue(m.censored)
        self.assertEqual(m.acq, math.inf)

    def test_slow_tail_inside_the_band_is_not_acquired(self):
        # climbs through the band edge and keeps creeping: 6 mV across the final 200 ns (3.7 bands/10 -> > DRIFT_FRAC)
        def creep(t):
            return 0.0 + 0.0 * t

        sched, vec = enable_vec(h=lambda s: np.zeros_like(s))
        ts = vec["time"]
        t1 = sched["events"][0][3]
        vec["v(vout)"] = np.where(ts >= sched["events"][0][2], VCM + 4e-3 + 2e-3 * (ts - (t1 - 4e-6)) / 4e-6 * 0 +
                                  3e-3 * np.clip((ts - (t1 - r.REF_WINDOW)) / r.REF_WINDOW, 0, None), vec["v(vout)"])
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(m.valid)
        e = m.events[0]
        self.assertGreater(e.drift, r.DRIFT_FRAC)
        self.assertTrue(e.censored)
        self.assertIn("slow tail", e.why)

    def test_single_crossing_just_before_the_window_end_is_not_acquired(self):
        # sits 50 mV off until 0.3 us before the end of the 4 us window (> REF_WINDOW, < 10 % dwell), then in band
        sched, vec = enable_vec()
        t1 = sched["events"][0][3]
        vec["v(vout)"] = np.where((vec["time"] >= t1 - 0.3e-6) | (vec["time"] < sched["events"][0][2]),
                                  vec["v(vout)"], VCM + 0.05)
        vec["v(vout)"] = np.where(vec["time"] >= t1 - 0.3e-6, VCM, vec["v(vout)"])
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(m.events[0].censored)

    def test_wrong_final_level_is_not_acquired(self):
        _, m = run(v_ref=VCM + 0.1)  # reference 100 mV away is invalid; use offset output instead
        self.assertFalse(m.valid)
        sched, vec = enable_vec()
        vec["v(vout)"] = vec["v(vout)"] + 0.05 * (vec["time"] > sched["events"][0][2])
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(m.events[0].censored)

    def test_bias_cycle_events_are_independent(self):
        # enable acquires, re-enable never does
        sched, vec = enable_vec(("bias-cycle", "fast"))
        tc2 = sched["events"][1][2]
        vec["v(vout)"] = np.where(vec["time"] >= tc2, 0.3, vec["v(vout)"])
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(m.valid, m.reason)
        self.assertFalse(m.events[0].censored)
        self.assertTrue(m.events[1].censored)
        self.assertEqual(m.acq, math.inf)

    def test_both_bias_cycle_events_acquire_with_known_times(self):
        _, m = run(("bias-cycle", "fast"))
        self.assertTrue(m.valid, m.reason)
        want = TAU * math.log(0.2 / (0.01 * VCM))
        for e in m.events:
            self.assertAlmostEqual(e.acq_s, want, delta=1e-9)


class InvalidTests(unittest.TestCase):
    def test_invalid_reference_is_invalid(self):
        sched, vec = enable_vec()
        for ref in (float("nan"), float("inf"), VCM + 0.05, 0.0):
            m = r.extract_enable(vec, sched, VDD, ref)
            self.assertFalse(m.valid, ref)
            self.assertIn("reference", m.reason)

    def test_powered_start_is_invalid(self):
        sched, vec = enable_vec(powered_start=True)
        vec["v(vdd)"] = np.full_like(vec["time"], VDD)
        vec["v(vout)"] = np.full_like(vec["time"], VCM)
        vec["v(vinp)"] = np.full_like(vec["time"], VCM)
        vec["v(ibias)"] = np.full_like(vec["time"], 0.9)
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertFalse(m.valid)
        self.assertIn("initial state", m.reason)

    def test_each_initial_quantity_is_checked(self):
        for name in ("v(vdd)", "v(vinp)", "v(vout)", "v(ibias)", "v(rs)", "v(rb)"):
            sched, vec = enable_vec()
            vec[name] = vec[name].copy()
            vec[name][0] = 1.0
            self.assertFalse(r.extract_enable(vec, sched, VDD, VCM).valid, name)

    def test_unreached_rail_or_bias_is_invalid(self):
        sched, vec = enable_vec(rail_scale=0.9)
        self.assertIn("rail", r.extract_enable(vec, sched, VDD, VCM).reason)
        sched, vec = enable_vec()
        vec["v(rb)"] = np.where(vec["time"] >= sched["params"]["t_ib"], 0.5, 0.0)
        self.assertIn("bias envelope", r.extract_enable(vec, sched, VDD, VCM).reason)

    def test_bias_not_removed_in_the_cycle_is_invalid(self):
        sched, vec = enable_vec(("bias-cycle", "fast"))
        vec["v(rb)"] = np.where(vec["time"] >= sched["params"]["t_ib"], 1.0, 0.0)
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertFalse(m.valid)
        self.assertIn("removed", m.reason)

    def test_missing_samples_in_a_window_are_invalid(self):
        sched, vec = enable_vec()
        t = vec["time"]
        keep = ~((t > 2.0e-6) & (t < 3.5e-6))  # a 1.5 us hole inside the window
        thin = {k: v[keep] for k, v in vec.items()}
        m = r.extract_enable(thin, sched, VDD, VCM)
        self.assertFalse(m.valid)
        self.assertIn("missing samples", m.reason)

    def test_missing_vectors_truncated_and_nonmonotonic_runs_are_invalid(self):
        sched, vec = enable_vec()
        for need in r.REQUIRED:
            bad = {k: v for k, v in vec.items() if k != need}
            self.assertFalse(r.extract_enable(bad, sched, VDD, VCM).valid, need)
        short = {k: v[: v.size // 2] for k, v in vec.items()}
        self.assertFalse(r.extract_enable(short, sched, VDD, VCM).valid)
        t = vec["time"].copy()
        t[10] = t[9]
        self.assertFalse(r.extract_enable(dict(vec, time=t), sched, VDD, VCM).valid)
        tiny = {k: v[:50] for k, v in vec.items()}
        self.assertFalse(r.extract_enable(tiny, sched, VDD, VCM).valid)


class ScheduleTests(unittest.TestCase):
    def test_six_cases_with_fast_slow_pair(self):
        self.assertEqual(len(r.mc.cases()), 6)
        self.assertEqual({rp for _, rp in r.mc.cases()}, {"fast", "slow"})
        self.assertEqual(r.mc.SCENARIOS, ("simultaneous", "supply-first", "bias-cycle"))
        fast, slow = (next(x for x in r.mc.RAMPS if x[0] == n) for n in ("fast", "slow"))
        self.assertGreaterEqual(slow[1] / fast[1], 10)

    def test_pwl_times_increase_and_events_follow_the_ramps(self):
        for case in r.mc.cases():
            s = r.mc.schedule(*case)
            p = s["params"]
            self.assertLess(p["t_vdd"], p["t_vdd"] + p["tr_s"])
            self.assertLess(p["t_ib"] + p["tr_b"], p["t_off"])
            self.assertLess(p["t_off"] + p["tr_b"], p["t_on2"])
            for name, t0, tc, t1 in s["events"]:
                self.assertLess(t0, tc)
                self.assertLessEqual(tc + r.REF_WINDOW * 5, t1)
                self.assertLessEqual(t1, s["tstop"] + 1e-15)
            self.assertAlmostEqual(s["events"][-1][3], s["tstop"], places=11)

    def test_scenarios_differ_as_described(self):
        sim = r.mc.schedule("simultaneous", "fast")["params"]
        sf = r.mc.schedule("supply-first", "fast")["params"]
        bc = r.mc.schedule("bias-cycle", "fast")
        self.assertEqual(sim["t_vdd"], sim["t_ib"])
        self.assertGreater(sf["t_ib"], sf["t_vdd"] + sf["tr_s"])
        self.assertLess(bc["params"]["t_off"], bc["tstop"])
        self.assertEqual([e[0] for e in bc["events"]], ["enable", "re-enable"])
        self.assertEqual(len(r.mc.schedule("simultaneous", "fast")["events"]), 1)

    def test_long_window_is_longer(self):
        a = r.mc.schedule("bias-cycle", "slow")
        b = r.mc.schedule("bias-cycle", "slow", r.mc.WINDOW_T * r.mc.LONG_WINDOW_FACTOR)
        self.assertGreater(b["tstop"], a["tstop"] + 9 * r.mc.WINDOW_T)

    def test_window_spans_many_time_constants(self):
        tau_max = 1 / (2 * math.pi * r.GBW_MIN_HZ)
        self.assertGreaterEqual(r.mc.WINDOW_T / tau_max, 200)
        for _, _, _, tstep in r.mc.RAMPS:
            self.assertLessEqual(tstep, tau_max / 1.5)


class SummaryTests(unittest.TestCase):
    def test_worst_points_and_censored_rank_worst(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(len(set(keys)), 45)
        _, base = run()
        sched, vec = enable_vec()
        stuck(sched, vec, 0.5)
        stuck_m = r.extract_enable(vec, sched, VDD, VCM)
        _, slow = run(h=lambda s: np.exp(-np.clip(s, 0, None) / (3 * TAU)))
        res = {k: base for k in keys}
        res[keys[3]] = slow
        res[keys[4]] = stuck_m
        res[keys[5]] = r.Metrics(valid=False, reason="x")
        s = r.summarise(res)
        self.assertEqual(s.worst["enable"], (math.inf, keys[4]))
        self.assertEqual(s.censored["enable"], [keys[4]])
        self.assertEqual(s.n_invalid, 1)
        res.pop(keys[4])
        self.assertEqual(r.summarise(res).worst["enable"][1], keys[3])


class ControlTests(unittest.TestCase):
    def test_control_logic(self):
        _, nm = run()
        _, bg = run(h=lambda s: np.exp(-np.clip(s, 0, None) / (4 * TAU)))
        sched, vec = enable_vec()
        stuck(sched, vec, 0.0)
        dead = r.extract_enable(vec, sched, VDD, VCM)
        _, pw = run(powered_start=True)
        mk = lambda n, m, e="": r.ControlRun(n, "", m, e)  # noqa: E731
        ok = [mk("nominal", nm), mk("cl-x5", bg), mk("ibias-zero", dead), mk("powered-start", pw)]
        self.assertFalse(pw.valid)
        self.assertEqual(r.control_failures(ok), [])
        # the measurement must respond to the changed load
        self.assertTrue(r.control_failures([mk("nominal", nm), mk("cl-x5", nm), mk("ibias-zero", dead), mk("powered-start", pw)]))
        # a dead bias must not be accepted as acquired
        self.assertTrue(r.control_failures([mk("nominal", nm), mk("cl-x5", bg), mk("ibias-zero", nm), mk("powered-start", pw)]))
        # ... nor pass for the wrong reason: an invalid dead-bias run is not a passing control
        bad_dead = r.extract_enable(*reversed(enable_vec(powered_start=True)), VDD, VCM)
        self.assertFalse(bad_dead.valid)
        fails = r.control_failures([mk("nominal", nm), mk("cl-x5", bg), mk("ibias-zero", bad_dead), mk("powered-start", pw)])
        self.assertTrue(any(f.startswith("ibias-zero: invalid") for f in fails), fails)
        # a powered start must not be accepted as valid
        _, okpw = run()
        self.assertTrue(r.control_failures([mk("nominal", nm), mk("cl-x5", bg), mk("ibias-zero", dead), mk("powered-start", okpw)]))
        self.assertTrue(r.control_failures([mk("nominal", None, "boom"), mk("cl-x5", bg), mk("ibias-zero", dead), mk("powered-start", pw)]))


class DataTests(unittest.TestCase):
    def test_committed_data_rederive_the_same_figures(self):
        with tempfile.TemporaryDirectory() as d:
            for case in (("simultaneous", "fast"), ("bias-cycle", "slow")):
                sched, vec = enable_vec(case)
                m = r.extract_enable(vec, sched, VDD, VCM)
                p = Path(d) / "pt.dat.gz"
                r.save_point_data(m, p)
                m2 = r.rederive(p, sched, VDD, VCM)
                self.assertTrue(r.same_result(m, m2), case)
            other = run(h=lambda s: np.exp(-np.clip(s, 0, None) / (2 * TAU)))[1]
            self.assertFalse(r.same_result(m2, other) and r.same_result(run()[1], other))

    def test_ramp_completion_sample_is_post_ramp_despite_float_rounding(self):
        """The sample at the ramp-completion instant counts as post-ramp whether its time is read
        as float64 or re-read from the 10-digit committed text (it can sit ~1e-15 s below tc)."""
        sched, vec = enable_vec()
        tc = sched["events"][0][2]
        i = int(np.argmin(np.abs(vec["time"] - tc)))
        vec["v(vout)"] = vec["v(vout)"].copy()
        vec["v(vout)"][i] = VCM + 0.3
        vec["time"] = vec["time"].copy()
        vec["time"][i] = tc - 1e-15
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(m.valid, m.reason)
        self.assertGreater(m.events[0].over_mv, 299.0)

    def test_internal_node_traces_are_retained(self):
        sched, vec = enable_vec()
        m = r.extract_enable(vec, sched, VDD, VCM)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pt.dat.gz"
            r.save_point_data(m, p)
            import gzip
            head = gzip.open(p, "rt").read().splitlines()
            self.assertTrue(any(ln.lstrip("# ").startswith("time_s vdd_v rs rb ibias_v vinp_v vout_v n1_v n2_v nz_v tail_v") for ln in head))

    def test_censored_and_invalid_points_round_trip(self):
        sched, vec = enable_vec()
        stuck(sched, vec, 0.5)
        m = r.extract_enable(vec, sched, VDD, VCM)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pt.dat.gz"
            r.save_point_data(m, p)
            m2 = r.rederive(p, sched, VDD, VCM)
            self.assertTrue(m2.censored and r.same_result(m, m2))
            sched, vec = enable_vec(powered_start=True)
            vec["v(vdd)"] = np.full_like(vec["time"], VDD)
            bad = r.extract_enable(vec, sched, VDD, VCM)
            self.assertFalse(bad.valid)
            r.save_point_data(bad, p)
            import gzip
            self.assertIn("INVALID: ", gzip.open(p, "rt").read())
            self.assertFalse(r.rederive(p, sched, VDD, VCM).valid)

    def test_file_without_header_cannot_rederive(self):
        import gzip
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pt.dat.gz"
            with gzip.open(p, "wt") as fh:
                np.savetxt(fh, np.zeros((5, 3)), header="t v")
            with self.assertRaises(ValueError):
                r.rederive(p, r.mc.schedule("simultaneous", "fast"), VDD, VCM)


def _raw(vec) -> str:
    names = [n for n in r._RAW_ORDER if n in vec]
    n = vec["time"].size
    rows = []
    for i in range(n):
        rows.append(f" {i}\t{vec['time'][i]:.12e}")
        for nm in names[1:]:
            rows.append(f"\t{vec[nm][i]:.12e}")
    head = (f"Title: t\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: {len(names)}\nNo. Points: {n}\nVariables:\n"
            + "".join(f"\t{i}\t{nm}\tx\n" for i, nm in enumerate(names)) + "Values:\n")
    return head + "\n".join(rows) + "\n"


class RailExcessTests(unittest.TestCase):
    def test_rail_excess_is_clamped_at_zero(self):
        _, m = run()
        for e in m.events:
            self.assertGreaterEqual(e.rail_excess_mv, 0.0)
        tc = r.mc.schedule("simultaneous", "fast")["events"][0][2]
        # a brief 2 V spike just after completion lifts vout above the 3.3 V rail: a real, positive excess
        _, hot = run(extra=lambda t: np.where(np.abs(t - tc - 20e-9) < 6e-9, 2.0, 0.0))
        self.assertTrue(hot.valid, hot.reason)
        self.assertGreater(hot.events[0].rail_excess_mv, 100.0)


class ReportTests(unittest.TestCase):
    def _corner(self, raw_path, meas):
        return {"process": "typical", "temperature_c": 27.0, "supply_v": {"vdd": 3.3, "vcm": 1.65},
                "diagnostics": [], "measurements": [{"name": k, "value": v} for k, v in meas.items()],
                "artifacts": {"raw": str(raw_path)}}

    def test_missing_or_failed_points_are_problems(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        sched = r.mc.schedule("simultaneous", "fast")
        res, _, problems = r.analyse_report({"corners": []}, keys, sched, {})
        self.assertEqual((len(problems), res), (45, {}))
        _, _, problems = r.analyse_report({"corners": [self._corner("/nonexistent/w.raw", {})]}, [r.NOMINAL], sched, {})
        self.assertTrue(any("simulation failed" in p for p in problems))
        res, _, problems = r.analyse_report({"corners": []}, [], sched, {})
        refs, _, rp = r.analyse_reference({"corners": []}, keys)
        self.assertEqual((refs, len(rp)), ({}, 45))

    def test_missing_reference_makes_the_point_invalid(self):
        sched, vec = enable_vec()
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "w.raw"
            raw.write_text(_raw(vec))
            res, _, _ = r.analyse_report({"corners": [self._corner(raw, {})]}, [r.NOMINAL], sched, {})
            self.assertFalse(res[r.NOMINAL].valid)
            self.assertIn("reference", res[r.NOMINAL].reason)

    def test_synthetic_report_and_crosscheck(self):
        sched, vec = enable_vec()
        t = vec["time"]
        good = {"vfinal_v": float(np.interp(sched["tstop"] - r.SAMPLE_BEFORE, t, vec["v(vout)"])),
                "vdd_final_v": float(np.interp(sched["tstop"] - r.SAMPLE_BEFORE, t, vec["v(vdd)"])),
                "vout_max_v": float(vec["v(vout)"].max())}
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "w.raw"
            raw.write_text(_raw(vec))
            res, _, problems = r.analyse_report({"corners": [self._corner(raw, good)]}, [r.NOMINAL], sched, {r.NOMINAL: VCM})
            self.assertEqual(problems, [])
            self.assertTrue(res[r.NOMINAL].valid, res[r.NOMINAL].reason)
            bad = dict(good, vfinal_v=good["vfinal_v"] + 50e-3)
            _, _, problems = r.analyse_report({"corners": [self._corner(raw, bad)]}, [r.NOMINAL], sched, {r.NOMINAL: VCM})
            self.assertTrue(any("vfinal_v" in p for p in problems))

    def test_reference_report(self):
        c = {"process": "typical", "temperature_c": 27.0, "supply_v": {"vdd": 3.3, "vcm": 1.65}, "diagnostics": [],
             "measurements": [{"name": "vref_v", "value": 1.6503}], "artifacts": {}}
        refs, _, problems = r.analyse_reference({"corners": [c]}, [r.NOMINAL])
        self.assertEqual((refs, problems), ({r.NOMINAL: 1.6503}, []))
        c["measurements"] = []
        refs, _, problems = r.analyse_reference({"corners": [c]}, [r.NOMINAL])
        self.assertTrue(problems and not refs)


class LongWindowPersistenceTests(unittest.TestCase):
    """A long-window follow-up must leave the same committed evidence trail as
    a primary case -- schedule/request in the netlist snapshot, per-point log,
    deck and `.dat.gz` trace, sanitised klt report -- and the committed traces
    must re-extract to the recorded figures (review feedback, PR #193)."""

    KEY = r.NOMINAL

    def _censored_primary(self):
        sched, vec = enable_vec(("supply-first", "slow"))
        stuck(sched, vec, 0.5)
        m = r.extract_enable(vec, sched, VDD, VCM)
        self.assertTrue(any(e.censored for e in m.events))
        return sched, m

    def _long_followup(self):
        saved = r.mc.WINDOW_T
        r.mc.WINDOW_T = saved * r.mc.LONG_WINDOW_FACTOR
        try:
            lsched, lvec = enable_vec(("supply-first", "slow"))
            stuck(lsched, lvec, 0.5)
            lm = r.extract_enable(lvec, lsched, VDD, VCM)
        finally:
            r.mc.WINDOW_T = saved
        self.assertGreater(lsched["tstop"], r.mc.schedule("supply-first", "slow")["tstop"])
        return lsched, lm

    @staticmethod
    def _arts(d: Path):
        log, deck = d / "klt.log", d / "klt.cir"
        log.write_text("log text\n")
        deck.write_text("* deck\n")
        return {LongWindowPersistenceTests.KEY: {"log": log, "deck": deck, "klt_meas": {}}}

    def test_censored_primary_and_followup_keep_evidence_and_rederive(self):
        """End to end through `analyse_report`: a slow-settling point is
        censored in the primary window, the synthetic long-window klt report
        resolves it as acquired late, and both cases' evidence is written and
        re-extracts from the committed traces."""
        slow = lambda s: np.exp(-np.clip(s, 0, None) / 1.5e-6)  # noqa: E731
        case = ("supply-first", "slow")
        sched, vec = enable_vec(case, h=slow)
        saved = r.mc.WINDOW_T
        r.mc.WINDOW_T = saved * r.mc.LONG_WINDOW_FACTOR
        try:
            lsched, lvec = enable_vec(case, h=slow)
        finally:
            r.mc.WINDOW_T = saved
        self.assertEqual(lsched["params"], r.mc.schedule(*case, saved * r.mc.LONG_WINDOW_FACTOR)["params"])
        refs = {self.KEY: VCM}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            reports = {}
            for tag, v in (("primary", vec), ("long", lvec)):
                raw, log, deck = d / f"{tag}.raw", d / f"{tag}.log", d / f"{tag}.cir"
                raw.write_text(_raw(v))
                log.write_text(f"{tag} log\n")
                deck.write_text(f"* {tag} deck\n")
                reports[tag] = {"corners": [{
                    "process": "typical", "temperature_c": 27.0, "supply_v": {"vdd": 3.3, "vcm": 1.65},
                    "diagnostics": [], "measurements": [],
                    "artifacts": {"raw": str(raw), "log": str(log), "deck": str(deck)}}]}
            res, arts, problems = r.analyse_report(reports["primary"], [self.KEY], sched, refs)
            self.assertEqual(problems, [])
            self.assertTrue(res[self.KEY].valid and res[self.KEY].censored, res[self.KEY].reason)
            unresolved = [k for k, m in res.items() if m.censored]
            lres, larts, problems = r.analyse_report(reports["long"], [self.KEY], lsched, refs)
            self.assertEqual(problems, [])
            lres = {k: lres[k] for k in unresolved}
            larts = {k: larts[k] for k in unresolved}
            self.assertTrue(lres[self.KEY].valid and not lres[self.KEY].censored, lres[self.KEY].reason)

            primary, follow = d / "supply-first_slow", d / "long_supply-first_slow"
            primary.mkdir()
            follow.mkdir()
            r.persist_points(primary, res, arts, reports["primary"])
            r.persist_points(follow, lres, larts, reports["long"])
            stem = r.g.point_stem(self.KEY)
            for sub, tag in ((primary, "primary"), (follow, "long")):
                self.assertEqual((sub / f"{stem}.log").read_text(), f"{tag} log\n")
                self.assertEqual((sub / f"{stem}.cir").read_text(), f"* {tag} deck\n")
                self.assertTrue((sub / f"{stem}.dat.gz").is_file(), sub)
                rep = json.loads((sub / "klt-report.json").read_text())
                self.assertEqual(len(rep["corners"]), 1)
                self.assertNotIn(str(d), json.dumps(rep))  # sanitised: no scratch paths
            rbad = []
            n_ok = n_all = 0
            for sub, s, rr, label in ((primary, sched, res, "supply-first/slow"),
                                      (follow, lsched, lres, "long supply-first/slow")):
                a, b = r.check_rederive(sub, s, rr, refs, label, rbad)
                n_ok += a
                n_all += b
            self.assertEqual((n_ok, n_all, rbad), (2, 2, []))
            # the re-derived follow-up is the late acquisition, not the primary's censored result
            m2 = r.rederive(follow / f"{stem}.dat.gz", lsched, self.KEY[2], VCM)
            self.assertFalse(m2.censored)

    def test_lost_trace_is_flagged_not_silently_accepted(self):
        sched, m = self._censored_primary()
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sub = d / "supply-first_slow"
            sub.mkdir()
            r.persist_points(sub, {self.KEY: m}, self._arts(d), {"corners": []})
            (sub / f"{r.g.point_stem(self.KEY)}.dat.gz").unlink()
            rbad = []
            n_ok, n_all = r.check_rederive(sub, sched, {self.KEY: m}, {self.KEY: VCM}, "supply-first/slow", rbad)
            self.assertEqual((n_ok, n_all), (0, 1))
            self.assertEqual(len(rbad), 1)
            self.assertIn("supply-first/slow", rbad[0])

    def test_snapshot_records_the_followup_request(self):
        lsched, _ = self._long_followup()
        req = {"netlist": "/scratch/secret/tb.spice", "corners": {"process": ["typical"]},
               "analysis": {"kind": "tran", "args": "5n 50u"}}
        lines = r.snapshot_case_lines("long-window supply-first/slow", req, lsched)
        self.assertEqual(lines[0], "* ---- case long-window supply-first/slow ----")
        joined = "\n".join(lines)
        self.assertNotIn("/scratch", joined)  # netlist path elided, as for the primaries
        self.assertIn('"analysis"', joined)
        self.assertIn(json.dumps(lsched["params"]), joined)


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
        self.assertIn("Bibias vdd ibias I = 10u*v(rb)", t)  # final bias 10 uA
        self.assertIn("CL vout 0 2p", t)  # CL = 2 pF [DR-1]
        self.assertIn("Xdut vdd 0 vinp vout vout ibias opamp_two_stage", t)  # unity-gain follower
        self.assertIn("Vdd vddset 0 dc 3.3", t)
        self.assertIn("Vcm vcmset 0 dc 1.65", t)
        self.assertIn("Bvdd vdd 0 V = v(vddset)*v(rs)", t)
        self.assertIn("Bvin vinp 0 V = v(vcmset)*v(rs)", t)  # VCM tracks the ramped rail
        self.assertIn("Vrs rs 0 pwl(0 0 ", t)  # sources start off
        self.assertIn("Vrb rb 0 pwl(0 0 ", t)
        self.assertNotRegex(t, r"(?m)^\.tran.*uic")
        self.assertEqual(r.IBIAS_A if hasattr(r, "IBIAS_A") else r.mc.IBIAS_A, 10e-6)

    def test_committed_param_defaults_are_the_simultaneous_fast_schedule(self):
        sched = r.mc.schedule("simultaneous", "fast")
        for name in r._PARAM_NAMES:
            m = re.search(rf"(?m)^\.param\s+{name}=(\S+)\s*$", self.TEXT)
            self.assertIsNotNone(m, name)
            sec = {"p": 1e-12, "n": 1e-9, "u": 1e-6}
            v = m.group(1)
            val = float(v[:-1]) * sec[v[-1]] if v[-1] in sec else float(v)
            self.assertAlmostEqual(val, sched["params"][name], places=15, msg=name)

    def test_schedule_substitution_is_exactly_one_match_per_param(self):
        for case in r.mc.cases():
            s = r.mc.schedule(*case)
            tb = r.apply_schedule(self.TEXT, s)
            for name in r._PARAM_NAMES:
                self.assertEqual(len(re.findall(rf"(?m)^\.param\s+{name}=", tb)), 1)
            self.assertEqual(r.g.guard_testbench(tb), [])
        with self.assertRaises(RuntimeError):
            r.apply_schedule(self.TEXT.replace(".param t_off=1\n", ""), r.mc.schedule("bias-cycle", "fast"))
        with self.assertRaises(RuntimeError):
            r.apply_schedule(self.TEXT + "\n.param tr_s=5n\n", r.mc.schedule("bias-cycle", "fast"))

    def test_vcm_is_half_the_final_supply_at_every_point(self):
        req = r.make_request(Path("/x/tb.spice"), r.Pdk(Path("/x"), "gf180mcuD", "t"), r.mc.schedule("simultaneous", "fast"),
                             r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(req["corners"]["supply_v"]["vcm"], [round(v / 2, 6) for v in r.SUPPLIES_V])


def fake_pdk(d: Path) -> r.Pdk:
    ng = d / "pdk" / "libs.tech" / "ngspice"
    ng.mkdir(parents=True)
    (ng / "design.ngspice").write_text("* stub\n")
    return r.Pdk(d / "pdk", "gf180mcuD", "test")


class StaleDutTests(unittest.TestCase):
    """The bench must measure the committed export: an altered, stale or
    hand-edited DUT is rejected before anything is written or simulated."""

    SCHED = r.mc.schedule("simultaneous", "fast")

    def test_committed_dut_materialises_and_only_includes_and_params_change(self):
        with tempfile.TemporaryDirectory() as d:
            pdk = fake_pdk(Path(d))
            tb = r.materialise(Path(d) / "w", pdk, self.SCHED)
            got, want = tb.read_text().splitlines(), r.TESTBENCH.read_text().splitlines()
            diff = [(a, b) for a, b in zip(want, got) if a != b]
            self.assertEqual([a for a, _ in diff if not a.startswith(".param")].__len__(), 2)
            self.assertEqual(len(diff), 6)  # 2 includes + 4 schedule .param lines whose literals are re-formatted
            self.assertTrue(all(a.startswith((".include", ".param")) for a, _ in diff))
            self.assertEqual((Path(d) / "w" / r.g.DUT_INCLUDE_NAME).read_text(), r.load_dut_text())
            tb2 = r.materialise(Path(d) / "w2", pdk, self.SCHED, cl_f=20e-12, ibias_a=0.0)
            self.assertIn("CL vout 0 2e-11", tb2.read_text())
            self.assertIn("Bibias vdd ibias I = 0*v(rb)", tb2.read_text())
            tb3 = r.materialise(Path(d) / "w3", pdk, self.SCHED, powered_start=True)
            self.assertIn("Vrs rs 0 dc 1", tb3.read_text())
            self.assertIn("Vrb rb 0 dc 1", tb3.read_text())
            tb4 = r.materialise(Path(d) / "w4", pdk, r.mc.schedule("bias-cycle", "slow"))
            self.assertIn(".param tr_s=5e-06", tb4.read_text())

    def test_altered_device_in_the_dut_is_rejected(self):
        dut = r.load_dut_text()
        mm = re.search(r"\bW=(\S+)", dut)
        self.assertIsNotNone(mm)
        stale = dut[: mm.start()] + "W=" + mm.group(1) + "0" + dut[mm.end():]
        self.assertTrue(r.g.guard_dut(stale))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                r.materialise(Path(d) / "w", fake_pdk(Path(d)), self.SCHED, dut_text=stale)
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
        for n in ("SAMPLE_BEFORE", "REF_WINDOW", "BAND_FRAC", "DRIFT_FRAC", "MIN_DWELL_FRAC", "INIT_MAX_V",
                  "INIT_MAX_RAIL_FRAC", "FINAL_RAIL_TOL_V", "REF_LEVEL_TOL_V", "GBW_MIN_HZ"):
            self.assertEqual(getattr(r, n), getattr(r.mc, n), n)
        self.assertEqual(r.BAND_FRAC, 0.01)
        self.assertIs(r.CORNERS, r.mc.CORNERS)
        self.assertTrue(r.mc.TESTBENCH_REL.endswith(r.TESTBENCH.name))
        keys = r.mc.inputs(r.TESTBENCH.read_text())["params"]
        for n in ("band_frac", "drift_frac", "min_dwell_frac", "ref_window", "init_max_v", "window_t"):
            self.assertIn(n, keys)

    def test_request_is_what_the_fingerprint_hashes(self):
        inp = r.mc.inputs(r.TESTBENCH.read_text())
        for scenario, ramp in r.mc.cases():
            sched = r.mc.schedule(scenario, ramp)
            req = r.make_request(Path("/x/tb.spice"), self.PDK, sched, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
            dump = inp["analysis"]["per_schedule"][f"{scenario}/{ramp}"]
            self.assertEqual(req["analysis"], {"kind": inp["analysis"]["kind"], "args": f"{dump['tstep']:g} {dump['tstop']:g}"})
            self.assertEqual(req["measurements"], inp["analysis"]["measurements"][f"{scenario}/{ramp}"])
            self.assertEqual(dump["params"], sched["params"])
            self.assertEqual(len(req["corners"]["process"]) * len(req["corners"]["temperature_c"])
                             * len(req["corners"]["supply_v"]["vdd"]), 45)

    def test_fingerprint_changes_with_the_bench_and_the_schedule(self):
        a = r.mc.fingerprint(r.TESTBENCH.read_text())
        b = r.mc.fingerprint(r.TESTBENCH.read_text().replace("CL vout 0 2p", "CL vout 0 3p"))
        self.assertNotEqual(a, b)
        old = r.mc.GAP_T
        try:
            r.mc.GAP_T = old * 2
            c = r.mc.fingerprint(r.TESTBENCH.read_text())
        finally:
            r.mc.GAP_T = old
        self.assertNotEqual(a, c)


class RecordTests(unittest.TestCase):
    def _record(self, censor: bool, ctrls=None, censor_cases=(("supply-first", "slow"),)):
        from datetime import datetime, timezone

        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        case_results, long_results = {}, {}
        for case in r.mc.cases():
            sched, vec = enable_vec(case)
            base = r.extract_enable(vec, sched, VDD, VCM)
            res = {k: base for k in keys}
            if censor and case in censor_cases:
                s2, v2 = enable_vec(case)
                stuck(s2, v2, 0.5)
                res[keys[6]] = r.extract_enable(v2, s2, VDD, VCM)
                long_results[case] = (s2, {keys[6]: res[keys[6]]})
            case_results[case] = (sched, res)
        refs = {k: k[2] / 2 for k in keys}
        return r.build_record(
            record="rid", stamp=datetime(2026, 1, 1, tzinfo=timezone.utc), pdk=r.Pdk(Path("/x"), "gf180mcuD", "test"),
            ngspice="ng", klt_ver="k", backend_descs={"x": "local"}, case_results=case_results, long_results=long_results,
            refs=refs, ctrls=ctrls or [], ctrl_bad=[], plots=[], dut_sha="0" * 64, rederived=(270, 270, []), ref_remote={})

    def test_record_distinguishes_observations_from_guarantees_and_judges_nothing(self):
        md = self._record(False)
        self.assertIn("no pass or fail verdict, no startup bound", md)
        self.assertIn("supply / external-bias enable acquisition study -- record rid", md)
        self.assertIn("not a consumer startup guarantee", md)
        self.assertIn("diagnostic experiment conditions", md)
        self.assertIn("rail ramp (start, duration)", md)
        self.assertIn("no recovery is inferred from a single tolerance crossing", md)
        self.assertIn("No event was censored", md)
        for case in r.mc.cases():
            self.assertIn(f"{case[0]} / {case[1]}", md)
        self.assertEqual(sum(1 for ln in md.splitlines() if ln.startswith("| `")), 6 * 45)
        for word in ("PASS", "FAIL"):
            self.assertNotIn(f"**{word}**", md)

    def test_censored_point_is_named_and_followed_up(self):
        md = self._record(True)
        self.assertIn("NOT ACQUIRED at 1 point(s)", md)
        self.assertIn("not acquired", md)
        self.assertIn("Long-window follow-up", md)
        self.assertIn("still not acquired after", md)

    def test_followup_directories_follow_each_ramp(self):
        cases = (("supply-first", "fast"), ("supply-first", "slow"))
        self.assertTrue(all(c in r.mc.cases() for c in cases))
        md = self._record(True, censor_cases=cases)
        evidence = next(ln for ln in md.splitlines() if ln.startswith("Committed follow-up evidence"))
        for scenario, ramp in cases:
            self.assertIn(f"`sim/enable-acquisition/corners/rid/long_{scenario}_{ramp}/`", evidence)
        self.assertEqual(evidence.count("/long_"), 2)


class AppendOnlyTests(unittest.TestCase):
    def test_existing_record_paths_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            r.claim_record_paths(base, "rid-1")
            (base / "records").mkdir()
            (base / "records" / "rid-1.md").write_text("x")
            with self.assertRaises(FileExistsError):
                r.claim_record_paths(base, "rid-1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
