"""Effective measurement configuration of the offset Monte Carlo experiment (issue #89).

Stdlib only. `run_offset_mc.py` imports its conditions, Monte Carlo settings,
DC analysis and control settings FROM here, so the fingerprint cannot drift
from what is run.

Fingerprinted: the canonical bench content (including the `sw_stat_mismatch`
model switch default), the DC analysis and `.meas` cards, the Monte Carlo
request (sample count, seed, `vary` mode), the corner/temperature/supply/VCM
conditions, the model library and passive sections, and the control settings
(small-N, imbalance device and size, switch-off). NOT fingerprinted: record
IDs, workspace paths, backend/retry options, validity thresholds and the
extraction/statistics code. The DUT has its own hash (#75).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

EXPERIMENT = "offset-mc"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/offset-mc/testbench/tb_offset_mc.spice"}
MODULE_REL = "sim/offset-mc/measurement_config.py"

CORNERS = ["typical", "ff", "ss", "fs", "sf"]
PASSIVE_SECTIONS = ("res_typical", "mimcap_typical")
TEMP_C = 27.0
VDD_V = 3.30
VCM_V = 1.65
MODEL_LIB = "libs.tech/ngspice/sm141064.ngspice"

#: Monte Carlo request. The seed is recorded; klt derives every per-sample
#: seed from it deterministically, so re-running reproduces the draw.
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"

#: Control sizes (kept small).
CONTROL_N = 40
IMBALANCE_DEVICE = "xm1"
IMBALANCE_W_FROM, IMBALANCE_W_TO = "W=3.6u", "W=3.96u"  # +10 %

SIM_SOURCE = "Ibias"
SIM_ARGS = "Ibias 10u 11u 1u"
MEASUREMENTS = [
    {"name": "vout_v", "spice": ".meas dc vout_v FIND v(vout) AT=10u", "unit": "V"},
    {"name": "vinp_v", "spice": ".meas dc vinp_v FIND v(vinp) AT=10u", "unit": "V"},
    {"name": "vos_v", "spice": ".meas dc vos_v FIND par('v(vout)-v(vinp)') AT=10u", "unit": "V"},
]


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "dc", "args": SIM_ARGS, "measurements": [dict(m) for m in MEASUREMENTS]},
        "monte_carlo": {"n": MC_N, "seed": MC_SEED, "vary": MC_VARY, "model_switch": "sw_stat_mismatch"},
        "controls": {
            "mc_n": CONTROL_N,
            "imbalance": {"device": IMBALANCE_DEVICE, "from": IMBALANCE_W_FROM, "to": IMBALANCE_W_TO},
        },
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(TEMP_C)],
            "supply_v": [float(VDD_V)],
            "vcm_v": [float(VCM_V)],
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "bench content incl. the mismatch model switch, DC analysis, Monte Carlo count/seed/mode, "
        "control settings, corner conditions, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
