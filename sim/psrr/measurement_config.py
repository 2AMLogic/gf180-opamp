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
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_CMRR = harness.load_config_module(HERE.parent / "cmrr" / "measurement_config.py")

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


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
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


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "bench content, `.ac` analysis + operating-point print, rail/input excitation modes, servo and "
        "isolation values, feedthrough fixture, corner axes, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
