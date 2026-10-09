#!/usr/bin/env python3
"""Follower-biased input common-mode range (ICMR) of the committed sized
schematic across the full ratified PVT grid (issue #60; spec row proposed by
decision record 0005).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_input_common_mode.spice`) declares no transistor; it is
the CMRR bench's circuit body (buffered DC servo, CL = 2 pF, Ibias = 10 uA).

What it measures
----------------
For each of the 45 PVT combinations `process {typical, ff, ss, fs, sf} x
temperature {-40, 27, 125 C} x VDD {2.97, 3.30, 3.63 V}` the DC input common
mode VCM is SCANNED from 0 to VDD at <= 50 mV spacing (explicitly including
1.20 V and VDD/2). `klt sim`'s `corners.supply_v` pairs `vdd` and `vcm` by
index, so each request repeats every VDD for each of its VCM samples. At each
sample the bench is a follower at DC (the servo ties vinn to vout) and open loop
at every swept AC frequency; two AC excitations (differential and common mode)
are solved jointly for Ad and Acm from the ACTUAL input phasors, exactly as
`sim/cmrr/run_cmrr.py` does. The sample's gain is the minimum of Ad over the
0.1-1 Hz plateau band (the "DC-plateau gain"; AC has no literal 0 Hz sample).

A sample PASSES when ALL hold:
  * Ad plateau flat to 0.1 dB over 0.1-1 Hz and its minimum >= 60 dB;
  * every input-pair, tail, mirror and output-stage MOSFET has
    |Vds| - |Vdsat| >= -1 mV (the numerical tolerance; negative margins inside
    it are flagged, and the whole analysis is repeated with tolerance 0);
  * |vout - VCM| <= 0.10 V (follower-biased ICMR, not an arbitrary-output range
    and not an offset-accuracy claim);
  * the input-pair and tail currents are non-zero.
A sample is INVALID (never passing, never bridged) when a result is missing, a
field is non-finite, the analysis did not complete, the AC excitation is not the
intended one, the two excitations' operating points disagree by > 1 uV, or the
Ad plateau cannot be established. Missing evidence cannot yield a passing range.

Ranges: every contiguous passing interval per PVT point (adjacent valid passing
samples only), endpoints quoted on the PASSING side of each bracket with the
bracket width as uncertainty, transitions refined to <= 5 mV by additional
paired batch requests; intersection across all 45 points, and the component
containing 1.20 V. An empty intersection is a result, not a licence to weaken
the criteria.

All grids run through `klt sim` corner requests (`--backend`, the request's
`backend`, or `$KLT_SIM_BACKEND`; the Spot batch fleet on a dispatch worker).
This script never launches an ngspice grid itself and never falls back to a
local grid when a batch submit fails -- it stops with the error and writes no
record. Only single-unit controls run locally.

Evidence (append-only, a new record id every run):

    corners/<rid>/data/<process>_<T>c_<vdd>v.tar.gz   per-sample .raw/.log per excitation
    corners/<rid>/samples.csv                         one row per (point, VCM) sample
    corners/<rid>/requests/, corners/<rid>/reports/   the klt requests and sanitised reports
    corners/<rid>/controls/                           local servo / excitation controls
    netlist-snapshots/<rid>.spice                     DUT + testbench + conditions
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/input-common-mode/run_input_common_mode.py            # full scan + refinement + record
    python3 sim/input-common-mode/run_input_common_mode.py --smoke    # nominal point, local, no record
    python3 sim/input-common-mode/run_input_common_mode.py --recompute <rid>   # re-derive from retained data

Exit status: 0 when the evidence is complete and validated; 2 when a request
could not run or a control failed (no record is written).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import math
import re
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

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

# The CMRR driver owns the servo-bench guards, materialisation, the excitation
# checks and the joint Ad/Acm solve; reuse them unchanged so the benches stay
# structurally identical (it in turn reuses the gain driver's DUT guards).
if "cmrr_driver" in sys.modules:
    C = sys.modules["cmrr_driver"]
else:
    _spec = importlib.util.spec_from_file_location("cmrr_driver", REPO_ROOT / "sim" / "cmrr" / "run_cmrr.py")
    C = importlib.util.module_from_spec(_spec)
    sys.modules["cmrr_driver"] = C
    _spec.loader.exec_module(C)
G = C.G

TESTBENCH = HERE / "testbench" / "tb_input_common_mode.spice"
CMRR_DIR = REPO_ROOT / "sim" / "cmrr"

CORNERS = G.CORNERS
TEMPS_C = G.TEMPS_C
SUPPLIES_V = G.SUPPLIES_V
NOMINAL = G.NOMINAL
DEVICES = G.DEVICES  # xm1..xm7, xmb1
MODES = C.MODES
SERVO_NOMINAL = C.SERVO_NOMINAL

# --------------------------------------------------------------------------
# Axes, sweep, criteria
# --------------------------------------------------------------------------

#: Supplies and VCM are handled in integer millivolts so identities never
#: depend on float formatting.
SUPPLIES_MV = [int(round(v * 1000)) for v in SUPPLIES_V]
SCAN_STEP_MV = 50
REFINE_STEP_MV = 5
MAX_REFINE_ROUNDS = 3
TARGET_VCM_MV = 1200  # the LDO's VREF

#: Low-frequency AC grid: the shared grid's start and density, stopped at 10 Hz
#: (the plateau band is 0.1-1 Hz; the rest of the shared sweep is irrelevant to
#: a DC-plateau gain and would only multiply the cost of ~6000 units).
AC_FSTART, AC_FSTOP, AC_PPD = G.AC_FSTART, 10.0, G.AC_PPD
N_FREQ = int(round(math.log10(AC_FSTOP / AC_FSTART) * AC_PPD)) + 1
BAND_HI_HZ = 1.0  # plateau band = 0.1-1 Hz

GAIN_MIN_DB = G.GAIN_MIN_DB  # ratified bound (60 dB), reused not restated
PLATEAU_TOL_DB = C.PLATEAU_TOL_DB  # 0.10 dB
POLARITY_TOL_DEG = G.POLARITY_TOL_DEG
SAT_TOL_V = 1e-3  # numerical tolerance on |Vds| - |Vdsat| (bench-validity, not a performance bound)
VOUT_TOL_V = C.OP_VOUT_TOL_V  # the CMRR bench's OP tolerance, 0.10 V
OP_AGREE_V = C.OP_MODE_AGREE_V  # 1 uV
MIN_ID_A = 1e-9  # input-pair / tail current below this is "zero"
#: Controls.
MIDRAIL_TOL_DB = 0.05
ISOLATION_TOL_DB = C.ISOLATION_TOL_DB  # 0.01
LOCAL_VS_GRID_TOL_DB = C.TOL_LOCAL_VS_GRID_DB

GROUPS = {
    "input pair": ("xm1", "xm2"),
    "tail": ("xm5",),
    "mirror": ("xm3", "xm4"),
    "output stage": ("xm6", "xm7"),
}
CRITERIA_DEVICES = tuple(d for g in GROUPS.values() for d in g)  # xmb1 (bias reference) is recorded only
CURRENT_DEVICES = ("xm1", "xm2", "xm5")  # input pair + tail must carry current

#: Appended to `analysis.args`: klt places it verbatim in the `.control` block
#: after `ac`; it prints the DC operating point of the same deck (inputs,
#: output and every DUT MOSFET's vds, vdsat, id) into the retained log, then
#: re-selects the ac plot so klt's `write` dumps the AC response.
OP_PRINT = ["v(vout)", "v(vinp)", "v(vinn)"] + [
    f"@m.xdut.{d}.m0[{p}]" for d in DEVICES for p in ("vds", "vdsat", "id")
]
OP_TAIL = "\nop\nprint " + " ".join(OP_PRINT) + "\nsetplot ac1"

Key = tuple[str, float, int, int]  # (process, temperature_c, vdd_mv, vcm_mv)
Combo = tuple[str, float, int]  # (process, temperature_c, vdd_mv)


def fmt_combo(c: Combo) -> str:
    return f"{c[0]} / {c[1]:g} C / {c[2] / 1000:.2f} V"


def fmt_key(k: Key) -> str:
    return f"{fmt_combo(k[:3])} / VCM {k[3] / 1000:.3f} V"


#: An unsigned decimal (with optional exponent). Signs and range dashes such as
#: "0.1-1 Hz" are left in the text, so masked reasons stay readable.
_NUM_RX = re.compile(r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def validity_groups(inv: list) -> list[tuple[int, str, Key, str]]:
    """Group invalid samples by their first reason with every number masked as
    `N`; return (count, masked pattern, first sample's key, its verbatim reason),
    most frequent first. The verbatim example keeps the rendered line concrete."""
    groups: dict[str, list] = {}
    for s in inv:
        pat = _NUM_RX.sub("N", s.invalid_reasons[0])[:110]
        g = groups.setdefault(pat, [0, s.key, s.invalid_reasons[0]])
        g[0] += 1
    return [(n, pat, k, r) for pat, (n, k, r) in sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0]))]


def combo_stem(c: Combo) -> str:
    return f"{c[0]}_{c[1]:g}c_{c[2] / 1000:.2f}v"


def mv_to_v(mv: int) -> float:
    return round(mv / 1000.0, 6)


def all_combos() -> list[Combo]:
    return [(p, float(t), v) for p in CORNERS for v in SUPPLIES_MV for t in TEMPS_C]


def scan_vcm_mv(vdd_mv: int) -> list[int]:
    """The initial VCM scan of one supply: 0..VDD at <= 50 mV, plus VDD itself,
    VDD/2 and the 1.20 V target (always present, explicitly)."""
    pts = set(range(0, vdd_mv + 1, SCAN_STEP_MV))
    pts |= {vdd_mv, vdd_mv // 2, TARGET_VCM_MV}
    out = sorted(p for p in pts if 0 <= p <= vdd_mv)
    if any(b - a > SCAN_STEP_MV for a, b in zip(out, out[1:])):
        raise AssertionError("scan spacing exceeds the step")
    return out


# --------------------------------------------------------------------------
# klt requests
# --------------------------------------------------------------------------


def paired_supply(pairs: list[tuple[int, int]]) -> dict:
    """`corners.supply_v` for a list of (vdd_mv, vcm_mv) pairs.

    klt pairs `vdd` and `vcm` by INDEX, so each VDD is repeated once per VCM
    sample; both arrays have the same length by construction."""
    return {"vdd": [mv_to_v(v) for v, _ in pairs], "vcm": [mv_to_v(c) for _, c in pairs]}


def icmr_request(netlist: Path, pdk: Pdk, processes: str | list[str], temps: list[float],
                 pairs: list[tuple[int, int]], *, local: bool = False) -> dict:
    """The process corner(s) x the temperatures x the paired (VDD, VCM) samples.

    Built from the gain bench's request (corner bundles, model library), with the
    paired supply axis, the low-frequency `.ac` grid, the operating-point print
    appended and no `.meas` cards (no fleet expression-measurement support is
    needed). `local` pins the model library to the harness-resolved PDK root."""
    procs = [processes] if isinstance(processes, str) else list(processes)
    req = G.ac_request(netlist, pdk, procs, temps, [3.3])
    req["corners"]["supply_v"] = paired_supply(pairs)
    req["measurements"] = []
    req["analysis"] = {"kind": "ac", "args": f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}" + OP_TAIL}
    if local:
        req["models"]["pdk_root"] = str(pdk.path.parent)
    return req


def expected_keys(processes: str | list[str], temps: list[float], pairs: list[tuple[int, int]]) -> list[Key]:
    procs = [processes] if isinstance(processes, str) else list(processes)
    return [(p, float(t), v, c) for p in procs for (v, c) in pairs for t in temps]


def unit_key(corner: dict) -> Key:
    """Identity of one report corner, from its process, temperature, VDD, VCM."""
    sv = corner["supply_v"]
    vdd, vcm = sv["vdd"] * 1000.0, sv["vcm"] * 1000.0
    if abs(vdd - round(vdd)) > 1e-6 or abs(vcm - round(vcm)) > 1e-6:
        raise ValueError(f"supply values not on a whole millivolt: vdd={sv['vdd']} vcm={sv['vcm']}")
    return (corner["process"], float(corner["temperature_c"]), int(round(vdd)), int(round(vcm)))


# --------------------------------------------------------------------------
# Per-unit parsing
# --------------------------------------------------------------------------

_OP_RE = re.compile(r"^(\S+)\s*=\s*([-+]?[0-9.]+(?:[eE][-+]?\d+)?)\s*$", re.M)
_OP_NAMES = {p.lower() for p in OP_PRINT}


def parse_op_log(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, val in _OP_RE.findall(text):
        if name.lower() in _OP_NAMES:
            out[name.lower()] = float(val)
    return out


@dataclass
class Unit:
    """What one simulation unit (one excitation of one sample) returned."""

    vec: dict | None = None
    op: dict | None = None
    error: str = ""  # non-empty: the unit did not complete / was unreadable
    raw_path: str | None = None
    log_path: str | None = None


def unit_from_texts(raw_text: str | None, log_text: str | None, error: str = "") -> Unit:
    if error:
        return Unit(error=error)
    if raw_text is None or log_text is None:
        return Unit(error="rawfile or log not retained")
    try:
        vec = G.parse_ascii_complex_raw(raw_text)
    except ValueError as exc:
        return Unit(error=f"malformed rawfile: {exc}")
    miss = [n for n in ("frequency", "v(vout)", "v(vinp)", "v(vinn)") if n not in vec]
    if miss:
        return Unit(error=f"rawfile lacks {', '.join(miss)}")
    return Unit(vec=vec, op=parse_op_log(log_text))


def units_from_report(report: dict, want: list[Key]) -> tuple[dict[Key, Unit], list[str]]:
    """{key: Unit} for the expected samples of one request, + structural problems
    (duplicates, extras). A missing or failed unit becomes a Unit with an error,
    which the sample evaluation turns into an INVALID (never passing) sample."""
    problems: list[str] = []
    seen: dict[Key, dict] = {}
    for c in report.get("corners", []):
        try:
            k = unit_key(c)
        except (KeyError, ValueError) as exc:
            problems.append(f"unidentifiable report corner {c.get('corner_id')}: {exc}")
            continue
        if k in seen:
            problems.append(f"duplicate result for {fmt_key(k)}")
        seen[k] = c
    out: dict[Key, Unit] = {}
    n_job = 0
    for k in want:
        c = seen.get(k)
        if c is None:
            out[k] = Unit(error="no result returned for this sample")
            continue
        job_diag = [d for d in c.get("diagnostics", []) if str(d.get("code", "")).startswith("batch_")]
        if job_diag:
            # the whole job failed (e.g. a runner/client version refusal): a request-level
            # failure, not a per-sample simulation outcome
            n_job += 1
            out[k] = Unit(error=f"batch job failure: {job_diag[0].get('message', '')[:200]}")
            continue
        a = c.get("artifacts") or {}
        raw, logp = a.get("raw"), a.get("log")
        diag = "; ".join(d.get("message", "")[:160] for d in c.get("diagnostics", []) if d.get("severity") == "error")
        if not raw or not Path(raw).is_file():
            out[k] = Unit(error=f"analysis did not complete: {diag or 'no rawfile retained'}")
            continue
        if not logp or not Path(logp).is_file():
            out[k] = Unit(error="no ngspice log retained")
            continue
        u = unit_from_texts(Path(raw).read_text(), Path(logp).read_text())
        u.raw_path, u.log_path = raw, logp
        out[k] = u
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"unexpected extra samples: {sorted(extra)[:4]}")
    if n_job:
        first = next(u.error for u in out.values() if u.error.startswith("batch job failure"))
        problems.append(f"{n_job} sample(s) carry a batch JOB failure (request-level, not a simulation outcome): {first}")
    return out, problems


# --------------------------------------------------------------------------
# Sample evaluation (pure; unit-tested on synthetic data)
# --------------------------------------------------------------------------


@dataclass
class Sample:
    key: Key
    valid: bool
    invalid_reasons: list[str] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)  # criteria other than saturation
    gain_db: float = float("nan")  # min of Ad over the 0.1-1 Hz plateau
    gain_mean_db: float = float("nan")
    gain_spread_db: float = float("nan")
    vout_v: float = float("nan")
    margins_v: dict[str, float] = field(default_factory=dict)  # |Vds|-|Vdsat| per MOSFET
    ids_a: dict[str, float] = field(default_factory=dict)  # |id| per MOSFET
    min_margin_v: float = float("nan")  # over CRITERIA_DEVICES
    limiting_device: str = ""

    def status(self, sat_tol: float = SAT_TOL_V) -> str:
        if not self.valid:
            return "invalid"
        if self.fail_reasons or self.min_margin_v < -sat_tol:
            return "fail"
        return "pass"

    def tolerance_flags(self, sat_tol: float = SAT_TOL_V) -> list[str]:
        """Devices whose margin is negative but inside the numerical tolerance."""
        return [d for d in CRITERIA_DEVICES if -sat_tol <= self.margins_v.get(d, 0.0) < 0.0]

    def reasons(self, sat_tol: float = SAT_TOL_V) -> list[str]:
        if not self.valid:
            return list(self.invalid_reasons)
        out = list(self.fail_reasons)
        if self.min_margin_v < -sat_tol:
            out.append(f"{self.limiting_device.upper()} below saturation by {-self.min_margin_v * 1e3:.1f} mV")
        return out


def invalid_sample(key: Key, *reasons: str) -> Sample:
    return Sample(key=key, valid=False, invalid_reasons=list(reasons))


def _op_complete(op: dict[str, float] | None) -> list[str]:
    if op is None:
        return ["operating point not returned"]
    missing = [p for p in OP_PRINT if p.lower() not in op]
    if missing:
        return [f"operating-point fields missing ({len(missing)}: {', '.join(missing[:3])}...)"]
    bad = [p for p in OP_PRINT if not math.isfinite(op[p.lower()])]
    if bad:
        return [f"non-finite operating-point fields: {', '.join(bad[:3])}"]
    return []


def evaluate_sample(key: Key, dm: Unit | None, cm: Unit | None) -> Sample:
    """The verdict inputs of ONE sample from its two excitation units.

    Any validity problem returns an INVALID sample (never passing)."""
    vcm = key[3] / 1000.0
    probs: list[str] = []
    for tag, u in (("dm", dm), ("cm", cm)):
        if u is None:
            probs.append(f"[{tag}] no result")
        elif u.error:
            probs.append(f"[{tag}] {u.error}")
        else:
            probs += [f"[{tag}] {p}" for p in _op_complete(u.op)]
    if probs:
        return invalid_sample(key, *probs)
    assert dm is not None and cm is not None and dm.op is not None and cm.op is not None
    # Paired operating points must be the SAME DC solution.
    worst = max(abs(dm.op[p.lower()] - cm.op[p.lower()]) for p in OP_PRINT if p.startswith("v(") or "[vds" in p or "[vdsat" in p)
    if worst > OP_AGREE_V:
        return invalid_sample(key, f"paired operating points disagree by {worst * 1e6:.2f} uV (> {OP_AGREE_V * 1e6:g} uV)")
    op = dm.op
    if abs(op["v(vinp)"] - vcm) > OP_AGREE_V:
        return invalid_sample(key, f"v(vinp) = {op['v(vinp)']:.6f} V is not the intended VCM {vcm:.6f} V")

    # AC excitation and the joint Ad/Acm solve (the CMRR bench's method).
    try:
        f_dm, f_cm = np.asarray(dm.vec["frequency"].real, float), np.asarray(cm.vec["frequency"].real, float)
        C.check_axis(f_dm, N_FREQ)
        C.same_axis(f_dm, f_cm)
        xd, xc = C.Excitation.from_vec(dm.vec), C.Excitation.from_vec(cm.vec)
        C.finite(xd.vd, xd.vc, xd.vout, xc.vd, xc.vc, xc.vout)
        _, b1 = C.check_dm(xd)
        _, _, b2 = C.check_cm(xc)
        if b1 or b2:
            return invalid_sample(key, *(b1 + b2))
        ad, _acm = C.solve_ad_acm(xd, xc)
    except (C.ExtractionError, KeyError) as exc:
        return invalid_sample(key, f"AC extraction: {exc}")

    # DC-plateau gain.
    band = f_dm <= BAND_HI_HZ * (1 + 1e-9)
    if band.sum() < 3:
        return invalid_sample(key, "fewer than 3 points in the 0.1-1 Hz plateau band")
    mag = np.abs(ad[band])
    if np.any(mag <= 0) or not np.all(np.isfinite(mag)):
        return invalid_sample(key, "zero or non-finite Ad in the plateau band")
    gdb = 20.0 * np.log10(mag)
    spread = float(gdb.max() - gdb.min())
    phase = float(np.degrees(np.angle(np.mean(ad[band]))))
    if spread > PLATEAU_TOL_DB:
        return invalid_sample(key, f"no Ad plateau over 0.1-1 Hz: varies by {spread:.3f} dB (> {PLATEAU_TOL_DB} dB)")
    if abs(phase) > POLARITY_TOL_DEG:
        return invalid_sample(key, f"Ad plateau phase {phase:.1f} deg is not ~0 (wrong polarity)")

    margins = {d: abs(op[f"@m.xdut.{d}.m0[vds]"]) - abs(op[f"@m.xdut.{d}.m0[vdsat]"]) for d in DEVICES}
    ids = {d: abs(op[f"@m.xdut.{d}.m0[id]"]) for d in DEVICES}
    lim = min(CRITERIA_DEVICES, key=lambda d: margins[d])
    s = Sample(key=key, valid=True, gain_db=float(gdb.min()), gain_mean_db=float(gdb.mean()), gain_spread_db=spread,
               vout_v=op["v(vout)"], margins_v=margins, ids_a=ids, min_margin_v=margins[lim], limiting_device=lim)
    if s.gain_db < GAIN_MIN_DB:
        s.fail_reasons.append(f"plateau gain {s.gain_db:.2f} dB < {GAIN_MIN_DB:g} dB")
    if abs(s.vout_v - vcm) > VOUT_TOL_V:
        s.fail_reasons.append(f"|vout - VCM| = {abs(s.vout_v - vcm) * 1e3:.0f} mV > {VOUT_TOL_V * 1e3:g} mV")
    zero = [d for d in CURRENT_DEVICES if ids[d] < MIN_ID_A]
    if zero:
        s.fail_reasons.append(f"zero current in {', '.join(d.upper() for d in zero)}")
    return s


# --------------------------------------------------------------------------
# Ranges (pure; unit-tested)
# --------------------------------------------------------------------------


@dataclass
class Interval:
    """One contiguous passing interval of one PVT point (mV), conservative
    endpoints on the passing side, with the bracket to the adjacent failing
    sample as the uncertainty (None: the scan edge is the bound)."""

    lo_mv: int
    hi_mv: int
    n_samples: int
    lo_unc_mv: int | None  # distance to the nearest non-passing sample below; None at the scan edge
    hi_unc_mv: int | None
    lo_nb: str = ""  # status of that neighbour
    hi_nb: str = ""
    lo_src: str = ""  # which PVT point set the endpoint (intersections)
    hi_src: str = ""


def pass_intervals(samples: list[Sample], *, sat_tol: float = SAT_TOL_V, max_gap_mv: int = SCAN_STEP_MV) -> list[Interval]:
    """Contiguous passing intervals of one PVT point.

    Samples are taken in VCM order; two passing samples join only if they are
    ADJACENT in the table and no further than `max_gap_mv` apart. A failing,
    invalid or missing sample ends an interval; nothing is bridged."""
    s = sorted(samples, key=lambda x: x.key[3])
    ok = [x.status(sat_tol) == "pass" for x in s]
    out: list[Interval] = []
    i = 0
    while i < len(s):
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(s) and ok[j + 1] and s[j + 1].key[3] - s[j].key[3] <= max_gap_mv:
            j += 1
        lo_nb = s[i - 1] if i > 0 else None
        hi_nb = s[j + 1] if j + 1 < len(s) else None
        out.append(Interval(
            lo_mv=s[i].key[3], hi_mv=s[j].key[3], n_samples=j - i + 1,
            lo_unc_mv=None if lo_nb is None else s[i].key[3] - lo_nb.key[3],
            hi_unc_mv=None if hi_nb is None else hi_nb.key[3] - s[j].key[3],
            lo_nb="scan edge" if lo_nb is None else lo_nb.status(sat_tol),
            hi_nb="scan edge" if hi_nb is None else hi_nb.status(sat_tol),
        ))
        i = j + 1
    return out


def intersect_intervals(per_combo: dict[Combo, list[Interval]]) -> list[Interval]:
    """Intersection of the interval unions of every PVT point (empty list = empty
    intersection). Each endpoint keeps the uncertainty and the source of the
    point that set it. A PVT point with no interval empties the result."""
    cur: list[Interval] | None = None
    for combo, ivs in per_combo.items():
        label = fmt_combo(combo)
        tagged = [Interval(i.lo_mv, i.hi_mv, i.n_samples, i.lo_unc_mv, i.hi_unc_mv, i.lo_nb, i.hi_nb, label, label) for i in ivs]
        if cur is None:
            cur = tagged
            continue
        nxt: list[Interval] = []
        for a in cur:
            for b in tagged:
                lo_src = b if b.lo_mv > a.lo_mv else a
                hi_src = b if b.hi_mv < a.hi_mv else a
                lo, hi = max(a.lo_mv, b.lo_mv), min(a.hi_mv, b.hi_mv)
                if lo <= hi:
                    nxt.append(Interval(lo, hi, 0, lo_src.lo_unc_mv, hi_src.hi_unc_mv, lo_src.lo_nb, hi_src.hi_nb,
                                        lo_src.lo_src, hi_src.hi_src))
        cur = sorted(nxt, key=lambda i: i.lo_mv)
        if not cur:
            return []
    return cur or []


def component_containing(ivs: list[Interval], vcm_mv: int) -> Interval | None:
    for i in ivs:
        if i.lo_mv <= vcm_mv <= i.hi_mv:
            return i
    return None


def target_verdict(table: dict[Key, Sample], combos: list[Combo], vcm_mv: int = TARGET_VCM_MV,
                   sat_tol: float = SAT_TOL_V) -> dict:
    """Grade the explicit VCM sample at every PVT point.

    meets: all pass; fails: at least one valid sample fails; unknown: no failure
    but some sample is invalid/absent (missing evidence is never a pass)."""
    rows, n_pass, n_fail, n_inv = [], 0, 0, 0
    for c in combos:
        s = table.get((*c, vcm_mv))
        st = "invalid" if s is None else s.status(sat_tol)
        n_pass += st == "pass"
        n_fail += st == "fail"
        n_inv += st == "invalid"
        rows.append((c, st, s))
    verdict = "fails" if n_fail else ("unknown" if n_inv else "meets")
    return {"verdict": verdict, "n_pass": n_pass, "n_fail": n_fail, "n_invalid": n_inv, "rows": rows}


def worst_over(samples: list[Sample]) -> dict:
    """Worst plateau gain and limiting device margin over valid samples."""
    v = [s for s in samples if s.valid]
    if not v:
        return {"gain": None, "margin": None}
    return {"gain": min(v, key=lambda s: s.gain_db), "margin": min(v, key=lambda s: s.min_margin_v)}


def refinement_points(table: dict[Key, Sample], combos: list[Combo], *, sat_tol: float = SAT_TOL_V,
                      step_mv: int = REFINE_STEP_MV) -> dict[Combo, list[int]]:
    """VCM samples that bring every pass/non-pass transition bracket to <= step_mv.

    A bracket is two adjacent samples, one passing and one not, further apart
    than `step_mv`. The returned points fill it at `step_mv` spacing."""
    need: dict[Combo, list[int]] = {}
    for c in combos:
        s = sorted((v for k, v in table.items() if k[:3] == c), key=lambda x: x.key[3])
        for a, b in zip(s, s[1:]):
            pa, pb = a.status(sat_tol) == "pass", b.status(sat_tol) == "pass"
            if pa != pb and b.key[3] - a.key[3] > step_mv:
                pts = list(range(a.key[3] + step_mv, b.key[3], step_mv))
                if pts:
                    need.setdefault(c, []).extend(pts)
    return need


# --------------------------------------------------------------------------
# Committed-evidence reference for the midrail control
# --------------------------------------------------------------------------


def latest_cmrr_dir() -> Path | None:
    base = CMRR_DIR / "corners"
    if not base.is_dir():
        return None
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        if len(list(d.glob("*.cmrr.dat"))) >= 45:
            return d
    return None


def committed_midrail_gain_db(cdir: Path, c: Combo) -> float | None:
    """Mean Ad (dB) over 0.1-1 Hz of the committed CMRR record at (c, VDD/2)."""
    p = cdir / f"{c[0]}_{c[1]:g}c_{c[2] / 1000:.2f}v.cmrr.dat"
    if not p.is_file():
        return None
    d = np.loadtxt(p)
    sel = d[:, 0] <= BAND_HI_HZ * (1 + 1e-9)
    return float(np.mean(d[sel, 1])) if sel.sum() >= 3 else None


# --------------------------------------------------------------------------
# Local single units (controls)
# --------------------------------------------------------------------------


def run_local_unit(name: str, pdk: Pdk, work: Path, params: dict, vdd_mv: int, vcm_mv: int,
                   *, process: str = "typical", temp_c: float = 27.0, dut_text: str | None = None) -> Unit:
    try:
        wd = work / name
        tb = C.materialise(wd, pdk, params, testbench=TESTBENCH, guard=C.guard_testbench, dut_text=dut_text)
        req = icmr_request(tb, pdk, process, [temp_c], [(vdd_mv, vcm_mv)], local=True)
        rep = run_klt(req, wd / "out", "local", wd, env=C.local_env(work))
        key = (process, temp_c, vdd_mv, vcm_mv)
        units, probs = units_from_report(rep, [key])
        u = units[key]
        if probs and not u.error:
            u.error = "; ".join(probs)
        return u
    except (KltError, RuntimeError, ValueError) as exc:
        return Unit(error=str(exc))


def local_sample(name: str, pdk: Pdk, work: Path, vcm_mv: int, *, vdd_mv: int = 3300, over: dict | None = None,
                 cm_params: dict | None = None) -> tuple[Sample, Unit, Unit]:
    over = over or {}
    dm = run_local_unit(f"{name}-dm", pdk, work, C.with_servo(MODES["dm"], **over), vdd_mv, vcm_mv)
    cm = run_local_unit(f"{name}-cm", pdk, work, C.with_servo(cm_params or MODES["cm"], **over), vdd_mv, vcm_mv)
    return evaluate_sample((*NOMINAL[:1], NOMINAL[1], vdd_mv, vcm_mv), dm, cm), dm, cm


@dataclass
class Controls:
    nominal: Sample | None
    iso: list[tuple[str, Sample]]
    inadequate: Sample | None
    unequal: Sample | None
    local_vs_grid_db: float | None
    midrail: dict[Combo, tuple[float, float]]  # combo -> (this scan mean dB, committed mean dB)
    midrail_ref_dir: str
    problems: list[str]


def run_controls(pdk: Pdk, work: Path, table: dict[Key, Sample]) -> Controls:
    probs: list[str] = []
    nom_key_vcm = int(round(NOMINAL[2] * 1000)) // 2  # 1650 mV
    nominal, _, _ = local_sample("nominal", pdk, work, nom_key_vcm)
    if not nominal.valid:
        probs.append(f"local nominal midrail control invalid: {'; '.join(nominal.invalid_reasons)}")
    iso: list[tuple[str, Sample]] = []
    for csv_v in C.ISOLATION_CSV:
        s, _, _ = local_sample(f"iso-csv{csv_v:g}", pdk, work, nom_key_vcm, over={"csv": csv_v})
        label = f"servo tau = {SERVO_NOMINAL['rsv'] * csv_v:g} s"
        iso.append((label, s))
        if not s.valid:
            probs.append(f"isolation {label}: invalid ({'; '.join(s.invalid_reasons)})")
        elif nominal.valid and abs(s.gain_mean_db - nominal.gain_mean_db) > ISOLATION_TOL_DB:
            probs.append(f"isolation {label}: plateau gain moves {abs(s.gain_mean_db - nominal.gain_mean_db):.4f} dB "
                         f"(> {ISOLATION_TOL_DB} dB)")
    inad, _, _ = local_sample("iso-inadequate", pdk, work, nom_key_vcm, over={"csv": C.ISOLATION_INADEQUATE_CSV})
    if inad.valid:
        probs.append("an intentionally inadequate servo (tau = 1e-3 s) was NOT rejected")
    uneq, _, _ = local_sample("unequal-cm", pdk, work, nom_key_vcm, cm_params={"acp": 1.0, "acn": 0.99})
    if uneq.valid:
        probs.append("unequal common-mode drive (acp = 1, acn = 0.99) was NOT rejected")

    nom_combo: Combo = (NOMINAL[0], NOMINAL[1], int(round(NOMINAL[2] * 1000)))
    grid_nom = table.get((*nom_combo, nom_key_vcm))
    lvg = None
    if nominal.valid and grid_nom is not None and grid_nom.valid:
        lvg = abs(nominal.gain_mean_db - grid_nom.gain_mean_db)
        if lvg > LOCAL_VS_GRID_TOL_DB:
            probs.append(f"local nominal unit and the scan's nominal midrail sample differ by {lvg:.3f} dB")
    else:
        probs.append("nominal midrail sample missing or invalid in the scan (local-vs-grid parity not checked)")

    cdir = latest_cmrr_dir()
    mid: dict[Combo, tuple[float, float]] = {}
    if cdir is None:
        probs.append("committed CMRR record not available; midrail reproduction NOT CHECKED")
    else:
        for c in all_combos():
            s = table.get((*c, c[2] // 2))
            ref = committed_midrail_gain_db(cdir, c)
            if s is None or not s.valid or ref is None:
                probs.append(f"midrail reproduction unavailable at {fmt_combo(c)}")
                continue
            mid[c] = (s.gain_mean_db, ref)
            if abs(s.gain_mean_db - ref) > MIDRAIL_TOL_DB:
                probs.append(f"midrail plateau gain at {fmt_combo(c)} differs from the committed CMRR record by "
                             f"{abs(s.gain_mean_db - ref):.3f} dB (> {MIDRAIL_TOL_DB} dB)")
    return Controls(nominal, iso, inad, uneq, lvg, mid, cdir.name if cdir else "n/a", probs)


# --------------------------------------------------------------------------
# Batch requests: scan + refinement
# --------------------------------------------------------------------------


@dataclass
class Plan:
    """One klt request: process corner(s), all temperatures, the paired samples."""

    name: str
    mode: str
    processes: list[str]
    pairs: list[tuple[int, int]]


#: Requests whose report came from the `--keep-work` cache (an identical request
#: submitted by an earlier invocation); named in the record's execution block.
REUSED: set[str] = set()


_INCLUDE_RX = re.compile(r"^\s*\.(?:include|inc|lib)\s+['\"]?([^'\"\s]+)", re.M | re.I)


def netlist_fingerprint(netlist: Path) -> str:
    """sha256 over the materialised netlist and every file it `.include`s / `.lib`s
    that exists on disk (recursively, each file once, in first-reference order).

    Part of the `--keep-work` cache key: the request JSON names the netlist only
    by path, so without its CONTENT a changed DUT or bench would silently reuse
    a stale fleet report. Missing targets (e.g. PDK-relative model names the
    request resolves itself) are hashed by name only."""
    h = hashlib.sha256()
    seen: set[Path] = set()

    def walk(f: Path) -> None:
        f = f.resolve()
        if f in seen:
            return
        seen.add(f)
        data = f.read_bytes()
        h.update(f"file {f.name} {len(data)}\n".encode())
        h.update(data)
        for m in _INCLUDE_RX.finditer(data.decode(errors="replace")):
            tgt = Path(m.group(1))
            tgt = tgt if tgt.is_absolute() else f.parent / tgt
            if tgt.is_file():
                walk(tgt)
            else:
                h.update(f"missing {m.group(1)}\n".encode())

    walk(netlist)
    return h.hexdigest()


def request_cache_key(req: dict, netlist: Path) -> str:
    """`--keep-work` cache key: the request minus its (path-only) `netlist`
    field, plus the content fingerprint of the netlist and its includes."""
    body = {k: v for k, v in req.items() if k != "netlist"}
    body["netlist_sha256"] = netlist_fingerprint(netlist)
    return json.dumps(body, sort_keys=True)


def run_requests(plans: list[Plan], pdk: Pdk, work: Path, args, reqs_out: dict, reports_out: dict,
                 walls: dict) -> None:
    """Submit each plan as one `klt sim` corner request (retrying only a
    capacity-refused batch submit; never changing backend). A report cached in
    the work directory for an IDENTICAL request is reused (`--keep-work` re-runs);
    identity includes the content of the materialised netlist and its includes.

    The plans of one phase (one per excitation) are independent off-host jobs,
    so they are submitted concurrently; this host only waits on them. A
    `KltError` from any of them propagates (no record is written)."""
    with ThreadPoolExecutor(max_workers=max(1, len(plans))) as pool:
        futs = [pool.submit(_run_plan, p, pdk, work, args, reqs_out, reports_out, walls) for p in plans]
        errs = []
        for f in futs:
            try:
                f.result()
            except KltError as exc:
                errs.append(exc)
        if errs:
            raise errs[0]


def _run_plan(p: Plan, pdk: Pdk, work: Path, args, reqs_out: dict, reports_out: dict, walls: dict) -> None:
    wd = work / p.name
    tb = C.materialise(wd, pdk, C.with_servo(MODES[p.mode]), testbench=TESTBENCH, guard=C.guard_testbench)
    req = icmr_request(tb, pdk, p.processes, TEMPS_C, p.pairs)
    req["batch"] = batch_block(args)
    cache = work / f"{p.name}.cached-report.json"
    rq = request_cache_key(req, tb)
    if cache.is_file():
        blob = json.loads(cache.read_text())
        if blob["request"] == rq and all(Path(c["artifacts"]["raw"]).is_file() for c in blob["report"]["corners"]
                                          if (c.get("artifacts") or {}).get("raw")):
            print(f"  reusing cached report for {p.name}", flush=True)
            reports_out[p.name], reqs_out[p.name], walls[p.name] = blob["report"], req, blob["wall"]
            REUSED.add(p.name)
            return
    n = len(p.pairs) * len(TEMPS_C) * len(p.processes)
    print(f"  submitting {p.name} ({n} units)...", flush=True)
    t0 = time.time()
    rep = run_klt_retrying(req, wd / "out", args.backend, wd,
                           retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
    walls[p.name] = time.time() - t0
    reports_out[p.name], reqs_out[p.name] = rep, req
    if not any(str(d.get("code", "")).startswith("batch_") for c in rep.get("corners", [])
               for d in c.get("diagnostics", [])):  # never cache a failed JOB
        cache.write_text(json.dumps({"request": rq, "report": rep, "wall": walls[p.name]}))
    r = remote_of(rep)
    print(f"    done in {walls[p.name]:.0f} s (job {r.get('job_id', 'local')}, "
          f"{rep.get('passed', '?')}/{rep.get('corner_count', '?')} units passed)", flush=True)


def phase_plans(phase: str, processes: list[str], pairs: list[tuple[int, int]]) -> list[Plan]:
    """One request per excitation: the processes x all temperatures x the pairs."""
    return [Plan(f"{phase}-{mode}", mode, list(processes), list(pairs)) for mode in MODES]


def refine_pairs(need: dict[Combo, list[int]]) -> tuple[list[str], list[tuple[int, int]]]:
    """The refinement request axes: the processes that need points and the union
    of their paired (VDD, VCM) samples. A request crosses its pairs with every
    listed process and temperature, so points needed at one PVT point are run
    at all of them (extra samples are kept as evidence; they cannot hurt)."""
    pairs: set[tuple[int, int]] = set()
    procs: list[str] = []
    for (proc, _t, vdd), pts in need.items():
        if proc not in procs:
            procs.append(proc)
        pairs.update((vdd, c) for c in pts)
    return [p for p in CORNERS if p in procs], sorted(pairs)


def collect(plans: list[Plan], reports: dict, retained: dict[Key, dict[str, Unit]]) -> list[str]:
    problems: list[str] = []
    for p in plans:
        want = expected_keys(p.processes, TEMPS_C, p.pairs)
        units, pr = units_from_report(reports[p.name], want)
        problems += [f"[{p.name}] {x}" for x in pr]
        for k, u in units.items():
            retained.setdefault(k, {})[p.mode] = u
    return problems


def build_table(retained: dict[Key, dict[str, Unit]]) -> dict[Key, Sample]:
    return {k: evaluate_sample(k, m.get("dm"), m.get("cm")) for k, m in retained.items()}


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass
class Analysis:
    per_combo: dict[Combo, list[Interval]]
    per_combo_strict: dict[Combo, list[Interval]]
    intersection: list[Interval]
    intersection_strict: list[Interval]
    component: Interval | None
    component_strict: Interval | None
    verdict: dict
    verdict_strict: dict
    worst_target: dict
    worst_component: dict
    n_samples: int
    n_status: dict[str, int]


def analyse(table: dict[Key, Sample], combos: list[Combo] | None = None) -> Analysis:
    combos = combos or all_combos()
    by_combo: dict[Combo, list[Sample]] = {c: [] for c in combos}
    for k, s in table.items():
        by_combo.setdefault(k[:3], []).append(s)
    per = {c: pass_intervals(by_combo[c]) for c in combos}
    per_s = {c: pass_intervals(by_combo[c], sat_tol=0.0) for c in combos}
    inter, inter_s = intersect_intervals(per), intersect_intervals(per_s)
    comp = component_containing(inter, TARGET_VCM_MV)
    comp_s = component_containing(inter_s, TARGET_VCM_MV)
    verdict = target_verdict(table, combos)
    verdict_s = target_verdict(table, combos, sat_tol=0.0)
    tgt = worst_over([s for c in combos if (s := table.get((*c, TARGET_VCM_MV))) is not None])
    if comp is not None:
        inside = [s for k, s in table.items() if k[:3] in set(combos) and comp.lo_mv <= k[3] <= comp.hi_mv]
        wc = worst_over(inside)
    else:
        wc = {"gain": None, "margin": None}
    n_status = {"pass": 0, "fail": 0, "invalid": 0}
    for s in table.values():
        n_status[s.status()] += 1
    return Analysis(per, per_s, inter, inter_s, comp, comp_s, verdict, verdict_s, tgt, wc, len(table), n_status)


def expected_table_keys(table: dict[Key, Sample]) -> list[str]:
    """Structural completeness: every combo has its full initial scan."""
    probs = []
    for c in all_combos():
        have = {k[3] for k in table if k[:3] == c}
        miss = [v for v in scan_vcm_mv(c[2]) if v not in have]
        if miss:
            probs.append(f"{fmt_combo(c)}: scan samples absent from the table: {miss[:5]}")
    return probs


# --------------------------------------------------------------------------
# Evidence writing
# --------------------------------------------------------------------------

CSV_FIELDS = ["process", "temp_c", "vdd_v", "vcm_v", "status", "status_strict", "gain_db", "gain_mean_db",
              "gain_spread_db", "vout_v", "min_margin_mv", "limiting_device", "tolerance_flags", "reasons"] + \
             [f"margin_mv_{d}" for d in DEVICES] + [f"id_a_{d}" for d in DEVICES]


def sample_row(s: Sample) -> dict:
    p, t, v, c = s.key
    row = {"process": p, "temp_c": f"{t:g}", "vdd_v": f"{v / 1000:.3f}", "vcm_v": f"{c / 1000:.3f}",
           "status": s.status(), "status_strict": s.status(0.0),
           "gain_db": _f(s.gain_db), "gain_mean_db": _f(s.gain_mean_db), "gain_spread_db": _f(s.gain_spread_db, 6),
           "vout_v": _f(s.vout_v, 6), "min_margin_mv": _f(s.min_margin_v * 1e3, 4) if s.valid else "",
           "limiting_device": s.limiting_device, "tolerance_flags": " ".join(s.tolerance_flags()),
           "reasons": " | ".join(s.reasons())}
    for d in DEVICES:
        row[f"margin_mv_{d}"] = _f(s.margins_v[d] * 1e3, 4) if d in s.margins_v else ""
        row[f"id_a_{d}"] = f"{s.ids_a[d]:.6e}" if d in s.ids_a else ""
    return row


def _f(x: float, nd: int = 4) -> str:
    return f"{x:.{nd}f}" if math.isfinite(x) else ""


def write_samples_csv(path: Path, table: dict[Key, Sample]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for k in sorted(table, key=lambda k: (CORNERS.index(k[0]), k[2], k[1], k[3])):
            w.writerow(sample_row(table[k]))


def vcm_tag(vcm_mv: int) -> str:
    return f"vcm{vcm_mv / 1000:.3f}V"


#: Vectors kept in the committed rawfiles: exactly what the extraction reads.
#: klt's rawfile also carries every other node and source current (20 vectors);
#: committing all of them for ~12 000 units would be ~135 MB of evidence that
#: no figure is derived from.
RAW_KEEP = ("frequency", "v(vinp)", "v(vinn)", "v(vout)")


def trim_raw(text: str, keep: tuple[str, ...] = RAW_KEEP) -> str:
    """An ngspice ASCII rawfile reduced to the `keep` vectors.

    Header lines other than the variable count and table are copied verbatim;
    every kept value line is copied verbatim (no re-formatting of numbers), so
    the trimmed file parses to bit-identical vectors. Raises ValueError when the
    file is malformed or lacks a kept vector."""
    head, sep, body = text.partition("Values:\n")
    if not sep or "Variables:\n" not in head:
        raise ValueError("rawfile header incomplete")
    pre, _, vtab = head.partition("Variables:\n")
    rows = [ln for ln in vtab.splitlines() if ln.strip()]
    names = [r.split("\t")[2].lower() for r in rows]
    m = re.search(r"^No\. Variables:\s*(\d+)$", pre, re.M)
    p = re.search(r"^No\. Points:\s*(\d+)$", pre, re.M)
    if not m or not p or int(m.group(1)) != len(names):
        raise ValueError("rawfile variable count does not match its table")
    nvar, npts = len(names), int(p.group(1))
    idx = []
    for k in keep:
        if k not in names:
            raise ValueError(f"rawfile has no {k} vector")
        idx.append(names.index(k))
    if idx[0] != 0:
        raise ValueError("the first kept vector must be the scale (frequency)")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) != nvar * npts:
        raise ValueError(f"rawfile has {len(lines)} value lines, expected {nvar} x {npts}")
    pre = re.sub(r"^No\. Variables:\s*\d+$", f"No. Variables: {len(idx)}", pre, flags=re.M)
    out = [pre + "Variables:"]
    for new_i, old_i in enumerate(idx):
        parts = rows[old_i].split("\t")
        parts[1] = str(new_i)
        out.append("\t".join(parts))
    out.append("Values:")
    for pt in range(npts):
        block = lines[pt * nvar:(pt + 1) * nvar]
        out += [block[i] for i in idx]
    return "\n".join(out) + "\n"


def write_archives(data_dir: Path, retained: dict[Key, dict[str, Unit]]) -> int:
    """One deterministic tar.gz per PVT point holding every retained rawfile
    (trimmed to RAW_KEEP; an untrimmable one is kept whole) and ngspice log, per
    sample and excitation. Returns the number of members."""
    data_dir.mkdir(parents=True, exist_ok=True)
    by_combo: dict[Combo, list[Key]] = {}
    for k in retained:
        by_combo.setdefault(k[:3], []).append(k)
    n = 0
    for c, keys in by_combo.items():
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            for k in sorted(keys, key=lambda k: k[3]):
                for mode, u in sorted(retained[k].items()):
                    for kind, src in (("raw", u.raw_path), ("log", u.log_path)):
                        if not src or not Path(src).is_file():
                            continue  # a failed unit retains whatever ngspice left (nothing, often)
                        blob = Path(src).read_bytes()
                        if kind == "raw":
                            try:
                                blob = trim_raw(blob.decode()).encode()
                            except (ValueError, UnicodeDecodeError):
                                pass  # malformed: keep it whole, it is the evidence of the failure
                        ti = tarfile.TarInfo(f"{vcm_tag(k[3])}.{mode}.{kind}")
                        ti.size, ti.mtime = len(blob), 0
                        tar.addfile(ti, io.BytesIO(blob))
                        n += 1
        (data_dir / f"{combo_stem(c)}.tar.gz").write_bytes(buf.getvalue())
    return n


def load_retained(data_dir: Path) -> dict[Key, dict[str, Unit]]:
    """Re-read every retained unit from the archives (the independent path used
    by `--recompute` and by the post-write self-check)."""
    texts: dict[tuple[Combo, int, str], dict[str, str]] = {}
    for tgz in sorted(data_dir.glob("*.tar.gz")):
        m = re.match(r"^(\w+?)_(-?[0-9.]+)c_([0-9.]+)v\.tar\.gz$", tgz.name)
        if not m:
            raise ValueError(f"unrecognised archive name {tgz.name}")
        combo: Combo = (m.group(1), float(m.group(2)), int(round(float(m.group(3)) * 1000)))
        with tarfile.open(tgz, "r:gz") as tar:
            for mem in tar.getmembers():
                mm = re.match(r"^vcm([0-9.]+)V\.(dm|cm)\.(raw|log)$", mem.name)
                if not mm:
                    raise ValueError(f"unrecognised member {mem.name} in {tgz.name}")
                key = (combo, int(round(float(mm.group(1)) * 1000)), mm.group(2))
                texts.setdefault(key, {})[mm.group(3)] = tar.extractfile(mem).read().decode()
    out: dict[Key, dict[str, Unit]] = {}
    for (combo, vcm, mode), t in texts.items():
        out.setdefault((*combo, vcm), {})[mode] = unit_from_texts(t.get("raw"), t.get("log"))
    return out


def strip_report(report: dict) -> dict:
    """Sanitised klt report without the per-corner `skipped` coverage lists."""
    rep = sanitise_report(report)
    cov = rep.get("coverage")
    if isinstance(cov, dict):
        cov["skipped"] = f"<{len(cov.get('skipped', []))} entries omitted: no_requested_measurements>"
    return rep


def write_gz_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb", mtime=0) as gz:
        gz.write(json.dumps(obj, indent=1).encode())


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------


def build_plots(table: dict[Key, Sample], an: Analysis, plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    out = []
    # 1. every contiguous passing interval of the 45 PVT points + the intersection.
    combos = all_combos()
    fig, ax = plt.subplots(figsize=(9, 11))
    for i, c in enumerate(combos):
        for iv in an.per_combo[c]:
            ax.plot([iv.lo_mv / 1000, iv.hi_mv / 1000], [i, i], lw=3, solid_capstyle="butt", color="C0")
    ax.axvline(TARGET_VCM_MV / 1000, color="C3", ls="--", lw=1, label="1.20 V")
    if an.component is not None:
        ax.axvspan(an.component.lo_mv / 1000, an.component.hi_mv / 1000, color="C2", alpha=0.25,
                   label="intersection (component containing 1.20 V)")
    ax.set_yticks(range(len(combos)))
    ax.set_yticklabels([fmt_combo(c) for c in combos], fontsize=6)
    ax.set_xlabel("VCM (V)")
    ax.set_title("Passing VCM intervals at the 45 PVT points")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(plot_dir / "passing-intervals-all45.png", dpi=110)
    plt.close(fig)
    out.append("passing-intervals-all45.png")
    # 2. plateau gain and limiting margin vs VCM at the nominal point and the worst-gain point.
    fig, axs = plt.subplots(2, 1, figsize=(8, 7), sharex=True)
    shown = [(NOMINAL[0], NOMINAL[1], int(round(NOMINAL[2] * 1000)))]
    wg = an.worst_target["gain"]
    if wg is not None and wg.key[:3] not in shown:
        shown.append(wg.key[:3])
    for c in shown:
        ss = sorted((s for k, s in table.items() if k[:3] == c and s.valid), key=lambda s: s.key[3])
        x = [s.key[3] / 1000 for s in ss]
        axs[0].plot(x, [s.gain_db for s in ss], marker=".", ms=3, lw=0.8, label=fmt_combo(c))
        axs[1].plot(x, [s.min_margin_v * 1e3 for s in ss], marker=".", ms=3, lw=0.8, label=fmt_combo(c))
    axs[0].axhline(GAIN_MIN_DB, color="k", ls=":", lw=0.8)
    axs[0].set_ylabel("plateau gain (dB)")
    axs[1].axhline(0, color="k", ls=":", lw=0.8)
    axs[1].set_ylabel("min |Vds|-|Vdsat| (mV)")
    axs[1].set_ylim(-300, 1500)
    axs[1].set_xlabel("VCM (V)")
    for a in axs:
        a.axvline(TARGET_VCM_MV / 1000, color="C3", ls="--", lw=1)
        a.grid(True, alpha=0.3)
        a.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(plot_dir / "gain-and-margin-vs-vcm.png", dpi=110)
    plt.close(fig)
    out.append("gain-and-margin-vs-vcm.png")
    return out


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def fmt_iv(i: Interval) -> str:
    return f"[{i.lo_mv / 1000:.3f}, {i.hi_mv / 1000:.3f}] V"


def fmt_unc(u: int | None) -> str:
    return "scan edge" if u is None else f"{u} mV"


def sample_line(s: Sample | None) -> str:
    if s is None:
        return "n/a"
    return (f"{s.gain_db:.2f} dB, limiting {s.limiting_device.upper()} {s.min_margin_v * 1e3:+.1f} mV at "
            f"{fmt_key(s.key)}")


def margin_line(s: Sample | None) -> str:
    if s is None:
        return "n/a"
    return (f"{s.limiting_device.upper()} {s.min_margin_v * 1e3:+.1f} mV (plateau gain {s.gain_db:.2f} dB) at "
            f"{fmt_key(s.key)}")


def build_record(*, record, stamp, pdk, ngspice, kver, table, an: Analysis, controls: Controls, reports, walls,
                 plans, rounds, plots, dut_sha, n_members, recheck_ok) -> str:
    L: list[str] = []
    add = L.append
    combos = all_combos()
    add(f"# Input common-mode range (ICMR) record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; commit `{record.rsplit('-', 1)[-1]}`; issue #60")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include "
        f"`{dut_sha[:16]}`), unchanged; snapshot `netlist-snapshots/{record}.spice`")
    for ln in C.pdk_lines(pdk, reports):
        add(ln.replace("the grid's open-loop response reproduces the committed gain-bench record (see Cross-checks)",
                       "the scan's midrail samples reproduce the committed CMRR record (see Controls: midrail "
                       "reproduction)"))
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`, numpy `{np.__version__}`")
    n_scan = sum(1 for p in plans if p.name.startswith("scan"))
    n_ref = len(plans) - n_scan
    add(f"- **Execution**: {len(plans)} `klt sim` corner requests ({n_scan} scan: one per excitation, crossing the "
        f"5 process corners x 3 temperatures with the paired VDD/VCM samples; {n_ref} refinement over {rounds} "
        "round(s), one per excitation per round). Per request:")
    for ln in C.execution_lines(reports, walls):
        add(ln)
    if REUSED:
        add(f"  - Reports of {', '.join(f'`{n}`' for n in sorted(REUSED))} were reused from the `--keep-work` cache: "
            "byte-identical requests (netlist path aside) submitted by an earlier invocation of this driver on the "
            "same work directory; the job ids above are those submissions. Wall times are the original jobs'.")
    add("  - Local single units (controls) run with an empty `HOME` so no user `~/.spiceinit` applies (DR-0004).")
    add("- **Axes**: process " + ", ".join(CORNERS) + "; T " + ", ".join(f"{t:g} C" for t in TEMPS_C) + "; VDD "
        + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V) + "; VCM scanned 0..VDD at <= 50 mV (explicit 1.20 V and "
        "VDD/2), transitions refined to <= 5 mV; ibias = 10 uA (ideal); CL = 2 pF. Every MOS corner uses "
        "`res_typical` + `mimcap_typical` (RZ/CC passive spread is NOT swept). `corners.supply_v` pairs `vdd` and "
        "`vcm` by index (each VDD repeated for each of its VCM samples).")
    add(f"- **Samples**: {an.n_samples} (PVT point, VCM) samples x 2 excitations: "
        f"{an.n_status['pass']} pass, {an.n_status['fail']} fail, {an.n_status['invalid']} invalid.")
    add("")
    add("## Claim")
    add("")
    add("**Measured, follower-biased ICMR of the schematic** (perfectly matched, passives at typical). It is the VCM "
        "range over which the input pair, tail, mirror and output stage stay saturated, the open-loop DC-plateau gain "
        f"stays >= {GAIN_MIN_DB:g} dB and the follower output stays within {VOUT_TOL_V * 1e3:g} mV of VCM. It is not "
        "an arbitrary-output-voltage input range and not an offset-accuracy claim; the bench-validity tolerances "
        "(1 mV saturation tolerance, 0.10 V output tolerance) are not ratified performance bounds. Sampled coverage "
        "does not prove continuous behaviour between samples: endpoints are the nearest verified PASSING samples and "
        "the bracket to the adjacent non-passing sample is the stated uncertainty.")
    add("")
    add("## Headline")
    add("")
    comp, inter = an.component, an.intersection
    if not inter:
        add("- **Intersection of the passing ranges over the 45 PVT points: EMPTY.** No VCM passes at every point. "
            "This is a result; no criterion was weakened to produce a range.")
    else:
        add("- **Intersection over the 45 PVT points** (every contiguous component): "
            + ", ".join(fmt_iv(i) for i in inter) + ".")
    if comp is not None:
        add(f"- **Component containing 1.20 V**: {fmt_iv(comp)}; low endpoint {comp.lo_mv / 1000:.3f} V set by "
            f"{comp.lo_src} (bracket {fmt_unc(comp.lo_unc_mv)} to a `{comp.lo_nb}` sample), high endpoint "
            f"{comp.hi_mv / 1000:.3f} V set by {comp.hi_src} (bracket {fmt_unc(comp.hi_unc_mv)} to a `{comp.hi_nb}` "
            "sample).")
    else:
        add("- **1.20 V is NOT inside the intersection** (no component contains it).")
    v = an.verdict
    add(f"- **1.20 V explicit sample at all 45 combinations**: **{v['verdict'].upper()}** "
        f"({v['n_pass']} pass, {v['n_fail']} fail, {v['n_invalid']} invalid/missing).")
    if v["n_fail"]:
        for c, st, s in v["rows"]:
            if st == "fail":
                add(f"  - fails at {fmt_combo(c)}: {'; '.join(s.reasons())}")
    add(f"- Worst plateau gain at 1.20 V: {sample_line(an.worst_target['gain'])}.")
    add(f"- Smallest device margin at 1.20 V: {margin_line(an.worst_target['margin'])}.")
    if comp is not None:
        add(f"- Worst plateau gain inside the 1.20 V component (all 45 points, all samples in it): "
            f"{sample_line(an.worst_component['gain'])}.")
        add(f"- Smallest device margin inside that component: {margin_line(an.worst_component['margin'])}.")
    add("")
    add("### Tolerance sensitivity (saturation tolerance 1 mV vs 0 mV)")
    add("")
    add(f"- Intersection with the 1 mV tolerance: "
        f"{', '.join(fmt_iv(i) for i in inter) if inter else 'empty'}; strict (0 mV): "
        f"{', '.join(fmt_iv(i) for i in an.intersection_strict) if an.intersection_strict else 'empty'}.")
    add(f"- 1.20 V verdict with 1 mV: {an.verdict['verdict']}; strict: {an.verdict_strict['verdict']}.")
    flagged = [s for s in table.values() if s.valid and s.tolerance_flags() and s.status() == "pass"]
    add(f"- Passing samples whose saturation margin is negative but inside the 1 mV tolerance: {len(flagged)}.")
    for s in sorted(flagged, key=lambda s: s.key)[:30]:
        add(f"  - {fmt_key(s.key)}: {', '.join(d.upper() for d in s.tolerance_flags())} "
            f"({min(s.margins_v[d] for d in s.tolerance_flags()) * 1e3:+.3f} mV)")
    ends = [(i, which) for c in combos for i in an.per_combo[c] for which in ("lo", "hi")]
    n_end_flag = 0
    for c in combos:
        for iv in an.per_combo[c]:
            for e in (iv.lo_mv, iv.hi_mv):
                s = table.get((*c, e))
                if s is not None and s.tolerance_flags():
                    n_end_flag += 1
    add(f"- Interval endpoints (of {len(ends)}) whose endpoint sample is inside the tolerance band: {n_end_flag}.")
    add("")
    add("## Passing intervals at every PVT point")
    add("")
    add("Every contiguous passing interval (adjacent valid passing samples; failed, invalid or missing samples are "
        "never bridged). Endpoints are the nearest passing samples; `unc` is the bracket to the adjacent non-passing "
        "sample (the true transition lies within it, on the non-passing side).")
    add("")
    add("| PVT point | interval (V) | samples | low unc | low neighbour | high unc | high neighbour | 1.20 V | strict (0 mV) intervals |")
    add("|---|---|---|---|---|---|---|---|---|")
    for c in combos:
        s120 = table.get((*c, TARGET_VCM_MV))
        st120 = "n/a" if s120 is None else s120.status()
        strict = ", ".join(fmt_iv(i) for i in an.per_combo_strict[c]) or "none"
        ivs = an.per_combo[c]
        if not ivs:
            add(f"| {fmt_combo(c)} | none | 0 | | | | | {st120} | {strict} |")
        for i in ivs:
            add(f"| {fmt_combo(c)} | {fmt_iv(i)} | {i.n_samples} | {fmt_unc(i.lo_unc_mv)} | {i.lo_nb} | "
                f"{fmt_unc(i.hi_unc_mv)} | {i.hi_nb} | {st120} | {strict} |")
    add("")
    add("## Explicit 1.20 V samples")
    add("")
    add("| PVT point | status | plateau gain (dB) | limiting device | margin (mV) | vout - VCM (mV) | reasons |")
    add("|---|---|---|---|---|---|---|")
    for c, st, s in v["rows"]:
        if s is None or not s.valid:
            add(f"| {fmt_combo(c)} | {st} | | | | | {'; '.join(s.reasons()) if s else 'sample absent'} |")
        else:
            add(f"| {fmt_combo(c)} | {st} | {s.gain_db:.2f} | {s.limiting_device.upper()} | "
                f"{s.min_margin_v * 1e3:+.1f} | {(s.vout_v - TARGET_VCM_MV / 1000) * 1e3:+.3f} | "
                f"{'; '.join(s.reasons())} |")
    add("")
    add("## Validity failures (preserved)")
    add("")
    inv = sorted((s for s in table.values() if not s.valid), key=lambda s: s.key)
    add(f"- {len(inv)} invalid sample(s) of {an.n_samples}. Invalid samples are never passing and never bridged; they "
        "are listed in `corners/<rid>/samples.csv` with their reasons.")
    for n, pattern, ex_key, ex_reason in validity_groups(inv)[:12]:
        add(f"  - {n} x {pattern} (e.g. {fmt_key(ex_key)}: {ex_reason})")
    add("")
    add("## Method")
    add("")
    add(f"- Per sample and excitation: `.ac dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}` ({N_FREQ} points), then an `op` "
        "of the same deck whose printout (vout, vinp, vinn and every DUT MOSFET's vds, vdsat, id) is retained in the "
        "log (`analysis.args`, no fleet expression-measurement support needed). `dm`: vinp +0.5 V, vinn -0.5 V AC; "
        "`cm`: vinp = vinn = 1 V AC; same DC common mode.")
    add("- The DC loop is closed by the CMRR bench's ideal servo (`Esb` buffer -> `Rsv`/`Csv`, tau = 1e18 s -> `Esv` in "
        "series with the vinn AC source): a unity-gain-follower operating point at DC and an open loop at every swept "
        "frequency. Closed-loop follower gain is not what is measured. From the ACTUAL input phasors the two runs are "
        "solved jointly for Ad and Acm per frequency (the CMRR driver's exact solve).")
    add(f"- **Plateau gain** = the minimum of |Ad| (dB) over 0.1-1 Hz, valid only if |Ad| is flat there to "
        f"{PLATEAU_TOL_DB} dB and has ~0 deg phase (a DC-plateau gain, not an exact zero-frequency measurement).")
    add(f"- **Saturation**: |Vds| - |Vdsat| >= -{SAT_TOL_V * 1e3:g} mV for the input pair (XM1, XM2), tail (XM5), "
        "mirror (XM3, XM4) and output stage (XM6, XM7); XM-B1 (the bias reference) is retained in the log and CSV "
        "but is not part of the criterion. Negative margins inside the tolerance are flagged; the analysis is "
        "repeated with 0 mV.")
    add(f"- **Rejected as INVALID**: missing/non-finite fields, an analysis that did not complete, incorrect AC "
        f"excitation (differential within {C.EXC_TOL:g} V of vd = 1, vc = 0; common mode within {C.EXC_TOL:g} V of "
        f"vc = 1 with |vd/vc| <= {C.CM_LEAK_MAX:g}), paired operating points disagreeing by > "
        f"{OP_AGREE_V * 1e6:g} uV, v(vinp) not at VCM, or no Ad plateau. **FAIL**: gain below {GAIN_MIN_DB:g} dB, "
        f"|vout - VCM| > {VOUT_TOL_V * 1e3:g} mV, a device below saturation by more than the tolerance, or zero "
        f"(< {MIN_ID_A:g} A) input-pair/tail current.")
    add("- **Refinement**: every adjacent pass/non-pass pair further apart than 5 mV is filled at 5 mV spacing by "
        f"additional paired batch requests ({rounds} round(s)); each round's request crosses the union of the "
        "needed (VDD, VCM) pairs with every process corner that needed one and every temperature, and all "
        "returned samples are kept.")
    add("")
    add("## Controls")
    add("")
    cn = controls
    if cn.nominal is not None and cn.nominal.valid:
        add(f"- **Nominal local unit** (typical / 27 C / 3.30 V, VCM 1.650 V): plateau gain "
            f"{cn.nominal.gain_mean_db:.3f} dB (min {cn.nominal.gain_db:.3f} dB).")
    if cn.local_vs_grid_db is not None:
        add(f"- Local nominal unit vs the scan's nominal midrail sample: {cn.local_vs_grid_db:.4f} dB "
            f"(tolerance {LOCAL_VS_GRID_TOL_DB} dB).")
    if cn.midrail:
        dev = {c: abs(a - b) for c, (a, b) in cn.midrail.items()}
        wc = max(dev, key=dev.get)
        nom = (NOMINAL[0], NOMINAL[1], int(round(NOMINAL[2] * 1000)))
        add(f"- **Midrail reproduction** of the committed CMRR record `{cn.midrail_ref_dir}` (mean Ad over 0.1-1 Hz at "
            f"VDD/2): {len(cn.midrail)}/45 points compared; max deviation {dev[wc]:.4f} dB at {fmt_combo(wc)}; "
            f"nominal point {dev.get(nom, float('nan')):.4f} dB (tolerance {MIDRAIL_TOL_DB} dB).")
    for label, s in cn.iso:
        d = abs(s.gain_mean_db - cn.nominal.gain_mean_db) if s.valid and cn.nominal and cn.nominal.valid else float("nan")
        add(f"- **Servo isolation**, {label}: plateau gain moves {d:.5f} dB (tolerance {ISOLATION_TOL_DB} dB).")
    add(f"- **Inadequate servo** (tau = {SERVO_NOMINAL['rsv'] * C.ISOLATION_INADEQUATE_CSV:g} s): "
        + (f"REJECTED ({cn.inadequate.invalid_reasons[0][:140]})" if cn.inadequate is not None and not cn.inadequate.valid
           else "NOT rejected") + ".")
    add("- **Unequal common-mode drive** (acp = 1, acn = 0.99): "
        + (f"REJECTED ({cn.unequal.invalid_reasons[0][:140]})" if cn.unequal is not None and not cn.unequal.valid
           else "NOT rejected") + ".")
    for p in cn.problems:
        add(f"- CONTROL PROBLEM: {p}")
    add("")
    add("## Independent recomputation")
    add("")
    add(f"- After writing, every sample was re-derived from the RETAINED archives ({n_members} rawfile/log members in "
        f"`corners/{record}/data/`) through a separate read path (`--recompute {record}` repeats it) and compared "
        f"with `samples.csv` (status, gain, margin) and with the headline numbers above: "
        f"**{'identical' if recheck_ok else 'MISMATCH'}**.")
    add("")
    add("## Plots")
    add("")
    for p in plots:
        add(f"![{p}]({record}-plots/{p})")
    add("")
    add("## Reproduce / evidence")
    add("")
    add("```")
    add("python3 sim/input-common-mode/run_input_common_mode.py            # full scan + refinement + a new record")
    add("python3 sim/input-common-mode/run_input_common_mode.py --smoke    # nominal point, local, no record")
    add(f"python3 sim/input-common-mode/run_input_common_mode.py --recompute {record}")
    add("```")
    add("")
    add(f"- `corners/{record}/samples.csv` (every sample, with margins and currents of every MOSFET), "
        f"`corners/{record}/data/*.tar.gz` (retained `.raw` and `.log` per sample and excitation), "
        f"`corners/{record}/requests/*.json.gz` and `reports/*.json.gz` (every klt request and sanitised report), "
        f"`corners/{record}/controls/` (local control logs).")
    add(f"- Testbench `sim/input-common-mode/testbench/tb_input_common_mode.spice`; driver "
        "`sim/input-common-mode/run_input_common_mode.py`; tests `sim/input-common-mode/test_input_common_mode.py`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #60)")
    add("")
    return "\n".join(L)


def write_snapshot(path: Path, record: str, reqs: dict, deck0: str) -> None:
    dut_text = load_dut_text()
    lines = [f"* netlist snapshot for record {record} (issue #60)",
             "* Reproduces the measured design: DUT contents, testbench, conditions."]
    first = next(iter(reqs))
    lines += [f"* ---- conditions: klt sim request `{first}` (one of {len(reqs)}; the others differ only in process "
              "corner, excitation and the paired vdd/vcm samples) ----"]
    req = {k: v for k, v in reqs[first].items() if k != "netlist"}
    sv = req["corners"]["supply_v"]
    req = json.loads(json.dumps(req))
    req["corners"]["supply_v"] = {"vdd": f"<{len(sv['vdd'])} entries>", "vcm": f"<{len(sv['vcm'])} entries>"}
    lines += ["* " + ln for ln in json.dumps(req, indent=1).splitlines()]
    lines += [
        "",
        "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
        dut_text,
        f"* ---- testbench: {TESTBENCH.relative_to(REPO_ROOT)} (verbatim; the driver rewrites only its .param line) ----",
        TESTBENCH.read_text(),
        "* ---- klt-generated deck of the first unit (corner.cir) ----",
        *("* | " + ln for ln in deck0.splitlines()),
        "",
    ]
    path.write_text("\n".join(lines))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--smoke", action="store_true", help="nominal point, local, no record")
    ap.add_argument("--recompute", metavar="RECORD_ID", help="re-derive a record's headline from its retained data")
    ap.add_argument("--backend", help="klt execution backend for the grids (default: klt's own resolution, "
                    "e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet capacity "
                    "(never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--keep-work", type=Path, default=None,
                    help="use this directory as the work dir and keep it (never committed); an existing one is "
                    "reused, and a cached report for an identical request is not re-submitted")


def headline(an: Analysis) -> dict:
    """The numbers a recompute must reproduce."""
    return {
        "intersection": [(i.lo_mv, i.hi_mv) for i in an.intersection],
        "component": None if an.component is None else (an.component.lo_mv, an.component.hi_mv),
        "verdict": an.verdict["verdict"], "n_pass_120": an.verdict["n_pass"],
        "per_combo": {fmt_combo(c): [(i.lo_mv, i.hi_mv) for i in v] for c, v in an.per_combo.items()},
    }


def recompute(record: str) -> int:
    base = HERE / "corners" / record
    if not base.is_dir():
        print(f"ERROR: {base} does not exist", file=sys.stderr)
        return 2
    table = build_table(load_retained(base / "data"))
    an = analyse(table)
    with (base / "samples.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    problems = compare_csv(table, rows)
    h = headline(an)
    print(f"record {record}: {len(table)} samples re-derived from retained data")
    print(f"  intersection {h['intersection']}, component {h['component']}, 1.20 V verdict {h['verdict']} "
          f"({h['n_pass_120']}/45 pass)")
    if problems:
        print("MISMATCH between retained data and samples.csv:", file=sys.stderr)
        for p in problems[:20]:
            print(f"  - {p}", file=sys.stderr)
        return 2
    print("  samples.csv agrees with the retained data (status, gain, margin)")
    return 0


def compare_csv(table: dict[Key, Sample], rows: list[dict]) -> list[str]:
    problems = []
    if len(rows) != len(table):
        problems.append(f"{len(rows)} CSV rows vs {len(table)} re-derived samples")
    by = {}
    for r in rows:
        by[(r["process"], float(r["temp_c"]), int(round(float(r["vdd_v"]) * 1000)), int(round(float(r["vcm_v"]) * 1000)))] = r
    for k, s in table.items():
        r = by.get(k)
        if r is None:
            problems.append(f"{fmt_key(k)} missing from samples.csv")
            continue
        if r["status"] != s.status() or r["status_strict"] != s.status(0.0):
            problems.append(f"{fmt_key(k)}: status {r['status']}/{r['status_strict']} vs re-derived "
                            f"{s.status()}/{s.status(0.0)}")
        if s.valid and (abs(float(r["gain_db"]) - s.gain_db) > 1e-4 or abs(float(r["min_margin_mv"]) - s.min_margin_v * 1e3) > 1e-3):
            problems.append(f"{fmt_key(k)}: gain/margin differ from the CSV")
    return problems


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="icmr-smoke-") as scratch:
        for vcm in (TARGET_VCM_MV, 1650):
            s, dm, cm = local_sample(f"smoke-{vcm}", pdk, Path(scratch), vcm)
            if not s.valid:
                print("SMOKE TEST FAILED: " + "; ".join(s.invalid_reasons))
                return 1
            print(f"  VCM {vcm / 1000:.3f} V: {s.status()}, plateau gain {s.gain_db:.2f} dB, limiting "
                  f"{s.limiting_device.upper()} {s.min_margin_v * 1e3:+.0f} mV, vout - VCM "
                  f"{(s.vout_v - vcm / 1000) * 1e3:+.3f} mV")
    print("smoke test OK (both excitations ran and validated; the full run records the evidence)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_args(ap)
    args = ap.parse_args(argv)
    if args.recompute:
        return recompute(args.recompute)
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    err = C.require_plotting()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record)
    ngspice, kver = ngspice_version(), klt_version()
    print(f"record {record}: PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    if args.keep_work is not None:
        args.keep_work.mkdir(parents=True, exist_ok=True)
        scratch_ctx = None
        work = args.keep_work.resolve()
    else:
        scratch_ctx = tempfile.TemporaryDirectory(prefix="icmr-")
        work = Path(scratch_ctx.name)
    try:
        return run_all(args, pdk, record, stamp, paths, ngspice, kver, work)
    finally:
        if scratch_ctx is not None:
            scratch_ctx.cleanup()


def run_all(args, pdk, record, stamp, paths, ngspice, kver, work: Path) -> int:
    reqs: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    walls: dict[str, float] = {}
    plans: list[Plan] = []
    retained: dict[Key, dict[str, Unit]] = {}
    problems: list[str] = []

    initial = [(v, c) for v in SUPPLIES_MV for c in scan_vcm_mv(v)]
    try:
        phase = phase_plans("scan", CORNERS, initial)
        run_requests(phase, pdk, work, args, reqs, reports, walls)
    except KltError as exc:
        print(f"ERROR: a scan request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
        return 2
    plans += phase
    problems += collect(phase, reports, retained)
    table = build_table(retained)

    rounds = 0
    while rounds < MAX_REFINE_ROUNDS:
        need = refinement_points(table, all_combos())
        if not need:
            break
        rounds += 1
        procs, pairs = refine_pairs(need)
        print(f"refinement round {rounds}: {len(pairs)} paired samples over {len(procs)} process corner(s)", flush=True)
        try:
            phase = phase_plans(f"refine{rounds}", procs, pairs)
            run_requests(phase, pdk, work, args, reqs, reports, walls)
        except KltError as exc:
            print(f"ERROR: a refinement request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        plans += phase
        problems += collect(phase, reports, retained)
        table = build_table(retained)
    left = refinement_points(table, all_combos())
    if left:
        problems.append(f"transition brackets still wider than {REFINE_STEP_MV} mV after {rounds} round(s): "
                        f"{sum(len(v) for v in left.values())} samples outstanding")
    problems += expected_table_keys(table)

    controls = run_controls(pdk, work, table)
    problems += controls.problems
    if problems:
        print("ERROR: the ICMR evidence did not validate; NO RECORD WRITTEN:", file=sys.stderr)
        for p in problems[:60]:
            print(f"  - {p}", file=sys.stderr)
        print(f"(work directory with all klt output: {work})", file=sys.stderr)
        return 2

    an = analyse(table)
    base = paths["corners"]
    base.mkdir(parents=True)
    write_samples_csv(base / "samples.csv", table)
    n_members = write_archives(base / "data", retained)
    for name in reqs:
        write_gz_json(base / "requests" / f"{name}.request.json.gz",
                      {k: v for k, v in reqs[name].items() if k != "netlist"})
        write_gz_json(base / "reports" / f"{name}.report.json.gz", strip_report(reports[name]))
    ctl_dir = base / "controls"
    ctl_dir.mkdir()
    for log in sorted(work.glob("*-dm/out/**/ngspice.log")) + sorted(work.glob("*-cm/out/**/ngspice.log")):
        top = log.relative_to(work).parts[0]
        if re.match(r"^(nominal|iso-|unequal-)", top):
            (ctl_dir / f"{top}.log").write_text(log.read_text())

    # Self-check: re-derive everything from the retained archives and compare.
    table2 = build_table(load_retained(base / "data"))
    with (base / "samples.csv").open() as fh:
        diffs = compare_csv(table2, list(csv.DictReader(fh)))
    an2 = analyse(table2)
    recheck_ok = not diffs and headline(an) == headline(an2)
    if not recheck_ok:
        print("ERROR: the retained data does not reproduce the in-memory result:", file=sys.stderr)
        for d in diffs[:10]:
            print(f"  - {d}", file=sys.stderr)
        return 2

    dut_sha = hashlib.sha256(load_dut_text().encode()).hexdigest()
    paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
    first_deck = next((Path(c["artifacts"]["deck"]) for rep in reports.values() for c in rep["corners"]
                       if (c.get("artifacts") or {}).get("deck") and Path(c["artifacts"]["deck"]).is_file()), None)
    write_snapshot(paths["snapshot"], record, reqs, first_deck.read_text() if first_deck else "")
    plots = build_plots(table, an, paths["plots"])
    md = build_record(record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, table=table, an=an,
                      controls=controls, reports=reports, walls=walls, plans=plans, rounds=rounds, plots=plots,
                      dut_sha=dut_sha, n_members=n_members, recheck_ok=recheck_ok)
    paths["record"].parent.mkdir(parents=True, exist_ok=True)
    paths["record"].write_text(md)

    h = headline(an)
    print(f"wrote {paths['record']}")
    print(f"  intersection: {h['intersection']} (mV); component containing 1.20 V: {h['component']}")
    print(f"  1.20 V: {h['verdict']} ({h['n_pass_120']}/45 pass)")
    print(f"  worst gain at 1.20 V: {sample_line(an.worst_target['gain'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
