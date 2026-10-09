#!/usr/bin/env python3
"""Common-mode rejection (CMRR) of the committed sized schematic across the
full ratified PVT grid (issue #39; tracker #7 items 5 and 9; DR-3 residual (e3)).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_cmrr.spice`) declares no transistor.

What it runs
------------
Two excitations of the same bench, each the 45-point grid `process {typical,
ff, ss, fs, sf} x temperature {-40, 27, 125 C} x supply {2.97, 3.30, 3.63 V}`
as ONE `klt sim` corner request (so two requests):

  * `dm`  differential: vinp = +0.5 V AC, vinn = -0.5 V AC (1 V differential
          about the same DC common mode);
  * `cm`  common mode: vinp = vinn = 1 V AC.

The DC loop is closed by an ideal servo that leaves vout unloaded and admits
equal AC drive on both input pins (see the testbench header); DC input common
mode is VDD/2 at every grid point. Which backend executes the grid is `klt`'s
decision (`--backend`, the request's `backend`, `$KLT_SIM_BACKEND`; the Spot
batch fleet on a dispatch worker). This script never launches an ngspice grid
itself and never falls back to a local grid when a batch submit fails -- it
stops with the error and writes no record. Only single-unit studies and
controls run locally.

Per point and frequency, the ACTUAL phasors on both DUT input pins of both
runs give vd = v(vinp) - v(vinn) and vc = (v(vinp) + v(vinn)) / 2, and

    vout_dm = Ad * vd_dm + Acm * vc_dm
    vout_cm = Ad * vd_cm + Acm * vc_cm

is solved exactly for Ad and Acm (a residual differential leak in the CM run
cannot masquerade as common-mode gain). CMRR = 20 log10 |Ad / Acm|.

Reported per point: CMRR at "DC" (the verified 0.1-1 Hz plateau, or flagged
unavailable), at 1 kHz, 10 kHz, 100 kHz, 1 MHz and at the differential
unity-gain frequency f_u (all by linear interpolation in (log10 f, dB)); the
worst-case corner of each figure. A perfectly matched schematic gives a
SYSTEMATIC-ONLY CMRR; the mismatch-limited figure needs Monte Carlo (DR-3
residual (e2)) and is out of scope here.

The result is MEASURED: no pass or fail verdict is issued because the row's
bound is not ratified. `spec/target-spec.md` is never edited.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<point>.<mode>.{dat,log,cir}   per point and excitation
    corners/<rid>/<point>.cmrr.dat               derived |Ad|, |Acm|, CMRR curves
    corners/<rid>/klt-report.<mode>.json         sanitised klt reports
    corners/<rid>/controls/...                   isolation, floor and negative controls
    netlist-snapshots/<rid>.spice                DUT + testbench + conditions
    netlist-snapshots/<rid>-controls.spice       the control DUT / fixtures, separately
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/cmrr/run_cmrr.py                  # full grid + record
    python3 sim/cmrr/run_cmrr.py --smoke          # nominal point, local, no record
    python3 sim/cmrr/run_cmrr.py --backend local  # force a backend

Exit status: 0 when the evidence is complete and validated; 2 when the grid
could not run or failed validation (no record is written).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

from harness import Pdk, allocate_record_id, find_pdk, ngspice_version  # noqa: E402

# The gain driver owns the committed-DUT guards, the klt plumbing and the grid
# bookkeeping; reuse them unchanged so the benches stay structurally identical.
if "gain_gbw_pm_driver" in sys.modules:
    G = sys.modules["gain_gbw_pm_driver"]
else:
    _spec = importlib.util.spec_from_file_location(
        "gain_gbw_pm_driver", REPO_ROOT / "sim" / "gain-gbw-pm" / "run_gain_gbw_pm.py"
    )
    G = importlib.util.module_from_spec(_spec)
    sys.modules["gain_gbw_pm_driver"] = G
    _spec.loader.exec_module(G)

TESTBENCH = HERE / "testbench" / "tb_cmrr.spice"
GAIN_DIR = REPO_ROOT / "sim" / "gain-gbw-pm"

CORNERS = G.CORNERS
TEMPS_C = G.TEMPS_C
SUPPLIES_V = G.SUPPLIES_V
NOMINAL = G.NOMINAL
DEVICES = G.DEVICES
Key = G.Key
KltError = G.KltError
fmt_key = G.fmt_key
point_key = G.point_key
point_stem = G.point_stem
expected_keys = G.expected_keys

# --------------------------------------------------------------------------
# Sweep, excitations, tolerances
# --------------------------------------------------------------------------

#: Same `.ac` sweep as the gain bench: 0.1 Hz (plateau) to 1 GHz (well past
#: the differential unity-gain frequency at every corner).
AC_FSTART, AC_FSTOP, AC_PPD = G.AC_FSTART, G.AC_FSTOP, G.AC_PPD
N_FREQ = int(round(math.log10(AC_FSTOP / AC_FSTART) * AC_PPD)) + 1

SERVO_NOMINAL = {"rsv": 1e9, "csv": 1e9}  # tau = 1e18 s
MODES = {
    "dm": {"acp": 0.5, "acn": -0.5},
    "cm": {"acp": 1.0, "acn": 1.0},
}
SPOT_HZ = (1e3, 1e4, 1e5, 1e6)

PLATEAU_DECADE_HI = 10.0  # "DC" band = [f0, 10 f0] = 0.1-1 Hz
PLATEAU_TOL_DB = 0.10
#: Actual input phasors must equal the intended excitation to this (V).
EXC_TOL = 1e-6
#: Common-mode run: residual differential |vd / vc| allowed (unequal drive).
CM_LEAK_MAX = 1e-6
#: Operating point (blocking): vout within this of VCM, and every excitation
#: of the same point at the same DC operating point.
OP_VOUT_TOL_V = 0.10
OP_MODE_AGREE_V = 1e-6
#: Ad cross-check against the gain bench's committed open-loop data.
TOL_GAIN_BENCH_DB = 0.05
#: Local nominal unit vs the grid's nominal point (engine/environment parity).
TOL_LOCAL_VS_GRID_DB = 0.05
#: Servo-isolation study: tau varied 1e16..1e20 s must move nothing.
ISOLATION_CSV = (1e7, 1e11)
ISOLATION_INADEQUATE_CSV = 1e-12  # tau = 1e-3 s: the loop closes at AC
ISOLATION_TOL_DB = 0.01
#: Numerical floor: a response below FLOOR_MARGIN x the demonstrated
#: superposition residual is reported as a lower bound, never as a ratio.
FLOOR_MARGIN = 10.0
FLOOR_MIN = 1e-15
#: A negative control must lower the nominal low-frequency figure by this.
CONTROL_MIN_DROP_DB = 6.0
#: Mirror-imbalance control: the diode-connected load XM3's width, -10 %.
#: (Chosen after checking nominal behaviour: the systematic Acm is a signed
#: sum, so the opposite imbalance, +10 %, partially CANCELS it and raises the
#: CMRR; that run is recorded as information, without a criterion.)
CONTROL_MIRROR = ("XM3", "W", "6u", "5.4u")
CONTROL_MIRROR_INFO = ("XM3", "W", "6u", "6.6u")

#: Appended to `analysis.args`: klt places it verbatim in the `.control`
#: block after `ac`. It prints the DC operating point of the same deck into
#: the retained log (vout, inputs, every DUT device's Vds and Vdsat), then
#: re-selects the ac plot so klt's `write` dumps the AC response.
OP_PRINT = ["v(vout)", "v(vinp)", "v(vinn)"] + [
    f"@m.xdut.{d}.m0[{p}]" for d in DEVICES for p in ("vds", "vdsat")
]
OP_TAIL = "\nop\nprint " + " ".join(OP_PRINT) + "\nsetplot ac1"


# --------------------------------------------------------------------------
# Testbench guards and materialisation
# --------------------------------------------------------------------------

_SERVO_LINES = (
    r"^Esb\s+vsb\s+0\s+vout\s+0\s+1$",
    r"^Rsv\s+vsb\s+vsv\s+\{rsv\}$",
    r"^Csv\s+vsv\s+0\s+\{csv\}$",
    r"^Esv\s+vinn\s+vnac\s+vsv\s+0\s+1$",
    r"^Vnac\s+vnac\s+0\s+dc\s+0\s+ac\s+\{acn\}$",
)


def code_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("*")]


def _require(code: list[str], pattern: str, what: str, errs: list[str]) -> None:
    if not any(re.match(pattern, ln, re.I) for ln in code):
        errs.append(what)


def guard_common(text: str) -> list[str]:
    """Shared guards for the CMRR and PSRR benches (empty list = ok)."""
    errs = G.guard_testbench(text)
    code = code_lines(text)
    for pat in _SERVO_LINES:
        _require(code, pat, f"testbench lost the DC servo line `{pat}`", errs)
    _require(code, r"^CL\s+vout\s+0\s+2p$", "testbench lost CL = 2 pF on vout", errs)
    _require(code, r"^Ibias\s+vdd\s+ibias\s+dc\s+10u$", "testbench lost Ibias = 10 uA", errs)
    _require(code, r"^Vcm\s+vinp\s+0\s+dc\s+\S+\s+ac\s+\{acp\}$", "testbench lost `Vcm vinp 0 dc ... ac {acp}`", errs)
    if any(re.match(r"^Lfb\b", ln, re.I) or re.match(r"^Cfb\b", ln, re.I) for ln in code):
        errs.append("the gain bench's Lfb/Cfb arrangement AC-grounds vinn; it must not be used here")
    params = [ln for ln in code if ln.lower().startswith(".param")]
    if len(params) != 1:
        errs.append(f"expected exactly one .param line, found {len(params)}")
    return errs


def guard_testbench(text: str) -> list[str]:
    errs = guard_common(text)
    code = code_lines(text)
    _require(code, r"^Xdut\s+vdd\s+0\s+vinp\s+vinn\s+vout\s+ibias\s+opamp_two_stage$",
             "Xdut must be `Xdut vdd 0 vinp vinn vout ibias opamp_two_stage`", errs)
    _require(code, r"^Vdd\s+vdd\s+0\s+dc\s+\S+$", "testbench lost the AC-quiet `Vdd vdd 0 dc ...`", errs)
    return errs


def param_keys(text: str) -> list[str]:
    m = re.search(r"^\.param\s+(.*)$", text, re.M | re.I)
    if not m:
        raise RuntimeError("testbench has no .param line")
    return [kv.split("=", 1)[0].strip().lower() for kv in m.group(1).split()]


def param_line(keys: list[str], params: dict[str, float]) -> str:
    if sorted(keys) != sorted(params):
        raise RuntimeError(f".param keys {sorted(params)} do not match the testbench's {sorted(keys)}")
    return ".param " + " ".join(f"{k}={params[k]:.12g}" for k in keys)


def load_dut_text() -> str:
    return G.load_dut_text()


def mutate_instance(dut_text: str, inst: str, param: str, old: str, new: str) -> str:
    """A control DUT: one parameter of one device changed, nothing else.

    The production export is never written; the mutated text only ever lives
    in a control work directory and the controls snapshot.
    """
    out: list[str] = []
    n = 0
    for line in dut_text.splitlines():
        first = line.split(None, 1)[0].lower() if line.strip() else ""
        if first == inst.lower():
            new_line, k = re.subn(rf"(\s){param}={re.escape(old)}(\s)", rf"\g<1>{param}={new}\g<2>", line)
            if k != 1:
                raise RuntimeError(f"{inst}: `{param}={old}` not found exactly once")
            line = new_line
            n += 1
        out.append(line)
    if n != 1:
        raise RuntimeError(f"expected exactly one {inst} in the DUT, found {n}")
    return "\n".join(out) + "\n"


def materialise(
    work: Path,
    pdk: Pdk,
    params: dict[str, float],
    *,
    testbench: Path = TESTBENCH,
    guard: Callable[[str], list[str]] = guard_testbench,
    dut_text: str | None = None,
    allow_missing: tuple[str, ...] = (),
    fixture: tuple[str, ...] = (),
) -> Path:
    """Write the per-run work directory and return the testbench path.

    The committed testbench is used verbatim except for (a) the two `.include`
    targets, rewritten to absolute paths in `work`, (b) its single `.param`
    line, rewritten to the excitation / servo values `params` (every key must
    be the testbench's own), and (c) `fixture` lines appended for a stimulus
    -fixture control only. `dut_text` overrides the DUT only for a control.
    """
    src = testbench.read_text()
    errs = guard(src)
    if errs:
        raise RuntimeError("testbench guard failed:\n  " + "\n  ".join(errs))
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdk.design_include, work / "design.ngspice")
    dut = load_dut_text() if dut_text is None else dut_text
    derrs = G.guard_dut(dut, allow_missing=allow_missing)
    if derrs:
        raise RuntimeError("DUT guard failed:\n  " + "\n  ".join(derrs))
    (work / G.DUT_INCLUDE_NAME).write_text(dut)
    tb = src.replace("'design.ngspice'", f"'{work / 'design.ngspice'}'")
    tb = tb.replace("'opamp_two_stage.dut.spice'", f"'{work / G.DUT_INCLUDE_NAME}'")
    tb, n = re.subn(r"^\.param\s+.*$", param_line(param_keys(src), params), tb, flags=re.M | re.I)
    if n != 1:
        raise RuntimeError("testbench .param line not found exactly once")
    if fixture:
        block = "\n* ---- control fixture (NOT part of the baseline bench) ----\n" + "\n".join(fixture) + "\n"
        tb += block
        ferrs = G.guard_testbench(src + block)
        if ferrs:
            raise RuntimeError("fixture guard failed:\n  " + "\n  ".join(ferrs))
    path = work / "tb.spice"
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt request / run
# --------------------------------------------------------------------------


def ac_request(netlist: Path, pdk: Pdk, corners, temps, supplies, *, local: bool = False) -> dict:
    """The gain bench's request (grid, corner bundles, vcm = VDD/2), with the
    operating-point print appended and no `.meas` cards.

    `local` pins the model library to the harness-resolved PDK root (klt's own
    lookup prefers ~/.ciel over ~/.volare; the fleet uses its own install,
    reported in the klt provenance)."""
    req = G.ac_request(netlist, pdk, corners, temps, supplies)
    req["measurements"] = []
    req["analysis"]["args"] = req["analysis"]["args"] + OP_TAIL
    if local:
        req["models"]["pdk_root"] = str(pdk.path.parent)
    return req


def local_env(scratch: Path) -> dict[str, str]:
    """Environment for LOCAL single units: HOME is an empty scratch directory.

    ngspice reads `~/.spiceinit`; a host-level one (e.g. `set wnflag=1`) would
    change the model binning of the local units but not of the fleet grid, and
    DR-0004 keeps every sim/ deck at ngspice defaults. Nothing else in a local
    unit reads HOME (the PDK root is passed explicitly)."""
    home = scratch / "clean-home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def run_klt(request: dict, outdir: Path, backend: str | None, workdir: Path, env: dict | None = None) -> dict:
    """`klt sim` on a request dict; returns its JSON report (see the gain driver)."""
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    req_path = workdir / f"{outdir.name}.request.json"
    req_path.write_text(json.dumps(request, indent=2))
    cmd = ["klt", "sim", str(req_path), "-o", str(outdir), "--format", "json"]
    if backend:
        cmd += ["--backend", backend]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise KltError(
            f"klt sim produced no JSON report (exit {proc.returncode}):\n"
            f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
        ) from exc
    if "error" in report and "corners" not in report:
        raise KltError(f"klt sim error: {report['error']}")
    report["_exit_code"] = proc.returncode
    return report


def run_klt_retrying(request, outdir, backend, workdir, *, retries: int, wait_s: float) -> dict:
    """Re-submit only when the BATCH submit was refused for capacity (nothing
    ran); never changes backend. Any other error propagates."""
    for attempt in range(retries + 1):
        try:
            return run_klt(request, outdir, backend, workdir)
        except KltError as exc:
            msg = str(exc)
            if attempt < retries and any(t in msg for t in G._TRANSIENT_SUBMIT):
                print(f"  batch submit refused ({attempt + 1}/{retries + 1}); retrying in {wait_s:g}s: {msg[-160:]}", flush=True)
                time.sleep(wait_s)
                continue
            raise
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Report parsing: per point, per excitation
# --------------------------------------------------------------------------

_OP_RE = re.compile(r"^(\S+)\s*=\s*([-+]?[0-9.]+(?:[eE][-+]?\d+)?)\s*$", re.M)


def parse_op_log(text: str) -> dict[str, float]:
    """The operating point printed by OP_TAIL into the ngspice log."""
    out: dict[str, float] = {}
    for name, val in _OP_RE.findall(text):
        if name.lower() in {p.lower() for p in OP_PRINT}:
            out[name.lower()] = float(val)
    return out


@dataclass
class OpCheck:
    vout_v: float
    vcm_v: float
    sat_margin_v: dict[str, float]
    problems: list[str]  # blocking: missing / output not at VCM
    flags: list[str]  # recorded: a device below saturation


def check_op(vals: dict[str, float], vcm: float, label: str) -> OpCheck:
    probs: list[str] = []
    flags: list[str] = []
    missing = [p for p in OP_PRINT if p.lower() not in vals]
    if missing:
        probs.append(f"{label}: operating point not returned ({', '.join(missing[:3])}...)")
        return OpCheck(float("nan"), vcm, {}, probs, flags)
    vout = vals["v(vout)"]
    if abs(vout - vcm) > OP_VOUT_TOL_V:
        probs.append(f"{label}: vout {vout:.4f} V is {vout - vcm:+.4f} V from VCM {vcm:.3f} V "
                     "(saturated or differently biased DUT)")
    sat = {}
    for d in DEVICES:
        m = abs(vals[f"@m.xdut.{d}.m0[vds]"]) - abs(vals[f"@m.xdut.{d}.m0[vdsat]"])
        sat[d] = m
        if m < 0:
            flags.append(f"{d.upper()[1:]} below saturation (|Vds|-|Vdsat| = {m * 1e3:+.0f} mV)")
    return OpCheck(vout, vcm, sat, probs, flags)


def analyse_mode_report(report: dict, want: list[Key], mode: str, need: tuple[str, ...]):
    """{key: {vec, op, arts}} for one excitation's grid report, + problems.

    Every expected point must be present exactly once with a retained
    rawfile and log; anything else is a failed simulation.
    """
    problems: list[str] = []
    out: dict[Key, dict] = {}
    seen: dict[Key, dict] = {}
    for c in report.get("corners", []):
        k = point_key(c)
        if k in seen:
            problems.append(f"[{mode}] duplicate result for {fmt_key(k)}")
        seen[k] = c
    for k in want:
        c = seen.get(k)
        if c is None:
            problems.append(f"[{mode}] missing result for {fmt_key(k)}")
            continue
        diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
        a = c.get("artifacts") or {}
        raw, logp = a.get("raw"), a.get("log")
        if not raw or not Path(raw).is_file():
            problems.append(f"[{mode}] simulation failed for {fmt_key(k)}: {diag or 'no rawfile retained'}")
            continue
        if not logp or not Path(logp).is_file():
            problems.append(f"[{mode}] no ngspice log retained for {fmt_key(k)}")
            continue
        try:
            vec = G.parse_ascii_complex_raw(Path(raw).read_text())
        except ValueError as exc:
            problems.append(f"[{mode}] malformed data for {fmt_key(k)}: {exc}")
            continue
        miss = [n for n in ("frequency", *need) if n not in vec]
        if miss:
            problems.append(f"[{mode}] rawfile for {fmt_key(k)} lacks {', '.join(miss)}")
            continue
        vcm = float((c.get("supply_v") or {}).get("vcm", k[2] / 2))
        op = check_op(parse_op_log(Path(logp).read_text()), vcm, f"[{mode}] {fmt_key(k)}")
        problems += op.problems
        out[k] = {"vec": vec, "op": op, "log": logp, "deck": a.get("deck"), "raw": raw}
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"[{mode}] unexpected extra points: {sorted(extra)}")
    return out, problems


def op_agreement(per_mode: dict[str, dict[Key, dict]], want: list[Key]) -> tuple[float, list[str]]:
    """Every excitation of a point must sit at the same DC operating point."""
    worst = 0.0
    bad: list[str] = []
    for k in want:
        vs = [per_mode[m][k]["op"].vout_v for m in per_mode if k in per_mode[m]]
        vs = [v for v in vs if math.isfinite(v)]
        if len(vs) >= 2:
            d = max(vs) - min(vs)
            worst = max(worst, d)
            if d > OP_MODE_AGREE_V:
                bad.append(f"{fmt_key(k)}: DC vout differs by {d * 1e6:.2f} uV between excitations")
    return worst, bad


# --------------------------------------------------------------------------
# Extraction (pure functions; unit-tested on synthetic responses)
# --------------------------------------------------------------------------


class ExtractionError(ValueError):
    pass


def check_axis(freq: np.ndarray, n_expect: int | None = N_FREQ) -> None:
    if freq.ndim != 1 or (n_expect is not None and freq.size != n_expect):
        raise ExtractionError(f"{freq.size} frequency points, expected {n_expect}")
    if not np.all(np.isfinite(freq)) or np.any(freq <= 0) or np.any(np.diff(freq) <= 0):
        raise ExtractionError("frequency axis not finite, positive and strictly increasing")


def same_axis(f1: np.ndarray, f2: np.ndarray) -> None:
    if f1.shape != f2.shape or np.any(np.abs(f1 / f2 - 1) > 1e-9):
        raise ExtractionError("excitations were run on different frequency axes")


def finite(*arrs: np.ndarray) -> None:
    for a in arrs:
        if not np.all(np.isfinite(a)):
            raise ExtractionError("non-finite phasor data")


def db(x) -> np.ndarray:
    return 20.0 * np.log10(np.abs(x))


def interp_db(freq: np.ndarray, ydb: np.ndarray, f: float) -> float:
    """Linear interpolation in (log10 f, dB); ValueError outside the sweep."""
    if f < freq[0] * (1 - 1e-9) or f > freq[-1] * (1 + 1e-9):
        raise ExtractionError(f"{f:g} Hz is outside the swept range")
    return float(np.interp(math.log10(f), np.log10(freq), ydb))


@dataclass
class Rejection:
    """One rejection figure (CMRR, PSRR+ or PSRR-) at one point."""

    plateau_ok: bool
    dc_db: float  # mean over the plateau band; NaN when no plateau
    plateau_spread_db: float
    low_db: float  # value at the lowest swept frequency
    spot_db: dict[float, float]
    at_fu_db: float
    fu_hz: float
    lower_bound: bool  # some reported value is limited by the numerical floor
    floor: float
    err_phase_deg: float = float("nan")  # phase of A_err at the lowest frequency
    err_low_mag: float = float("nan")  # |A_err| at the lowest frequency (V/V)
    curve_db: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    err_db: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))

    def figure(self, which: str) -> float:
        if which == "dc":
            return self.dc_db
        if which == "fu":
            return self.at_fu_db
        return self.spot_db[float(which)]


def summarise_rejection(freq: np.ndarray, ad: np.ndarray, a_err: np.ndarray, fu_hz: float, floor: float) -> Rejection:
    """Rejection = 20 log10 |Ad / A_err| with the documented summaries.

    * A_err below `floor` (the demonstrated numerical floor) is clamped to the
      floor, and every value derived from a clamped sample is a LOWER BOUND.
      There is never a division by zero and never an "infinite" rejection.
    * "DC" = mean dB over [f0, 10 f0], valid only if both the rejection and
      |A_err| are flat there to PLATEAU_TOL_DB; else reported unavailable.
    * Spot values and the value at f_u: linear interpolation in (log10 f, dB).
    """
    if floor <= 0 or not math.isfinite(floor):
        raise ExtractionError("numerical floor must be positive and finite")
    mag_err = np.abs(a_err)
    clamped = mag_err < floor
    err_eff = np.where(clamped, floor, mag_err)
    if np.any(np.abs(ad) <= 0):
        raise ExtractionError("zero differential gain in the sweep")
    curve = db(ad) - db(err_eff)
    band = freq <= freq[0] * PLATEAU_DECADE_HI
    if band.sum() < 3:
        raise ExtractionError("fewer than 3 points in the plateau band")
    spread = float(curve[band].max() - curve[band].min())
    err_spread = float(db(err_eff)[band].max() - db(err_eff)[band].min())
    ok = spread <= PLATEAU_TOL_DB and err_spread <= PLATEAU_TOL_DB
    lb = bool(clamped[band].any())

    def at(f: float) -> tuple[float, bool]:
        i = int(np.searchsorted(freq, f))
        near = clamped[max(i - 1, 0): min(i + 1, len(freq))].any()
        return interp_db(freq, curve, f), bool(near)

    spots: dict[float, float] = {}
    for f in SPOT_HZ:
        spots[f], c = at(f)
        lb |= c
    at_fu, c = at(fu_hz)
    lb |= c
    return Rejection(
        plateau_ok=ok, dc_db=float(np.mean(curve[band])) if ok else float("nan"),
        plateau_spread_db=max(spread, err_spread), low_db=float(curve[0]), spot_db=spots,
        at_fu_db=at_fu, fu_hz=fu_hz, lower_bound=lb, floor=floor, curve_db=curve, err_db=db(err_eff),
        err_phase_deg=float(np.angle(a_err[0], deg=True)), err_low_mag=float(mag_err[0]),
    )


@dataclass
class Excitation:
    """Actual input-pin phasors of one excitation run."""

    vd: np.ndarray
    vc: np.ndarray
    vout: np.ndarray

    @classmethod
    def from_vec(cls, vec: dict[str, np.ndarray]) -> "Excitation":
        vp, vn = vec["v(vinp)"], vec["v(vinn)"]
        return cls(vd=vp - vn, vc=(vp + vn) / 2, vout=vec["v(vout)"])


def check_dm(x: Excitation) -> tuple[float, list[str]]:
    """Differential run: vd = 1 V and vc = 0 at every frequency."""
    err = float(max(np.max(np.abs(x.vd - 1)), np.max(np.abs(x.vc))))
    return err, ([] if err <= EXC_TOL else
                 [f"differential excitation off by {err:.3g} V (> {EXC_TOL:g}): servo isolation or wiring"])


def check_cm(x: Excitation) -> tuple[float, float, list[str]]:
    """Common-mode run: vc = 1 V; residual differential |vd/vc| <= CM_LEAK_MAX."""
    err = float(np.max(np.abs(x.vc - 1)))
    if np.any(np.abs(x.vc) < 1e-12):
        return err, float("inf"), ["common-mode run has no common-mode input (vc ~ 0)"]
    leak = float(np.max(np.abs(x.vd / x.vc)))
    bad = []
    if err > EXC_TOL:
        bad.append(f"common-mode excitation off by {err:.3g} V (> {EXC_TOL:g})")
    if leak > CM_LEAK_MAX:
        bad.append(f"common-mode run carries a residual differential input |vd/vc| = {leak:.3g} "
                   f"(> {CM_LEAK_MAX:g}): unequal CM drive")
    return err, leak, bad


def solve_ad_acm(dm: Excitation, cm: Excitation) -> tuple[np.ndarray, np.ndarray]:
    """Exact per-frequency solve of vout = Ad vd + Acm vc for the two runs."""
    det = dm.vd * cm.vc - dm.vc * cm.vd
    if np.any(np.abs(det) < 1e-6):
        raise ExtractionError("the two excitations are not independent (singular excitation matrix)")
    ad = (dm.vout * cm.vc - cm.vout * dm.vc) / det
    acm = (dm.vd * cm.vout - cm.vd * dm.vout) / det
    return ad, acm


@dataclass
class CmrrPoint:
    valid: bool
    reason: str = ""
    freq: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    ad: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    acm: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    ad_dc_db: float = float("nan")
    fu_hz: float = float("nan")
    cmrr: Rejection | None = None
    dm_err: float = float("nan")
    cm_err: float = float("nan")
    cm_leak: float = float("nan")
    naive_dev_db: float = float("nan")  # |CMRR(naive vout/vc) - CMRR(solved)| max over sweep
    acm_dc_db: float = float("nan")


def extract_cmrr(freq_dm, dm_vec: dict, freq_cm, cm_vec: dict, floor: float, *, n_expect: int | None = N_FREQ) -> CmrrPoint:
    """Ad, Acm and CMRR of one point from its two excitation runs.

    Any validation failure returns an INVALID point with the reason; invalid
    points block the record."""
    try:
        freq = np.asarray(freq_dm, dtype=float)
        check_axis(freq, n_expect)
        same_axis(freq, np.asarray(freq_cm, dtype=float))
        dm, cm = Excitation.from_vec(dm_vec), Excitation.from_vec(cm_vec)
        finite(dm.vd, dm.vc, dm.vout, cm.vd, cm.vc, cm.vout)
        dm_err, b1 = check_dm(dm)
        cm_err, leak, b2 = check_cm(cm)
        if b1 or b2:
            return CmrrPoint(False, "; ".join(b1 + b2), freq=freq, dm_err=dm_err, cm_err=cm_err, cm_leak=leak)
        ad, acm = solve_ad_acm(dm, cm)
        m = G.extract_metrics(freq, ad)
        if not m.valid:
            return CmrrPoint(False, f"differential response invalid: {m.reason}", freq=freq)
        rej = summarise_rejection(freq, ad, acm, m.gbw_hz, floor)
        naive = cm.vout / cm.vc
        clamp = lambda x: np.maximum(np.abs(x), floor)  # noqa: E731
        naive_dev = float(np.max(np.abs(db(clamp(naive)) - db(clamp(acm)))))
        return CmrrPoint(
            True, freq=freq, ad=ad, acm=acm, ad_dc_db=m.dc_gain_db, fu_hz=m.gbw_hz, cmrr=rej,
            dm_err=dm_err, cm_err=cm_err, cm_leak=leak, naive_dev_db=naive_dev,
            acm_dc_db=float(np.mean(rej.err_db[freq <= freq[0] * PLATEAU_DECADE_HI])),
        )
    except (ExtractionError, KeyError) as exc:
        return CmrrPoint(False, str(exc))


# --------------------------------------------------------------------------
# Cross-check against the gain bench's committed open-loop data
# --------------------------------------------------------------------------


def latest_gain_dir() -> Path | None:
    base = GAIN_DIR / "corners"
    if not base.is_dir():
        return None
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        if len(list(d.glob("*_*c_*v.dat"))) >= 45:
            return d
    return None


def gain_bench_dev_db(gdir: Path | None, k: Key, freq: np.ndarray, h: np.ndarray,
                     fmax: float | None = None) -> float | None:
    """max | |h| - |gain-bench vout/vdiff| | in dB over the sweep (up to
    `fmax` if given), or None when the committed data is unavailable.

    The gain bench drives vinp alone (vd = 1, vc = 0.5), so its vout/vd is
    Ad + Acm/2, not Ad: the CMRR driver compares exactly that over the whole
    sweep; the PSRR driver (no Acm) compares Ad up to f_u, where
    |Acm/2| <= |Ad| x 1e-3 at every corner."""
    if gdir is None:
        return None
    p = gdir / f"{point_stem(k)}.dat"
    if not p.is_file():
        return None
    d = np.loadtxt(p)
    if d.shape[0] != len(freq) or np.any(np.abs(d[:, 0] / freq - 1) > 1e-6):
        return None
    ref = d[:, 1] + 1j * d[:, 2]
    sel = np.ones(freq.shape, bool) if fmax is None else freq <= fmax * (1 + 1e-9)
    return float(np.max(np.abs(db(h[sel]) - db(ref[sel]))))


# --------------------------------------------------------------------------
# Local single-unit runs (nominal point only)
# --------------------------------------------------------------------------


@dataclass
class LocalRun:
    name: str
    desc: str
    vec: dict | None = None
    op: OpCheck | None = None
    error: str = ""
    files: dict = field(default_factory=dict)


def run_local(name: str, desc: str, pdk: Pdk, work: Path, params: dict, *, testbench: Path = TESTBENCH,
              guard=guard_testbench, need=("v(vout)", "v(vinp)", "v(vinn)"), dut_text=None,
              allow_missing=(), fixture=()) -> LocalRun:
    """ONE nominal-corner `.ac` unit, run LOCALLY through `klt sim`."""
    run = LocalRun(name, desc)
    try:
        wd = work / name
        tb = materialise(wd, pdk, params, testbench=testbench, guard=guard, dut_text=dut_text,
                         allow_missing=allow_missing, fixture=fixture)
        p, t, v = NOMINAL
        req = ac_request(tb, pdk, [p], [t], [v], local=True)
        rep = run_klt(req, wd / "out", "local", wd, env=local_env(work))
        res, probs = analyse_mode_report(rep, [NOMINAL], name, need)
        if NOMINAL in res:
            run.vec = res[NOMINAL]["vec"]
            run.op = res[NOMINAL]["op"]
            a = res[NOMINAL]
            run.files = {"log": a["log"], "deck": a["deck"], "raw": a["raw"]}
        run.error = "; ".join(probs)
    except (KltError, RuntimeError, ValueError) as exc:
        run.error = str(exc)
    return run


def with_servo(mode_params: dict, **over) -> dict:
    p = {**SERVO_NOMINAL, **mode_params}
    p.update(over)
    return p


def superposition_floor(both: LocalRun, parts: list[LocalRun]) -> tuple[float, str]:
    """Numerical floor demonstrated at the nominal point.

    The AC solution is linear, so the response to two sources driven together
    must equal the sum of the responses to each alone. The largest deviation
    (V per V of drive) over the sweep is the observed numerical noise of an
    output that is itself the cancellation of two large terms; FLOOR_MARGIN x
    that is the floor below which a response is not resolved."""
    if both.vec is None or any(p.vec is None for p in parts):
        return float("nan"), "superposition runs did not complete: " + "; ".join(
            r.error for r in [both, *parts] if r.error)
    total = sum(p.vec["v(vout)"] for p in parts)
    resid = float(np.max(np.abs(both.vec["v(vout)"] - total)))
    return resid, ""


# --------------------------------------------------------------------------
# CMRR studies and controls (local, nominal)
# --------------------------------------------------------------------------


@dataclass
class Studies:
    nominal: CmrrPoint | None
    floor: float
    floor_resid: float
    iso: list[tuple[str, CmrrPoint]]
    inadequate: CmrrPoint | None
    unequal: CmrrPoint | None
    unequal_naive_db: float
    mirror: CmrrPoint | None
    mirror_info: CmrrPoint | None
    runs: list[LocalRun]
    problems: list[str]


def cmrr_pair(pdk, work, tag, desc, floor, **kw) -> tuple[CmrrPoint, list[LocalRun]]:
    over = kw.pop("over", {})
    cm_params = kw.pop("cm_params", MODES["cm"])
    dm = run_local(f"{tag}-dm", f"{desc} (differential)", pdk, work, with_servo(MODES["dm"], **over), **kw)
    cm = run_local(f"{tag}-cm", f"{desc} (common mode)", pdk, work, with_servo(cm_params, **over), **kw)
    if dm.vec is None or cm.vec is None:
        return CmrrPoint(False, f"did not simulate: {dm.error or cm.error}"), [dm, cm]
    return extract_cmrr(dm.vec["frequency"].real, dm.vec, cm.vec["frequency"].real, cm.vec, floor), [dm, cm]


def run_cmrr_studies(pdk: Pdk, work: Path) -> Studies:
    probs: list[str] = []
    runs: list[LocalRun] = []
    # Numerical floor: CM drive vs vinp-only + vinn-only.
    sp = run_local("floor-p-only", "vinp only (acp=1, acn=0)", pdk, work, with_servo({"acp": 1.0, "acn": 0.0}))
    sn = run_local("floor-n-only", "vinn only (acp=0, acn=1)", pdk, work, with_servo({"acp": 0.0, "acn": 1.0}))
    sb = run_local("floor-both", "both (acp=acn=1)", pdk, work, with_servo(MODES["cm"]))
    runs += [sp, sn, sb]
    resid, err = superposition_floor(sb, [sp, sn])
    if err:
        probs.append(err)
        floor = FLOOR_MIN
    else:
        floor = max(FLOOR_MARGIN * resid, FLOOR_MIN)

    nominal, r = cmrr_pair(pdk, work, "nominal", "nominal servo", floor)
    runs += r
    if not nominal.valid:
        probs.append(f"local nominal pair invalid: {nominal.reason}")
    iso = []
    for c in ISOLATION_CSV:
        pt, r = cmrr_pair(pdk, work, f"iso-csv{c:g}", f"servo tau = {SERVO_NOMINAL['rsv'] * c:g} s", floor, over={"csv": c})
        runs += r
        iso.append((f"tau = {SERVO_NOMINAL['rsv'] * c:g} s", pt))
    inad, r = cmrr_pair(pdk, work, "iso-inadequate", "servo tau = 1e-3 s (inadequate)", floor,
                        over={"csv": ISOLATION_INADEQUATE_CSV})
    runs += r
    # Unequal CM drive: 1 % less on vinn.
    uneq, r = cmrr_pair(pdk, work, "unequal-cm", "CM drive acp = 1, acn = 0.99", floor,
                        cm_params={"acp": 1.0, "acn": 0.99})
    runs += r
    uneq_naive = float("nan")
    if r[0].vec is not None and r[1].vec is not None and nominal.valid:
        x = Excitation.from_vec(r[1].vec)
        uneq_naive = float(db(nominal.ad[0] / (x.vout[0] / x.vc[0])))
    # Mirror-imbalance control DUT.
    inst, prm, old, new = CONTROL_MIRROR
    mdut = mutate_instance(load_dut_text(), inst, prm, old, new)
    mirror, r = cmrr_pair(pdk, work, "control-mirror", f"control DUT: {inst} {prm} {old} -> {new}", floor,
                          dut_text=mdut, allow_missing=(inst.lower(),))
    runs += r
    inst, prm, old, new = CONTROL_MIRROR_INFO
    idut = mutate_instance(load_dut_text(), inst, prm, old, new)
    mirror_info, r = cmrr_pair(pdk, work, "info-mirror-opposite", f"control DUT: {inst} {prm} {old} -> {new}", floor,
                               dut_text=idut, allow_missing=(inst.lower(),))
    runs += r

    # --- validation of the studies (each blocking) ---
    if nominal.valid:
        for label, pt in iso:
            if not pt.valid:
                probs.append(f"isolation {label}: invalid ({pt.reason})")
                continue
            d = max(abs(pt.cmrr.low_db - nominal.cmrr.low_db),
                    max(abs(pt.cmrr.spot_db[f] - nominal.cmrr.spot_db[f]) for f in SPOT_HZ),
                    abs(pt.cmrr.at_fu_db - nominal.cmrr.at_fu_db))
            if d > ISOLATION_TOL_DB:
                probs.append(f"isolation {label}: CMRR moves {d:.4f} dB (> {ISOLATION_TOL_DB} dB)")
    if inad.valid:
        probs.append("inadequate servo isolation was NOT detected by the excitation checks")
    if uneq.valid:
        probs.append("unequal CM drive was NOT detected by the excitation checks")
    if not mirror.valid:
        probs.append(f"mirror-imbalance control did not produce a valid measurement: {mirror.reason}")
    elif nominal.valid:
        drop = nominal.cmrr.low_db - mirror.cmrr.low_db
        if drop < CONTROL_MIN_DROP_DB:
            probs.append(f"mirror-imbalance control lowered CMRR by only {drop:.2f} dB (< {CONTROL_MIN_DROP_DB} dB)")
    return Studies(nominal, floor, resid if not err else float("nan"), iso, inad, uneq, uneq_naive, mirror, mirror_info,
                   runs, probs)


# --------------------------------------------------------------------------
# Worst case
# --------------------------------------------------------------------------

FIGURES = (("dc", "DC plateau (0.1-1 Hz)"), *((f"{f:g}", f"{f / 1e3:g} kHz" if f < 1e6 else f"{f / 1e6:g} MHz") for f in SPOT_HZ),
           ("fu", "at f_u (differential unity gain)"))


def worst_case(values: dict[Key, Rejection], which: str) -> tuple[float, Key | None, int]:
    """(lowest value, its corner, number of points without the figure)."""
    items = [(r.figure(which), k) for k, r in values.items() if math.isfinite(r.figure(which))]
    missing = len(values) - len(items)
    if not items:
        return float("nan"), None, missing
    v, k = min(items)
    return v, k, missing


def fmt_db(v: float, lb: bool = False) -> str:
    if not math.isfinite(v):
        return "n/a"
    return (">= " if lb else "") + f"{v:.2f}"


def remote_of(report: dict) -> dict:
    return (report.get("environment") or {}).get("remote") or {}


def klt_pdk_version(report: dict) -> str:
    return ((report.get("provenance") or {}).get("pdk") or {}).get("version", "n/a")


def pdk_lines(pdk: Pdk, reports: dict[str, dict]) -> list[str]:
    """PDK provenance, stating exactly what each source does and does not say."""
    client = sorted({f"{klt_pdk_version(r)} ({((r.get('provenance') or {}).get('pdk') or {}).get('source', '?')})"
                     for r in reports.values()})
    return [
        f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (harness `find_pdk`, via {pdk.source}); the "
        "local single units are pinned to it (`models.pdk_root`).",
        f"  - klt's `provenance.pdk` and `environment.models_lib_sha256` in the grid reports are resolved on the "
        f"SUBMITTING CLIENT ({'; '.join(client)}), not on the fleet runner, which uses its own install and (klt "
        "0.5.0) does not report its revision. The fleet library is tied to the pinned revision indirectly: the "
        "local pinned units reproduce the grid's nominal point, and the grid's open-loop response reproduces the "
        "committed gain-bench record (see Cross-checks).",
    ]


def execution_lines(reports: dict[str, dict], walls: dict[str, float]) -> list[str]:
    L = []
    for mode, rep in reports.items():
        r = remote_of(rep)
        env = rep.get("environment") or {}
        if r:
            L.append(f"  - `{mode}`: backend `{r.get('provider')}`, job `{r.get('job_id')}`, "
                     f"{'Spot ' if r.get('spot') else ''}{r.get('instance_type', '')}, state `{r.get('state')}`, "
                     f"runner klt `{r.get('runner_klt_version')}` vs client `{r.get('client_klt_version')}` "
                     f"(compatibility `{r.get('runner_compatibility')}`); engine `{env.get('engine')} "
                     f"{env.get('engine_version')}`; wall {walls.get(mode, 0):.0f} s")
        else:
            L.append(f"  - `{mode}`: `klt sim` local backend (no `environment.remote`); engine "
                     f"`{env.get('engine')} {env.get('engine_version')}`; wall {walls.get(mode, 0):.0f} s")
    return L


# --------------------------------------------------------------------------
# Evidence writing (shared)
# --------------------------------------------------------------------------


def save_mode_point(corners_dir: Path, k: Key, mode: str, a: dict, cols: tuple[str, ...]) -> None:
    stem = f"{point_stem(k)}.{mode}"
    shutil.copyfile(a["log"], corners_dir / f"{stem}.log")
    if a.get("deck"):
        shutil.copyfile(a["deck"], corners_dir / f"{stem}.cir")
    save_mode_point_vec(corners_dir / f"{stem}.dat", a["vec"], cols)


SAVED_VECTORS = ("v(vout)", "v(vinp)", "v(vinn)", "v(vdd)", "v(vss)")


def save_local_runs(cdir: Path, runs: list[LocalRun]) -> None:
    """Log + klt deck of every local unit, and its node phasors as `.dat`
    (the same layout as the grid points; the full rawfile is not kept)."""
    cdir.mkdir(parents=True, exist_ok=True)
    for r in runs:
        for kind in ("log", "deck"):
            src = r.files.get(kind)
            if src and Path(src).is_file():
                shutil.copyfile(src, cdir / f"{r.name}.{'cir' if kind == 'deck' else 'log'}")
        if r.vec is not None:
            cols = tuple(c for c in SAVED_VECTORS if c in r.vec)
            save_mode_point_vec(cdir / f"{r.name}.dat", r.vec, cols)


def save_mode_point_vec(path: Path, vec: dict, cols: tuple[str, ...]) -> None:
    data = [vec["frequency"].real]
    hdr = ["freq_hz"]
    for c in cols:
        data += [vec[c].real, vec[c].imag]
        hdr += [f"re{c}", f"im{c}"]
    np.savetxt(path, np.column_stack(data), fmt="%.12e", header=" ".join(hdr))


def request_comment(req: dict) -> list[str]:
    return ["* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()]


def write_snapshot(path: Path, record: str, issue: str, testbench: Path, reqs: dict[str, dict], deck0: str) -> None:
    dut_text = load_dut_text()
    lines = [f"* netlist snapshot for record {record} ({issue})",
             "* Reproduces the measured design: DUT contents, testbench, conditions."]
    for mode, req in reqs.items():
        lines += [f"* ---- conditions: klt sim request, excitation `{mode}` (grid) ----", *request_comment(req), ""]
    lines += [
        "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
        dut_text,
        f"* ---- testbench: {testbench.relative_to(REPO_ROOT)} (verbatim; the driver rewrites only its .param line) ----",
        testbench.read_text(),
        "* ---- klt-generated deck of the nominal point, first excitation (corner.cir) ----",
        *("* | " + ln for ln in deck0.splitlines()),
        "",
    ]
    path.write_text("\n".join(lines))


def build_plots_rejection(freq_by_key: dict[Key, np.ndarray], curves: dict[str, dict[Key, np.ndarray]],
                          ad_nom: np.ndarray | None, errs_nom: dict[str, np.ndarray], plot_dir: Path,
                          ylabel: str) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for name, cur in curves.items():
        fig, ax = plt.subplots(figsize=(8, 5))
        for k, c in cur.items():
            ax.semilogx(freq_by_key[k], c, lw=0.6, alpha=0.5, color="0.6")
        for corner in CORNERS:
            k = (corner, 27.0, 3.30)
            if k in cur:
                ax.semilogx(freq_by_key[k], cur[k], lw=1.3, label=f"{corner} 27C 3.30V")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(f"{name} (dB)")
        ax.set_title(f"{name}: all 45 points (grey), 27 C / 3.30 V corners (colour)")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fn = f"{name.lower().replace('+', 'plus').replace('-', 'minus')}-all45.png"
        fig.savefig(plot_dir / fn, dpi=110)
        plt.close(fig)
        out.append(fn)
    if ad_nom is not None:
        fig, ax = plt.subplots(figsize=(8, 5))
        f = freq_by_key[NOMINAL]
        ax.semilogx(f, db(ad_nom), label="|Ad|")
        for label, e in errs_nom.items():
            ax.semilogx(f, e, label=label)
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(ylabel)
        ax.set_title("Transfers at typical / 27 C / 3.30 V")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(plot_dir / "transfers-nominal.png", dpi=110)
        plt.close(fig)
        out.append("transfers-nominal.png")
    return out


CANCEL_EXCESS_DB = 20.0


def cancellation_notes(values: dict[Key, Rejection], name: str, err_name: str) -> list[str]:
    """Flag low-frequency figures that come from a near-cancellation.

    If the plateau phase of the error transfer (Acm, Asupply) flips between
    0 and 180 deg across the grid, that transfer passes through zero somewhere
    in PVT and the rejection near the flip is a cancellation. Points of the
    minority sign, and points more than CANCEL_EXCESS_DB above the grid median,
    are listed. Their numerical resolution (|A| over the floor) is stated."""
    if not values:
        return []
    pos = {k: math.cos(math.radians(r.err_phase_deg)) >= 0 for k, r in values.items()}
    n_pos = sum(pos.values())
    minority = n_pos < len(pos) / 2
    flipped = sorted(k for k, v in pos.items() if v == minority) if 0 < n_pos < len(pos) else []
    lows = [r.low_db for r in values.values()]
    med = float(np.median(lows))
    high = sorted(k for k, r in values.items() if r.low_db > med + CANCEL_EXCESS_DB)
    if not flipped and not high:
        return [f"- {err_name} keeps one sign (plateau phase ~{'0' if n_pos else '180'} deg) at all {len(values)} points; "
                f"no {name} value exceeds the grid median ({med:.1f} dB) by more than {CANCEL_EXCESS_DB:g} dB."]
    out = []
    if flipped:
        out.append(f"- **{err_name} changes sign across the grid**: its plateau phase is ~"
                   f"{'0' if not minority else '180'} deg at {len(values) - len(flipped)} points and ~"
                   f"{'0' if minority else '180'} deg at {len(flipped)} ({', '.join(fmt_key(k) for k in flipped[:8])}"
                   f"{' ...' if len(flipped) > 8 else ''}). The systematic {err_name} passes through zero somewhere in "
                   "PVT, so near the flip the low-frequency " + name + " is a cancellation.")
    for k in high:
        r = values[k]
        out.append(f"  - {fmt_key(k)}: {name} {r.low_db:.1f} dB at 0.1 Hz, {r.low_db - med:.1f} dB above the grid "
                   f"median; |{err_name}| = {r.err_low_mag:.3g} V/V, {math.log10(r.err_low_mag / r.floor):.1f} decades "
                   "above the numerical floor (numerically resolved).")
    out.append(f"- Such values are numerically real for the matched schematic but are NOT design margin: a "
               "mismatch-sized perturbation of the cancelling terms removes them. Use the worst-case figure.")
    return out


def figure_table(add, values: dict[Key, Rejection], title: str) -> None:
    add(f"| point | {title} DC | " + " | ".join(lbl for w, lbl in FIGURES[1:-1]) + " | at f_u | f_u (MHz) | note |")
    add("|---|---|" + "---|" * (len(FIGURES) - 2) + "---|---|---|")
    for k in sorted(values, key=lambda k: (CORNERS.index(k[0]), k[2], k[1])):
        r = values[k]
        note = []
        if not r.plateau_ok:
            note.append(f"no plateau (spread {r.plateau_spread_db:.3f} dB); 0.1 Hz value {r.low_db:.2f} dB")
        if r.lower_bound:
            note.append("lower bound (numerical floor)")
        add(f"| {fmt_key(k)} | {fmt_db(r.dc_db, r.lower_bound)} | "
            + " | ".join(fmt_db(r.spot_db[f], r.lower_bound) for f in SPOT_HZ)
            + f" | {fmt_db(r.at_fu_db, r.lower_bound)} | {r.fu_hz / 1e6:.3f} | {'; '.join(note)} |")


def worst_table(add, values: dict[Key, Rejection], name: str) -> None:
    add(f"| {name} figure | worst (lowest) value (dB) | worst-case corner (process / T / VDD) | best (dB) | points without the figure |")
    add("|---|---|---|---|---|")
    for which, lbl in FIGURES:
        v, k, miss = worst_case(values, which)
        hi = max((r.figure(which) for r in values.values() if math.isfinite(r.figure(which))), default=float("nan"))
        add(f"| {lbl} | **{fmt_db(v)}** | {fmt_key(k) if k else 'n/a'} | {fmt_db(hi)} | {miss} |")


def op_section(add, per_mode: dict[str, dict[Key, dict]], agree_v: float) -> None:
    add("## Operating point (every point, every excitation)")
    add("")
    add("The analysis line prints the DC operating point of the same deck after the `.ac` sweep (`op` + `print`, "
        "retained in each point's log). Blocking: the operating point is returned, "
        f"|vout - VCM| <= {OP_VOUT_TOL_V * 1e3:g} mV (no saturated or differently biased DUT), and every "
        f"excitation of a point sits at the same DC vout to {OP_MODE_AGREE_V * 1e6:g} uV. Recorded: each "
        "DUT MOSFET's saturation margin |Vds| - |Vdsat|.")
    add("")
    first = next(iter(per_mode.values()))
    offs = [abs(a["op"].vout_v - a["op"].vcm_v) for a in first.values()]
    margins = [(min(a["op"].sat_margin_v.values()), k) for k, a in first.items() if a["op"].sat_margin_v]
    add(f"- max |vout - VCM| over the grid: {max(offs) * 1e3:.3f} mV; max DC vout disagreement between "
        f"excitations: {agree_v * 1e6:.4f} uV.")
    if margins:
        lo, klo = min(margins)
        add(f"- smallest device saturation margin: {lo * 1e3:.0f} mV at {fmt_key(klo)}.")
    flagged = {k: a["op"].flags for k, a in first.items() if a["op"].flags}
    add(f"- points with a device below saturation: {len(flagged)}/{len(first)}")
    for k, fl in sorted(flagged.items()):
        add(f"  - {fmt_key(k)}: " + "; ".join(fl))
    add("")


# --------------------------------------------------------------------------
# CMRR record
# --------------------------------------------------------------------------


def build_record(*, record, stamp, pdk, ngspice, kver, reports, walls, points: dict[Key, CmrrPoint],
                 per_mode, agree_v, studies: Studies, gdir, gdev, local_vs_grid, plots, dut_sha, mdut_sha) -> str:
    L: list[str] = []
    add = L.append
    values = {k: p.cmrr for k, p in points.items()}
    add(f"# CMRR record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; commit `{record.rsplit('-', 1)[-1]}`; issue #39")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include `{dut_sha[:16]}`), "
        f"unchanged; snapshot `netlist-snapshots/{record}.spice`")
    for ln in pdk_lines(pdk, reports):
        add(ln)
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`, numpy `{np.__version__}`")
    add("- **Execution**: two `klt sim` corner requests (one per excitation), 45 points each:")
    for ln in execution_lines(reports, walls):
        add(ln)
    add("  - Local single units (studies/controls below) run with an empty `HOME` so no user `~/.spiceinit` "
        "applies (the fleet grid runs without one; DR-0004).")
    add("- **Corner matrix**: process " + ", ".join(CORNERS) + "; T " + ", ".join(f"{t:g} C" for t in TEMPS_C)
        + "; VDD " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V) + " (DC input common mode = VDD/2); "
        "ibias = 10 uA (ideal); CL = 2 pF. Every MOS corner uses `res_typical` + `mimcap_typical` "
        "(RZ/CC passive spread is NOT swept).")
    add("")
    add("## Claim")
    add("")
    add("**Measured, no pass or fail verdict: the row's bound is not ratified** (DR-3 residual (e3)). "
        "`spec/target-spec.md` and the decision records are untouched; a proposed bound citing this record is a "
        "separate decision-record issue for a human to ratify.")
    add("")
    add("**Systematic only.** Every device in the simulated schematic is perfectly matched, so this CMRR is the "
        "systematic (topology- and bias-limited) figure. Real CMRR is usually limited by random mismatch "
        "(input pair, load mirror); a mismatch-limited CMRR needs Monte Carlo, which is DR-3 residual (e2)'s subject "
        "and out of scope here. The mirror-imbalance control below shows how strongly a 10 % load-mirror "
        "imbalance moves it.")
    add("")
    add("## Worst case over the 45 points")
    add("")
    worst_table(add, values, "CMRR")
    add("")
    n_plateau = sum(r.plateau_ok for r in values.values())
    n_lb = sum(r.lower_bound for r in values.values())
    add(f"- Low-frequency plateau verified at {n_plateau}/{len(values)} points; lower bounds (numerical floor): "
        f"{n_lb}/{len(values)}.")
    for ln in cancellation_notes(values, "CMRR", "Acm"):
        add(ln)
    v, k, _ = worst_case({kk: p.cmrr for kk, p in points.items()}, "dc")
    add(f"- f_u (differential unity-gain frequency) over the grid: {min(p.fu_hz for p in points.values()) / 1e6:.3f} .. "
        f"{max(p.fu_hz for p in points.values()) / 1e6:.3f} MHz; Ad plateau "
        f"{min(p.ad_dc_db for p in points.values()):.2f} .. {max(p.ad_dc_db for p in points.values()):.2f} dB.")
    add("")
    add("## Method")
    add("")
    add(f"- `.ac dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}` ({N_FREQ} points) per point and excitation: "
        "`dm` (vinp +0.5 V, vinn -0.5 V AC) and `cm` (vinp = vinn = 1 V AC), same DC common mode.")
    add("- DC loop closed by an ideal servo (`Esb` buffer of vout -> `Rsv`/`Csv` low-pass, tau = 1e18 s -> `Esv` in "
        "series with the vinn AC source): the unity-buffer operating point at DC, an open loop at every swept "
        "frequency, no load on vout, and equal AC drive possible on both input pins. The gain bench's "
        "`Cfb vinn 0` AC-grounds vinn and cannot carry a CM signal.")
    add("- From the ACTUAL phasors, vd = v(vinp) - v(vinn), vc = (v(vinp) + v(vinn))/2; the two runs are solved "
        "exactly for Ad and Acm per frequency (vout = Ad vd + Acm vc). CMRR = 20 log10 |Ad/Acm|.")
    add(f"- **DC** = mean CMRR over {AC_FSTART:g}-{AC_FSTART * PLATEAU_DECADE_HI:g} Hz, valid only if CMRR and |Acm| "
        f"are flat there to {PLATEAU_TOL_DB} dB (else reported unavailable with the 0.1 Hz value). AC has no "
        "literal 0 Hz sample.")
    add("- **Spot values and the value at f_u**: linear interpolation in (log10 f, dB) between sweep points; f_u is "
        "the first descending 0 dB crossing of |Ad| (the gain driver's extraction, reused unchanged; a second "
        "crossing or a non-flat / wrong-polarity Ad plateau invalidates the point).")
    add(f"- **Numerical floor**: |Acm| below {FLOOR_MARGIN:g} x the demonstrated superposition residual "
        f"(= {studies.floor:.3g} V/V, see Studies) is clamped to it and the figure reported as a lower bound "
        "(`>=`); there is no division by zero and no infinite CMRR.")
    add(f"- **Blocking validation**: 45/45 points per excitation, identical frequency axes, finite data, "
        f"differential excitation within {EXC_TOL:g} V of (vd = 1, vc = 0), CM excitation within {EXC_TOL:g} V of "
        f"vc = 1 with |vd/vc| <= {CM_LEAK_MAX:g}, operating point (below), Ad vs the gain bench, local vs grid, "
        "and the studies/controls.")
    add("")
    add("## Excitation and cross-checks")
    add("")
    add(f"- Differential excitation error max {max(p.dm_err for p in points.values()):.3g} V; CM excitation error max "
        f"{max(p.cm_err for p in points.values()):.3g} V; CM residual differential |vd/vc| max "
        f"{max(p.cm_leak for p in points.values()):.3g}.")
    add(f"- Joint solve vs the naive CM ratio vout_cm/vc_cm: max difference {max(p.naive_dev_db for p in points.values()):.2e} dB "
        "over all sweeps (the solve guards against unequal drive; here the drive is equal).")
    if gdev:
        add(f"- **Decomposition vs the gain bench** (`sim/gain-gbw-pm/corners/{gdir.name}`, committed |vout/vdiff|, "
            "same 201 frequencies): the gain bench drives vinp alone (vd = 1 V, vc = 0.5 V), so its response must "
            f"equal Ad + Acm/2 from this record's solve. All {len(gdev)} points, whole sweep: max deviation "
            f"{max(gdev.values()):.2e} dB (tolerance {TOL_GAIN_BENCH_DB} dB). This independently validates the servo "
            "bench and the joint Ad/Acm solve (comparing Ad alone would differ above f_u, where Acm is no longer "
            "negligible).")
    else:
        add("- **Ad vs the gain bench**: committed gain-bench data not available; NOT CHECKED.")
    if local_vs_grid is not None:
        add(f"- **Local nominal unit vs the grid's nominal point**: CMRR differs by at most {local_vs_grid:.4f} dB "
            f"(tolerance {TOL_LOCAL_VS_GRID_DB} dB) at the plateau, the spots and f_u.")
    add("")
    op_section(add, per_mode, agree_v)
    add("## CMRR at all 45 points (dB)")
    add("")
    figure_table(add, values, "CMRR")
    add("")
    add("## Ad and Acm at the plateau (dB)")
    add("")
    add("| point | Ad | Acm | CMRR DC |")
    add("|---|---|---|---|")
    for k in sorted(points, key=lambda k: (CORNERS.index(k[0]), k[2], k[1])):
        p = points[k]
        add(f"| {fmt_key(k)} | {p.ad_dc_db:.2f} | {p.acm_dc_db:.2f} | {fmt_db(p.cmrr.dc_db, p.cmrr.lower_bound)} |")
    add("")
    add("## Studies and negative controls (local single units, typical / 27 C / 3.30 V)")
    add("")
    s = studies
    nom = s.nominal
    if nom and nom.valid:
        add(f"- **Nominal (local)**: CMRR DC {fmt_db(nom.cmrr.dc_db)} dB, 1 MHz {nom.cmrr.spot_db[1e6]:.2f} dB, "
            f"at f_u ({nom.fu_hz / 1e6:.3f} MHz) {nom.cmrr.at_fu_db:.2f} dB.")
    add(f"- **Numerical floor (superposition)**: |vout(both) - vout(vinp only) - vout(vinn only)| max "
        f"{s.floor_resid:.3g} V over the sweep (each single-input response is ~|Ad|/2, so this is the noise of a "
        f"cancellation of ~1e4-1e5 V terms); floor = max({FLOOR_MARGIN:g} x that, {FLOOR_MIN:g}) = {s.floor:.3g} V/V. "
        f"Grid |Acm| minimum: {min(float(np.min(np.abs(p.acm))) for p in points.values()):.3g} V/V.")
    add("")
    add("| study / control | condition | outcome | CMRR 0.1 Hz (dB) | CMRR 1 MHz (dB) | CMRR at f_u (dB) | criterion |")
    add("|---|---|---|---|---|---|---|")

    def row(name, cond, pt: CmrrPoint | None, crit):
        if pt is None:
            add(f"| {name} | {cond} | not run | | | | {crit} |")
        elif pt.valid:
            add(f"| {name} | {cond} | valid | {pt.cmrr.low_db:.3f} | {pt.cmrr.spot_db[1e6]:.3f} | {pt.cmrr.at_fu_db:.3f} | {crit} |")
        else:
            add(f"| {name} | {cond} | **rejected**: {pt.reason[:160]} | | | | {crit} |")

    row("isolation (nominal)", "tau = 1e18 s", nom, "reference")
    for label, pt in s.iso:
        row("isolation", label, pt, f"moves <= {ISOLATION_TOL_DB} dB")
    row("inadequate isolation", "tau = 1e-3 s (servo closes the loop at AC)", s.inadequate, "must be rejected")
    row("unequal CM drive", "acp = 1, acn = 0.99", s.unequal, "must be rejected")
    row("**negative control**: mirror imbalance", f"control DUT: {' '.join(CONTROL_MIRROR[:2])} "
        f"{CONTROL_MIRROR[2]} -> {CONTROL_MIRROR[3]} (-10 %)", s.mirror,
        f"CMRR drops >= {CONTROL_MIN_DROP_DB:g} dB")
    row("information: opposite imbalance", f"control DUT: {' '.join(CONTROL_MIRROR_INFO[:2])} "
        f"{CONTROL_MIRROR_INFO[2]} -> {CONTROL_MIRROR_INFO[3]} (+10 %)", s.mirror_info, "none (sign study)")
    add("")
    if math.isfinite(s.unequal_naive_db) and nom and nom.valid:
        add(f"- With 1 % unequal CM drive, the naive ratio |Ad / (vout_cm / vc_cm)| at 0.1 Hz reads "
            f"{s.unequal_naive_db:.2f} dB instead of {nom.cmrr.low_db:.2f} dB: the differential leak dominates the "
            "common-mode output of a high-gain amplifier. The excitation check rejects such a run.")
    if s.mirror and s.mirror.valid and nom and nom.valid:
        add(f"- Mirror-imbalance control: CMRR at 0.1 Hz {s.mirror.cmrr.low_db:.2f} dB vs {nom.cmrr.low_db:.2f} dB "
            f"(drop {nom.cmrr.low_db - s.mirror.cmrr.low_db:.2f} dB). Control DUT sha256 `{mdut_sha[:16]}`, snapshotted "
            f"separately in `netlist-snapshots/{record}-controls.spice`; the production export is not modified. "
            "Diagnostic only, not a verdict.")
    if s.mirror_info and s.mirror_info.valid and nom and nom.valid:
        add(f"- The opposite imbalance (+10 %) gives {s.mirror_info.cmrr.low_db:.2f} dB: the systematic Acm is a "
            "signed sum of terms, so an imbalance can partially cancel it as well as add to it. That is why a "
            "single deterministic corner cannot stand in for the mismatch-limited CMRR (Monte Carlo, DR-3 (e2)).")
    for p in s.problems:
        add(f"- STUDY PROBLEM: {p}")
    add("")
    add("## Plots")
    add("")
    for p in plots:
        add(f"![{p}]({record}-plots/{p})")
    add("")
    add("## Reproduce / evidence")
    add("")
    add("```")
    add("python3 sim/cmrr/run_cmrr.py            # full grid + a new record")
    add("python3 sim/cmrr/run_cmrr.py --smoke    # nominal point, local, no record")
    add("```")
    add("")
    add(f"- Per point and excitation: `corners/{record}/<point>.<dm|cm>.{{dat,log,cir}}` (actual vout, vinp, vinn "
        "phasors; log with the printed operating point; klt deck); derived curves `<point>.cmrr.dat` "
        "(freq, |Ad| dB, |Acm| dB, CMRR dB); sanitised klt reports `klt-report.<mode>.json`; studies/controls under "
        "`controls/`.")
    add(f"- Testbench `sim/cmrr/testbench/tb_cmrr.spice`; driver `sim/cmrr/run_cmrr.py`; tests `sim/cmrr/test_cmrr.py`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #39)")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def add_common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--smoke", action="store_true", help="nominal point, local, no record")
    ap.add_argument("--backend", help="klt execution backend for the 45-point grids "
                    "(default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None,
                    help="forward batch.runner_version_check (only meaningful on the batch backend)")
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None, help="forward batch.capacity_wait_s")
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet capacity "
                    "(never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--keep-work", type=Path, default=None,
                    help="use this (new) directory as the work dir and keep it: grid reports, rawfiles and logs "
                    "survive a failed validation for diagnosis (never committed)")


def run_grid_modes(pdk, work, args, modes: dict[str, dict], *, testbench=TESTBENCH, guard=guard_testbench):
    """One `klt sim` corner request per excitation. Raises KltError on a submit
    failure (no local fallback)."""
    reports, reqs, walls = {}, {}, {}
    for mode, mp in modes.items():
        tb = materialise(work / mode, pdk, with_servo(mp), testbench=testbench, guard=guard)
        req = ac_request(tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
        req["batch"] = G.batch_block(args)
        print(f"  submitting `{mode}` grid ({len(CORNERS) * len(TEMPS_C) * len(SUPPLIES_V)} points)...", flush=True)
        t0 = time.time()
        reports[mode] = run_klt_retrying(req, work / mode / "out", args.backend, work / mode,
                                         retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
        walls[mode] = time.time() - t0
        reqs[mode] = req
        r = remote_of(reports[mode])
        print(f"  `{mode}` done in {walls[mode]:.0f} s (job {r.get('job_id', 'local')})", flush=True)
    return reports, reqs, walls


def require_plotting() -> str | None:
    """Fail fast, BEFORE any grid is submitted, if matplotlib cannot import
    (e.g. a user-site numpy 2 shadowing a distro matplotlib built for numpy 1;
    `python3 -s` skips the user site). Returns an error message or None."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot  # noqa: F401
    except Exception as exc:  # noqa: BLE001 -- any import failure blocks the record
        return (f"matplotlib is not importable ({type(exc).__name__}: {str(exc)[:200]}); the record's plots "
                "cannot be written. Fix the Python environment (try `python3 -s ...`) before running the grid.")
    return None


def work_dir(args, prefix: str):
    """A temporary work directory, or the kept `--keep-work` one."""
    import contextlib

    if args.keep_work is None:
        return tempfile.TemporaryDirectory(prefix=prefix)
    args.keep_work.mkdir(parents=True, exist_ok=False)
    return contextlib.nullcontext(str(args.keep_work.resolve()))


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="cmrr-smoke-") as scratch:
        pt, runs = cmrr_pair(pdk, Path(scratch), "smoke", "nominal", FLOOR_MIN)
    for r in runs:
        if r.op is not None and r.op.problems:
            print("SMOKE TEST FAILED: " + "; ".join(r.op.problems))
            return 1
    if not pt.valid:
        print(f"SMOKE TEST FAILED: {pt.reason}")
        return 1
    c = pt.cmrr
    print(f"  Ad {pt.ad_dc_db:.2f} dB, f_u {pt.fu_hz / 1e6:.3f} MHz; CMRR DC {fmt_db(c.dc_db)} dB, "
          + ", ".join(f"{f:g} Hz {c.spot_db[f]:.1f}" for f in SPOT_HZ) + f", at f_u {c.at_fu_db:.1f} dB")
    print("smoke test OK (both excitations ran and validated; the full run records the evidence)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_args(ap)
    args = ap.parse_args(argv)
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    err = require_plotting()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    want = expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = G.claim_record_paths(HERE, record)
    ctl_snap = HERE / "netlist-snapshots" / f"{record}-controls.spice"
    if ctl_snap.exists():
        raise FileExistsError(f"{ctl_snap} already exists; evidence is append-only")
    ngspice, kver = ngspice_version(), G.klt_version()
    gdir = latest_gain_dir()
    print(f"record {record}: 2 excitations x {len(want)} points, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    with work_dir(args, "cmrr-") as scratch:
        work = Path(scratch)
        try:
            reports, reqs, walls = run_grid_modes(pdk, work, args, MODES)
        except KltError as exc:
            print(f"ERROR: a grid request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        need = ("v(vout)", "v(vinp)", "v(vinn)")
        per_mode, problems = {}, []
        for mode, rep in reports.items():
            per_mode[mode], p = analyse_mode_report(rep, want, mode, need)
            problems += p
        agree_v, p = op_agreement(per_mode, want)
        problems += p

        studies = run_cmrr_studies(pdk, work)
        problems += studies.problems
        points: dict[Key, CmrrPoint] = {}
        gdev: dict[Key, float] = {}
        for k in want:
            if k not in per_mode["dm"] or k not in per_mode["cm"]:
                continue
            dm, cm = per_mode["dm"][k]["vec"], per_mode["cm"][k]["vec"]
            pt = extract_cmrr(dm["frequency"].real, dm, cm["frequency"].real, cm, studies.floor)
            if not pt.valid:
                problems.append(f"{fmt_key(k)}: invalid: {pt.reason}")
                continue
            points[k] = pt
            d = gain_bench_dev_db(gdir, k, pt.freq, pt.ad + 0.5 * pt.acm)
            if gdir is not None:
                if d is None:
                    problems.append(f"{fmt_key(k)}: gain-bench data missing or on another frequency grid")
                else:
                    gdev[k] = d
                    if d > TOL_GAIN_BENCH_DB:
                        problems.append(f"{fmt_key(k)}: |Ad + Acm/2| differs from the gain bench by {d:.3f} dB")
        local_vs_grid = None
        if NOMINAL in points and studies.nominal and studies.nominal.valid:
            a, b = points[NOMINAL].cmrr, studies.nominal.cmrr
            local_vs_grid = max(abs(a.low_db - b.low_db), abs(a.at_fu_db - b.at_fu_db),
                                *(abs(a.spot_db[f] - b.spot_db[f]) for f in SPOT_HZ))
            if local_vs_grid > TOL_LOCAL_VS_GRID_DB:
                problems.append(f"local nominal unit and the grid's nominal point differ by {local_vs_grid:.3f} dB "
                                "(engine/environment parity)")
        if problems or len(points) != len(want):
            print("ERROR: the CMRR evidence did not validate; NO RECORD WRITTEN:", file=sys.stderr)
            for p in problems[:60]:
                print(f"  - {p}", file=sys.stderr)
            return 2

        corners_dir = paths["corners"]
        corners_dir.mkdir(parents=True)
        for mode in MODES:
            for k, a in per_mode[mode].items():
                save_mode_point(corners_dir, k, mode, a, need)
            (corners_dir / f"klt-report.{mode}.json").write_text(json.dumps(G.sanitise_report(reports[mode]), indent=1))
        for k, p in points.items():
            np.savetxt(corners_dir / f"{point_stem(k)}.cmrr.dat",
                       np.column_stack([p.freq, db(p.ad), db(p.acm), p.cmrr.curve_db]), fmt="%.8e",
                       header="freq_hz Ad_dB Acm_dB CMRR_dB(Acm clamped at the numerical floor)")
        save_local_runs(corners_dir / "controls", studies.runs)

        dut_sha = hashlib.sha256(load_dut_text().encode()).hexdigest()
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        deck0 = Path(per_mode["dm"][NOMINAL]["deck"]).read_text() if per_mode["dm"][NOMINAL].get("deck") else ""
        write_snapshot(paths["snapshot"], record, "issue #39", TESTBENCH, reqs, deck0)
        inst, prm, old, new = CONTROL_MIRROR
        mdut = mutate_instance(load_dut_text(), inst, prm, old, new)
        mdut_sha = hashlib.sha256(mdut.encode()).hexdigest()
        ctl_snap.write_text("\n".join([
            f"* control snapshot for record {record} (issue #39) -- NOT the production DUT",
            f"* negative control `control-mirror`: {inst} {prm}={old} -> {prm}={new}; every other line verbatim.",
            "* stimulus-fixture controls change only the testbench .param line:",
            f"*   unequal CM drive: acp=1 acn=0.99; inadequate isolation: csv={ISOLATION_INADEQUATE_CSV:g}",
            f"*   isolation study: csv in {ISOLATION_CSV}",
            "* ---- control DUT (opamp_two_stage.dut.spice of control-mirror-dm/-cm) ----",
            mdut,
            f"* ---- information DUT (info-mirror-opposite-dm/-cm): {CONTROL_MIRROR_INFO[0]} "
            f"{CONTROL_MIRROR_INFO[1]}={CONTROL_MIRROR_INFO[2]} -> {CONTROL_MIRROR_INFO[3]} ----",
            *("* | " + ln for ln in mutate_instance(load_dut_text(), *CONTROL_MIRROR_INFO).splitlines()),
        ]))

        nomp = points[NOMINAL]
        plots = build_plots_rejection(
            {k: p.freq for k, p in points.items()}, {"CMRR": {k: p.cmrr.curve_db for k, p in points.items()}},
            nomp.ad, {"|Acm|": db(nomp.acm), "CMRR": nomp.cmrr.curve_db}, paths["plots"], "dB")
        md = build_record(record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, reports=reports,
                          walls=walls, points=points, per_mode=per_mode, agree_v=agree_v, studies=studies,
                          gdir=gdir, gdev=gdev, local_vs_grid=local_vs_grid, plots=plots, dut_sha=dut_sha,
                          mdut_sha=mdut_sha)
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    for which, lbl in FIGURES:
        v, k, _ = worst_case({kk: p.cmrr for kk, p in points.items()}, which)
        print(f"  worst CMRR {lbl}: {fmt_db(v)} dB at {fmt_key(k) if k else 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
