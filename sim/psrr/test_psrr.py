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
from harness import PdkNotFound, find_pdk, skip_or_fail  # noqa: E402

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
            skip_or_fail(self, "PDK not available")
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
            skip_or_fail(cls, "klt/ngspice not available")
        try:
            cls.pdk = find_pdk()
        except PdkNotFound:
            skip_or_fail(cls, "gf180mcu PDK not available")

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


class MeasurementFingerprint(unittest.TestCase):
    """Issue #89: the runner and the fingerprint share ONE configuration source."""

    TB = r.TESTBENCH.read_text()
    PDK = r.C.Pdk(Path("/x/gf180mcuD"), "gf180mcuD", "test")

    def inp(self):
        return r.mc.inputs({"bench": self.TB})

    def test_runner_constants_are_the_fingerprinted_ones(self):
        self.assertIs(r.MODES, r.mc.MODES)
        self.assertEqual(r.FEEDTHROUGH_R, r.mc.FEEDTHROUGH_R)
        self.assertEqual(r.C.SERVO_NOMINAL, self.inp()["excitation"]["servo"])
        self.assertEqual(list(r.C.ISOLATION_CSV), self.inp()["controls"]["isolation_csv"])

    def test_request_analysis_is_what_the_fingerprint_hashes(self):
        req = r.C.ac_request(Path("/x/tb.spice"), self.PDK, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        self.assertEqual(req["analysis"], {"kind": self.inp()["analysis"]["kind"], "args": self.inp()["analysis"]["args"]})

    def test_supply_modes_are_fingerprinted(self):
        self.assertEqual(self.inp()["excitation"]["modes"]["vss"]["acss"], 1.0)
        self.assertEqual(self.inp()["excitation"]["modes"]["vdd"]["acss"], 0.0)

    def test_record_embeds_header_and_inputs(self):
        blob = "\n".join(r.mc.fingerprint_lines({"bench": self.TB}) + r.mc.inputs_section({"bench": self.TB}))
        self.assertIn(r.mc.fingerprint({"bench": self.TB}), blob)
        self.assertIn("## Measurement fingerprint inputs", blob)


# --------------------------------------------------------------------------
# Issue #155: opt-in RZ x CC passive-corner study (no simulator, no PDK)
# --------------------------------------------------------------------------

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

import passive_corners as pc  # noqa: E402

FULL_WANT = pc.passive_expected_keys(pc.FULL_POINTS)
SMALL_POINTS = [("typical", 27.0, 3.30), ("ss", 125.0, 2.97)]
SMALL_WANT = pc.passive_expected_keys(SMALL_POINTS)


def cell_vectors(k):
    """Synthetic dm/vdd/vss vectors for a cell: Ad depends on the MOS corner,
    Avdd on RZ and CC (so the passive deltas are non-trivial), Avss is flat."""
    mos, rz, cc = pc.split_passive_key(k)
    g = {"typical": 1.0, "ff": 0.8, "ss": 1.2, "fs": 0.9, "sf": 1.1}[mos]
    ad = g * ad_true()
    avdd = 0.5 * (1 + S / (2e4 * pc.MIM_FACTOR[cc])) * pc.RES_FACTOR[rz] ** 0.1
    avss = 0.02 * (1 + k[1] / 1000.0) * np.ones_like(ad)
    dm, sup = triple(ad, avdd, avss)
    return {"dm": dm, **sup}


def fake_raw(vec: dict) -> str:
    names = ["frequency", *r.NEED]
    head = ["Title: fake", "Plotname: AC Analysis", "Flags: complex", f"No. Variables: {len(names)}",
            f"No. Points: {len(FREQ)}", "Variables:"]
    head += [f"\t{i}\t{nm}\t{'frequency' if i == 0 else 'voltage'}" for i, nm in enumerate(names)]
    cols = np.column_stack([np.asarray(vec[nm], dtype=complex) for nm in names])
    body = [f"{i if j == 0 else ''}\t{v.real:.15e},{v.imag:.15e}" for i, row in enumerate(cols) for j, v in enumerate(row)]
    return "\n".join([*head, "Values:", *body]) + "\n"


def fake_log(vcm: float) -> str:
    vals = {"v(vout)": vcm, "v(vinp)": vcm, "v(vinn)": vcm}
    out = []
    for name in C.OP_PRINT:
        v = vals.get(name, 1.0 if name.endswith("[vds]") else 0.2)
        out.append(f"{name} = {v:.6e}")
    return "\n".join(out) + "\n"


def mode_of(tb_text: str) -> str:
    p = dict(kv.split("=") for kv in re.search(r"^\.param\s+(.*)$", tb_text, re.M).group(1).split())
    if float(p["acdd"]):
        return "vdd"
    return "vss" if float(p["acss"]) else "dm"


class FakeKlt:
    """Stands in for `harness.run_klt`: expands the request with the runner's
    own `request_cells` and writes rawfile / log / deck per cell. Hooks:
    `elapsed` (fleet execution seconds), `tamper(mode, key, vec)` (returns a
    vector or None to drop the cell), `fail_first` ({(mode, mos)}: first
    attempt of those partitions drops a cell), `refuse` (first N submits
    refused for capacity)."""

    def __init__(self, elapsed=60, tamper=None, fail_first=(), refuse=0):
        self.elapsed, self.tamper, self.fail_first, self.refuse = elapsed, tamper, set(fail_first), refuse
        self.calls = []
        self.vec_cache = {}

    def __call__(self, req, outdir, backend, workdir):
        if self.refuse:
            self.refuse -= 1
            raise r.KltError("klt sim error: batch_no_capacity")
        mode = mode_of(Path(req["netlist"]).read_text())
        cells = r.request_cells(req)
        mos = {pc.split_passive_key(k)[0] for k in cells}
        first = (mode, *sorted(mos)) not in {c[:2] for c in self.calls} and (mode, *sorted(mos)) in self.fail_first
        self.calls.append((mode, *sorted(mos), len(cells)))
        corners = []
        outdir.mkdir(parents=True, exist_ok=True)
        for i, k in enumerate(cells):
            if first and i == 0:
                continue
            if k not in self.vec_cache:
                self.vec_cache[k] = cell_vectors(k)
            vec = self.vec_cache[k][mode]
            if self.tamper:
                vec = self.tamper(mode, k, dict(vec))
                if vec is None:
                    continue
            d = outdir / f"c{i}"
            d.mkdir()
            (d / "waveform.raw").write_text(fake_raw(vec))
            (d / "ngspice.log").write_text(fake_log(k[2] / 2))
            mos_k, rz, cc = pc.split_passive_key(k)
            (d / "corner.cir").write_text("\n".join(f'.lib "/fleet/libs.tech/ngspice/sm141064.ngspice" {s}'
                                                    for s in (mos_k, *pc.passive_sections(rz, cc))) + "\n")
            corners.append({"process": k[0], "temperature_c": k[1], "supply_v": {"vdd": k[2], "vcm": k[2] / 2},
                            "status": "pass", "diagnostics": [],
                            "artifacts": {"raw": str(d / "waveform.raw"), "log": str(d / "ngspice.log"),
                                          "deck": str(d / "corner.cir")}})
        return {"corners": corners, "errored": 0, "provenance": {"pdk": {"version": "test", "source": "test"}},
                "environment": {"engine": "ngspice", "engine_version": "46", "remote": {
                    "provider": "aws-batch-fleet", "job_id": f"klt-sim-fake{len(self.calls):03d}", "state": "done",
                    "spot": True, "instance_type": "c7i.4xlarge", "elapsed_seconds": self.elapsed,
                    "runner_klt_version": "0.5.0"}}}


def fake_studies(pdk, work):
    nom = (pc.passive_name("typical", "typical", "typical"), 27.0, 3.30)
    v = cell_vectors(nom)
    pt = r.extract_psrr(v["dm"], {"vdd": v["vdd"], "vss": v["vss"]}, FLOOR)
    return r.Studies(pt, FLOOR, FLOOR / 10, [("tau = 1e16 s", pt)], r.PsrrPoint(False, "rejected as intended"),
                     {"vdd": pt, "vss": pt}, [], [])


def fake_pdk(d: Path):
    p = d / "pdk"
    (p / "libs.tech" / "ngspice").mkdir(parents=True)
    (p / "libs.tech" / "ngspice" / "design.ngspice").write_text("* fake\n")
    return r.Pdk(p, "gf180mcuD", "test")


def study_args(**kw):
    a = dict(passive_corners=True, passive_scope="full", partition_split="auto", partition_retries=1, backend=None,
             batch_runner_version_check=None, batch_capacity_wait_s=None, batch_submit_retries=0,
             batch_retry_wait_s=0.0, keep_work=None, smoke=False)
    a.update(kw)
    return argparse.Namespace(**a)


def vec_entry(k, mode, vec=None, key=None, vout=None):
    vec = vec if vec is not None else cell_vectors(k)[mode]
    op = C.OpCheck(k[2] / 2 if vout is None else vout, k[2] / 2, {}, [], [])
    return {"vec": vec, "op": op, "key": k if key is None else key}


class PassivePlanTests(unittest.TestCase):
    def test_population_is_405_cells_and_1215_responses(self):
        self.assertEqual(len(FULL_WANT), 405)
        self.assertEqual(len(set(FULL_WANT)), 405)
        self.assertEqual(len(FULL_WANT) * len(r.MODES), 1215)
        self.assertEqual(r.STUDY_SCOPES, ("full",))
        self.assertEqual(list(pc.FULL_MOS), list(r.CORNERS))

    def test_mos_split_is_15_requests_of_81_with_exact_union(self):
        plan = r.build_schedule(pc.FULL_POINTS, "mos")
        self.assertEqual(sum(len(v) for v in plan.values()), 15)
        for mode, parts in plan.items():
            self.assertEqual([len(p.keys) for p in parts], [81] * 5)
            keys = [k for p in parts for k in p.keys]
            self.assertEqual(len(keys), len(set(keys)))  # no overlap
            self.assertEqual(set(keys), set(FULL_WANT))
        self.assertEqual(r.plan_problems(plan, FULL_WANT), [])

    def test_temperature_fallback_is_27_cell_requests_with_exact_union(self):
        plan = r.build_schedule(pc.FULL_POINTS, "mos-temp")
        self.assertEqual(sum(len(v) for v in plan.values()), 45)
        self.assertTrue(all(len(p.keys) == 27 for v in plan.values() for p in v))
        self.assertEqual(r.plan_problems(plan, FULL_WANT), [])
        # after an 81-cell preflight the dm plan keeps it and drops that corner's 27-cell partitions
        pf = r.partition_plan(pc.FULL_POINTS, "mos")[0]
        plan = r.build_schedule(pc.FULL_POINTS, "mos-temp", pf)
        self.assertEqual([len(p.keys) for p in plan["dm"]], [81] + [27] * 12)
        self.assertEqual(len(plan["vdd"]), 15)
        self.assertEqual(r.plan_problems(plan, FULL_WANT), [])

    def test_plan_problems_catch_missing_duplicate_and_unexpected(self):
        plan = r.build_schedule(pc.FULL_POINTS, "mos")
        bad = {**plan, "vdd": plan["vdd"][:-1]}
        self.assertTrue(any("missing cell" in p for p in r.plan_problems(bad, FULL_WANT)))
        bad = {**plan, "vss": plan["vss"] + [r.partition_plan(pc.FULL_POINTS, "mos-temp")[0]]}
        self.assertTrue(any("duplicate cell" in p for p in r.plan_problems(bad, FULL_WANT)))
        stray = r.Partition("xx", "typical", (99.0,), (("typical", 99.0, 3.3),))
        bad = {**plan, "dm": plan["dm"] + [stray]}
        self.assertTrue(any("unexpected cell" in p for p in r.plan_problems(bad, FULL_WANT)))
        self.assertTrue(r.plan_problems({**plan, "cm": plan["dm"]}, FULL_WANT))

    def test_requests_expand_to_exactly_their_partitions(self):
        pdk = r.Pdk(Path("/x/gf180mcuD"), "gf180mcuD", "test")
        base = C.ac_request(Path("/x/tb.spice"), pdk, r.CORNERS, r.TEMPS_C, r.SUPPLIES_V)
        for split in ("mos", "mos-temp"):
            got = []
            for part in r.partition_plan(pc.FULL_POINTS, split):
                req = r.partition_request(Path("/x/tb.spice"), pdk, part)
                cells = r.request_cells(req)
                self.assertEqual(sorted(cells), sorted(part.keys))
                got += cells
                # identical to the default grid request except the corner axes
                self.assertEqual(req["analysis"], base["analysis"])
                self.assertEqual(req["models"], base["models"])
                self.assertEqual(req["options"], base["options"])
                for proc in req["corners"]["process"]:
                    m, rz, cc = pc.split_passive_key((proc["name"], 0, 0))
                    self.assertEqual(proc["sections"], [m, *pc.passive_sections(rz, cc)])
            self.assertEqual(sorted(got), sorted(FULL_WANT))

    def test_cli_is_opt_in(self):
        for argv in (["--passive-scope", "full"], ["--passive-corners"], ["--partition-split", "mos"],
                     ["--passive-corners", "--passive-scope", "limited"]):
            with self.assertRaises(SystemExit):
                r.main(argv)


class PassivePairingTests(unittest.TestCase):
    def per_mode(self, keys=SMALL_WANT):
        return {m: {k: vec_entry(k, m) for k in keys} for m in r.MODES}

    def test_pairs_every_cell_and_both_rails(self):
        pts, probs = r.pair_cells(self.per_mode(), SMALL_WANT, FLOOR)
        self.assertEqual(probs, [])
        self.assertEqual(len(pts), len(SMALL_WANT))
        k = (pc.passive_name("ss", "worst", "best"), 125.0, 2.97)
        v = cell_vectors(k)
        self.assertAlmostEqual(pts[k].rej["vdd"].spot_db[1e4], r.C.interp_db(
            FREQ, r.db(v["dm"]["v(vout)"]) - r.db(v["vdd"]["v(vout)"]), 1e4), delta=1e-9)
        d = r.deltas_vs_typical(pts, "vdd", 1e4)
        self.assertEqual(len(d), len(SMALL_WANT) - 2)  # every non-typical cell
        self.assertLess(d[(pc.passive_name("typical", "typical", "best"), 27.0, 3.30)], 0)  # smaller CC: lower PSRR+

    def test_missing_excitation_and_cross_cell_pairing_are_refused(self):
        pm = self.per_mode()
        k0, k1 = SMALL_WANT[0], SMALL_WANT[1]
        del pm["vss"][k0]
        pts, probs = r.pair_cells(pm, SMALL_WANT, FLOOR)
        self.assertNotIn(k0, pts)
        self.assertTrue(any("no vss response" in p for p in probs))
        pm = self.per_mode()
        pm["vdd"][k0] = vec_entry(k1, "vdd")  # cell k1's response filed under k0
        pts, probs = r.pair_cells(pm, SMALL_WANT, FLOOR)
        self.assertNotIn(k0, pts)
        self.assertTrue(any("cross-cell pairing refused" in p for p in probs))

    def test_wrong_excitation_nonfinite_and_axis_mismatch_are_rejected(self):
        k = SMALL_WANT[3]
        for mutate, why in (
            (lambda pm: pm["vdd"].__setitem__(k, vec_entry(k, "vss", key=k)), "rail excitation"),
            (lambda pm: pm["dm"][k]["vec"].__setitem__("v(vout)", np.full(FREQ.shape, np.nan, complex)), "non-finite"),
            (lambda pm: pm["vss"][k]["vec"].__setitem__("frequency", FREQ.astype(complex) * 1.01), "frequency axes"),
        ):
            pm = self.per_mode()
            for m in pm:
                pm[m][k] = vec_entry(k, m, vec={n: a.copy() for n, a in cell_vectors(k)[m].items()})
            mutate(pm)
            pts, probs = r.pair_cells(pm, SMALL_WANT, FLOOR)
            self.assertNotIn(k, pts, why)
            self.assertTrue(any(why in p for p in probs), (why, probs))

    def test_floor_clamped_cell_is_a_lower_bound(self):
        pm = self.per_mode()
        k = SMALL_WANT[0]
        vec = dict(pm["vdd"][k]["vec"])
        vec["v(vout)"] = np.zeros_like(vec["v(vout)"])
        pm["vdd"][k] = vec_entry(k, "vdd", vec=vec)
        pts, probs = r.pair_cells(pm, SMALL_WANT, 1e-10)
        self.assertEqual(probs, [])
        self.assertTrue(pts[k].rej["vdd"].lower_bound)
        self.assertTrue(r._fmt_cell_val(pts[k], "vdd", 1e4).startswith(">= "))

    def test_op_mismatch_between_excitations_is_blocking(self):
        pm = self.per_mode()
        k = SMALL_WANT[2]
        pm["vss"][k] = vec_entry(k, "vss", vout=k[2] / 2 + 1e-3)
        _, probs = C.op_agreement(pm, SMALL_WANT)
        self.assertTrue(probs)

    def test_merge_rejects_overlap_foreign_and_missing_cells(self):
        parts = r.partition_plan(SMALL_POINTS, "mos")
        units = [(m, p, {k: vec_entry(k, m) for k in p.keys}) for m in r.MODES for p in parts]
        pm, probs = r.merge_units(units, SMALL_WANT)
        self.assertEqual(probs, [])
        self.assertEqual({m: len(v) for m, v in pm.items()}, {m: len(SMALL_WANT) for m in r.MODES})
        _, probs = r.merge_units(units + [units[0]], SMALL_WANT)
        self.assertTrue(any("returned by partitions" in p for p in probs))
        foreign = (units[0][0], units[1][1], units[0][2])  # partition 1 returning partition 0's cells
        _, probs = r.merge_units([foreign, *units[1:]], SMALL_WANT)
        self.assertTrue(any("does not contain it" in p for p in probs))
        self.assertTrue(any("missing cell" in p for p in probs))


class PassiveFingerprintTests(unittest.TestCase):
    TB = {"bench": r.TESTBENCH.read_text()}

    def test_default_fingerprint_is_the_historical_one(self):
        # the latest default record (before #155) states this fingerprint; the study code must not move it
        rec = (HERE / "records" / "20261010-163854-0127eb7.md").read_text()
        self.assertIn(f"sha256 `{r.mc.fingerprint(self.TB)}`", rec)
        self.assertNotIn("passive_study", r.mc.inputs(self.TB))
        self.assertEqual(r.mc.fingerprint(self.TB), r.mc.fingerprint(self.TB, {"experiment": "psrr"}))

    def test_study_identity_covers_scope_sections_and_factors(self):
        study = r.mc.study_retained("full")
        inp = r.mc.inputs(self.TB, study)
        self.assertEqual(inp["passive_study"]["cells"], 405)
        self.assertEqual(inp["passive_study"]["rz"]["worst"], {"section": "res_ss", "factor": 1.2})
        self.assertEqual(inp["passive_study"]["cc"]["best"], {"section": "mimcap_ff", "factor": 0.9})
        fp = r.mc.fingerprint(self.TB, study)
        self.assertNotEqual(fp, r.mc.fingerprint(self.TB))
        self.assertNotEqual(fp, r.mc.fingerprint(self.TB, r.mc.study_retained("limited")))
        P = r.mc._PASSIVE
        for name, new in (("MIM_FACTOR", {**P.MIM_FACTOR, "worst": 1.2}), ("RES_FACTOR", {**P.RES_FACTOR, "best": 0.7}),
                          ("_LEVEL_SUFFIX", {**P._LEVEL_SUFFIX, "worst": "ff"}), ("FULL_TEMPS", (-40.0, 27.0))):
            old = getattr(P, name)
            setattr(P, name, new)
            if name == "FULL_TEMPS":
                P.SCOPES["full"] = [(m, t, v) for m in P.FULL_MOS for t in new for v in P.FULL_SUPPLIES]
            try:
                self.assertNotEqual(r.mc.fingerprint(self.TB, study), fp, name)
                self.assertEqual(r.mc.fingerprint(self.TB), r.mc.fingerprint(self.TB, None), name)
            finally:
                setattr(P, name, old)
                P.SCOPES["full"] = P.FULL_POINTS

    def test_study_header_names_the_passive_inputs(self):
        lines = r.mc.fingerprint_lines(self.TB, r.mc.study_retained("full"))
        self.assertIn("RZ x CC passive-study", lines[0])
        self.assertNotIn("passive-study", r.mc.fingerprint_lines(self.TB)[0])


class PassiveStudyEndToEnd(unittest.TestCase):
    """The whole `--passive-corners` path against a fake klt (no simulator)."""

    def run_study(self, fake, points=SMALL_POINTS, **kw):
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = d / "psrr"
        for sub in ("records", "corners", "netlist-snapshots"):
            (root / sub).mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = r.run_passive_study(fake_pdk(d), study_args(**kw), runner=fake, studies_fn=fake_studies,
                                       root=root, gain_base=d / "no-gain", points_pvt=points)
        return code, root

    def outputs(self, root):
        return {sub: sorted(p.name for p in (root / sub).iterdir()) for sub in ("records", "corners", "netlist-snapshots")}

    def test_full_population_publishes_405_cells_from_1215_responses(self):
        fake = FakeKlt()
        code, root = self.run_study(fake, points=pc.FULL_POINTS)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(c[-1] for c in fake.calls), [81] * 15)
        rec = next((root / "records").glob("*.md"))
        md = rec.read_text()
        self.assertTrue(md.startswith(r.STUDY_TITLE))
        self.assertIn("405 cells", md)
        self.assertIn("1215 AC responses", md)
        rows = [ln for ln in md.splitlines() if re.match(r"^\| (typical|ff|ss|fs|sf) / .* / RZ ", ln)]
        self.assertEqual(len(rows), 405)
        for mode in r.MODES:
            self.assertIn(f"union 405 cells, 0 overlapping, 0 missing", md)
        # binding cell named with its complete identity; CC best lowers PSRR+ in the synthetic data
        self.assertRegex(md, r"\| PSRR\+ 10 kHz \| \*\*[0-9.]+\*\* \| \w+ / -?\d+ C / \d\.\d\d V / RZ \w+ / CC best \|")
        cdir = root / "corners" / rec.stem
        self.assertEqual(len(list(cdir.glob("*.psrr.dat"))), 405)
        self.assertEqual(len(list(cdir.glob("*.dm.dat"))) + len(list(cdir.glob("*.vdd.dat")))
                         + len(list(cdir.glob("*.vss.dat"))), 1215)
        pj = json.loads((cdir / "partitions.json").read_text())
        self.assertEqual(pj["population"], {"cells": 405, "excitations": ["dm", "vdd", "vss"], "responses": 1215})
        self.assertEqual({m: c["cells"] for m, c in pj["coverage"].items()}, {m: 405 for m in r.MODES})
        self.assertEqual(len([s for s in pj["submissions"] if s["outcome"] == "accepted"]), 15)
        self.assertTrue(all(s["job_id"].startswith("klt-sim-fake") for s in pj["submissions"]))
        # the stated fingerprint is recomputable from the retained inputs and carries the study block
        fp = re.search(r"^- \*\*Measurement fingerprint\*\*: version \d+, sha256 `([0-9a-f]{64})`", md, re.M).group(1)
        inputs = json.loads(md.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(harness_fp(inputs), fp)
        self.assertEqual(r.mc.fingerprint({"bench": r.TESTBENCH.read_text()}, inputs), fp)
        self.assertEqual(inputs["passive_study"]["cells"], 405)
        # snapshot: the committed DUT unchanged (passives are sections, not netlist edits) + every request
        snap = (root / "netlist-snapshots" / f"{rec.stem}.spice").read_text()
        self.assertIn(r.load_dut_text(), snap)
        self.assertEqual(r.C.G.guard_dut(r.load_dut_text()), [])
        self.assertEqual(snap.count("---- conditions: klt sim request"), 15)

    def test_invalid_response_publishes_nothing(self):
        bad = (pc.passive_name("ss", "best", "worst"), 125.0, 2.97)

        def tamper(mode, k, vec):
            if mode == "vss" and k == bad:
                vec["v(vout)"] = vec["v(vout)"].copy()
                vec["v(vout)"][10] = complex("nan")
            return vec
        code, root = self.run_study(FakeKlt(tamper=tamper))
        self.assertEqual(code, 2)
        self.assertEqual(self.outputs(root), {"records": [], "corners": [], "netlist-snapshots": []})

    def test_missing_cell_after_retries_publishes_nothing(self):
        drop = (pc.passive_name("typical", "worst", "worst"), 27.0, 3.30)
        code, root = self.run_study(FakeKlt(tamper=lambda m, k, v: None if (m, k) == ("vdd", drop) else v))
        self.assertEqual(code, 2)
        self.assertEqual(self.outputs(root), {"records": [], "corners": [], "netlist-snapshots": []})

    def test_wrong_rail_driven_publishes_nothing(self):
        def tamper(mode, k, vec):
            if mode == "vdd" and k == SMALL_WANT[5]:
                vec["v(vss)"] = np.full(FREQ.shape, 1e-3, complex)
            return vec
        code, root = self.run_study(FakeKlt(tamper=tamper))
        self.assertEqual(code, 2)
        self.assertEqual(self.outputs(root)["records"], [])

    def test_failed_partition_is_rerun_whole_and_recorded(self):
        fake = FakeKlt(fail_first={("vdd", "ss")})
        code, root = self.run_study(fake)
        self.assertEqual(code, 0)
        rec = next((root / "records").glob("*.md"))
        pj = json.loads((root / "corners" / rec.stem / "partitions.json").read_text())
        out = [(s["excitation"], s["partition"], s["attempt"], s["outcome"].split(" ")[0]) for s in pj["submissions"]]
        self.assertIn(("vdd", "ss", 1, "rejected"), out)
        self.assertIn(("vdd", "ss", 2, "accepted"), out)
        self.assertIn("rejected (", rec.read_text())

    def test_no_partition_retries_means_no_record(self):
        code, root = self.run_study(FakeKlt(fail_first={("vdd", "ss")}), partition_retries=0)
        self.assertEqual(code, 2)
        self.assertEqual(self.outputs(root)["records"], [])

    def test_slow_preflight_switches_to_temperature_partitions(self):
        fake = FakeKlt(elapsed=r.SPLIT_AT_FRACTION * r.SPLIT_REFERENCE_S)
        code, root = self.run_study(fake, points=[(m, t, 3.30) for m in ("typical", "ss") for t in (27.0, 125.0)])
        self.assertEqual(code, 0)
        # preflight: dm/typical over both temperatures (18 cells); then 27->9-cell (one T) partitions
        self.assertEqual(fake.calls[0], ("dm", "typical", 18))
        self.assertEqual(sorted(c[-1] for c in fake.calls[1:]), [9] * 10)
        md = next((root / "records").glob("*.md")).read_text()
        self.assertIn("-> split `mos-temp`", md)

    def test_fast_preflight_keeps_mos_partitions_and_capacity_refusal_is_resubmitted(self):
        fake = FakeKlt(elapsed=10, refuse=1)
        code, root = self.run_study(fake, batch_submit_retries=2)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 6)
        md = next((root / "records").glob("*.md")).read_text()
        self.assertIn("submit refused (fleet capacity)", md)
        self.assertIn("-> split `mos`", md)


def harness_fp(inputs):
    import harness
    return harness.measurement_fingerprint(inputs)


if __name__ == "__main__":
    unittest.main(verbosity=1)
