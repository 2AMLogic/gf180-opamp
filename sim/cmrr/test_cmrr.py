#!/usr/bin/env python3
"""Regression tests for the CMRR experiment (issue #39).

The unit tests need no simulator: extraction is checked against synthetic
complex responses with known Ad / Acm, and the source/request guards by
mutating the committed testbench / DUT text in memory. `SimTests` runs local
nominal single units (one point each) and is skipped when klt, ngspice or the
PDK is unavailable; it never runs the grid.

    python3 sim/cmrr/test_cmrr.py          # or: python3 -m unittest
"""

from __future__ import annotations

import hashlib
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_cmrr as r  # noqa: E402
from harness import PdkNotFound, find_pdk  # noqa: E402

FREQ = np.logspace(math.log10(r.AC_FSTART), math.log10(r.AC_FSTOP), r.N_FREQ)
S = 1j * FREQ


def ad_true(a0=6e4, p1=200.0, p2=40e6):
    return a0 / ((1 + S / p1) * (1 + S / p2))


def acm_true(c0=0.6, p1=200.0, z=None):
    h = c0 / (1 + S / p1)
    if z is not None:
        h = h * (1 + S / z)
    return h


def vecs(ad, acm, vp, vn):
    vp = np.full(FREQ.shape, vp, dtype=complex)
    vn = np.full(FREQ.shape, vn, dtype=complex)
    vd, vc = vp - vn, (vp + vn) / 2
    return {"frequency": FREQ.astype(complex), "v(vinp)": vp, "v(vinn)": vn, "v(vout)": ad * vd + acm * vc}


def pair(ad, acm, cm=(1.0, 1.0)):
    return vecs(ad, acm, 0.5, -0.5), vecs(ad, acm, *cm)


FLOOR = 1e-12


class ExtractionTests(unittest.TestCase):
    def test_recovers_known_cmrr(self):
        ad, acm = ad_true(), acm_true()
        dm, cm = pair(ad, acm)
        pt = r.extract_cmrr(FREQ, dm, FREQ, cm, FLOOR)
        self.assertTrue(pt.valid, pt.reason)
        want = 20 * math.log10(6e4 / 0.6)  # 100 dB
        self.assertAlmostEqual(pt.cmrr.dc_db, want, delta=1e-3)
        self.assertGreater(pt.cmrr.dc_db, 0)  # rejection is a positive dB figure
        self.assertTrue(pt.cmrr.plateau_ok)
        self.assertFalse(pt.cmrr.lower_bound)
        np.testing.assert_allclose(pt.ad, ad, rtol=1e-12)
        np.testing.assert_allclose(pt.acm, acm, rtol=1e-9)
        # spot value: exact curve at 1 kHz (same pole in Ad and Acm until p2)
        f = 1e3
        exact = 20 * math.log10(abs((6e4 / (1 + 1j * f / 40e6)) / 0.6))
        self.assertAlmostEqual(pt.cmrr.spot_db[1e3], exact, delta=1e-3)

    def test_differential_normalisation_uses_actual_vd(self):
        # The dm run is +-0.5 V: Ad must come out per volt of DIFFERENTIAL input.
        ad, acm = ad_true(a0=1e4), acm_true(c0=1.0)
        dm, cm = pair(ad, acm)
        pt = r.extract_cmrr(FREQ, dm, FREQ, cm, FLOOR)
        self.assertAlmostEqual(pt.ad_dc_db, 80.0, delta=1e-3)
        self.assertAlmostEqual(pt.cmrr.dc_db, 80.0, delta=1e-3)

    def test_value_at_unity_gain_is_interpolated(self):
        ad, acm = ad_true(), acm_true()
        pt = r.extract_cmrr(FREQ, *pair(ad, acm)[:1], FREQ, pair(ad, acm)[1], FLOOR)
        fu = pt.fu_hz
        exact = float(20 * np.log10(np.abs(ad_true() / acm_true())[0]))
        self.assertTrue(1e6 < fu < 1e8)
        cur = 20 * np.log10(np.abs(ad / acm))
        self.assertAlmostEqual(pt.cmrr.at_fu_db, float(np.interp(math.log10(fu), np.log10(FREQ), cur)), delta=1e-9)
        self.assertLess(pt.cmrr.at_fu_db, exact)

    def test_interp_db_is_log_linear_and_bounded(self):
        f = np.array([1.0, 10.0, 100.0])
        y = np.array([0.0, 20.0, 40.0])
        self.assertAlmostEqual(r.interp_db(f, y, math.sqrt(10)), 10.0)
        with self.assertRaises(r.ExtractionError):
            r.interp_db(f, y, 1e3)

    def test_unequal_cm_drive_is_rejected_but_solvable(self):
        ad, acm = ad_true(), acm_true()
        dm, cm = pair(ad, acm, cm=(1.0, 0.99))
        pt = r.extract_cmrr(FREQ, dm, FREQ, cm, FLOOR)
        self.assertFalse(pt.valid)
        self.assertIn("unequal CM drive", pt.reason)
        # The joint solve itself still separates the two gains exactly ...
        a, c = r.solve_ad_acm(r.Excitation.from_vec(dm), r.Excitation.from_vec(cm))
        np.testing.assert_allclose(c, acm, rtol=1e-6)
        # ... whereas the naive ratio would be wrong by orders of magnitude.
        x = r.Excitation.from_vec(cm)
        naive = x.vout / x.vc
        self.assertGreater(abs(naive[0]) / abs(acm[0]), 100)

    def test_wrong_differential_excitation_rejected(self):
        ad, acm = ad_true(), acm_true()
        dm = vecs(ad, acm, 0.5, -0.4)  # vd = 0.9
        pt = r.extract_cmrr(FREQ, dm, FREQ, pair(ad, acm)[1], FLOOR)
        self.assertFalse(pt.valid)
        self.assertIn("differential excitation", pt.reason)

    def test_mismatched_axes_and_missing_points(self):
        ad, acm = ad_true(), acm_true()
        dm, cm = pair(ad, acm)
        pt = r.extract_cmrr(FREQ, dm, FREQ * 1.01, cm, FLOOR)
        self.assertFalse(pt.valid)
        self.assertIn("different frequency axes", pt.reason)
        short = {k: v[:-3] for k, v in dm.items()}
        pt = r.extract_cmrr(FREQ[:-3], short, FREQ[:-3], {k: v[:-3] for k, v in cm.items()}, FLOOR)
        self.assertFalse(pt.valid)
        decreasing = FREQ[::-1].copy()
        pt = r.extract_cmrr(decreasing, dm, decreasing, cm, FLOOR)
        self.assertFalse(pt.valid)

    def test_non_finite_data_rejected(self):
        ad, acm = ad_true(), acm_true()
        dm, cm = pair(ad, acm)
        cm["v(vout)"] = cm["v(vout)"].copy()
        cm["v(vout)"][7] = complex(float("nan"), 0)
        self.assertFalse(r.extract_cmrr(FREQ, dm, FREQ, cm, FLOOR).valid)

    def test_missing_vector_rejected(self):
        ad, acm = ad_true(), acm_true()
        dm, cm = pair(ad, acm)
        del cm["v(vinn)"]
        self.assertFalse(r.extract_cmrr(FREQ, dm, FREQ, cm, FLOOR).valid)

    def test_zero_common_mode_gain_is_a_lower_bound_not_infinite(self):
        ad = ad_true()
        dm, cm = pair(ad, np.zeros_like(ad))
        pt = r.extract_cmrr(FREQ, dm, FREQ, cm, 1e-9)
        self.assertTrue(pt.valid, pt.reason)
        self.assertTrue(pt.cmrr.lower_bound)
        self.assertTrue(np.all(np.isfinite(pt.cmrr.curve_db)))
        self.assertAlmostEqual(pt.cmrr.dc_db, 20 * math.log10(6e4 / 1e-9), delta=1e-3)

    def test_near_zero_common_mode_gain_at_one_frequency(self):
        ad, acm = ad_true(), acm_true().copy()
        acm[150] = 1e-20
        pt = r.extract_cmrr(FREQ, *pair(ad, acm)[:1], FREQ, pair(ad, acm)[1], 1e-12)
        self.assertTrue(pt.valid, pt.reason)
        self.assertTrue(np.isfinite(pt.cmrr.curve_db[150]))
        self.assertLessEqual(pt.cmrr.curve_db[150], 20 * math.log10(abs(ad[150]) / 1e-12) + 1e-9)

    def test_non_flat_plateau_reported_unavailable(self):
        ad = ad_true()
        acm = acm_true(z=0.3)  # CM gain already rising in the lowest decade
        pt = r.extract_cmrr(FREQ, *pair(ad, acm)[:1], FREQ, pair(ad, acm)[1], FLOOR)
        self.assertTrue(pt.valid, pt.reason)
        self.assertFalse(pt.cmrr.plateau_ok)
        self.assertTrue(math.isnan(pt.cmrr.dc_db))
        self.assertTrue(math.isfinite(pt.cmrr.low_db))

    def test_invalid_differential_response_rejected(self):
        ad = -ad_true()  # wrong polarity
        pt = r.extract_cmrr(FREQ, *pair(ad, acm_true())[:1], FREQ, pair(ad, acm_true())[1], FLOOR)
        self.assertFalse(pt.valid)
        self.assertIn("differential response invalid", pt.reason)

    def test_floor_must_be_positive(self):
        with self.assertRaises(r.ExtractionError):
            r.summarise_rejection(FREQ, ad_true(), acm_true(), 1e7, 0.0)

    def test_worst_case_picks_lowest(self):
        a = r.extract_cmrr(FREQ, *pair(ad_true(), acm_true(0.6))[:1], FREQ, pair(ad_true(), acm_true(0.6))[1], FLOOR)
        b = r.extract_cmrr(FREQ, *pair(ad_true(), acm_true(6.0))[:1], FREQ, pair(ad_true(), acm_true(6.0))[1], FLOOR)
        ka, kb = ("typical", 27.0, 3.3), ("ss", 125.0, 2.97)
        v, k, miss = r.worst_case({ka: a.cmrr, kb: b.cmrr}, "dc")
        self.assertEqual(k, kb)
        self.assertAlmostEqual(v, 80.0, delta=1e-3)
        self.assertEqual(miss, 0)


class ReportTests(unittest.TestCase):
    def _report(self, tmp: Path, keys, *, drop=None, dup=None):
        corners = []
        raw = tmp / "w.raw"
        names = ["frequency", "v(vinp)", "v(vinn)", "v(vout)"]
        dm, _ = pair(ad_true(), acm_true())
        lines = ["Title: t", "Plotname: AC Analysis", "Flags: complex", f"No. Variables: {len(names)}",
                 f"No. Points: {len(FREQ)}", "Variables:"]
        lines += [f"\t{i}\t{n}\tvoltage" for i, n in enumerate(names)] + ["Values:"]
        for i in range(len(FREQ)):
            for j, n in enumerate(names):
                v = dm[n][i]
                lines.append((f" {i}" if j == 0 else "") + f"\t{v.real:.15e},{v.imag:.15e}")
        raw.write_text("\n".join(lines) + "\n")
        log = tmp / "n.log"
        log.write_text("v(vout) = 1.65e+00\nv(vinp) = 1.65e+00\nv(vinn) = 1.65e+00\n"
                       + "".join(f"@m.xdut.{d}.m0[vds] = 1.0e+00\n@m.xdut.{d}.m0[vdsat] = 1.0e-01\n" for d in r.DEVICES))
        for k in keys:
            if k == drop:
                continue
            c = {"process": k[0], "temperature_c": k[1], "supply_v": {"vdd": k[2], "vcm": k[2] / 2},
                 "artifacts": {"raw": str(raw), "log": str(log)}, "diagnostics": []}
            corners.append(c)
            if k == dup:
                corners.append(dict(c))
        return {"corners": corners}

    def test_complete_grid_parses(self):
        with tempfile.TemporaryDirectory() as d:
            keys = [("typical", 27.0, 3.3)]
            rep = self._report(Path(d), keys)
            # vcm in the fake report is 1.65 for 3.3 V
            out, probs = r.analyse_mode_report(rep, keys, "dm", ("v(vout)", "v(vinp)", "v(vinn)"))
            self.assertEqual(probs, [])
            self.assertIn(keys[0], out)
            self.assertEqual(out[keys[0]]["op"].flags, [])

    def test_missing_and_duplicate_points_are_failures(self):
        keys = r.expected_keys(r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(len(keys), 45)
        with tempfile.TemporaryDirectory() as d:
            rep = self._report(Path(d), keys, drop=keys[3], dup=keys[5])
            _, probs = r.analyse_mode_report(rep, keys, "cm", ("v(vout)", "v(vinp)", "v(vinn)"))
            self.assertTrue(any("missing result" in p for p in probs))
            self.assertTrue(any("duplicate result" in p for p in probs))

    def test_op_parsing_and_rejection(self):
        txt = "v(vout) = 2.900000e+00\nv(vinp) = 1.65e+00\nv(vinn) = 2.9e+00\n" + "".join(
            f"@m.xdut.{d}.m0[vds] = 1.0e-01\n@m.xdut.{d}.m0[vdsat] = 2.0e-01\n" for d in r.DEVICES)
        vals = r.parse_op_log(txt)
        self.assertAlmostEqual(vals["v(vout)"], 2.9)
        op = r.check_op(vals, 1.65, "x")
        self.assertTrue(op.problems)  # output not at VCM: saturated / differently biased
        self.assertEqual(len(op.flags), len(r.DEVICES))
        self.assertTrue(r.check_op({}, 1.65, "x").problems)  # not returned

    def test_modes_must_share_the_operating_point(self):
        k = ("typical", 27.0, 3.3)
        mk = lambda v: {k: {"op": r.OpCheck(v, 1.65, {}, [], [])}}  # noqa: E731
        _, bad = r.op_agreement({"dm": mk(1.65), "cm": mk(1.65)}, [k])
        self.assertEqual(bad, [])
        _, bad = r.op_agreement({"dm": mk(1.65), "cm": mk(1.66)}, [k])
        self.assertTrue(bad)


class GuardTests(unittest.TestCase):
    def tb(self):
        return r.TESTBENCH.read_text()

    def test_committed_testbench_passes(self):
        self.assertEqual(r.guard_testbench(self.tb()), [])

    def test_hand_declared_transistor_rejected(self):
        self.assertTrue(r.guard_testbench(self.tb() + "\nM9 a b c d nfet_03v3 W=1u L=1u\n"))
        self.assertTrue(r.guard_testbench(self.tb() + "\nX9 a b c d nfet_03v3 W=1u L=1u\n"))

    def test_missing_dut_include_rejected(self):
        self.assertTrue(r.guard_testbench(self.tb().replace(".include 'opamp_two_stage.dut.spice'", "")))

    def test_bias_load_servo_and_equal_drive_lines_required(self):
        base = self.tb()
        for line in ("CL vout 0 2p", "Ibias vdd ibias dc 10u", "Esv vinn vnac vsv 0 1", "Esb vsb 0 vout 0 1",
                     "Vnac vnac 0 dc 0 ac {acn}", "Vcm vinp 0 dc 1.65 ac {acp}", "Csv vsv 0 {csv}"):
            self.assertTrue(r.guard_testbench(base.replace(line, "")), line)
        self.assertTrue(r.guard_testbench(base.replace("CL vout 0 2p", "CL vout 0 1p")))
        self.assertTrue(r.guard_testbench(base.replace("dc 10u", "dc 20u")))
        # The gain bench's AC ground on vinn would make CM drive impossible.
        self.assertTrue(r.guard_testbench(base + "\nCfb vinn 0 {csv}\n"))

    def test_dut_port_order_is_pinned(self):
        bad = self.tb().replace("Xdut vdd 0 vinp vinn vout ibias", "Xdut vdd 0 vinn vinp vout ibias")
        self.assertTrue(r.guard_testbench(bad))

    def test_altered_dut_rejected(self):
        dut = r.load_dut_text()
        self.assertEqual(r.G.guard_dut(dut), [])
        self.assertTrue(r.G.guard_dut(dut.replace("L=1u", "L=2u", 1)))


class RequestAndMaterialiseTests(unittest.TestCase):
    PDK = type("P", (), {"variant": "gf180mcuD", "path": Path("/pdks/x/gf180mcuD"),
                         "design_include": None})()

    def test_request_shape(self):
        req = r.ac_request(Path("/x/tb.spice"), self.PDK, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(req["analysis"]["kind"], "ac")
        self.assertTrue(req["analysis"]["args"].startswith(f"dec {r.AC_PPD:g} {r.AC_FSTART:g} {r.AC_FSTOP:g}"))
        self.assertIn("\nop\nprint v(vout)", req["analysis"]["args"])
        self.assertTrue(req["analysis"]["args"].endswith("setplot ac1"))
        self.assertEqual(req["measurements"], [])
        self.assertEqual(req["corners"]["supply_v"]["vcm"], [1.485, 1.65, 1.815])
        self.assertEqual([p["name"] for p in req["corners"]["process"]], r.CORNERS)
        self.assertNotIn("pdk_root", req["models"])  # the fleet uses its own install
        loc = r.ac_request(Path("/x/tb.spice"), self.PDK, ["typical"], [27.0], [3.3], local=True)
        self.assertEqual(loc["models"]["pdk_root"], "/pdks/x")

    def test_modes_are_unit_differential_and_equal_common_mode(self):
        self.assertEqual(r.MODES["dm"]["acp"] - r.MODES["dm"]["acn"], 1.0)
        self.assertEqual(r.MODES["dm"]["acp"] + r.MODES["dm"]["acn"], 0.0)
        self.assertEqual(r.MODES["cm"]["acp"], r.MODES["cm"]["acn"])

    def test_param_line_rewrite(self):
        keys = r.param_keys(r.TESTBENCH.read_text())
        self.assertEqual(sorted(keys), sorted(["acp", "acn", "rsv", "csv"]))
        self.assertEqual(r.param_line(keys, r.with_servo(r.MODES["cm"])), ".param acp=1 acn=1 rsv=1000000000 csv=1000000000")
        with self.assertRaises(RuntimeError):
            r.param_line(keys, {"acp": 1})

    def test_materialise_writes_only_the_param_line(self):
        try:
            pdk = find_pdk()
        except PdkNotFound:
            self.skipTest("PDK not available")
        with tempfile.TemporaryDirectory() as d:
            tb = r.materialise(Path(d), pdk, r.with_servo(r.MODES["cm"]))
            got = tb.read_text().splitlines()
            src = r.TESTBENCH.read_text().splitlines()
            diff = [(a, b) for a, b in zip(src, got) if a != b]
            self.assertEqual(len(got), len(src))
            self.assertEqual(len(diff), 3)  # two includes + .param
            self.assertTrue(any(b.startswith(".param acp=1 acn=1") for _, b in diff))

    def test_control_mutation_never_touches_the_production_file(self):
        before = hashlib.sha256(r.G.DUT_EXPORT.read_bytes()).hexdigest()
        base = r.load_dut_text()
        mut = r.mutate_instance(base, *r.CONTROL_MIRROR)
        after = hashlib.sha256(r.G.DUT_EXPORT.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        changed = [(a, b) for a, b in zip(base.splitlines(), mut.splitlines()) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0][0].startswith("XM3 "))
        self.assertIn("W=5.4u", changed[0][1])
        self.assertTrue(r.G.guard_dut(mut))  # not the production DUT ...
        self.assertEqual(r.G.guard_dut(mut, allow_missing=("xm3",)), [])  # ... only XM3 differs
        with self.assertRaises(RuntimeError):
            r.mutate_instance(base, "XM3", "W", "7u", "8u")

    def test_records_are_append_only(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            paths = r.G.claim_record_paths(base, "20990101-000000-abcdef0")
            paths["record"].parent.mkdir(parents=True)
            paths["record"].write_text("x")
            with self.assertRaises(FileExistsError):
                r.G.claim_record_paths(base, "20990101-000000-abcdef0")
            r.G.claim_record_paths(base, "20990101-000001-abcdef0")  # a fresh id is fine


class SimTests(unittest.TestCase):
    """Local nominal single units (never the grid)."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("klt") is None or shutil.which("ngspice") is None:
            raise unittest.SkipTest("klt/ngspice not available")
        try:
            cls.pdk = find_pdk()
        except PdkNotFound:
            raise unittest.SkipTest("gf180mcu PDK not available")

    def test_nominal_pair_is_valid_with_equal_drive(self):
        with tempfile.TemporaryDirectory() as d:
            pt, runs = r.cmrr_pair(self.pdk, Path(d), "t", "nominal", r.FLOOR_MIN)
        self.assertTrue(pt.valid, pt.reason)
        self.assertLess(pt.cm_leak, 1e-12)
        self.assertTrue(pt.cmrr.plateau_ok)
        self.assertGreater(pt.cmrr.dc_db, 60)
        for run in runs:
            self.assertEqual(run.op.problems, [])

    def test_unequal_drive_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            pt, _ = r.cmrr_pair(self.pdk, Path(d), "u", "unequal", r.FLOOR_MIN, cm_params={"acp": 1.0, "acn": 0.99})
        self.assertFalse(pt.valid)
        self.assertIn("unequal CM drive", pt.reason)


if __name__ == "__main__":
    unittest.main(verbosity=1)
