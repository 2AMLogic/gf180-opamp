"""Effective measurement configuration of the noise experiment (issue #89).

Stdlib only (the offline report generator loads it without a simulator or
numpy). `run_noise.py` imports its sweep, band, fit-window and control
constants FROM here, so the fingerprint cannot drift from what is run.

Fingerprinted (changes the measured numbers): the canonical bench content
(bias, load, stimulus, feedback isolation), the `.noise` analysis (output
node, input source, sweep, the totals/plot commands), the integration bands,
spot frequencies and the floor/corner fit window, the dense-sweep and
feedback-isolation control settings, the corner/temperature/supply axes (VCM
tracks VDD/2) and the model library/passive sections (shared with the gain
bench). NOT fingerprinted: record IDs, workspace paths, backend/retry options,
validation tolerances and the extraction code. The DUT has its own hash (#75).

The shared grid axes come from the gain experiment's configuration module, so
there is one source for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "noise"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/noise/testbench/tb_noise.spice"}
MODULE_REL = "sim/noise/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

# --------------------------------------------------------------------------
# Sweep, spot frequencies, bands
# --------------------------------------------------------------------------

F_START, F_STOP, PPD = 0.1, 1e7, 20
DENSE_PPD = 200
SPOT_HZ = (10.0, 100.0, 1e3, 1e4, 1e5)
#: (label, f_lo, f_hi). The first is the sg13g2-opamp twin precedent named in
#: DR-3 residual (e1); the others are alternatives so the band choice can be
#: argued from data. None is ratified.
BANDS = (
    ("100 Hz - 1 MHz", 100.0, 1e6),
    ("10 Hz - 100 kHz", 10.0, 1e5),
    ("100 Hz - 100 kHz", 100.0, 1e5),
    ("1 Hz - 10 kHz", 1.0, 1e4),
)
PRIMARY = 0
FIT_LO, FIT_HI = 1.0, 1e7
ISOLATION_VALUES = (1e8, 1e10)

ANALYSIS_TAIL = "\nprint noise2.inoise_total noise2.onoise_total\nsetplot noise1"


def analysis_args(ppd: int | None = None) -> str:
    ppd = PPD if ppd is None else ppd
    return f"v(vout) Vcm dec {ppd:g} {F_START:g} {F_STOP:g}" + ANALYSIS_TAIL


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `texts` maps TESTBENCHES_REL keys to bench text. `retained` (a record's
    stored inputs) is unused here; it exists so every experiment shares one
    signature (slew/swing/power selects figures from it).
    """
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "noise", "args": analysis_args(PPD)},
        "bands": {
            "integration": [[n, lo, hi] for n, lo, hi in BANDS],
            "primary": BANDS[PRIMARY][0],
            "spot_hz": list(SPOT_HZ),
            "fit_hz": [FIT_LO, FIT_HI],
        },
        "controls": {"dense_ppd": DENSE_PPD, "isolation_lfb_cfb": list(ISOLATION_VALUES)},
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
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "bench content, `.noise` analysis, integration bands/spot/fit settings, control settings, "
        "corner axes, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
