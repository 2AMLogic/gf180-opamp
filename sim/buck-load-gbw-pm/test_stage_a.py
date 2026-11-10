#!/usr/bin/env python3
"""No-simulator self-test of the Stage A runner's classification logic (issue #295).

    python3 sim/buck-load-gbw-pm/test_stage_a.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_stage_a as S  # noqa: E402


def freqs():
    return np.logspace(-1, 9, 201)


def two_pole(a0_db=80.0, p1=100.0, p2=2e6):
    f = freqs()
    return f, 10 ** (a0_db / 20) / ((1 + 1j * f / p1) * (1 + 1j * f / p2))


def dev(on=True, sat=True, name="M1"):
    return S.DevOp(name, "r", 5e-6, 0.8 if on else 0.5, 0.68, 1.0 if sat else 0.0, 0.1, 1e-5, on, sat)


class Plateau(unittest.TestCase):
    def test_flat_passes(self):
        f, h = two_pole()
        self.assertTrue(S.plateau_check(f, h)["ok"])

    def test_inadequate_isolation_rejected(self):
        f = freqs()
        h = 1000 / (1 + 1j * 1e-1 * 1e4 / (2 * np.pi * f * 1e4 + 1e-30) * 0 + 1j * f / 0.5)  # pole inside decade
        self.assertFalse(S.plateau_check(f, h)["ok"])

    def test_wrong_polarity_rejected(self):
        f, h = two_pole()
        self.assertIn("polarity", S.plateau_check(f, -h)["reason"])

    def test_nonfinite_rejected(self):
        f, h = two_pole()
        h[3] = np.nan
        self.assertFalse(S.plateau_check(f, h)["ok"])


class Crossings(unittest.TestCase):
    def test_single_pole_unity(self):
        f = freqs()
        h = 1000 / (1 + 1j * f / 1e3)
        (fx, d), = S.level_crossings(f, 20 * np.log10(abs(h)))
        self.assertEqual(d, "down")
        self.assertAlmostEqual(fx / 1e6, 1.0, delta=0.03)

    def test_small_signal_known_margin(self):
        f, aol = two_pole(80, 100.0, 2e6)
        beta = np.full_like(aol, 0.01)  # 1/beta = 40 dB
        r = S.small_signal(f, aol, beta)
        down = [c for c in r["crossings"] if c["direction"] == "down"]
        self.assertEqual(len(down), 1)
        # |T| = 1 where |Aol| = 100: GBW = 1e4 * 100 Hz = 1e6 Hz, so f ~ 1e4 Hz
        self.assertAlmostEqual(down[0]["f_hz"] / 1e4, 1.0, delta=0.05)
        want = 180 - np.degrees(np.arctan(down[0]["f_hz"] / 100.0)) - np.degrees(np.arctan(down[0]["f_hz"] / 2e6))
        self.assertAlmostEqual(down[0]["margin_deg"], want, delta=0.5)


class Region(unittest.TestCase):
    def ops(self, **kw):
        return {"xm1": dev(name="M1", **kw), "xm2": dev(name="M2", **kw), "xm5": dev(name="M5")}

    def test_valid(self):
        self.assertTrue(S.region_verdict(self.ops())[0])

    def test_pair_off_invalid(self):
        ok, why = S.region_verdict(self.ops(on=False))
        self.assertFalse(ok)
        self.assertTrue(any("off" in w for w in why))

    def test_missing_is_invalid_not_valid(self):
        ops = self.ops()
        ops["xm5"] = S.DevOp("M5", "r", float("nan"), float("nan"), float("nan"), float("nan"),
                             float("nan"), float("nan"), False, False)
        self.assertFalse(S.region_verdict(ops)[0])


class Classification(unittest.TestCase):
    base = dict(region_valid=True, servo_ok=True, iso_problems=[], plateau_ok=True, ac_region_reproduces=True)

    def test_valid(self):
        self.assertEqual(S.classify(**self.base)["kind"], "valid")

    def test_invalid_region_is_finding_and_stops(self):
        c = S.classify(**{**self.base, "region_valid": False})
        self.assertEqual((c["kind"], c["stop"]), ("finding", True))

    def test_servo_failure_is_bench_fault_not_finding(self):
        self.assertEqual(S.classify(**{**self.base, "servo_ok": False})["kind"], "bench-fault")

    def test_ac_only_invalid_region_is_bench_fault(self):
        self.assertEqual(S.classify(**{**self.base, "ac_region_reproduces": False})["kind"], "bench-fault")

    def test_iso_failure_is_bench_fault(self):
        self.assertEqual(S.classify(**{**self.base, "iso_problems": ["x"]})["kind"], "bench-fault")

    def test_routing_never_opens_a_row(self):
        for k in ("finding", "bench-fault", "valid"):
            self.assertNotIn("GBW >=", S.route(k))
        self.assertIn("unknown", S.route("valid"))


class Isolation(unittest.TestCase):
    def run_for(self, lfb, plateau_ok=True, fx=1e5, ph=-120.0, dc=90.0):
        return {"plateau": {"ok": plateau_ok, "dc_db": dc, "reason": ""},
                "crossings": [{"f_hz": fx, "direction": "down", "phase_deg": ph, "margin_deg": 180 + ph}]}

    def runs(self, over=None):
        r = {S.LFB_NOMINAL: self.run_for(1), 1e8: self.run_for(1), 1e10: self.run_for(1),
             S.LFB_INADEQUATE: self.run_for(1, plateau_ok=False)}
        r.update(over or {})
        return r

    def test_ok(self):
        self.assertEqual(S.isolation_verdict(self.runs()), [])

    def test_inadequate_must_be_rejected(self):
        p = S.isolation_verdict(self.runs({S.LFB_INADEQUATE: self.run_for(1)}))
        self.assertTrue(any("NOT rejected" in x for x in p))

    def test_disagreement_reported(self):
        p = S.isolation_verdict(self.runs({1e8: self.run_for(1, fx=1.2e5)}))
        self.assertTrue(any("crossing frequency" in x for x in p))


class Guards(unittest.TestCase):
    def test_committed_benches_pass(self):
        self.assertEqual(S.guard_tb(S.TB_REGION.read_text(), name="r"), [])
        self.assertEqual(S.guard_tb(S.TB_AC.read_text(), name="a"), [])
        self.assertEqual(S.guard_tb(S.TB_BETA.read_text(), name="b", need_dut=False), [])
        self.assertEqual(S.guard_load_inc(S.LOAD_INC.read_text()), [])

    def test_hand_declared_mosfet_rejected(self):
        self.assertTrue(S.guard_tb(S.TB_AC.read_text() + "\nM1 a b c d nfet_03v3\n", name="a"))

    def test_changed_consumer_value_rejected(self):
        self.assertTrue(S.guard_load_inc(S.LOAD_INC.read_text().replace("R2 fbn z 4.55k", "R2 fbn z 5k")))


if __name__ == "__main__":
    unittest.main()
