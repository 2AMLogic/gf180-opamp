"""Effective measurement configuration of the gain/GBW/PM Monte Carlo experiment (issue #197; #89 convention).

Stdlib only. `run_gain_gbw_pm_mc.py` imports its population, Monte Carlo
settings, the mismatch-switch injection and the control-language vector-print
tail FROM here, so the fingerprint cannot drift from what is run. The bench,
the `.ac` sweep, the corner/passive/model choices come from the deterministic
gain configuration (`sim/gain-gbw-pm/measurement_config.py`): this experiment's
testbench IS the gain bench plus one `.param sw_stat_mismatch=1` line.

Fingerprinted: the canonical gain bench content WITH the injected mismatch
switch, the `.ac` analysis plus the vector-print tail (the exact transport
cards that put the complex node phasors into each sample's log), the Monte
Carlo request (count, seed, `vary`), the population (corners, temperature,
supply, VCM rule), the passive sections and the model library. NOT
fingerprinted: record IDs, workspace paths, backend/retry/scheduling options,
the ratified bounds and their statistics, the excitation validity tolerance,
the `.meas` cross-check cards, the probe size and the extraction code (an
extraction-only change needs no new simulation). The DUT has its own hash
(#75).

Records written before this module carry no fingerprint and stay
freshness-unknown (no fingerprint is back-filled for them).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "gain-gbw-pm-mc"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
#: The committed gain bench; the mismatch switch is injected (`inject_mismatch`).
TESTBENCHES_REL = {"bench": "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice"}
MODULE_REL = "sim/gain-gbw-pm-mc/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
MODEL_LIB = _GAIN.MODEL_LIB
AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, _GAIN.AC_FSTOP, _GAIN.AC_PPD

#: Population: nominal temperature and supply, VCM = VDD/2.
TEMPS_C = [27.0]
SUPPLIES_V = [3.30]

#: Monte Carlo request.
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"

#: Vector-print transport: table columns and the markers around the table.
VEC_COLS = ("gmc_fr", "gmc_vo_re", "gmc_vo_im", "gmc_vp_re", "gmc_vp_im", "gmc_vn_re", "gmc_vn_im")
BEGIN, END = "GMC_VECTORS_BEGIN", "GMC_VECTORS_END"

_DESIGN_INC_RE = re.compile(r"^\.include\s+['\"]?[^'\"\n]*design\.ngspice['\"]?[ \t]*$", re.M | re.I)


def inject_mismatch(tb_text: str, mismatch: int) -> str:
    """The bench with `.param sw_stat_mismatch=<0|1>` right after the
    `design.ngspice` include (after it, so it overrides the PDK default 0)."""
    if mismatch not in (0, 1):
        raise ValueError(f"mismatch switch must be 0 or 1, got {mismatch!r}")
    out, n = _DESIGN_INC_RE.subn(lambda m: m.group(0) + f"\n.param sw_stat_mismatch={mismatch}", tb_text, count=0)
    if n != 1:
        raise RuntimeError(f"expected exactly one design.ngspice include in the bench, found {n}")
    return out


def ac_args() -> str:
    return f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}"


def analysis_tail() -> str:
    """Control commands klt places verbatim after its `ac <args>` line: print
    the real/imag parts of the three node phasors, 15 significant digits, one
    table without page breaks, between two markers."""
    return "\n".join([
        "set width=4000",
        "set nobreak",
        "set numdgt=15",
        "let gmc_fr = real(frequency)",
        "let gmc_vo_re = real(v(vout))",
        "let gmc_vo_im = imag(v(vout))",
        "let gmc_vp_re = real(v(vinp))",
        "let gmc_vp_im = imag(v(vinp))",
        "let gmc_vn_re = real(v(vinn))",
        "let gmc_vn_im = imag(v(vinn))",
        f"echo {BEGIN}",
        "print " + " ".join(VEC_COLS),
        f"echo {END}",
    ])


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `texts["bench"]` is the committed gain bench; the Monte Carlo bench adds
    the mismatch switch, which is therefore part of the canonical content."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(inject_mismatch(texts["bench"], 1)),
        "analysis": {"kind": "ac", "args": ac_args() + "\n" + analysis_tail()},
        "monte_carlo": {"n": MC_N, "seed": MC_SEED, "vary": MC_VARY, "model_switch": "sw_stat_mismatch"},
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(t) for t in TEMPS_C],
            "supply_v": [float(v) for v in SUPPLIES_V],
            "vcm_rule": _GAIN.vcm_rule(None),
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "gain bench content incl. the mismatch switch, `.ac` analysis + vector-print tail, Monte Carlo "
        "count/seed/mode, corner conditions, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
