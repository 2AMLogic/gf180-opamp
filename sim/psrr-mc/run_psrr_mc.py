#!/usr/bin/env python3
"""Mismatch-aware PSRR+ / PSRR- of the committed sized schematic: Monte Carlo
over per-instance MOS mismatch (issue #99; follow-up of #61 / DR-6).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_psrr_mc.spice`) declares no transistor; its circuit
is line-for-line the systematic PSRR bench (`sim/psrr/testbench/tb_psrr.spice`)
with the mismatch switch added to its single `.param` line.

Why one deck per sample
-----------------------
The systematic driver (`sim/psrr/run_psrr.py`) gets Ad, Avdd and Avss from
THREE `klt sim` requests. Under Monte Carlo the three excitations must see the
SAME sampled devices, or PSRR mixes different amplifiers. Here all three run
in ONE ngspice process on one parsed circuit (the technique of
`sim/cmrr-mc/`): the driver appends control commands to the `.ac` analysis
line, which

    ac (dm) -> op -> alter to the vdd drive -> ac (vdd) -> op
            -> alter to the vss drive -> ac (vss) -> op

and computes, per frequency and from the ACTUAL phasors, Ad = vout / vd of the
dm sweep and Asupply = vout / v(rail) of each supply sweep (the systematic
driver's definitions, written in the ngspice control language), then prints
the scalar figures into the log. Each sample proves that the three sweeps saw
the same devices: the DC output printed after each sweep (it carries the
sampled offset) must agree to 1 uV.

What it runs
------------
ONE `klt sim` request: the five MOS corners at 27 C / 3.30 V with
`monte_carlo = {n: 300, seed: 45, vary: "mismatch"}` -- the population of
`sim/offset-mc/` and `sim/cmrr-mc/`. Which backend executes it is klt's
decision (`--backend`, `$KLT_SIM_BACKEND`); on a dispatch worker that is the
Spot batch fleet. This script never launches ngspice itself, never loops
ngspice over a grid and never falls back to a local grid when a batch submit
fails: it stops with the error and writes no record.

Controls (small requests, same deck):
  * switch-off: `sw_stat_mismatch = 0`, every corner, a few samples: the
    spread must be exactly zero and the values must reproduce the committed
    systematic record (`sim/psrr/records/20261010-163854-0127eb7.md`).
  * process-only: `vary: "process"` at typical, recorded as measured (klt
    feeds ngspice one combined seed, 2AMLogic/klayout-tools#2937).
  * imbalance (negative control): the mirror-imbalance DUT copy (XM3 W 6u ->
    5.4u) under the same mismatch Monte Carlo at typical: mean |Asupply| at
    DC must rise by more than 3 standard errors and the linear 3-sigma PSRR
    must fall, for each rail in IMBALANCE_RAILS (README: why).

    python3 sim/psrr-mc/run_psrr_mc.py --smoke     # one local unit, no record
    python3 sim/psrr-mc/run_psrr_mc.py --probe     # N=3 AC+MC capability probe (batch)
    python3 sim/psrr-mc/run_psrr_mc.py             # full run + record

No bound is judged here: the DR-6 bounds are compared, not graded.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import math
import re
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
    extract_mc_samples,
    find_pdk,
    klt_version,
    load_dut_text,
    load_sibling,
    ngspice_version,
    percentile,
    remote_of,
    run_klt,
    run_klt_retrying,
    sanitise_report,
    skew,
)

# The systematic PSRR driver owns the bench guard (driven vss, ground-
# referenced stimuli) and, through the CMRR driver, the materialisation, the
# mirror-imbalance mutation and the gain driver's request shape; reuse them.
P = load_sibling("psrr_driver", "sim/psrr/run_psrr.py")
C = P.C
G = C.G
# One source for everything the fingerprint covers (issue #89).
mc = load_sibling("psrr_mc_measurement_config", "sim/psrr-mc/measurement_config.py")

TESTBENCH = HERE / "testbench" / "tb_psrr_mc.spice"
SYSTEMATIC_TB = REPO_ROOT / "sim" / "psrr" / "testbench" / "tb_psrr.spice"
#: The committed (post-#42 retune) systematic record the switch-off control
#: must reproduce. The 20261009-105929-30ec86d record predates the retune.
SYSTEMATIC_RECORD = "20261010-163854-0127eb7"
SYSTEMATIC_DIR = REPO_ROOT / "sim" / "psrr"
#: The committed CMRR Monte Carlo (same DUT, corners, N, seed): per-sample link.
CMRR_MC_RECORD = "20261010-202454-e4be1ac"
CMRR_MC_CSV = REPO_ROOT / "sim" / "cmrr-mc" / "corners" / CMRR_MC_RECORD / "samples.csv"

# --------------------------------------------------------------------------
# Conditions (from measurement_config.py)
# --------------------------------------------------------------------------

CORNERS = list(mc.CORNERS)
TEMPS = [float(mc.TEMP_C)]
SUPPLIES = [float(mc.VDD_V)]
NOMINAL = ("typical", float(mc.TEMP_C), float(mc.VDD_V))
MC_N, MC_SEED, MC_VARY = mc.MC_N, mc.MC_SEED, mc.MC_VARY
PROBE_N = 3
CONTROL_OFF_N, CONTROL_PROC_N, CONTROL_IMB_N = mc.CONTROL_OFF_N, mc.CONTROL_PROC_N, mc.CONTROL_IMB_N
CONTROL_MIRROR = tuple(mc.CONTROL_MIRROR)

AC_ARGS = mc.ac_args()
N_FREQ = C.N_FREQ
BAND = tuple(mc.BAND)
SPOTS = dict(mc.SPOTS)
FIGS = tuple(mc.FIGS)
RAILS = tuple(mc.RAILS)
LABEL = {"vdd": "PSRR+", "vss": "PSRR-"}
ALABEL = {"vdd": "Avdd", "vss": "Avss"}
FIG_LABEL = {"dc": "DC plateau (0.1-1 Hz)", "1k": "1 kHz", "10k": "10 kHz", "100k": "100 kHz",
             "1m": "1 MHz", "fu": "at f_u (differential unity gain)"}
#: DR-6 proposed bounds (dB), the same for PSRR+ and PSRR-. Compared, never graded.
DR6_BOUNDS = {"dc": 60.0, "1k": 60.0, "10k": 50.0}
FLOOR = mc.FLOOR
SCALARS = mc.scalars()

#: Per-sample validity (blocking): the systematic drivers' tolerances.
EXC_TOL = C.EXC_TOL
INPUT_QUIET_V = P.INPUT_QUIET_V
INPUT_LEAK_FRAC = P.INPUT_LEAK_FRAC
OP_VOUT_TOL_V = C.OP_VOUT_TOL_V
#: All three sweeps of one sample must sit at the same DC operating point.
PARITY_TOL_V = 1e-6
PLATEAU_TOL_DB = C.PLATEAU_TOL_DB
#: Mismatch must visibly act: per-corner offset sigma above this.
MIN_OFFSET_SIGMA_V = 1e-3
#: Slack of the knee monotonicity test, per band step, relative. ngspice's
#: printed precision rounds a flat band to relative steps of order 1e-6; the
#: smallest true in-band knee that can reach the classifier (band spread
#: above PLATEAU_TOL_DB, i.e. corner below ~7 Hz) rises by at least ~1.4e-4
#: per step.
KNEE_STEP_SLACK = 1e-5
#: Switch-off control vs the committed systematic record (same point).
TOL_SYS_DB = 0.01
TOL_SYS_FU_REL = 5e-3
TOL_SYS_FU_DB = 0.1
#: Negative control: mean |Asupply| at DC rises by more than this many
#: standard errors of the difference, and the linear 3-sigma PSRR falls.
CONTROL_SE_MULT = 3.0
#: Rails on which the XM3 -10 % imbalance must degrade the statistic. The
#: matched-device study (README "Negative control") shows the imbalance adds a
#: systematic Asupply term of the SAME sign as the matched one on both rails
#: at typical / 27 C / 3.30 V, so |Avdd| and |Avss| both rise.
IMBALANCE_RAILS = ("vdd", "vss")

# --------------------------------------------------------------------------
# Testbench guard and materialisation
# --------------------------------------------------------------------------

PARAM_KEYS = ["acp", "acn", "acdd", "acss", "rsv", "csv", "sw_stat_mismatch"]


def _circuit_lines(text: str) -> list[str]:
    return [ln for ln in C.code_lines(text) if not ln.lower().startswith(".param")]


def guard_testbench(text: str) -> list[str]:
    """Reasons the bench no longer is the systematic PSRR bench + the switch.

    The systematic PSRR guards (servo, CL, Ibias, driven vss, AC-driven
    rails, ground-referenced stimuli, no hand-declared device, circuit body),
    plus: every circuit line is identical to
    `sim/psrr/testbench/tb_psrr.spice`; the one `.param` line carries exactly
    the PSRR keys + sw_stat_mismatch and comes after the design.ngspice
    include; sw_stat_global is never touched.
    """
    errs = P.guard_testbench(text)
    if _circuit_lines(text) != _circuit_lines(SYSTEMATIC_TB.read_text()):
        errs.append("circuit differs from sim/psrr/testbench/tb_psrr.spice (only the .param line may differ)")
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


def deck_params(mismatch: int = 1) -> dict:
    return {**mc.DM_PARAMS, **C.SERVO_NOMINAL, "sw_stat_mismatch": int(mismatch)}


def materialise(work: Path, pdk: Pdk, *, mismatch: int = 1, dut_text: str | None = None,
                allow_missing: tuple[str, ...] = ()) -> Path:
    return C.materialise(work, pdk, deck_params(mismatch), testbench=TESTBENCH, guard=guard_testbench,
                         dut_text=dut_text, allow_missing=allow_missing)


def imbalance_dut(mirror: tuple = CONTROL_MIRROR) -> str:
    inst, prm, old, new = mirror
    return C.mutate_instance(load_dut_text(), inst, prm, old, new)


# --------------------------------------------------------------------------
# klt requests
# --------------------------------------------------------------------------


def request(netlist: Path, pdk: Pdk, corners, temps, supplies, *, n: int | None, seed: int = MC_SEED,
            vary: str = MC_VARY, local: bool = False) -> dict:
    """The systematic bench's request shape (gain driver's corner bundles,
    vcm = VDD/2), the three-sweep tail appended to the analysis line, no
    `measurements` (the klt 0.5.0 fleet runner rejects `expr`; the scalars are
    printed by the tail and read from each sample's retained log), no
    rawfile, logs retained."""
    req = G.ac_request(netlist, pdk, corners, temps, supplies)
    args = req["analysis"]["args"]
    if args != AC_ARGS:
        raise RuntimeError(f"gain request sweep `{args}` differs from the configured `{AC_ARGS}`")
    req["analysis"]["args"] = args + "\n" + mc.analysis_tail(args)
    req["measurements"] = []
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
_SCALAR_RE = re.compile(rf"^\s*(pmc_\w+)\s*=\s*({_NUM})\s*$", re.M | re.I)


def parse_scalars(log: str) -> dict[str, float]:
    """`pmc_* = value` lines of an ngspice log (last occurrence wins, so the
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
    lower_bound: set = field(default_factory=set)  # rails whose |Asupply| hit the floor

    @property
    def valid(self) -> bool:
        return not self.problems

    def psrr(self, rail: str, f: str) -> float:
        return self.vals[f"pmc_psrr_{rail}_{f}"]

    def asup(self, rail: str, f: str) -> float:
        return self.vals[f"pmc_asup_{rail}_{f}"]

    def ad(self, f: str) -> float:
        return self.vals[f"pmc_ad_{f}"]

    def knee_rails(self) -> set:
        return knee_rails(self.vals)

    def dc_figure(self, rail: str) -> tuple[float, float]:
        """(PSRR, |Asupply|) at DC. A plateau sample reports the band means
        (the systematic record's definition); a knee sample the 0.1 Hz edge --
        the f->0 asymptote to within (1 + (0.1 Hz / f_c)^2)^(1/2) (<= 0.15 dB
        for any knee that reaches the classifier). The band mean would
        misstate a deep knee by up to ~10 dB."""
        if rail in self.knee_rails():
            a0 = self.vals[f"pmc_{rail}_aband{BAND[0]}"]
            return self.vals["pmc_ad_b0"] - 20 * math.log10(a0), a0
        return self.psrr(rail, "dc"), self.asup(rail, "dc")


def validate_sample(vals: dict[str, float], vcm: float) -> tuple[list[str], set]:
    """Blocking problems of one sample (empty = valid) and the rails whose
    |Asupply| fell below the numerical floor somewhere in the sweep."""
    p: list[str] = []
    miss = [s for s in SCALARS if s not in vals or not math.isfinite(vals[s])]
    if miss:
        return [f"missing/non-finite scalars: {', '.join(miss[:4])}{'...' if len(miss) > 4 else ''}"], set()
    if int(round(vals["pmc_npts"])) != N_FREQ:
        p.append(f"{vals['pmc_npts']:g} frequency points, expected {N_FREQ}")
    want = {"pmc_f0": C.AC_FSTART, f"pmc_f{BAND[1]}": C.AC_FSTART * 10 ** (BAND[1] / C.AC_PPD),
            **{f"pmc_f{i}": f for f, i in SPOTS.values()}}
    for k, f in want.items():
        if abs(vals[k] / f - 1) > 1e-6:
            p.append(f"{k} = {vals[k]:g} Hz, expected {f:g} Hz (sweep grid changed)")
    if vals["pmc_dm_vderr"] > EXC_TOL or vals["pmc_dm_vcerr"] > EXC_TOL:
        p.append(f"differential excitation off (|vd-1| {vals['pmc_dm_vderr']:.3g}, |vc| {vals['pmc_dm_vcerr']:.3g} V)")
    if vals["pmc_dm_railq"] > EXC_TOL:
        p.append(f"[dm] rails not AC-quiet ({vals['pmc_dm_railq']:.3g} V)")
    for r in RAILS:
        if vals[f"pmc_{r}_err"] > EXC_TOL or vals[f"pmc_{r}_oth"] > EXC_TOL:
            p.append(f"[{r}] rail excitation off (driven rail |v-1| {vals[f'pmc_{r}_err']:.3g} V, other rail "
                     f"{vals[f'pmc_{r}_oth']:.3g} V): wrong rail driven or other rail not quiet")
        if vals[f"pmc_{r}_inq"] > INPUT_QUIET_V:
            p.append(f"[{r}] inputs not AC-quiet ({vals[f'pmc_{r}_inq']:.3g} V > {INPUT_QUIET_V:g})")
        if vals[f"pmc_{r}_leak"] > INPUT_LEAK_FRAC:
            p.append(f"[{r}] input leak contributes {vals[f'pmc_{r}_leak']:.3g} of the output (> {INPUT_LEAK_FRAC:g})")
        if abs(vals[f"pmc_par_{r}"]) > PARITY_TOL_V:
            p.append(f"dm/{r} DC operating points differ by {vals[f'pmc_par_{r}'] * 1e6:.3f} uV "
                     "(the sweeps did not see the same devices)")
        if classify_rail_dc(vals, r) == "invalid":
            p.append(f"no {LABEL[r]}/|{ALABEL[r]}| plateau and not a near-cancellation knee "
                     f"({vals[f'pmc_{r}_spread']:.3f} / {vals[f'pmc_{r}_aspread']:.3f} dB over 0.1-1 Hz; "
                     "the |Asupply| band profile is not monotonically non-decreasing)")
    if abs(vals["pmc_op1_vinp"] - vcm) > 1e-6:
        p.append(f"vinp = {vals['pmc_op1_vinp']:.6g} V, expected VCM {vcm:g} V")
    if abs(vals["pmc_op1_vout"] - vcm) > OP_VOUT_TOL_V:
        p.append(f"vout {vals['pmc_op1_vout']:.4f} V is more than {OP_VOUT_TOL_V} V from VCM")
    if vals["pmc_ad_spread"] > PLATEAU_TOL_DB:
        p.append(f"no Ad plateau ({vals['pmc_ad_spread']:.3f} dB over 0.1-1 Hz)")
    if not (C.AC_FSTART < vals["pmc_fu_hz"] < C.AC_FSTOP):
        p.append(f"f_u {vals['pmc_fu_hz']:g} Hz outside the sweep")
    lb = {r for r in RAILS if vals[f"pmc_{r}_amin"] < FLOOR}
    return p, lb


def clamp_floor(s: Sample) -> None:
    """Defensive twin of the deck's clamp: a printed |Asupply| below the floor
    is clamped and PSRR re-derived from it (a lower bound); never a division
    by zero."""
    for r in RAILS:
        for f in FIGS:
            a = s.vals[f"pmc_asup_{r}_{f}"]
            if a < FLOOR:
                s.vals[f"pmc_asup_{r}_{f}"] = FLOOR
                s.vals[f"pmc_psrr_{r}_{f}"] = s.vals[f"pmc_ad_{f}"] - 20 * math.log10(FLOOR)
                s.lower_bound.add(r)


def band_profile(vals: dict[str, float], rail: str) -> list[float]:
    """The clamped linear |Asupply| band profile, 0.1 Hz .. 1 Hz (21 points)."""
    return [vals[f"pmc_{rail}_aband{i}"] for i in range(BAND[0], BAND[1] + 1)]


def classify_rail_dc(vals: dict[str, float], rail: str) -> str:
    """The DC band (0.1-1 Hz) of one rail: 'plateau' (flat within
    PLATEAU_TOL_DB), 'knee' (non-flat but monotonically non-decreasing --
    the near-cancellation signature), or 'invalid'.

    A mismatch draw whose supply term cancels the systematic one leaves the
    residual to the reactive path through the DUT's dominant pole,
    |Asupply| ~ sqrt(Re^2 + (k f)^2): flat below the corner f_c = |Re|/k,
    rising toward +20 dB/dec above it. A draw with f_c inside or near the
    band therefore has NO flat 0.1-1 Hz plateau, by physics, while every
    bench-quality check passes; its profile identifies it: rising,
    single-knee, minimum at the 0.1 Hz edge (diagnosed on job
    klt-sim-ba5466061590: 7.7 dB monotone rise, |Asupply| 1.3e7 x the floor).
    Anything else non-flat -- falling or humped, no mechanism in this bench --
    is a defect, not a knee.
    """
    if (vals[f"pmc_{rail}_spread"] <= PLATEAU_TOL_DB
            and vals[f"pmc_{rail}_aspread"] <= PLATEAU_TOL_DB):
        return "plateau"
    prof = band_profile(vals, rail)
    steps = [b / a for a, b in zip(prof, prof[1:])]
    if steps and min(steps) >= 1.0 - KNEE_STEP_SLACK:
        return "knee"
    return "invalid"


def knee_rails(vals: dict[str, float]) -> set:
    """Rails whose DC band is a near-cancellation knee (valid, but non-flat)."""
    if not vals:
        return set()
    return {r for r in RAILS if classify_rail_dc(vals, r) == "knee"}


def extract(report: dict, want: list[tuple], n_expected: int, *, read_log=None) -> tuple[dict[tuple, list[Sample]], list[str]]:
    """Samples per grid point from a klt Monte Carlo report (shared logic: harness.extract_mc_samples)."""
    return extract_mc_samples(
        report, want, n_expected, point_key=G.point_key, fmt_key=G.fmt_key, sample_cls=Sample,
        parse_scalars=parse_scalars, validate_sample=validate_sample, clamp_floor=clamp_floor, read_log=read_log,
    )


# --------------------------------------------------------------------------
# Statistics (pure; unit-tested offline)
# --------------------------------------------------------------------------


@dataclass
class FigStats:
    n: int
    mean_db: float
    sigma_db: float
    min_db: float
    p1_db: float
    p5_db: float
    db_3s: float  # mean - 3 sigma of PSRR (dB)
    a_mean: float  # |Asupply| (V/V)
    a_sigma: float
    ad_mean_db: float
    lin_3s: float  # mean Ad (dB) - 20 log10(mean |Asupply| + 3 sigma |Asupply|)
    skew_db: float
    lower_bounds: int
    knees: int  # DC only: samples whose DC figure is the knee asymptote


def fig_stats(samples: list[Sample], rail: str, f: str) -> FigStats:
    good = [s for s in samples if s.valid]
    if len(good) < 2:
        raise ValueError("need at least two valid samples")
    if f == "dc":
        pairs = [s.dc_figure(rail) for s in good]
        c = [x for x, _ in pairs]
        a = [x for _, x in pairs]
        knees = sum(rail in s.knee_rails() for s in good)
    else:
        c = [s.psrr(rail, f) for s in good]
        a = [s.asup(rail, f) for s in good]
        knees = 0
    d = [s.ad(f) for s in good]
    mean, sig = statistics.fmean(c), statistics.stdev(c)
    am, asg = statistics.fmean(a), statistics.stdev(a)
    adm = statistics.fmean(d)
    return FigStats(
        n=len(good), mean_db=mean, sigma_db=sig, min_db=min(c), p1_db=percentile(c, 1), p5_db=percentile(c, 5),
        db_3s=mean - 3 * sig, a_mean=am, a_sigma=asg, ad_mean_db=adm,
        lin_3s=adm - 20 * math.log10(am + 3 * asg), skew_db=skew(c),
        lower_bounds=sum(rail in s.lower_bound for s in good),
        knees=knees,
    )


def point_stats(samples: dict[tuple, list[Sample]]) -> dict[tuple, dict[str, dict[str, FigStats]]]:
    return {k: {r: {f: fig_stats(v, r, f) for f in FIGS} for r in RAILS} for k, v in samples.items() if v}


def check_offset_sigma(samples: dict) -> list[str]:
    """Blocking gate: at every point the sampled offset sigma must exceed MIN_OFFSET_SIGMA_V."""
    probs = []
    for k, ss in samples.items():
        sg = offset_sigma(ss)
        if not sg > MIN_OFFSET_SIGMA_V:
            probs.append(f"{G.fmt_key(k)}: offset sigma {sg * 1e3:.3f} mV -- mismatch not acting")
    return probs


def offset_sigma(samples: list[Sample]) -> float:
    xs = [s.vals["pmc_offset"] for s in samples if s.valid]
    return statistics.stdev(xs) if len(xs) >= 2 else float("nan")


def worst(stats: dict, rail: str, f: str, attr: str) -> tuple[float, tuple]:
    k = min(stats, key=lambda k: getattr(stats[k][rail][f], attr))
    return getattr(stats[k][rail][f], attr), k


def sign_counts(samples: list[Sample], rail: str) -> tuple[int, int]:
    """(positive, total) real part of Asupply at 0.1 Hz over valid samples."""
    xs = [s.vals[f"pmc_{rail}_re0"] for s in samples if s.valid]
    return sum(x > 0 for x in xs), len(xs)


def dr6_rows(stats: dict) -> list[dict]:
    """Per rail and DR-6 frequency: the worst corner of each statistic beside
    the proposed bound. A comparison, not a verdict."""
    rows = []
    for r in RAILS:
        for f, bound in DR6_BOUNDS.items():
            lin, kl = worst(stats, r, f, "lin_3s")
            dbs, kd = worst(stats, r, f, "db_3s")
            mn, km = worst(stats, r, f, "min_db")
            rows.append({"rail": r, "fig": f, "bound": bound, "lin_3s": lin, "lin_at": kl, "db_3s": dbs,
                         "db_at": kd, "min": mn, "min_at": km,
                         "below": [n for n, v in (("lin 3s", lin), ("dB 3s", dbs), ("min", mn)) if v < bound]})
    return rows


# --------------------------------------------------------------------------
# Controls (pure checks; unit-tested offline)
# --------------------------------------------------------------------------


def check_switch_off(samples: dict[tuple, list[Sample]], reference: dict) -> tuple[list[str], list[dict]]:
    """Mismatch off: zero spread, and agreement with the systematic record."""
    probs: list[str] = []
    rows: list[dict] = []
    for k, ss in samples.items():
        good = [s for s in ss if s.valid]
        if len(good) != len(ss) or not good:
            probs.append(f"switch-off {G.fmt_key(k)}: invalid samples")
            continue
        spread = max(max(s.psrr(r, f) for s in good) - min(s.psrr(r, f) for s in good) for r in RAILS for f in FIGS)
        off_spread = max(s.vals["pmc_offset"] for s in good) - min(s.vals["pmc_offset"] for s in good)
        if spread != 0.0 or off_spread != 0.0:
            probs.append(f"switch-off {G.fmt_key(k)}: samples differ (PSRR spread {spread:.3g} dB, "
                         f"offset spread {off_spread:.3g} V); something other than mismatch varies")
        s0 = good[0]
        ref = reference.get(k)
        row = {"key": k, "vals": {r: {f: s0.psrr(r, f) for f in FIGS} for r in RAILS}, "fu": s0.vals["pmc_fu_hz"],
               "re0": {r: s0.vals[f"pmc_{r}_re0"] for r in RAILS}, "ref": ref, "dev": {r: {} for r in RAILS}}
        if ref is None:
            probs.append(f"switch-off {G.fmt_key(k)}: no committed systematic reference")
        else:
            for r in RAILS:
                for f in FIGS:
                    dev = abs(s0.psrr(r, f) - ref[r][f])
                    row["dev"][r][f] = dev
                    tol = TOL_SYS_FU_DB if f == "fu" else TOL_SYS_DB
                    if dev > tol:
                        probs.append(f"switch-off {G.fmt_key(k)}: {LABEL[r]} {f} {s0.psrr(r, f):.4f} dB vs systematic "
                                     f"{ref[r][f]:.4f} dB (|dev| {dev:.4f} > {tol} dB)")
            rel = abs(s0.vals["pmc_fu_hz"] / ref["fu_hz"] - 1)
            row["fu_rel"] = rel
            if rel > TOL_SYS_FU_REL:
                probs.append(f"switch-off {G.fmt_key(k)}: f_u {s0.vals['pmc_fu_hz']:.6g} Hz vs systematic "
                             f"{ref['fu_hz']:.6g} Hz (rel {rel:.2e} > {TOL_SYS_FU_REL})")
        rows.append(row)
    return probs, rows


def check_imbalance(base: list[Sample], ctl: list[Sample], rails=IMBALANCE_RAILS) -> tuple[list[str], dict]:
    """The deliberately unbalanced mirror must raise mean |Asupply| at DC by
    more than CONTROL_SE_MULT standard errors and lower the linear 3-sigma
    PSRR, on every rail in `rails`."""
    probs: list[str] = []
    info: dict = {}
    for r in RAILS:
        b, c = fig_stats(base, r, "dc"), fig_stats(ctl, r, "dc")
        se = math.sqrt(b.a_sigma**2 / b.n + c.a_sigma**2 / c.n)
        shift = c.a_mean - b.a_mean
        info[r] = {"base": b, "ctl": c, "se": se, "shift": shift, "criterion": r in rails}
        if r not in rails:
            continue
        if not shift > CONTROL_SE_MULT * se:
            probs.append(f"imbalance raised mean |{ALABEL[r]}| by {shift:.4g} V/V, not > {CONTROL_SE_MULT:g} SE ({se:.3g})")
        if not c.lin_3s < b.lin_3s:
            probs.append(f"imbalance did not lower the linear 3-sigma {LABEL[r]} ({c.lin_3s:.2f} vs {b.lin_3s:.2f} dB)")
    return probs, info


def _section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def systematic_reference(keys: list[tuple]) -> dict:
    """PSRR figures of the committed systematic record at `keys`: DC and spots
    recomputed from its committed `<point>.psrr.dat` curves (exact grid
    samples), f_u and PSRR at f_u read from its per-point tables."""
    cdir = SYSTEMATIC_DIR / "corners" / SYSTEMATIC_RECORD
    rec = (SYSTEMATIC_DIR / "records" / f"{SYSTEMATIC_RECORD}.md").read_text()
    sections = {r: _section(rec, f"{LABEL[r]} at all 45 points (dB, input-referred)") for r in RAILS}
    out: dict = {}
    for k in keys:
        p = cdir / f"{G.point_stem(k)}.psrr.dat"
        if not p.is_file():
            continue
        rows = [ln.split() for ln in p.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if len(rows) != N_FREQ:
            continue
        ref: dict = {}
        ok = True
        for r, col in (("vdd", 4), ("vss", 5)):
            cv = [float(x[col]) for x in rows]
            d = {"dc": statistics.fmean(cv[BAND[0]: BAND[1] + 1])}
            for name, (_, i) in SPOTS.items():
                d[name] = cv[i]
            m = re.search(rf"^\| {re.escape(G.fmt_key(k))} \|([^\n]*)$", sections[r], re.M)
            if not m:
                ok = False
                break
            cells = [x.strip() for x in m.group(1).split("|")]
            d["fu"] = float(cells[5].lstrip(">= "))
            ref["fu_hz"] = float(cells[6]) * 1e6
            ref[r] = d
        if ok:
            out[k] = ref
    return out


def cmrr_mc_link(samples: dict[tuple, list[Sample]]) -> dict:
    """Per-sample comparison with the committed CMRR Monte Carlo (same DUT,
    corners, N and seed): if klt hands each sample the same seed and ngspice
    draws the same devices, the DC offsets coincide. Information only."""
    if not CMRR_MC_CSV.is_file():
        return {"available": False}
    ref: dict[tuple[str, int], dict] = {}
    with CMRR_MC_CSV.open() as fh:
        for r in csv.DictReader(fh):
            if float(r["temperature_c"]) == 27.0 and abs(float(r["vdd_v"]) - 3.30) < 1e-9:
                ref[(r["process"], int(r["sample_index"]))] = r
    n = same_seed = 0
    devs: list[float] = []
    for k, ss in samples.items():
        for s in ss:
            r = ref.get((k[0], s.index))
            if r is None or not s.valid:
                continue
            n += 1
            if str(s.seeds.get("seed")) == r["rndseed"]:
                same_seed += 1
                devs.append(abs(s.vals["pmc_offset"] - float(r["cmc_offset"])))
    return {"available": True, "n": n, "same_seed": same_seed,
            "max_dev_v": max(devs) if devs else float("nan"),
            "within_1uv": sum(d <= 1e-6 for d in devs)}


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def submit(tag: str, tb: Path, pdk: Pdk, corners, temps, supplies, n: int, args, work: Path, *,
           vary: str = MC_VARY) -> tuple[dict, dict, float]:
    req = request(tb, pdk, corners, temps, supplies, n=n, vary=vary)
    req["batch"] = batch_block(args)
    units = len(corners) * len(temps) * len(supplies) * n
    print(f"  submitting `{tag}` ({units} units)...", flush=True)
    t0 = time.time()
    rep = run_klt_retrying(req, work / tag / "out", args.backend, work / tag, retries=args.batch_submit_retries,
                           wait_s=args.batch_retry_wait_s)
    wall = time.time() - t0
    rem = remote_of(rep)
    print(f"  `{tag}` done in {wall:.0f} s (job {rem.get('job_id', 'local')})", flush=True)
    return rep, req, wall


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
            "valid", "lower_bound_rails", "knee_rails"] + [s for s in SCALARS if not re.match(r"pmc_f\d+$", s)]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for k, ss in samples.items():
            for s in sorted(ss, key=lambda s: (s.index is None, s.index)):
                row = [k[0], f"{k[1]:g}", f"{k[2]:.2f}", s.index, s.seeds.get("seed"), s.seeds.get("mismatch_seed"),
                       s.seeds.get("process_seed"), int(s.valid), "+".join(sorted(s.lower_bound)),
                       "+".join(sorted(s.knee_rails()))]
                row += [repr(s.vals.get(c)) if c in s.vals else "" for c in cols[10:]]
                w.writerow(row)


def write_snapshot(path: Path, record: str, reqs: dict[str, dict], tb_text: str, dut_text: str, ctl_dut: str) -> None:
    L = [f"* netlist snapshot for PSRR Monte Carlo record {record} (issue #99)",
         "* ---- committed testbench (sim/psrr-mc/testbench/tb_psrr_mc.spice) ----"]
    L += [f"* | {ln}" for ln in tb_text.splitlines()]
    L += [f"* ---- DUT include (wrapper-normalised export), sha256 {_sha(dut_text)[:16]} ----"]
    L += [f"* | {ln}" for ln in dut_text.splitlines()]
    L += [(f"* ---- negative-control DUT copy ({CONTROL_MIRROR[0]} {CONTROL_MIRROR[1]} changed), sha256 "
           f"{_sha(ctl_dut)[:16]}: the changed line ----")]
    L += [f"* | {ln}" for ln in ctl_dut.splitlines() if ln.lower().startswith(CONTROL_MIRROR[0].lower() + " ")]
    for tag, req in reqs.items():
        r = json.loads(json.dumps(req))
        r["netlist"] = Path(r["netlist"]).name
        r.get("models", {}).pop("pdk_root", None)
        L += [f"* ---- klt request `{tag}` ----"] + [f"* | {ln}" for ln in json.dumps(r, indent=2).splitlines()]
    path.write_text("\n".join(L) + "\n")


def fmt(x: float, nd: int = 2) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:.{nd}f}"


def build_record(*, record, stamp, pdk, ngspice, kver, reports, walls, samples, stats, off_rows, off_probs,
                 proc_stats, imb_info, imb_probs, link, mc_env, dut_sha, problems_none) -> str:
    L: list[str] = []
    add = L.append
    npts = len(stats)
    add(f"# PSRR Monte Carlo record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; issue #99 (follow-up of #61 / DR-6)")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include "
        f"`{dut_sha[:16]}`), unchanged; snapshot `netlist-snapshots/{record}.spice`")
    for ln in mc.fingerprint_lines({"bench": TESTBENCH.read_text()}):
        add(ln)
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (harness `find_pdk`, via {pdk.source}). "
        "klt's `provenance.pdk` describes the submitting client, not the fleet runner; the fleet library is tied "
        "to the pinned revision through the switch-off control below, which reproduces the committed systematic "
        f"record `{SYSTEMATIC_RECORD}`.")
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`")
    add("- **Execution**: `klt sim` Monte Carlo requests (one ngspice process per sample, all three excitations in it):")
    for tag in reports:
        add(exec_line(tag, reports[tag], walls[tag]))
    add(f"- **Population**: process {', '.join(CORNERS)}; T {mc.TEMP_C:g} C; VDD {mc.VDD_V:.2f} V (VCM = VDD/2); "
        f"ibias = 10 uA (ideal); CL = 2 pF; `monte_carlo = {{n: {MC_N}, seed: {MC_SEED}, vary: \"{MC_VARY}\"}}` "
        f"per grid point -> {npts} x {MC_N} = {npts * MC_N} samples. MOS mismatch only (`sw_stat_mismatch=1`; "
        "`sw_stat_global` untouched, global spread is the five corners); RZ/CC passives at typical, no passive "
        "mismatch. The same corners, N and seed as `sim/offset-mc/` and `sim/cmrr-mc/`.")
    add("- **Temperature and supply are NOT sampled under mismatch** in this record (issue #99's stated population). "
        f"The systematic 45-point grid (`sim/psrr/records/{SYSTEMATIC_RECORD}.md`) covers T and VDD for matched "
        "devices only; its four Avss sign-flip points are all at 125 C.")
    add(f"- **environment.monte_carlo** (grid report): `{json.dumps(mc_env, sort_keys=True)[:400]}`")
    add("")
    add("## Claim")
    add("")
    add("**Measured, no pass or fail verdict.** This record carries the mismatch-inclusive PSRR+ and PSRR- "
        "statistics and sets them beside the DR-6 proposed bounds; it neither grades nor alters DR-6 or "
        "`spec/target-spec.md`. Bound ratification is an operator's.")
    add("")
    add("**Definitions and references** (as the systematic record): input-referred PSRR+/- = 20 log10 |Ad / Asupply|, "
        "Asupply = vout / v(rail) with one rail driven (1 V AC), the other rail and both inputs AC-quiet; inputs, "
        "servo, CL and Vdd referenced to simulator ground, the DUT's `vss` port a driven node; the ideal 10 uA bias "
        "source carries no small-signal current, so **bias-generator supply rejection is excluded**.")
    add("")
    add("## Comparison with the DR-6 proposed bounds")
    add("")
    add("DR-6 (`spec/decision-records/0006-cmrr-psrr-bounds.md`, proposed, not ratified) proposes PSRR+ and PSRR- "
        ">= 60 dB at the DC plateau and 1 kHz and >= 50 dB at 10 kHz on systematic evidence. Worst corner of each "
        "mismatch statistic (27 C / 3.30 V only):")
    add("")
    add("| figure | frequency | DR-6 bound (dB) | **lin 3s** worst (dB) | at | dB 3s worst (dB) | at | sample min (dB) | at | statistics below the bound |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    rows = dr6_rows(stats)
    for r in rows:
        add(f"| {LABEL[r['rail']]} | {FIG_LABEL[r['fig']]} | {r['bound']:g} | **{fmt(r['lin_3s'])}** | "
            f"{G.fmt_key(r['lin_at'])} | {fmt(r['db_3s'])} | {G.fmt_key(r['db_at'])} | {fmt(r['min'])} | "
            f"{G.fmt_key(r['min_at'])} | {', '.join(r['below']) if r['below'] else 'none'} |")
    add("")
    below = [r for r in rows if r["below"]]
    if below:
        add("- **Below a DR-6 bound**: " + "; ".join(
            f"{LABEL[r['rail']]} {FIG_LABEL[r['fig']]} ({', '.join(r['below'])} < {r['bound']:g} dB)" for r in below)
            + ". Reported, not graded; DR-6 is not edited here.")
    else:
        add("- Every reported statistic (linear 3-sigma, dB 3-sigma, empirical minimum) is at or above the DR-6 "
            "proposed bound at every corner. Reported, not graded.")
    add("- 27 C / 3.30 V only: the PVT claim of a bound remains the systematic 45-point grid; the mismatch penalty "
        "at other T/VDD (in particular the 125 C Avss sign-flip points) is not measured here.")
    add("")
    add("## Statistics per grid point")
    add("")
    add("PSRR = 20 log10 |Ad / Asupply| per sample (dB). Two 3-sigma statistics are reported side by side:")
    add("")
    add("- **linear 3-sigma** `PSRR_3s,lin = mean(Ad dB) - 20 log10(mean|Asupply| + 3 sigma|Asupply|)`: the PSRR "
        "of the +3-sigma supply gain in the linear |Asupply| domain (the analogue of `sim/cmrr-mc/`'s CMRR figure).")
    add("- **dB 3-sigma** `mean - 3 sigma` of the per-sample PSRR in dB, shown for comparison.")
    add(f"- Empirical minimum, 1st and 5th percentiles as the normality cross-check: N = {MC_N} cannot resolve a "
        "0.135 % tail, so neither 3-sigma figure is a sample statistic.")
    add("")
    for r in RAILS:
        for f in FIGS:
            add(f"### {LABEL[r]} {FIG_LABEL[f]}")
            add("")
            kn_head = " | knee DCs" if f == "dc" else ""
            add(f"| point | N | mean (dB) | sigma (dB) | skew | min | p1 | p5 | **lin 3s** | dB 3s | "
                f"mean \\|{ALABEL[r]}\\| (V/V) | sigma \\|{ALABEL[r]}\\| | mean Ad (dB) |{kn_head} |")
            add("|---|---|---|---|---|---|---|---|---|---|---|---|---|" + ("---|" if f == "dc" else ""))
            for k, st in stats.items():
                s = st[r][f]
                lb = " (>=)" if s.lower_bounds else ""
                kn = f" | {s.knees}" if f == "dc" else ""
                add(f"| {G.fmt_key(k)} | {s.n} | {fmt(s.mean_db)} | {fmt(s.sigma_db)} | {s.skew_db:+.2f} | "
                    f"{fmt(s.min_db)} | {fmt(s.p1_db)} | {fmt(s.p5_db)} | **{fmt(s.lin_3s)}**{lb} | {fmt(s.db_3s)} | "
                    f"{s.a_mean:.4g} | {s.a_sigma:.4g} | {fmt(s.ad_mean_db)} |{kn} |")
            if f == "dc" and any(st[r][f].knees for st in stats.values()):
                add("")
                add(f"knee DCs: samples of this point whose {LABEL[r]} DC band is a near-cancellation knee "
                    "(non-flat but monotonically rising; their DC figure is the 0.1 Hz asymptote, listed in "
                    "\"Numerical floor and near-cancellation\").")
            add("")
    add("## Worst point per figure")
    add("")
    add("| figure | lin 3s worst (dB) | at | dB 3s worst (dB) | at | sample min (dB) | at | p5 worst (dB) |")
    add("|---|---|---|---|---|---|---|---|")
    for r in RAILS:
        for f in FIGS:
            a, ka = worst(stats, r, f, "lin_3s")
            b, kb = worst(stats, r, f, "db_3s")
            m, km = worst(stats, r, f, "min_db")
            p, _ = worst(stats, r, f, "p5_db")
            add(f"| {LABEL[r]} {FIG_LABEL[f]} | **{fmt(a)}** | {G.fmt_key(ka)} | {fmt(b)} | {G.fmt_key(kb)} | "
                f"{fmt(m)} | {G.fmt_key(km)} | {fmt(p)} |")
    add("")
    fus = [s.vals["pmc_fu_hz"] for ss in samples.values() for s in ss if s.valid]
    add(f"- f_u over all samples: {min(fus) / 1e6:.3f} .. {max(fus) / 1e6:.3f} MHz.")
    add("")
    add("## Numerical floor and near-cancellation")
    add("")
    for r in RAILS:
        nlb = sum(r in s.lower_bound for ss in samples.values() for s in ss)
        amin = min(s.vals[f"pmc_{r}_amin"] for ss in samples.values() for s in ss if s.valid)
        add(f"- {LABEL[r]}: samples with |{ALABEL[r]}| below the numerical floor ({FLOOR:.3g} V/V, clamped, figure a "
            f"lower bound): **{nlb}**; smallest |{ALABEL[r]}| anywhere in any sweep {amin:.3g} V/V.")
    add("")
    knees = [(k, s, r) for k, ss in samples.items() for s in ss if s.valid for r in sorted(s.knee_rails())]
    if knees:
        add(f"### Near-cancellation knees ({len(knees)} sample rails)")
        add("")
        add("A draw whose supply term cancels the systematic one leaves the residual to the reactive path "
            "through the DUT's dominant pole: |Asupply| ~ sqrt(Re^2 + (k f)^2), flat below f_c = |Re|/k, rising "
            "toward +20 dB/dec above it. With f_c inside or near 0.1-1 Hz the band has NO plateau (no fixed dB "
            "tolerance covers it: f_c = 2.4 Hz alone gives ~1 dB) while every bench-quality check passes and the "
            "figure sits orders of magnitude above the numerical floor. The DC band is then accepted as a "
            "**near-cancellation knee**: the band profile must be monotonically non-decreasing (rising, "
            "single-knee, minimum at the 0.1 Hz edge); anything falling or humped is a defect, not a knee. The "
            "knee sample's DC figure is the 0.1 Hz asymptote, not the band mean. Diagnosed after the first "
            "full-grid attempt (PR #151) was blocked by its 0.1 dB plateau gate on 4 such draws and wrote no "
            "record; the 900-draw diagnostic (job `klt-sim-ba5466061590`) showed the profile: 7.7 dB monotone "
            "rise at |Avdd| = 1.9e-3 V/V, 1.3e7 x the floor.")
        add("")
        add("| point | sample | rail | band spread (dB) | \\|Asupply\\| at 0.1 Hz (V/V) | PSRR at 0.1 Hz (dB) |")
        add("|---|---|---|---|---|---|")
        for k, s, r in knees:
            a0 = s.vals[f"pmc_{r}_aband{BAND[0]}"]
            add(f"| {G.fmt_key(k)} | {s.index} | {LABEL[r]} | {s.vals[f'pmc_{r}_aspread']:.3f} | {a0:.4g} | "
                f"{s.vals['pmc_ad_b0'] - 20 * math.log10(a0):.2f} |")
        add("")
    else:
        add("- Near-cancellation knees: none (every sample's DC band is flat within "
            f"{PLATEAU_TOL_DB} dB).")
        add("")
    sysign = {row["key"]: row["re0"] for row in off_rows}
    add("| point | Avdd > 0 at 0.1 Hz | matched-device sign | Avss > 0 at 0.1 Hz | matched-device sign | samples with Avss sign opposite to matched |")
    add("|---|---|---|---|---|---|")
    for k, ss in samples.items():
        pv, nv = sign_counts(ss, "vdd")
        ps, ns = sign_counts(ss, "vss")
        ref = sysign.get(k, {})
        sv = ref.get("vdd", float("nan"))
        sn = ref.get("vss", float("nan"))
        opp = (ns - ps) if sn > 0 else ps if sn < 0 else 0
        add(f"| {G.fmt_key(k)} | {pv}/{nv} | {'+' if sv > 0 else '-'} | {ps}/{ns} | {'+' if sn > 0 else '-'} | {opp} |")
    add("")
    add("- A sample whose Asupply plateau sign is opposite to the matched device's has its mismatch term larger than, "
        "and cancelling through, the systematic term; near that crossing the dB figure is high and not margin. The "
        "linear 3-sigma statistic is the conservative one for exactly this reason (the dB image is skewed toward "
        "high PSRR).")
    add("")
    add("## Offset (the DC operating point of the same samples)")
    add("")
    add("| point | offset mean (mV) | offset sigma (mV) |")
    add("|---|---|---|")
    for k, ss in samples.items():
        xs = [s.vals["pmc_offset"] for s in ss if s.valid]
        add(f"| {G.fmt_key(k)} | {statistics.fmean(xs) * 1e3:+.3f} | {statistics.stdev(xs) * 1e3:.3f} |")
    add("")
    if link.get("available"):
        add(f"- Link to `sim/cmrr-mc/` record `{CMRR_MC_RECORD}` (same DUT, corners, N and seed): {link['n']} samples "
            f"compared; {link['same_seed']} carry the same klt per-sample seed; of those, {link['within_1uv']} reproduce "
            f"its DC offset within 1 uV (max |dev| {link['max_dev_v'] * 1e6:.3f} uV). Information only, not a criterion.")
        add("")
    add("## Method")
    add("")
    add(f"- One ngspice process per sample (`.ac {AC_ARGS}`, {N_FREQ} points): the testbench's dm drive (vinp +0.5 V, "
        "vinn -0.5 V AC, rails quiet) -> `op` -> `alter vcm acmag=0`, `alter vnac acmag=0`, `alter vdd acmag=1` -> "
        "the same `.ac` with 1 V AC on vdd -> `op` -> `alter vdd acmag=0`, `alter vss acmag=1` -> the same `.ac` "
        "with 1 V AC on the DUT's vss -> `op`. The circuit is the systematic bench's "
        "(`sim/psrr/testbench/tb_psrr.spice`, line-for-line; guarded), with `sw_stat_mismatch` on its `.param` line.")
    add("- Ad = v(vout) / (v(vinp) - v(vinn)) of the dm sweep and Asupply = v(vout) / v(rail) of each supply sweep, "
        "from the actual phasors, per frequency (the systematic driver's definitions, in the ngspice control "
        f"language). |Asupply| below {FLOOR:.3g} V/V is clamped elementwise in the deck. The scalars are printed "
        "into each sample's log and read from it.")
    add(f"- **DC** = the low-frequency plateau value per sample: the band mean over 0.1-1 Hz (21 points) for a "
        f"flat band (the systematic record's definition); for a near-cancellation knee (non-flat band, "
        f"monotonically rising profile -- see \"Numerical floor and near-cancellation\") the 0.1 Hz value, the "
        "f->0 asymptote. |Asupply| and Ad at DC follow the same rule. Spot values are exact grid samples "
        "(1 kHz, 10 kHz, 100 kHz, 1 MHz; checked per sample). **f_u** = first falling 0 dB crossing of |Ad| by "
        "ngspice `meas` (linear interpolation in f; the systematic driver interpolates in log f -- the "
        "switch-off control bounds the difference).")
    add(f"- **Per-sample validity (blocking)**: every scalar present and finite; sweep grid as expected; dm "
        f"excitation vd = 1, vc = 0 and rails quiet to {EXC_TOL:g} V; each supply sweep: driven rail = 1 V and the "
        f"other rail quiet to {EXC_TOL:g} V, both inputs quiet to {INPUT_QUIET_V:g} V, input-leak term |Ad vd| below "
        f"{INPUT_LEAK_FRAC:g} of the output; **dm/vdd/vss parity**: the DC output after each supply sweep agrees "
        f"with the one after the dm sweep to {PARITY_TOL_V * 1e6:g} uV (the output carries the sampled offset, so a "
        f"re-drawn device set would differ by mV); vinp = VCM; |vout - VCM| <= {OP_VOUT_TOL_V} V; each rail's DC "
        f"band flat over 0.1-1 Hz to {PLATEAU_TOL_DB} dB or a near-cancellation knee (monotonically non-decreasing "
        "band profile; blocking in the invalid direction); f_u inside the sweep.")
    add(f"- **Population validity (blocking)**: every grid point contributes exactly N = {MC_N} distinct sample "
        f"indices, all valid; offset sigma > {MIN_OFFSET_SIGMA_V * 1e3:g} mV at every point (mismatch visibly acts; "
        "klt's `family_mismatch` reports `active: null` for an `.include`d DUT, 2AMLogic/klayout-tools#2928).")
    add("")
    add("## Controls")
    add("")
    add(f"**Switch-off** (`sw_stat_mismatch=0`, {CONTROL_OFF_N} samples per corner, 27 C / 3.30 V): spread must be "
        f"exactly zero, and PSRR+ and PSRR- must reproduce the committed systematic record `{SYSTEMATIC_RECORD}` "
        f"(DC/spots within {TOL_SYS_DB} dB on identical grid samples; f_u within {TOL_SYS_FU_REL * 100:g} % and PSRR at "
        f"f_u within {TOL_SYS_FU_DB} dB, the interpolation difference). Result: "
        f"{'**PASS**' if not off_probs else '**FAIL**: ' + '; '.join(off_probs)}")
    add("")
    add("| point | figure | DC | 1 kHz | 10 kHz | 100 kHz | 1 MHz | at f_u | systematic DC / at f_u | max dev DC..1M (dB) | dev at f_u (dB) | f_u (MHz) |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in off_rows:
        ref = row["ref"] or {}
        for r in RAILS:
            v, dv, rr = row["vals"][r], row["dev"][r], ref.get(r, {})
            mx = max((dv.get(f, float("nan")) for f in FIGS[:-1]), default=float("nan"))
            add(f"| {G.fmt_key(row['key'])} | {LABEL[r]} | " + " | ".join(fmt(v[f], 3) for f in FIGS)
                + f" | {fmt(rr.get('dc', float('nan')))} / {fmt(rr.get('fu', float('nan')))} | {mx:.4f} | "
                f"{dv.get('fu', float('nan')):.3f} | {row['fu'] / 1e6:.3f} |")
    add("")
    if proc_stats is not None:
        st, osig = proc_stats
        add(f"**Process-only** (`vary: \"process\"`, typical, {CONTROL_PROC_N} samples, mismatch switch on): "
            + ", ".join(f"{LABEL[r]} DC sigma {st[r].sigma_db:.3f} dB" for r in RAILS)
            + f", offset sigma {osig * 1e3:.3f} mV. Recorded as measured, not a criterion: klt feeds ngspice one "
            "combined seed that changes with the process seed, so the RNG-drawn mismatch still varies "
            "(2AMLogic/klayout-tools#2937).")
        add("")
    if imb_info:
        inst, prm, old, new = CONTROL_MIRROR
        add(f"**Negative control: mirror imbalance** ({inst} {prm} {old} -> {new} in a DUT copy, same mismatch Monte "
            f"Carlo, typical / 27 C / 3.30 V, N = {CONTROL_IMB_N}). Criterion on "
            f"{', '.join(LABEL[r] for r in IMBALANCE_RAILS)}: mean |Asupply| at DC rises by more than "
            f"{CONTROL_SE_MULT:g} standard errors and the linear 3-sigma PSRR falls (README: why this direction). "
            f"Result: {'**PASS**' if not imb_probs else '**FAIL**: ' + '; '.join(imb_probs)}")
        add("")
        add("| figure | DUT | N | mean \\|Asupply\\| (V/V) | sigma \\|Asupply\\| | PSRR mean (dB) | lin 3s (dB) | dB 3s (dB) | min (dB) | shift of mean \\|Asupply\\| / SE |")
        add("|---|---|---|---|---|---|---|---|---|---|")
        for r in RAILS:
            i = imb_info[r]
            for name, s in (("committed (grid, typical)", i["base"]), (f"{inst} {prm} {new}", i["ctl"])):
                extra = f"{i['shift']:+.4g} / {i['se']:.3g}" if s is i["ctl"] else ""
                add(f"| {LABEL[r]} DC | {name} | {s.n} | {s.a_mean:.4g} | {s.a_sigma:.4g} | {fmt(s.mean_db)} | "
                    f"{fmt(s.lin_3s)} | {fmt(s.db_3s)} | {fmt(s.min_db)} | {extra} |")
        add("")
    add("## Validation summary")
    add("")
    add(f"- All blocking checks passed: {problems_none}.")
    add("")
    L.extend(mc.inputs_section({"bench": TESTBENCH.read_text()}))
    add("## Reproduce / evidence")
    add("")
    add("```")
    add("python3 sim/psrr-mc/run_psrr_mc.py --batch-runner-version-check warn")
    add("```")
    add("")
    add(f"- Per-sample scalars (all three klt seeds, offset, parities, every figure): `corners/{record}/samples.csv`; "
        f"controls: `corners/{record}/controls/*.csv`; sanitised klt reports `corners/{record}/klt-report.*.json`.")
    add("- Testbench `sim/psrr-mc/testbench/tb_psrr_mc.spice`; driver `sim/psrr-mc/run_psrr_mc.py`; configuration "
        "`sim/psrr-mc/measurement_config.py`; tests `sim/psrr-mc/test_psrr_mc.py`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #99)")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--smoke", action="store_true", help="one local unit at the nominal point (mismatch on, no MC)")
    ap.add_argument("--probe", action="store_true", help=f"N={PROBE_N} AC+monte_carlo capability probe at typical")
    ap.add_argument("--backend", default=None, help="klt backend (default: klt's own, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0)
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--keep-work", type=Path, default=None, help="keep the work dir (never commit it)")


def _as_mc(rep: dict) -> dict:
    """A non-MC single-unit report, dressed as sample 0 (smoke test only)."""
    for c in rep.get("corners", []):
        if not c.get("monte_carlo"):
            c["monte_carlo"] = {"sample_index": 0}
    return rep


def local_unit(pdk: Pdk, *, mismatch: int = 1, dut_text: str | None = None,
               allow_missing: tuple[str, ...] = ()) -> tuple[Sample, list[str]]:
    """One LOCAL single unit at the nominal point (never a grid, never MC)."""
    with tempfile.TemporaryDirectory(prefix="psrr-mc-unit-") as scratch:
        work = Path(scratch)
        tb = materialise(work, pdk, mismatch=mismatch, dut_text=dut_text, allow_missing=allow_missing)
        req = request(tb, pdk, ["typical"], TEMPS, SUPPLIES, n=None, local=True)
        rep = run_klt(req, work / "out", "local", work, env=C.local_env(work))
        samples, probs = extract(_as_mc(rep), [NOMINAL], 1)
    return samples[NOMINAL][0], probs


def smoke(pdk: Pdk) -> int:
    s, probs = local_unit(pdk)
    if probs:
        print("SMOKE TEST FAILED: " + "; ".join(probs))
        return 1
    print(f"  offset {s.vals['pmc_offset'] * 1e3:+.3f} mV, parity vdd {s.vals['pmc_par_vdd'] * 1e6:.3f} uV / "
          f"vss {s.vals['pmc_par_vss'] * 1e6:.3f} uV")
    for r in RAILS:
        print(f"  {LABEL[r]}: DC {s.psrr(r, 'dc'):.2f} dB, 1 kHz {s.psrr(r, '1k'):.2f}, 10 kHz {s.psrr(r, '10k'):.2f}, "
              f"at f_u {s.psrr(r, 'fu'):.2f} dB; {ALABEL[r]}(0.1 Hz) = {s.vals[f'pmc_{r}_re0']:+.4g} "
              f"{s.vals[f'pmc_{r}_im0']:+.3g}j")
    print("smoke test OK")
    return 0


def probe(pdk: Pdk, args) -> int:
    """N=3 AC + monte_carlo capability probe of the three-sweep deck on the
    batch fleet: writes a small append-only probe record."""
    record, stamp = allocate_record_id(REPO_ROOT)
    base = HERE / "probes"
    rec_path = base / f"{record}.md"
    data_path = base / f"{record}.json"
    for p in (rec_path, data_path):
        if p.exists():
            raise FileExistsError(f"{p} exists; evidence is append-only")
    kver = klt_version()
    want = [NOMINAL]
    with tempfile.TemporaryDirectory(prefix="psrr-mc-probe-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "probe", pdk)
        try:
            rep, _req, wall = submit("probe", tb, pdk, ["typical"], TEMPS, SUPPLIES, PROBE_N, args, work)
        except KltError as exc:
            print(f"ERROR: probe request could not be run; nothing recorded.\n{exc}", file=sys.stderr)
            return 2
        samples, probs = extract(rep, want, PROBE_N)
        ss = samples[NOMINAL]
        mc_env = (rep.get("environment") or {}).get("monte_carlo")
        san = sanitise_report(rep)
    base.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(san, indent=1, sort_keys=True) + "\n")
    distinct = len({round(s.psrr("vss", "dc"), 4) for s in ss if s.valid})
    L = [f"# PSRR Monte Carlo capability probe `{record}`", "",
         f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; issue #99; klt `{kver}`; PDK open_pdks `{pdk.version}`",
         ("- **Request**: one `klt sim` request, `analysis.kind: \"ac\"` with the three-sweep control tail "
          f"(`measurement_config.analysis_tail`), typical / 27 C / 3.30 V, `monte_carlo = {{n: {PROBE_N}, seed: "
          f"{MC_SEED}, vary: \"{MC_VARY}\"}}`; no `measurements[]` (the scalars are `print`ed by the tail and read "
          "from each sample's retained log)."),
         exec_line("probe", rep, wall).replace("  - ", "- **Execution**: ", 1),
         f"- **environment.monte_carlo**: `{json.dumps(mc_env, sort_keys=True)}`", "",
         "## Result", "",
         "| sample | klt seed | offset (mV) | parity vdd / vss (uV) | PSRR+ DC | PSRR+ 10 kHz | PSRR- DC | PSRR- 10 kHz | valid |",
         "|---|---|---|---|---|---|---|---|---|"]
    for s in sorted(ss, key=lambda s: s.index):
        g = s.vals.get
        if s.vals:
            L.append(f"| {s.index} | {s.seeds.get('seed')} | {g('pmc_offset', float('nan')) * 1e3:+.3f} | "
                     f"{g('pmc_par_vdd', float('nan')) * 1e6:.3f} / {g('pmc_par_vss', float('nan')) * 1e6:.3f} | "
                     f"{fmt(g('pmc_psrr_vdd_dc', float('nan')), 3)} | {fmt(g('pmc_psrr_vdd_10k', float('nan')), 3)} | "
                     f"{fmt(g('pmc_psrr_vss_dc', float('nan')), 3)} | {fmt(g('pmc_psrr_vss_10k', float('nan')), 3)} | "
                     f"{s.valid} |")
        else:
            L.append(f"| {s.index} | {s.seeds.get('seed')} | | | | | | | {s.valid} |")
    L += ["", f"- Distinct per-sample PSRR- DC values: {distinct}/{PROBE_N}.",
          f"- Problems: {'; '.join(probs) if probs else 'none'}",
          "- **Decision**: " + (
              "the three-sweep deck works on the fleet -- all three excitations of one sample run in one deck on one "
              "request and the per-sample dm/vdd/vss parity holds, so the full driver uses this deck."
              if not probs and distinct == PROBE_N else "the one-deck form did not validate; see problems."),
          "", f"Sanitised klt report: `probes/{record}.json`.", ""]
    rec_path.write_text("\n".join(L))
    print(f"probe record written: {rec_path.relative_to(REPO_ROOT)}")
    print("\n".join(L))
    return 0 if not probs and distinct == PROBE_N else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_args(ap)
    args = ap.parse_args(argv)
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    if args.probe:
        return probe(pdk, args)
    want = G.expected_keys(CORNERS, TEMPS, SUPPLIES)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    ngspice, kver = ngspice_version(), klt_version()
    dut = load_dut_text()
    ctl_dut = imbalance_dut()
    print(f"record {record}: {len(want)} points x N={MC_N}, PDK {pdk.path} (open_pdks {pdk.version}), klt {kver}")
    if args.keep_work:
        args.keep_work.mkdir(parents=True, exist_ok=False)
        ctx = contextlib.nullcontext(str(args.keep_work.resolve()))
    else:
        ctx = tempfile.TemporaryDirectory(prefix="psrr-mc-")
    with ctx as scratch:
        work = Path(scratch)
        reports, reqs, walls = {}, {}, {}
        try:
            tb = materialise(work / "grid", pdk)
            reports["grid"], reqs["grid"], walls["grid"] = submit("grid", tb, pdk, CORNERS, TEMPS, SUPPLIES, MC_N,
                                                                  args, work)
            tb0 = materialise(work / "switch-off", pdk, mismatch=0)
            reports["switch-off"], reqs["switch-off"], walls["switch-off"] = submit(
                "switch-off", tb0, pdk, CORNERS, TEMPS, SUPPLIES, CONTROL_OFF_N, args, work)
            tbp = materialise(work / "process-only", pdk)
            reports["process-only"], reqs["process-only"], walls["process-only"] = submit(
                "process-only", tbp, pdk, ["typical"], TEMPS, SUPPLIES, CONTROL_PROC_N, args, work, vary="process")
            tbi = materialise(work / "imbalance", pdk, dut_text=ctl_dut, allow_missing=(CONTROL_MIRROR[0].lower(),))
            reports["imbalance"], reqs["imbalance"], walls["imbalance"] = submit(
                "imbalance", tbi, pdk, ["typical"], TEMPS, SUPPLIES, CONTROL_IMB_N, args, work)
        except KltError as exc:
            print(f"ERROR: a Monte Carlo request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        problems: list[str] = []
        samples, p = extract(reports["grid"], want, MC_N)
        problems += p
        problems += check_offset_sigma(samples)
        off_s, p = extract(reports["switch-off"], want, CONTROL_OFF_N)
        problems += [f"[switch-off] {x}" for x in p]
        off_probs, off_rows = check_switch_off(off_s, systematic_reference(want))
        problems += off_probs
        proc_s, p = extract(reports["process-only"], [NOMINAL], CONTROL_PROC_N)
        problems += [f"[process-only] {x}" for x in p]
        imb_s, p = extract(reports["imbalance"], [NOMINAL], CONTROL_IMB_N)
        problems += [f"[imbalance] {x}" for x in p]
        if problems:
            print("VALIDATION FAILED; NO RECORD WRITTEN:\n  " + "\n  ".join(problems[:60]), file=sys.stderr)
            if len(problems) > 60:
                print(f"  ... {len(problems) - 60} more", file=sys.stderr)
            return 2
        stats = point_stats(samples)
        imb_probs, imb_info = check_imbalance(samples[NOMINAL], imb_s[NOMINAL])
        if imb_probs:
            print("NEGATIVE CONTROL FAILED; NO RECORD WRITTEN:\n  " + "\n  ".join(imb_probs), file=sys.stderr)
            return 2
        ps = proc_s[NOMINAL]
        proc_stats = ({r: fig_stats(ps, r, "dc") for r in RAILS}, offset_sigma(ps))
        link = cmrr_mc_link(samples)
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
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, reports=reports, walls=walls,
            samples=samples, stats=stats, off_rows=off_rows, off_probs=off_probs, proc_stats=proc_stats,
            imb_info=imb_info, imb_probs=imb_probs, link=link, mc_env=mc_env, dut_sha=_sha(dut),
            problems_none="yes"))
    print(f"record written: {paths['record'].relative_to(REPO_ROOT)}")
    for r in dr6_rows(stats):
        print(f"  {LABEL[r['rail']]} {r['fig']}: lin 3s {r['lin_3s']:.2f} dB ({G.fmt_key(r['lin_at'])}), "
              f"min {r['min']:.2f} dB; DR-6 bound {r['bound']:g} dB; below: {', '.join(r['below']) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
