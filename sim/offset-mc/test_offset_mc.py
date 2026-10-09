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


if __name__ == "__main__":
    unittest.main(verbosity=1)
