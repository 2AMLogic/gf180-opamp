#!/usr/bin/env python3
"""Regression tests for the PSRR Monte Carlo experiment (issue #99).

The unit tests need no simulator: the control-language tail, the request,
the testbench guard, the log parsing, the per-sample validity checks, the
statistics, the DR-6 comparison and the controls are checked on synthetic
data and on mutated copies of the committed testbench. `SimTests` runs local
single units at the nominal point (never a grid, never Monte Carlo) and is
skipped when klt, ngspice or the PDK is unavailable.

    python3 sim/psrr-mc/test_psrr_mc.py          # or: python3 -m pytest sim/psrr-mc -q
"""

from __future__ import annotations

import json
import math
import shutil
import statistics
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_psrr_mc as m  # noqa: E402
from harness import PdkNotFound, find_pdk, measurement_fingerprint, skip_or_fail  # noqa: E402

NOM = m.NOMINAL


def good_vals(pp: float = 100.0, pm: float | None = None, avdd: float = 0.6, avss: float = 0.2,
              offset: float = 2e-3, vcm: float = 1.65) -> dict:
    """A self-consistent set of printed scalars for one valid sample below f_u:
    PSRR+ = pp with |Avdd| = avdd fixes Ad; PSRR- = pm (then |Avss| follows)
    or |Avss| = avss (then PSRR- follows)."""
    ad_dc = pp + 20 * math.log10(avdd)
    if pm is not None:
        avss = 10 ** ((ad_dc - pm) / 20)
    v = {
        "pmc_op1_vout": vcm + offset, "pmc_op1_vinp": vcm, "pmc_offset": offset, "pmc_par_vdd": 0.0,
        "pmc_par_vss": 0.0, "pmc_npts": 201.0, "pmc_f0": 0.1, "pmc_f20": 1.0, "pmc_f80": 1e3, "pmc_f100": 1e4,
        "pmc_f120": 1e5, "pmc_f140": 1e6, "pmc_dm_vderr": 1e-13, "pmc_dm_vcerr": 5e-14, "pmc_dm_railq": 0.0,
        "pmc_ad_spread": 1e-4, "pmc_fu_hz": 1.6e7,
    }
    for r, a in (("vdd", avdd), ("vss", avss)):
        v.update({f"pmc_{r}_err": 1e-15, f"pmc_{r}_oth": 0.0, f"pmc_{r}_inq": 1e-18, f"pmc_{r}_leak": 1e-15,
                  f"pmc_{r}_amin": a / 2, f"pmc_{r}_spread": 1e-4, f"pmc_{r}_aspread": 1e-4,
                  f"pmc_{r}_re0": a, f"pmc_{r}_im0": 1e-4})
        v.update({f"pmc_{r}_aband{i}": a for i in range(m.BAND[0], m.BAND[1] + 1)})
    for f in m.FIGS:
        v[f"pmc_ad_{f}"] = ad_dc if f != "fu" else 0.0
        v[f"pmc_psrr_vdd_{f}"] = pp if f != "fu" else 0.8
        v[f"pmc_asup_vdd_{f}"] = avdd if f != "fu" else 0.9
        v[f"pmc_psrr_vss_{f}"] = ad_dc - 20 * math.log10(avss) if f != "fu" else 37.0
        v[f"pmc_asup_vss_{f}"] = avss if f != "fu" else 0.014
    v["pmc_ad_b0"] = ad_dc
    return v


def knee_profile(vals: dict, rail: str, rise_db: float) -> dict:
    """Make `rail`'s DC band a monotone in-band knee of `rise_db` total (the
    near-cancellation signature: clamped |Asupply| rising, minimum at the
    0.1 Hz edge), with the printed scalars kept self-consistent."""
    n = m.BAND[1] - m.BAND[0]
    mult = 10 ** (rise_db / 20.0 / n)
    prof = [vals[f"pmc_{rail}_aband{m.BAND[0]}"] * mult**i for i in range(n + 1)]
    for i, a in enumerate(prof):
        vals[f"pmc_{rail}_aband{m.BAND[0] + i}"] = a
    vals[f"pmc_{rail}_spread"] = rise_db
    vals[f"pmc_{rail}_aspread"] = rise_db
    vals[f"pmc_{rail}_amin"] = prof[0]
    vals[f"pmc_asup_{rail}_dc"] = statistics.fmean(prof)
    vals[f"pmc_psrr_{rail}_dc"] = vals["pmc_ad_dc"] - 20 * math.log10(statistics.fmean(prof))
    return vals


def log_of(vals: dict) -> str:
    lines = ["Doing analysis at TEMP = 27.000000", "No. of Data Rows : 201"]
    lines += [f"pmc_fu_hz           =  {vals['pmc_fu_hz']:.5e}"]  # the `meas` line shape
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
            corners.append({"corner_id": f"{k[0]}/{k[1]:g}/{k[2]:g}/mc{i}", "process": k[0], "temperature_c": k[1],
                            "supply_v": {"vdd": k[2], "vcm": round(k[2] / 2, 6)}, "status": "pass",
                            "monte_carlo": {"sample_index": 0 if dup else i, "seed": 1000 + i, "mismatch_seed": 7,
                                            "process_seed": 9},
                            "artifacts": {"log": path}})
    return {"corners": corners}, logs


class P:  # minimal Pdk stand-in
    variant = "gf180mcuD"
    path = Path("/pdk/gf180mcuD")


class TailAndRequestTests(unittest.TestCase):
    def test_tail_runs_three_sweeps_in_one_deck(self):
        lines = m.mc.analysis_tail().splitlines()
        acs = [i for i, ln in enumerate(lines) if ln.startswith("ac ")]
        self.assertEqual(len(acs), 2, "exactly two extra .ac (the vdd and vss sweeps)")
        self.assertEqual(lines.count("op"), 3)
        # vdd drive is set (and the dm drive removed) before the first extra sweep ...
        for a in ("alter vcm acmag=0", "alter vnac acmag=0", "alter vdd acmag=1"):
            self.assertLess(lines.index(a), acs[0], a)
        # ... and the vdd drive removed, the vss drive set, before the second
        for a in ("alter vdd acmag=0", "alter vss acmag=1"):
            self.assertTrue(acs[0] < lines.index(a) < acs[1], a)
        # every scalar is printed, after everything is computed
        prints = [ln.split(None, 1)[1] for ln in lines if ln.startswith("print ")]
        self.assertEqual(prints, m.SCALARS)
        self.assertGreater(lines.index(f"print {m.SCALARS[0]}"),
                           max(i for i, ln in enumerate(lines) if ln.startswith("meas ")))
        # Ad from the dm sweep, Asupply from the ACTUAL rail phasor of its own sweep
        self.assertIn("let pmc_adv = ac1.v(vout) / pmc_vd1", lines)
        self.assertIn("let pmc_vdd_av = ac2.v(vout) / ac2.v(vdd)", lines)
        self.assertIn("let pmc_vss_av = ac3.v(vout) / ac3.v(vss)", lines)
        # parity: both supply sweeps' DC output against the dm sweep's
        self.assertIn("let pmc_par_vdd = v(vout) - op1.v(vout)", lines)
        self.assertEqual(sum("op1.v(vout)" in ln for ln in lines), 2)
        self.assertIn("setplot ac3", lines)
        # the floor is applied in the deck, elementwise
        self.assertTrue(any("pos(" in ln and "pmc_vss_ae" in ln for ln in lines))
        for s in m.SCALARS:
            self.assertEqual(s, s.lower(), "ngspice lower-cases printed names")

    def test_spot_indices_land_on_the_sweep(self):
        for f, i in m.SPOTS.values():
            self.assertAlmostEqual(m.C.AC_FSTART * 10 ** (i / m.C.AC_PPD), f, delta=f * 1e-12)
        self.assertEqual(m.BAND, (0, 20))
        self.assertEqual(m.N_FREQ, 201)

    def test_request_shape(self):
        req = m.request(Path("/w/tb.spice"), P(), m.CORNERS, [27.0], [3.30], n=300)
        self.assertEqual(req["analysis"]["kind"], "ac")
        self.assertEqual(req["analysis"]["args"], m.AC_ARGS + "\n" + m.mc.analysis_tail())
        self.assertEqual(req["monte_carlo"], {"n": 300, "seed": 45, "vary": "mismatch"})
        self.assertEqual(req["measurements"], [], "the klt 0.5.0 fleet runner rejects `expr` measurements")
        self.assertFalse(req["options"]["waveforms"])
        self.assertTrue(req["options"]["keep_artifacts"])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [3.30], "vcm": [1.65]})
        self.assertEqual([p["name"] for p in req["corners"]["process"]], m.CORNERS)
        self.assertNotIn("pdk_root", req["models"])
        r2 = m.request(Path("/w/tb.spice"), P(), ["typical"], [27.0], [3.30], n=None, local=True)
        self.assertNotIn("monte_carlo", r2)
        self.assertIn("pdk_root", r2["models"])
        r3 = m.request(Path("/w/tb.spice"), P(), ["typical"], [27.0], [3.30], n=20, vary="process")
        self.assertEqual(r3["monte_carlo"]["vary"], "process")

    def test_population_is_the_issue_99_one(self):
        self.assertEqual(m.CORNERS, ["typical", "ff", "ss", "fs", "sf"])
        self.assertEqual((m.MC_N, m.MC_SEED, m.MC_VARY), (300, 45, "mismatch"))
        self.assertEqual((m.TEMPS, m.SUPPLIES), ([27.0], [3.30]))
        self.assertEqual(m.SYSTEMATIC_RECORD, "20261010-163854-0127eb7")
        self.assertEqual(m.DR6_BOUNDS, {"dc": 60.0, "1k": 60.0, "10k": 50.0})


class FingerprintTests(unittest.TestCase):
    """Issue #89: the runner and the fingerprint share ONE configuration source."""

    TB = m.TESTBENCH.read_text()

    def test_request_is_what_the_fingerprint_hashes(self):
        inp = m.mc.inputs({"bench": self.TB})
        req = m.request(Path("/w/tb.spice"), P(), m.CORNERS, m.TEMPS, m.SUPPLIES, n=m.MC_N)
        self.assertEqual(inp["analysis"]["args"], req["analysis"]["args"])
        self.assertEqual({k: inp["monte_carlo"][k] for k in ("n", "seed", "vary")}, req["monte_carlo"])
        self.assertEqual(inp["corners"]["process"], [p["name"] for p in req["corners"]["process"]])
        self.assertEqual(m.deck_params(1)["sw_stat_mismatch"], 1)
        self.assertEqual({k: m.deck_params()[k] for k in inp["excitation"]["dm"]}, inp["excitation"]["dm"])

    def test_mismatch_switch_and_tail_move_the_fingerprint(self):
        base = m.mc.fingerprint({"bench": self.TB})
        self.assertNotEqual(m.mc.fingerprint({"bench": self.TB.replace("sw_stat_mismatch=1", "sw_stat_mismatch=0")}),
                            base)
        old = m.mc.FLOOR
        m.mc.FLOOR = old * 10
        try:
            self.assertNotEqual(m.mc.fingerprint({"bench": self.TB}), base)
        finally:
            m.mc.FLOOR = old

    def test_header_and_inputs_block_roundtrip(self):
        blob = "\n".join(m.mc.fingerprint_lines({"bench": self.TB}) + m.mc.inputs_section({"bench": self.TB}))
        fp = m.mc.fingerprint({"bench": self.TB})
        self.assertIn(fp, blob)
        body = blob.split("```json\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(measurement_fingerprint(json.loads(body)), fp)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tb = m.TESTBENCH.read_text()

    def test_committed_bench_passes(self):
        self.assertEqual(m.guard_testbench(self.tb), [])

    def test_circuit_must_equal_the_systematic_bench(self):
        self.assertTrue(m.guard_testbench(self.tb.replace("CL vout 0 2p", "CL vout 0 3p")))
        self.assertTrue(m.guard_testbench(self.tb.replace("Rsv vsb vsv {rsv}", "Rsv vsb vsv 1k")))
        self.assertTrue(m.guard_testbench(self.tb + "Rx vout 0 1meg\n"))

    def test_driven_vss_and_ground_references(self):
        errs = m.guard_testbench(self.tb.replace("Xdut vdd vss vinp", "Xdut vdd 0 vinp"))
        self.assertTrue(any("vss" in e for e in errs), errs)
        errs = m.guard_testbench(self.tb.replace("Vss vss 0 dc 0 ac {acss}", "Vss vss 0 dc 0"))
        self.assertTrue(any("acss" in e for e in errs), errs)
        errs = m.guard_testbench(self.tb.replace("CL vout 0 2p", "CL vout vss 2p"))
        self.assertTrue(any("ground" in e for e in errs), errs)

    def test_param_line_rules(self):
        self.assertTrue(m.guard_testbench(self.tb.replace(" sw_stat_mismatch=1", "")))
        self.assertTrue(m.guard_testbench(self.tb.replace("sw_stat_mismatch=1", "sw_stat_mismatch=1 sw_stat_global=1")))
        line = ".param acp=0.5 acn=-0.5 acdd=0 acss=0 rsv=1e9 csv=1e9 sw_stat_mismatch=1\n"
        self.assertIn(line, self.tb)
        moved = self.tb.replace(line, "").replace(".include 'design.ngspice'", line + ".include 'design.ngspice'")
        errs = m.guard_testbench(moved)
        self.assertTrue(any("precedes" in e for e in errs), errs)

    def test_shared_switch_guard_cases(self):
        # duplicate switch, and the switch hidden behind a standalone second line
        errs = m.guard_testbench(self.tb + ".param sw_stat_mismatch=1\n")
        self.assertTrue(any("exactly one" in e for e in errs), errs)
        # global spread anywhere in the code, even off a .param line
        errs = m.guard_testbench(self.tb + ".option sw_stat_global=1\n")
        self.assertTrue(any("sw_stat_global" in e for e in errs), errs)

    def test_no_transistor_and_circuit_body(self):
        self.assertTrue(m.guard_testbench(self.tb + "M1 a b c d nfet_03v3 W=1u L=1u\n"))
        self.assertTrue(m.guard_testbench(self.tb + ".control\nrun\n.endc\n"))


class ParseAndValidateTests(unittest.TestCase):
    def test_parse_reads_print_and_meas_lines(self):
        vals = good_vals()
        got = m.parse_scalars(log_of(vals))
        for k, v in vals.items():
            self.assertAlmostEqual(got[k], v, delta=abs(v) * 1e-6 + 1e-30, msg=k)
        self.assertEqual(m.parse_scalars("pmc_x = 1.0\npmc_x   =  2.5e+00\n")["pmc_x"], 2.5)
        self.assertEqual(m.parse_scalars("PMC_Y = 3\n"), {"pmc_y": 3.0})
        self.assertEqual(m.parse_scalars("pmc_z = nan\nfoo = 1\ncmc_w = 2\n"), {})

    def test_valid_sample(self):
        p, lb = m.validate_sample(good_vals(), 1.65)
        self.assertEqual(p, [])
        self.assertEqual(lb, set())

    def test_each_check_bites(self):
        cases = {
            "pmc_par_vdd": (2e-6, "same devices"),
            "pmc_par_vss": (-2e-6, "same devices"),
            "pmc_dm_vderr": (1e-3, "differential excitation"),
            "pmc_dm_vcerr": (1e-3, "differential excitation"),
            "pmc_dm_railq": (1e-3, "rails not AC-quiet"),
            "pmc_vdd_err": (1e-3, "[vdd] rail excitation"),
            "pmc_vss_oth": (1e-3, "[vss] rail excitation"),
            "pmc_vdd_inq": (1e-6, "[vdd] inputs not AC-quiet"),
            "pmc_vss_inq": (1e-6, "[vss] inputs not AC-quiet"),
            "pmc_vss_leak": (1e-3, "[vss] input leak"),
            "pmc_npts": (200.0, "frequency points"),
            "pmc_f100": (1.1e4, "sweep grid"),
            "pmc_op1_vinp": (1.6, "vinp"),
            "pmc_op1_vout": (1.9, "from VCM"),
            "pmc_ad_spread": (0.2, "Ad plateau"),
            "pmc_fu_hz": (2e9, "outside the sweep"),
        }
        for key, (bad, needle) in cases.items():
            v = good_vals()
            v[key] = bad
            p, _ = m.validate_sample(v, 1.65)
            self.assertTrue(p and any(needle in x for x in p), f"{key}: {p}")

    def test_nonflat_band_without_a_knee_is_invalid(self):
        # a falling band: no plateau and not the near-cancellation signature
        v = good_vals()
        v["pmc_vdd_spread"] = v["pmc_vdd_aspread"] = 0.5
        v["pmc_vdd_aband20"] = v["pmc_vdd_aband0"] / 2
        p, _ = m.validate_sample(v, 1.65)
        self.assertTrue(any("not a near-cancellation knee" in x for x in p), p)
        # a humped band: rises, then falls
        v = good_vals()
        v["pmc_vss_spread"] = v["pmc_vss_aspread"] = 2.0
        v["pmc_vss_aband10"] = v["pmc_vss_aband0"] * 2
        p, _ = m.validate_sample(v, 1.65)
        self.assertTrue(any("PSRR-" in x and "not a near-cancellation knee" in x for x in p), p)

    def test_near_cancellation_knee_is_valid_flagged_and_dc_reads_the_edge(self):
        # the diagnosed case: |Avdd| = 1.9e-3 at 0.1 Hz rising 7.7 dB in band,
        # 1.3e7 x the floor, every bench check healthy
        v = knee_profile(good_vals(pp=147.9, avdd=1.9e-3, avss=0.2), "vdd", 7.7)
        p, lb = m.validate_sample(v, 1.65)
        self.assertEqual(p, [])
        self.assertEqual(lb, set())
        s = sample(v)
        self.assertEqual(s.knee_rails(), {"vdd"})
        psrr0, a0 = s.dc_figure("vdd")
        self.assertAlmostEqual(psrr0, v["pmc_ad_b0"] - 20 * math.log10(v["pmc_vdd_aband0"]), places=12)
        self.assertEqual(a0, v["pmc_vdd_aband0"])
        # the plateau rail keeps the band-mean figures
        self.assertEqual(s.dc_figure("vss"), (s.psrr("vss", "dc"), s.asup("vss", "dc")))
        # both rails knee: each reads its own edge
        v2 = knee_profile(knee_profile(good_vals(), "vdd", 1.2), "vss", 3.3)
        self.assertEqual(sample(v2).knee_rails(), {"vdd", "vss"})
        # statistics: the knee enters at its asymptote, and is counted
        st = m.fig_stats([s, sample(good_vals(pp=100.0, avdd=0.6))], "vdd", "dc")
        self.assertEqual(st.knees, 1)
        self.assertAlmostEqual(st.mean_db, (psrr0 + 100.0) / 2, places=9)
        st1k = m.fig_stats([s, sample(good_vals(pp=100.0, avdd=0.6))], "vdd", "1k")
        self.assertEqual(st1k.knees, 0)
        self.assertAlmostEqual(st1k.min_db, min(s.psrr("vdd", "1k"), 100.0), places=9)

    def test_knee_monotonicity_slack(self):
        # steps within the printed-precision slack stay a knee; a real fall is not
        v = knee_profile(good_vals(), "vdd", 2.0)
        v["pmc_vdd_aband7"] = v["pmc_vdd_aband6"] * (1 - 5e-6)
        self.assertEqual(m.classify_rail_dc(v, "vdd"), "knee")
        v = knee_profile(good_vals(), "vdd", 2.0)
        v["pmc_vdd_aband7"] = v["pmc_vdd_aband6"] * (1 - 1e-4)
        self.assertEqual(m.classify_rail_dc(v, "vdd"), "invalid")

    def test_missing_or_nonfinite_scalar_is_invalid(self):
        v = good_vals()
        del v["pmc_psrr_vss_10k"]
        self.assertIn("missing", m.validate_sample(v, 1.65)[0][0])
        v = good_vals()
        v["pmc_asup_vdd_dc"] = float("inf")
        self.assertIn("missing", m.validate_sample(v, 1.65)[0][0])

    def test_near_cancellation_floor_is_a_lower_bound_never_a_division_by_zero(self):
        v = good_vals()
        v["pmc_vss_amin"] = 0.0
        v["pmc_asup_vss_dc"] = 0.0
        s = sample(v)
        self.assertTrue(s.valid)
        self.assertEqual(s.lower_bound, {"vss"})
        self.assertEqual(s.vals["pmc_asup_vss_dc"], m.FLOOR)
        self.assertAlmostEqual(s.psrr("vss", "dc"), s.ad("dc") - 20 * math.log10(m.FLOOR))
        st = m.fig_stats([s, sample(good_vals())], "vss", "dc")
        self.assertEqual(st.lower_bounds, 1)
        self.assertTrue(math.isfinite(st.lin_3s))


class ExtractTests(unittest.TestCase):
    def test_complete_population(self):
        rep, logs = report({NOM: [good_vals(100 + i * 0.1) for i in range(5)]})
        out, probs = m.extract(rep, [NOM], 5, read_log=logs.__getitem__)
        self.assertEqual(probs, [])
        self.assertEqual([s.index for s in out[NOM]], list(range(5)))
        self.assertEqual(out[NOM][3].seeds["seed"], 1003)

    def test_fewer_than_n_duplicate_and_unexpected_block(self):
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
        bad["pmc_par_vss"] = 1e-3
        rep, logs = report({NOM: [good_vals(), bad]})
        _, probs = m.extract(rep, [NOM], 2, read_log=logs.__getitem__)
        self.assertTrue(any("same devices" in p for p in probs), probs)
        rep, logs = report({NOM: [good_vals()]})
        rep["corners"][0]["artifacts"] = {}
        _, probs = m.extract(rep, [NOM], 1, read_log=logs.__getitem__)
        self.assertTrue(any("no ngspice log" in p for p in probs), probs)


class StatsTests(unittest.TestCase):
    def test_statistics(self):
        pp = [96.0, 98.0, 100.0, 102.0, 104.0]
        av = [1.2, 0.95, 0.75, 0.6, 0.48]
        ss = [sample(good_vals(p, 110.0, avdd=a)) for p, a in zip(pp, av)]
        st = m.fig_stats(ss, "vdd", "dc")
        self.assertEqual(st.n, 5)
        self.assertAlmostEqual(st.mean_db, 100.0)
        self.assertAlmostEqual(st.sigma_db, statistics.stdev(pp))
        self.assertAlmostEqual(st.db_3s, 100.0 - 3 * statistics.stdev(pp))
        self.assertEqual(st.min_db, 96.0)
        self.assertAlmostEqual(st.p5_db, 96.0 + 0.2 * 2.0)
        ad = statistics.fmean(p + 20 * math.log10(a) for p, a in zip(pp, av))
        want = ad - 20 * math.log10(statistics.fmean(av) + 3 * statistics.stdev(av))
        self.assertAlmostEqual(st.lin_3s, want)
        self.assertLess(st.lin_3s, st.min_db)

    def test_percentile_matches_numpy_linear(self):
        import numpy as np

        xs = [3.0, 1.0, 4.0, 1.5, 9.0, 2.6, 5.0]
        for q in (0, 1, 5, 50, 95, 100):
            self.assertAlmostEqual(m.percentile(xs, q), float(np.percentile(xs, q)))

    def test_worst_and_dr6_comparison(self):
        def st(pp, pm):
            ss = [sample(good_vals(pp, pm)), sample(good_vals(pp + 1, pm + 1))]
            return {r: {f: m.fig_stats(ss, r, f) for f in m.FIGS} for r in m.RAILS}

        stats = {NOM: st(100.0, 110.0), ("ss", 27.0, 3.30): st(90.0, 55.0)}
        _, k = m.worst(stats, "vss", "dc", "lin_3s")
        self.assertEqual(k, ("ss", 27.0, 3.30))
        rows = {(r["rail"], r["fig"]): r for r in m.dr6_rows(stats)}
        self.assertEqual(set(rows), {(r, f) for r in m.RAILS for f in ("dc", "1k", "10k")})
        self.assertEqual(rows[("vdd", "dc")]["below"], [])
        self.assertIn("min", rows[("vss", "1k")]["below"])
        self.assertEqual(rows[("vss", "10k")]["bound"], 50.0)

    def test_sign_counts(self):
        ss = [sample(good_vals(avss=0.2)), sample(good_vals()), sample(good_vals())]
        ss[1].vals["pmc_vss_re0"] = -0.1
        self.assertEqual(m.sign_counts(ss, "vss"), (2, 3))


class ControlTests(unittest.TestCase):
    def ref(self, pp=100.0, pm=None):
        v = good_vals(pp)
        pm = v["pmc_psrr_vss_dc"] if pm is None else pm
        return {NOM: {"vdd": {**{f: pp for f in m.FIGS[:-1]}, "fu": 0.8},
                      "vss": {**{f: pm for f in m.FIGS[:-1]}, "fu": 37.0}, "fu_hz": 1.6e7}}

    def test_switch_off_passes_on_identical_samples_matching_the_record(self):
        ss = {NOM: [sample(good_vals(100.0), i) for i in range(4)]}
        probs, rows = m.check_switch_off(ss, self.ref())
        self.assertEqual(probs, [])
        self.assertEqual(rows[0]["dev"]["vss"]["dc"], 0.0)

    def test_switch_off_fails_on_spread_or_deviation(self):
        ss = {NOM: [sample(good_vals(100.0 + i * 1e-3), i) for i in range(4)]}
        probs, _ = m.check_switch_off(ss, self.ref())
        self.assertTrue(any("samples differ" in p for p in probs), probs)
        ss = {NOM: [sample(good_vals(100.0), i) for i in range(2)]}
        probs, _ = m.check_switch_off(ss, self.ref(pm=111.0))
        self.assertTrue(any("PSRR- dc" in p for p in probs), probs)
        probs, _ = m.check_switch_off(ss, {})
        self.assertTrue(any("no committed" in p for p in probs), probs)

    def test_imbalance_control(self):
        base = [sample(good_vals(100 - (i % 7), avdd=0.6 + 0.1 * (i % 7), avss=0.2 + 0.3 * (i % 5)), i)
                for i in range(50)]
        worse = [sample(good_vals(94 - (i % 7), avdd=1.2 + 0.1 * (i % 7), avss=1.8 + 0.3 * (i % 5)), i)
                 for i in range(50)]
        probs, info = m.check_imbalance(base, worse)
        self.assertEqual(probs, [])
        self.assertGreater(info["vss"]["shift"], 0)
        probs, _ = m.check_imbalance(base, base)
        self.assertEqual(len(probs), 2 * len(m.IMBALANCE_RAILS))
        # a rise on one rail only fails the criterion for the other
        half = [sample(good_vals(94 - (i % 7), avdd=1.2 + 0.1 * (i % 7), avss=0.2 + 0.3 * (i % 5)), i)
                for i in range(50)]
        probs, _ = m.check_imbalance(base, half)
        self.assertTrue(probs and all("Avss" in p or "PSRR-" in p for p in probs), probs)

    def test_imbalance_rails_pinned(self):
        self.assertEqual(m.IMBALANCE_RAILS, ("vdd", "vss"))

    def test_offset_sigma_gate_blocks_when_mismatch_not_acting(self):
        def pts(scale):
            out = []
            for i in range(20):
                v = good_vals(100.0)
                v["pmc_offset"] = scale * (i - 9.5)
                out.append(sample(v, i))
            return out
        self.assertEqual(m.check_offset_sigma({NOM: pts(1e-3)}), [])
        probs = m.check_offset_sigma({NOM: pts(1e-3), ("ss", 27.0, 3.30): pts(1e-6)})
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("mismatch not acting", probs[0])
        self.assertTrue(m.check_offset_sigma({NOM: pts(0.0)}))

    def test_systematic_reference_reads_the_current_committed_record(self):
        ref = m.systematic_reference(m.G.expected_keys(m.CORNERS, m.TEMPS, m.SUPPLIES))
        self.assertEqual(len(ref), 5)
        self.assertAlmostEqual(ref[NOM]["vdd"]["dc"], 100.74, delta=0.005)
        self.assertAlmostEqual(ref[NOM]["vdd"]["10k"], 63.96, delta=0.005)
        self.assertAlmostEqual(ref[NOM]["vss"]["dc"], 111.69, delta=0.005)
        self.assertAlmostEqual(ref[NOM]["vss"]["fu"], 37.43)
        self.assertAlmostEqual(ref[NOM]["vdd"]["fu"], 0.81)
        self.assertAlmostEqual(ref[NOM]["fu_hz"], 16.399e6)
        self.assertAlmostEqual(ref[("ff", 27.0, 3.30)]["vss"]["dc"], 115.87, delta=0.005)


class RecordTests(unittest.TestCase):
    """The record writer on a synthetic population (no simulator)."""

    def test_record_carries_dr6_table_controls_and_fingerprint(self):
        import datetime as _dt
        import tempfile

        keys = m.G.expected_keys(m.CORNERS, m.TEMPS, m.SUPPLIES)
        samples = {k: [sample(good_vals(100 + (i % 5), 70 + 3 * (i % 7), offset=1e-3 * (i % 9 - 4)), i)
                       for i in range(12)] for k in keys}
        samples[NOM][3] = sample(knee_profile(good_vals(147.9, avdd=1.9e-3), "vdd", 7.7), 3)
        stats = m.point_stats(samples)
        off = {k: [sample(good_vals(100.0), i) for i in range(2)] for k in keys}
        off_probs, off_rows = m.check_switch_off(off, {})
        base = samples[NOM]
        ctl = [sample(good_vals(95 - (i % 5), avdd=1.2 + 0.1 * (i % 5), avss=2.0 + 0.2 * (i % 7)), i) for i in range(12)]
        imb_probs, imb_info = m.check_imbalance(base, ctl)

        class Pk:
            variant, version, source = "gf180mcuD", "c6d73a3", "PDK_ROOT"

        rep = {"corners": [], "environment": {}}
        md = m.build_record(record="20990101-000000-abcdef0", stamp=_dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc), pdk=Pk(),
                            ngspice="ngspice-46", kver="klt x", reports={"grid": rep}, walls={"grid": 1.0},
                            samples=samples, stats=stats, off_rows=off_rows, off_probs=off_probs,
                            proc_stats=({r: m.fig_stats(base, r, "dc") for r in m.RAILS}, 1e-3),
                            imb_info=imb_info, imb_probs=imb_probs, link={"available": False}, mc_env=None,
                            dut_sha="0" * 64, problems_none="yes")
        self.assertIn("## Comparison with the DR-6 proposed bounds", md)
        rows = m.dr6_rows(stats)
        self.assertEqual(len(rows), 6)
        for r in rows:
            self.assertIn(f"| {m.LABEL[r['rail']]} | {m.FIG_LABEL[r['fig']]} | {r['bound']:g} |", md)
        self.assertIn(m.mc.fingerprint({"bench": m.TESTBENCH.read_text()}), md)
        self.assertIn("## Measurement fingerprint inputs", md)
        self.assertIn("Negative control: mirror imbalance", md)
        self.assertIn("near-cancellation", md)
        self.assertIn("Near-cancellation knees (1 sample rails)", md)
        self.assertIn(f"| {m.G.fmt_key(NOM)} | 3 | PSRR+ | 7.700 |", md)
        self.assertIn("| knee DCs", md)
        self.assertNotIn("PASS verdict", md)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.csv"
            m.write_csv(p, samples)
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 1 + 5 * 12)
            self.assertIn("pmc_psrr_vss_10k", lines[0])
            self.assertIn("knee_rails", lines[0])


class GridTests(unittest.TestCase):
    """The `--grid full` paths (issue #190): axes, keys, request shape,
    extraction over the 45-point population, shard-refusal accounting, the
    floor/knee accounting gate, and the grid record writer. No simulator."""

    def test_grid_axes(self):
        self.assertEqual(m.mc.grid_axes("nominal"), ([27.0], [3.30], [1.65]))
        temps, sups, vcms = m.mc.grid_axes("full")
        self.assertEqual((temps, sups), ([-40.0, 27.0, 125.0], [2.97, 3.30, 3.63]))
        self.assertEqual(vcms, [round(v / 2, 6) for v in sups])
        with self.assertRaises(ValueError):
            m.mc.grid_axes("hot")

    def test_grid_keys_are_the_45_points(self):
        keys = m.grid_keys("full")
        self.assertEqual(len(keys), 45)
        self.assertEqual(len(set(keys)), 45)
        self.assertEqual(keys[0], ("typical", -40.0, 2.97))
        self.assertEqual({k[0] for k in keys}, set(m.CORNERS))
        self.assertEqual({k[1] for k in keys}, {-40.0, 27.0, 125.0})
        self.assertEqual({k[2] for k in keys}, {2.97, 3.30, 3.63})

    def test_nominal_inputs_unchanged_full_inputs_name_the_grid(self):
        inp = m.mc.inputs({"bench": self.TB})
        self.assertNotIn("grid", inp)
        self.assertEqual(inp["corners"]["temperature_c"], [27.0])
        self.assertEqual(inp["corners"]["supply_v"], [3.30])
        self.assertNotIn("vcm_v", inp["corners"])
        full = m.mc.inputs({"bench": self.TB}, {"grid": "full"})
        self.assertEqual(full["grid"], "full")
        self.assertEqual(full["corners"]["temperature_c"], [-40.0, 27.0, 125.0])
        self.assertEqual(full["corners"]["supply_v"], [2.97, 3.30, 3.63])
        self.assertEqual(full["corners"]["vcm_v"], [1.485, 1.65, 1.815])
        self.assertEqual(m.mc.grid_of({"grid": "full"}), "full")
        self.assertEqual(m.mc.grid_of(None), "nominal")
        base = m.mc.fingerprint({"bench": self.TB})
        self.assertNotEqual(m.mc.fingerprint({"bench": self.TB}, {"grid": "full"}), base)

    TB = m.TESTBENCH.read_text()

    def test_grid_request_shape(self):
        req = m.grid_request(Path("/w/tb.spice"), P(), "full", n=m.MC_N, hosts=8)
        self.assertEqual(req["corners"]["temperature_c"], [-40.0, 27.0, 125.0])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [2.97, 3.30, 3.63], "vcm": [1.485, 1.65, 1.815]})
        self.assertEqual(req["remote"], {"hosts": 8})
        self.assertEqual(req["monte_carlo"], {"n": m.MC_N, "seed": m.MC_SEED, "vary": m.MC_VARY})
        self.assertEqual(req["analysis"]["args"], m.AC_ARGS + "\n" + m.mc.analysis_tail())
        one = m.grid_request(Path("/w/tb.spice"), P(), "nominal", n=m.MC_N)
        self.assertNotIn("remote", one, "the nominal request must stay byte-identical (no remote block)")
        self.assertEqual(one["corners"]["temperature_c"], [27.0])

    def test_lost_shard_refusals_and_remote_jobs(self):
        rep = {"environment": {"remote": {"fleet": [{"job_id": "j1"}, None, {"job_id": "j3"}]}},
               "corners": [
                   {"corner_id": "a", "diagnostics": [{"code": "lost_shard",
                                                       "message": "shard lost: BATCH_MAX_CONCURRENT_INSTANCES"}]},
                   {"corner_id": "b", "diagnostics": [{"code": "lost_shard", "message": "shard lost: expired"}]},
               ]}
        self.assertEqual(m.remote_jobs(rep), [{"job_id": "j1"}, {}, {"job_id": "j3"}])
        self.assertEqual(m.lost_shard_refusals(rep), 1)
        self.assertEqual(m.lost_shard_refusals({"corners": []}), 0)
        self.assertEqual(m.remote_jobs({"environment": {"remote": {"job_id": "j"}}}), [{"job_id": "j"}])
        self.assertEqual(m.remote_jobs({}), [])

    def grid_population(self, n=4):
        keys = m.grid_keys("full")
        pts = {}
        for k in keys:
            pts[k] = [good_vals(100.0 + (i % 5) * 0.3, vcm=k[2] / 2) for i in range(n)]
        return pts

    def test_extract_full_grid(self):
        rep, logs = report(self.grid_population())
        keys = m.grid_keys("full")
        out, probs = m.extract(rep, keys, 4, read_log=logs.__getitem__)
        self.assertEqual(probs, [])
        self.assertEqual(sorted(out), sorted(keys))
        self.assertTrue(all(len(out[k]) == 4 for k in keys))
        self.assertEqual({s.index for k in keys for s in out[k]}, set(range(4)))

    def test_incomplete_grid_point_blocks(self):
        pts = self.grid_population()
        k0 = m.grid_keys("full")[7]
        pts[k0] = pts[k0][:3]
        rep, logs = report(pts)
        _, probs = m.extract(rep, m.grid_keys("full"), 4, read_log=logs.__getitem__)
        self.assertTrue(any(m.G.fmt_key(k0) in p and "sample indices [0, 1, 2]" in p for p in probs), probs)

    def test_miskeyed_grid_point_blocks(self):
        rep, logs = report(self.grid_population())
        bad = dict(rep["corners"][0])
        bad["temperature_c"] = 85.0
        rep["corners"].append(bad)
        _, probs = m.extract(rep, m.grid_keys("full"), 4, read_log=logs.__getitem__)
        self.assertTrue(any("unexpected grid point" in p for p in probs), probs)

    def test_parity_failure_blocks_its_sample(self):
        pts = self.grid_population()
        k = ("ss", 125.0, 3.63)
        pts[k][1]["pmc_par_vss"] = 5e-6
        rep, logs = report(pts)
        _, probs = m.extract(rep, m.grid_keys("full"), 4, read_log=logs.__getitem__)
        self.assertTrue(any("same devices" in p for p in probs), probs)

    def test_accounting_gate_catches_bad_floor_accounting(self):
        pts = self.grid_population()
        k = ("fs", -40.0, 2.97)
        pts[k][0]["pmc_vss_amin"] = 0.0
        pts[k][0]["pmc_asup_vss_dc"] = 0.0
        rep, logs = report(pts)
        samples, probs = m.extract(rep, m.grid_keys("full"), 4, read_log=logs.__getitem__)
        self.assertEqual(probs, [])
        stats = m.point_stats(samples)
        self.assertEqual(m.accounting_problems(samples, stats), [])
        stats[k]["vss"]["dc"].lower_bounds += 1  # tamper: the record would over-count the floor
        probs = m.accounting_problems(samples, stats)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("floor/knee accounting", probs[0])

    def test_nominal_reference_rederives_the_committed_record(self):
        ref = m.nominal_reference()
        self.assertEqual(len(ref), 5)
        self.assertAlmostEqual(ref[NOM]["vdd"]["dc"].lin_3s, 94.78, delta=0.005)
        self.assertAlmostEqual(ref[("ss", 27.0, 3.30)]["vdd"]["dc"].lin_3s, 93.71, delta=0.005)
        self.assertAlmostEqual(ref[("sf", 27.0, 3.30)]["vss"]["dc"].lin_3s, 82.59, delta=0.005)
        self.assertEqual(m.nominal_reference(Path("/nonexistent.csv")), {})

    def test_grid_record_content(self):
        import datetime as _dt
        keys = m.grid_keys("full")
        samples = {k: [sample(good_vals(100 + (i % 5) * 0.4, 70 + 2 * (i % 6), offset=1e-3 * (i % 9 - 4),
                                               vcm=k[2] / 2), i, key=k) for i in range(6)] for k in keys}
        knee_k = ("ss", 125.0, 2.97)
        samples[knee_k][2] = sample(knee_profile(good_vals(147.9, avdd=1.9e-3, vcm=knee_k[2] / 2), "vdd", 7.7),
                                    2, key=knee_k)
        stats = m.point_stats(samples)
        self.assertEqual(m.accounting_problems(samples, stats), [])
        off = {k: [sample(good_vals(100.0, vcm=k[2] / 2), i, key=k) for i in range(2)] for k in keys}
        off_probs, off_rows = m.check_switch_off({NOM: off[NOM]}, self._ref())
        base = samples[NOM]
        ctl = [sample(good_vals(95 - (i % 5), avdd=1.2 + 0.1 * (i % 5), avss=2.0 + 0.2 * (i % 7)), i) for i in range(6)]
        imb_probs, imb_info = m.check_imbalance(base, ctl)

        class Pk:
            variant, version, source = "gf180mcuD", "c6d73a3", "PDK_ROOT"

        rep = {"corners": [], "environment": {"remote": {"fleet": [{"job_id": "j1"}, {"job_id": "j2"}]}}}
        md = m.build_grid_record(
            record="20990101-000000-abcdef0", stamp=_dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc), pdk=Pk(),
            ngspice="ngspice-46", kver="klt x", reports={"grid": rep}, walls={"grid": 1.0}, samples=samples,
            stats=stats, off_rows=off_rows, off_probs=off_probs,
            proc_stats=({r: m.fig_stats(base, r, "dc") for r in m.RAILS}, 1e-3), imb_info=imb_info,
            imb_probs=imb_probs, link={"available": False}, mc_env=None, dut_sha="0" * 64,
            nominal_ref=m.nominal_reference(), resubmits=0)
        self.assertIn("# PSRR Monte Carlo PVT grid record", md)
        self.assertIn("- **Grid**: full PVT -- process typical, ff, ss, fs, sf; T -40, 27, 125 C; "
                      "VDD 2.97, 3.30, 3.63 V (VCM = VDD/2: 1.485, 1.65, 1.815 V) = 45 points, "
                      '`monte_carlo = {n: 300, seed: 45, vary: "mismatch"}` per point -> 45 x 300 = 13500 samples', md)
        self.assertIn(m.mc.fingerprint({"bench": m.TESTBENCH.read_text()}, {"grid": "full"}), md)
        self.assertIn('"grid":"full"', md)
        self.assertIn("2 fleet job(s)", md)
        self.assertIn("## Comparison with the DR-6 proposed bounds (full grid)", md)
        self.assertIn("## Linear 3-sigma by temperature and supply (worst MOS corner)", md)
        for h in (f"{m.LABEL[r]} {m.FIG_LABEL[f]}" for r in m.RAILS for f in m.FIGS):
            self.assertIn(f"### {h}", md)
            self.assertEqual(md.count(f"| {m.G.fmt_key(k)} |" for k in [keys[0]]), 0) if False else None
        self.assertEqual(md.count(f"| {m.G.fmt_key(keys[0])} | 6 |"), 12, "every figure's table carries every point")
        self.assertIn("| typical / -40 C / 2.97 V |", md)
        self.assertIn(f"### Near-cancellation knees (1 sample rails)", md)
        self.assertIn(f"| {m.G.fmt_key(knee_k)} | 2 | PSRR+ |", md)
        self.assertIn("Result: all 13500 samples valid", md)
        self.assertIn("- All blocking checks passed: yes.", md)
        self.assertIn("it neither grades nor alters DR-6", md)
        self.assertNotIn("PASS verdict", md)
        # nominal-only figure beside the grid's
        self.assertIn("nominal-only lin 3s", md)
        self.assertIn(m.G.fmt_key(("ss", 27.0, 3.30)), md)

    def _ref(self, pp=100.0):
        v = good_vals(pp)
        pm = v["pmc_psrr_vss_dc"]
        return {NOM: {"vdd": {**{f: pp for f in m.FIGS[:-1]}, "fu": 0.8},
                      "vss": {**{f: pm for f in m.FIGS[:-1]}, "fu": 37.0}, "fu_hz": 1.6e7}}


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

    def test_switch_off_unit_reproduces_the_systematic_record(self):
        s, probs = m.local_unit(self.pdk, mismatch=0)
        self.assertEqual(probs, [])
        probs, rows = m.check_switch_off({NOM: [s]}, m.systematic_reference([NOM]))
        self.assertEqual(probs, [], rows)
        self.assertEqual(s.vals["pmc_par_vdd"], 0.0)
        self.assertEqual(s.vals["pmc_par_vss"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
