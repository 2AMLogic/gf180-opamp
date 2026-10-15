"""Effective measurement configuration of the CMRR experiment (issue #89).

Stdlib only. `run_cmrr.py` imports its excitation, servo, operating-point
print, isolation and control settings FROM here, so the fingerprint cannot
drift from what is run. The grid axes and the `.ac` sweep come from the
gain experiment's configuration module (one source for both benches).

Fingerprinted: the canonical bench content (servo network, load, bias), the
`.ac` analysis plus the operating-point print appended to it, the
differential / common-mode excitation and servo values, the unequal-drive,
servo-isolation and mirror-imbalance control settings, the corner axes and
the model library/passive sections. NOT fingerprinted: record IDs, workspace
paths, backend/retry options, validation tolerances, the numerical-floor
margin and the extraction code. The DUT has its own hash (#75).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "cmrr"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/cmrr/testbench/tb_cmrr.spice"}
MODULE_REL = "sim/cmrr/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

#: Same `.ac` sweep as the gain bench.
AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, _GAIN.AC_FSTOP, _GAIN.AC_PPD

#: DUT devices whose Vds / Vdsat are printed at the operating point.
DEVICES = ("xm1", "xm2", "xm3", "xm4", "xm5", "xm6", "xm7", "xmb1")

SERVO_NOMINAL = {"rsv": 1e9, "csv": 1e9}  # tau = 1e18 s
MODES = {
    "dm": {"acp": 0.5, "acn": -0.5},
    "cm": {"acp": 1.0, "acn": 1.0},
}
#: Unequal common-mode drive control (1 % less on vinn).
UNEQUAL_CM = {"acp": 1.0, "acn": 0.99}
#: Servo-isolation study: csv values, and the deliberately inadequate one.
ISOLATION_CSV = (1e7, 1e11)
ISOLATION_INADEQUATE_CSV = 1e-12  # tau = 1e-3 s: the loop closes at AC
#: Mirror-imbalance control: the diode-connected load XM3's width, -10 %;
#: and the opposite (+10 %) sign study.
CONTROL_MIRROR = ("XM3", "W", "6u", "5.4u")
CONTROL_MIRROR_INFO = ("XM3", "W", "6u", "6.6u")

#: Appended to `analysis.args`: klt places it verbatim in the `.control`
#: block after `ac`. It prints the DC operating point of the same deck into
#: the retained log (vout, inputs, every DUT device's Vds and Vdsat), then
#: re-selects the ac plot so klt's `write` dumps the AC response.
def op_print() -> list[str]:
    return ["v(vout)", "v(vinp)", "v(vinn)"] + [
        f"@m.xdut.{d}.m0[{p}]" for d in DEVICES for p in ("vds", "vdsat")
    ]


def op_tail() -> str:
    return "\nop\nprint " + " ".join(op_print()) + "\nsetplot ac1"


#: Import-time snapshots for the runner (the fingerprint always recomputes).
OP_PRINT = op_print()
OP_TAIL = op_tail()


def ac_args() -> str:
    return f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}" + op_tail()


def corner_inputs() -> dict:
    return {
        "process": list(CORNERS),
        "passive_sections": list(PASSIVE_SECTIONS),
        "temperature_c": [float(t) for t in TEMPS_C],
        "supply_v": [float(v) for v in SUPPLIES_V],
        "vcm_rule": "vdd/2",
    }


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "ac", "args": ac_args()},
        "excitation": {
            "modes": {k: dict(v) for k, v in MODES.items()},
            "servo": dict(SERVO_NOMINAL),
        },
        "controls": {
            "unequal_cm": dict(UNEQUAL_CM),
            "isolation_csv": list(ISOLATION_CSV),
            "isolation_inadequate_csv": ISOLATION_INADEQUATE_CSV,
            "mirror": list(CONTROL_MIRROR),
            "mirror_info": list(CONTROL_MIRROR_INFO),
        },
        "corners": corner_inputs(),
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "bench content, `.ac` analysis + operating-point print, excitation/servo values, control settings, "
        "corner axes, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
