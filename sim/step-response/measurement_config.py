"""Effective measurement configuration of the follower step-response experiment (issue #113).

Stdlib only. `run_step_response.py` imports its analysis, timing, extraction
thresholds and control settings FROM here, so the measurement fingerprint
cannot drift from what is run. The grid axes come from the gain configuration
module (one source for the 45-point grid).

Fingerprinted: the canonical bench content, the analysis and `.meas` cards,
the step timing, the settling bands / reference window / monotonic tolerance,
the controls, the corner axes and the model library/sections. NOT
fingerprinted: record IDs, workspace paths, backend/retry options, validity
and cross-check tolerances and the extraction code. The DUT has its own hash
(#75).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "step-response"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCH_REL = "sim/step-response/testbench/tb_step.spice"
MODULE_REL = "sim/step-response/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

IBIAS_A = 10e-6
CL_F = 2e-12  # [DR-1]

# Step timing (tb_step.spice): input low until RISE_T, high until FALL_T, run ends at TSTOP.
STEP_V = 0.1  # input step, volts (+-50 mV about VCM)
RISE_T = 100e-9
FALL_T = 600e-9
TSTOP = 1.1e-6
TSTEP = 1e-9  # output sample interval; also bounds ngspice's internal step (tmax defaults to tstep)
EDGE_T = 1e-9  # input edge duration (pulse TR = TF)

# Extraction.
SAMPLE_BEFORE = 10e-9  # pre-edge level read this long before an edge
REF_WINDOW = 50e-9  # settled (final) level = mean vout over the last REF_WINDOW of each edge window
SETTLE_BANDS = (0.01, 0.001)  # settling bands, fraction of the realised step (1 %, 0.1 %)
MONO_TOL = 0.001  # a reversal or preshoot larger than this fraction of the step is non-monotonic
# A band b is only claimed settled if the output drifts by <= DRIFT_FRAC * b of the
# step across the reference window: a slow tail must not hide inside the
# reference-window mean.
DRIFT_FRAC = 0.1

# The ratified small-signal GBW lower bound (spec/target-spec.md Sec.2), used
# ONLY to size the window: tau_max = 1 / (2 pi GBW_MIN). Never judged here.
GBW_MIN_HZ = 10e6

#: Single-unit controls (name, description, overrides). Overrides: `ibias_a`, `cl_f`.
CONTROLS = (
    ("nominal", "unmodified nominal point (reference for the controls)", {}),
    ("cl-x10", "CL = 20 pF instead of 2 pF (lower phase margin; must ring MORE than nominal)", {"cl_f": 20e-12}),
    ("ibias-zero", "ibias driven at 0 A (dead amplifier; must not yield a settled step)", {"ibias_a": 0.0}),
)


def analysis() -> dict:
    """The klt `analysis` block."""
    return {"kind": "tran", "args": f"{TSTEP:g} {TSTOP:g}"}


def measurements() -> list[dict]:
    """ngspice-native `.meas` cards, used only to cross-check the rawfile extraction."""
    pre_rise = RISE_T - SAMPLE_BEFORE
    pre_fall = FALL_T - SAMPLE_BEFORE
    return [
        {"name": "vlo_v", "spice": f".meas tran vlo_v FIND v(vout) AT={pre_rise:g}", "unit": "V"},
        {"name": "vhi_v", "spice": f".meas tran vhi_v FIND v(vout) AT={pre_fall:g}", "unit": "V"},
        {"name": "vmax_rise_v", "spice": f".meas tran vmax_rise_v MAX v(vout) FROM={RISE_T:g} TO={pre_fall:g}", "unit": "V"},
        {"name": "vmin_fall_v", "spice": f".meas tran vmin_fall_v MIN v(vout) FROM={FALL_T:g} TO={TSTOP:g}", "unit": "V"},
    ]


def params() -> dict:
    return {
        "step_v": STEP_V, "rise_t": RISE_T, "fall_t": FALL_T, "tstop": TSTOP, "tstep": TSTEP,
        "edge_t": EDGE_T, "sample_before": SAMPLE_BEFORE, "ref_window": REF_WINDOW,
        "settle_bands": list(SETTLE_BANDS), "mono_tol": MONO_TOL, "drift_frac": DRIFT_FRAC, "ibias_a": IBIAS_A, "cl_f": CL_F,
    }


def inputs(bench_text: str) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(bench_text),
        "analysis": {**analysis(), "measurements": measurements()},
        "params": params(),
        "controls": {name: dict(ov) for name, _, ov in CONTROLS},
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(t) for t in TEMPS_C],
            "supply_v": [float(v) for v in SUPPLIES_V],
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(bench_text: str) -> str:
    return harness.measurement_fingerprint(inputs(bench_text))


def fingerprint_lines(bench_text: str) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(bench_text),
        "bench content, transient analysis and `.meas` cards, step timing, settling bands, reference "
        "window, monotonic tolerance, controls, corner axes and model library/sections")


def inputs_section(bench_text: str) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(bench_text), MODULE_REL)
