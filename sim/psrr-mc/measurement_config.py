"""Effective measurement configuration of the PSRR Monte Carlo experiment (issue #99; #89 convention).

Stdlib only. `run_psrr_mc.py` imports its conditions, Monte Carlo settings,
excitation, the control-language tail appended to the `.ac` analysis line,
the numerical floor and the control settings FROM here, so the fingerprint
cannot drift from what is run. The sweep, servo and model library come from
the CMRR configuration module (the systematic PSRR bench shares them), which
takes the corner axes from the gain configuration.

Fingerprinted: the canonical bench content (including the `sw_stat_mismatch`
switch), the `.ac` analysis plus the three-sweep control tail (dm -> vdd ->
vss in one deck, the exact per-frequency transfer computations, the
numerical-floor clamp and every printed scalar), the dm excitation and servo
values, the Monte Carlo request (count, seed, `vary`), the corner conditions,
the figure definitions (DC band, spot indices), the control settings and the
model library/sections. NOT fingerprinted: record IDs, workspace paths,
backend/retry options, validation tolerances and the statistics code. The
DUT has its own hash (#75).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_CMRR = harness.load_config_module(HERE.parent / "cmrr" / "measurement_config.py")

EXPERIMENT = "psrr-mc"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCHES_REL = {"bench": "sim/psrr-mc/testbench/tb_psrr_mc.spice"}
MODULE_REL = "sim/psrr-mc/measurement_config.py"

CORNERS = _CMRR.CORNERS
PASSIVE_SECTIONS = _CMRR.PASSIVE_SECTIONS
MODEL_LIB = _CMRR.MODEL_LIB
#: The issue #99 population: nominal temperature and supply (= sim/offset-mc/,
#: sim/cmrr-mc/ nominal), VCM = VDD/2.
TEMP_C = 27.0
VDD_V = 3.30

#: Monte Carlo request (same as sim/offset-mc/ and sim/cmrr-mc/).
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"

#: The `.param` values of the first (dm) sweep; the tail's `alter`s move the
#: AC drive to the vdd rail, then to the vss rail.
DM_PARAMS = {"acp": 0.5, "acn": -0.5, "acdd": 0.0, "acss": 0.0}

#: Numerical floor of |Asupply| (V/V): 10 x the superposition residual
#: demonstrated by the systematic PSRR record 20261010-163854-0127eb7. Below
#: it the transfer is clamped in the deck and the figure is a lower bound.
FLOOR = 1.45e-10

#: Figures. The DC band is sweep indices 0..20 (0.1-1 Hz); spot frequencies
#: sit exactly on the 20-points-per-decade grid from 0.1 Hz.
BAND = (0, 20)
SPOTS = {"1k": (1e3, 80), "10k": (1e4, 100), "100k": (1e5, 120), "1m": (1e6, 140)}
FIGS = ("dc", "1k", "10k", "100k", "1m", "fu")

#: Controls: mismatch switch-off (samples per corner), process-only (typical)
#: and the mirror-imbalance DUT copy under the same Monte Carlo (typical).
CONTROL_OFF_N = 4
CONTROL_PROC_N = 20
CONTROL_IMB_N = 300
CONTROL_MIRROR = ("XM3", "W", "6u", "5.4u")

RAILS = ("vdd", "vss")


def ac_args() -> str:
    return f"dec {_CMRR.AC_PPD:g} {_CMRR.AC_FSTART:g} {_CMRR.AC_FSTOP:g}"


def scalars() -> list[str]:
    """Every scalar the tail prints (`print <name>` -> `<name> = <value>`)."""
    out = ["pmc_op1_vout", "pmc_op1_vinp", "pmc_offset", "pmc_par_vdd", "pmc_par_vss", "pmc_npts", "pmc_f0",
           f"pmc_f{BAND[1]}"]
    out += [f"pmc_f{i}" for _, i in SPOTS.values()]
    out += ["pmc_dm_vderr", "pmc_dm_vcerr", "pmc_dm_railq", "pmc_ad_spread"]
    for r in RAILS:
        out += [f"pmc_{r}_err", f"pmc_{r}_oth", f"pmc_{r}_inq", f"pmc_{r}_leak", f"pmc_{r}_amin",
                f"pmc_{r}_spread", f"pmc_{r}_aspread", f"pmc_{r}_re0", f"pmc_{r}_im0"]
    out += [f"pmc_ad_{f}" for f in FIGS]
    out += [f"pmc_{k}_{r}_{f}" for r in RAILS for k in ("psrr", "asup") for f in FIGS]
    out += ["pmc_fu_hz"]
    return out


def analysis_tail(args: str | None = None) -> str:
    """Control commands klt places verbatim after its `ac <args>` line.

    Plot ac1 is the dm sweep (the bench's own `.param` drive); ac2 the vdd
    sweep; ac3 the vss sweep; op1..op3 the DC points after each. Every figure
    is computed in plot ac3, which stays current. Asupply = vout / v(rail)
    from the actual rail phasor; |Asupply| below FLOOR is clamped elementwise
    (`pos()`), so PSRR is never a division by zero.
    """
    args = args or ac_args()
    lo, hi = BAND
    fl = f"{FLOOR:.6g}"
    L = [
        "op",
        "let pmc_op1_vout = v(vout)",
        "let pmc_op1_vinp = v(vinp)",
        "let pmc_offset = v(vout) - v(vinp)",
        "alter vcm acmag=0",
        "alter vnac acmag=0",
        "alter vdd acmag=1",
        f"ac {args}",
        "op",
        "let pmc_par_vdd = v(vout) - op1.v(vout)",
        "alter vdd acmag=0",
        "alter vss acmag=1",
        f"ac {args}",
        "op",
        "let pmc_par_vss = v(vout) - op1.v(vout)",
        "setplot ac3",
        "let pmc_op1_vout = op1.pmc_op1_vout",
        "let pmc_op1_vinp = op1.pmc_op1_vinp",
        "let pmc_offset = op1.pmc_offset",
        "let pmc_par_vdd = op2.pmc_par_vdd",
        "let pmc_par_vss = op3.pmc_par_vss",
        "let pmc_npts = length(frequency)",
        "let pmc_f0 = real(frequency[0])",
        f"let pmc_f{hi} = real(frequency[{hi}])",
        *[f"let pmc_f{i} = real(frequency[{i}])" for _, i in SPOTS.values()],
        # dm sweep: vd = 1, vc = 0, both rails quiet (actual phasors)
        "let pmc_vd1 = ac1.v(vinp) - ac1.v(vinn)",
        "let pmc_dm_vderr = vecmax(mag(pmc_vd1 - 1))",
        "let pmc_dm_vcerr = vecmax(mag((ac1.v(vinp) + ac1.v(vinn)) / 2))",
        "let pmc_dm_railq = vecmax(mag(ac1.v(vdd))) + vecmax(mag(ac1.v(vss)))",
        "let pmc_adv = ac1.v(vout) / pmc_vd1",
        "let pmc_addb = db(pmc_adv)",
        f"let pmc_ad_spread = vecmax(pmc_addb[{lo},{hi}]) - vecmin(pmc_addb[{lo},{hi}])",
    ]
    for r, plot, other in (("vdd", "ac2", "vss"), ("vss", "ac3", "vdd")):
        L += [
            f"let pmc_{r}_err = vecmax(mag({plot}.v({r}) - 1))",
            f"let pmc_{r}_oth = vecmax(mag({plot}.v({other})))",
            f"let pmc_{r}_inq = vecmax(mag({plot}.v(vinp))) + vecmax(mag({plot}.v(vinn)))",
            f"let pmc_{r}_av = {plot}.v(vout) / {plot}.v({r})",
            # max over f of |Ad vd| / |vout|: the input-leak share of the supply-run output
            (f"let pmc_{r}_leak = vecmax(mag(pmc_adv * ({plot}.v(vinp) - {plot}.v(vinn))) "
             f"/ (mag({plot}.v(vout)) + {fl}))"),
            f"let pmc_{r}_am = mag(pmc_{r}_av)",
            f"let pmc_{r}_amin = vecmin(pmc_{r}_am)",
            f"let pmc_{r}_ae = pmc_{r}_am + pos({fl} - pmc_{r}_am) * ({fl} - pmc_{r}_am)",
            f"let pmc_{r}_adb = db(pmc_{r}_ae)",
            f"let pmc_{r}_cps = pmc_addb - pmc_{r}_adb",
            f"let pmc_{r}_spread = vecmax(pmc_{r}_cps[{lo},{hi}]) - vecmin(pmc_{r}_cps[{lo},{hi}])",
            f"let pmc_{r}_aspread = vecmax(pmc_{r}_adb[{lo},{hi}]) - vecmin(pmc_{r}_adb[{lo},{hi}])",
            f"let pmc_{r}_re0 = real(pmc_{r}_av[0])",
            f"let pmc_{r}_im0 = imag(pmc_{r}_av[0])",
            f"let pmc_psrr_{r}_dc = mean(pmc_{r}_cps[{lo},{hi}])",
            f"let pmc_asup_{r}_dc = mean(pmc_{r}_ae[{lo},{hi}])",
        ]
        for name, (_, i) in SPOTS.items():
            L += [f"let pmc_psrr_{r}_{name} = pmc_{r}_cps[{i}]", f"let pmc_asup_{r}_{name} = pmc_{r}_ae[{i}]"]
    L.append(f"let pmc_ad_dc = mean(pmc_addb[{lo},{hi}])")
    L += [f"let pmc_ad_{name} = pmc_addb[{i}]" for name, (_, i) in SPOTS.items()]
    # f_u: first falling 0 dB crossing of |Ad| (ngspice `meas`, linear in f)
    L.append("meas ac pmc_fu_hz when pmc_addb=0 fall=1")
    for r in RAILS:
        L += [f"meas ac pmc_psrr_{r}_fu find pmc_{r}_cps when pmc_addb=0 fall=1",
              f"meas ac pmc_asup_{r}_fu find pmc_{r}_ae when pmc_addb=0 fall=1"]
    L.append("let pmc_ad_fu = 0")
    L += [f"print {s}" for s in scalars()]
    return "\n".join(L)


def inputs(texts: dict, retained: dict | None = None) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(texts["bench"]),
        "analysis": {"kind": "ac", "args": ac_args() + "\n" + analysis_tail()},
        "excitation": {"dm": dict(DM_PARAMS), "servo": dict(_CMRR.SERVO_NOMINAL), "rails": list(RAILS)},
        "figures": {"band": list(BAND), "spots": {k: list(v) for k, v in SPOTS.items()}, "figs": list(FIGS),
                    "floor": FLOOR},
        "monte_carlo": {"n": MC_N, "seed": MC_SEED, "vary": MC_VARY, "model_switch": "sw_stat_mismatch"},
        "controls": {"switch_off_n": CONTROL_OFF_N, "process_only_n": CONTROL_PROC_N,
                     "imbalance_n": CONTROL_IMB_N, "mirror": list(CONTROL_MIRROR)},
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(TEMP_C)],
            "supply_v": [float(VDD_V)],
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(texts: dict, retained: dict | None = None) -> str:
    return harness.measurement_fingerprint(inputs(texts, retained))


def fingerprint_lines(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(texts, retained),
        "bench content incl. the mismatch switch, `.ac` analysis + three-sweep control tail, dm drive/servo, "
        "figure definitions and numerical floor, Monte Carlo count/seed/mode, control settings, corner "
        "conditions, model library/sections")


def inputs_section(texts: dict, retained: dict | None = None) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(texts, retained), MODULE_REL)
