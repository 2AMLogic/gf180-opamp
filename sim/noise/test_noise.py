#!/usr/bin/env python3
"""Regression tests for the noise experiment (issue #46).

The unit tests need no simulator: parsing, integration and the floor/corner
fit are checked against synthetic spectra with known answers, and the source
guards by mutating the committed testbench / DUT text in memory.
`SimControlTests` runs one LOCAL nominal-point simulation and is skipped when
klt, ngspice or the PDK is unavailable; it never runs the grid.

    python3 sim/noise/test_noise.py          # or: python3 -m unittest
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

import run_noise as r  # noqa: E402
from harness import PdkNotFound, find_pdk  # noqa: E402


def grid(ppd: int = r.PPD) -> np.ndarray:
    n = int(round(math.log10(r.F_STOP / r.F_START) * ppd)) + 1
    return np.logspace(math.log10(r.F_START), math.log10(r.F_STOP), n)


def synth(sw: float = (30e-9) ** 2, k: float = 2e-10, a: float = 1.0, gain: float = 6e4) -> r.Spectrum:
    f = grid()
    s = sw + k / f**a
    d = np.sqrt(s)
    return r.Spectrum(f, d, d * gain / np.sqrt(1 + (f / 400.0) ** 2))


def raw_text(spec: r.Spectrum) -> str:
    lines = ["Title: t", "Plotname: Noise Spectral Density Curves", "Flags: real", "No. Variables: 3",
             f"No. Points: {len(spec.freq)}", "Variables:", "\t0\tfrequency\tfrequency grid=3",
             "\t1\tinoise_spectrum\tvoltage-density", "\t2\tonoise_spectrum\tvoltage-density", "Values:"]
    for i, (f, a, b) in enumerate(zip(spec.freq, spec.inoise, spec.onoise)):
        lines += [f" {i}\t{f:.15e}", f"\t{a:.15e}", f"\t{b:.15e}"]
    return "\n".join(lines) + "\n"


class IntegrationTests(unittest.TestCase):
    def test_flat_density_is_exact(self):
        f = grid()
        d = np.full_like(f, 10e-9)
        got = r.integrate_power(f, d, 100.0, 1e6)
        self.assertAlmostEqual(got / (1e-16 * (1e6 - 100)), 1.0, places=9)

    def test_one_over_f_is_exact_on_a_coarse_grid(self):
        f = grid(5)  # coarse: a trapezoid would be visibly wrong
        d = np.sqrt(1e-10 / f)
        got = r.integrate_power(f, d, 10.0, 1e5)
        self.assertAlmostEqual(got / (1e-10 * math.log(1e4)), 1.0, places=9)

    def test_off_grid_band_edges_interpolate(self):
        f = grid()
        d = np.sqrt(1e-10 / f)
        got = r.integrate_power(f, d, 123.0, 45678.0)
        self.assertAlmostEqual(got / (1e-10 * math.log(45678.0 / 123.0)), 1.0, places=6)

    def test_band_outside_sweep_rejected(self):
        f = grid()
        with self.assertRaises(ValueError):
            r.integrate_power(f, np.ones_like(f), 0.01, 1e3)
        with self.assertRaises(ValueError):
            r.integrate_power(f, np.ones_like(f), 1e3, 1e9)

    def test_spot_interpolation(self):
        f = grid()
        d = 1e-6 / np.sqrt(f)
        self.assertAlmostEqual(r.interp_loglog(f, d, 100.0) / 1e-7, 1.0, places=9)
        with self.assertRaises(ValueError):
            r.interp_loglog(f, d, 1e9)


class FitTests(unittest.TestCase):
    def test_recovers_floor_corner_and_exponent(self):
        sw, k, a = (30e-9) ** 2, 2e-10, 0.95
        s = synth(sw=sw, k=k, a=a)
        fit = r.fit_floor(s.freq, s.inoise)
        self.assertTrue(fit.ok, fit.note)
        self.assertAlmostEqual(fit.floor_nv, 30.0, delta=0.3)
        self.assertAlmostEqual(fit.a, a, delta=0.02)
        self.assertAlmostEqual(fit.corner_hz / (k / sw) ** (1 / a), 1.0, delta=0.05)
        self.assertTrue(fit.flicker_visible)

    def test_thermal_only_spectrum_reports_no_flicker(self):
        f = grid()
        fit = r.fit_floor(f, np.full_like(f, 25e-9))
        self.assertFalse(fit.flicker_visible)
        self.assertAlmostEqual(fit.floor_nv, 25.0, delta=0.1)

    def test_poor_fit_is_not_reported_as_ok(self):
        f = grid()
        d = np.sqrt((30e-9) ** 2 * (1 + 40 * np.sin(np.log10(f) * 5) ** 2))
        self.assertFalse(r.fit_floor(f, d).ok)


class ParseTests(unittest.TestCase):
    def test_roundtrip(self):
        s = synth()
        got = r.parse_noise_raw(raw_text(s))
        np.testing.assert_allclose(got.freq, s.freq, rtol=1e-12)
        np.testing.assert_allclose(got.inoise, s.inoise, rtol=1e-12)
        np.testing.assert_allclose(got.onoise, s.onoise, rtol=1e-12)

    def test_malformed_inputs_fail_loudly(self):
        good = raw_text(synth())
        for bad in (good.replace("Flags: real", "Flags: complex"),
                    good.replace("Values:", "Vals:"),
                    good + "\t1.0\n",
                    good.replace("inoise_spectrum", "zz"),
                    good.replace("e-0", "nan", 1)):
            with self.assertRaises(ValueError):
                r.parse_noise_raw(bad)

    def test_totals_from_log(self):
        log = "x\nnoise2.inoise_total = 1.153807e-04\nnoise2.onoise_total = 2.366976e+00\n"
        self.assertEqual(r.parse_totals(log), {"inoise_total": 1.153807e-04, "onoise_total": 2.366976})
        self.assertEqual(r.parse_totals("nothing"), {})


class SweepAndExtractTests(unittest.TestCase):
    def test_good_sweep_has_no_problems(self):
        self.assertEqual(r.sweep_problems(synth(), r.PPD, "t"), [])

    def test_short_sweep_detected(self):
        s = synth()
        s = r.Spectrum(s.freq[::2], s.inoise[::2], s.onoise[::2])
        self.assertTrue(r.sweep_problems(s, r.PPD, "t"))

    def test_nonpositive_density_detected(self):
        s = synth()
        s.inoise[5] = 0.0
        self.assertTrue(r.sweep_problems(s, r.PPD, "t"))

    def test_extract_matches_analytic_band(self):
        sw, k = (30e-9) ** 2, 2e-10
        res = r.extract(synth(sw=sw, k=k, a=1.0))
        want = math.sqrt(sw * (1e6 - 100) + k * math.log(1e4)) * 1e6
        self.assertAlmostEqual(res.band_uv["100 Hz - 1 MHz"] / want, 1.0, delta=2e-3)

    def test_totals_crosscheck_flags_disagreement(self):
        res = r.extract(synth())
        ok, bad = r.totals_crosscheck(r.NOMINAL, res, {"onoise_total": res.integrated_out_v, "inoise_total": res.integrated_in_v})
        self.assertEqual(bad, [])
        _, bad = r.totals_crosscheck(r.NOMINAL, res, {"onoise_total": res.integrated_out_v * 1.02, "inoise_total": res.integrated_in_v})
        self.assertTrue(bad)
        _, bad = r.totals_crosscheck(r.NOMINAL, res, {"onoise_total": res.integrated_out_v, "inoise_total": res.integrated_in_v * 1.2})
        self.assertTrue(bad)
        _, bad = r.totals_crosscheck(r.NOMINAL, res, {})
        self.assertTrue(bad)

    def test_gain_crosscheck_catches_wrong_input_reference(self):
        s = synth()
        gdir = r.latest_gain_dir()
        if gdir is None:
            self.skipTest("no committed gain-bench dataset")
        dev, bad = r.gain_crosscheck(r.NOMINAL, s, gdir)  # synthetic gain differs from the real one
        self.assertTrue(bad)


class GuardTests(unittest.TestCase):
    def test_committed_testbench_passes(self):
        self.assertEqual(r.guard_testbench(r.TESTBENCH.read_text()), [])

    def test_hand_declared_transistor_rejected(self):
        self.assertTrue(r.guard_testbench(r.TESTBENCH.read_text() + "\nM9 a b c d nfet_03v3 W=1u L=1u\n"))

    def test_missing_dut_include_rejected(self):
        tb = r.TESTBENCH.read_text().replace(".include 'opamp_two_stage.dut.spice'", "")
        self.assertTrue(r.guard_testbench(tb))

    def test_missing_feedback_or_load_rejected(self):
        base = r.TESTBENCH.read_text()
        for line in ("Lfb vout vinn {lfb}", "Cfb vinn 0 {cfb}", "CL vout 0 2p", "Ibias vdd ibias dc 10u",
                     "Vcm vinp 0 dc 1.65 ac 1"):
            self.assertTrue(r.guard_testbench(base.replace(line, "")), line)

    def test_altered_dut_rejected(self):
        dut = r.G.load_dut_text()
        self.assertEqual(r.G.guard_dut(dut), [])
        self.assertTrue(r.G.guard_dut(dut.replace("L=1u", "L=2u", 1)))

    def test_request_uses_noise_analysis_and_spectrum_plot(self):
        req = r.noise_request(Path("/x/tb.spice"), type("P", (), {"variant": "gf180mcuD"})(), r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(req["analysis"]["kind"], "noise")
        self.assertIn("setplot noise1", req["analysis"]["args"])
        self.assertIn("v(vout) Vcm", req["analysis"]["args"])
        self.assertEqual(len(r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)), 45)
        self.assertEqual(req["corners"]["supply_v"]["vcm"], [1.485, 1.65, 1.815])


class SimControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("klt") is None or shutil.which("ngspice") is None:
            raise unittest.SkipTest("klt/ngspice not available")
        try:
            cls.pdk = find_pdk()
        except PdkNotFound:
            raise unittest.SkipTest("gf180mcu PDK not available")

    def test_nominal_point_is_consistent_and_shows_flicker(self):
        with tempfile.TemporaryDirectory() as d:
            run = r.run_local_control("t", "nominal", self.pdk, Path(d))
        self.assertEqual(run.error, "")
        res = run.res
        self.assertLess(abs(res.integrated_out_v / run.totals["onoise_total"] - 1), r.TOL_ONOISE_REL)
        # 1/f rise: density at 10 Hz is much higher than at 100 kHz.
        self.assertGreater(res.spot_nv[10.0], 10 * res.spot_nv[1e5])
        self.assertTrue(res.fit.flicker_visible)

    def test_audit_finds_flicker_parameters(self):
        a = r.audit_pdk(self.pdk)
        self.assertTrue(all(a.sections_with_noise_corner.values()))
        for m in r.AUDIT_MODELS:
            self.assertIn(m, a.models)
            self.assertIn("1", a.models[m]["fnoimod"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
