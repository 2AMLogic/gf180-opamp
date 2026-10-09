#!/usr/bin/env python3
"""Mismatch-aware CMRR of the committed sized schematic: Monte Carlo over
per-instance MOS mismatch (issue #61; DR-3 residual (e3)).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_cmrr_mc.spice`) declares no transistor; its circuit
is line-for-line the systematic CMRR bench (`sim/cmrr/testbench/tb_cmrr.spice`)
with the mismatch switch added to its single `.param` line.

Why one deck per sample
-----------------------
The systematic driver (`sim/cmrr/run_cmrr.py`) gets Ad and Acm from TWO
`klt sim` requests (a differential and a common-mode excitation) and solves
them jointly. Under Monte Carlo the two excitations must see the SAME sampled
devices, or the solve mixes two different amplifiers. Here both excitations
run in ONE ngspice process on one parsed circuit: the driver appends control
commands to the `.ac` analysis line (the same mechanism as the systematic
driver's operating-point print), which

    ac (dm) -> op -> alter the two AC sources to the cm drive -> ac (cm) -> op

and then solves `vout = Ad vd + Acm vc` per frequency from the ACTUAL input
phasors of both sweeps (the systematic driver's exact solve, written in the
ngspice control language), and prints the scalar figures into the log. The
per-instance mismatch values are drawn when the netlist is parsed, so both
sweeps see the same devices; each sample proves it: the DC output printed
after each sweep (it carries the sampled offset) must agree to 1 uV.

What it runs
------------
ONE `klt sim` request: the five MOS corners at the grid's temperature and
supply points with `monte_carlo = {n: 300, seed: 45, vary: "mismatch"}` --
the same corners, N and seed as `sim/offset-mc/`. Which backend executes it
is klt's decision (`--backend`, `$KLT_SIM_BACKEND`); on a dispatch worker that
is the Spot batch fleet. This script never launches ngspice itself, never
loops ngspice over a grid and never falls back to a local grid when a batch
submit fails: it stops with the error and writes no record.

Controls (small requests, same deck):
  * switch-off: `sw_stat_mismatch = 0`, every corner, a few samples: the
    spread must be exactly zero and the values must reproduce the committed
    systematic record (`sim/cmrr/records/20261009-105631-30ec86d.md`).
  * process-only: `vary: "process"` at typical, recorded as measured (klt
    feeds ngspice one combined seed, so it is not a sigma = 0 control with
    this deck -- the offset-mc finding, 2AMLogic/klayout-tools#2937).
  * imbalance (negative control): the systematic driver's mirror-imbalance
    DUT copy (XM3 W 6u -> 5.4u) under the same mismatch Monte Carlo at
    typical: it must lower the CMRR statistic and raise mean |Acm|
    significantly.

    python3 sim/cmrr-mc/run_cmrr_mc.py --probe     # N=3 AC+MC capability probe
    python3 sim/cmrr-mc/run_cmrr_mc.py             # full run + record
    python3 sim/cmrr-mc/run_cmrr_mc.py --smoke     # one local unit, no record

No bound is judged here: bound ratification is a decision record's job.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import re
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))

from harness import (  # noqa: E402
    KltError,
    Pdk,
    allocate_record_id,
    batch_block,
    claim_record_paths,
    find_pdk,
    klt_version,
    load_dut_text,
    ngspice_version,
    remote_of,
    run_klt,
    run_klt_retrying,
    sanitise_report,
)

# The systematic CMRR driver owns the bench guards, the materialisation, the
# mirror-imbalance mutation and the gain driver's request shape; reuse them.
if "cmrr_driver" in sys.modules:
    C = sys.modules["cmrr_driver"]
else:
    _spec = importlib.util.spec_from_file_location("cmrr_driver", REPO_ROOT / "sim" / "cmrr" / "run_cmrr.py")
    C = importlib.util.module_from_spec(_spec)
    sys.modules["cmrr_driver"] = C
    _spec.loader.exec_module(C)
G = C.G

TESTBENCH = HERE / "testbench" / "tb_cmrr_mc.spice"
SYSTEMATIC_TB = REPO_ROOT / "sim" / "cmrr" / "testbench" / "tb_cmrr.spice"
#: The committed systematic record the switch-off control must reproduce.
SYSTEMATIC_RECORD = "20261009-105631-30ec86d"
SYSTEMATIC_DIR = REPO_ROOT / "sim" / "cmrr"
#: The committed offset Monte Carlo (same corners, N, seed): per-sample link.
OFFSET_MC_RECORD = "20261009-072205-96bf3cc"
OFFSET_MC_CSV = REPO_ROOT / "sim" / "offset-mc" / "corners" / OFFSET_MC_RECORD / "offset_samples.csv"

# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------

CORNERS = list(C.CORNERS)
#: Issue #61 population: nominal temperature and supply, like sim/offset-mc/.
#: `--grid full` adds the ratified T and VDD axes (45 points).
GRIDS = {
    "nominal": ([27.0], [3.30]),
    "full": (list(C.TEMPS_C), list(C.SUPPLIES_V)),
}
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"
PROBE_N = 3
CONTROL_OFF_N = 4
CONTROL_PROC_N = 20
CONTROL_IMB_N = 300

#: Sweep: the systematic bench's `.ac dec 20 0.1 1e9` (201 points). The DC
#: band is indices 0..20 (0.1-1 Hz) and the spot frequencies sit exactly on
#: the grid (index = 20 log10(f / 0.1)); `cmc_f*` prints prove it per sample.
AC_ARGS = f"dec {C.AC_PPD:g} {C.AC_FSTART:g} {C.AC_FSTOP:g}"
N_FREQ = C.N_FREQ
BAND = (0, 20)
SPOTS = {"1k": (1e3, 80), "10k": (1e4, 100), "100k": (1e5, 120), "1m": (1e6, 140)}
FIGS = ("dc", "1k", "10k", "100k", "1m", "fu")
FIG_LABEL = {"dc": "DC plateau (0.1-1 Hz)", "1k": "1 kHz", "10k": "10 kHz", "100k": "100 kHz",
             "1m": "1 MHz", "fu": "at f_u (differential unity gain)"}

#: Per-sample validity (blocking): the systematic driver's tolerances.
EXC_TOL = C.EXC_TOL
CM_LEAK_MAX = C.CM_LEAK_MAX
OP_VOUT_TOL_V = C.OP_VOUT_TOL_V
#: Both sweeps of one sample must sit at the same DC operating point; the DC
#: output carries the sampled offset (sigma ~ 5 mV), so a re-drawn mismatch
#: between the sweeps would show up here by orders of magnitude.
PARITY_TOL_V = 1e-6
PLATEAU_TOL_DB = C.PLATEAU_TOL_DB
#: |Acm| below this is clamped and the figure is a lower bound (the systematic
#: record's demonstrated superposition floor, 10 x 2.62e-11 V/V).
FLOOR = 2.62e-10
#: Mismatch must visibly act: per-corner offset sigma above this (the
#: switch-off control gives exactly 0; offset-mc measured 4.3-5.0 mV).
MIN_OFFSET_SIGMA_V = 1e-3
#: Switch-off control vs the committed systematic record (same point).
TOL_SYS_DB = 0.01  # DC and spot figures, read on identical grid samples
TOL_SYS_FU_REL = 5e-3  # f_u: ngspice `meas` interpolates linearly in f
TOL_SYS_FU_DB = 0.1  # CMRR at f_u, same reason
#: Negative control: the imbalance must raise mean |Acm| at DC by more than
#: this many standard errors of the difference, and lower the statistic.
CONTROL_SE_MULT = 3.0
CONTROL_MIRROR = C.CONTROL_MIRROR  # ("XM3", "W", "6u", "5.4u")

# --------------------------------------------------------------------------
# The control-language tail appended to the `.ac` analysis line
# --------------------------------------------------------------------------

#: Every scalar the tail prints (`print <name>` -> `<name> = <value>` in the
#: log). Order is the print order.
SCALARS: list[str] = (
    ["cmc_op1_vout", "cmc_op1_vinp", "cmc_offset", "cmc_op_parity", "cmc_npts", "cmc_f0", "cmc_f20"]
    + [f"cmc_f{i}" for _, i in SPOTS.values()]
    + ["cmc_dm_vderr", "cmc_dm_vcerr", "cmc_cm_vcerr", "cmc_cm_leak", "cmc_det_min", "cmc_acm_min",
       "cmc_cmrr_spread", "cmc_acm_spread", "cmc_ad_spread", "cmc_acm_re0", "cmc_acm_im0"]
    + [f"cmc_{k}_{f}" for f in FIGS for k in ("cmrr", "ad", "acm")]
    + ["cmc_fu_hz"]
)


def analysis_tail(ac_args: str = AC_ARGS) -> str:
    """Control commands klt places verbatim after its `ac <args>` line.

    Plot ac1 is the differential sweep (the testbench's own .param values,
    acp = +0.5, acn = -0.5); the two `alter`s set the common-mode drive
    (acp = acn = 1) for plot ac2. `op1`/`op2` are the operating points after
    each sweep. Everything is evaluated in plot ac2, which stays current, so
    klt's own `expr` lines (if any) see the same vectors.
    """
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
        f"let cmc_f20 = real(frequency[{hi}])",
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
    L += [f"print {s}" for s in SCALARS]
    return "\n".join(L)


# --------------------------------------------------------------------------
# Testbench guard and materialisation
# --------------------------------------------------------------------------

PARAM_KEYS = ["acp", "acn", "rsv", "csv", "sw_stat_mismatch"]
DM_PARAMS = {"acp": 0.5, "acn": -0.5, **C.SERVO_NOMINAL}


def _circuit_lines(text: str) -> list[str]:
    return [ln for ln in C.code_lines(text) if not ln.lower().startswith(".param")]


def guard_testbench(text: str) -> list[str]:
    """Reasons the bench no longer is the systematic CMRR bench + the switch.

    The systematic driver's guards (servo, CL, Ibias, Xdut wiring, AC-quiet
    Vdd, no hand-declared device, circuit body), plus: every circuit line is
    identical to `sim/cmrr/testbench/tb_cmrr.spice`; the one `.param` line
    carries exactly acp/acn/rsv/csv/sw_stat_mismatch and comes after the
    design.ngspice include; sw_stat_global is never touched.
    """
    errs = C.guard_testbench(text)
    if _circuit_lines(text) != _circuit_lines(SYSTEMATIC_TB.read_text()):
        errs.append("circuit differs from sim/cmrr/testbench/tb_cmrr.spice (only the .param line may differ)")
    code = C.code_lines(text)
    try:
        keys = C.param_keys(text)
        if sorted(keys) != sorted(PARAM_KEYS):
            errs.append(f".param keys {keys} != {PARAM_KEYS}")
    except RuntimeError as exc:
        errs.append(str(exc))
    pi = [i for i, ln in enumerate(code) if ln.lower().startswith(".param")]
    ii = [i for i, ln in enumerate(code) if re.match(r"\.include\s+['\"]?design\.ngspice", ln, re.I)]
    if pi and ii and pi[0] < ii[0]:
        errs.append("the .param line precedes the design.ngspice include (the PDK default sw_stat_mismatch=0 would win)")
    if re.search(r"sw_stat_global", "\n".join(code), re.I):
        errs.append("testbench must not touch sw_stat_global (global spread is the corners' job)")
    return errs


def materialise(work: Path, pdk: Pdk, *, mismatch: int = 1, dut_text: str | None = None,
                allow_missing: tuple[str, ...] = ()) -> Path:
    params = dict(DM_PARAMS, sw_stat_mismatch=int(mismatch))
    return C.materialise(work, pdk, params, testbench=TESTBENCH, guard=guard_testbench,
                         dut_text=dut_text, allow_missing=allow_missing)


def imbalance_dut() -> str:
    inst, prm, old, new = CONTROL_MIRROR
    return C.mutate_instance(load_dut_text(), inst, prm, old, new)


# --------------------------------------------------------------------------
# klt requests
# --------------------------------------------------------------------------


def request(netlist: Path, pdk: Pdk, corners, temps, supplies, *, n: int | None, seed: int = MC_SEED,
            vary: str = MC_VARY, expr: tuple[str, ...] = (), local: bool = False) -> dict:
    """The systematic bench's request shape (gain driver's corner bundles,
    vcm = VDD/2), the two-sweep tail appended to the analysis line, no
    rawfile (the figures are printed scalars), logs retained."""
    req = G.ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"]["args"] = req["analysis"]["args"] + "\n" + analysis_tail(req["analysis"]["args"])
    req["measurements"] = [{"name": s.removeprefix("cmc_") + "_x", "expr": s, "unit": ""} for s in expr]
    req["options"] = {"timeout_s": 300, "keep_artifacts": True, "waveforms": False}
    if n is not None:
        req["monte_carlo"] = {"n": int(n), "seed": int(seed), "vary": vary}
    if local:
        req["models"]["pdk_root"] = str(pdk.path.parent)
    return req


# --------------------------------------------------------------------------
# Log parsing, per-sample validation (pure; unit-tested offline)
# --------------------------------------------------------------------------

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_SCALAR_RE = re.compile(rf"^\s*(cmc_\w+)\s*=\s*({_NUM})\s*$", re.M | re.I)


def parse_scalars(log: str) -> dict[str, float]:
    """`cmc_* = value` lines of an ngspice log (last occurrence wins, so the
    explicit `print` after a `meas` line is the value read)."""
    out: dict[str, float] = {}
    for name, val in _SCALAR_RE.findall(log):
        out[name.lower()] = float(val)
    return out


@dataclass
class Sample:
    key: tuple  # (process, temperature_c, vdd)
    index: int | None
    seeds: dict
    vals: dict[str, float]
    problems: list[str] = field(default_factory=list)
    lower_bound: bool = False

    @property
    def valid(self) -> bool:
        return not self.problems

    def fig(self, kind: str, f: str) -> float:
        return self.vals[f"cmc_{kind}_{f}"]


def validate_sample(vals: dict[str, float], vcm: float) -> tuple[list[str], bool]:
    """Blocking problems of one sample (empty = valid) and its lower-bound flag."""
    p: list[str] = []
    miss = [s for s in SCALARS if s not in vals or not math.isfinite(vals[s])]
    if miss:
        return [f"missing/non-finite scalars: {', '.join(miss[:4])}{'...' if len(miss) > 4 else ''}"], False
    if int(round(vals["cmc_npts"])) != N_FREQ:
        p.append(f"{vals['cmc_npts']:g} frequency points, expected {N_FREQ}")
    want = {"cmc_f0": C.AC_FSTART, "cmc_f20": C.AC_FSTART * 10, **{f"cmc_f{i}": f for f, i in SPOTS.values()}}
    for k, f in want.items():
        if abs(vals[k] / f - 1) > 1e-6:
            p.append(f"{k} = {vals[k]:g} Hz, expected {f:g} Hz (sweep grid changed)")
    if vals["cmc_dm_vderr"] > EXC_TOL or vals["cmc_dm_vcerr"] > EXC_TOL:
        p.append(f"differential excitation off (|vd-1| {vals['cmc_dm_vderr']:.3g}, |vc| {vals['cmc_dm_vcerr']:.3g} V)")
    if vals["cmc_cm_vcerr"] > EXC_TOL:
        p.append(f"common-mode excitation off by {vals['cmc_cm_vcerr']:.3g} V")
    if vals["cmc_cm_leak"] > CM_LEAK_MAX:
        p.append(f"common-mode run carries |vd/vc| = {vals['cmc_cm_leak']:.3g} (unequal CM drive)")
    if vals["cmc_det_min"] < 1e-6:
        p.append("singular excitation matrix")
    if abs(vals["cmc_op_parity"]) > PARITY_TOL_V:
        p.append(f"dm/cm DC operating points differ by {vals['cmc_op_parity'] * 1e6:.3f} uV "
                 "(the two sweeps did not see the same devices)")
    if abs(vals["cmc_op1_vinp"] - vcm) > 1e-6:
        p.append(f"vinp = {vals['cmc_op1_vinp']:.6g} V, expected VCM {vcm:g} V")
    if abs(vals["cmc_op1_vout"] - vcm) > OP_VOUT_TOL_V:
        p.append(f"vout {vals['cmc_op1_vout']:.4f} V is more than {OP_VOUT_TOL_V} V from VCM")
    if vals["cmc_ad_spread"] > PLATEAU_TOL_DB:
        p.append(f"no Ad plateau ({vals['cmc_ad_spread']:.3f} dB over 0.1-1 Hz)")
    if vals["cmc_cmrr_spread"] > PLATEAU_TOL_DB or vals["cmc_acm_spread"] > PLATEAU_TOL_DB:
        p.append(f"no CMRR/|Acm| plateau ({vals['cmc_cmrr_spread']:.3f} / {vals['cmc_acm_spread']:.3f} dB)")
    if not (C.AC_FSTART < vals["cmc_fu_hz"] < C.AC_FSTOP):
        p.append(f"f_u {vals['cmc_fu_hz']:g} Hz outside the sweep")
    lb = vals["cmc_acm_min"] < FLOOR
    return p, lb


def clamp_floor(s: Sample) -> None:
    """Below the numerical floor, |Acm| is clamped and CMRR re-derived from it
    (a lower bound); never a division by zero."""
    for f in FIGS:
        a = s.vals[f"cmc_acm_{f}"]
        if a < FLOOR:
            s.vals[f"cmc_acm_{f}"] = FLOOR
            s.vals[f"cmc_cmrr_{f}"] = s.vals[f"cmc_ad_{f}"] - 20 * math.log10(FLOOR)
            s.lower_bound = True


def _corner_key(c: dict) -> tuple:
    return G.point_key(c)


def extract(report: dict, want: list[tuple], n_expected: int, *, read_log=None) -> tuple[dict[tuple, list[Sample]], list[str]]:
    """Samples per grid point from a klt Monte Carlo report.

    Blocking: every expected point contributes exactly `n_expected` samples
    with distinct sample indices 0..n-1, each with a retained log, every
    scalar present and every per-sample check passing. Nothing is dropped
    silently: an invalid sample is a problem string.
    """
    read_log = read_log or (lambda p: Path(p).read_text())
    problems: list[str] = []
    out: dict[tuple, list[Sample]] = {k: [] for k in want}
    seen: dict[tuple, set] = {k: set() for k in want}
    for c in report.get("corners", []):
        try:
            k = _corner_key(c)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{c.get('corner_id', '?')}: unparseable corner")
            continue
        cid = str(c.get("corner_id", G.fmt_key(k)))
        if k not in out:
            problems.append(f"{cid}: unexpected grid point")
            continue
        mc = c.get("monte_carlo") or {}
        idx = mc.get("sample_index")
        if idx is None or idx in seen[k]:
            problems.append(f"{cid}: missing or duplicate sample index {idx!r}")
            continue
        seen[k].add(idx)
        logp = (c.get("artifacts") or {}).get("log")
        try:
            log = read_log(logp) if logp else None
        except OSError:
            log = None
        seeds = {s: mc.get(s) for s in ("seed", "mismatch_seed", "process_seed")}
        if not log:
            diag = "; ".join(d.get("message", "")[:160] for d in c.get("diagnostics", []) if d.get("severity") == "error")
            s = Sample(k, idx, seeds, {}, [f"no ngspice log retained ({diag or c.get('status')})"])
        else:
            vals = parse_scalars(log)
            vcm = float((c.get("supply_v") or {}).get("vcm", k[2] / 2))
            probs, lb = validate_sample(vals, vcm)
            s = Sample(k, idx, seeds, vals, probs, lb)
            if s.valid:
                clamp_floor(s)
        if not s.valid:
            problems.append(f"{cid}: " + "; ".join(s.problems))
        out[k].append(s)
    for k in want:
        got = sorted(seen[k])
        if got != list(range(n_expected)):
            problems.append(f"{G.fmt_key(k)}: sample indices {got[:5]}{'...' if len(got) > 5 else ''} "
                            f"({len(got)}), expected 0..{n_expected - 1}")
    return out, problems


# --------------------------------------------------------------------------
# Statistics (pure; unit-tested offline)
# --------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default 'linear' method)."""
    xs = sorted(values)
    if not xs:
        raise ValueError("no values")
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


@dataclass
class FigStats:
    n: int
    mean_db: float
    sigma_db: float
    min_db: float
    p1_db: float
    p5_db: float
    db_3s: float  # mean - 3 sigma of CMRR (dB)
    acm_mean: float  # |Acm| (V/V)
    acm_sigma: float
    ad_mean_db: float
    lin_3s: float  # mean Ad (dB) - 20 log10(mean |Acm| + 3 sigma |Acm|)
    skew_db: float
    lower_bounds: int


def _skew(xs: list[float]) -> float:
    n = len(xs)
    m = statistics.fmean(xs)
    m2 = sum((x - m) ** 2 for x in xs) / n
    return 0.0 if m2 <= 0 else sum((x - m) ** 3 for x in xs) / n / m2**1.5


def fig_stats(samples: list[Sample], f: str) -> FigStats:
    good = [s for s in samples if s.valid]
    if len(good) < 2:
        raise ValueError("need at least two valid samples")
    c = [s.fig("cmrr", f) for s in good]
    a = [s.fig("acm", f) for s in good]
    d = [s.fig("ad", f) for s in good]
    mean, sig = statistics.fmean(c), statistics.stdev(c)
    am, asg = statistics.fmean(a), statistics.stdev(a)
    adm = statistics.fmean(d)
    return FigStats(
        n=len(good), mean_db=mean, sigma_db=sig, min_db=min(c), p1_db=percentile(c, 1), p5_db=percentile(c, 5),
        db_3s=mean - 3 * sig, acm_mean=am, acm_sigma=asg, ad_mean_db=adm,
        lin_3s=adm - 20 * math.log10(am + 3 * asg), skew_db=_skew(c),
        lower_bounds=sum(s.lower_bound for s in good),
    )


def point_stats(samples: dict[tuple, list[Sample]]) -> dict[tuple, dict[str, FigStats]]:
    return {k: {f: fig_stats(v, f) for f in FIGS} for k, v in samples.items() if v}


def offset_sigma(samples: list[Sample]) -> float:
    xs = [s.vals["cmc_offset"] for s in samples if s.valid]
    return statistics.stdev(xs) if len(xs) >= 2 else float("nan")


def worst(stats: dict[tuple, dict[str, FigStats]], f: str, attr: str) -> tuple[float, tuple]:
    k = min(stats, key=lambda k: getattr(stats[k][f], attr))
    return getattr(stats[k][f], attr), k


# --------------------------------------------------------------------------
# Controls (pure checks; unit-tested offline)
# --------------------------------------------------------------------------


def check_switch_off(samples: dict[tuple, list[Sample]], reference: dict[tuple, dict[str, float]]) -> tuple[list[str], list[dict]]:
    """Mismatch off: zero spread, and agreement with the systematic record."""
    probs: list[str] = []
    rows: list[dict] = []
    for k, ss in samples.items():
        good = [s for s in ss if s.valid]
        if len(good) != len(ss) or not good:
            probs.append(f"switch-off {G.fmt_key(k)}: invalid samples")
            continue
        spread = max(max(s.fig("cmrr", f) for s in good) - min(s.fig("cmrr", f) for s in good) for f in FIGS)
        off_spread = max(s.vals["cmc_offset"] for s in good) - min(s.vals["cmc_offset"] for s in good)
        if spread != 0.0 or off_spread != 0.0:
            probs.append(f"switch-off {G.fmt_key(k)}: samples differ (CMRR spread {spread:.3g} dB, "
                         f"offset spread {off_spread:.3g} V); something other than mismatch varies")
        s0 = good[0]
        ref = reference.get(k)
        row = {"key": k, "vals": {f: s0.fig("cmrr", f) for f in FIGS}, "fu": s0.vals["cmc_fu_hz"],
               "offset": s0.vals["cmc_offset"], "ref": ref, "dev": {}}
        if ref is None:
            probs.append(f"switch-off {G.fmt_key(k)}: no committed systematic reference")
        else:
            for f in FIGS:
                dev = abs(s0.fig("cmrr", f) - ref[f])
                row["dev"][f] = dev
                tol = TOL_SYS_FU_DB if f == "fu" else TOL_SYS_DB
                if dev > tol:
                    probs.append(f"switch-off {G.fmt_key(k)}: CMRR {f} {s0.fig('cmrr', f):.4f} dB vs systematic "
                                 f"{ref[f]:.4f} dB (|dev| {dev:.4f} > {tol} dB)")
            rel = abs(s0.vals["cmc_fu_hz"] / ref["fu_hz"] - 1)
            row["dev"]["fu_hz"] = rel
            if rel > TOL_SYS_FU_REL:
                probs.append(f"switch-off {G.fmt_key(k)}: f_u {s0.vals['cmc_fu_hz']:.6g} Hz vs systematic "
                             f"{ref['fu_hz']:.6g} Hz (rel {rel:.2e} > {TOL_SYS_FU_REL})")
        rows.append(row)
    return probs, rows


def check_imbalance(base: list[Sample], ctl: list[Sample]) -> tuple[list[str], dict]:
    """The deliberately unbalanced mirror must raise mean |Acm| at DC by more
    than CONTROL_SE_MULT standard errors and lower both 3-sigma statistics."""
    b, c = fig_stats(base, "dc"), fig_stats(ctl, "dc")
    se = math.sqrt(b.acm_sigma**2 / b.n + c.acm_sigma**2 / c.n)
    shift = c.acm_mean - b.acm_mean
    info = {"base": b, "ctl": c, "se": se, "shift": shift}
    probs = []
    if not shift > CONTROL_SE_MULT * se:
        probs.append(f"imbalance raised mean |Acm| by {shift:.4g} V/V, not > {CONTROL_SE_MULT:g} SE ({se:.3g})")
    if not c.lin_3s < b.lin_3s:
        probs.append(f"imbalance did not lower the linear 3-sigma CMRR ({c.lin_3s:.2f} vs {b.lin_3s:.2f} dB)")
    if not c.db_3s < b.db_3s:
        probs.append(f"imbalance did not lower the dB mean-3sigma CMRR ({c.db_3s:.2f} vs {b.db_3s:.2f} dB)")
    return probs, info


def systematic_reference(keys: list[tuple]) -> dict[tuple, dict[str, float]]:
    """CMRR figures of the committed systematic record at `keys`: DC and
    spots recomputed from its committed `<point>.cmrr.dat` curve (exact grid
    samples), f_u and CMRR at f_u read from its per-point table."""
    cdir = SYSTEMATIC_DIR / "corners" / SYSTEMATIC_RECORD
    rec = (SYSTEMATIC_DIR / "records" / f"{SYSTEMATIC_RECORD}.md").read_text()
    out: dict[tuple, dict[str, float]] = {}
    for k in keys:
        p = cdir / f"{G.point_stem(k)}.cmrr.dat"
        if not p.is_file():
            continue
        rows = [ln.split() for ln in p.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        cm = [float(r[3]) for r in rows]
        if len(cm) != N_FREQ:
            continue
        ref = {"dc": statistics.fmean(cm[BAND[0]: BAND[1] + 1])}
        for name, (_, i) in SPOTS.items():
            ref[name] = cm[i]
        m = re.search(rf"^\| {re.escape(G.fmt_key(k))} \|([^\n]*)$", rec, re.M)
        if not m:
            continue
        cells = [x.strip() for x in m.group(1).split("|")]
        ref["fu"] = float(cells[5])
        ref["fu_hz"] = float(cells[6]) * 1e6
        out[k] = ref
    return out


def offset_mc_link(samples: dict[tuple, list[Sample]]) -> dict:
    """Per-sample comparison with the committed offset Monte Carlo (same
    corners, N and seed): if klt hands each sample the same seeds and ngspice
    draws the same devices, the DC offsets coincide. Information only."""
    if not OFFSET_MC_CSV.is_file():
        return {"available": False}
    ref: dict[tuple[str, int], dict] = {}
    with OFFSET_MC_CSV.open() as fh:
        for r in csv.DictReader(fh):
            ref[(r["corner"], int(r["sample_index"]))] = r
    n = same_seed = 0
    devs: list[float] = []
    for k, ss in samples.items():
        if k[1] != 27.0 or abs(k[2] - 3.30) > 1e-9:
            continue
        for s in ss:
            r = ref.get((k[0], s.index))
            if r is None or not s.valid:
                continue
            n += 1
            if str(s.seeds.get("seed")) == r["rndseed"]:
                same_seed += 1
                devs.append(abs(s.vals["cmc_offset"] - float(r["offset_v"])))
    return {"available": True, "n": n, "same_seed": same_seed,
            "max_dev_v": max(devs) if devs else float("nan"),
            "within_10uv": sum(d <= 1e-5 for d in devs)}


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def _run(req, outdir, args, workdir) -> dict:
    return run_klt_retrying(req, outdir, args.backend, workdir, retries=args.batch_submit_retries,
                            wait_s=args.batch_retry_wait_s)


def submit(tag: str, tb: Path, pdk: Pdk, corners, temps, supplies, n: int, args, work: Path, *,
           vary: str = MC_VARY, expr: tuple[str, ...] = ()) -> tuple[dict, dict, float]:
    req = request(tb, pdk, corners, temps, supplies, n=n, vary=vary, expr=expr)
    req["batch"] = batch_block(args)
    units = len(corners) * len(temps) * len(supplies) * n
    print(f"  submitting `{tag}` ({units} units)...", flush=True)
    t0 = time.time()
    rep = _run(req, work / tag / "out", args, work / tag)
    wall = time.time() - t0
    rem = remote_of(rep)
    print(f"  `{tag}` done in {wall:.0f} s (job {rem.get('job_id', 'local')})", flush=True)
    return rep, req, wall


def keys_of(corners, temps, supplies) -> list[tuple]:
    return G.expected_keys(corners, temps, supplies)


# --------------------------------------------------------------------------
# Record writing
# --------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def exec_line(tag: str, rep: dict, wall: float) -> str:
    r = remote_of(rep)
    env = rep.get("environment") or {}
    eng_s = f"{env.get('engine', 'ngspice')} {env.get('engine_version', '')}".strip()
    if not r:
        return f"  - `{tag}`: backend `local`; engine `{eng_s}`; wall {wall:.0f} s"
    return (f"  - `{tag}`: backend `{r.get('provider')}`, job `{r.get('job_id')}`, "
            f"{'Spot ' if r.get('spot') else ''}{r.get('instance_type', '')}, state `{r.get('state')}`, "
            f"runner klt `{r.get('runner_klt_version')}` vs client `{r.get('client_klt_version')}` "
            f"(compatibility `{r.get('runner_compatibility')}`); engine `{eng_s}`; wall {wall:.0f} s")


def write_csv(path: Path, samples: dict[tuple, list[Sample]]) -> None:
    cols = ["process", "temperature_c", "vdd_v", "sample_index", "rndseed", "mismatch_seed", "process_seed",
            "valid", "lower_bound"] + [s for s in SCALARS if not re.match(r"cmc_f\d+$", s)]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for k, ss in samples.items():
            for s in sorted(ss, key=lambda s: (s.index is None, s.index)):
                row = [k[0], f"{k[1]:g}", f"{k[2]:.2f}", s.index, s.seeds.get("seed"), s.seeds.get("mismatch_seed"),
                       s.seeds.get("process_seed"), int(s.valid), int(s.lower_bound)]
                row += [repr(s.vals.get(c)) if c in s.vals else "" for c in cols[9:]]
                w.writerow(row)


def write_snapshot(path: Path, record: str, reqs: dict[str, dict], tb_text: str, dut_text: str, ctl_dut: str) -> None:
    L = [f"* netlist snapshot for CMRR Monte Carlo record {record} (issue #61)",
         "* ---- committed testbench (sim/cmrr-mc/testbench/tb_cmrr_mc.spice) ----"]
    L += [f"* | {ln}" for ln in tb_text.splitlines()]
    L += [f"* ---- DUT include (wrapper-normalised export), sha256 {_sha(dut_text)[:16]} ----"]
    L += [f"* | {ln}" for ln in dut_text.splitlines()]
    L += [f"* ---- negative-control DUT copy (XM3 W changed), sha256 {_sha(ctl_dut)[:16]}: the changed line ----"]
    L += [f"* | {ln}" for ln in ctl_dut.splitlines() if ln.lower().startswith(CONTROL_MIRROR[0].lower() + " ")]
    for tag, req in reqs.items():
        r = json.loads(json.dumps(req))
        r["netlist"] = Path(r["netlist"]).name
        r.get("models", {}).pop("pdk_root", None)
        L += [f"* ---- klt request `{tag}` ----"] + [f"* | {ln}" for ln in json.dumps(r, indent=2).splitlines()]
    path.write_text("\n".join(L) + "\n")


def fmt(x: float, nd: int = 2) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:.{nd}f}"


def build_record(*, record, stamp, pdk, ngspice, kver, grid, reports, walls, samples, stats, off_rows, off_probs,
                 proc_stats, imb_info, imb_probs, link, mc_env, dut_sha, problems_none) -> str:
    L: list[str] = []
    add = L.append
    temps, supplies = GRIDS[grid]
    npts = len(stats)
    add(f"# CMRR Monte Carlo record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; issue #61")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include "
        f"`{dut_sha[:16]}`), unchanged; snapshot `netlist-snapshots/{record}.spice`")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (harness `find_pdk`, via {pdk.source}). "
        "klt's `provenance.pdk` describes the submitting client, not the fleet runner; the fleet library is tied "
        "to the pinned revision through the switch-off control below, which reproduces the committed systematic "
        f"record `{SYSTEMATIC_RECORD}`.")
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`")
    add("- **Execution**: `klt sim` Monte Carlo requests (one ngspice process per sample, both excitations in it):")
    for tag in reports:
        add(exec_line(tag, reports[tag], walls[tag]))
    add(f"- **Population**: process {', '.join(CORNERS)}; T {', '.join(f'{t:g} C' for t in temps)}; "
        f"VDD {', '.join(f'{v:.2f} V' for v in supplies)} (VCM = VDD/2); ibias = 10 uA (ideal); CL = 2 pF; "
        f"`monte_carlo = {{n: {MC_N}, seed: {MC_SEED}, vary: \"{MC_VARY}\"}}` per grid point -> "
        f"{npts} x {MC_N} = {npts * MC_N} samples. MOS mismatch only (`sw_stat_mismatch=1`; `sw_stat_global` "
        "untouched, global spread is the five corners); RZ/CC passives at typical, no passive mismatch.")
    if grid == "nominal":
        add("- **Temperature and supply are NOT sampled under mismatch** in this record (issue #61's stated "
            "population, the same as `sim/offset-mc/`). The systematic 45-point grid "
            f"(`sim/cmrr/records/{SYSTEMATIC_RECORD}.md`) covers T and VDD for matched devices only.")
    add(f"- **environment.monte_carlo** (grid report): `{json.dumps(mc_env, sort_keys=True)[:400]}`")
    add("")
    add("## Claim")
    add("")
    add("**Measured, no pass or fail verdict.** This record carries the mismatch-inclusive CMRR statistics; "
        "any bound is a decision record's to propose and an operator's to ratify.")
    add("")
    add("## Statistics per grid point")
    add("")
    add("CMRR = 20 log10 |Ad / Acm| per sample (dB). Two 3-sigma statistics are reported side by side:")
    add("")
    add("- **linear 3-sigma** `CMRR_3s,lin = mean(Ad dB) - 20 log10(mean|Acm| + 3 sigma|Acm|)`: the CMRR of "
        "the +3-sigma common-mode gain, in the linear |Acm| domain (the definition the `sg13g2-opamp` twin "
        "ratified in its DR-0004).")
    add("- **dB 3-sigma** `CMRR_3s,dB = mean - 3 sigma` of the per-sample CMRR in dB (issue #61's proposal).")
    add("- Empirical minimum, 1st and 5th percentiles (linear interpolation) as the normality cross-check: "
        f"N = {MC_N} cannot resolve a 0.135 % tail, so neither 3-sigma figure is a sample statistic.")
    add("")
    for f in FIGS:
        add(f"### {FIG_LABEL[f]}")
        add("")
        add("| point | N | mean (dB) | sigma (dB) | skew | min | p1 | p5 | **lin 3s** | dB 3s | mean \\|Acm\\| (V/V) | sigma \\|Acm\\| | mean Ad (dB) |")
        add("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for k, st in stats.items():
            s = st[f]
            lb = " (>=)" if s.lower_bounds else ""
            add(f"| {G.fmt_key(k)} | {s.n} | {fmt(s.mean_db)} | {fmt(s.sigma_db)} | {s.skew_db:+.2f} | {fmt(s.min_db)} | "
                f"{fmt(s.p1_db)} | {fmt(s.p5_db)} | **{fmt(s.lin_3s)}**{lb} | {fmt(s.db_3s)} | {s.acm_mean:.4g} | "
                f"{s.acm_sigma:.4g} | {fmt(s.ad_mean_db)} |")
        add("")
    add("## Worst point per figure")
    add("")
    add("| figure | lin 3s worst (dB) | at | dB 3s worst (dB) | at | sample min (dB) | at | p5 worst (dB) |")
    add("|---|---|---|---|---|---|---|---|")
    for f in FIGS:
        a, ka = worst(stats, f, "lin_3s")
        b, kb = worst(stats, f, "db_3s")
        m, km = worst(stats, f, "min_db")
        p, _ = worst(stats, f, "p5_db")
        add(f"| {FIG_LABEL[f]} | **{fmt(a)}** | {G.fmt_key(ka)} | {fmt(b)} | {G.fmt_key(kb)} | {fmt(m)} | "
            f"{G.fmt_key(km)} | {fmt(p)} |")
    add("")
    fus = [s.vals["cmc_fu_hz"] for ss in samples.values() for s in ss if s.valid]
    add(f"- f_u over all samples: {min(fus) / 1e6:.3f} .. {max(fus) / 1e6:.3f} MHz.")
    nlb = sum(s.lower_bound for ss in samples.values() for s in ss)
    add(f"- Samples with |Acm| below the numerical floor ({FLOOR:.3g} V/V, clamped, figure a lower bound): {nlb}.")
    acm_re = [s.vals["cmc_acm_re0"] for ss in samples.values() for s in ss if s.valid]
    npos = sum(x > 0 for x in acm_re)
    add(f"- Sign of Acm at 0.1 Hz (real part): positive in {npos}/{len(acm_re)} samples. A sign change "
        "means the mismatch term can cancel the systematic term, which is why the dB image is skewed toward "
        "high CMRR and why the linear-domain statistic is the conservative one.")
    add("")
    add("## Offset (the DC operating point of the same samples)")
    add("")
    add("| point | offset mean (mV) | offset sigma (mV) |")
    add("|---|---|---|")
    for k, ss in samples.items():
        xs = [s.vals["cmc_offset"] for s in ss if s.valid]
        add(f"| {G.fmt_key(k)} | {statistics.fmean(xs) * 1e3:+.3f} | {statistics.stdev(xs) * 1e3:.3f} |")
    add("")
    if link.get("available"):
        add(f"- Link to `sim/offset-mc/` record `{OFFSET_MC_RECORD}` (same corners, N and seed): {link['n']} "
            f"nominal samples compared; {link['same_seed']} carry the same klt per-sample seed; of those, "
            f"{link['within_10uv']} reproduce the committed offset within 10 uV (max |dev| "
            f"{link['max_dev_v'] * 1e6:.3f} uV). Information only, not a criterion.")
        add("")
    add("## Method")
    add("")
    add(f"- One ngspice process per sample (`.ac {AC_ARGS}`, 201 points): the testbench's dm drive "
        "(vinp +0.5 V, vinn -0.5 V AC) -> `op` -> `alter vcm acmag=1`, `alter vnac acmag=1` -> the same `.ac` "
        "with the cm drive (vinp = vinn = 1 V AC) -> `op`. The circuit is the systematic bench's "
        "(`sim/cmrr/testbench/tb_cmrr.spice`, line-for-line; guarded), with `sw_stat_mismatch` on its `.param` line.")
    add("- From the actual phasors of both sweeps, vd = v(vinp) - v(vinn), vc = (v(vinp) + v(vinn))/2, and "
        "`vout = Ad vd + Acm vc` is solved exactly per frequency (the systematic driver's solve, in the ngspice "
        "control language). The scalars are printed into each sample's log and read from it.")
    add("- **DC** = mean CMRR (dB) over 0.1-1 Hz (21 points); |Acm| and Ad at DC are band means. Spot values are "
        "exact grid samples (1 kHz, 10 kHz, 100 kHz, 1 MHz are sweep points; checked per sample). **f_u** = first "
        "falling 0 dB crossing of |Ad| by ngspice `meas` (linear interpolation in f; the systematic driver "
        "interpolates in log f -- the switch-off control bounds the difference).")
    add("- **Per-sample validity (blocking)**: every scalar present and finite; sweep grid as expected; dm "
        f"excitation vd = 1, vc = 0 and cm excitation vc = 1 to {EXC_TOL:g} V, residual |vd/vc| <= {CM_LEAK_MAX:g}; "
        f"**dm/cm parity**: the DC output after the two sweeps agrees to {PARITY_TOL_V * 1e6:g} uV (the output "
        "carries the sampled offset, so a re-drawn device set would differ by mV); vinp = VCM; |vout - VCM| <= "
        f"{OP_VOUT_TOL_V} V; Ad, CMRR and |Acm| flat over 0.1-1 Hz to {PLATEAU_TOL_DB} dB; f_u inside the sweep.")
    add(f"- **Population validity (blocking)**: every grid point contributes exactly N = {MC_N} distinct sample "
        f"indices, all valid; offset sigma > {MIN_OFFSET_SIGMA_V * 1e3:g} mV at every point (mismatch visibly "
        "acts; klt's `family_mismatch` reports `active: null` for an `.include`d DUT, 2AMLogic/klayout-tools#2928).")
    add("")
    add("## Controls")
    add("")
    add(f"**Switch-off** (`sw_stat_mismatch=0`, {CONTROL_OFF_N} samples per corner, 27 C / 3.30 V): spread must "
        f"be exactly zero, and the values must reproduce the committed systematic record `{SYSTEMATIC_RECORD}` "
        f"(DC/spots within {TOL_SYS_DB} dB on identical grid samples; f_u within {TOL_SYS_FU_REL * 100:g} % and CMRR "
        f"at f_u within {TOL_SYS_FU_DB} dB, the interpolation difference). Result: "
        f"{'**PASS**' if not off_probs else '**FAIL**: ' + '; '.join(off_probs)}")
    add("")
    add("| point | DC | 1 kHz | 10 kHz | 100 kHz | 1 MHz | at f_u | f_u (MHz) | systematic DC / at f_u | max dev DC..1M (dB) | dev at f_u (dB) |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in off_rows:
        v, ref, dv = r["vals"], r["ref"] or {}, r["dev"]
        mx = max((dv.get(f, float("nan")) for f in FIGS[:-1]), default=float("nan"))
        add(f"| {G.fmt_key(r['key'])} | " + " | ".join(fmt(v[f], 3) for f in FIGS) + f" | {r['fu'] / 1e6:.3f} | "
            f"{fmt(ref.get('dc', float('nan')))} / {fmt(ref.get('fu', float('nan')))} | {mx:.4f} | "
            f"{dv.get('fu', float('nan')):.3f} |")
    add("")
    if proc_stats is not None:
        st, osig = proc_stats
        add(f"**Process-only** (`vary: \"process\"`, typical, {CONTROL_PROC_N} samples, mismatch switch on): "
            f"CMRR DC sigma {st.sigma_db:.3f} dB, offset sigma {osig * 1e3:.3f} mV. Recorded as measured, not a "
            "criterion: klt feeds ngspice one combined seed that changes with the process seed, so the RNG-drawn "
            "mismatch still varies (the `sim/offset-mc/` finding, 2AMLogic/klayout-tools#2937).")
        add("")
    if imb_info:
        b, c = imb_info["base"], imb_info["ctl"]
        add(f"**Negative control: mirror imbalance** (XM3 W 6u -> 5.4u in a DUT copy, same mismatch Monte Carlo, "
            f"typical / 27 C / 3.30 V, N = {c.n}). Criterion: mean |Acm| at DC rises by more than "
            f"{CONTROL_SE_MULT:g} standard errors and both 3-sigma statistics fall. Result: "
            f"{'**PASS**' if not imb_probs else '**FAIL**: ' + '; '.join(imb_probs)}")
        add("")
        add("| DUT | N | mean \\|Acm\\| (V/V) | sigma \\|Acm\\| | CMRR mean (dB) | lin 3s (dB) | dB 3s (dB) | min (dB) |")
        add("|---|---|---|---|---|---|---|---|")
        for name, s in (("committed (grid, typical)", b), ("XM3 -10 %", c)):
            add(f"| {name} | {s.n} | {s.acm_mean:.4g} | {s.acm_sigma:.4g} | {fmt(s.mean_db)} | {fmt(s.lin_3s)} | "
                f"{fmt(s.db_3s)} | {fmt(s.min_db)} |")
        add(f"- shift of mean |Acm|: {imb_info['shift']:+.4g} V/V; standard error of the difference {imb_info['se']:.3g}.")
        add("")
    add("## Validation summary")
    add("")
    add(f"- All blocking checks passed: {problems_none}.")
    add("")
    add("## Reproduce / evidence")
    add("")
    add("```")
    add(f"python3 sim/cmrr-mc/run_cmrr_mc.py --grid {grid} --batch-runner-version-check warn")
    add("```")
    add("")
    add(f"- Per-sample scalars (all three klt seeds, offset, parity, every figure): `corners/{record}/samples.csv`; "
        f"controls: `corners/{record}/controls/*.csv`; sanitised klt reports `corners/{record}/klt-report.*.json`.")
    add("- Testbench `sim/cmrr-mc/testbench/tb_cmrr_mc.spice`; driver `sim/cmrr-mc/run_cmrr_mc.py`; tests "
        "`sim/cmrr-mc/test_cmrr_mc.py`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #61)")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--smoke", action="store_true", help="one local unit at the nominal point (mismatch on, no MC)")
    ap.add_argument("--probe", action="store_true", help=f"N={PROBE_N} AC+monte_carlo capability probe at typical")
    ap.add_argument("--probe-expr", action="store_true",
                    help="probe: also declare one `expr` measurement (the klt 0.5.0 fleet runner rejects it)")
    ap.add_argument("--grid", choices=sorted(GRIDS), default="nominal")
    ap.add_argument("--backend", default=None, help="klt backend (default: klt's own, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0)
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--keep-work", type=Path, default=None, help="keep the work dir (never commit it)")


def smoke(pdk: Pdk) -> int:
    with tempfile.TemporaryDirectory(prefix="cmrr-mc-smoke-") as scratch:
        work = Path(scratch)
        tb = materialise(work, pdk)
        req = request(tb, pdk, ["typical"], [27.0], [3.30], n=None, local=True)
        rep = run_klt(req, work / "out", "local", work, env=C.local_env(work))
        samples, probs = extract(_as_mc(rep), [("typical", 27.0, 3.30)], 1)
    s = samples[("typical", 27.0, 3.30)][0]
    if probs:
        print("SMOKE TEST FAILED: " + "; ".join(probs))
        return 1
    print(f"  offset {s.vals['cmc_offset'] * 1e3:+.3f} mV, parity {s.vals['cmc_op_parity'] * 1e6:.3f} uV; CMRR DC "
          f"{s.fig('cmrr', 'dc'):.2f} dB, 10 kHz {s.fig('cmrr', '10k'):.2f} dB, at f_u {s.fig('cmrr', 'fu'):.2f} dB")
    print("smoke test OK")
    return 0


def _as_mc(rep: dict) -> dict:
    """A non-MC single-unit report, dressed as sample 0 (smoke test only)."""
    for c in rep.get("corners", []):
        if not c.get("monte_carlo"):
            c["monte_carlo"] = {"sample_index": 0}
    return rep


def probe(pdk: Pdk, args) -> int:
    """N=3 AC + monte_carlo capability probe (issue #61 step (a)): writes a
    small append-only probe record."""
    record, stamp = allocate_record_id(REPO_ROOT)
    base = HERE / "probes"
    rec_path = base / f"{record}.md"
    data_path = base / f"{record}.json"
    for p in (rec_path, data_path):
        if p.exists():
            raise FileExistsError(f"{p} exists; evidence is append-only")
    kver = klt_version()
    want = [("typical", 27.0, 3.30)]
    with tempfile.TemporaryDirectory(prefix="cmrr-mc-probe-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "probe", pdk)
        try:
            rep, req, wall = submit("probe", tb, pdk, ["typical"], [27.0], [3.30], PROBE_N, args, work,
                                    expr=("cmc_cmrr_dc",) if args.probe_expr else ())
        except KltError as exc:
            print(f"ERROR: probe request could not be run; nothing recorded.\n{exc}", file=sys.stderr)
            return 2
        samples, probs = extract(rep, want, PROBE_N)
        ss = samples[want[0]]
        expr_vals = []
        for c in rep.get("corners", []):
            m = {x.get("name"): x for x in c.get("measurements", [])}.get("cmrr_dc_x", {})
            expr_vals.append({"sample_index": (c.get("monte_carlo") or {}).get("sample_index"),
                              "value": m.get("value"), "status": m.get("status")})
        mc_env = (rep.get("environment") or {}).get("monte_carlo")
        san = sanitise_report(rep)
    base.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(san, indent=1, sort_keys=True) + "\n")
    distinct = len({round(s.fig("cmrr", "dc"), 4) for s in ss if s.valid})
    evals = [e["value"] for e in expr_vals if isinstance(e["value"], (int, float))]
    L = [f"# CMRR Monte Carlo capability probe `{record}`", "",
         f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; issue #61 step (a); klt `{kver}`; PDK open_pdks `{pdk.version}`",
         "- **Request**: one `klt sim` request, `analysis.kind: \"ac\"` with the two-sweep control tail "
         f"(`run_cmrr_mc.analysis_tail`), typical / 27 C / 3.30 V, `monte_carlo = {{n: {PROBE_N}, seed: {MC_SEED}, "
         f"vary: \"{MC_VARY}\"}}`; " + ("one `expr` measurement (`cmrr_dc_x` = `cmc_cmrr_dc`)." if args.probe_expr
                                    else "no `measurements[]` (the scalars are `print`ed by the tail and read from "
                                    "each sample's retained log; the fleet runner rejects `expr`, see probe "
                                    "`20261009-233828-95dfc2a`)."),
         exec_line("probe", rep, wall).replace("  - ", "- **Execution**: ", 1),
         f"- **environment.monte_carlo**: `{json.dumps(mc_env, sort_keys=True)}`", "",
         "## Result", "",
         "| sample | klt seed | offset (mV) | dm/cm parity (uV) | CMRR DC (dB) | CMRR 10 kHz (dB) | CMRR at f_u (dB) | expr `cmrr_dc_x` | valid |",
         "|---|---|---|---|---|---|---|---|---|"]
    ev = {e["sample_index"]: e for e in expr_vals}
    for s in sorted(ss, key=lambda s: s.index):
        e = ev.get(s.index, {})
        if s.vals:
            L.append(f"| {s.index} | {s.seeds.get('seed')} | {s.vals.get('cmc_offset', float('nan')) * 1e3:+.3f} | "
                     f"{s.vals.get('cmc_op_parity', float('nan')) * 1e6:.3f} | {fmt(s.vals.get('cmc_cmrr_dc', float('nan')), 3)} | "
                     f"{fmt(s.vals.get('cmc_cmrr_10k', float('nan')), 3)} | {fmt(s.vals.get('cmc_cmrr_fu', float('nan')), 3)} | "
                     f"{e.get('value')} ({e.get('status')}) | {s.valid} |")
        else:
            L.append(f"| {s.index} | {s.seeds.get('seed')} | | | | | | {e.get('value')} ({e.get('status')}) | {s.valid} |")
    L += ["", f"- Distinct per-sample CMRR DC values: {distinct}/{PROBE_N}; `expr` values returned: {len(evals)}/{PROBE_N}.",
          f"- Problems: {'; '.join(probs) if probs else 'none'}",
          "- **Decision (issue #61 option 1/2/3)**: " + (
              "option 1 works -- both excitations of one sample run in one deck on one request; the per-sample "
              "dm/cm parity check holds, so the full driver uses this deck." if not probs and distinct == PROBE_N
              else "the one-deck form did not validate; see problems."),
          "", f"Sanitised klt report: `probes/{record}.json`.", ""]
    rec_path.write_text("\n".join(L))
    print(f"probe record written: {rec_path.relative_to(REPO_ROOT)}")
    print("\n".join(L))
    return 0 if not probs else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_args(ap)
    args = ap.parse_args(argv)
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    if args.probe:
        return probe(pdk, args)
    temps, supplies = GRIDS[args.grid]
    want = keys_of(CORNERS, temps, supplies)
    nominal = [k for k in keys_of(CORNERS, [27.0], [3.30])]
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    ngspice, kver = ngspice_version(), klt_version()
    dut = load_dut_text()
    ctl_dut = imbalance_dut()
    print(f"record {record}: {len(want)} points x N={MC_N}, PDK {pdk.path} (open_pdks {pdk.version}), klt {kver}")
    import contextlib

    if args.keep_work:
        args.keep_work.mkdir(parents=True, exist_ok=False)
        ctx = contextlib.nullcontext(str(args.keep_work.resolve()))
    else:
        ctx = tempfile.TemporaryDirectory(prefix="cmrr-mc-")
    with ctx as scratch:
        work = Path(scratch)
        reports, reqs, walls = {}, {}, {}
        try:
            tb = materialise(work / "grid", pdk)
            reports["grid"], reqs["grid"], walls["grid"] = submit("grid", tb, pdk, CORNERS, temps, supplies, MC_N, args, work)
            tb0 = materialise(work / "switch-off", pdk, mismatch=0)
            reports["switch-off"], reqs["switch-off"], walls["switch-off"] = submit(
                "switch-off", tb0, pdk, CORNERS, [27.0], [3.30], CONTROL_OFF_N, args, work)
            tbp = materialise(work / "process-only", pdk)
            reports["process-only"], reqs["process-only"], walls["process-only"] = submit(
                "process-only", tbp, pdk, ["typical"], [27.0], [3.30], CONTROL_PROC_N, args, work, vary="process")
            tbi = materialise(work / "imbalance", pdk, dut_text=ctl_dut, allow_missing=(CONTROL_MIRROR[0].lower(),))
            reports["imbalance"], reqs["imbalance"], walls["imbalance"] = submit(
                "imbalance", tbi, pdk, ["typical"], [27.0], [3.30], CONTROL_IMB_N, args, work)
        except KltError as exc:
            print(f"ERROR: a Monte Carlo request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        problems: list[str] = []
        samples, p = extract(reports["grid"], want, MC_N)
        problems += p
        for k, ss in samples.items():
            sg = offset_sigma(ss)
            if not sg > MIN_OFFSET_SIGMA_V:
                problems.append(f"{G.fmt_key(k)}: offset sigma {sg * 1e3:.3f} mV -- mismatch not acting")
        off_s, p = extract(reports["switch-off"], nominal, CONTROL_OFF_N)
        problems += [f"[switch-off] {x}" for x in p]
        off_probs, off_rows = check_switch_off(off_s, systematic_reference(nominal))
        problems += off_probs
        proc_s, p = extract(reports["process-only"], [("typical", 27.0, 3.30)], CONTROL_PROC_N)
        problems += [f"[process-only] {x}" for x in p]
        imb_s, p = extract(reports["imbalance"], [("typical", 27.0, 3.30)], CONTROL_IMB_N)
        problems += [f"[imbalance] {x}" for x in p]
        if problems:
            print("VALIDATION FAILED; NO RECORD WRITTEN:\n  " + "\n  ".join(problems[:60]), file=sys.stderr)
            if len(problems) > 60:
                print(f"  ... {len(problems) - 60} more", file=sys.stderr)
            return 2
        stats = point_stats(samples)
        imb_probs, imb_info = check_imbalance(samples[("typical", 27.0, 3.30)], imb_s[("typical", 27.0, 3.30)])
        if imb_probs:
            print("NEGATIVE CONTROL FAILED; NO RECORD WRITTEN:\n  " + "\n  ".join(imb_probs), file=sys.stderr)
            return 2
        ps = proc_s[("typical", 27.0, 3.30)]
        proc_stats = (fig_stats(ps, "dc"), offset_sigma(ps))
        link = offset_mc_link(samples)
        mc_env = (reports["grid"].get("environment") or {}).get("monte_carlo")

        cdir = paths["corners"]
        (cdir / "controls").mkdir(parents=True)
        write_csv(cdir / "samples.csv", samples)
        write_csv(cdir / "controls" / "switch-off.csv", off_s)
        write_csv(cdir / "controls" / "process-only.csv", proc_s)
        write_csv(cdir / "controls" / "imbalance.csv", imb_s)
        for tag, rep in reports.items():
            (cdir / f"klt-report.{tag}.json").write_text(json.dumps(sanitise_report(rep), indent=1, sort_keys=True) + "\n")
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        write_snapshot(paths["snapshot"], record, reqs, TESTBENCH.read_text(), dut, ctl_dut)
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, grid=args.grid, reports=reports,
            walls=walls, samples=samples, stats=stats, off_rows=off_rows, off_probs=off_probs, proc_stats=proc_stats,
            imb_info=imb_info, imb_probs=imb_probs, link=link, mc_env=mc_env, dut_sha=_sha(dut),
            problems_none="yes"))
    print(f"record written: {paths['record'].relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
