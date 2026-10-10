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

EXPERIMENTS = ("noise", "offset-mc", "cmrr", "psrr", "slew-swing-power")

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
        gain = harness.load_config_module(HERE / "gain-gbw-pm" / "measurement_config.py")
        for exp in ("noise", "cmrr", "slew-swing-power"):
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


class BenchNormalisation(unittest.TestCase):
    def test_bench_edit_moves_and_formatting_does_not(self):
        edits = {
            "noise": ("CL vout 0 2p", "CL vout 0 3p"),
            "offset-mc": ("Vcm vinp 0 dc 1.65", "Vcm vinp 0 dc 1.60"),
            "cmrr": ("Rsv vsb vsv {rsv}", "Rsv vsb vsv {rsv}\nRx vsv 0 1e12"),
            "psrr": ("Vss vss 0 dc 0 ac {acss}", "Vss vss 0 dc 0.1 ac {acss}"),
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
