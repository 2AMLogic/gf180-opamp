#!/usr/bin/env python3
"""Offline tests of the per-experiment measurement-configuration modules (issue #89).

Stdlib only: no simulator, no numpy. The runners' sharing of these modules is
tested next to each runner (test_<experiment>.py); the report gate's use of
them is tested in report/test_report.py.

    python3 sim/test_measurement_config.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402

EXPERIMENTS = ("noise", "offset-mc", "cmrr", "cmrr-mc", "psrr", "psrr-mc", "gain-gbw-pm-mc", "slew-swing-power",
               "input-common-mode")

#: Public constants that deliberately do NOT enter the fingerprint (identity
#: or cosmetic). Anything else a config module defines must move its inputs.
NOT_FINGERPRINTED = {
    "EXPERIMENT": "identity label (the experiment is named separately)",
    "FINGERPRINT_VERSION": "harness normalisation version, stored separately",
    "TESTBENCHES_REL": "where the report reads the benches from",
    "MODULE_REL": "pointer printed in the record",
    "FIGURES": "the selectable figure set; selection comes from the record's own inputs",
    "QUIET": "folded into MODES at import (covered through it)",
    "OP_PRINT": "derived from DEVICES (covered through it)",
    "OP_TAIL": "derived from DEVICES (covered through it)",
    "CONTROLS": "names/descriptions are cosmetic; the Ibias overrides are asserted in test_control_ibias_moves",
    "SIM_SOURCE": "name of the swept source, restated inside SIM_ARGS",
}


def load(exp: str):
    return harness.load_config_module(HERE / exp / "measurement_config.py")


def texts(exp: str, mod) -> dict:
    return {k: (REPO / rel).read_text() for k, rel in mod.TESTBENCHES_REL.items()}


def last_replace(text: str, old: str, new: str) -> str:
    """Replace the last occurrence (the code line, not a header comment quoting it)."""
    head, sep, tail = text.rpartition(old)
    assert sep, old
    return head + new + tail


def perturb(v):
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1  # stays an int (some constants are indices)
    if isinstance(v, float):
        return v * 1.5 + 1
    if isinstance(v, str):
        return v + "_x"
    if isinstance(v, (list, tuple)):
        return type(v)([perturb(v[0]), *v[1:]]) if v else type(v)(["x"])
    if isinstance(v, dict):
        k = next(iter(v))
        return {**v, k: perturb(v[k])}
    raise TypeError(v)


class Contract(unittest.TestCase):
    def test_module_contract(self):
        for exp in EXPERIMENTS:
            with self.subTest(exp):
                m = load(exp)
                self.assertEqual(m.EXPERIMENT, exp)
                self.assertEqual(m.FINGERPRINT_VERSION, harness.FINGERPRINT_VERSION)
                for rel in m.TESTBENCHES_REL.values():
                    self.assertTrue((REPO / rel).is_file(), rel)
                inp = m.inputs(texts(exp, m))
                self.assertEqual(inp["experiment"], exp)
                self.assertEqual(inp["fingerprint_version"], harness.FINGERPRINT_VERSION)
                json.dumps(inp)  # JSON-serialisable
                self.assertEqual(m.fingerprint(texts(exp, m)), harness.measurement_fingerprint(inp))

    def test_header_and_block_roundtrip(self):
        for exp in EXPERIMENTS:
            with self.subTest(exp):
                m = load(exp)
                t = texts(exp, m)
                blob = "\n".join(m.fingerprint_lines(t) + m.inputs_section(t))
                self.assertIn(m.fingerprint(t), blob)
                body = blob.split("```json\n", 1)[1].split("\n```", 1)[0]
                self.assertEqual(harness.measurement_fingerprint(json.loads(body)), m.fingerprint(t))
                self.assertIn(m.MODULE_REL, blob)


class Completeness(unittest.TestCase):
    """A configuration constant omitted from the fingerprint is caught."""

    def test_every_public_constant_moves_the_fingerprint(self):
        for exp in EXPERIMENTS:
            m = load(exp)
            t = texts(exp, m)
            base = m.fingerprint(t)
            names = [n for n, v in vars(m).items()
                     if n.isupper() and not n.startswith("_") and not isinstance(v, Path)]
            self.assertTrue(names, exp)
            for n in names:
                if n in NOT_FINGERPRINTED:
                    continue
                with self.subTest(f"{exp}.{n}"):
                    old = getattr(m, n)
                    setattr(m, n, perturb(old))
                    try:
                        self.assertNotEqual(m.fingerprint(t), base,
                                            f"{exp}: {n} is configuration but is missing from the fingerprint")
                    finally:
                        setattr(m, n, old)

    def test_dependent_modules_follow_their_sources(self):
        """psrr takes servo/sweep/isolation from cmrr; both take the grid from gain."""
        psrr, cmrr = load("psrr"), load("cmrr")
        t = texts("psrr", psrr)
        base = psrr.fingerprint(t)
        for n in ("SERVO_NOMINAL", "ISOLATION_CSV", "ISOLATION_INADEQUATE_CSV", "AC_PPD", "AC_FSTART", "AC_FSTOP",
                  "DEVICES", "MODEL_LIB", "CORNERS", "TEMPS_C", "SUPPLIES_V", "PASSIVE_SECTIONS"):
            with self.subTest(f"psrr<-cmrr.{n}"):
                old = getattr(cmrr, n)
                setattr(cmrr, n, perturb(old))
                try:
                    self.assertNotEqual(psrr.fingerprint(t), base)
                finally:
                    setattr(cmrr, n, old)
        psrr_mc = load("psrr-mc")
        cmrr2 = psrr_mc._CMRR
        tm = texts("psrr-mc", psrr_mc)
        base = psrr_mc.fingerprint(tm)
        for n in ("SERVO_NOMINAL", "AC_PPD", "AC_FSTART", "AC_FSTOP"):
            with self.subTest(f"psrr-mc<-cmrr.{n}"):
                old = getattr(cmrr2, n)
                setattr(cmrr2, n, perturb(old))
                try:
                    self.assertNotEqual(psrr_mc.fingerprint(tm), base)
                finally:
                    setattr(cmrr2, n, old)
        # cmrr-mc (issue #197): sweep, servo, corner axes and the control mirror come from cmrr
        cmrr_mc = load("cmrr-mc")
        tc = texts("cmrr-mc", cmrr_mc)
        for n in ("SERVO_NOMINAL", "AC_PPD", "AC_FSTART", "AC_FSTOP"):
            base = cmrr_mc.fingerprint(tc)
            with self.subTest(f"cmrr-mc<-cmrr.{n}"):
                old = getattr(cmrr_mc._CMRR, n)
                setattr(cmrr_mc._CMRR, n, perturb(old))
                try:
                    self.assertNotEqual(cmrr_mc.fingerprint(tc), base)
                finally:
                    setattr(cmrr_mc._CMRR, n, old)
        for n in ("TEMPS_C", "SUPPLIES_V"):  # the full grid's axes
            base = cmrr_mc.fingerprint(tc, {"grid": "full"})
            old = getattr(cmrr_mc._CMRR, n)
            setattr(cmrr_mc._CMRR, n, perturb(old))
            try:
                self.assertNotEqual(cmrr_mc.fingerprint(tc, {"grid": "full"}), base, n)
                self.assertEqual(cmrr_mc.fingerprint(tc), cmrr_mc.fingerprint(tc), n)
            finally:
                setattr(cmrr_mc._CMRR, n, old)
        base = cmrr_mc.fingerprint(tc)
        old = cmrr_mc._CMRR.MODES
        cmrr_mc._CMRR.MODES = {**old, "cm": {"acp": 1.0, "acn": 0.5}}  # the common-mode drive the tail alters to
        try:
            self.assertNotEqual(cmrr_mc.fingerprint(tc), base)
        finally:
            cmrr_mc._CMRR.MODES = old
        self.assertIs(cmrr_mc.CONTROL_MIRROR, load("cmrr").CONTROL_MIRROR)
        gain = harness.load_config_module(HERE / "gain-gbw-pm" / "measurement_config.py")
        # gain-gbw-pm-mc (issue #197): bench, sweep, corners and models come from the gain configuration
        gmc = load("gain-gbw-pm-mc")
        tg = texts("gain-gbw-pm-mc", gmc)
        self.assertEqual(tg["bench"], (REPO / "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice").read_text())
        for n in ("CORNERS", "PASSIVE_SECTIONS", "MODEL_LIB", "AC_FSTART", "AC_FSTOP", "AC_PPD"):
            self.assertIs(getattr(gmc, n), getattr(gain, n), n)  # one source; a gain edit reaches this fingerprint
        for exp in ("noise", "cmrr", "slew-swing-power", "input-common-mode"):
            m = load(exp)
            for n in ("CORNERS", "TEMPS_C", "SUPPLIES_V", "PASSIVE_SECTIONS", "MODEL_LIB"):
                self.assertIs(getattr(m, n), getattr(gain, n), (exp, n))
            tt = texts(exp, m)
            b = m.fingerprint(tt)
            old = m.TEMPS_C
            m.TEMPS_C = [-55.0, *old]
            try:
                self.assertNotEqual(m.fingerprint(tt), b, exp)
            finally:
                m.TEMPS_C = old
        # input-common-mode (issue #180): excitations/servo/isolation/devices come from cmrr,
        # the AC start/density from gain -- the same objects its runner imports.
        icmr = load("input-common-mode")
        for n in ("MODES", "SERVO_NOMINAL", "ISOLATION_CSV", "ISOLATION_INADEQUATE_CSV", "UNEQUAL_CM", "DEVICES"):
            self.assertIs(getattr(icmr, n), getattr(load("cmrr"), n), n)
        for n in ("AC_FSTART", "AC_PPD"):
            self.assertIs(getattr(icmr, n), getattr(gain, n), n)


class BenchNormalisation(unittest.TestCase):
    def test_bench_edit_moves_and_formatting_does_not(self):
        edits = {
            "noise": ("CL vout 0 2p", "CL vout 0 3p"),
            "offset-mc": ("Vcm vinp 0 dc 1.65", "Vcm vinp 0 dc 1.60"),
            "cmrr": ("Rsv vsb vsv {rsv}", "Rsv vsb vsv {rsv}\nRx vsv 0 1e12"),
            "psrr": ("Vss vss 0 dc 0 ac {acss}", "Vss vss 0 dc 0.1 ac {acss}"),
            "psrr-mc": ("Vss vss 0 dc 0 ac {acss}", "Vss vss 0 dc 0.1 ac {acss}"),
            "cmrr-mc": ("CL vout 0 2p", "CL vout 0 3p"),
            "gain-gbw-pm-mc": ("CL vout 0 2p", "CL vout 0 3p"),
            "input-common-mode": ("CL vout 0 2p", "CL vout 0 3p"),
        }
        for exp, (old, new) in edits.items():
            with self.subTest(exp):
                m = load(exp)
                t = texts(exp, m)
                self.assertIn(old, t["bench"])
                base = m.fingerprint(t)
                self.assertNotEqual(m.fingerprint({"bench": last_replace(t["bench"], old, new)}), base)
                cosmetic = "* comment\n\n" + t["bench"].replace("'design.ngspice'", "'/x/work/design.ngspice'")
                cosmetic = last_replace(cosmetic, old, "  " + old.replace(" ", "   ") + " ; note")
                self.assertEqual(m.fingerprint({"bench": cosmetic}), base)

    def test_experiment_specific_inputs_move(self):
        cases = {
            "noise": ("BANDS", lambda m: (("100 Hz - 1 MHz", 100.0, 2e6),) + m.BANDS[1:]),
            "offset-mc": ("MC_SEED", lambda m: m.MC_SEED + 1),
            "cmrr": ("MODES", lambda m: {**m.MODES, "cm": {"acp": 1.0, "acn": 0.5}}),
            "psrr": ("MODES", lambda m: {**m.MODES, "vss": {**m.MODES["vss"], "acss": 2.0}}),
            "psrr-mc": ("MC_SEED", lambda m: m.MC_SEED + 1),
            "cmrr-mc": ("MC_SEED", lambda m: m.MC_SEED + 1),
            "gain-gbw-pm-mc": ("MC_SEED", lambda m: m.MC_SEED + 1),
            "input-common-mode": ("SCAN_STEP_MV", lambda m: m.SCAN_STEP_MV // 2),
        }
        for exp, (name, f) in cases.items():
            with self.subTest(exp):
                m = load(exp)
                t = texts(exp, m)
                base, old = m.fingerprint(t), getattr(m, name)
                setattr(m, name, f(m))
                try:
                    self.assertNotEqual(m.fingerprint(t), base)
                finally:
                    setattr(m, name, old)


class Scheduling(unittest.TestCase):
    def test_equivalent_measurements_share_a_fingerprint(self):
        """Record IDs, workspace paths and backend/retry options are not inputs."""
        for exp in EXPERIMENTS:
            with self.subTest(exp):
                m = load(exp)
                blob = harness.fingerprint_json(m.inputs(texts(exp, m)))
                for word in ("record", "backend", "retr", "batch", "/tmp", "work", "timeout", "workspace"):
                    self.assertNotIn(word, blob.lower().replace("opamp_two_stage.dut", ""), (exp, word))
                # the bench is the only text input; rewriting per-run include paths is a no-op
                t = {k: v.replace("'design.ngspice'", "'/scratch/run-17/design.ngspice'")
                     .replace("'opamp_two_stage.dut.spice'", "'/scratch/run-17/opamp_two_stage.dut.spice'")
                     for k, v in texts(exp, m).items()}
                self.assertEqual(m.fingerprint(t), m.fingerprint(texts(exp, m)))


class InputCommonMode(unittest.TestCase):
    """Issue #180: the ICMR scan/refinement policy and its low-frequency analysis are
    fingerprinted inputs, and the inputs block names the explicit samples."""
    m = load("input-common-mode")

    def t(self):
        return texts("input-common-mode", self.m)

    def moves(self, name, value):
        t = self.t()
        base, old = self.m.fingerprint(t), getattr(self.m, name)
        setattr(self.m, name, value)
        try:
            self.assertNotEqual(self.m.fingerprint(t), base, name)
        finally:
            setattr(self.m, name, old)

    def test_scan_and_target_move(self):
        self.moves("SCAN_STEP_MV", 25)
        self.moves("TARGET_VCM_MV", 1201)

    def test_refinement_policy_moves(self):
        self.moves("REFINE_STEP_MV", 4)
        self.moves("MAX_REFINE_ROUNDS", 4)

    def test_analysis_and_controls_move(self):
        self.moves("AC_FSTOP", 12.0)
        self.moves("UNEQUAL_CM", {"acp": 1.0, "acn": 0.98})
        self.moves("ISOLATION_CSV", (1e7, 1e12))

    def test_inputs_name_the_scan_and_refinement_policy(self):
        inp = self.m.inputs(self.t())
        self.assertEqual(inp["scan"], {"vcm_step_mv": 50, "explicit_mv": ["vdd", "vdd/2", 1200]})
        self.assertEqual(inp["refinement"], {"step_mv": 5, "max_rounds": 3})
        self.assertIn("0.1", harness.fingerprint_json(inp))  # the AC start is retained
        self.assertIn("vdsat", harness.fingerprint_json(inp))  # the op print (with ids) is retained


class SlewSwingPower(unittest.TestCase):
    m = load("slew-swing-power")

    def t(self):
        return texts("slew-swing-power", self.m)

    def sel(self, *figs):
        return {"figures": {f: True for f in figs}}

    def test_only_measured_figures_are_covered(self):
        for figs in (("power",), ("swing",), ("power", "slew"), ("power", "slew", "swing")):
            inp = self.m.inputs(self.t(), self.sel(*figs))
            self.assertEqual(sorted(inp["figures"]), sorted(figs))

    def test_default_selection_is_every_supplied_bench(self):
        self.assertEqual(sorted(self.m.inputs(self.t())["figures"]), ["power", "slew", "swing"])

    def test_power_only_record_does_not_certify_slew_or_swing(self):
        t = self.t()
        base = self.m.fingerprint(t, self.sel("power"))
        t2 = {**t, "slew": t["slew"].replace("CL vout 0 2p", "CL vout 0 9p"),
              "swing": t["swing"].replace("Rf vout vinn 1Meg", "Rf vout vinn 2Meg")}
        self.assertEqual(self.m.fingerprint(t2, self.sel("power")), base)
        # ... but the slew/swing records are sensitive to exactly their own bench
        self.assertNotEqual(self.m.fingerprint(t2, self.sel("slew")), self.m.fingerprint(t, self.sel("slew")))
        self.assertNotEqual(self.m.fingerprint(t2, self.sel("swing")), self.m.fingerprint(t, self.sel("swing")))
        t3 = {**t, "power": t["power"].replace("dc 10u", "dc 12u")}
        self.assertNotEqual(self.m.fingerprint(t3, self.sel("power")), base)

    def test_per_figure_settings_are_not_cross_certified(self):
        t = self.t()
        for fig, name in (("power", "IBIAS_A"), ("slew", "SLEW_STEP_V"), ("swing", "SWING_VIN_STOP_V")):
            base = {f: self.m.fingerprint(t, self.sel(f)) for f in self.m.FIGURES}
            old = getattr(self.m, name)
            setattr(self.m, name, old * 1.1)
            try:
                for f in self.m.FIGURES:
                    moved = self.m.fingerprint(t, self.sel(f)) != base[f]
                    # IBIAS_A is a shared operating point: it enters every figure that uses the bias.
                    expect = (f == fig) or (name == "IBIAS_A")
                    self.assertEqual(moved, expect, (name, f))
            finally:
                setattr(self.m, name, old)

    def test_control_ibias_moves(self):
        t = self.t()
        base = self.m.fingerprint(t)
        old = self.m.CONTROLS
        self.m.CONTROLS = (old[0], (old[1][0], old[1][1], 4e-6), old[2])
        try:
            self.assertNotEqual(self.m.fingerprint(t), base)
        finally:
            self.m.CONTROLS = old


def _runner(name: str, rel: str):
    """A runner module (they need numpy; the stdlib-only checks above do not)."""
    try:
        return harness.load_sibling(name, rel)
    except ModuleNotFoundError as e:  # pragma: no cover - numpy missing
        raise unittest.SkipTest(f"{rel}: {e}")


class _FakePdk:
    variant = harness.DEFAULT_VARIANT
    path = Path("/pdk/" + harness.DEFAULT_VARIANT)


class MismatchRunnerParity(unittest.TestCase):
    """Issue #197: what the CMRR-MC and gain/GBW/PM-MC runners send to klt is what their
    fingerprints name (request/config parity, inherited bench inputs and mismatch population)."""

    def check_request(self, req, inp, temps, supplies, bench_ok=True):
        self.assertEqual(req["analysis"]["kind"], inp["analysis"]["kind"])
        self.assertEqual(req["analysis"]["args"], inp["analysis"]["args"])
        mc = inp["monte_carlo"]
        self.assertEqual(req["monte_carlo"], {k: mc[k] for k in ("n", "seed", "vary")})
        c = inp["corners"]
        self.assertEqual([x["name"] for x in req["corners"]["process"]], c["process"])
        for x in req["corners"]["process"]:
            self.assertEqual(x["sections"][1:], c["passive_sections"])
        self.assertEqual(req["corners"]["temperature_c"], c["temperature_c"])
        self.assertEqual(req["corners"]["supply_v"]["vdd"], c["supply_v"])
        self.assertEqual(c["vcm_rule"], "vdd/2")
        self.assertEqual(req["corners"]["supply_v"]["vcm"], [round(v / 2, 6) for v in c["supply_v"]])
        self.assertEqual(req["models"], {"pdk": inp["models"]["pdk_variant"], "lib": inp["models"]["lib"]})

    def test_cmrr_mc_request_matches_fingerprinted_inputs(self):
        r = _runner("cmrr_mc_parity", "sim/cmrr-mc/run_cmrr_mc.py")
        cfg = load("cmrr-mc")
        t = texts("cmrr-mc", cfg)
        self.assertEqual(r.TESTBENCH.read_text(), t["bench"])
        for grid in ("nominal", "full"):
            with self.subTest(grid):
                temps, supplies = r.GRIDS[grid]
                req = r.request(Path("/w/tb.spice"), _FakePdk(), r.CORNERS, temps, supplies, n=r.MC_N)
                inp = cfg.inputs(t, {"grid": grid})
                self.check_request(req, inp, temps, supplies)
                self.assertEqual(("grid" in inp), grid != "nominal")
        # runner conditions and settings come from the configuration module
        inp = cfg.inputs(t)
        self.assertEqual(r.DM_PARAMS, inp["excitation"]["dm"] | inp["excitation"]["servo"])
        self.assertEqual(r.FLOOR, inp["figures"]["floor"])
        self.assertEqual([r.BAND[0], r.BAND[1]], inp["figures"]["band"])
        self.assertEqual({k: list(v) for k, v in r.SPOTS.items()}, inp["figures"]["spots"])
        self.assertEqual((r.CONTROL_OFF_N, r.CONTROL_PROC_N, r.CONTROL_IMB_N),
                         (inp["controls"]["switch_off_n"], inp["controls"]["process_only_n"],
                          inp["controls"]["imbalance_n"]))
        self.assertEqual(list(r.CONTROL_MIRROR), inp["controls"]["mirror"])
        self.assertEqual(r.SCALARS, cfg.scalars())
        self.assertEqual(inp["models"]["lib"], r.G.MODEL_LIB)
        # the controls' mismatch population: the bench carries the switch the inputs name
        self.assertIn("sw_stat_mismatch", t["bench"])
        self.assertEqual(inp["monte_carlo"]["model_switch"], "sw_stat_mismatch")

    def test_cmrr_mc_record_carries_its_fingerprint(self):
        """The record builder embeds the header line and the retained inputs block for its grid."""
        r = _runner("cmrr_mc_parity", "sim/cmrr-mc/run_cmrr_mc.py")
        cfg = load("cmrr-mc")
        t = texts("cmrr-mc", cfg)
        src = Path(r.__file__).read_text()
        self.assertIn("mc.fingerprint_lines(", src)
        self.assertIn("mc.inputs_section(", src)
        for grid in ("nominal", "full"):
            blob = "\n".join(cfg.fingerprint_lines(t, {"grid": grid}) + cfg.inputs_section(t, {"grid": grid}))
            body = blob.split("```json\n", 1)[1].split("\n```", 1)[0]
            self.assertEqual(harness.measurement_fingerprint(json.loads(body)), cfg.fingerprint(t, {"grid": grid}))
        self.assertNotEqual(cfg.fingerprint(t), cfg.fingerprint(t, {"grid": "full"}))

    def test_gain_mc_request_matches_fingerprinted_inputs(self):
        r = _runner("gain_mc_parity", "sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py")
        cfg = load("gain-gbw-pm-mc")
        t = texts("gain-gbw-pm-mc", cfg)
        self.assertEqual(r.G.TESTBENCH.read_text(), t["bench"])
        req = r.mc_request(Path("/w/tb.spice"), _FakePdk(), r.CORNERS, r.TEMPS_C, r.SUPPLIES_V, n=r.MC_N)
        inp = cfg.inputs(t)
        self.check_request(req, inp, r.TEMPS_C, r.SUPPLIES_V)
        # the bench the runner materialises is the gain bench plus the mismatch switch the inputs hold
        self.assertEqual(harness.canonical_bench_lines(r.inject_mismatch(r.G.TESTBENCH.read_text(), 1)), inp["bench"])
        self.assertIn(".param sw_stat_mismatch=1", inp["bench"])
        self.assertNotEqual(inp["bench"], harness.canonical_bench_lines(t["bench"]))
        self.assertEqual(r.VEC_COLS, cfg.VEC_COLS)
        self.assertEqual(r.analysis_tail(), cfg.analysis_tail())

    def test_gain_mc_mismatch_switch_is_fingerprinted(self):
        cfg = load("gain-gbw-pm-mc")
        t = texts("gain-gbw-pm-mc", cfg)
        off = harness.measurement_fingerprint({**cfg.inputs(t),
                                               "bench": harness.canonical_bench_lines(cfg.inject_mismatch(t["bench"], 0))})
        self.assertNotEqual(off, cfg.fingerprint(t))

    def test_extraction_only_and_scheduling_options_are_outside(self):
        """Tolerances, probe sizes and scheduling are not inputs: changing them keeps the fingerprint."""
        for exp, rel, names in (
                ("cmrr-mc", "sim/cmrr-mc/run_cmrr_mc.py", ("PARITY_TOL_V", "MIN_OFFSET_SIGMA_V", "TOL_SYS_DB", "PROBE_N")),
                ("gain-gbw-pm-mc", "sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py", ("DIFF_EXC_TOL_V", "PROBE_N"))):
            r = _runner({"cmrr-mc": "cmrr_mc_parity", "gain-gbw-pm-mc": "gain_mc_parity"}[exp], rel)
            cfg = load(exp)
            t = texts(exp, cfg)
            base = cfg.fingerprint(t)
            for n in names:
                with self.subTest(f"{exp}.{n}"):
                    old = getattr(r, n)
                    setattr(r, n, perturb(old))
                    try:
                        self.assertEqual(cfg.fingerprint(t), base)
                    finally:
                        setattr(r, n, old)
            blob = harness.fingerprint_json(cfg.inputs(t)).lower()
            for word in ("backend", "retr", "batch", "timeout", "keep_artifacts", "waveforms"):
                self.assertNotIn(word, blob, (exp, word))


if __name__ == "__main__":
    unittest.main(verbosity=1)
