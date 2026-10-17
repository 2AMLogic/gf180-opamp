"""Effective measurement configuration of the input-common-mode experiment (issue #180).

Stdlib only, so the offline report generator can load it without a simulator
or numpy. The driver (`run_input_common_mode.py`) imports its sweep, scan,
refinement and operating-point-print constants FROM here, so the fingerprint
cannot drift from what is run. The grid axes and the `.ac` start/density come
from the gain experiment's configuration module and the excitations, servo and
isolation studies from the CMRR one (the ICMR bench is the CMRR bench's
circuit body) -- one source for all three benches.

Fingerprinted: the canonical bench content (servo network, load, bias), the
`.ac` analysis stopped at 10 Hz plus the operating-point print appended to it
(vout, the two inputs and every DUT device's vds, vdsat AND id), the
differential / common-mode excitation and servo values, the servo-isolation
and unequal-drive control settings, the coarse VCM scan and refinement policy
(step, the explicit VDD / VDD/2 / 1.20 V samples, refinement step and round
cap), the corner axes and the model library/passive sections. NOT
fingerprinted: record IDs, workspace paths, backend/retry options, the
extraction code, and the bench-validity / range-grading settings that are
read only AFTER a simulation -- the 0.1-1 Hz plateau band and its flatness
tolerance, the 1 mV saturation and 0.10 V follower-output tolerances, the
input-current floor, the control (midrail, isolation, local-vs-grid)
tolerances and the 60 dB gain bound. An extraction-only fix (interval
extraction, grading, tolerances) needs no new simulation and does not move
this fingerprint. The DUT is covered by its own hash (issue #75), not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")
_CMRR = harness.load_config_module(HERE.parent / "cmrr" / "measurement_config.py")

EXPERIMENT = "input-common-mode"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/input-common-mode/testbench/tb_input_common_mode.spice"}
MODULE_REL = "sim/input-common-mode/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

#: The shared grid's start and density, stopped at 10 Hz (the plateau band is
#: 0.1-1 Hz; the rest of the shared sweep is irrelevant to a DC-plateau gain
#: and would only multiply the cost of ~6000 units).
AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, 10.0, _GAIN.AC_PPD

#: Coarse VCM scan: 0..VDD at <= step (integer millivolts), always also VDD,
#: VDD/2 and the LDO's 1.20 V target; every adjacent pass/non-pass bracket
#: wider than the refinement step is refined at that spacing by up to
#: `MAX_REFINE_ROUNDS` additional paired requests (issue #60).
SCAN_STEP_MV = 50
REFINE_STEP_MV = 5
MAX_REFINE_ROUNDS = 3
TARGET_VCM_MV = 1200

#: DUT devices whose Vds / Vdsat / id are printed at the operating point (the
#: CMRR set; the ICMR current criteria read the printed ids).
DEVICES = _CMRR.DEVICES

MODES = _CMRR.MODES
SERVO_NOMINAL = _CMRR.SERVO_NOMINAL
#: Servo-isolation study: csv values, and the deliberately inadequate one.
ISOLATION_CSV = _CMRR.ISOLATION_CSV
ISOLATION_INADEQUATE_CSV = _CMRR.ISOLATION_INADEQUATE_CSV
#: Unequal common-mode drive control (1 % less on vinn).
UNEQUAL_CM = _CMRR.UNEQUAL_CM

#: Appended to `analysis.args`: klt places it verbatim in the `.control`
#: block after `ac`. It prints the DC operating point of the same deck into
#: the retained log (vout, the inputs and every DUT device's Vds, Vdsat AND
#: id -- the current criteria read the ids), then re-selects the ac plot so
#: klt's `write` dumps the AC response.
def op_print() -> list[str]:
    return ["v(vout)", "v(vinp)", "v(vinn)"] + [
        f"@m.xdut.{d}.m0[{p}]" for d in DEVICES for p in ("vds", "vdsat", "id")
    ]


def op_tail() -> str:
    return "\nop\nprint " + " ".join(op_print()) + "\nsetplot ac1"


#: Import-time snapshots for the runner (the fingerprint always recomputes).
OP_PRINT = op_print()
OP_TAIL = op_tail()


def ac_args() -> str:
    return f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}" + op_tail()


def scan_inputs() -> dict:
    """The coarse scan policy: step and the always-present explicit samples."""
    return {
        "vcm_step_mv": SCAN_STEP_MV,
        "explicit_mv": ["vdd", "vdd/2", TARGET_VCM_MV],
    }


def refinement_inputs() -> dict:
    return {"step_mv": REFINE_STEP_MV, "max_rounds": MAX_REFINE_ROUNDS}


def corner_inputs() -> dict:
    return {
        "process": list(CORNERS),
        "passive_sections": list(PASSIVE_SECTIONS),
        "temperature_c": [float(t) for t in TEMPS_C],
        "supply_v": [float(v) for v in SUPPLIES_V],
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
        "scan": scan_inputs(),
        "refinement": refinement_inputs(),
        "controls": {
            "isolation_csv": list(ISOLATION_CSV),
            "isolation_inadequate_csv": ISOLATION_INADEQUATE_CSV,
            "unequal_cm": dict(UNEQUAL_CM),
        },
        "corners": corner_inputs(),
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


fingerprint, fingerprint_lines, inputs_section = harness.fingerprint_api(
    inputs,
    "bench content, `.ac` analysis (0.1-10 Hz) + operating-point print with device currents, "
    "excitation/servo values, VCM scan and refinement policy, control settings, corner axes, "
    "model library/sections",
    MODULE_REL)
