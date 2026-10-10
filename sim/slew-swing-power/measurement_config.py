"""Effective measurement configuration of the slew / swing / power experiment (issue #89).

Stdlib only. `run_slew_swing_power.py` imports its per-figure analysis,
timing, sweep and control settings FROM here, so the fingerprint cannot drift
from what is run. The grid axes come from the gain configuration module.

The experiment has three independent figures (`--figures`). The fingerprint
is therefore keyed per figure and covers ONLY the figures a record measured
(`inputs(texts, retained)` reads the figure set from the record's own
retained inputs): a power-only record certifies the power bench, analysis and
settings and says nothing about slew or swing, so a change to the slew bench
never makes it stale.

Per figure: the canonical bench content, the analysis and `.meas` cards, the
figure's timing / sweep / criterion settings and the figure's control
settings. Shared: corner axes and model library/sections. NOT fingerprinted:
record IDs, workspace paths, backend/retry options, validity tolerances,
cross-check tolerances, reported-only sensitivities and the extraction code.
The DUT has its own hash (#75).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "slew-swing-power"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
FIGURES = ("power", "slew", "swing")
TESTBENCHES_REL = {f: f"sim/slew-swing-power/testbench/tb_{f}.spice" for f in FIGURES}
MODULE_REL = "sim/slew-swing-power/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

IBIAS_A = 10e-6

# Slew bench timing (tb_slew.spice): input low until RISE_T, high until FALL_T.
SLEW_RISE_T = 1e-6
SLEW_FALL_T = 3e-6
SLEW_TSTOP = 5e-6
SLEW_TSTEP = 5e-9  # output sample interval; 0.5 ns made the 45-point fleet job exceed its per-point timeout
SLEW_STEP_V = 1.0  # input step, volts peak to peak (+-0.5 V about VCM)
SLEW_LO_FRAC, SLEW_HI_FRAC = 0.2, 0.8
SLEW_SAMPLE_BEFORE = 10e-9  # settled level read this long before the next edge

# Swing bench (tb_swing.spice): FIXED sweep range that covers every grid supply.
SWING_VIN_STOP_V = 3.63
SWING_VIN_STEP_V = 5e-3
SWING_FRAC = 1.0 / math.sqrt(2.0)  # -3 dB: the criterion
SWING_MID_BAND_V = 0.1  # mid-range gain read within +-this of VCM

#: Single-unit controls (name, description, Ibias override; None = nominal).
CONTROLS = (
    ("nominal", "unmodified nominal point (reference for the controls)", None),
    ("ibias-half", "ibias driven at 5 uA instead of 10 uA (all else nominal)", 5e-6),
    ("ibias-zero", "ibias driven at 0 A (no bias current; all else nominal)", 0.0),
)


def analysis_for(fig: str, ibias_a: float = IBIAS_A) -> dict:
    """The klt `analysis` block of one figure."""
    if fig == "power":
        # Operating point as a one-step DC sweep of Ibias (`.meas dc ... AT=` works on every runner).
        return {"kind": "dc", "args": f"Ibias {ibias_a:g} {ibias_a + 1e-6:g} 1u"}
    if fig == "slew":
        return {"kind": "tran", "args": f"{SLEW_TSTEP:g} {SLEW_TSTOP:g}"}
    if fig == "swing":
        return {"kind": "dc", "args": f"Vin 0 {SWING_VIN_STOP_V:g} {SWING_VIN_STEP_V:g}"}
    raise ValueError(fig)


def measurements_for(fig: str, ibias_a: float = IBIAS_A) -> list[dict]:
    """The klt `.meas` cross-check cards of one figure."""
    if fig == "power":
        at = f"{ibias_a:g}"
        return [
            {"name": "ivdd_a", "spice": f".meas dc ivdd_a FIND i(vdd) AT={at}", "unit": "A"},
            {"name": "vout_v", "spice": f".meas dc vout_v FIND v(vout) AT={at}", "unit": "V"},
        ]
    if fig == "slew":
        pre_rise = SLEW_RISE_T - SLEW_SAMPLE_BEFORE
        pre_fall = SLEW_FALL_T - SLEW_SAMPLE_BEFORE
        return [
            {"name": "vlo_v", "spice": f".meas tran vlo_v FIND v(vout) AT={pre_rise:g}", "unit": "V"},
            {"name": "vhi_v", "spice": f".meas tran vhi_v FIND v(vout) AT={pre_fall:g}", "unit": "V"},
        ]
    if fig == "swing":
        return [
            {"name": "vout_max_v", "spice": ".meas dc vout_max_v MAX v(vout)", "unit": "V"},
            {"name": "vout_min_v", "spice": ".meas dc vout_min_v MIN v(vout)", "unit": "V"},
        ]
    raise ValueError(fig)


def figure_params(fig: str) -> dict:
    """Non-bench settings that define what the figure measures."""
    if fig == "power":
        return {"ibias_a": IBIAS_A}
    if fig == "slew":
        return {
            "rise_t": SLEW_RISE_T, "fall_t": SLEW_FALL_T, "tstop": SLEW_TSTOP, "tstep": SLEW_TSTEP,
            "step_v": SLEW_STEP_V, "lo_frac": SLEW_LO_FRAC, "hi_frac": SLEW_HI_FRAC,
            "sample_before": SLEW_SAMPLE_BEFORE, "ibias_a": IBIAS_A,
        }
    if fig == "swing":
        return {
            "vin_stop_v": SWING_VIN_STOP_V, "vin_step_v": SWING_VIN_STEP_V,
            "frac": SWING_FRAC, "mid_band_v": SWING_MID_BAND_V, "ibias_a": IBIAS_A,
        }
    raise ValueError(fig)


def selected_figures(texts: dict, retained: dict | None) -> list[str]:
    """Figures covered: those retained by a record, else every supplied bench."""
    if retained is not None:
        have = retained.get("figures")
        have = have if isinstance(have, dict) else {}
        return [f for f in FIGURES if f in have]
    return [f for f in FIGURES if f in texts]


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `texts` maps figure name -> bench text; only the figures selected by
    `retained` (a record's stored inputs; default: every figure in `texts`)
    appear in the result.
    """
    figs = {}
    for f in selected_figures(texts, retained):
        figs[f] = {
            "bench": harness.canonical_bench_lines(texts[f]),
            "analysis": {**analysis_for(f), "measurements": measurements_for(f)},
            "params": figure_params(f),
            "controls": {"ibias_a": [c[2] for c in CONTROLS]},
        }
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "figures": figs,
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(t) for t in TEMPS_C],
            "supply_v": [float(v) for v in SUPPLIES_V],
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    figs = ", ".join(selected_figures(texts, retained)) or "none"
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        f"measured figures only ({figs}): per-figure bench content, analysis, timing/sweep/criterion "
        "and control settings; shared corner axes and model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
