#!/usr/bin/env python3
"""Regression tests for the input-common-mode-range experiment (issue #60).

The unit tests need no simulator: request construction (paired axes, the 45 PVT
identities, explicit 1.20 V samples), sample evaluation (unequal AC drive,
missing / non-finite fields, saturation-boundary tolerance, paired-OP
disagreement), interval extraction (disconnected pass islands, no bridging of
failed/invalid samples, conservative brackets), intersections (including an
empty one) and the source guards are checked on synthetic data. `SimTests` runs
local nominal single units (never the grid) and is skipped when klt, ngspice or
the PDK is unavailable (a failure under SIM_REQUIRE_PREREQS=1).

    python3 sim/input-common-mode/test_input_common_mode.py
"""

from __future__ import annotations

import csv
import io
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_input_common_mode as m  # noqa: E402
from harness import PdkNotFound, find_pdk, skip_or_fail  # noqa: E402

FREQ = np.logspace(math.log10(m.AC_FSTART), math.log10(m.AC_FSTOP), m.N_FREQ)
NOM_COMBO = ("typical", 27.0, 3300)


def key(vcm_mv: int, combo=NOM_COMBO) -> m.Key:
    return (*combo, vcm_mv)


def make_op(vcm=1.65, vout=None, margin_mv=300.0, ids=10e-6, vdsat=0.1):
    """A complete synthetic operating point; every device `margin_mv` above saturation."""
    op = {"v(vout)": vcm if vout is None else vout, "v(vinp)": vcm, "v(vinn)": vcm if vout is None else vout}
    for d in m.DEVICES:
        op[f"@m.xdut.{d}.m0[vds]"] = vdsat + margin_mv * 1e-3
        op[f"@m.xdut.{d}.m0[vdsat]"] = vdsat
        op[f"@m.xdut.{d}.m0[id]"] = ids
    return op


def make_unit(mode, gain_db=90.0, vcm=1.65, op=None, acp=None, acn=None, ripple_db=0.0, acm=0.1):
    """Synthetic excitation unit with the ACTUAL input phasors and vout = Ad vd + Acm vc."""
    p = dict(m.MODES[mode])
    acp = p["acp"] if acp is None else acp
    acn = p["acn"] if acn is None else acn
    ad = 10 ** (gain_db / 20) * 10 ** (ripple_db * np.linspace(0, 1, FREQ.size) / 20)
    vp = np.full(FREQ.shape, acp, dtype=complex)
    vn = np.full(FREQ.shape, acn, dtype=complex)
    vd, vc = vp - vn, (vp + vn) / 2
    vec = {"frequency": FREQ.astype(complex), "v(vinp)": vp, "v(vinn)": vn, "v(vout)": ad * vd + acm * vc}
    return m.Unit(vec=vec, op=make_op(vcm) if op is None else op)


def sample(vcm_mv=1650, combo=NOM_COMBO, **kw) -> m.Sample:
    vcm = vcm_mv / 1000
    op = kw.pop("op", None)
    dm = make_unit("dm", vcm=vcm, op=None if op is None else dict(op), **kw)
    cm = make_unit("cm", vcm=vcm, op=None if op is None else dict(op))
    return m.evaluate_sample(key(vcm_mv, combo), dm, cm)


def stub(vcm_mv, status, combo=NOM_COMBO, margin_mv=300.0):
    """A Sample with a prescribed status, for interval logic."""
    s = m.Sample(key=key(vcm_mv, combo), valid=status != "invalid", gain_db=90.0, min_margin_v=margin_mv * 1e-3,
                 limiting_device="xm5")
    if status == "fail":
        s.fail_reasons.append("gain")
    if status == "invalid":
        s.invalid_reasons.append("x")
    return s


class RequestTests(unittest.TestCase):
    def test_scan_axis(self):
        for vdd in m.SUPPLIES_MV:
            pts = m.scan_vcm_mv(vdd)
            self.assertEqual(pts[0], 0)
            self.assertEqual(pts[-1], vdd)
            self.assertIn(1200, pts)
            self.assertIn(vdd // 2, pts)
            self.assertLessEqual(max(b - a for a, b in zip(pts, pts[1:])), 50)
            self.assertEqual(pts, sorted(set(pts)))

    def test_paired_supply_repeats_vdd_per_vcm(self):
        pairs = [(v, c) for v in m.SUPPLIES_MV for c in m.scan_vcm_mv(v)]
        sv = m.paired_supply(pairs)
        self.assertEqual(len(sv["vdd"]), len(sv["vcm"]))
        self.assertEqual(list(zip(sv["vdd"], sv["vcm"]))[0], (2.97, 0.0))
        for v in m.SUPPLIES_MV:
            self.assertEqual(sv["vdd"].count(v / 1000), len(m.scan_vcm_mv(v)))
        self.assertEqual(len(set(zip(sv["vdd"], sv["vcm"]))), len(pairs))

    def test_45_pvt_identities(self):
        combos = m.all_combos()
        self.assertEqual(len(combos), 45)
        self.assertEqual(len(set(combos)), 45)
        self.assertEqual({c[0] for c in combos}, {"typical", "ss", "ff", "fs", "sf"})
        self.assertEqual({c[1] for c in combos}, {-40.0, 27.0, 125.0})
        self.assertEqual({c[2] for c in combos}, {2970, 3300, 3630})

    def test_expected_keys_cross_temperatures_with_pairs(self):
        pairs = [(2970, 1200), (3300, 1200), (3300, 1650)]
        ks = m.expected_keys("ss", m.TEMPS_C, pairs)
        self.assertEqual(len(ks), 9)
        self.assertEqual(len(m.expected_keys(["ss", "ff"], m.TEMPS_C, pairs)), 18)
        self.assertIn(("ss", 125.0, 3300, 1650), ks)
        self.assertIn(("ss", -40.0, 2970, 1200), ks)

    def test_unit_key_from_report_corner(self):
        c = {"process": "ff", "temperature_c": -40.0, "supply_v": {"vdd": 3.63, "vcm": 1.815}}
        self.assertEqual(m.unit_key(c), ("ff", -40.0, 3630, 1815))
        with self.assertRaises(ValueError):
            m.unit_key({"process": "ff", "temperature_c": 27.0, "supply_v": {"vdd": 3.3, "vcm": 1.6504}})

    def test_request_shape(self):
        pdk = m.Pdk(path=Path("/x/gf180mcuD"), variant="gf180mcuD", source="test")
        req = m.icmr_request(Path("/tmp/tb.spice"), pdk, "sf", m.TEMPS_C, [(3300, 1200), (3300, 1250)])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [3.3, 3.3], "vcm": [1.2, 1.25]})
        self.assertEqual([p["name"] for p in req["corners"]["process"]], ["sf"])
        self.assertEqual(req["corners"]["temperature_c"], m.TEMPS_C)
        self.assertEqual(req["measurements"], [])
        args = req["analysis"]["args"]
        self.assertTrue(args.startswith("dec 20 0.1 10"))
        self.assertIn("\nop\nprint ", args)
        self.assertTrue(args.endswith("setplot ac1"))
        for d in m.DEVICES:
            for p in ("vds", "vdsat", "id"):
                self.assertIn(f"@m.xdut.{d}.m0[{p}]", args)


class EvaluationTests(unittest.TestCase):
    def test_baseline_passes(self):
        s = sample()
        self.assertTrue(s.valid, s.invalid_reasons)
        self.assertEqual(s.status(), "pass")
        self.assertAlmostEqual(s.gain_db, 90.0, places=6)
        self.assertAlmostEqual(s.min_margin_v, 0.3, places=9)
        self.assertEqual(s.tolerance_flags(), [])

    def test_gain_below_60_fails(self):
        s = sample(gain_db=59.9)
        self.assertTrue(s.valid)
        self.assertEqual(s.status(), "fail")
        self.assertIn("60", s.reasons()[0])
        self.assertEqual(sample(gain_db=60.1).status(), "pass")

    def test_minimum_over_plateau_is_compared(self):
        s = sample(gain_db=60.05, ripple_db=-0.08)  # drifts down by <= 0.1 dB over the sweep (band is the first part)
        self.assertTrue(s.valid, s.invalid_reasons)
        self.assertLessEqual(s.gain_db, s.gain_mean_db)

    def test_unflat_plateau_is_invalid(self):
        s = sample(ripple_db=-6.0)
        self.assertFalse(s.valid)
        self.assertIn("plateau", s.invalid_reasons[0])

    def test_unequal_ac_drive_is_invalid(self):
        dm = make_unit("dm")
        cm = make_unit("cm", acp=1.0, acn=0.99)
        s = m.evaluate_sample(key(1650), dm, cm)
        self.assertFalse(s.valid)
        self.assertTrue(any("residual differential" in r for r in s.invalid_reasons), s.invalid_reasons)

    def test_wrong_differential_drive_is_invalid(self):
        s = m.evaluate_sample(key(1650), make_unit("dm", acp=0.4), make_unit("cm"))
        self.assertFalse(s.valid)
        self.assertTrue(any("differential excitation" in r for r in s.invalid_reasons))

    def test_missing_field_is_invalid(self):
        op = make_op()
        del op["@m.xdut.xm5.m0[id]"]
        s = sample(op=op)
        self.assertFalse(s.valid)
        self.assertIn("missing", s.invalid_reasons[0])
        self.assertEqual(s.status(), "invalid")

    def test_nonfinite_field_is_invalid(self):
        op = make_op()
        op["@m.xdut.xm1.m0[vds]"] = float("nan")
        s = sample(op=op)
        self.assertFalse(s.valid)
        self.assertIn("non-finite", s.invalid_reasons[0])

    def test_failed_unit_is_invalid(self):
        s = m.evaluate_sample(key(1650), m.Unit(error="analysis did not complete"), make_unit("cm"))
        self.assertEqual(s.status(), "invalid")
        s2 = m.evaluate_sample(key(1650), None, None)
        self.assertEqual(s2.status(), "invalid")

    def test_paired_operating_points_must_agree_to_1uv(self):
        op2 = make_op()
        op2["v(vout)"] += 2e-6
        op2["v(vinn)"] += 2e-6
        s = m.evaluate_sample(key(1650), make_unit("dm"), make_unit("cm", op=op2))
        self.assertFalse(s.valid)
        self.assertIn("disagree", s.invalid_reasons[0])
        op3 = make_op()
        op3["v(vout)"] += 0.5e-6
        op3["v(vinn)"] += 0.5e-6
        self.assertTrue(m.evaluate_sample(key(1650), make_unit("dm"), make_unit("cm", op=op3)).valid)

    def test_vinp_must_be_at_vcm(self):
        op = make_op(vcm=1.60)
        s = m.evaluate_sample(key(1650), make_unit("dm", op=op), make_unit("cm", op=dict(op)))
        self.assertFalse(s.valid)

    def test_output_far_from_vcm_fails(self):
        s = sample(op=make_op(vcm=1.65, vout=1.65 - 0.2))
        self.assertTrue(s.valid)
        self.assertEqual(s.status(), "fail")
        self.assertIn("vout", s.reasons()[0])
        ok = sample(op=make_op(vcm=1.65, vout=1.65 - 0.09))
        self.assertEqual(ok.status(), "pass")

    def test_saturation_boundary_tolerance(self):
        # margin exactly 0 passes; -0.9 mV passes and is flagged; -1.1 mV fails; strict (0 mV) rejects any negative
        for margin_mv, status, strict, flagged in ((0.0, "pass", "pass", False), (-0.9, "pass", "fail", True),
                                                   (-1.1, "fail", "fail", False), (5.0, "pass", "pass", False)):
            op = make_op()
            op["@m.xdut.xm5.m0[vds]"] = 0.1 + margin_mv * 1e-3
            s = sample(op=op)
            self.assertEqual(s.status(), status, margin_mv)
            self.assertEqual(s.status(0.0), strict, margin_mv)
            self.assertEqual(bool(s.tolerance_flags()), flagged, margin_mv)
            if flagged:
                self.assertEqual(s.tolerance_flags(), ["xm5"])

    def test_bias_reference_is_recorded_not_a_criterion(self):
        op = make_op()
        op["@m.xdut.xmb1.m0[vds]"] = 0.0  # bias diode below its Vdsat
        s = sample(op=op)
        self.assertEqual(s.status(), "pass")
        self.assertLess(s.margins_v["xmb1"], 0)
        self.assertEqual(m.CRITERIA_DEVICES, ("xm1", "xm2", "xm5", "xm3", "xm4", "xm6", "xm7"))

    def test_zero_pair_or_tail_current_fails(self):
        for dev in ("xm1", "xm2", "xm5"):
            op = make_op()
            op[f"@m.xdut.{dev}.m0[id]"] = 0.0
            s = sample(op=op)
            self.assertEqual(s.status(), "fail", dev)
            self.assertIn("zero current", s.reasons()[0])

    def test_current_sign_is_ignored(self):
        op = make_op()
        op["@m.xdut.xm1.m0[id]"] = -10e-6
        self.assertEqual(sample(op=op).status(), "pass")


class RangeTests(unittest.TestCase):
    def series(self, statuses: dict[int, str], combo=NOM_COMBO):
        return [stub(v, st, combo) for v, st in sorted(statuses.items())]

    def dense(self, lo, hi, pass_lo, pass_hi, combo, step=50):
        return self.series({v: "pass" if pass_lo <= v <= pass_hi else "fail" for v in range(lo, hi + 1, step)}, combo)

    def test_single_interval_with_conservative_brackets(self):
        s = self.series({0: "fail", 50: "fail", 100: "pass", 150: "pass", 200: "pass", 250: "fail"})
        (iv,) = m.pass_intervals(s)
        self.assertEqual((iv.lo_mv, iv.hi_mv, iv.n_samples), (100, 200, 3))
        self.assertEqual((iv.lo_unc_mv, iv.hi_unc_mv), (50, 50))  # endpoints are the PASSING samples
        self.assertEqual((iv.lo_nb, iv.hi_nb), ("fail", "fail"))

    def test_scan_edges_have_no_uncertainty(self):
        s = self.series({0: "pass", 50: "pass", 100: "fail"})
        (iv,) = m.pass_intervals(s)
        self.assertEqual((iv.lo_mv, iv.lo_unc_mv, iv.lo_nb), (0, None, "scan edge"))
        self.assertEqual((iv.hi_mv, iv.hi_unc_mv), (50, 50))

    def test_disconnected_pass_islands_are_not_bridged(self):
        s = self.series({0: "pass", 50: "pass", 100: "fail", 150: "pass", 200: "pass", 250: "invalid", 300: "pass"})
        ivs = m.pass_intervals(s)
        self.assertEqual([(i.lo_mv, i.hi_mv) for i in ivs], [(0, 50), (150, 200), (300, 300)])
        self.assertEqual(ivs[1].hi_nb, "invalid")

    def test_missing_sample_never_bridges(self):
        # 100 absent from the table: 50 and 150 are 100 mV apart and must not join
        s = self.series({0: "pass", 50: "pass", 150: "pass", 200: "pass"})
        ivs = m.pass_intervals(s)
        self.assertEqual([(i.lo_mv, i.hi_mv) for i in ivs], [(0, 50), (150, 200)])

    def test_invalid_is_never_passing(self):
        s = self.series({0: "invalid", 50: "invalid"})
        self.assertEqual(m.pass_intervals(s), [])

    def test_tolerance_changes_intervals(self):
        a, b, c = stub(0, "pass"), stub(50, "pass", margin_mv=-0.5), stub(100, "pass")
        self.assertEqual([(i.lo_mv, i.hi_mv) for i in m.pass_intervals([a, b, c])], [(0, 100)])
        self.assertEqual([(i.lo_mv, i.hi_mv) for i in m.pass_intervals([a, b, c], sat_tol=0.0)], [(0, 0), (100, 100)])

    def test_intersection_and_component(self):
        c1, c2 = ("ss", -40.0, 2970), ("ff", 125.0, 3630)
        per = {
            c1: m.pass_intervals(self.dense(1000, 1700, 1050, 1650, c1)),
            c2: m.pass_intervals(self.dense(1100, 2050, 1150, 2000, c2)),
        }
        (iv,) = m.intersect_intervals(per)
        self.assertEqual((iv.lo_mv, iv.hi_mv), (1150, 1650))
        self.assertIn("ff", iv.lo_src)
        self.assertIn("ss", iv.hi_src)
        self.assertIsNone(m.component_containing([iv], 1200) and None)
        self.assertIs(m.component_containing([iv], 1200), iv)
        self.assertIsNone(m.component_containing([iv], 1100))

    def test_empty_intersection_is_explicit(self):
        c1, c2 = ("ss", -40.0, 2970), ("ff", 125.0, 3630)
        per = {c1: m.pass_intervals(self.series({0: "pass", 50: "fail"}, c1)),
               c2: m.pass_intervals(self.series({0: "fail", 50: "pass"}, c2))}
        self.assertEqual(m.intersect_intervals(per), [])
        per[c2] = []
        self.assertEqual(m.intersect_intervals(per), [])  # a point with no interval empties everything

    def test_intersection_keeps_disjoint_components(self):
        c1, c2 = ("ss", -40.0, 2970), ("ff", 125.0, 3630)
        per = {c1: m.pass_intervals(self.series({0: "pass", 50: "pass", 100: "fail", 150: "pass", 200: "pass"}, c1)),
               c2: m.pass_intervals(self.series({0: "pass", 50: "pass", 100: "pass", 150: "pass", 200: "pass"}, c2))}
        self.assertEqual([(i.lo_mv, i.hi_mv) for i in m.intersect_intervals(per)], [(0, 50), (150, 200)])

    def test_target_verdict(self):
        combos = m.all_combos()
        table = {(*c, 1200): stub(1200, "pass", c) for c in combos}
        self.assertEqual(m.target_verdict(table, combos)["verdict"], "meets")
        table[(*combos[3], 1200)] = stub(1200, "invalid", combos[3])
        v = m.target_verdict(table, combos)
        self.assertEqual((v["verdict"], v["n_invalid"]), ("unknown", 1))
        table[(*combos[4], 1200)] = stub(1200, "fail", combos[4])
        self.assertEqual(m.target_verdict(table, combos)["verdict"], "fails")
        del table[(*combos[5], 1200)]  # absent sample is invalid evidence, never a pass
        self.assertEqual(m.target_verdict(table, combos)["n_invalid"], 2)
        only_missing = {(*c, 1200): stub(1200, "pass", c) for c in combos[1:]}
        self.assertEqual(m.target_verdict(only_missing, combos)["verdict"], "unknown")

    def test_refinement_points_fill_transition_brackets_only(self):
        c = NOM_COMBO
        table = {(*c, v): stub(v, st, c) for v, st in
                 {0: "fail", 50: "fail", 100: "pass", 150: "pass", 200: "fail"}.items()}
        need = m.refinement_points(table, [c])
        self.assertEqual(need[c], list(range(55, 100, 5)) + list(range(155, 200, 5)))
        # an already-refined bracket needs nothing
        table2 = {(*c, v): stub(v, "fail" if v < 100 else "pass", c) for v in range(50, 151, 5)}
        self.assertEqual(m.refinement_points(table2, [c]), {})

    def test_refine_pairs_group_by_process(self):
        need = {("ss", -40.0, 2970): [1105, 1110], ("ss", 27.0, 2970): [1100], ("ff", 27.0, 3300): [900]}
        procs, pairs = m.refine_pairs(need)
        self.assertEqual(procs, ["ff", "ss"])
        self.assertEqual(pairs, [(2970, 1100), (2970, 1105), (2970, 1110), (3300, 900)])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.text = m.TESTBENCH.read_text()

    def test_committed_bench_is_clean(self):
        self.assertEqual(m.C.guard_testbench(self.text), [])
        self.assertFalse([ln for ln in m.C.code_lines(self.text) if ln.split()[0].lower().startswith("m")])

    def test_bench_matches_cmrr_bench_body(self):
        def body(t):
            return "\n".join(m.C.code_lines(t))

        self.assertEqual(body(self.text), body(m.C.TESTBENCH.read_text()))

    def test_guard_catches_lost_servo_and_transistors(self):
        self.assertTrue(m.C.guard_testbench(self.text.replace("Esb vsb 0 vout 0 1", "")))
        self.assertTrue(m.C.guard_testbench(self.text + "\nM1 a b c d nfet_03v3 w=1u l=1u\n"))
        self.assertTrue(m.C.guard_testbench(self.text.replace("CL vout 0 2p", "CL vout 0 3p")))


class ValidityGroupTests(unittest.TestCase):
    def test_grouping_masks_numbers_readably_and_keeps_a_verbatim_example(self):
        mk = lambda v, r: m.Sample(key(v), False, [r])  # noqa: E731
        inv = [mk(100, "no Ad plateau over 0.1-1 Hz: varies by 3.214 dB (> 0.1 dB)"),
               mk(150, "no Ad plateau over 0.1-1 Hz: varies by 0.512 dB (> 0.1 dB)"),
               mk(50, "Ad plateau phase -179.9 deg is not ~0 (wrong polarity)")]
        g = m.validity_groups(inv)
        self.assertEqual(g[0], (2, "no Ad plateau over N-N Hz: varies by N dB (> N dB)", key(100),
                                "no Ad plateau over 0.1-1 Hz: varies by 3.214 dB (> 0.1 dB)"))
        self.assertEqual(g[1][:2], (1, "Ad plateau phase -N deg is not ~N (wrong polarity)"))
        self.assertEqual(g[1][3], "Ad plateau phase -179.9 deg is not ~0 (wrong polarity)")
        for _n, pat, _k, _r in g:
            self.assertNotIn("#", pat)


class KeepWorkCacheTests(unittest.TestCase):
    """The `--keep-work` report cache must be keyed on netlist CONTENT (DUT and
    bench), not only on the request JSON, which names the netlist by path."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.work = Path(self._t.name)
        self.dut_text = "* dut v1\n.subckt opamp a b\n.ends\n"

    def tearDown(self):
        self._t.cleanup()

    def _materialise(self, wd: Path, *_a, **_k) -> Path:
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "design.ngspice").write_text("* design include\n")
        (wd / "opamp_two_stage.dut.spice").write_text(self.dut_text)
        tb = wd / "tb.spice"
        tb.write_text(f".include '{wd / 'design.ngspice'}'\n.include '{wd / 'opamp_two_stage.dut.spice'}'\n.end\n")
        return tb

    def test_fingerprint_follows_included_dut_content(self):
        tb = self._materialise(self.work / "a")
        req = {"netlist": str(tb), "corners": {"process": ["typical"]}}
        k1 = m.request_cache_key(req, tb)
        self.assertEqual(k1, m.request_cache_key(dict(req, netlist="/elsewhere/tb.spice"), tb))
        (tb.parent / "opamp_two_stage.dut.spice").write_text("* dut v2\n")
        self.assertNotEqual(k1, m.request_cache_key(req, tb))

    def test_fingerprint_follows_bench_content(self):
        tb = self._materialise(self.work / "a")
        f1 = m.netlist_fingerprint(tb)
        tb.write_text(tb.read_text() + "* bench edit\n")
        self.assertNotEqual(f1, m.netlist_fingerprint(tb))

    def test_changed_netlist_invalidates_the_cached_report(self):
        calls = []

        def fake_klt(req, out, backend, wd, **_k):
            calls.append(req)
            return {"corners": [], "passed": 0, "corner_count": 0}

        class Args:
            backend = "batch"
            batch_submit_retries = 0
            batch_retry_wait_s = 0
            batch_runner_version_check = None
            batch_capacity_wait_s = None

        saved = (m.C.materialise, m.icmr_request, m.run_klt_retrying)
        m.C.materialise = self._materialise
        m.icmr_request = lambda tb, *a, **k: {"netlist": str(tb), "corners": {"process": ["typical"]}}
        m.run_klt_retrying = fake_klt
        try:
            plan = m.Plan("scan-dm", "dm", ["typical"], [(3300, 1200)])
            run = lambda: m._run_plan(plan, None, self.work, Args(), {}, {}, {})  # noqa: E731
            m.REUSED.discard(plan.name)
            run()
            self.assertEqual(len(calls), 1)
            run()  # identical request AND identical netlist content: reused
            self.assertEqual(len(calls), 1)
            self.assertIn(plan.name, m.REUSED)
            m.REUSED.discard(plan.name)
            self.dut_text = "* dut v2 (resized)\n.subckt opamp a b\n.ends\n"
            run()  # same request JSON, changed DUT: must re-submit
            self.assertEqual(len(calls), 2)
            self.assertNotIn(plan.name, m.REUSED)
        finally:
            m.C.materialise, m.icmr_request, m.run_klt_retrying = saved
            m.REUSED.discard("scan-dm")


class EvidenceTests(unittest.TestCase):
    def test_archive_roundtrip(self):
        ret = {}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            raw_text = (
                "Title: x\nFlags: complex\nNo. Variables: 2\nNo. Points: 1\nVariables:\n"
                "\t0\tfrequency\tfrequency\n\t1\tv(vout)\tvoltage\nValues:\n 0\t1.0,0.0\n\t2.0,0.5\n")
            (d / "a.raw").write_text(raw_text)
            (d / "a.log").write_text("v(vout) = 1.0\n")
            k = key(1200)
            ret[k] = {"dm": m.Unit(raw_path=str(d / "a.raw"), log_path=str(d / "a.log")),
                      "cm": m.Unit(error="failed")}
            n = m.write_archives(d / "data", ret)
            self.assertEqual(n, 2)
            back = m.load_retained(d / "data")
            self.assertEqual(set(back), {k})
            self.assertIsNone(back[k]["dm"].error and None)
            self.assertIn("rawfile lacks", back[k]["dm"].error)  # no vinp/vinn in this toy raw: rejected, not accepted
            self.assertNotIn("cm", back[k])  # nothing retained for the failed unit
            self.assertEqual(m.build_table(back)[k].status(), "invalid")

    def test_trim_raw_keeps_used_vectors_bit_identical(self):
        names = ["frequency", "i(esb)", "v(vinn)", "v(vinp)", "v(vdd)", "v(vout)"]
        npts = 3
        hdr = ("Title: x\nDate: d\nPlotname: AC Analysis\nFlags: complex\n"
               f"No. Variables: {len(names)}\nNo. Points: {npts}\nVariables:\n"
               + "".join(f"\t{i}\t{n}\t{'frequency grid=3' if i == 0 else 'voltage'}\n" for i, n in enumerate(names))
               + "Values:\n")
        rng = np.random.default_rng(1)
        body = []
        for p in range(npts):
            for i in range(len(names)):
                a, b = (0.1 * 10 ** p, 0.0) if i == 0 else rng.normal(size=2)
                body.append(f"{p if i == 0 else ''}\t{a:.15e},{b:.15e}")
        text = hdr + "\n".join(body) + "\n"
        full = m.G.parse_ascii_complex_raw(text)
        trimmed = m.trim_raw(text)
        part = m.G.parse_ascii_complex_raw(trimmed)
        self.assertEqual(set(part), set(m.RAW_KEEP))
        for k in m.RAW_KEEP:
            self.assertTrue(np.array_equal(part[k], full[k]), k)
        self.assertIn("No. Variables: 4", trimmed)
        with self.assertRaises(ValueError):
            m.trim_raw(text.replace("v(vout)", "v(vx)"))
        with self.assertRaises(ValueError):
            m.trim_raw(text.rsplit("\n", 2)[0] + "\n")  # truncated values

    def test_csv_roundtrip_detects_tampering(self):
        table = {key(v): sample(v) for v in (1200, 1650)}
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=m.CSV_FIELDS)
        w.writeheader()
        for k in sorted(table):
            w.writerow(m.sample_row(table[k]))
        rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertEqual(m.compare_csv(table, rows), [])
        rows[0]["status"] = "fail"
        self.assertTrue(m.compare_csv(table, rows))
        self.assertTrue(m.compare_csv(table, rows[:1]))


class SimTests(unittest.TestCase):
    """Local nominal single units (never the grid)."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("klt") is None or shutil.which("ngspice") is None:
            skip_or_fail(cls, "klt/ngspice not available")
        try:
            cls.pdk = find_pdk()
        except PdkNotFound:
            skip_or_fail(cls, "gf180mcu PDK not available")

    def test_nominal_midrail_reproduces_committed_cmrr_plateau(self):
        cdir = m.latest_cmrr_dir()
        if cdir is None:
            skip_or_fail(self, "committed CMRR record not available")
        with tempfile.TemporaryDirectory() as d:
            s, _, _ = m.local_sample("nominal", self.pdk, Path(d), 1650)
        self.assertTrue(s.valid, s.invalid_reasons)
        ref = m.committed_midrail_gain_db(cdir, NOM_COMBO)
        self.assertIsNotNone(ref)
        self.assertLess(abs(s.gain_mean_db - ref), m.MIDRAIL_TOL_DB)
        self.assertEqual(s.status(), "pass")

    def test_servo_isolation_moves_gain_under_001_db(self):
        with tempfile.TemporaryDirectory() as d:
            nom, _, _ = m.local_sample("nom", self.pdk, Path(d), 1650)
            for c in m.C.ISOLATION_CSV:
                s, _, _ = m.local_sample(f"iso{c:g}", self.pdk, Path(d), 1650, over={"csv": c})
                self.assertTrue(s.valid, s.invalid_reasons)
                self.assertLessEqual(abs(s.gain_mean_db - nom.gain_mean_db), m.ISOLATION_TOL_DB)

    def test_inadequate_servo_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            s, _, _ = m.local_sample("bad", self.pdk, Path(d), 1650, over={"csv": m.C.ISOLATION_INADEQUATE_CSV})
        self.assertFalse(s.valid)

    def test_unequal_cm_drive_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            s, _, _ = m.local_sample("uneq", self.pdk, Path(d), 1650, cm_params={"acp": 1.0, "acn": 0.99})
        self.assertFalse(s.valid)

    def test_vcm_at_the_rail_does_not_pass(self):
        with tempfile.TemporaryDirectory() as d:
            s, _, _ = m.local_sample("rail", self.pdk, Path(d), 3300)
        self.assertNotEqual(s.status(), "pass")


if __name__ == "__main__":
    unittest.main(verbosity=1)
