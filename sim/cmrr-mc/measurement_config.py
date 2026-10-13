"""Effective measurement configuration of the CMRR Monte Carlo experiment (issue #197; #89 convention).

Stdlib only. `run_cmrr_mc.py` imports its conditions, Monte Carlo settings,
the control-language tail appended to the `.ac` analysis line, the figure
definitions, the numerical floor and the control settings FROM here, so the
fingerprint cannot drift from what is run. The sweep, servo, excitation
values and corner axes come from the CMRR configuration module (the
systematic bench is shared), which takes the corner axes from the gain
configuration.

Fingerprinted: the canonical bench content (including the `sw_stat_mismatch`
switch), the `.ac` analysis plus the two-sweep control tail (the exact
per-frequency Ad/Acm solve, the numerical-floor clamp context and every
printed scalar), the dm excitation and servo values, the Monte Carlo request
(count, seed, `vary`), the corner conditions of the selected grid, the figure
definitions (DC band, spot indices), the control settings and the model
library/sections. NOT fingerprinted: record IDs, workspace paths,
backend/retry options, validation tolerances (excitation, parity, plateau,
switch-off and imbalance tolerances), the extraction/statistics code and the
probe sizes. The DUT has its own hash (#75).

Two grids. `nominal` is the issue #61 population (27 C / 3.30 V, VCM =
VDD/2); `full` takes the T and VDD axes from the CMRR configuration module
(one source for every 45-point bench) and adds `"grid": "full"` to the
inputs; a record's retained inputs select the grid it was measured on.
Records written before this module carry no fingerprint and stay
freshness-unknown (no fingerprint is back-filled for them).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_CMRR = harness.load_config_module(HERE.parent / "cmrr" / "measurement_config.py")

EXPERIMENT = "cmrr-mc"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/cmrr-mc/testbench/tb_cmrr_mc.spice"}
MODULE_REL = "sim/cmrr-mc/measurement_config.py"

CORNERS = _CMRR.CORNERS
PASSIVE_SECTIONS = _CMRR.PASSIVE_SECTIONS
MODEL_LIB = _CMRR.MODEL_LIB
#: Nominal temperature and supply (= sim/offset-mc/); also the point of every control.
TEMP_C = 27.0
VDD_V = 3.30

#: Monte Carlo request (same as sim/offset-mc/).
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"

#: Control sizes: switch-off (samples per corner), process-only (typical)
#: and the mirror-imbalance DUT copy under the same Monte Carlo (typical).
CONTROL_OFF_N = 4
CONTROL_PROC_N = 20
CONTROL_IMB_N = 300
CONTROL_MIRROR = _CMRR.CONTROL_MIRROR  # ("XM3", "W", "6u", "5.4u")

#: The `.param` values of the first (dm) sweep, servo included; the tail's
#: `alter`s set the common-mode drive (acp = acn = 1) for the second.
DM_PARAMS = {"acp": 0.5, "acn": -0.5, **_CMRR.SERVO_NOMINAL}

#: |Acm| below this is clamped and the figure is a lower bound (10 x the
#: systematic record's demonstrated superposition floor of 2.62e-11 V/V).
FLOOR = 2.62e-10

#: Figures. The DC band is sweep indices 0..20 (0.1-1 Hz); spot frequencies
#: sit exactly on the 20-points-per-decade grid from 0.1 Hz.
BAND = (0, 20)
SPOTS = {"1k": (1e3, 80), "10k": (1e4, 100), "100k": (1e5, 120), "1m": (1e6, 140)}
FIGS = ("dc", "1k", "10k", "100k", "1m", "fu")


def grid_axes(grid: str = "nominal") -> tuple[list[float], list[float]]:
    """(temperatures, supplies) of a grid; VCM = VDD/2 paired by index."""
    if grid == "nominal":
        return [float(TEMP_C)], [float(VDD_V)]
    if grid == "full":
        return [float(t) for t in _CMRR.TEMPS_C], [float(v) for v in _CMRR.SUPPLIES_V]
    raise ValueError(f"unknown grid {grid!r}")


def grid_of(retained: dict | None) -> str:
    """The grid a record measured (`nominal` when its inputs name none)."""
    return (retained or {}).get("grid", "nominal")


def ac_args_default() -> str:
    return f"dec {_CMRR.AC_PPD:g} {_CMRR.AC_FSTART:g} {_CMRR.AC_FSTOP:g}"


def ac_args() -> str:
    return ac_args_default()


def scalars() -> list[str]:
    """Every scalar the tail prints (`print <name>` -> `<name> = <value>` in the log)."""
    return (
        ["cmc_op1_vout", "cmc_op1_vinp", "cmc_offset", "cmc_op_parity", "cmc_npts", "cmc_f0", f"cmc_f{BAND[1]}"]
        + [f"cmc_f{i}" for _, i in SPOTS.values()]
        + ["cmc_dm_vderr", "cmc_dm_vcerr", "cmc_cm_vcerr", "cmc_cm_leak", "cmc_det_min", "cmc_acm_min",
           "cmc_cmrr_spread", "cmc_acm_spread", "cmc_ad_spread", "cmc_acm_re0", "cmc_acm_im0"]
        + [f"cmc_{k}_{f}" for f in FIGS for k in ("cmrr", "ad", "acm")]
        + ["cmc_fu_hz"]
    )


def analysis_tail(ac_args: str | None = None) -> str:
    """Control commands klt places verbatim after its `ac <args>` line.

    Plot ac1 is the differential sweep (the testbench's own .param values,
    acp = +0.5, acn = -0.5); the two `alter`s set the common-mode drive
    (acp = acn = 1) for plot ac2. `op1`/`op2` are the operating points after
    each sweep. Everything is evaluated in plot ac2, which stays current, so
    klt's own `expr` lines (if any) see the same vectors.
    """
    ac_args = ac_args or ac_args_default()
    lo, hi = BAND
    L = [
        "op",
        "let cmc_op1_vout = v(vout)",
        "let cmc_op1_vinp = v(vinp)",
        "let cmc_offset = v(vout) - v(vinp)",
        "alter vcm acmag=1",
        "alter vnac acmag=1",
        f"ac {ac_args}",
        "op",
        "let cmc_op_parity = v(vout) - op1.v(vout)",
        "setplot ac2",
        "let cmc_op1_vout = op1.cmc_op1_vout",
        "let cmc_op1_vinp = op1.cmc_op1_vinp",
        "let cmc_offset = op1.cmc_offset",
        "let cmc_op_parity = op2.cmc_op_parity",
        "let cmc_npts = length(frequency)",
        "let cmc_f0 = real(frequency[0])",
        f"let cmc_f{hi} = real(frequency[{hi}])",
        *[f"let cmc_f{i} = real(frequency[{i}])" for _, i in SPOTS.values()],
        # actual input phasors of both sweeps
        "let cmc_vd1 = ac1.v(vinp) - ac1.v(vinn)",
        "let cmc_vc1 = (ac1.v(vinp) + ac1.v(vinn)) / 2",
        "let cmc_vd2 = v(vinp) - v(vinn)",
        "let cmc_vc2 = (v(vinp) + v(vinn)) / 2",
        "let cmc_dm_vderr = vecmax(mag(cmc_vd1 - 1))",
        "let cmc_dm_vcerr = vecmax(mag(cmc_vc1))",
        "let cmc_cm_vcerr = vecmax(mag(cmc_vc2 - 1))",
        "let cmc_cm_leak = vecmax(mag(cmc_vd2 / cmc_vc2))",
        # exact per-frequency solve of vout = Ad vd + Acm vc (run_cmrr.solve_ad_acm)
        "let cmc_det = cmc_vd1 * cmc_vc2 - cmc_vc1 * cmc_vd2",
        "let cmc_det_min = vecmin(mag(cmc_det))",
        "let cmc_adv = (ac1.v(vout) * cmc_vc2 - v(vout) * cmc_vc1) / cmc_det",
        "let cmc_acmv = (cmc_vd1 * v(vout) - cmc_vd2 * ac1.v(vout)) / cmc_det",
        "let cmc_acmmag = mag(cmc_acmv)",
        "let cmc_acm_min = vecmin(cmc_acmmag)",
        "let cmc_addb = db(cmc_adv)",
        "let cmc_acmdb = db(cmc_acmv)",
        "let cmc_cmrrdb = cmc_addb - cmc_acmdb",
        f"let cmc_cmrr_spread = vecmax(cmc_cmrrdb[{lo},{hi}]) - vecmin(cmc_cmrrdb[{lo},{hi}])",
        f"let cmc_acm_spread = vecmax(cmc_acmdb[{lo},{hi}]) - vecmin(cmc_acmdb[{lo},{hi}])",
        f"let cmc_ad_spread = vecmax(cmc_addb[{lo},{hi}]) - vecmin(cmc_addb[{lo},{hi}])",
        "let cmc_acm_re0 = real(cmc_acmv[0])",
        "let cmc_acm_im0 = imag(cmc_acmv[0])",
        f"let cmc_cmrr_dc = mean(cmc_cmrrdb[{lo},{hi}])",
        f"let cmc_ad_dc = mean(cmc_addb[{lo},{hi}])",
        f"let cmc_acm_dc = mean(cmc_acmmag[{lo},{hi}])",
    ]
    for name, (_, i) in SPOTS.items():
        L += [f"let cmc_cmrr_{name} = cmc_cmrrdb[{i}]", f"let cmc_ad_{name} = cmc_addb[{i}]",
              f"let cmc_acm_{name} = cmc_acmmag[{i}]"]
    # f_u: first falling 0 dB crossing of |Ad| (ngspice `meas`, linear in f)
    L += [
        "meas ac cmc_fu_hz when cmc_addb=0 fall=1",
        "meas ac cmc_cmrr_fu find cmc_cmrrdb when cmc_addb=0 fall=1",
        "meas ac cmc_acm_fu find cmc_acmmag when cmc_addb=0 fall=1",
        "let cmc_ad_fu = 0",
    ]
    L += [f"print {s}" for s in scalars()]
    return "\n".join(L)


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict.

    `retained` is a record's stored inputs (or `{"grid": "full"}` from the
    runner); it selects the grid only."""
    grid = grid_of(retained)
    temps, supplies = grid_axes(grid)
    out = {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "ac", "args": ac_args() + "\n" + analysis_tail()},
        "excitation": {"dm": dict(DM_PARAMS), "servo": dict(_CMRR.SERVO_NOMINAL), "cm": dict(_CMRR.MODES["cm"])},
        "figures": {"band": list(BAND), "spots": {k: list(v) for k, v in SPOTS.items()}, "figs": list(FIGS),
                    "floor": FLOOR},
        "monte_carlo": {"n": MC_N, "seed": MC_SEED, "vary": MC_VARY, "model_switch": "sw_stat_mismatch"},
        "controls": {"switch_off_n": CONTROL_OFF_N, "process_only_n": CONTROL_PROC_N,
                     "imbalance_n": CONTROL_IMB_N, "mirror": list(CONTROL_MIRROR),
                     "point": {"temperature_c": float(TEMP_C), "supply_v": float(VDD_V)}},
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": temps,
            "supply_v": supplies,
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }
    if grid != "nominal":
        out["grid"] = grid
    return out


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "bench content incl. the mismatch switch, `.ac` analysis + two-sweep control tail, dm/cm drive and servo, "
        "figure definitions and numerical floor, Monte Carlo count/seed/mode, control settings, corner "
        "conditions, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
