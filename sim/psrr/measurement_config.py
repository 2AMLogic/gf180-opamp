"""Effective measurement configuration of the PSRR experiment (issue #89).

Stdlib only. `run_psrr.py` imports its excitation modes, feedthrough fixture
and supply-isolation settings FROM here, so the fingerprint cannot drift from
what is run. The servo, `.ac` sweep with operating-point print, isolation
study and grid axes come from the CMRR configuration module (the two benches
share them), which in turn takes the grid axes from the gain configuration.

Fingerprinted: the canonical bench content (rail drives, servo network, load,
bias), the `.ac` analysis plus operating-point print, the differential /
VDD / VSS excitation modes (which rail is driven and which are quiet), the
servo and isolation-study values, the feedthrough fixture, the corner axes and
the model library/passive sections. NOT fingerprinted: record IDs, workspace
paths, backend/retry options, validation tolerances and the extraction code.
The DUT has its own hash (#75).

RZ x CC passive-corner study (issue #155, `run_psrr.py --passive-corners
--passive-scope full`): a study record retains an extra `passive_study` block
(scope, MOS/T/VDD points, RZ/CC levels with their PDK sections and nominal
factors, cell count). It is present ONLY when a record's retained inputs carry
it (`retained["passive_study"]`) and is always RECOMPUTED here from
`sim/passive_corners.py`, never copied, so a changed section map or factor
changes the study fingerprint. Without it the inputs (and the fingerprint of
every ordinary PSRR record) are exactly the historical ones. The fleet
partitioning of a study is backend scheduling and is not fingerprinted.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_CMRR = harness.load_config_module(HERE.parent / "cmrr" / "measurement_config.py")
_PASSIVE = harness.load_config_module(HERE.parent / "passive_corners.py")

EXPERIMENT = "psrr"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/psrr/testbench/tb_psrr.spice"}
MODULE_REL = "sim/psrr/measurement_config.py"

QUIET = {"acp": 0.0, "acn": 0.0, "acdd": 0.0, "acss": 0.0}
MODES = {
    "dm": {**QUIET, "acp": 0.5, "acn": -0.5},
    "vdd": {**QUIET, "acdd": 1.0},
    "vss": {**QUIET, "acss": 1.0},
}
#: Feedthrough fixture (stimulus-fixture negative control): a resistor from
#: the driven rail to vout, appended to the bench only for the control.
FEEDTHROUGH_R = 100e3


def passive_study_inputs(scope: str) -> dict:
    """The effective RZ x CC study inputs for `scope` (issue #155).

    Everything that decides WHICH cells are simulated and with WHICH PDK
    sections: the MOS/T/VDD study points, the RZ and CC levels, the section
    each level loads and its nominal factor, and the resulting cell count.
    """
    p = _PASSIVE
    pts = p.scope_points(scope)
    return {
        "scope": scope,
        "points": [[m, float(t), float(v)] for m, t, v in pts],
        "levels": list(p.PASSIVE_LEVELS),
        "rz": {lv: {"section": p.passive_sections(lv, "typical")[0], "factor": p.RES_FACTOR[lv]}
               for lv in p.PASSIVE_LEVELS},
        "cc": {lv: {"section": p.passive_sections("typical", lv)[1], "factor": p.MIM_FACTOR[lv]}
               for lv in p.PASSIVE_LEVELS},
        "process_naming": p.passive_name("<mos>", "<rz>", "<cc>"),
        "cells": len(pts) * len(p.PASSIVE_LEVELS) ** 2,
    }


def study_retained(scope: str) -> dict:
    """The `retained` argument a study run passes to `inputs`/`fingerprint`."""
    return {"passive_study": {"scope": scope}}


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `retained` (a record's stored inputs, or `study_retained(scope)`) adds the
    `passive_study` block only when it carries one; the block is recomputed
    from the current code for the retained scope."""
    out = _default_inputs(texts)
    study = (retained or {}).get("passive_study")
    if study is not None:
        out["passive_study"] = passive_study_inputs(study["scope"])
    return out


def _default_inputs(texts: dict) -> dict:
    c = _CMRR
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "ac", "args": c.ac_args()},
        "excitation": {
            "modes": {k: dict(v) for k, v in MODES.items()},
            "servo": dict(c.SERVO_NOMINAL),
        },
        "controls": {
            "isolation_csv": list(c.ISOLATION_CSV),
            "isolation_inadequate_csv": c.ISOLATION_INADEQUATE_CSV,
            "feedthrough_r_ohm": FEEDTHROUGH_R,
        },
        "corners": c.corner_inputs(),
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": c.MODEL_LIB},
    }


def _covers(texts: dict, retained: dict | None = None) -> str:
    covers = ("bench content, `.ac` analysis + operating-point print, rail/input excitation modes, servo and "
              "isolation values, feedthrough fixture, corner axes, model library/sections")
    if "passive_study" in inputs(texts, retained):
        covers += ", RZ x CC passive-study scope/points/sections/factors"
    return covers


fingerprint, fingerprint_lines, inputs_section = harness.fingerprint_api(inputs, _covers, MODULE_REL)
