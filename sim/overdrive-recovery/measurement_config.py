"""Effective measurement configuration of the input-overdrive / output-saturation
recovery experiment (issue #183).

Stdlib only. `run_overdrive_recovery.py` imports its analysis, timing,
extraction thresholds and control settings FROM here, so the measurement
fingerprint cannot drift from what is run. The grid axes come from the gain
configuration module (one source for the 45-point grid).

Fingerprinted: the canonical bench content, the analysis and `.meas` cards,
the overdrive amplitudes and timing, the saturation requirement, the recovery
band / reference window / drift rule, the controls, the corner axes and the
model library/sections. NOT fingerprinted: record IDs, workspace paths,
backend/retry options, validity and cross-check tolerances and the extraction
code. The DUT has its own hash (#75).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "overdrive-recovery"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCH_REL = "sim/overdrive-recovery/testbench/tb_overdrive.spice"
MODULE_REL = "sim/overdrive-recovery/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

IBIAS_A = 10e-6
CL_F = 2e-12  # [DR-1]

# Overdrive timing (tb_overdrive.spice). The input rests at VCM; a +OD_UP_V
# excursion from RISE_T saturates the output HIGH until FALL_T (overdrive
# removed; recovery-high window opens); a -OD_DN_V excursion from OD_LO_T
# saturates the output LOW until RISE2_T (recovery-low window opens, runs to
# TSTOP). Amplitudes are FIXED in volts: past the rails at every grid supply
# (see the testbench header for the per-supply margins).
OD_UP_V = 1.85  # input excursion above VCM (volts)
OD_DN_V = 1.90  # input excursion below VCM (volts)
RISE_T = 400e-9  # overdrive high begins
FALL_T = 1.0e-6  # overdrive high removed (recovery-high window opens)
OD_LO_T = 2.6e-6  # overdrive low begins
RISE2_T = 3.2e-6  # overdrive low removed (recovery-low window opens)
TSTOP = 4.9e-6
TSTEP = 5e-9  # output sample interval; also bounds ngspice's internal step (tmax defaults to tstep)
EDGE_T = 1e-9  # input edge duration (pulse TR = TF)

# Extraction.
SAMPLE_BEFORE = 10e-9  # levels read this long before an edge
REF_WINDOW = 50e-9  # reference (settled) level = mean vout over the last REF_WINDOW before an edge
# Saturation requirement: over the last REF_WINDOW of the saturation hold the
# output must have travelled at least MIN_TRAVEL_V in the overdriven direction
# (a deep large-signal excursion, ~30 % of the half-supply, far beyond any
# linear response, which would have followed the demand) AND be essentially
# stopped: residual output velocity <= SAT_MAX_RATE_V_US, ~1/30 of the
# nominal 14.5 V/us slew rate. The landing on the current-limited limits is
# soft (residual creep 0.06 .. 0.12 V/us across the grid, the negative clip
# limit sits ~0.5 V above ground), so a flatness bar or a fraction-of-rail
# test would be meaningless; travel + velocity is corner-robust and still
# 15x below the slowest slewing the stage does.
MIN_TRAVEL_V = 0.5
SAT_MAX_RATE_V_US = 0.5  # V/us
BAND_FRAC = 0.01  # recovery band: +-BAND_FRAC of the linear output level (the issue's "+-1 %")
# A recovery is only claimed when the output drifts by <= DRIFT_FRAC * band across the
# final reference window: a slow tail must not hide inside the window mean.
DRIFT_FRAC = 0.1

# The ratified small-signal GBW lower bound (spec/target-spec.md Sec.2), used
# ONLY to size the saturation hold and the recovery windows against
# tau_max = 1 / (2 pi GBW_MIN). Never judged here.
GBW_MIN_HZ = 10e6

#: Single-unit controls (name, description, overrides). Overrides: `ibias_a`, `cl_f`.
CONTROLS = (
    ("nominal", "unmodified nominal point (reference for the controls)", {}),
    ("cl-x5", "CL = 10 pF instead of 2 pF (heavier load; must recover SLOWER than nominal)", {"cl_f": 10e-12}),
    ("ibias-zero", "ibias driven at 0 A (dead amplifier; must never yield a valid recovered overdrive)", {"ibias_a": 0.0}),
)


def analysis() -> dict:
    """The klt `analysis` block."""
    return {"kind": "tran", "args": f"{TSTEP:g} {TSTOP:g}"}


def measurements() -> list[dict]:
    """ngspice-native `.meas` cards, used only to cross-check the rawfile extraction."""
    pre_rise = RISE_T - SAMPLE_BEFORE
    return [
        {"name": "vpre_v", "spice": f".meas tran vpre_v FIND v(vout) AT={pre_rise:g}", "unit": "V"},
        {"name": "vsat_hi_v", "spice": f".meas tran vsat_hi_v MAX v(vout) FROM={RISE_T:g} TO={FALL_T - SAMPLE_BEFORE:g}", "unit": "V"},
        {"name": "vsat_lo_v", "spice": f".meas tran vsat_lo_v MIN v(vout) FROM={OD_LO_T:g} TO={RISE2_T - SAMPLE_BEFORE:g}", "unit": "V"},
        {"name": "vfinal_v", "spice": f".meas tran vfinal_v FIND v(vout) AT={TSTOP - SAMPLE_BEFORE:g}", "unit": "V"},
    ]


def params() -> dict:
    return {
        "od_up_v": OD_UP_V, "od_dn_v": OD_DN_V, "rise_t": RISE_T, "fall_t": FALL_T,
        "od_lo_t": OD_LO_T, "rise2_t": RISE2_T, "tstop": TSTOP, "tstep": TSTEP, "edge_t": EDGE_T,
        "sample_before": SAMPLE_BEFORE, "ref_window": REF_WINDOW, "min_travel_v": MIN_TRAVEL_V,
        "sat_max_rate_v_us": SAT_MAX_RATE_V_US,
        "band_frac": BAND_FRAC, "drift_frac": DRIFT_FRAC, "ibias_a": IBIAS_A, "cl_f": CL_F,
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
        "bench content, transient analysis and `.meas` cards, overdrive amplitudes and timing, "
        "saturation requirement, recovery band, reference window, drift rule, controls, corner axes "
        "and model library/sections")

def inputs_section(bench_text: str) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(bench_text), MODULE_REL)
