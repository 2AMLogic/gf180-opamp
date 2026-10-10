#!/usr/bin/env python3
"""Regression tests for the CMRR Monte Carlo experiment (issue #61).

The unit tests need no simulator: the control-language tail, the request,
the testbench guard, the log parsing, the per-sample validity checks, the
statistics and the controls are checked on synthetic data and on mutated
copies of the committed testbench. `SimTests` runs local single units at the
nominal point (never a grid, never Monte Carlo) and is skipped when klt,
ngspice or the PDK is unavailable.

    python3 sim/cmrr-mc/test_cmrr_mc.py          # or: python3 -m pytest sim/cmrr-mc -q
"""

from __future__ import annotations

import math
import shutil
import statistics
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_cmrr_mc as m  # noqa: E402
from harness import PdkNotFound, find_pdk, skip_or_fail  # noqa: E402

NOM = ("typical", 27.0, 3.30)


def good_vals(cmrr_dc: float = 90.0, acm_dc: float = 1.0, offset: float = 2e-3, vcm: float = 1.65) -> dict:
    """A self-consistent set of printed scalars for one valid sample."""
    v = {
        "cmc_op1_vout": vcm + offset, "cmc_op1_vinp": vcm, "cmc_offset": offset, "cmc_op_parity": 0.0,
        "cmc_npts": 201.0, "cmc_f0": 0.1, "cmc_f20": 1.0, "cmc_f80": 1e3, "cmc_f100": 1e4, "cmc_f120": 1e5,
        "cmc_f140": 1e6, "cmc_dm_vderr": 1e-13, "cmc_dm_vcerr": 5e-14, "cmc_cm_vcerr": 2e-18, "cmc_cm_leak": 4e-18,
        "cmc_det_min": 1.0, "cmc_acm_min": 1e-4, "cmc_cmrr_spread": 1e-10, "cmc_acm_spread": 1e-4,
        "cmc_ad_spread": 1e-4, "cmc_acm_re0": acm_dc, "cmc_acm_im0": -1e-3, "cmc_fu_hz": 1.3e7,
    }
    ad_dc = cmrr_dc + 20 * math.log10(acm_dc)
    for f in m.FIGS:
        v[f"cmc_cmrr_{f}"] = cmrr_dc if f != "fu" else 64.0
        v[f"cmc_acm_{f}"] = acm_dc if f != "fu" else 6e-4
        v[f"cmc_ad_{f}"] = ad_dc if f != "fu" else 0.0
    return v


def log_of(vals: dict) -> str:
    lines = ["Doing analysis at TEMP = 27.000000", "No. of Data Rows : 201"]
    lines += [f"cmc_fu_hz           =  {vals['cmc_fu_hz']:.5e}"]  # the `meas` line shape
    lines += [f"{k} = {v:.6e}" for k, v in vals.items()]
    return "\n".join(lines) + "\nngspice-46 done\n"


def sample(vals: dict, idx: int = 0, key=NOM) -> m.Sample:
    p, lb = m.validate_sample(vals, key[2] / 2)
    s = m.Sample(key, idx, {"seed": idx}, dict(vals), p, lb)
    if s.valid:
        m.clamp_floor(s)
    return s


def report(points: dict, *, dup: bool = False, drop: int | None = None) -> tuple[dict, dict]:
    """A synthetic klt MC report {key: [vals per sample]} and its log store."""
    corners, logs = [], {}
    for k, vlist in points.items():
        for i, vals in enumerate(vlist):
            if drop is not None and i == drop:
                continue
            path = f"/x/{k[0]}_{k[1]:g}_{k[2]:g}_{i}.log"
            logs[path] = log_of(vals)
            c = {"corner_id": f"{k[0]}/{k[1]:g}/{k[2]:g}/mc{i}", "process": k[0], "temperature_c": k[1],
                 "supply_v": {"vdd": k[2], "vcm": round(k[2] / 2, 6)}, "status": "pass",
                 "monte_carlo": {"sample_index": 0 if dup else i, "seed": 1000 + i, "mismatch_seed": 7,
                                 "process_seed": 9},
                 "artifacts": {"log": path}}
            corners.append(c)
    return {"corners": corners}, logs


class TailAndRequestTests(unittest.TestCase):
    def test_tail_runs_both_sweeps_in_one_deck(self):
        t = m.analysis_tail()
        lines = t.splitlines()
        self.assertEqual(sum(ln.startswith("ac ") for ln in lines), 1, "exactly one extra .ac (the cm sweep)")
        self.assertIn("alter vcm acmag=1", lines)
        self.assertIn("alter vnac acmag=1", lines)
        self.assertLess(lines.index("alter vcm acmag=1"), next(i for i, ln in enumerate(lines) if ln.startswith("ac ")))
        self.assertEqual(lines.count("op"), 2)
        # every scalar is printed, after everything is computed
        prints = [ln.split(None, 1)[1] for ln in lines if ln.startswith("print ")]
        self.assertEqual(prints, m.SCALARS)
        self.assertGreater(lines.index(f"print {m.SCALARS[0]}"), max(i for i, ln in enumerate(lines) if ln.startswith("meas ")))
        # the exact solve uses BOTH sweeps' actual phasors
        self.assertTrue(any("ac1.v(vout)" in ln and "cmc_adv" in ln for ln in lines))
        self.assertTrue(any("ac1.v(vinp)" in ln for ln in lines))
        self.assertIn("setplot ac2", lines)
        for s in m.SCALARS:
            self.assertEqual(s, s.lower(), "ngspice lower-cases printed names")

    def test_spot_indices_land_on_the_sweep(self):
        for f, i in m.SPOTS.values():
            self.assertAlmostEqual(m.C.AC_FSTART * 10 ** (i / m.C.AC_PPD), f, delta=f * 1e-12)
        self.assertEqual(m.BAND, (0, 20))
        self.assertEqual(m.N_FREQ, 201)

    def test_request_shape(self):
        class P:  # minimal Pdk stand-in
            variant = "gf180mcuD"
            path = Path("/pdk/gf180mcuD")

        req = m.request(Path("/w/tb.spice"), P(), m.CORNERS, [27.0], [3.30], n=300)
        self.assertEqual(req["analysis"]["kind"], "ac")
        self.assertTrue(req["analysis"]["args"].startswith(m.AC_ARGS + "\n"))
        self.assertEqual(req["monte_carlo"], {"n": 300, "seed": 45, "vary": "mismatch"})
        self.assertEqual(req["measurements"], [])
        self.assertFalse(req["options"]["waveforms"])
        self.assertTrue(req["options"]["keep_artifacts"])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [3.30], "vcm": [1.65]})
        self.assertEqual([p["name"] for p in req["corners"]["process"]], m.CORNERS)
        self.assertNotIn("pdk_root", req["models"])
        r2 = m.request(Path("/w/tb.spice"), P(), ["typical"], [27.0], [3.30], n=None, expr=("cmc_cmrr_dc",), local=True)
        self.assertNotIn("monte_carlo", r2)
        self.assertEqual(r2["measurements"][0]["expr"], "cmc_cmrr_dc")
        self.assertIn("pdk_root", r2["models"])
        r3 = m.request(Path("/w/tb.spice"), P(), ["typical"], [27.0], [3.30], n=20, vary="process")
        self.assertEqual(r3["monte_carlo"]["vary"], "process")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tb = m.TESTBENCH.read_text()

    def test_committed_bench_passes(self):
        self.assertEqual(m.guard_testbench(self.tb), [])

    def test_circuit_must_equal_the_systematic_bench(self):
        self.assertTrue(m.guard_testbench(self.tb.replace("CL vout 0 2p", "CL vout 0 3p")))
        self.assertTrue(m.guard_testbench(self.tb.replace("Rsv vsb vsv {rsv}", "Rsv vsb vsv 1k")))
        self.assertTrue(m.guard_testbench(self.tb + "Rx vout 0 1meg\n"))

    def test_param_line_rules(self):
        self.assertTrue(m.guard_testbench(self.tb.replace(" sw_stat_mismatch=1", "")))
        self.assertTrue(m.guard_testbench(self.tb.replace("sw_stat_mismatch=1", "sw_stat_mismatch=1 sw_stat_global=1")))
        moved = self.tb.replace(".param acp=0.5 acn=-0.5 rsv=1e9 csv=1e9 sw_stat_mismatch=1\n", "")
        moved = moved.replace(".include 'design.ngspice'",
                              ".param acp=0.5 acn=-0.5 rsv=1e9 csv=1e9 sw_stat_mismatch=1\n.include 'design.ngspice'")
        errs = m.guard_testbench(moved)
        self.assertTrue(any("precedes" in e for e in errs), errs)

    def test_no_transistor_and_circuit_body(self):
        self.assertTrue(m.guard_testbench(self.tb + "M1 a b c d nfet_03v3 W=1u L=1u\n"))
        self.assertTrue(m.guard_testbench(self.tb + ".control\nrun\n.endc\n"))


class ParseAndValidateTests(unittest.TestCase):
    def test_parse_reads_print_and_meas_lines(self):
        vals = good_vals()
        got = m.parse_scalars(log_of(vals))
        for k, v in vals.items():
            self.assertAlmostEqual(got[k], v, delta=abs(v) * 1e-6 + 1e-30, msg=k)
        # last occurrence wins (the explicit print after the `meas` line)
        self.assertEqual(m.parse_scalars("cmc_x = 1.0\ncmc_x   =  2.5e+00\n")["cmc_x"], 2.5)
        self.assertEqual(m.parse_scalars("CMC_Y = 3\n"), {"cmc_y": 3.0})
        self.assertEqual(m.parse_scalars("cmc_z = nan\nfoo = 1\n"), {})

    def test_valid_sample(self):
        p, lb = m.validate_sample(good_vals(), 1.65)
        self.assertEqual(p, [])
        self.assertFalse(lb)

    def test_each_check_bites(self):
        cases = {
            "cmc_op_parity": (2e-6, "same devices"),
            "cmc_dm_vderr": (1e-3, "differential excitation"),
            "cmc_cm_vcerr": (1e-3, "common-mode excitation"),
            "cmc_cm_leak": (1e-2, "unequal CM drive"),
            "cmc_det_min": (0.0, "singular"),
            "cmc_npts": (200.0, "frequency points"),
            "cmc_f100": (1.1e4, "sweep grid"),
            "cmc_op1_vinp": (1.6, "vinp"),
            "cmc_op1_vout": (1.9, "from VCM"),
            "cmc_cmrr_spread": (0.2, "plateau"),
            "cmc_acm_spread": (0.2, "plateau"),
            "cmc_ad_spread": (0.2, "Ad plateau"),
            "cmc_fu_hz": (2e9, "outside the sweep"),
        }
        for key, (bad, needle) in cases.items():
            v = good_vals()
            v[key] = bad
            p, _ = m.validate_sample(v, 1.65)
            self.assertTrue(p and any(needle in x for x in p), f"{key}: {p}")

    def test_missing_or_nonfinite_scalar_is_invalid(self):
        v = good_vals()
        del v["cmc_cmrr_10k"]
        self.assertIn("missing", m.validate_sample(v, 1.65)[0][0])
        v = good_vals()
        v["cmc_acm_dc"] = float("inf")
        self.assertIn("missing", m.validate_sample(v, 1.65)[0][0])

    def test_floor_clamp_is_a_lower_bound_never_a_division_by_zero(self):
        v = good_vals()
        v["cmc_acm_min"] = 0.0
        v["cmc_acm_dc"] = 0.0
        s = sample(v)
        self.assertTrue(s.valid)
        self.assertTrue(s.lower_bound)
        self.assertEqual(s.vals["cmc_acm_dc"], m.FLOOR)
        self.assertAlmostEqual(s.fig("cmrr", "dc"), s.fig("ad", "dc") - 20 * math.log10(m.FLOOR))


class ExtractTests(unittest.TestCase):
    def test_complete_population(self):
        rep, logs = report({NOM: [good_vals(90 + i * 0.1) for i in range(5)]})
        out, probs = m.extract(rep, [NOM], 5, read_log=logs.__getitem__)
        self.assertEqual(probs, [])
        self.assertEqual([s.index for s in out[NOM]], list(range(5)))
        self.assertEqual(out[NOM][3].seeds["seed"], 1003)

    def test_missing_duplicate_and_unexpected(self):
        rep, logs = report({NOM: [good_vals() for _ in range(5)]}, drop=2)
        _, probs = m.extract(rep, [NOM], 5, read_log=logs.__getitem__)
        self.assertTrue(any("expected 0..4" in p for p in probs), probs)
        rep, logs = report({NOM: [good_vals() for _ in range(3)]}, dup=True)
        _, probs = m.extract(rep, [NOM], 3, read_log=logs.__getitem__)
        self.assertTrue(any("duplicate" in p for p in probs), probs)
        rep, logs = report({("ff", 27.0, 3.30): [good_vals()]})
        _, probs = m.extract(rep, [NOM], 1, read_log=logs.__getitem__)
        self.assertTrue(any("unexpected" in p for p in probs), probs)

    def test_invalid_sample_and_missing_log_are_problems(self):
        bad = good_vals()
        bad["cmc_op_parity"] = 1e-3
        rep, logs = report({NOM: [good_vals(), bad]})
        _, probs = m.extract(rep, [NOM], 2, read_log=logs.__getitem__)
        self.assertTrue(any("same devices" in p for p in probs), probs)
        rep, logs = report({NOM: [good_vals()]})
        rep["corners"][0]["artifacts"] = {}
        _, probs = m.extract(rep, [NOM], 1, read_log=logs.__getitem__)
        self.assertTrue(any("no ngspice log" in p for p in probs), probs)


class StatsTests(unittest.TestCase):
    def test_statistics(self):
        cm = [88.0, 90.0, 92.0, 94.0, 96.0]
        acm = [2.0, 1.6, 1.25, 1.0, 0.8]
        ss = [sample(good_vals(c, a)) for c, a in zip(cm, acm)]
        st = m.fig_stats(ss, "dc")
        self.assertEqual(st.n, 5)
        self.assertAlmostEqual(st.mean_db, 92.0)
        self.assertAlmostEqual(st.sigma_db, statistics.stdev(cm))
        self.assertAlmostEqual(st.db_3s, 92.0 - 3 * statistics.stdev(cm))
        self.assertEqual(st.min_db, 88.0)
        self.assertAlmostEqual(st.p5_db, 88.0 + 0.2 * 2.0)  # linear: pos = 4 * 0.05 = 0.2
        ad = statistics.fmean(c + 20 * math.log10(a) for c, a in zip(cm, acm))
        want = ad - 20 * math.log10(statistics.fmean(acm) + 3 * statistics.stdev(acm))
        self.assertAlmostEqual(st.lin_3s, want)
        self.assertLess(st.lin_3s, st.min_db)

    def test_percentile_matches_numpy_linear(self):
        import numpy as np

        xs = [3.0, 1.0, 4.0, 1.5, 9.0, 2.6, 5.0]
        for q in (0, 1, 5, 50, 95, 100):
            self.assertAlmostEqual(m.percentile(xs, q), float(np.percentile(xs, q)))

    def test_worst(self):
        a = {f: m.fig_stats([sample(good_vals(90)), sample(good_vals(91))], f) for f in m.FIGS}
        b = {f: m.fig_stats([sample(good_vals(80)), sample(good_vals(81))], f) for f in m.FIGS}
        v, k = m.worst({NOM: a, ("ss", 27.0, 3.30): b}, "dc", "lin_3s")
        self.assertEqual(k, ("ss", 27.0, 3.30))


class ControlTests(unittest.TestCase):
    def ref(self, dc=90.0, fu=64.0):
        return {NOM: {"dc": dc, "1k": dc, "10k": dc, "100k": dc, "1m": dc, "fu": fu, "fu_hz": 1.3e7}}

    def test_switch_off_passes_on_identical_samples_matching_the_record(self):
        ss = {NOM: [sample(good_vals(90.0), i) for i in range(4)]}
        probs, rows = m.check_switch_off(ss, self.ref())
        self.assertEqual(probs, [])
        self.assertEqual(rows[0]["dev"]["dc"], 0.0)

    def test_switch_off_fails_on_spread_or_deviation(self):
        ss = {NOM: [sample(good_vals(90.0 + i * 1e-3), i) for i in range(4)]}
        probs, _ = m.check_switch_off(ss, self.ref())
        self.assertTrue(any("samples differ" in p for p in probs), probs)
        ss = {NOM: [sample(good_vals(90.05), i) for i in range(2)]}
        probs, _ = m.check_switch_off(ss, self.ref())
        self.assertTrue(any("systematic" in p for p in probs), probs)
        ss = {NOM: [sample(good_vals(90.0), i) for i in range(2)]}
        probs, _ = m.check_switch_off(ss, {})
        self.assertTrue(any("no committed" in p for p in probs), probs)

    def test_imbalance_control(self):
        base = [sample(good_vals(95 - (i % 7), 0.5 + 0.1 * (i % 7)), i) for i in range(50)]
        worse = [sample(good_vals(85 - (i % 7), 1.6 + 0.1 * (i % 7)), i) for i in range(50)]
        probs, info = m.check_imbalance(base, worse)
        self.assertEqual(probs, [])
        self.assertGreater(info["shift"], 0)
        probs, _ = m.check_imbalance(base, base)
        self.assertTrue(probs)

    def test_systematic_reference_reads_the_committed_record(self):
        ref = m.systematic_reference([NOM, ("ss", 27.0, 3.30)])
        self.assertAlmostEqual(ref[NOM]["dc"], 100.64, delta=0.005)
        self.assertAlmostEqual(ref[NOM]["1m"], 94.78, delta=0.005)
        self.assertAlmostEqual(ref[NOM]["fu"], 64.16)
        self.assertAlmostEqual(ref[NOM]["fu_hz"], 13.220e6)
        self.assertIn(("ss", 27.0, 3.30), ref)


class SimTests(unittest.TestCase):
    """Local single units at the nominal point (never a grid, never MC)."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("klt") is None or shutil.which("ngspice") is None:
            skip_or_fail(cls, "klt/ngspice not available")
        try:
            cls.pdk = find_pdk()
        except PdkNotFound:
            skip_or_fail(cls, "gf180mcu PDK not available")

    def unit(self, mismatch: int) -> m.Sample:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            tb = m.materialise(work, self.pdk, mismatch=mismatch)
            req = m.request(tb, self.pdk, ["typical"], [27.0], [3.30], n=None, local=True)
            rep = m.run_klt(req, work / "out", "local", work, env=m.C.local_env(work))
            out, probs = m.extract(m._as_mc(rep), [NOM], 1)
        self.assertEqual(probs, [])
        return out[NOM][0]

    def test_switch_off_unit_reproduces_the_systematic_record(self):
        s = self.unit(0)
        probs, rows = m.check_switch_off({NOM: [s]}, m.systematic_reference([NOM]))
        self.assertEqual(probs, [], rows)
        self.assertEqual(s.vals["cmc_op_parity"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
