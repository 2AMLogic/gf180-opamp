#!/usr/bin/env python3
"""Regression tests for the follower step-response experiment (issue #113).

No simulator is needed: the extraction is checked against synthetic
waveforms with analytically known answers (first-order monotonic, underdamped
second-order ringing, non-settling, preshoot, invalid followers), and the
source guards are checked by mutating the committed testbench / DUT text in
memory (a stale or altered DUT must be rejected before anything runs).

    python3 sim/step-response/test_step_response.py      # or: python3 -m unittest
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

import run_step_response as r  # noqa: E402

VCM = 1.65
DT = 1e-9


# ---------------------------------------------------------------- fixtures


def first_order(tau):
    return lambda s: np.where(s > 0, 1 - np.exp(-np.clip(s, 0, None) / tau), 0.0)


def second_order(zeta, wn):
    wd = wn * math.sqrt(1 - zeta ** 2)
    phi = math.acos(zeta)

    def h(s):
        s = np.clip(s, 0, None)
        out = 1 - np.exp(-zeta * wn * s) / math.sqrt(1 - zeta ** 2) * np.sin(wd * s + phi)
        return np.where(s > 0, out, 0.0)

    return h


def follower(h, *, step=r.STEP_V, vcm=VCM, dt=DT, extra=None, offset=0.0):
    """Linear follower: vout = (vcm - step/2) + step * (h(t - RISE_T) - h(t - FALL_T)) + offset.
    `extra(t)` adds an arbitrary disturbance (e.g. a sustained ring)."""
    t = np.arange(0, r.TSTOP + dt / 2, dt)
    lo = vcm - r.STEP_V / 2
    vin = np.where((t >= r.RISE_T) & (t < r.FALL_T), vcm + r.STEP_V / 2, lo)
    vo = lo + offset + step * (h(t - r.RISE_T) - h(t - r.FALL_T))
    if extra is not None:
        vo = vo + extra(t)
    return {"time": t, "v(vout)": vo, "v(vinp)": vin}


def brute_settling(h, band, horizon=2e-6, dt=1e-12):
    """Independent reference: last time |h - 1| > band on a 1 ps grid."""
    s = np.arange(dt, horizon, dt)
    out = np.nonzero(np.abs(h(s) - 1) > band)[0]
    return float(s[out[-1]]) if out.size else 0.0


# ------------------------------------------------------------------ tests


class MonotonicTests(unittest.TestCase):
    def test_first_order_is_monotonic_with_known_settling(self):
        tau = 15e-9
        m = r.extract_step(follower(first_order(tau)))
        self.assertTrue(m.valid, m.reason)
        for e in m.edges():
            self.assertTrue(e.monotonic, e.name)
            self.assertAlmostEqual(e.overshoot_pct, 0.0, places=6)
            self.assertAlmostEqual(e.preshoot_pct, 0.0, places=6)
            self.assertAlmostEqual(e.ts[0.01], tau * math.log(100), delta=0.3e-9)
            self.assertAlmostEqual(e.ts[0.001], tau * math.log(1000), delta=0.3e-9)
            self.assertAlmostEqual(e.t10_90, tau * math.log(9), delta=0.3e-9)
            self.assertAlmostEqual(e.step_v, (1 if e.name == "rise" else -1) * r.STEP_V, delta=1e-6)
        self.assertTrue(m.monotonic)
        self.assertAlmostEqual(m.overshoot_pct, 0.0, places=6)

    def test_reversal_below_tolerance_is_still_monotonic(self):
        # a 0.05 % dip after the response has reached ~1 (below MONO_TOL = 0.1 %)
        h1 = first_order(10e-9)
        dip = lambda t: -0.0005 * r.STEP_V * np.exp(-(((t - r.RISE_T - 150e-9) / 5e-9) ** 2))  # noqa: E731
        m = r.extract_step(follower(h1, extra=dip))
        self.assertTrue(m.valid, m.reason)
        self.assertTrue(m.rise.monotonic)
        dip2 = lambda t: -0.005 * r.STEP_V * np.exp(-(((t - r.RISE_T - 150e-9) / 5e-9) ** 2))  # noqa: E731
        m2 = r.extract_step(follower(h1, extra=dip2))
        self.assertFalse(m2.rise.monotonic)
        self.assertFalse(m2.monotonic)

    def test_preshoot_is_non_monotonic_without_overshoot(self):
        # wrong-way start (RHP-zero-like): -3 % for the first few ns, then a monotonic first-order rise
        h1 = first_order(10e-9)
        h = lambda s: h1(s - 5e-9) - 0.03 * np.where(s > 0, np.exp(-(((s - 2.5e-9) / 1e-9) ** 2)), 0)  # noqa: E731
        m = r.extract_step(follower(h, dt=0.25e-9))
        self.assertTrue(m.valid, m.reason)
        self.assertGreater(m.rise.preshoot_pct, 0.5)
        self.assertAlmostEqual(m.rise.overshoot_pct, 0.0, places=6)
        self.assertFalse(m.rise.monotonic)


class RingingTests(unittest.TestCase):
    def test_second_order_overshoot_and_settling(self):
        for zeta in (0.3, 0.5, 0.6):
            with self.subTest(zeta=zeta):
                wn = 2 * math.pi * 12e6
                h = second_order(zeta, wn)
                m = r.extract_step(follower(h))
                self.assertTrue(m.valid, m.reason)
                expect = 100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta ** 2))
                for e in m.edges():
                    self.assertAlmostEqual(e.overshoot_pct, expect, delta=0.05 * expect, msg=e.name)
                    self.assertFalse(e.monotonic)
                    for b in r.SETTLE_BANDS:
                        self.assertAlmostEqual(e.ts[b], brute_settling(h, b), delta=1.0e-9, msg=(e.name, b))
                self.assertAlmostEqual(m.overshoot_pct, expect, delta=0.05 * expect)
                self.assertFalse(m.monotonic)

    def test_worse_edge_is_reported(self):
        h_r, h_f = second_order(0.6, 2 * math.pi * 12e6), second_order(0.4, 2 * math.pi * 12e6)
        vec = follower(h_r)
        t = vec["time"]
        lo = VCM - r.STEP_V / 2
        vec["v(vout)"] = lo + r.STEP_V * (h_r(t - r.RISE_T) - h_f(t - r.FALL_T))
        m = r.extract_step(vec)
        self.assertTrue(m.valid, m.reason)
        self.assertGreater(m.fall.overshoot_pct, m.rise.overshoot_pct)
        self.assertEqual(m.overshoot_pct, m.fall.overshoot_pct)
        for b in r.SETTLE_BANDS:
            self.assertEqual(m.settling(b), max(m.rise.ts[b], m.fall.ts[b]))

    def test_systematic_offset_does_not_change_the_figures(self):
        # settling is relative to the output's own settled level, not to the input
        h = second_order(0.5, 2 * math.pi * 12e6)
        a = r.extract_step(follower(h))
        b = r.extract_step(follower(h, offset=4e-3))
        self.assertTrue(b.valid, b.reason)
        self.assertAlmostEqual(a.overshoot_pct, b.overshoot_pct, places=6)
        for band in r.SETTLE_BANDS:
            self.assertAlmostEqual(a.settling(band), b.settling(band), delta=1e-12)


class NonSettlingTests(unittest.TestCase):
    def test_sustained_ring_is_not_settled(self):
        h = first_order(10e-9)
        ring = lambda t: 2e-3 * np.sin(2 * math.pi * 20e6 * t)  # noqa: E731  # 2 % of the step, never decays
        m = r.extract_step(follower(h, extra=ring))
        self.assertTrue(m.valid, m.reason)  # the follower follows; it just never settles
        for e in m.edges():
            self.assertTrue(math.isinf(e.ts[0.01]), e.name)
            self.assertTrue(math.isinf(e.ts[0.001]), e.name)
            self.assertFalse(e.monotonic)
        self.assertTrue(math.isinf(m.settling(0.01)))
        self.assertEqual(r.fmt_ts(m.settling(0.01)), "not settled")

    def test_slow_tail_settles_to_1_but_not_to_0p1_percent(self):
        # 4.6 tau = 299 ns < 440 ns to the reference window; 6.9 tau = 449 ns > 440 ns. The
        # residual (0.12 % .. 0.08 % across the window) is hidden in the window mean, but the
        # drift across the window (0.04 %) exceeds DRIFT_FRAC x 0.1 % and not 1 %.
        tau = 65e-9
        m = r.extract_step(follower(first_order(tau)))
        self.assertTrue(m.valid, m.reason)
        for e in m.edges():
            self.assertTrue(math.isfinite(e.ts[0.01]), e.name)
            self.assertTrue(math.isinf(e.ts[0.001]), e.name)
            self.assertGreater(e.drift_pct, r.DRIFT_FRAC * 0.1)  # the tail is visible as drift

    def test_slow_tail_hidden_in_the_window_mean_is_not_settled(self):
        # A small slow tail (0.4 % of the step, tau 300 ns) on top of a fast settle: the
        # deviation from the reference-window MEAN stays inside 0.1 %, but the output still
        # drifts across the window, so the 0.1 % band is not claimed.
        fast = first_order(5e-9)
        h = lambda s: fast(s) - 0.004 * np.where(s > 0, np.exp(-np.clip(s, 0, None) / 300e-9), 0)  # noqa: E731
        m = r.extract_step(follower(h))
        self.assertTrue(m.valid, m.reason)
        self.assertTrue(math.isinf(m.rise.ts[0.001]))
        self.assertTrue(math.isfinite(m.rise.ts[0.01]))

    def test_settling_time_helper(self):
        t = np.arange(0, 100e-9, 1e-9)
        err = np.exp(-t / 10e-9)
        self.assertAlmostEqual(r.settling_time(t, err, 0.01, 0.0, 90e-9), 10e-9 * math.log(100), delta=0.2e-9)
        self.assertTrue(math.isinf(r.settling_time(t, err, 0.01, 0.0, 30e-9)))
        self.assertEqual(r.settling_time(t, np.zeros_like(t), 0.01, 0.0, 90e-9), 0.0)


class InvalidTests(unittest.TestCase):
    def test_stuck_output_is_invalid(self):
        vec = follower(first_order(10e-9))
        vec["v(vout)"] = np.full_like(vec["time"], VCM)
        m = r.extract_step(vec)
        self.assertFalse(m.valid)
        self.assertTrue(math.isnan(m.overshoot_pct))

    def test_clipped_or_wrong_size_step_is_invalid(self):
        vec = follower(first_order(10e-9))
        vec["v(vout)"] = np.minimum(vec["v(vout)"], VCM)  # output stops at VCM: half the step
        m = r.extract_step(vec)
        self.assertFalse(m.valid)
        self.assertIn("reach the input", m.reason)

    def test_wrong_polarity_is_invalid(self):
        vec = follower(first_order(10e-9))
        vec["v(vinp)"] = 2 * VCM - vec["v(vinp)"]
        vec["v(vout)"] = 2 * VCM - vec["v(vout)"]
        m = r.extract_step(vec)  # input falls first: no rising edge where the bench puts it
        self.assertFalse(m.valid)

    def test_output_not_following_before_the_edge_is_invalid(self):
        m = r.extract_step(follower(first_order(10e-9), offset=0.05))
        self.assertFalse(m.valid)
        self.assertIn("before the edge", m.reason)

    def test_malformed_inputs_are_invalid(self):
        vec = follower(first_order(10e-9))
        self.assertFalse(r.extract_step({k: v[:50] for k, v in vec.items()}).valid)
        self.assertFalse(r.extract_step({k: v[: len(v) // 2] for k, v in vec.items()}).valid)
        self.assertFalse(r.extract_step({"time": vec["time"]}).valid)
        self.assertFalse(r.extract_step({k: v[::-1] for k, v in vec.items()}).valid)


class SummaryTests(unittest.TestCase):
    def _grid(self):
        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        base = r.extract_step(follower(second_order(0.6, 2 * math.pi * 12e6)))
        return keys, {k: base for k in keys}

    def test_grid_is_45_unique_points(self):
        keys, _ = self._grid()
        self.assertEqual(len(set(keys)), 45)

    def test_worst_points_and_not_settled_rank_worst(self):
        keys, res = self._grid()
        res = dict(res)
        res[keys[7]] = r.extract_step(follower(second_order(0.4, 2 * math.pi * 12e6)))
        res[keys[9]] = r.extract_step(follower(first_order(80e-9)))  # 0.1 % not settled
        res[keys[11]] = r.Metrics(valid=False, reason="x")
        s = r.summarise(res)
        self.assertEqual(s.worst_overshoot[1], keys[7])
        self.assertEqual(s.worst_ts[0.001][1], keys[9])
        self.assertTrue(math.isinf(s.worst_ts[0.001][0]))
        self.assertEqual(s.not_settled[0.001], [keys[9]])
        self.assertEqual(s.n_invalid, 1)
        self.assertEqual(s.n_monotonic, 1)  # only the first-order point
        self.assertNotIn(keys[11], s.nonmonotonic)

    def test_control_logic(self):
        nom = r.ControlRun("nominal", "", r.extract_step(follower(second_order(0.6, 2 * math.pi * 12e6))))
        big = r.ControlRun("cl-x10", "", r.extract_step(follower(second_order(0.3, 2 * math.pi * 6e6))))
        dead = r.ControlRun("ibias-zero", "", r.Metrics(valid=False, reason="dead"))
        self.assertEqual(r.control_failures([nom, big, dead]), [])
        same = r.ControlRun("cl-x10", "", nom.metrics)
        self.assertTrue(r.control_failures([nom, same, dead]))
        alive = r.ControlRun("ibias-zero", "", nom.metrics)
        self.assertTrue(r.control_failures([nom, big, alive]))
        self.assertTrue(r.control_failures([r.ControlRun("nominal", "", None, error="boom"), big, dead]))


class DataTests(unittest.TestCase):
    def test_committed_data_rederive_the_same_figures(self):
        with tempfile.TemporaryDirectory() as d:
            for vec in (follower(second_order(0.5, 2 * math.pi * 12e6)), follower(first_order(80e-9))):
                m = r.extract_step(vec)
                p = Path(d) / "pt.dat"
                r.save_point_data(m, p)
                m2 = r.rederive(p)
                self.assertTrue(r.same_result(m, m2))
            other = r.extract_step(follower(second_order(0.4, 2 * math.pi * 12e6)))
            self.assertFalse(r.same_result(m2, other))

    def test_invalid_point_keeps_its_data_and_reason(self):
        vec = follower(first_order(10e-9), offset=0.05)
        m = r.extract_step(vec)
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
        vec = follower(second_order(0.5, 2 * math.pi * 12e6))
        m = r.extract_step(vec)
        t, vo = vec["time"], vec["v(vout)"]
        good = {"vlo_v": m.rise.v0, "vhi_v": m.fall.v0,
                "vmax_rise_v": float(vo[(t >= r.RISE_T) & (t <= r.FALL_T - r.SAMPLE_BEFORE)].max()),
                "vmin_fall_v": float(vo[t >= r.FALL_T].min())}
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "w.raw"
            raw.write_text(_raw(vec))
            res, _, problems = r.analyse_report({"corners": [self._corner(raw, good)]}, [r.NOMINAL])
            self.assertEqual(problems, [])
            self.assertTrue(res[r.NOMINAL].valid)
            self.assertAlmostEqual(res[r.NOMINAL].overshoot_pct, m.overshoot_pct, places=4)
            bad = dict(good, vmax_rise_v=good["vmax_rise_v"] + 2e-3)  # ngspice disagrees by 2 mV
            _, _, problems = r.analyse_report({"corners": [self._corner(raw, bad)]}, [r.NOMINAL])
            self.assertTrue(any("vmax_rise_v" in p for p in problems))


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
        mm = re.search(r"^Vstep vinp vcm dc 0 pulse\((\S+) (\S+) (\S+)n (\S+)n (\S+)n (\S+)n \S+\)", t, re.M)
        self.assertIsNotNone(mm)
        lo, hi, td, tr, tf, pw = (float(x) for x in mm.groups())
        self.assertAlmostEqual(hi - lo, r.STEP_V)
        self.assertAlmostEqual(lo + hi, 0.0)  # centred on VCM
        self.assertAlmostEqual(td * 1e-9, r.RISE_T)
        self.assertAlmostEqual(tr * 1e-9, r.EDGE_T)
        self.assertAlmostEqual(tf * 1e-9, r.EDGE_T)
        self.assertAlmostEqual((td + tr + pw) * 1e-9, r.FALL_T)  # falling edge starts at FALL_T

    def test_window_spans_many_time_constants(self):
        tau_max = 1 / (2 * math.pi * r.GBW_MIN_HZ)
        self.assertGreaterEqual((r.FALL_T - r.RISE_T - r.SAMPLE_BEFORE - r.REF_WINDOW) / tau_max, 20)
        self.assertGreaterEqual(r.TSTOP - r.FALL_T, r.FALL_T - r.RISE_T)
        self.assertLessEqual(r.TSTEP, tau_max / 10)


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
        for n in ("STEP_V", "RISE_T", "FALL_T", "TSTOP", "TSTEP", "EDGE_T", "SAMPLE_BEFORE", "REF_WINDOW",
                  "SETTLE_BANDS", "MONO_TOL", "DRIFT_FRAC", "IBIAS_A", "CL_F", "GBW_MIN_HZ"):
            self.assertEqual(getattr(r, n), getattr(r.mc, n), n)
        self.assertEqual(r.SETTLE_BANDS, (0.01, 0.001))
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


class GainCrossReferenceTests(unittest.TestCase):
    def test_selected_gain_record_pm_is_parsed_for_all_45_points(self):
        rel, pm = r.load_gain_pm()
        if not rel:
            self.skipTest("no selected gain record")
        self.assertEqual(len(pm), 45)
        self.assertEqual(set(pm), set(r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)))
        self.assertTrue(all(5 < gbw < 50 and 0 < p < 90 for gbw, p in pm.values()))


class RecordTests(unittest.TestCase):
    def test_record_reports_worst_points_and_judges_nothing(self):
        from datetime import datetime, timezone

        keys = r.g.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        base = r.extract_step(follower(second_order(0.6, 2 * math.pi * 12e6)))
        res = {k: base for k in keys}
        res[keys[5]] = r.extract_step(follower(second_order(0.4, 2 * math.pi * 12e6)))
        res[keys[6]] = r.extract_step(follower(first_order(65e-9)))
        ctrls = [r.ControlRun("nominal", "n", base), r.ControlRun("ibias-zero", "z", r.Metrics(valid=False, reason="dead"))]
        md = r.build_record(
            record="rid", stamp=datetime(2026, 1, 1, tzinfo=timezone.utc), pdk=r.Pdk(Path("/x"), "gf180mcuD", "test"),
            ngspice="ng", klt_ver="k", backend_desc="local", report={}, results=res, ctrls=ctrls, ctrl_bad=[],
            plots=[], dut_sha="0" * 64, xchk_count=(45, 45), rederived=(45, 45, []), gain_pm=("g.md", {}),
        )
        self.assertIn("evidence only, no verdict", md)
        self.assertIn(f"| Overshoot (worse edge) | {res[keys[5]].overshoot_pct:.2f} % | {r.g.fmt_key(keys[5])} |", md)
        self.assertIn(f"| Settling to 0.1 % (worse edge) | not settled (1 points not settled) | {r.g.fmt_key(keys[6])} |", md)
        self.assertIn("Largest reference-window drift", md)
        self.assertEqual(sum(1 for ln in md.splitlines() if ln.startswith("| `") and "|" in ln[3:]) - len(ctrls), 45)
        for word in ("PASS", "FAIL"):
            self.assertNotIn(f"**{word}**", md)


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
