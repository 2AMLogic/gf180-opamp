#!/usr/bin/env python3
"""Regression tests for the ibias / CL sensitivity experiment (issue #114).

No simulator needed: extraction is checked on synthetic data with analytic
answers, substitutions on the committed bench text, and the source guard on the
driver source.

    python3 -m pytest sim/ibias-cl-sensitivity
"""

from __future__ import annotations

import math
import re
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_ibias_cl_sensitivity as r  # noqa: E402

REPO = HERE.parents[1]
PDK = r.Pdk(Path("/x/gf180mcuD"), "gf180mcuD", "test")


def two_pole(gbw_hz=10e6, p2_hz=40e6, a0=1e4):
    """A0 / ((1+s/p1)(1+s/p2)), p1 = GBW/A0: GBW ~ gbw_hz; PM = 90 - atan(GBW/p2) (approx)."""
    f = np.logspace(-1, 9, 1201)
    p1 = gbw_hz / a0
    h = a0 / ((1 + 1j * f / p1) * (1 + 1j * f / p2_hz))
    return f, h


class Extraction(unittest.TestCase):
    def test_gbw_pm_known_poles(self):
        f, h = two_pole()
        m = r.g.extract_metrics(f, h)
        self.assertTrue(m.valid, m.reason)
        flat = r.flatten("ac", m)
        # |H(j f)| = 1 at f ~ gbw*(1 - small); PM = 90 - atan(f/p2) within the 2nd-pole shift.
        self.assertAlmostEqual(flat["gbw_mhz"], 9.7, delta=0.4)
        self.assertAlmostEqual(flat["pm_deg"], 90 - math.degrees(math.atan(flat["gbw_mhz"] * 1e6 / 40e6)), delta=0.3)
        self.assertAlmostEqual(flat["gain_db"], 80.0, delta=0.05)

    def test_lower_second_pole_lowers_pm(self):
        a = r.flatten("ac", r.g.extract_metrics(*two_pole(p2_hz=40e6)))
        b = r.flatten("ac", r.g.extract_metrics(*two_pole(p2_hz=20e6)))
        self.assertLess(b["pm_deg"], a["pm_deg"])

    def test_invalid_ac_is_flagged_not_reported(self):
        f = np.logspace(-1, 9, 5)
        m = r.g.extract_metrics(f, np.ones(5, dtype=complex))
        flat = r.flatten("ac", m)
        self.assertFalse(flat["valid"])
        self.assertNotIn("gbw_mhz", flat)

    def test_power_from_synthetic_vectors(self):
        vec = {"sweep": np.array([10e-6, 11e-6]), "i(vdd)": np.array([-80e-6, -81e-6]),
               "v(vdd)": np.array([3.3, 3.3]), "v(vout)": np.array([1.65, 1.65])}
        m = r.s.extract_power(vec, 3.3, 10e-6)
        flat = r.flatten("power", m)
        self.assertAlmostEqual(flat["power_uw"], 80e-6 * 3.3 * 1e6, places=6)
        self.assertAlmostEqual(flat["idd_ua"], 80.0, places=6)

    def test_power_invalid_when_sweep_start_is_not_ibias(self):
        vec = {"sweep": np.array([8e-6, 9e-6]), "i(vdd)": np.array([-80e-6, -81e-6]),
               "v(vdd)": np.array([3.3, 3.3]), "v(vout)": np.array([1.65, 1.65])}
        self.assertFalse(r.flatten("power", r.s.extract_power(vec, 3.3, 10e-6))["valid"])
        self.assertTrue(r.flatten("power", r.s.extract_power(vec, 3.3, 8e-6))["valid"])

    def test_slew_from_synthetic_follower(self):
        t = np.arange(0, r.s.SLEW_TSTOP + 1e-9, 1e-9)
        vin = np.where((t >= r.s.SLEW_RISE_T) & (t < r.s.SLEW_FALL_T), 2.15, 1.15)
        lo, hi, slope = 1.15, 2.15, 12.0  # V/us
        vo = np.full_like(t, lo)
        up = (t >= r.s.SLEW_RISE_T) & (t < r.s.SLEW_FALL_T)
        vo[up] = np.minimum(hi, lo + (t[up] - r.s.SLEW_RISE_T) * slope * 1e6)
        dn = t >= r.s.SLEW_FALL_T
        vo[dn] = np.maximum(lo, hi - (t[dn] - r.s.SLEW_FALL_T) * slope * 1e6)
        flat = r.flatten("slew", r.s.extract_slew({"time": t, "v(vout)": vo, "v(vinp)": vin}))
        self.assertTrue(flat["valid"])
        self.assertAlmostEqual(flat["slew_vus"], 12.0, delta=0.05)

    def test_slew_output_that_never_follows_is_invalid(self):
        t = np.arange(0, r.s.SLEW_TSTOP + 1e-9, 1e-9)
        vin = np.where((t >= r.s.SLEW_RISE_T) & (t < r.s.SLEW_FALL_T), 2.15, 1.15)
        flat = r.flatten("slew", r.s.extract_slew({"time": t, "v(vout)": np.full_like(t, 1.65), "v(vinp)": vin}))
        self.assertFalse(flat["valid"])


class Substitution(unittest.TestCase):
    def test_ibias_and_cl_hit_exactly_once_in_the_committed_benches(self):
        for fig, p in r.BENCH.items():
            text = p.read_text()
            self.assertEqual(len(r._IBIAS_RE.findall(text)), 1, fig)
            self.assertIn("Ibias vdd ibias dc 8e-06", r.set_ibias(text, 8e-6))
        for fig in ("ac", "slew"):
            out = r.set_cl(r.BENCH[fig].read_text(), 10e-12)
            self.assertEqual(len(re.findall(r"^CL vout 0 1e-11$", out, re.M)), 1)

    def test_power_bench_has_no_load_to_substitute(self):
        with self.assertRaises(RuntimeError):
            r.set_cl(r.BENCH["power"].read_text(), 1e-12)

    def test_substitution_errors_unless_exactly_one_match(self):
        tb = r.BENCH["ac"].read_text()
        for bad in (tb.replace("Ibias vdd ibias dc 10u", "* gone"), tb + "\nIbias vdd ibias dc 5u\n"):
            with self.assertRaises(RuntimeError):
                r.set_ibias(bad, 9e-6)
        for bad in (tb.replace("CL vout 0 2p", "* gone"), tb + "\nCL vout 0 1p\n"):
            with self.assertRaises(RuntimeError):
                r.set_cl(bad, 4e-12)

    def test_committed_benches_unchanged_by_substitution(self):
        before = {k: p.read_text() for k, p in r.BENCH.items()}
        r.set_ibias(before["ac"], 12e-6)
        r.set_cl(before["ac"], 1e-12)
        self.assertEqual(before, {k: p.read_text() for k, p in r.BENCH.items()})
        self.assertIn("Ibias vdd ibias dc 10u", before["ac"])
        self.assertIn("CL vout 0 2p", before["ac"])


class Requests(unittest.TestCase):
    def test_units_share_the_control_point(self):
        names = [u["name"] for u in r.units()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("ac_ibias10u_cl2p", names)
        self.assertIn("power_ibias10u", names)
        self.assertEqual(sum(1 for u in r.units() if u["fig"] == "ac"), len(r.IBIAS_SWEEP_A) + len(r.CL_SWEEP_F) - 1)

    def test_request_is_exactly_the_named_points(self):
        for fig in ("ac", "power", "slew"):
            req = r.make_request(fig, Path("/x/tb.spice"), PDK, r.POINTS[fig], 9e-6)
            c = req["corners"]
            procs = [p["name"] for p in c["process"]]
            cells = {(p, t, v) for p in procs for t in c["temperature_c"] for v in c["supply_v"]["vdd"]}
            ex = {(e["process"], e["temperature_c"], e["supply_v"]["vdd"]) for e in req["exclude"]}
            self.assertEqual(cells - ex, set(r.POINTS[fig]), fig)
            self.assertEqual(c["supply_v"]["vcm"], [round(v / 2, 6) for v in c["supply_v"]["vdd"]])

    def test_power_analysis_follows_ibias(self):
        req = r.make_request("power", Path("/x/tb.spice"), PDK, r.POINTS["power"], 8e-6)
        self.assertTrue(req["analysis"]["args"].startswith("Ibias 8e-06"))

    def test_corner_tuples_are_in_the_committed_grid(self):
        for pts in r.POINTS.values():
            for proc, t, v in pts:
                self.assertIn(proc, r.mc._GAIN.CORNERS)
                self.assertIn(t, r.mc._GAIN.TEMPS_C)
                self.assertIn(v, r.mc._GAIN.SUPPLIES_V)


class Control(unittest.TestCase):
    GAIN_MD = "| `typical` | 27 | 3.30 | 95.99 | 13.224 | 59.49 | ok | ok | ok | FAIL | |\n"
    SSP_MD = "| `typical` | 27 | 3.30 | 273.66 ok | 82.93 | 15.1 | 15.0 | 15.00 ok | 2.9 ok | 3.1 | 0.2 |  |\n"

    def test_row_parsers(self):
        self.assertEqual(r.reference_gain(self.GAIN_MD, ("typical", 27.0, 3.30)), (95.99, 13.224, 59.49))
        self.assertEqual(r.reference_ssp(self.SSP_MD, ("typical", 27.0, 3.30)), (273.66, 15.0))
        self.assertIsNone(r.reference_gain(self.GAIN_MD, ("ss", 125.0, 2.97)))

    def test_control_passes_and_fails(self):
        key = r.NOMINAL
        r.POINTS_BAK = r.POINTS
        try:
            r.POINTS = {"ac": [key], "power": [key], "slew": [key]}
            ok = {
                "ac": {key: {"valid": True, "gain_db": 95.99, "gbw_mhz": 13.2237, "pm_deg": 59.495}},
                "power": {key: {"valid": True, "power_uw": 273.659}},
                "slew": {key: {"valid": True, "slew_vus": 15.005}},
            }
            refs = {"gain-gbw-pm": self.GAIN_MD, "power": self.SSP_MD, "slew": self.SSP_MD}
            self.assertEqual(r.control_failures(ok, refs), [])
            ok["ac"][key]["pm_deg"] = 57.0
            self.assertEqual(len(r.control_failures(ok, refs)), 1)
            ok["slew"][key] = {"valid": False, "reason": "x"}
            self.assertEqual(len(r.control_failures(ok, refs)), 2)
        finally:
            r.POINTS = r.POINTS_BAK

    def test_committed_records_contain_every_control_point(self):
        recs = r.selected_records(REPO)
        gmd = (REPO / recs["gain-gbw-pm"]).read_text()
        smd = (REPO / recs["power"]).read_text()
        for p in r.POINTS["ac"]:
            self.assertIsNotNone(r.reference_gain(gmd, p), p)
        for p in r.POINTS["power"]:
            self.assertIsNotNone(r.reference_ssp(smd, p), p)
        self.assertIsNotNone(r.reference_gain(gmd, r.mc.PM_BINDING))
        self.assertAlmostEqual(r.reference_gain(gmd, r.mc.PM_BINDING)[2], 57.34)
        self.assertAlmostEqual(r.reference_ssp(smd, r.mc.POWER_BINDING)[0], 310.98)
        self.assertAlmostEqual(r.reference_ssp(smd, r.mc.SLEW_BINDING)[1], 14.51)


class SourceGuard(unittest.TestCase):
    def setUp(self):
        self.src = (HERE / "run_ibias_cl_sensitivity.py").read_text()
        # docstring prose may mention the rule; check code only
        self.code = re.sub(r'^"""[\s\S]*?"""', "", self.src, count=1)

    def test_driver_never_launches_a_simulator_or_a_local_grid(self):
        for pat in (r"\bsubprocess\b", r"\bPopen\b", r"os\.system", r"ngspice\s+-b", r"multiprocessing",
                    r"ThreadPool", r"concurrent\.futures", r"xargs", r"nohup", r"setsid"):
            self.assertIsNone(re.search(pat, self.code), pat)

    def test_no_local_fallback_on_submit_failure(self):
        # "local" appears only in the single-unit smoke probe
        self.assertEqual(len(re.findall(r'"local"', self.code)), 1)
        self.assertIn("def smoke", self.code.split('"local"')[0])
        self.assertIn("return 2", self.code)
        self.assertIn("NO RECORD WRITTEN", self.code)

    def test_driver_edits_no_committed_file(self):
        self.assertNotIn("spec/", self.code.replace("spec/target-spec", ""))
        self.assertNotIn("selection.json\").write", self.code)


class Fingerprint(unittest.TestCase):
    def test_fingerprint_covers_sweeps_and_benches(self):
        texts = {f: p.read_text() for f, p in r.BENCH.items()}
        a = r.mc.fingerprint(texts)
        self.assertRegex(a, r"^[0-9a-f]{64}$")
        self.assertEqual(a, r.mc.fingerprint(texts))
        self.assertNotEqual(a, r.mc.fingerprint({**texts, "ac": texts["ac"].replace("CL vout 0 2p", "CL vout 0 3p")}))


class AppendOnly(unittest.TestCase):
    def test_existing_record_paths_are_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "records").mkdir()
            (base / "records" / "R.md").write_text("x")
            with self.assertRaises(FileExistsError):
                r.claim_record_paths(base, "R", plots=False)


class RecordRender(unittest.TestCase):
    def test_record_has_tables_no_verdict_and_reproduction_command(self):
        from datetime import datetime, timezone
        vals = {}
        for u in r.units():
            pts = {}
            for p in r.POINTS[u["fig"]]:
                if u["fig"] == "ac":
                    pts[p] = {"valid": True, "gain_db": 95.0, "gbw_mhz": 12.0 * u["ibias_a"] / 1e-5, "pm_deg": 59.0}
                elif u["fig"] == "power":
                    pts[p] = {"valid": True, "power_uw": 270.0, "idd_ua": 80.0}
                else:
                    pts[p] = {"valid": True, "slew_vus": 15.0, "slew_rise_vus": 15.0, "slew_fall_vus": 16.0}
            vals[u["name"]] = pts
        texts = {f: p.read_text() for f, p in r.BENCH.items()}
        md = r.build_record(record="R", stamp=datetime(2026, 1, 1, tzinfo=timezone.utc), pdk=PDK, ngspice="n",
                            klt_ver="k", vals=vals, ctrl_bad=[], refs={"gain-gbw-pm_id": "a/A.md", "slew_id": "b/B.md"},
                            backend_lines=["- **Execution**: x"], dut_sha="0" * 64, texts=texts)
        self.assertIn("### GBW and PM vs ibias", md)
        self.assertIn("### GBW and PM vs CL", md)
        self.assertIn("### Quiescent power vs ibias", md)
        self.assertIn("### Slew rate vs ibias", md)
        self.assertIn("python3 sim/ibias-cl-sensitivity/run_ibias_cl_sensitivity.py", md)
        self.assertIn("+20.0 %", md)  # 12 uA GBW vs the 10 uA control
        self.assertNotRegex(md, r"\b(PASS|FAIL)\b")


if __name__ == "__main__":
    unittest.main()
