"""Effective measurement configuration of the ibias / CL sensitivity experiment (issue #114).

Stdlib only (no simulator, no numpy). The driver imports its sweep and corner
constants FROM here so the fingerprint cannot drift from what is run. The
benches are the committed benches of the sibling experiments, used verbatim
except for the single `Ibias` / `CL` line the driver substitutes; the
fingerprint therefore covers the sibling bench text, the substitution
points, the sweep values, the corner tuples, and the sibling analyses.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")
_SSP = harness.load_config_module(HERE.parent / "slew-swing-power" / "measurement_config.py")

EXPERIMENT = "ibias-cl-sensitivity"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
MODULE_REL = "sim/ibias-cl-sensitivity/measurement_config.py"
BENCHES_REL = {
    "ac": "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice",
    "power": "sim/slew-swing-power/testbench/tb_power.spice",
    "slew": "sim/slew-swing-power/testbench/tb_slew.spice",
}

IBIAS_NOM_A = _SSP.IBIAS_A
CL_NOM_F = 2e-12
#: +-20 % of 10 uA in 1 uA steps; exploratory, NOT a consumer requirement.
IBIAS_SWEEP_A = [8e-6, 9e-6, 10e-6, 11e-6, 12e-6]
#: Load sweep; exploratory, NOT a consumer requirement (only DR-1's 2 pF is ratified).
CL_SWEEP_F = [1e-12, 2e-12, 4e-12, 10e-12]

#: Named corner tuples (process, T in C, VDD in V), from the selected records
#: `gain-gbw-pm/20261010-020141-1e51d1c` and `slew-swing-power/20261009-142137-1dab1db`.
NOMINAL = ("typical", 27.0, 3.30)
PM_BINDING = ("fs", 125.0, 2.97)
GBW_BINDING = ("ss", 125.0, 2.97)  # also the slew-binding point
POWER_BINDING = ("ff", -40.0, 3.63)
SLEW_BINDING = GBW_BINDING
POINTS = {
    "ac": [NOMINAL, PM_BINDING, GBW_BINDING],
    "power": [NOMINAL, POWER_BINDING, SLEW_BINDING],
    "slew": [NOMINAL, POWER_BINDING, SLEW_BINDING],
}
POINT_NAMES = {
    NOMINAL: "nominal",
    PM_BINDING: "PM-binding",
    GBW_BINDING: "GBW-/slew-binding",
    POWER_BINDING: "power-binding",
}

FIGS = ("ac", "power", "slew")


def inputs(texts: dict) -> dict:
    """Effective measurement inputs as a JSON-serialisable dict. `texts`: figure -> bench text."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "benches": {f: harness.canonical_bench_lines(texts[f]) for f in FIGS if f in texts},
        "analysis": {
            "ac": {"kind": "ac", "args": f"dec {_GAIN.AC_PPD:g} {_GAIN.AC_FSTART:g} {_GAIN.AC_FSTOP:g}"},
            "power": _SSP.analysis_for("power"),
            "slew": _SSP.analysis_for("slew"),
        },
        "sweeps": {
            "ibias_a": list(IBIAS_SWEEP_A),
            "cl_f": list(CL_SWEEP_F),
            "nominal": {"ibias_a": IBIAS_NOM_A, "cl_f": CL_NOM_F},
            "substituted_lines": {"ibias": r"^Ibias\s+vdd\s+ibias\s+dc\s+\S+\s*$", "cl": r"^CL\s+vout\s+0\s+\S+\s*$"},
        },
        "points": {f: [list(p) for p in POINTS[f]] for f in FIGS},
        "corners": {
            "passive_sections": list(_GAIN.PASSIVE_SECTIONS),
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": _GAIN.MODEL_LIB},
    }


def fingerprint(texts: dict) -> str:
    return harness.measurement_fingerprint(inputs(texts))


def fingerprint_lines(texts: dict) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts),
        "sibling bench content, analyses, sweep values, substituted lines, corner tuples, model library/sections")


def inputs_section(texts: dict) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts), MODULE_REL)
