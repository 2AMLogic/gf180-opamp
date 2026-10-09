#!/usr/bin/env python3
"""Regression tests for the PSRR experiment (issue #39).

The unit tests need no simulator: extraction is checked against synthetic
complex responses with known Ad / Avdd / Avss, and the source/request guards
by mutating the committed testbench text in memory. `SimTests` runs local
nominal single units and is skipped when klt, ngspice or the PDK is
unavailable; it never runs the grid.

    python3 sim/psrr/test_psrr.py          # or: python3 -m unittest
"""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_psrr as r  # noqa: E402
from harness import PdkNotFound, find_pdk  # noqa: E402

C = r.C
FREQ = np.logspace(math.log10(C.AC_FSTART), math.log10(C.AC_FSTOP), C.N_FREQ)
S = 1j * FREQ
FLOOR = 1e-12


def ad_true():
    return 6e4 / ((1 + S / 200.0) * (1 + S / 40e6))


def run_vec(vout, vp=0.0, vn=0.0, vdd=0.0, vss=0.0):
    full = lambda v: np.full(FREQ.shape, v, dtype=complex) if np.isscalar(v) else v  # noqa: E731
    return {"frequency": FREQ.astype(complex), "v(vout)": full(vout), "v(vinp)": full(vp), "v(vinn)": full(vn),
            "v(vdd)": full(vdd), "v(vss)": full(vss)}


def triple(ad, avdd, avss, *, vdd_drive=1.0, vss_drive=1.0, leak=0.0):
    dm = run_vec(ad * 1.0, vp=0.5, vn=-0.5)
    sp = run_vec(avdd * vdd_drive + ad * (-leak), vn=leak, vdd=vdd_drive)
    sn = run_vec(avss * vss_drive, vss=vss_drive)
    return dm, {"vdd": sp, "vss": sn}


class ExtractionTests(unittest.TestCase):
    def test_recovers_input_referred_psrr(self):
        ad = ad_true()
        avdd = 0.5 * np.ones_like(ad)  # -6.02 dB
        avss = 0.02 * (1 + S / 1e5)  # -34 dB, rising
        dm, sup = triple(ad, avdd, avss)
        pt = r.extract_psrr(dm, sup, FLOOR)
        self.assertTrue(pt.valid, pt.reason)
        self.assertAlmostEqual(pt.rej["vdd"].dc_db, 20 * math.log10(6e4 / 0.5), delta=1e-3)
        self.assertAlmostEqual(pt.rej["vss"].dc_db, 20 * math.log10(6e4 / 0.02), delta=1e-3)
        # Output feedthrough is a different quantity: -20 log10 |Asupply| only.
        self.assertAlmostEqual(pt.feedthrough_db["vdd"], 20 * math.log10(2), delta=1e-3)
        self.assertNotAlmostEqual(pt.feedthrough_db["vdd"], pt.rej["vdd"].dc_db, delta=1)

    def test_rail_amplitude_is_divided_out(self):
        ad = ad_true()
        dm, sup = triple(ad, 0.5 * np.ones_like(ad), 0.02 * np.ones_like(ad))
        # A 2 V rail drive with the same transfer must give the same PSRR -- but
        # the excitation check insists on the stated 1 V drive.
        dm2, sup2 = triple(ad, 0.5 * np.ones_like(ad), 0.02 * np.ones_like(ad), vdd_drive=2.0)
        self.assertFalse(r.extract_psrr(dm2, sup2, FLOOR).valid)
        self.assertTrue(r.extract_psrr(dm, sup, FLOOR).valid)

    def test_wrong_rail_or_noisy_other_rail_rejected(self):
        ad = ad_true()
        dm, sup = triple(ad, 0.5 * np.ones_like(ad), 0.02 * np.ones_like(ad))
        swapped = {"vdd": sup["vss"], "vss": sup["vdd"]}
        pt = r.extract_psrr(dm, swapped, FLOOR)
        self.assertFalse(pt.valid)
        self.assertIn("rail excitation", pt.reason)

    def test_inputs_must_be_quiet_in_supply_runs(self):
        ad = ad_true()
        dm, sup = triple(ad, 0.5 * np.ones_like(ad), 0.02 * np.ones_like(ad), leak=1e-6)
        pt = r.extract_psrr(dm, sup, FLOOR)
        self.assertFalse(pt.valid)
        self.assertIn("not AC-quiet", pt.reason)

    def test_dm_run_rails_must_be_quiet(self):
        ad = ad_true()
        dm, sup = triple(ad, 0.5 * np.ones_like(ad), 0.02 * np.ones_like(ad))
        dm["v(vdd)"] = np.full(FREQ.shape, 1e-3, dtype=complex)
        self.assertFalse(r.extract_psrr(dm, sup, FLOOR).valid)

    def test_mismatched_axes_and_non_finite(self):
        ad = ad_true()
        dm, sup = triple(ad, 0.5 * np.ones_like(ad), 0.02 * np.ones_like(ad))
        bad = dict(sup["vss"])
        bad["frequency"] = bad["frequency"] * 1.01
        self.assertFalse(r.extract_psrr(dm, {"vdd": sup["vdd"], "vss": bad}, FLOOR).valid)
        bad = dict(sup["vdd"])
        bad["v(vout)"] = bad["v(vout)"].copy()
        bad["v(vout)"][3] = complex(float("inf"), 0)
        self.assertFalse(r.extract_psrr(dm, {"vdd": bad, "vss": sup["vss"]}, FLOOR).valid)

    def test_zero_supply_gain_is_a_lower_bound(self):
        ad = ad_true()
        dm, sup = triple(ad, np.zeros_like(ad), 0.02 * np.ones_like(ad))
        pt = r.extract_psrr(dm, sup, 1e-10)
        self.assertTrue(pt.valid, pt.reason)
        self.assertTrue(pt.rej["vdd"].lower_bound)
        self.assertFalse(pt.rej["vss"].lower_bound)
        self.assertTrue(np.all(np.isfinite(pt.rej["vdd"].curve_db)))


class GuardTests(unittest.TestCase):
    def tb(self):
        return r.TESTBENCH.read_text()

    def test_committed_testbench_passes(self):
        self.assertEqual(r.guard_testbench(self.tb()), [])

    def test_vss_must_be_a_driven_port(self):
        self.assertTrue(r.guard_testbench(self.tb().replace("Xdut vdd vss vinp", "Xdut vdd 0 vinp")))
        self.assertTrue(r.guard_testbench(self.tb().replace("Vss vss 0 dc 0 ac {acss}", "")))
        self.assertTrue(r.guard_testbench(self.tb().replace("Vdd vdd 0 dc 3.3 ac {acdd}", "Vdd vdd 0 dc 3.3")))

    def test_references_stay_on_ground(self):
        self.assertTrue(r.guard_testbench(self.tb().replace("CL vout 0 2p", "CL vout vss 2p")))

    def test_bench_lines_and_no_transistors(self):
        base = self.tb()
        for line in ("CL vout 0 2p", "Ibias vdd ibias dc 10u", "Esv vinn vnac vsv 0 1"):
            self.assertTrue(r.guard_testbench(base.replace(line, "")), line)
        self.assertTrue(r.guard_testbench(base + "\nM1 a b c d pfet_03v3 W=1u L=1u\n"))
        self.assertTrue(r.guard_testbench(base.replace(".include 'opamp_two_stage.dut.spice'", "")))

    def test_modes_drive_one_source_each(self):
        self.assertEqual(sorted(C.param_keys(self.tb())), sorted(["acp", "acn", "acdd", "acss", "rsv", "csv"]))
        for mode, p in r.MODES.items():
            driven = {k for k, v in p.items() if v}
            self.assertEqual(driven, {"dm": {"acp", "acn"}, "vdd": {"acdd"}, "vss": {"acss"}}[mode])
        self.assertEqual(r.MODES["dm"]["acp"] - r.MODES["dm"]["acn"], 1.0)

    def test_fixture_is_separate_from_the_baseline(self):
        try:
            pdk = find_pdk()
        except PdkNotFound:
            self.skipTest("PDK not available")
        with tempfile.TemporaryDirectory() as d:
            base = C.materialise(Path(d) / "a", pdk, C.with_servo(r.MODES["vdd"]), testbench=r.TESTBENCH,
                                 guard=r.guard_testbench)
            fix = C.materialise(Path(d) / "b", pdk, C.with_servo(r.MODES["vdd"]), testbench=r.TESTBENCH,
                                guard=r.guard_testbench, fixture=("Rft vdd vout 100000",))
            self.assertNotIn("Rft", base.read_text())
            self.assertIn("Rft vdd vout 100000", fix.read_text())
            with self.assertRaises(RuntimeError):  # a fixture may not smuggle in a transistor
                C.materialise(Path(d) / "c", pdk, C.with_servo(r.MODES["vdd"]), testbench=r.TESTBENCH,
                              guard=r.guard_testbench, fixture=("M1 vout vdd 0 0 nfet_03v3 W=1u L=1u",))


class SimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("klt") is None or shutil.which("ngspice") is None:
            raise unittest.SkipTest("klt/ngspice not available")
        try:
            cls.pdk = find_pdk()
        except PdkNotFound:
            raise unittest.SkipTest("gf180mcu PDK not available")

    def test_nominal_triple_is_valid(self):
        with tempfile.TemporaryDirectory() as d:
            pt, runs = r.psrr_triple(self.pdk, Path(d), "t", "nominal", C.FLOOR_MIN)
        self.assertTrue(pt.valid, pt.reason)
        for rail in r.RAILS:
            self.assertTrue(pt.rej[rail].plateau_ok)
            self.assertGreater(pt.rej[rail].dc_db, 60)
        self.assertLess(pt.input_quiet, 1e-12)
        for run in runs:
            self.assertEqual(run.op.problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
