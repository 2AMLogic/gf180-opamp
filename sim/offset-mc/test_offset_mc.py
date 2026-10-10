#!/usr/bin/env python3
"""Regression tests for the offset Monte Carlo experiment (issue #45).

The unit tests need no simulator: statistics and extraction are checked
against synthetic klt reports with known answers, and the source guards by
mutating the committed testbench / DUT text in memory. `SimControlTests`
runs two deterministic single-unit LOCAL simulations (the imbalance control)
and is skipped when klt, ngspice or the PDK is unavailable; it never runs a
Monte Carlo grid.

    python3 sim/offset-mc/test_offset_mc.py          # or: python3 -m unittest
"""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_offset_mc as r  # noqa: E402
from harness import Pdk, PdkNotFound, find_pdk, require_prereqs  # noqa: E402


def corner(proc: str, idx: int | None, vos: float, *, vinp: float = r.VCM_V, vout: float | None = None,
           status: str = "pass") -> dict:
    """A synthetic klt corner entry. `vout` defaults to a consistent value."""
    vout = vinp + vos if vout is None else vout
    c = {
        "corner_id": f"{proc}/vcm=1.650_vdd=3.300V/27C" + (f"/mc{idx}" if idx is not None else ""),
        "process": proc,
        "status": status,
        "measurements": [
            {"name": "vout_v", "value": vout},
            {"name": "vinp_v", "value": vinp},
            {"name": "vos_v", "value": vos},
        ],
    }
    if idx is not None:
        c["monte_carlo"] = {"sample_index": idx, "seed": 1000 + idx, "process_seed": 7, "mismatch_seed": 2000 + idx}
    return c


def synthetic_report(n: int, sigma_by_corner: dict[str, float], mean_by_corner: dict[str, float]) -> dict:
    """Deterministic, alternating-sign samples (known mean, known pattern)."""
    corners = []
    for p, sig in sigma_by_corner.items():
        for i in range(n):
            sign = 1 if i % 2 == 0 else -1
            corners.append(corner(p, i, mean_by_corner[p] + sign * sig))
    return {"corners": corners}


class StatisticsTests(unittest.TestCase):
    def test_known_values(self):
        s = r.stats_of([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(s.n, 5)
        self.assertAlmostEqual(s.mean, 3.0)
        self.assertAlmostEqual(s.sigma, math.sqrt(2.5))  # sample sigma, n-1
        self.assertAlmostEqual(s.three_sigma, 3 * math.sqrt(2.5))
        self.assertAlmostEqual(s.lo, 3.0 - 3 * math.sqrt(2.5))
        self.assertAlmostEqual(s.hi, 3.0 + 3 * math.sqrt(2.5))
        self.assertEqual((s.vmin, s.vmax), (1.0, 5.0))
        self.assertAlmostEqual(s.skew, 0.0)

    def test_constant_samples_have_zero_sigma(self):
        s = r.stats_of([2e-3] * 10)
        self.assertEqual(s.sigma, 0.0)
        self.assertEqual(s.three_sigma, 0.0)

    def test_rejects_degenerate_input(self):
        with self.assertRaises(ValueError):
            r.stats_of([1.0])
        with self.assertRaises(ValueError):
            r.stats_of([1.0, float("nan")])

    def test_worst_corner_selection_separates_sigma_from_extreme(self):
        stats = {
            "typical": r.stats_of([-1.0, 1.0] * 5),  # sigma ~1.05, mean 0
            "ff": r.stats_of([9.0, 11.0] * 5),  # sigma ~1.05, mean 10  -> worst extreme
            "ss": r.stats_of([-2.0, 2.0] * 5),  # larger sigma, mean 0   -> worst sigma
        }
        w = r.worst_corners(stats)
        self.assertEqual(w["sigma"], "ss")
        self.assertEqual(w["extreme"], "ff")

    def test_per_corner_stats_uses_each_corner_separately(self):
        rep = synthetic_report(10, {"typical": 1e-3, "ff": 2e-3}, {"typical": 0.0, "ff": 5e-3})
        ex = r.extract_samples(rep, ["typical", "ff"], 10)
        self.assertEqual(ex.problems, [])
        st = r.per_corner_stats(ex.samples, ["typical", "ff"])
        self.assertAlmostEqual(st["typical"].mean, 0.0, places=12)
        self.assertAlmostEqual(st["ff"].mean, 5e-3, places=12)
        self.assertGreater(st["ff"].sigma, st["typical"].sigma)

    def test_rollup_crosscheck_flags_disagreement(self):
        st = {"typical": r.stats_of([-1e-3, 1e-3] * 5)}
        ok = {"typical/vcm=1.650_vdd=3.300V/27C": {"corner_id": "typical/x", "mean": 0.0, "sigma": st["typical"].sigma}}
        bad = {"typical/vcm=1.650_vdd=3.300V/27C": {"corner_id": "typical/x", "mean": 0.0, "sigma": 2 * st["typical"].sigma}}
        self.assertEqual(r.rollup_crosscheck(st, ok), [])
        self.assertTrue(r.rollup_crosscheck(st, bad))
        self.assertEqual(r.rollup_crosscheck(st, {}), [])


class ExtractionTests(unittest.TestCase):
    CORNERS = ["typical", "ff"]

    def report(self, **over):
        rep = synthetic_report(6, {"typical": 1e-3, "ff": 1e-3}, {"typical": 0.0, "ff": 0.0})
        return rep

    def test_clean_report_is_valid(self):
        ex = r.extract_samples(self.report(), self.CORNERS, 6)
        self.assertEqual(ex.problems, [])
        self.assertEqual({p: len(v) for p, v in ex.samples.items()}, {"typical": 6, "ff": 6})
        s = ex.samples["typical"][0]
        self.assertEqual((s.seed, s.mismatch_seed, s.process_seed), (1000, 2000, 7))

    def test_offset_is_the_full_precision_vos_not_the_rounded_difference(self):
        # vout/vinp are printed to ~7 digits; vos_v is the offset of record.
        rep = {"corners": [corner("typical", 0, -1.441426e-5, vout=1.649986)]}
        ex = r.extract_samples(rep, ["typical"], 1)
        self.assertEqual(ex.problems, [])
        self.assertEqual(ex.samples["typical"][0].offset_v, -1.441426e-5)

    def test_cross_check_disagreement_is_a_problem(self):
        rep = {"corners": [corner("typical", 0, 1e-3, vout=1.65 + 5e-3)]}
        ex = r.extract_samples(rep, ["typical"], 1)
        self.assertTrue(any("cross-check" in p for p in ex.problems))
        self.assertEqual(ex.samples, {})

    def test_wrong_common_mode_is_a_problem(self):
        rep = {"corners": [corner("typical", 0, 1e-3, vinp=1.5)]}
        ex = r.extract_samples(rep, ["typical"], 1)
        self.assertTrue(any("vinp" in p for p in ex.problems))

    def test_clipped_follower_is_invalid(self):
        rep = {"corners": [corner("typical", 0, 0.4)]}
        ex = r.extract_samples(rep, ["typical"], 1)
        self.assertTrue(any("valid follower window" in p for p in ex.problems))

    def test_missing_and_nonfinite_values_are_problems_not_dropped_silently(self):
        c = corner("typical", 0, 1e-3)
        c["measurements"][2]["value"] = None
        ex = r.extract_samples({"corners": [c]}, ["typical"], 1)
        self.assertTrue(ex.problems)
        c2 = corner("typical", 0, float("nan"))
        self.assertTrue(r.extract_samples({"corners": [c2]}, ["typical"], 1).problems)

    def test_short_corner_and_duplicate_index_are_problems(self):
        rep = self.report()
        rep["corners"] = [c for c in rep["corners"] if not c["corner_id"].endswith("ff/vcm=1.650_vdd=3.300V/27C/mc5")]
        ex = r.extract_samples(rep, self.CORNERS, 6)
        self.assertTrue(any("5 valid samples, expected 6" in p for p in ex.problems))
        rep2 = self.report()
        rep2["corners"].append(dict(rep2["corners"][0]))
        self.assertTrue(any("duplicate" in p for p in r.extract_samples(rep2, self.CORNERS, 6).problems))

    def test_unexpected_corner_is_a_problem(self):
        rep = {"corners": [corner("fs", 0, 1e-3)]}
        self.assertTrue(r.extract_samples(rep, ["typical"], 1).problems)

    def test_mismatch_activity_helpers(self):
        rep = {"environment": {"monte_carlo": {"family_mismatch": [
            {"family": "resistor", "active": False}, {"family": "mosfet", "active": True}]}}}
        self.assertIs(r.mosfet_active(rep), True)
        self.assertIsNone(r.mosfet_active({}))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.pdk = Pdk(Path("/nonexistent"), "gf180mcuD", "test")

    def test_grid_request_meets_the_issue_conditions(self):
        req = r.mc_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, r.MC_N, r.MC_SEED, r.MC_VARY)
        mc = req["monte_carlo"]
        self.assertGreaterEqual(mc["n"], 300)
        self.assertIsInstance(mc["seed"], int)
        self.assertEqual(mc["vary"], "mismatch")
        self.assertEqual([p["name"] for p in req["corners"]["process"]], ["typical", "ff", "ss", "fs", "sf"])
        self.assertEqual(req["corners"]["temperature_c"], [27.0])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [3.3], "vcm": [1.65]})
        self.assertEqual(len(r.CORNERS) * mc["n"], 1500)
        self.assertEqual(req["analysis"]["kind"], "dc")

    def test_request_never_pins_a_backend(self):
        # The backend is klt's decision (--backend / $KLT_SIM_BACKEND); a
        # request that hard-coded `local` would hand-run the grid here.
        req = r.mc_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, r.MC_N, r.MC_SEED, r.MC_VARY)
        self.assertNotIn("backend", req)

    def test_every_measurement_is_a_native_meas_card(self):
        for m in r.MEASUREMENTS:
            self.assertTrue(m["spice"].startswith(".meas dc "), m)

    def test_driver_never_launches_ngspice(self):
        text = (HERE / "run_offset_mc.py").read_text()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn('["ngspice"', code)
        self.assertNotIn("xargs", code)
        self.assertNotIn("Popen", code)


class GuardTests(unittest.TestCase):
    def tb(self) -> str:
        return r.TESTBENCH.read_text()

    def test_committed_testbench_passes(self):
        self.assertEqual(r.guard_testbench(self.tb()), [])

    def test_missing_dut_include_fails(self):
        t = self.tb().replace(".include 'opamp_two_stage.dut.spice'", "")
        self.assertTrue(r.guard_testbench(t))

    def test_hand_declared_transistor_fails(self):
        t = self.tb() + "\nM9 a b c d nfet_03v3 w=1u l=1u\n"
        self.assertTrue(any("MOSFET" in e for e in r.guard_testbench(t)))

    def test_pdk_device_instance_fails(self):
        t = self.tb() + "\nXM9 a b c d nfet_03v3 w=1u l=1u\n"
        self.assertTrue(r.guard_testbench(t))

    def test_non_follower_fails(self):
        t = self.tb().replace("vinp vout vout ibias", "vinp vinn vout ibias")
        self.assertTrue(any("unity follower" in e for e in r.guard_testbench(t)))

    def test_switch_before_the_pdk_include_fails(self):
        lines = self.tb().splitlines()
        sw = next(ln for ln in lines if ln.startswith(".param sw_stat_mismatch"))
        lines.remove(sw)
        i = next(i for i, ln in enumerate(lines) if ln.startswith(".include 'design.ngspice'"))
        lines.insert(i, sw)
        self.assertTrue(any("precedes" in e for e in r.guard_testbench("\n".join(lines))))

    def test_missing_or_duplicate_switch_fails(self):
        no_sw = "\n".join(ln for ln in self.tb().splitlines() if not ln.startswith(".param sw_stat_mismatch"))
        self.assertTrue(r.guard_testbench(no_sw))
        self.assertTrue(r.guard_testbench(self.tb() + "\n.param sw_stat_mismatch=1\n"))

    def test_global_statistics_must_stay_off(self):
        self.assertTrue(r.guard_testbench(self.tb() + "\n.param sw_stat_global=1\n"))

    def test_circuit_body_only(self):
        self.assertTrue(r.guard_testbench(self.tb() + "\n.temp 27\n"))

    def test_committed_dut_passes_and_alterations_fail(self):
        dut = r.load_dut_text()
        self.assertEqual(r.guard_dut(dut), [])
        self.assertTrue(r.guard_dut(dut.replace("W=72u", "W=73u")))
        self.assertTrue(r.guard_dut(dut.replace("XRZ", "XRZ0")))


class MaterialiseTests(unittest.TestCase):
    def fake_pdk(self, root: Path) -> Pdk:
        (root / "libs.tech" / "ngspice").mkdir(parents=True)
        (root / "libs.tech" / "ngspice" / "design.ngspice").write_text(".param sw_stat_mismatch = 0\n")
        return Pdk(root, "gf180mcuD", "test")

    def test_switch_is_the_only_rewritten_value_and_includes_are_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            pdk = self.fake_pdk(Path(d) / "pdk")
            for sw in (0, 1):
                work = Path(d) / f"w{sw}"
                tb = r.materialise(work, pdk, mismatch=sw).read_text()
                self.assertIn(f".param sw_stat_mismatch={sw}", tb)
                self.assertIn(f"'{work / 'design.ngspice'}'", tb)
                self.assertIn(f"'{work / r.DUT_INCLUDE_NAME}'", tb)
                orig = r.TESTBENCH.read_text().splitlines()
                got = tb.splitlines()
                diff = [(a, b) for a, b in zip(orig, got) if a != b and not a.startswith("*")]
                # two includes (+ the switch line when it is turned off)
                self.assertEqual(len(diff), 2 + (1 if sw == 0 else 0))
            # the DUT include is the committed export, wrapper-normalised
            self.assertEqual((work / r.DUT_INCLUDE_NAME).read_text(), r.load_dut_text())

    def test_committed_netlist_is_never_modified_by_the_imbalance_copy(self):
        before = r.DUT_EXPORT.read_bytes()
        dut = r.load_dut_text()
        imb = r.imbalance_dut(dut)
        self.assertEqual(r.DUT_EXPORT.read_bytes(), before)
        a, b = dut.splitlines(), imb.splitlines()
        changed = [(x, y) for x, y in zip(a, b) if x != y]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0][0].lower().startswith("xm1 "))
        self.assertIn(r.IMBALANCE_W_TO, changed[0][1])
        # the guard accepts the perturbation only when told to
        self.assertTrue(r.guard_dut(imb))
        self.assertEqual(r.guard_dut(imb, allow_changed=(r.IMBALANCE_DEVICE,)), [])

    def test_imbalance_fails_loudly_if_the_target_line_moves(self):
        with self.assertRaises(RuntimeError):
            r.imbalance_dut("XM1 a b c d nfet_03v3 L=1u W=9u\n")


class AppendOnlyTests(unittest.TestCase):
    def test_existing_record_paths_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first = r.claim_record_paths(root, "20261009-000000-abcdef0")
            first["corners"].mkdir(parents=True)
            first["record"].parent.mkdir(parents=True, exist_ok=True)
            first["record"].write_text("old")
            with self.assertRaises(FileExistsError):
                r.claim_record_paths(root, "20261009-000000-abcdef0")
            self.assertEqual(first["record"].read_text(), "old")


def _sim_available() -> bool:
    if not (shutil.which("klt") and shutil.which("ngspice")):
        return False
    try:
        find_pdk()
    except PdkNotFound:
        return False
    return True


@unittest.skipUnless(_sim_available() or require_prereqs(), "klt / ngspice / gf180mcu PDK not available")
class SimControlTests(unittest.TestCase):
    """Two deterministic single-unit LOCAL runs (no Monte Carlo)."""

    def test_imbalance_shifts_the_mean_offset_visibly(self):
        pdk = find_pdk()
        with tempfile.TemporaryDirectory(prefix="offmc-test-") as d:
            base = r.run_deterministic("base", "", pdk, Path(d), mismatch=0)
            imb = r.run_deterministic("imb", "", pdk, Path(d), mismatch=0,
                                      dut_text=r.imbalance_dut(r.load_dut_text()),
                                      allow_changed=(r.IMBALANCE_DEVICE,))
        self.assertIsNotNone(base.offset_v, base.error)
        self.assertIsNotNone(imb.offset_v, imb.error)
        self.assertGreater(abs(imb.offset_v - base.offset_v), r.IMBALANCE_MIN_SHIFT_V)
        # and the unperturbed deterministic offset is small (balanced design)
        self.assertLess(abs(base.offset_v), r.IMBALANCE_MIN_SHIFT_V)


class MeasurementFingerprint(unittest.TestCase):
    """Issue #89: the runner and the fingerprint share ONE configuration source."""

    TB = r.TESTBENCH.read_text()

    def inp(self):
        return r.mc.inputs({"bench": self.TB})

    def test_runner_constants_are_the_fingerprinted_ones(self):
        for n in ("CORNERS", "PASSIVE_SECTIONS", "MEASUREMENTS"):
            self.assertIs(getattr(r, n), getattr(r.mc, n), n)
        for n in ("TEMP_C", "VDD_V", "VCM_V", "MC_N", "MC_SEED", "MC_VARY", "CONTROL_N", "SIM_ARGS",
                  "IMBALANCE_DEVICE", "IMBALANCE_W_FROM", "IMBALANCE_W_TO"):
            self.assertEqual(getattr(r, n), getattr(r.mc, n), n)

    def test_mc_request_is_what_the_fingerprint_hashes(self):
        pdk = Pdk(Path("/nonexistent"), "gf180mcuD", "test")
        req = r.mc_request(Path("/x/tb.spice"), pdk, r.CORNERS, r.MC_N, r.MC_SEED, r.MC_VARY)
        inp = self.inp()
        self.assertEqual(req["monte_carlo"], {k: inp["monte_carlo"][k] for k in ("n", "seed", "vary")})
        self.assertEqual(req["analysis"], {"kind": inp["analysis"]["kind"], "args": inp["analysis"]["args"]})
        self.assertEqual(req["measurements"], inp["analysis"]["measurements"])
        self.assertEqual(req["corners"]["temperature_c"], inp["corners"]["temperature_c"])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": inp["corners"]["supply_v"], "vcm": inp["corners"]["vcm_v"]})
        self.assertEqual(req["models"]["lib"], inp["models"]["lib"])

    def test_model_switch_in_the_bench_moves_the_fingerprint(self):
        self.assertIn("sw_stat_mismatch=1", self.TB)
        self.assertNotEqual(r.mc.fingerprint({"bench": self.TB.replace("sw_stat_mismatch=1", "sw_stat_mismatch=0")}),
                            r.mc.fingerprint({"bench": self.TB}))

    def test_record_embeds_header_and_inputs(self):
        blob = "\n".join(r.mc.fingerprint_lines({"bench": self.TB}) + r.mc.inputs_section({"bench": self.TB}))
        self.assertIn(r.mc.fingerprint({"bench": self.TB}), blob)
        self.assertIn("## Measurement fingerprint inputs", blob)


def grid_corner(proc: str, t: float, vdd: float, idx: int, vos: float, *, vcm: float | None = None) -> dict:
    """A synthetic grid sample; `vcm` defaults to the commanded VDD/2."""
    vcm = round(vdd / 2, 6) if vcm is None else vcm
    c = corner(proc, idx, vos, vinp=vcm)
    c["corner_id"] = f"{proc}/vcm={vcm:.3f}_vdd={vdd:.3f}V/{t:g}C/mc{idx}"
    c["temperature_c"] = t
    c["supply_v"] = {"vdd": vdd, "vcm": vcm}
    return c


def grid_report(n: int, sigma=1e-3, mean_of=lambda k: 0.0) -> dict:
    corners = []
    for k in r.grid_keys("full"):
        for i in range(n):
            corners.append(grid_corner(*k, i, mean_of(k) + (sigma if i % 2 == 0 else -sigma)))
    return {"corners": corners}


class GridTests(unittest.TestCase):
    """Issue #106: the 45-point T/VDD grid (`--grid full`), offline."""

    def setUp(self):
        self.pdk = Pdk(Path("/nonexistent"), "gf180mcuD", "test")

    def test_full_grid_request_is_one_45_point_monte_carlo_request(self):
        req = r.grid_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, "full", r.MC_N, r.MC_SEED, r.MC_VARY)
        self.assertEqual(req["monte_carlo"], {"n": 300, "seed": r.MC_SEED, "vary": "mismatch"})
        self.assertEqual(req["corners"]["temperature_c"], [-40.0, 27.0, 125.0])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [2.97, 3.3, 3.63], "vcm": [1.485, 1.65, 1.815]})
        n_points = len(req["corners"]["process"]) * len(req["corners"]["temperature_c"]) * len(req["corners"]["supply_v"]["vdd"])
        self.assertEqual(n_points, 45)
        self.assertEqual(len(r.grid_keys("full")), 45)
        self.assertEqual(n_points * req["monte_carlo"]["n"], 13500)
        self.assertNotIn("backend", req)  # the batch fleet is klt's decision ($KLT_SIM_BACKEND)
        # the grid axes are the gain bench's (one source for every 45-point bench)
        gain = r.mc._GAIN
        self.assertEqual(req["corners"]["temperature_c"], [float(t) for t in gain.TEMPS_C])
        self.assertEqual(req["corners"]["supply_v"]["vdd"], [float(v) for v in gain.SUPPLIES_V])

    def test_full_grid_is_sharded_over_fleet_jobs_and_nominal_is_not(self):
        req = r.grid_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, "full", r.MC_N, r.MC_SEED, r.MC_VARY,
                             hosts=r.GRID_HOSTS)
        self.assertEqual(req["remote"], {"hosts": r.GRID_HOSTS})
        self.assertGreater(r.GRID_HOSTS, 1)
        # each shard stays within half the fleet's 3600 s job cap at the measured 446 s / 1500 units
        self.assertLessEqual(45 * r.MC_N / r.GRID_HOSTS * 446 / 1500, 3600 / 2)
        self.assertNotIn("remote", r.grid_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, "full", 4, 1, "mismatch"))
        self.assertNotIn("remote", r.mc_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, r.MC_N, r.MC_SEED, r.MC_VARY))

    def test_lost_shard_refusals_are_counted_and_other_errors_are_not(self):
        refused = grid_corner("ss", -40.0, 3.63, 0, 0.0)
        refused["status"] = "error"
        refused["diagnostics"] = [{"severity": "error", "code": "lost_shard", "message":
                                   "shard lost: launch failed (exit 1): error: 8 instance(s) already running + 1 requested"}]
        timed_out = grid_corner("ss", -40.0, 3.63, 1, 0.0)
        timed_out["status"] = "error"
        timed_out["diagnostics"] = [{"severity": "error", "code": "batch_job_timeout", "message": "job exceeded 3600s"}]
        self.assertEqual(r.lost_shard_refusals({"corners": [refused, timed_out, grid_corner("ff", 27.0, 3.3, 0, 0.0)]}), 1)

    def test_remote_jobs_reads_single_and_sharded_reports(self):
        one = {"environment": {"remote": {"job_id": "a"}}}
        fleet = {"environment": {"remote": {"fleet": [{"job_id": "a"}, None, {"job_id": "c"}]}}}
        self.assertEqual([j.get("job_id") for j in r.remote_jobs(one)], ["a"])
        self.assertEqual([j.get("job_id") for j in r.remote_jobs(fleet)], ["a", None, "c"])
        self.assertEqual(r.remote_jobs({}), [])

    def test_nominal_grid_request_is_the_issue_45_request(self):
        a = r.grid_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, "nominal", r.MC_N, r.MC_SEED, r.MC_VARY)
        b = r.mc_request(Path("/x/tb.spice"), self.pdk, r.CORNERS, r.MC_N, r.MC_SEED, r.MC_VARY)
        self.assertEqual(a, b)

    def test_clean_grid_report_is_valid_per_point(self):
        samples, probs = r.extract_grid(grid_report(4), "full", 4)
        self.assertEqual(probs, [])
        self.assertEqual(len(samples), 45)
        self.assertTrue(all(len(v) == 4 for v in samples.values()))

    def test_vinp_is_checked_against_each_points_own_vcm(self):
        rep = grid_report(2)
        # a sample at 2.97 V whose vinp stayed at the nominal 1.65 V (supply alter not applied)
        bad = next(c for c in rep["corners"] if c["supply_v"]["vdd"] == 2.97)
        bad["measurements"][1]["value"] = 1.65
        bad["measurements"][0]["value"] = 1.65 + bad["measurements"][2]["value"]
        _, probs = r.extract_grid(rep, "full", 2)
        self.assertTrue(any("vinp" in p and "1.485" in p for p in probs), probs)

    def test_missing_unexpected_and_short_points_are_problems(self):
        rep = grid_report(2)
        rep["corners"] = [c for c in rep["corners"] if not (c["process"] == "sf" and c["temperature_c"] == 125.0
                                                            and c["supply_v"]["vdd"] == 3.63)]
        _, probs = r.extract_grid(rep, "full", 2)
        self.assertTrue(any("sf / 125 C / 3.63 V" in p and "0 valid samples" in p for p in probs), probs)
        rep2 = grid_report(2)
        rep2["corners"].append(grid_corner("typical", 85.0, 3.3, 0, 1e-3))
        self.assertTrue(any("unexpected grid point" in p for p in r.extract_grid(rep2, "full", 2)[1]))
        rep3 = grid_report(2)
        del rep3["corners"][0]["temperature_c"]
        self.assertTrue(any("unparseable" in p for p in r.extract_grid(rep3, "full", 2)[1]))

    def test_offset_regression_in_the_shared_extraction_reaches_the_grid(self):
        # the grid validates every point through extract_samples: same offset of record
        samples, _ = r.extract_grid(grid_report(2, sigma=2e-3), "full", 2)
        k = ("ss", -40.0, 2.97)
        self.assertEqual(sorted(s.offset_v for s in samples[k]), [-2e-3, 2e-3])

    def test_worst_point_separates_sigma_from_linear_three_sigma(self):
        kx, ks = ("ff", 125.0, 3.63), ("ss", -40.0, 2.97)
        rep = grid_report(10, sigma=1e-3, mean_of=lambda k: 10e-3 if k == kx else 0.0)
        for c in rep["corners"]:  # wider spread at ks, zero mean
            if r._point_of(c) == ks:
                c["measurements"][2]["value"] *= 2
                c["measurements"][0]["value"] = c["measurements"][1]["value"] + c["measurements"][2]["value"]
        samples, probs = r.extract_grid(rep, "full", 10)
        self.assertEqual(probs, [])
        st = r.grid_stats(samples)
        w = r.worst_points(st)
        self.assertEqual(w["sigma"], ks)
        self.assertEqual(w["extreme"], kx)
        self.assertAlmostEqual(st[kx].worst_extreme, abs(st[kx].mean) + 3 * st[kx].sigma)

    def test_grid_rollup_crosscheck_is_per_point(self):
        rep = grid_report(4)
        st = r.grid_stats(r.extract_grid(rep, "full", 4)[0])
        k = ("fs", 27.0, 3.63)
        cid = "fs/vcm=1.815_vdd=3.630V/27C"
        good = {"name": "vos_v", "monte_carlo": {"by_corner": [{"corner_id": cid, "mean": st[k].mean, "stddev": st[k].sigma}]}}
        rep["measurements"] = [good]
        self.assertEqual(r.grid_rollup_crosscheck(st, rep), [])
        rep["measurements"] = [{"name": "vos_v", "monte_carlo": {"by_corner": [
            {"corner_id": cid, "mean": st[k].mean, "stddev": 2 * st[k].sigma}]}}]
        probs = r.grid_rollup_crosscheck(st, rep)
        self.assertEqual(len(probs), 1)
        self.assertIn("fs / 27 C / 3.63 V", probs[0])

    def test_nominal_reference_reproduces_the_committed_27c_record(self):
        ref = r.nominal_reference()
        if not ref:
            self.skipTest("committed nominal samples not present")
        w = max(ref, key=lambda p: ref[p].worst_extreme)
        self.assertEqual(w, "sf")
        self.assertAlmostEqual(ref[w].worst_extreme * 1e3, 15.635, places=3)  # the record's own figure
        self.assertEqual({st.n for st in ref.values()}, {300})

    def _record(self, stats, problems=()):
        det = r.DetRun("switch-off", "", -1.4e-5)
        imb = r.DetRun("imbalance", "", -8.5e-3)
        off = r.stats_of([-1.4e-5] * 4)
        rep = {"environment": {"remote": {"provider": "aws-batch-fleet", "job_id": "klt-sim-test123"}}}
        return r.build_grid_record(
            record="20991231-000000-abcdef0", stamp=__import__("datetime").datetime(2099, 12, 31),
            pdk=self.pdk, ngspice="ngspice-x", kver="klt x", report=rep, stats=stats,
            worst=r.worst_points(stats), stats_problems=list(problems), sw_off=det, imb=imb, proc_stats=None,
            proc_note="n/a", off_stats=off, off_note="", wall_s=1.0, dut_sha="0" * 64, n_units=45 * 2,
            nominal_ref=r.nominal_reference())

    def test_grid_record_states_worst_point_next_to_the_27c_figure_without_a_bound(self):
        st = r.grid_stats(r.extract_grid(grid_report(4), "full", 4)[0])
        md = self._record(st)
        self.assertTrue(md.startswith("# Offset Monte Carlo PVT grid record"))
        self.assertIn("`klt-sim-test123`", md)
        self.assertIn("Worst linear 3-sigma offset over the 45-point grid", md)
        self.assertIn("27 C / 3.30 V figure", md)
        self.assertIn(r.NOMINAL_RECORD, md)
        self.assertIn("No numeric offset bound is proposed, ratified or judged here", md)
        self.assertIn("#62", md)
        self.assertNotRegex(md, r"(?i)\b(pass|fail)(es|ed)?\b.*bound")
        for k in r.grid_keys("full"):  # every point has a row
            self.assertIn(f"| {k[0]} | {k[1]:g} | {k[2]:.2f} | 4 |", md)
        # the record carries the full-grid fingerprint, and it round-trips
        fp = r.mc.fingerprint({"bench": r.TESTBENCH.read_text()}, {"grid": "full"})
        self.assertIn(fp, md)
        body = md.split("```json\n", 1)[1].split("\n```", 1)[0]
        import json as _json
        from harness import measurement_fingerprint
        self.assertEqual(measurement_fingerprint(_json.loads(body)), fp)


class GridFingerprint(unittest.TestCase):
    TB = r.TESTBENCH.read_text()

    def test_full_grid_moves_the_fingerprint_and_nominal_stays_put(self):
        t = {"bench": self.TB}
        self.assertNotEqual(r.mc.fingerprint(t, {"grid": "full"}), r.mc.fingerprint(t))
        self.assertEqual(r.mc.fingerprint(t, {"grid": "nominal"}), r.mc.fingerprint(t))
        self.assertNotIn("grid", r.mc.inputs(t))  # nominal inputs are the pre-#106 ones
        # the nominal fingerprint of the committed bench/config (pre-#106 value)
        self.assertEqual(r.mc.fingerprint(t), "2b73d9b7d0c355fede9701ac29e8a6d985641e8657803dfc3031d7aefa0e7de3")

    def test_full_grid_request_is_what_the_fingerprint_hashes(self):
        pdk = Pdk(Path("/nonexistent"), "gf180mcuD", "test")
        req = r.grid_request(Path("/x/tb.spice"), pdk, r.CORNERS, "full", r.MC_N, r.MC_SEED, r.MC_VARY)
        inp = r.mc.inputs({"bench": self.TB}, {"grid": "full"})
        self.assertEqual(req["corners"]["temperature_c"], inp["corners"]["temperature_c"])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": inp["corners"]["supply_v"], "vcm": inp["corners"]["vcm_v"]})
        self.assertEqual(req["monte_carlo"], {k: inp["monte_carlo"][k] for k in ("n", "seed", "vary")})

    def test_unknown_grid_is_rejected(self):
        with self.assertRaises(ValueError):
            r.mc.grid_axes("half")


if __name__ == "__main__":
    unittest.main(verbosity=1)
