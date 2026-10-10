#!/usr/bin/env python3
"""Quiescent power, slew rate and output swing of the committed sized
schematic across the full ratified PVT grid (issue #44; tracker #7 item 5).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. None of
the three testbenches (`testbench/tb_power.spice`, `tb_slew.spice`,
`tb_swing.spice`) declares a transistor.

What it runs
------------
The 45-point grid `process {typical, ff, ss, fs, sf} x temperature {-40, 27,
125 C} x supply {2.97, 3.30, 3.63 V}` is expressed, per figure, as ONE
`klt sim` request (a `corners` block): three requests, one per figure, each
carrying all 45 points. Which backend executes them is `klt`'s decision
(`--backend`, the request's `backend`, or `$KLT_SIM_BACKEND`); on a dispatch
worker that is the Spot batch fleet. This script never launches ngspice itself
for a grid and never falls back to a local grid when a batch submit fails: a
figure whose request could not be run is recorded as NOT RUN with the error.

Per point it extracts, from the rawfile klt retains:

  * quiescent power -- `-i(Vdd) * VDD` at Ibias = 10 uA, unity-gain follower
    at VCM, NO load of any kind (`.op`-equivalent: a one-step DC sweep of
    Ibias, the form every klt runner supports). `i(Vdd)` includes the bias
    reference branch, so this is TOTAL supply power.
  * slew rate -- unity-gain follower, CL = 2 pF, a 1.0 V input step centred
    on VCM, one rising and one falling edge in a single transient. Per edge:
    the output's 20 % -> 80 % slope, with the 0 % / 100 % levels taken from
    the settled output before the edge and just before the next one. The
    reported figure is the SLOWER of the two edges.
  * output swing -- inverting unity-gain configuration (input common mode
    pinned at VCM), `Vin` swept 0 .. 3.63 V; each output edge is the FIRST of
    the incremental gain |dvout/dvin| falling to 1/sqrt(2) (-3 dB) of its
    mid-range value or output device M6 (upper) / M7 (lower) leaving
    saturation (|Vds| < |Vdsat|); the swing is the span between the two edges.
    See the README for why this criterion and how it relates to DR-2
    section (b). The per-point swing data keeps the M6/M7 Vds/Vdsat vectors,
    so the verdict can be re-derived from the committed files
    (`rederive_swing()`; the driver does so before writing a record).

Verdicts are checked per point against the ratified bounds in
`spec/target-spec.md` (slew >= 10 V/us, swing >= 2.3 Vpp, power <= 350 uW
worst case); each figure's binding corner is its worst point. Missing or
malformed data, an invalid measurement, or a failed simulation never passes.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<figure>/<process>_<T>c_<vdd>v.{log,cir,dat}   one set per point
    corners/<rid>/<figure>/klt-report.json                        sanitised klt report
    corners/<rid>/controls/...                                    single-unit controls
    netlist-snapshots/<rid>.spice                                 DUT + testbenches + conditions
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/slew-swing-power/run_slew_swing_power.py              # full grid + record
    python3 sim/slew-swing-power/run_slew_swing_power.py --smoke      # one point, no record
    python3 sim/slew-swing-power/run_slew_swing_power.py --backend local
    python3 sim/slew-swing-power/run_slew_swing_power.py --figures swing   # one figure, one request

Exit status: 0 when the evidence is complete (every figure ran and the controls
behave) -- INCLUDING when a spec row misses, because a miss is a result;
`--strict` makes a spec miss exit 1. A figure that could not be run exits 1.
Rows outside `--figures` are recorded as "not measured in this record", never
as a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
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
    run_klt,
    run_klt_retrying,
    load_sibling,
    sanitise_report,
)


# Shared RZ x CC passive-corner helpers (issue #97): ONE copy, also used by the gain driver.
import passive_corners as pc  # noqa: E402

# The gain driver is reused unchanged so the two experiments share ONE copy of
# the DUT source guards and grid bookkeeping, instead of a second copy that
# could drift. (The klt invocation/retry, report sanitising and DUT loading are
# in `harness`.)
g = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")

# One source for everything the fingerprint covers (issue #89).
mc = load_sibling("slew_swing_power_measurement_config", "sim/slew-swing-power/measurement_config.py")

TB_DIR = HERE / "testbench"
TESTBENCH = {f: TB_DIR / f"tb_{f}.spice" for f in ("power", "slew", "swing")}
FIGURES = mc.FIGURES

# --------------------------------------------------------------------------
# Grid and conditions (identical to sim/gain-gbw-pm; spec/target-spec.md Sec.1)
# --------------------------------------------------------------------------

CORNERS = mc.CORNERS
TEMPS_C = mc.TEMPS_C
SUPPLIES_V = mc.SUPPLIES_V
NOMINAL = g.NOMINAL
IBIAS_A = mc.IBIAS_A

# Ratified bounds (spec/target-spec.md Sec.2). Never edited here.
SLEW_MIN_VUS = 10.0
SWING_MIN_V = 2.3
SWING_STRETCH_V = 2.6
POWER_MAX_UW = 350.0

# Slew bench timing (tb_slew.spice): input low until RISE_T, high until FALL_T.
SLEW_RISE_T = mc.SLEW_RISE_T
SLEW_FALL_T = mc.SLEW_FALL_T
SLEW_TSTOP = mc.SLEW_TSTOP
SLEW_TSTEP = mc.SLEW_TSTEP
SLEW_STEP_V = mc.SLEW_STEP_V
SLEW_LO_FRAC, SLEW_HI_FRAC = mc.SLEW_LO_FRAC, mc.SLEW_HI_FRAC
SLEW_LEVEL_TOL_V = 0.02  # |vout - vinp| at the settled pre-edge instants
SLEW_SAMPLE_BEFORE = mc.SLEW_SAMPLE_BEFORE

# Swing bench (tb_swing.spice): FIXED sweep range that covers every grid supply.
SWING_VIN_STOP_V = mc.SWING_VIN_STOP_V
SWING_VIN_STEP_V = mc.SWING_VIN_STEP_V
SWING_FRAC = mc.SWING_FRAC
SWING_SENS_FRACS = (0.9, 0.5)  # reported as sensitivity only
SWING_MID_BAND_V = mc.SWING_MID_BAND_V
SWING_GAIN_TOL = 0.05  # mid-range |gain| must be 1 +- this

# Cross-check tolerances against ngspice's own `.meas`.
XCHK_POWER_REL = 1e-3
XCHK_LEVEL_V = 5e-3


# --------------------------------------------------------------------------
# Testbench materialisation (guards shared with the sibling experiment)
# --------------------------------------------------------------------------

_IBIAS_RE = re.compile(r"^Ibias\s+vdd\s+ibias\s+dc\s+\S+\s*$", re.M)


def materialise(fig: str, work: Path, pdk: Pdk, *, ibias_a: float | None = None) -> Path:
    """Write the per-run work directory for one figure; return the testbench path.

    The committed testbench is used verbatim except for the two `.include`
    targets (rewritten to absolute paths in `work`) and, for a control only,
    an explicit `Ibias` override.
    """
    tb_text = TESTBENCH[fig].read_text()
    errs = g.guard_testbench(tb_text)
    if errs:
        raise RuntimeError(f"{TESTBENCH[fig].name} guard failed:\n  " + "\n  ".join(errs))
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdk.design_include, work / "design.ngspice")
    dut = load_dut_text()
    derrs = g.guard_dut(dut)
    if derrs:
        raise RuntimeError("DUT guard failed:\n  " + "\n  ".join(derrs))
    (work / g.DUT_INCLUDE_NAME).write_text(dut)
    tb = tb_text.replace("'design.ngspice'", f"'{work / 'design.ngspice'}'")
    tb = tb.replace(f"'{g.DUT_INCLUDE_NAME}'", f"'{work / g.DUT_INCLUDE_NAME}'")
    if ibias_a is not None:
        tb, n = _IBIAS_RE.subn(f"Ibias vdd ibias dc {ibias_a:g}", tb)
        if n != 1:
            raise RuntimeError("testbench Ibias line not found exactly once")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt requests
# --------------------------------------------------------------------------


def make_request(fig: str, netlist: Path, pdk: Pdk, corners, temps, supplies, *, ibias_a: float = IBIAS_A) -> dict:
    """One 45-point (or single-point) `corners` request for one figure."""
    req = g.ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = mc.analysis_for(fig, ibias_a)
    req["measurements"] = mc.measurements_for(fig, ibias_a)
    req["options"] = {"timeout_s": 600, "keep_artifacts": True, "waveforms": True}
    return req


# --------------------------------------------------------------------------
# Rawfile parsing (ngspice ASCII, Flags: real)
# --------------------------------------------------------------------------


def parse_ascii_real_raw(text: str) -> dict[str, np.ndarray]:
    """Parse an ngspice ASCII real rawfile (`.tran`, `.dc`) into {name: array}.

    Variable names are returned lower-cased, e.g. `time`, `v(vout)`,
    `i(vdd)`. Raises ValueError on anything malformed (wrong value count,
    non-finite numbers): bad data fails loudly, it is never repaired.
    """
    if "Flags: real" not in text:
        raise ValueError("rawfile is not a real (tran/dc) rawfile")
    m = re.search(r"No\. Variables:\s*(\d+)", text)
    p = re.search(r"No\. Points:\s*(\d+)", text)
    if not m or not p or "Variables:" not in text or "Values:" not in text:
        raise ValueError("rawfile header incomplete")
    nvar, npts = int(m.group(1)), int(p.group(1))
    head, _, body = text.partition("Values:")
    names = re.findall(r"^\t\d+\t(\S+)\t", head.split("Variables:", 1)[1], re.M)
    if len(names) != nvar:
        raise ValueError(f"rawfile declares {nvar} variables, found {len(names)}")
    toks = body.split()
    if len(toks) != npts * (nvar + 1):
        raise ValueError(
            f"rawfile has {len(toks)} tokens, expected {npts} x ({nvar} + 1) = {npts * (nvar + 1)}"
        )
    try:
        arr = np.array([float(t) for t in toks]).reshape(npts, nvar + 1)[:, 1:]
    except ValueError as exc:
        raise ValueError(f"rawfile value not a number: {exc}") from exc
    if not np.all(np.isfinite(arr)):
        raise ValueError("rawfile contains non-finite values")
    return {n.lower(): arr[:, i] for i, n in enumerate(names)}


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class Metrics:
    """One point's figure. Only the fields of the point's figure are filled."""

    fig: str
    valid: bool
    reason: str = ""
    # power
    power_uw: float = float("nan")
    idd_ua: float = float("nan")
    vout_v: float = float("nan")
    # slew
    slew_vus: float = float("nan")  # the slower edge
    slew_rise_vus: float = float("nan")
    slew_fall_vus: float = float("nan")
    vlo_v: float = float("nan")
    vhi_v: float = float("nan")
    # swing
    swing_v: float = float("nan")
    vout_hi_v: float = float("nan")  # upper -3 dB output level
    vout_lo_v: float = float("nan")  # lower -3 dB output level
    mid_gain: float = float("nan")
    sens: dict = field(default_factory=dict)  # {criterion variant: swing_v}
    edge_hi: str = ""  # criterion that bound the upper / lower output edge
    edge_lo: str = ""
    # swing: the output-device saturation vectors that decide the edges, kept so
    # the committed per-point data can re-derive the verdict (SAT_COLUMNS order)
    sat: dict = field(default_factory=dict, repr=False)
    # waveform kept for plots/data
    x: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    y: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)


def _bad(fig: str, reason: str, **kw) -> Metrics:
    return Metrics(fig=fig, valid=False, reason=reason, **kw)


def extract_power(vec: dict[str, np.ndarray], vdd: float, ibias_a: float = IBIAS_A) -> Metrics:
    """Total supply power at the first point of the Ibias sweep (= `ibias_a`)."""
    for need in ("i(vdd)", "v(vdd)", "v(vout)"):
        if need not in vec:
            return _bad("power", f"rawfile has no {need} vector")
    sweep = next((k for k in vec if "sweep" in k), None)
    if sweep is None or not math.isclose(vec[sweep][0], ibias_a, rel_tol=1e-6):
        return _bad("power", f"first sweep point is not Ibias = {ibias_a:g} A")
    vdd_meas = float(vec["v(vdd)"][0])
    if abs(vdd_meas - vdd) > 1e-6:
        return _bad("power", f"v(vdd) = {vdd_meas:.6f} V differs from the requested {vdd:.2f} V")
    idd = -float(vec["i(vdd)"][0])  # SPICE: current INTO the + terminal, so the supply sources a negative i(Vdd)
    if not idd > 0:
        return _bad("power", f"supply current {idd:.3e} A is not positive (wrong polarity or dead circuit)")
    if idd <= ibias_a:
        return _bad("power", f"supply current {idd:.3e} A does not exceed the bias branch current")
    return Metrics(fig="power", valid=True, power_uw=idd * vdd * 1e6, idd_ua=idd * 1e6, vout_v=float(vec["v(vout)"][0]))


def _first_cross(t: np.ndarray, v: np.ndarray, level: float, rising: bool, i0: int, i1: int) -> float | None:
    """Linearly interpolated time of the first crossing of `level` in samples [i0, i1)."""
    for i in range(i0, i1 - 1):
        a, b = v[i], v[i + 1]
        hit = (a < level <= b) if rising else (a > level >= b)
        if hit:
            return float(t[i] + (level - a) / (b - a) * (t[i + 1] - t[i]))
    return None


def extract_slew(vec: dict[str, np.ndarray]) -> Metrics:
    """Slew rate of the follower step response (see the module docstring).

    Method:
      1. Validate: >= 100 finite samples, strictly increasing time, the run
         covers both edges plus the final settling window.
      2. Settled levels: vout read SLEW_SAMPLE_BEFORE ahead of each edge (low
         level before the rising edge, high level before the falling edge),
         plus the end of the run. Each must equal the input there to within
         SLEW_LEVEL_TOL_V -- otherwise the output never followed the step
         (clipped, stuck, or too slow to settle) and the point is INVALID,
         never a slew figure of a wrong swing.
      3. The step actually followed must be >= 0.8 x SLEW_STEP_V.
      4. Rising edge: 20 % and 80 % of (vhi - vlo) above vlo; the FIRST upward
         crossing of each after the edge; slope = 0.6*(vhi-vlo)/(t80 - t20).
         Falling edge: the same with the first DOWNWARD crossings of the 80 %
         then 20 % levels. The reported slew is the SLOWER edge.
    """
    for need in ("time", "v(vout)", "v(vinp)"):
        if need not in vec:
            return _bad("slew", f"rawfile has no {need} vector")
    t, vo, vi = vec["time"], vec["v(vout)"], vec["v(vinp)"]
    if t.size < 100 or np.any(np.diff(t) <= 0):
        return _bad("slew", "fewer than 100 samples or time not strictly increasing")
    if t[-1] < SLEW_TSTOP * 0.999:
        return _bad("slew", f"transient ends at {t[-1]:.3g} s, before {SLEW_TSTOP:g} s")

    def at(tt: float, arr: np.ndarray) -> float:
        return float(np.interp(tt, t, arr))

    t_lo, t_hi, t_end = SLEW_RISE_T - SLEW_SAMPLE_BEFORE, SLEW_FALL_T - SLEW_SAMPLE_BEFORE, t[-1]
    vlo, vhi, vend = at(t_lo, vo), at(t_hi, vo), at(t_end, vo)
    common = dict(vlo_v=vlo, vhi_v=vhi, x=t, y=vo)
    for name, tt, vv in (("pre-rise", t_lo, vlo), ("pre-fall", t_hi, vhi), ("end", t_end, vend)):
        if abs(vv - at(tt, vi)) > SLEW_LEVEL_TOL_V:
            return _bad(
                "slew",
                f"output did not settle to the input at the {name} instant "
                f"(vout {vv:.3f} V vs vinp {at(tt, vi):.3f} V)",
                **common,
            )
    step = vhi - vlo
    if step < 0.8 * SLEW_STEP_V:
        return _bad("slew", f"output step {step:.3f} V is below {0.8 * SLEW_STEP_V:.2f} V (output clipped)", **common)
    v20, v80 = vlo + SLEW_LO_FRAC * step, vlo + SLEW_HI_FRAC * step
    n = t.size
    i_rise0 = int(np.searchsorted(t, SLEW_RISE_T))
    i_fall0 = int(np.searchsorted(t, SLEW_FALL_T))
    tr20 = _first_cross(t, vo, v20, True, i_rise0, i_fall0)
    tr80 = _first_cross(t, vo, v80, True, i_rise0, i_fall0)
    tf80 = _first_cross(t, vo, v80, False, i_fall0, n)
    tf20 = _first_cross(t, vo, v20, False, i_fall0, n)
    if None in (tr20, tr80, tf80, tf20):
        return _bad("slew", "an edge never crossed its 20 % / 80 % level", **common)
    if not (tr80 > tr20 and tf20 > tf80):
        return _bad("slew", "20/80 % crossings out of order", **common)
    rise = (v80 - v20) / (tr80 - tr20) * 1e-6  # V/us
    fall = (v80 - v20) / (tf20 - tf80) * 1e-6
    if not (math.isfinite(rise) and math.isfinite(fall) and rise > 0 and fall > 0):
        return _bad("slew", "non-finite or non-positive slope", **common)
    return Metrics(
        fig="slew", valid=True, slew_vus=min(rise, fall), slew_rise_vus=rise, slew_fall_vus=fall, **common
    )


def _edge(vin: np.ndarray, vout: np.ndarray, quals: dict[str, np.ndarray], i0: int, step: int):
    """Walk from index i0 in direction `step` (+1/-1) to the first sample where
    any criterion array in `quals` goes negative (criterion violated); return
    (vin, vout, cause) at the linearly interpolated earliest crossing, or None
    if the sweep ends first. `cause` names the criterion that bound the edge."""
    i = i0
    n = vin.size
    while 0 <= i + step < n:
        j = i + step
        best = None
        for name, q in quals.items():
            if q[j] < 0:
                f = q[i] / (q[i] - q[j]) if q[i] != q[j] else 0.0
                if best is None or f < best[0]:
                    best = (f, name)
        if best is not None:
            f, name = best
            return float(vin[i] + f * (vin[j] - vin[i])), float(vout[i] + f * (vout[j] - vout[i])), name
        i = j
    return None


#: Swing data columns saved per point after vin_v / vout_v: (column name, rawfile vector).
SAT_COLUMNS = tuple(
    (f"{d[1:]}_{q}_v", f"v(@m.xdut.{d}.m0[{q}])") for d in ("xm6", "xm7") for q in ("vds", "vdsat")
)


def _sat_margin(vec: dict[str, np.ndarray], dev: str) -> np.ndarray:
    vds, vdsat = f"v(@m.xdut.{dev}.m0[vds])", f"v(@m.xdut.{dev}.m0[vdsat])"
    return np.abs(vec[vds]) - np.abs(vec[vdsat])


def extract_swing(vec: dict[str, np.ndarray], vcm: float) -> Metrics:
    """Output swing: the span the output covers while the loop still has gain
    AND both output devices stay saturated (see the README for the criterion).

    Method:
      1. Validate: >= 100 finite points, the sweep variable `v(vin)` strictly
         increasing and spanning past VCM on both sides; the saved output-stage
         device quantities present.
      2. slope = d vout / d vin (central differences). The mid-range gain is the
         median slope within +-SWING_MID_BAND_V of VCM; it must be -1 within
         SWING_GAIN_TOL (inverting unity gain). Otherwise the bench is not
         the circuit it claims to be and the point is INVALID. At the VCM sample
         both output devices must be saturated (|Vds| >= |Vdsat|), else INVALID.
      3. From VCM, walk toward lower vin (output rising) and toward higher vin
         (output falling) to the FIRST sample where either |slope| has fallen
         below SWING_FRAC * |mid gain| (-3 dB) or M6 / M7 has left saturation;
         the crossing is interpolated linearly. A sweep that ends before either
         happens is INVALID (the swing is not bounded by the data, and is never
         reported as a lower bound).
      4. swing = vout(upper edge) - vout(lower edge). Also reported, as
         sensitivity only: the gain-only (-3 dB, no saturation condition) span
         and the span at the SWING_SENS_FRACS gain thresholds (still with the
         saturation condition).
    """
    need = ["v(vin)", "v(vout)"] + [f"v(@m.xdut.{d}.m0[{q}])" for d in ("xm6", "xm7") for q in ("vds", "vdsat")]
    for n_ in need:
        if n_ not in vec:
            return _bad("swing", f"rawfile has no {n_} vector")
    vin, vout = vec["v(vin)"], vec["v(vout)"]
    if vin.size < 100 or np.any(np.diff(vin) <= 0):
        return _bad("swing", "fewer than 100 points or sweep not strictly increasing")
    if vin[0] > vcm - 0.5 or vin[-1] < vcm + 0.5:
        return _bad("swing", "sweep does not span VCM +- 0.5 V")
    slope = np.gradient(vout, vin)
    i0 = int(np.argmin(np.abs(vin - vcm)))
    band = np.abs(vin - vcm) <= SWING_MID_BAND_V
    g_mid = float(np.median(slope[band]))
    common = dict(mid_gain=g_mid, x=vin, y=vout, sat={col: vec[name] for col, name in SAT_COLUMNS})
    if abs(g_mid + 1.0) > SWING_GAIN_TOL:
        return _bad("swing", f"mid-range gain {g_mid:.3f} is not -1 +- {SWING_GAIN_TOL} (wrong polarity/feedback)", **common)
    m6, m7 = _sat_margin(vec, "xm6"), _sat_margin(vec, "xm7")
    if m6[i0] < 0 or m7[i0] < 0:
        return _bad("swing", "an output device is not saturated at the VCM operating point", **common)
    sat = {"M6": m6, "M7": m7}

    def span(frac: float | None, use_sat: bool):
        quals: dict[str, np.ndarray] = dict(sat) if use_sat else {}
        if frac is not None:
            quals["gain"] = (np.abs(slope) - frac * abs(g_mid)) / abs(g_mid)
        return _edge(vin, vout, quals, i0, -1), _edge(vin, vout, quals, i0, +1)

    up, dn = span(SWING_FRAC, True)
    if up is None or dn is None:
        side = "upper" if up is None else "lower"
        return _bad("swing", f"neither gain collapse nor loss of saturation on the {side} side within the sweep (swing unbounded by data)", **common)
    vhi_o, vlo_o = up[1], dn[1]
    swing = vhi_o - vlo_o
    sens: dict = {}
    for fr in SWING_SENS_FRACS:
        u, d = span(fr, True)
        sens[fr] = (u[1] - d[1]) if (u is not None and d is not None) else float("nan")
    u, d = span(SWING_FRAC, False)
    sens["gain-only"] = (u[1] - d[1]) if (u is not None and d is not None) else float("nan")
    u, d = span(None, True)
    sens["sat-only"] = (u[1] - d[1]) if (u is not None and d is not None) else float("nan")
    if not (math.isfinite(swing) and swing > 0):
        return _bad("swing", "non-finite or non-positive swing", **common)
    return Metrics(
        fig="swing", valid=True, swing_v=swing, vout_hi_v=vhi_o, vout_lo_v=vlo_o, sens=sens,
        edge_hi=up[2], edge_lo=dn[2], **common,
    )


# --------------------------------------------------------------------------
# Verdicts and binding corners
# --------------------------------------------------------------------------

Key = g.Key  # (process, temperature_c, supply_v)

#: (figure, label, attribute, bound, unit, direction) -- direction "min" means
#: the value must be >= bound (worst = smallest), "max" that it must be <= bound.
ROWS = {
    "slew": ("Slew rate (slower of rise/fall, CL = 2 pF)", "slew_vus", SLEW_MIN_VUS, "V/us", "min"),
    "swing": ("Output swing", "swing_v", SWING_MIN_V, "Vpp", "min"),
    "power": ("Quiescent power", "power_uw", POWER_MAX_UW, "uW", "max"),
}


def point_passes(m: Metrics, fig: str) -> bool:
    _, attr, bound, _, direction = ROWS[fig]
    if not m.valid:
        return False
    v = getattr(m, attr)
    if not math.isfinite(v):
        return False
    return v >= bound if direction == "min" else v <= bound


@dataclass
class RowVerdict:
    fig: str
    label: str
    bound: float
    unit: str
    direction: str
    verdict: str  # "PASS" | "FAIL" | "NOT RUN" | "NOT REQUESTED" (outside --figures)
    n_pass: int
    n_total: int
    worst_value: float
    binding: Key | None
    n_invalid: int
    stretch_pass: int | None = None  # swing only


def judge_figure(fig: str, results: dict[Key, Metrics] | None, *, requested: bool = True) -> RowVerdict:
    label, attr, bound, unit, direction = ROWS[fig]
    if not requested:
        # Outside this run's --figures: no claim either way (the row's evidence is another record).
        return RowVerdict(fig, label, bound, unit, direction, "NOT REQUESTED", 0, 0, float("nan"), None, 0)
    if results is None:
        return RowVerdict(fig, label, bound, unit, direction, "NOT RUN", 0, 0, float("nan"), None, 0)
    n_pass = sum(point_passes(m, fig) for m in results.values())
    n_inv = sum(not m.valid for m in results.values())
    finite = [(getattr(m, attr), k) for k, m in results.items() if m.valid]
    if n_inv:
        # An invalid point is the worst possible outcome for a row.
        worst, binding = float("nan"), next(k for k, m in results.items() if not m.valid)
    elif finite:
        worst, binding = (min if direction == "min" else max)(finite, key=lambda t: t[0])
    else:
        worst, binding = float("nan"), None
    stretch = None
    if fig == "swing":
        stretch = sum(m.valid and m.swing_v >= SWING_STRETCH_V for m in results.values())
    return RowVerdict(
        fig, label, bound, unit, direction,
        "PASS" if results and n_pass == len(results) else "FAIL",
        n_pass, len(results), worst, binding, n_inv, stretch,
    )


# --------------------------------------------------------------------------
# Report handling
# --------------------------------------------------------------------------


def _meas(c: dict) -> dict:
    return {x["name"]: x.get("value") for x in c.get("measurements", [])}


def analyse_report(fig: str, report: dict, want: list[Key], ibias_a: float = IBIAS_A):
    """Per-point metrics + artifacts from a klt report; list of failure strings."""
    problems: list[str] = []
    results: dict[Key, Metrics] = {}
    arts: dict[Key, dict] = {}
    seen: dict[Key, dict] = {}
    for c in report.get("corners", []):
        k = g.point_key(c)
        if k in seen:
            problems.append(f"duplicate result for {g.fmt_key(k)}")
        seen[k] = c
    for k in want:
        c = seen.get(k)
        if c is None:
            problems.append(f"missing result for {g.fmt_key(k)}")
            continue
        diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
        raw = (c.get("artifacts") or {}).get("raw")
        # klt grades a corner "error" if ANY measurement had no value; the point
        # only failed if the rawfile itself is missing.
        if not raw or not Path(raw).is_file():
            problems.append(f"simulation failed for {g.fmt_key(k)}: {diag or 'no rawfile retained'}")
            continue
        try:
            vec = parse_ascii_real_raw(Path(raw).read_text())
        except ValueError as exc:
            problems.append(f"malformed data for {g.fmt_key(k)}: {exc}")
            continue
        vals = _meas(c)
        vcm = float(c["supply_v"].get("vcm", k[2] / 2))
        if fig == "power":
            m = extract_power(vec, k[2], ibias_a)
        elif fig == "slew":
            m = extract_slew(vec)
        else:
            m = extract_swing(vec, vcm)
        if m.valid:
            problems += crosscheck(fig, k, vals, vec, m)
        results[k] = m
        arts[k] = {
            "log": (c.get("artifacts") or {}).get("log"),
            "deck": (c.get("artifacts") or {}).get("deck"),
            "klt_meas": vals,
        }
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"unexpected extra points: {sorted(extra)}")
    return results, arts, problems


def crosscheck(fig: str, k: Key, vals: dict, vec: dict, m: Metrics) -> list[str]:
    """Compare ngspice's own `.meas` values with the rawfile extraction.

    A `.meas` that produced no value is skipped (the extraction is the
    authority); a value that DISAGREES blocks the record.
    """
    bad: list[str] = []
    if fig == "power":
        i = vals.get("ivdd_a")
        if i is not None and abs(-i * 1e6 / m.idd_ua - 1) > XCHK_POWER_REL:
            bad.append(f"{g.fmt_key(k)}: ngspice supply current {-i * 1e6:.4f} uA vs rawfile {m.idd_ua:.4f} uA")
    elif fig == "slew":
        for name, mine in (("vlo_v", m.vlo_v), ("vhi_v", m.vhi_v)):
            v = vals.get(name)
            if v is not None and abs(v - mine) > XCHK_LEVEL_V:
                bad.append(f"{g.fmt_key(k)}: ngspice {name} {v:.4f} V vs rawfile {mine:.4f} V")
    else:
        for name, ref in (("vout_max_v", float(vec["v(vout)"].max())), ("vout_min_v", float(vec["v(vout)"].min()))):
            v = vals.get(name)
            if v is not None and abs(v - ref) > XCHK_LEVEL_V:
                bad.append(f"{g.fmt_key(k)}: ngspice {name} {v:.4f} V vs rawfile {ref:.4f} V")
    return bad


# --------------------------------------------------------------------------
# Single-unit controls (local, nominal point)
# --------------------------------------------------------------------------


@dataclass
class ControlRun:
    name: str
    description: str
    metrics: dict[str, Metrics]  # figure -> metrics
    errors: dict[str, str] = field(default_factory=dict)
    files: dict[str, dict[str, Path]] = field(default_factory=dict)


def run_unit(fig: str, name: str, pdk: Pdk, work: Path, ibias_a: float | None):
    """One nominal-point figure as its own single-unit `klt sim`, LOCAL (allowed
    by the host rules; the 45-point grids never take this path)."""
    wd = work / f"{name}-{fig}"
    tb = materialise(fig, wd, pdk, ibias_a=ibias_a)
    proc, temp, vdd = NOMINAL
    req = make_request(fig, tb, pdk, [proc], [temp], [vdd], ibias_a=IBIAS_A if ibias_a is None else ibias_a)
    rep = run_klt(req, wd / "out", "local", wd)
    res, arts, problems = analyse_report(
        fig, rep, [NOMINAL], IBIAS_A if ibias_a is None else ibias_a
    )
    if problems and NOMINAL not in res:
        raise KltError("; ".join(problems))
    return res[NOMINAL], arts[NOMINAL], problems


def run_controls(pdk: Pdk, work: Path, figs=FIGURES) -> list[ControlRun]:
    out: list[ControlRun] = []
    for name, desc, ib in mc.CONTROLS:
        cr = ControlRun(name, desc, {})
        for fig in figs:
            try:
                m, a, _ = run_unit(fig, name, pdk, work, ib)
                cr.metrics[fig] = m
                cr.files[fig] = {"log": a["log"], "deck": a["deck"]}
            except (KltError, KeyError, RuntimeError) as exc:
                cr.errors[fig] = str(exc)[:300]
        out.append(cr)
    return out


def control_failures(ctrls: list[ControlRun]) -> list[str]:
    """Reasons the controls do NOT behave as the method requires."""
    bad: list[str] = []
    by = {c.name: c for c in ctrls}
    for c in ctrls:
        for fig in FIGURES:
            if c.name == "ibias-zero" and fig in c.errors:
                continue  # a dead circuit may legitimately fail to simulate; handled below
            if fig in c.errors:
                bad.append(f"{c.name}/{fig}: did not simulate ({c.errors[fig]})")
    nom, half, zero = by.get("nominal"), by.get("ibias-half"), by.get("ibias-zero")
    if nom and nom.metrics.get("power") and not nom.metrics["power"].valid:
        bad.append(f"nominal/power: invalid ({nom.metrics['power'].reason})")
    if nom and half and all(f in nom.metrics and f in half.metrics for f in ("power", "slew")):
        np_, hp = nom.metrics["power"], half.metrics["power"]
        ns, hs = nom.metrics["slew"], half.metrics["slew"]
        if np_.valid and hp.valid and not hp.power_uw < 0.75 * np_.power_uw:
            bad.append(f"ibias-half: power {hp.power_uw:.1f} uW is not clearly below nominal {np_.power_uw:.1f} uW")
        if ns.valid and hs.valid and not hs.slew_vus < 0.8 * ns.slew_vus:
            bad.append(f"ibias-half: slew {hs.slew_vus:.2f} V/us is not clearly below nominal {ns.slew_vus:.2f} V/us")
        elif ns.valid and not hs.valid:
            pass  # a half-biased circuit that no longer follows is also "slower"
    if zero:
        for fig in ("slew", "swing"):
            m = zero.metrics.get(fig)
            if m is not None and point_passes(m, fig):
                bad.append(f"ibias-zero/{fig}: negative control PASSED -- the check cannot be trusted")
        pm = zero.metrics.get("power")
        npw = nom.metrics.get("power") if nom else None
        if pm is not None and pm.valid and npw is not None and npw.valid and not pm.power_uw < 0.1 * npw.power_uw:
            bad.append(f"ibias-zero/power: {pm.power_uw:.1f} uW is not far below nominal {npw.power_uw:.1f} uW")
    return bad


# --------------------------------------------------------------------------
# Plots and data
# --------------------------------------------------------------------------


def build_plots(all_results: dict[str, dict[Key, Metrics] | None], plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for fig_name, xlabel, ylabel, fname in (
        ("slew", "time (us)", "vout (V)", "slew_all45.png"),
        ("swing", "Vin (V)", "vout (V)", "swing_all45.png"),
    ):
        res = all_results.get(fig_name)
        if not res:
            continue
        fig, ax = plt.subplots(figsize=(6.5, 4.5))
        for k, m in res.items():
            if m.x.size == 0:
                continue
            x = m.x * 1e6 if fig_name == "slew" else m.x
            ax.plot(x, m.y, linewidth=1.2 if k == NOMINAL else 0.5, alpha=1.0 if k == NOMINAL else 0.5)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(f"{fig_name}: all {len(res)} PVT points (nominal bold)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(plot_dir / fname, dpi=110)
        plt.close(fig)
        names.append(fname)
    power = all_results.get("power")
    if power:
        fig, ax = plt.subplots(figsize=(6.5, 4.0))
        keys = sorted(power, key=lambda k: (CORNERS.index(k[0]), k[2], k[1]))
        ax.bar(range(len(keys)), [power[k].power_uw if power[k].valid else 0 for k in keys])
        ax.axhline(POWER_MAX_UW, color="red", linestyle="--", linewidth=0.8)
        ax.set_xlabel("grid point (process, VDD, T order)")
        ax.set_ylabel("quiescent power (uW)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(plot_dir / "power_all45.png", dpi=110)
        plt.close(fig)
        names.append("power_all45.png")
    return names


def save_point_data(fig: str, m: Metrics, path: Path, *, vcm: float | None = None) -> None:
    """Write one point's data file.

    swing: every sweep sample, full precision, as `vin_v vout_v` followed by the
    M6/M7 |Vds| and |Vdsat| vectors (SAT_COLUMNS) that decide the swing edges,
    and a `vcm_v=` header line -- enough for `rederive_swing()` to reproduce
    the point's swing, edges and binding criterion from the committed file
    alone.
    """
    if fig == "power":
        path.write_text(f"# power_uw idd_ua vout_v\n{m.power_uw:.6g} {m.idd_ua:.6g} {m.vout_v:.6g}\n" if m.valid else f"# INVALID: {m.reason}\n")
    elif fig == "swing" and m.x.size:
        cols = [m.x, m.y]
        names = ["vin_v", "vout_v"]
        if m.sat:
            cols += [m.sat[c] for c, _ in SAT_COLUMNS]
            names += [c for c, _ in SAT_COLUMNS]
        head = ([f"vcm_v={vcm!r}"] if vcm is not None else []) + ([] if m.valid else [f"INVALID: {m.reason}"]) + [" ".join(names)]
        np.savetxt(path, np.column_stack(cols), header="\n".join(head))
    elif m.x.size:
        x, y = m.x, m.y
        if fig == "slew" and x.size > 1200:  # thin the dense transient; edges stay well resolved
            sel = np.unique(np.concatenate([np.arange(0, x.size, 8), [x.size - 1]]))
            x, y = x[sel], y[sel]
        np.savetxt(path, np.column_stack([x, y]), header="time_s vout_v")
    else:
        path.write_text(f"# INVALID: {m.reason}\n")


def load_swing_dat(path: Path) -> tuple[dict[str, np.ndarray], float | None]:
    """Read a swing data file written by save_point_data() back into the rawfile
    vector names extract_swing() consumes; returns (vectors, vcm or None)."""
    vcm, names = None, None
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        body = line[1:].strip()
        if body.startswith("vcm_v="):
            vcm = float(body.split("=", 1)[1])
        elif body.startswith("vin_v"):
            names = body.split()
    if names is None:
        raise ValueError(f"{path}: no column header")
    data = np.loadtxt(path, ndmin=2)
    if data.shape[1] != len(names):
        raise ValueError(f"{path}: {data.shape[1]} columns, header names {len(names)}")
    to_vec = {"vin_v": "v(vin)", "vout_v": "v(vout)", **{c: v for c, v in SAT_COLUMNS}}
    return {to_vec[n]: data[:, i] for i, n in enumerate(names)}, vcm


def rederive_swing(path: Path) -> Metrics:
    """Re-run the swing extraction on a committed per-point data file."""
    vec, vcm = load_swing_dat(path)
    if vcm is None:
        raise ValueError(f"{path}: no vcm_v header (written before the saturation columns were saved)")
    return extract_swing(vec, vcm)


def same_swing(a: Metrics, b: Metrics) -> bool:
    """True when two swing extractions agree on validity, swing, edges and causes."""
    if a.valid != b.valid:
        return False
    if not a.valid:
        return True
    return (a.edge_hi, a.edge_lo) == (b.edge_hi, b.edge_lo) and all(
        math.isclose(getattr(a, f), getattr(b, f), rel_tol=0, abs_tol=1e-9)
        for f in ("swing_v", "vout_hi_v", "vout_lo_v")
    )


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def _fmt(v: float, spec: str = ".2f") -> str:
    return "n/a" if not (isinstance(v, float) and math.isfinite(v)) else format(v, spec)


def worst_primary(verdicts) -> str:
    return _fmt(verdicts['swing'].worst_value, '.3f')


def fingerprint_scope(ran) -> tuple[dict, dict]:
    """(bench texts, retained-figure selector) for the figures a record measured."""
    texts = {f: TESTBENCH[f].read_text() for f in FIGURES}
    return texts, {"figures": {f: True for f in ran}}


def build_record(
    *, record, stamp, pdk, ngspice, klt_version, backend_descs, reports, all_results, verdicts,
    ctrls, ctrl_bad, plots, dut_sha, not_run, xchk_counts, figs=FIGURES, rederived=None,
) -> str:
    L: list[str] = []
    add = L.append
    ran = [f for f in FIGURES if all_results.get(f) is not None]
    skipped = [f for f in FIGURES if f not in figs]
    add(f"# Record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    measured = ", ".join(ROWS[f][0].split(" (")[0].lower() for f in ran) or "nothing"
    add(
        f"- **Claim**: {measured} of the **committed sized schematic** "
        "(`design/opamp_two_stage.sch`, export `design/netlist/opamp_two_stage.spice`, instantiated "
        "as `opamp_two_stage`) across the full ratified PVT grid -- 5 MOS corners x 3 temperatures x 3 "
        "supplies = 45 points -- each judged against its ratified bound in `spec/target-spec.md`:"
    )
    for f in FIGURES:
        v = verdicts[f]
        if v.verdict == "NOT REQUESTED":
            add(f"  - **{v.label}: not measured in this record** (outside this run's `--figures "
                f"{','.join(figs)}`; no claim is made here -- that row's evidence is another record of this experiment).")
            continue
        if v.verdict == "NOT RUN":
            add(f"  - **{v.label}: NOT RUN** -- {not_run.get(f, 'no result')}.")
            continue
        cmp_ = ">=" if v.direction == "min" else "<="
        extra = f" (the >= {SWING_STRETCH_V:g} Vpp stretch holds at {v.stretch_pass}/{v.n_total})" if v.stretch_pass is not None else ""
        add(
            f"  - **{v.label}: {v.verdict}** vs ratified {cmp_} {v.bound:g} {v.unit} -- "
            f"{v.n_pass}/{v.n_total} points pass; worst {_fmt(v.worst_value)} {v.unit} at "
            f"{g.fmt_key(v.binding) if v.binding else 'n/a'}{extra}."
        )
    add("- **Overall**: " + (
        "every measured row passes at all 45 points." if all(v.verdict in ("PASS", "NOT RUN", "NOT REQUESTED") for v in verdicts.values()) and not not_run
        else "**at least one ratified row MISSES or was NOT RUN** -- recorded as measured; the spec is "
             "untouched, and a miss goes to a design follow-up issue citing this record, not to a "
             "resize or a relaxed target here."
    ))
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (via {pdk.source})")
    envs = {f: (reports[f].get("environment") or {}) for f in reports}
    eng = next(iter(envs.values()), {})
    add(f"- **Tools**: ngspice (local: {ngspice}; engine as run by klt: `{eng.get('engine')} {eng.get('engine_version')}`), klt `{klt_version}`")
    add(f"- **Execution**: each figure's 45 points are ONE `klt sim` corner-matrix request ({len(figs)} request"
        f"{'s' if len(figs) != 1 else ''} in all"
        + (f"; figures run: {', '.join(figs)} via `--figures`" if skipped else "") + "):")
    for f in figs:
        add(f"  - {f}: {backend_descs.get(f, 'not run')}")
    add(
        "  - `.meas` cross-checks against the rawfile extraction (power: supply current; slew: pre-edge "
        "levels; swing: output extrema) returned for "
        + ", ".join(f"{f} {xchk_counts[f][0]}/{xchk_counts[f][1]}" for f in ran)
        + " points and agreed within tolerance (a disagreement blocks the record)."
    )
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body verbatim; "
        f"normalised sha256 `{dut_sha}`), snapshotted in full in `netlist-snapshots/{record}.spice`")
    fp_texts, fp_sel = fingerprint_scope(ran)
    for ln in mc.fingerprint_lines(fp_texts, fp_sel):
        add(ln)
    add("- **Corner matrix run**:")
    add(f"  - Process (MOS): {', '.join(CORNERS)}")
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add("  - Supply VDD: " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V) + " (VCM tracks VDD/2)")
    add("  - ibias = 10 uA into `ibias`; " + "; ".join(
        {"power": "power: no load", "slew": "slew: CL = 2 pF", "swing": "swing: 1 Mohm feedback network, no CL"}[f] for f in figs))
    add(
        "- **Passive-section policy**: every MOS corner is paired with the SAME `res_typical` and "
        "`mimcap_typical` sections. The grid varies MOS corner, temperature and supply; it does **not** "
        "cover independent corners of RZ or CC (so, e.g., slew = Itail/CC is evaluated at the typical CC). "
        "No claim of passive-corner coverage is made."
    )
    add("- **Statistical convention**: N/A -- deterministic process/temperature/supply corners; no mismatch or Monte Carlo.")
    add("")
    add("## Extraction methods")
    add("")
    if "power" in figs:
        add("- **Quiescent power** = `-i(Vdd) * VDD` at `Ibias = 10 uA` (first point of a one-step DC sweep, i.e. the operating point), "
            "follower at VCM, no load. `i(Vdd)` includes the bias reference branch (total supply power). Invalid if the "
            "supply current is not positive or does not exceed the bias branch current.")
    if "slew" in figs:
        add(f"- **Slew rate**: follower, CL = 2 pF, input step {SLEW_STEP_V:g} Vpp about VCM, rising edge at {SLEW_RISE_T * 1e6:g} us and falling edge at "
            f"{SLEW_FALL_T * 1e6:g} us. Levels `vlo`/`vhi` = vout {SLEW_SAMPLE_BEFORE * 1e9:g} ns before each edge (must equal vinp to "
            f"{SLEW_LEVEL_TOL_V * 1e3:g} mV, else INVALID); slope = 0.6*(vhi-vlo)/(t80-t20) between the first {SLEW_LO_FRAC:.0%} and {SLEW_HI_FRAC:.0%} "
            "crossings (linear interpolation); reported = the SLOWER edge.")
    if "swing" in figs:
        add(f"- **Output swing**: inverting unity-gain, `Vin` swept 0..{SWING_VIN_STOP_V:g} V in {SWING_VIN_STEP_V * 1e3:g} mV steps; swing = "
            "vout(upper) - vout(lower), where each edge is the output level at the FIRST of (a) |dvout/dvin| falling below "
            f"{SWING_FRAC:.3f} (-3 dB) x its mid-range value or (b) output device M6 (upper) / M7 (lower) leaving saturation "
            "(|Vds| < |Vdsat|), walking outward from VCM. Mid-range gain must be -1 +- "
            f"{SWING_GAIN_TOL}; both devices must be saturated at VCM; a sweep that ends before either happens is INVALID.")
        add("- **Swing data committed per point** (`corners/<rid>/swing/*.dat`): every sweep sample at full precision -- "
            "`vin_v vout_v` plus the four output-device vectors the edges are decided on, "
            + ", ".join(f"`{c}`" for c, _ in SAT_COLUMNS) + " (ngspice `@m.xdut.xm6/xm7.m0[vds|vdsat]`), and a `vcm_v=` header "
            "-- so `rederive_swing()` in the run script reproduces each point's swing, edges and binding criterion from "
            "the committed file alone.")
    add("")
    add("## Verdicts (ratified rows, `spec/target-spec.md` Sec.2)")
    add("")
    add("| Row | Ratified bound | Verdict | Points passing | Worst value | Binding corner (process / T / VDD) |")
    add("|---|---|---|---|---|---|")
    for f in figs:
        v = verdicts[f]
        cmp_ = ">=" if v.direction == "min" else "<="
        add(f"| {v.label} | {cmp_} {v.bound:g} {v.unit} | **{v.verdict}** | {v.n_pass}/{v.n_total} | "
            f"{_fmt(v.worst_value)} {v.unit} | {g.fmt_key(v.binding) if v.binding else 'n/a'} |")
    add("")
    for f in FIGURES:
        res = all_results.get(f)
        if res is None:
            continue
        n_inv = sum(not m.valid for m in res.values())
        add(f"- {f}: invalid measurements (count as failing the row): {n_inv}/{len(res)}.")
    if rederived is not None:
        n_ok, n_all, bad = rederived
        add(f"- swing re-derivation from the committed `swing/*.dat` files (re-read from disk, extraction re-run): "
            f"{n_ok}/{n_all} points reproduce the swing, both edge levels and the binding criterion"
            + (" exactly." if not bad else "; **MISMATCH** at " + "; ".join(bad[:6]) + " (driver exits non-zero)."))
    add("")
    keyorder = lambda k: (CORNERS.index(k[0]), k[2], k[1])  # noqa: E731
    yn = lambda b: "ok" if b else "FAIL"  # noqa: E731
    pres, sres, wres = (all_results.get(f) for f in ("power", "slew", "swing"))
    add("## All 45 points")
    add("")
    add("| Process | T (C) | VDD (V) | Power (uW) | Idd (uA) | Slew rise (V/us) | Slew fall (V/us) | Slew min | Swing (Vpp) | vout hi (V) | vout lo (V) | swing edge cause (hi / lo) | note |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    allkeys = sorted({k for r in (pres, sres, wres) if r for k in r}, key=keyorder)
    for k in allkeys:
        p = pres.get(k) if pres else None
        s = sres.get(k) if sres else None
        w = wres.get(k) if wres else None
        notes = [f"{n}: {m.reason}" for n, m in (("power", p), ("slew", s), ("swing", w)) if m is not None and not m.valid]
        cells = [
            f"{_fmt(p.power_uw)} {yn(point_passes(p, 'power'))}" if p else "n/a",
            _fmt(p.idd_ua) if p else "n/a",
            _fmt(s.slew_rise_vus) if s else "n/a",
            _fmt(s.slew_fall_vus) if s else "n/a",
            f"{_fmt(s.slew_vus)} {yn(point_passes(s, 'slew'))}" if s else "n/a",
            f"{_fmt(w.swing_v, '.3f')} {yn(point_passes(w, 'swing'))}" if w else "n/a",
            _fmt(w.vout_hi_v, ".3f") if w else "n/a",
            _fmt(w.vout_lo_v, ".3f") if w else "n/a",
            f"{w.edge_hi} / {w.edge_lo}" if (w and w.valid) else "n/a",
        ]
        add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | " + " | ".join(cells) + f" | {'INVALID: ' + '; '.join(notes) if notes else ''} |")
    add("")
    if wres:
        add("## Swing criterion sensitivity (reported, not a verdict)")
        add("")
        def worst(key):
            vals = [m.sens.get(key, float("nan")) for m in wres.values() if m.valid]
            n_nan = sum(not math.isfinite(v) for v in vals)
            fin = [v for v in vals if math.isfinite(v)]
            if not fin:
                return "n/a (never bounded within the sweep)"
            return _fmt(min(fin), ".3f") + (f" ({n_nan} points unbounded within the sweep, excluded)" if n_nan else "")

        add("Worst-case (smallest) swing over the 45 points under variants of the criterion. The ratified "
            f"bound is judged ONLY at the primary criterion ({worst_primary(verdicts)} Vpp worst):")
        add("")
        add("| Variant | Worst swing over the grid (Vpp) |")
        add("|---|---|")
        add(f"| primary: first of -3 dB gain collapse or M6/M7 leaving saturation | {_fmt(verdicts['swing'].worst_value, '.3f')} |")
        add(f"| saturation only (gain ignored) | {worst('sat-only')} |")
        add(f"| gain collapse only, -3 dB (saturation ignored) | {worst('gain-only')} |")
        for fr in SWING_SENS_FRACS:
            add(f"| gain threshold {fr:g} x mid gain, plus saturation | {worst(fr)} |")
        causes: dict = {}
        for m in wres.values():
            if m.valid:
                for c_ in (m.edge_hi, m.edge_lo):
                    causes[c_] = causes.get(c_, 0) + 1
        add("")
        add("Criterion that bound the edge (counts over 45 points x 2 edges): "
            + ", ".join(f"{k} {v}" for k, v in sorted(causes.items())) + ".")
        add("")
    add("## Controls (single local units at the nominal point; recorded separately from the grid evidence)")
    add("")
    add("| Control | Condition | Power (uW) | Slew (V/us) | Swing (Vpp) | note |")
    add("|---|---|---|---|---|---|")
    for c in ctrls:
        cells = []
        notes = []
        for f, attr, fm in (("power", "power_uw", ".1f"), ("slew", "slew_vus", ".2f"), ("swing", "swing_v", ".3f")):
            m = c.metrics.get(f)
            if m is None and f not in figs:
                cells.append("not requested")
            elif m is None:
                cells.append("not simulated")
                notes.append(f"{f}: {c.errors.get(f, '')[:80]}")
            elif m.valid:
                cells.append(_fmt(getattr(m, attr), fm))
            else:
                cells.append("INVALID")
                notes.append(f"{f}: {m.reason[:100]}")
        add(f"| `{c.name}` | {c.description} | " + " | ".join(cells) + f" | {'; '.join(notes)} |")
    add("")
    add("Required behaviour (for the figures run): ibias-half lowers both power (< 0.75x) and slew (< 0.8x); ibias-zero never passes slew or swing and draws < 0.1x the nominal power.")
    if ctrl_bad:
        add("")
        add("**CONTROL PROBLEMS** (driver exits non-zero):")
        for s in ctrl_bad:
            add(f"- {s}")
    add("")
    L.extend(mc.inputs_section(fp_texts, fp_sel))
    add("## Plots")
    add("")
    for p in plots:
        add(f"- `sim/slew-swing-power/records/{record}-plots/{p}`")
    add("")
    add("## Links")
    add("")
    add("- Testbenches: " + ", ".join(f"`sim/slew-swing-power/testbench/tb_{f}.spice`" for f in figs))
    add("- Run script: `sim/slew-swing-power/run_slew_swing_power.py`; extraction/guard tests: `sim/slew-swing-power/test_slew_swing_power.py`")
    add(f"- Netlist snapshot (DUT + testbenches + conditions): `sim/slew-swing-power/netlist-snapshots/{record}.spice`")
    add(f"- Per-point logs, decks and data, the sanitised klt reports and the control runs: `sim/slew-swing-power/corners/{record}/`")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #44)")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Opt-in passive-corner side study (issue #97; DR-3 section (d) obligation)
# --------------------------------------------------------------------------

#: Title prefix of the side-study record. `sim/report` skips records starting
#: with this (STUDY_TITLES), so the aggregate report never selects it.
PASSIVE_TITLE = "# slew/swing/power passive-corner study"


def passive_request(fig: str, netlist: Path, pdk: Pdk, points=pc.PASSIVE_POINTS) -> dict:
    """ONE corner-matrix request for one figure: MOS x RZ x CC x T x VDD, with
    the cross-product cells that are not study points excluded (27 cells)."""
    temps = sorted({p[1] for p in points})
    vdds = sorted({p[2] for p in points})
    return pc.apply_passive_matrix(make_request(fig, netlist, pdk, [], temps, vdds), points)


def analyse_passive_report(fig: str, report: dict, want: list[Key]):
    """`analyse_report` plus a check that every cell's generated deck loads the
    PDK sections its name encodes. Returns (results, arts, problems)."""
    results, arts, problems = analyse_report(fig, report, want)
    for k, a in arts.items():
        deck = a.get("deck")
        if not deck or not Path(deck).is_file():
            problems.append(f"{fig}: no generated deck retained for {g.fmt_key(k)}; cannot verify its sections")
            continue
        problems += [f"{fig}: {p}" for p in pc.deck_section_problems(k, Path(deck).read_text())]
    return results, arts, problems


def passive_summary(results: dict[Key, Metrics], fig: str, points=pc.PASSIVE_POINTS) -> dict:
    """Per study point: {(res, mim): (metrics, relative change vs both-typical in %, passes)}."""
    _, attr, _, _, _ = ROWS[fig]
    out: dict = {}
    for (m, t, v) in points:
        base = results.get((pc.passive_name(m, "typical", "typical"), t, v))
        rows = {}
        for r_, c_ in pc.passive_combos():
            mt = results.get((pc.passive_name(m, r_, c_), t, v))
            if mt is None:
                continue
            ok = base is not None and base.valid and mt.valid
            d = 100 * (getattr(mt, attr) / getattr(base, attr) - 1) if ok else float("nan")
            rows[(r_, c_)] = (mt, d, point_passes(mt, fig))
        out[(m, t, v)] = rows
    return out


def _cell_label(k: Key) -> str:
    mos, r_, c_ = pc.split_passive_key(k)
    return f"{mos} / {k[1]:g} C / {k[2]:.2f} V, RZ {r_}, CC {c_}"


def _val(m: Metrics, fig: str) -> str:
    if not m.valid:
        return "INVALID"
    return {"power": f"{m.power_uw:.1f}", "slew": f"{m.slew_vus:.2f}", "swing": f"{m.swing_v:.3f}"}[fig]


def build_passive_record(
    *, record, stamp, pdk, ngspice, klt_version, backend_descs, all_results, verdicts, ctrls, ctrl_bad,
    dut_sha, not_run, figs=FIGURES, base_records: str = "",
) -> str:
    L: list[str] = []
    add = L.append
    ran = [f for f in FIGURES if all_results.get(f) is not None]
    ncell = len(pc.passive_expected_keys())
    add(f"{PASSIVE_TITLE} (RZ x CC) -- record {record}")
    add("")
    add(f"- **Date (UTC)**: {stamp:%Y-%m-%d %H:%M:%S}")
    add("- **Issue**: #97 (DR-3 section (d) obligation; replaces the analytic slew scaling of the gain/GBW/PM passive study with a measurement)")
    add("- **Record kind**: SIDE STUDY. It does not judge, replace or supersede any aggregate-report row; "
        f"the default 45-point grid records (typical passives) stay authoritative{(': ' + base_records) if base_records else ''}. "
        "No spec row or bound is edited here.")
    add("- **Verdict against the ratified bounds (`spec/target-spec.md` Sec.2), measured (not analytic) per RZ x CC cell**:")
    for f in FIGURES:
        v = verdicts[f]
        if f not in figs:
            add(f"  - **{v.label}: not measured in this record** (outside this run's `--figures {','.join(figs)}`).")
        elif v.verdict == "NOT RUN":
            add(f"  - **{v.label}: NOT RUN** -- {not_run.get(f, 'no result')}.")
        else:
            cmp_ = ">=" if v.direction == "min" else "<="
            worst = f"{v.worst_value:.4g} {v.unit}" if math.isfinite(v.worst_value) else "n/a (an invalid cell)"
            at = _cell_label(v.binding) if v.binding else "n/a"
            add(f"  - **{v.label}: {v.verdict}** vs ratified {cmp_} {v.bound:g} {v.unit} -- passes at {v.n_pass}/{v.n_total} "
                f"cells (fails at {v.n_total - v.n_pass}); worst {worst} at {at}.")
    if ctrl_bad:
        add("  - **CONTROL PROBLEMS**: see Controls; the evidence is not complete.")
    add("")
    add("## Conditions")
    add("")
    add(f"- **PDK**: {pdk.path} (open_pdks {pdk.version}); ngspice {ngspice}; klt {klt_version}")
    add("- **Execution** (per figure, each ONE `klt sim` corner-matrix request of "
        f"{ncell} cells):")
    for f in ran:
        add(f"  - {f}: {backend_descs.get(f, '')}")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (normalised sha256 `{dut_sha}`); snapshot `netlist-snapshots/{record}.spice`")
    add("- **Points**: " + "; ".join(f"{m} / {t:g} C / {v:.2f} V" for m, t, v in pc.PASSIVE_POINTS)
        + f", each x 3 RZ levels x 3 CC levels = {ncell} cells; ibias = 10 uA, same benches and extraction as the 45-point grid.")
    add("- **Passive-section policy (swept)**: RZ (`ppolyf_u_1k`) uses `res_typical` / `res_ff` (best, 0.8x) / `res_ss` (worst, 1.2x); "
        "CC (`cap_mim_2f0_m4m5_noshield`) uses `mimcap_typical` / `mimcap_ff` (best, 0.9x) / `mimcap_ss` (worst, 1.1x). "
        "All nine combinations run independently at each point. Each cell's generated deck was checked to load exactly the "
        "sections its name encodes. \"best\"/\"worst\" are the PDK ff/ss labels, not a claim about which is worse for a row; the tables decide.")
    add("")
    add("## Results")
    for f in ran:
        label, _, bound, unit, direction = ROWS[f]
        cmp_ = ">=" if direction == "min" else "<="
        summ = passive_summary(all_results[f], f)
        for (m, t, v), rows in summ.items():
            add("")
            add(f"### {label} -- {m} / {t:g} C / {v:.2f} V (bound {cmp_} {bound:g} {unit})")
            add("")
            add(f"| RZ | CC | {unit} | change vs typ./typ. (%) | vs bound |")
            add("|---|---|---|---|---|")
            for (r_, c_), (mt, d, ok) in rows.items():
                dd = f"{d:+.1f}" if math.isfinite(d) else "n/a"
                add(f"| {r_} | {c_} | {_val(mt, f)} | {dd} | {'PASS' if ok else 'FAIL'} |")
    add("")
    add("## Controls (single local units at the nominal point, typical passives)")
    add("")
    add("| Control | Condition | Power (uW) | Slew (V/us) | Swing (Vpp) | note |")
    add("|---|---|---|---|---|---|")
    for c in ctrls:
        cells, notes = [], []
        for f in FIGURES:
            m = c.metrics.get(f)
            if m is None and f not in figs:
                cells.append("not requested")
            elif m is None:
                cells.append("not simulated")
                notes.append(f"{f}: {c.errors.get(f, '')[:80]}")
            elif m.valid:
                cells.append(_val(m, f))
            else:
                cells.append("INVALID")
                notes.append(f"{f}: {m.reason[:100]}")
        add(f"| `{c.name}` | {c.description} | " + " | ".join(cells) + f" | {'; '.join(notes)} |")
    add("")
    add("Required behaviour (for the figures run): ibias-half lowers both power (< 0.75x) and slew (< 0.8x); ibias-zero never passes slew or swing and draws < 0.1x the nominal power.")
    if ctrl_bad:
        add("")
        add("**CONTROL PROBLEMS**:")
        for s in ctrl_bad:
            add(f"- {s}")
    add("")
    add("## Artifacts")
    add("")
    add("- Runner: `sim/slew-swing-power/run_slew_swing_power.py --passive-corners`; shared helpers: `sim/passive_corners.py`; tests: `sim/slew-swing-power/test_slew_swing_power.py`")
    add(f"- Per-cell logs, decks, data, sanitised klt reports and controls: `sim/slew-swing-power/corners/{record}/`")
    add("")
    return "\n".join(L)


def run_passive(pdk: Pdk, args, figs) -> int:
    want = pc.passive_expected_keys()
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: passive-corner study, {len(want)} cells x {len(figs)} figures ({', '.join(figs)}), PDK={pdk.path}, klt {kver}")
    all_results: dict[str, dict[Key, Metrics] | None] = {}
    all_arts: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    reqs: dict[str, dict] = {}
    not_run: dict[str, str] = {}
    backend_descs: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="ssp-pas-") as scratch:
        work = Path(scratch)
        for fig in figs:
            tb = materialise(fig, work / fig, pdk)
            req = passive_request(fig, tb, pdk)
            req["batch"] = batch_block(args)
            reqs[fig] = req
            try:
                report = run_klt_retrying(
                    req, work / fig / "out", args.backend, work / fig,
                    retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
                )
            except KltError as exc:
                # never fall back to a local grid
                not_run[fig] = f"the klt request could not be run: {str(exc)[-400:]}"
                print(f"ERROR: {fig}: {not_run[fig]}", file=sys.stderr)
                all_results[fig] = None
                continue
            results, arts, problems = analyse_passive_report(fig, report, want)
            if problems:
                not_run[fig] = "the passive-corner study did not complete cleanly: " + "; ".join(problems[:6])
                print(f"ERROR: {fig}: {not_run[fig]}", file=sys.stderr)
                all_results[fig] = None
                continue
            all_results[fig], all_arts[fig], reports[fig] = results, arts, report
            remote = (report.get("environment") or {}).get("remote") or {}
            backend_descs[fig] = (
                f"`klt sim` backend `{remote.get('provider', 'local')}`"
                + (f", job id `{remote.get('job_id')}`, {('Spot ' if remote.get('spot') else 'on-demand ')}{remote.get('instance_type')}, "
                   f"runner klt `{remote.get('runner_klt_version')}` vs client `{remote.get('client_klt_version')}` "
                   f"(compatibility `{remote.get('runner_compatibility')}`)" if remote else "")
            )
            print(f"  {fig}: {len(results)} cells ok ({backend_descs[fig]})")
        if not_run:
            # An incomplete side study is not evidence: write nothing.
            print("ERROR: passive-corner study incomplete; NO RECORD WRITTEN.", file=sys.stderr)
            return 2
        ctrls = run_controls(pdk, work, figs)
        ctrl_bad = control_failures(ctrls)
        verdicts = {f: judge_figure(f, all_results.get(f), requested=f in figs) for f in FIGURES}

        cdir = paths["corners"]
        cdir.mkdir(parents=True, exist_ok=False)
        for fig, arts in all_arts.items():
            fdir = cdir / fig
            fdir.mkdir()
            for k, a in arts.items():
                stem = g.point_stem(k)
                if a["log"]:
                    shutil.copyfile(a["log"], fdir / f"{stem}.log")
                if a["deck"]:
                    shutil.copyfile(a["deck"], fdir / f"{stem}.cir")
                save_point_data(fig, all_results[fig][k], fdir / f"{stem}.dat", vcm=k[2] / 2)
            (fdir / "klt-report.json").write_text(json.dumps(sanitise_report(reports[fig]), indent=1))
        ccdir = cdir / "controls"
        ccdir.mkdir()
        for c in ctrls:
            for fig, fl in c.files.items():
                for kind, src in fl.items():
                    if src and Path(src).is_file():
                        shutil.copyfile(src, ccdir / f"{c.name}-{fig}.{'log' if kind == 'log' else 'cir'}")

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        lines = [f"* netlist snapshot for record {record} (issue #97, passive-corner study)"]
        for fig in figs:
            lines += [f"* ---- conditions: klt sim request ({fig}) ----"]
            lines += ["* " + ln for ln in json.dumps({k: v for k, v in reqs[fig].items() if k != "netlist"}, indent=1).splitlines()]
        lines += ["", "* ---- DUT (wrapper-normalised) ----", dut_text]
        for fig in figs:
            lines += [f"* ---- testbench: sim/slew-swing-power/testbench/tb_{fig}.spice (verbatim) ----", TESTBENCH[fig].read_text(), ""]
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join(lines))

        base_records = ""
        try:
            sel = json.loads((REPO_ROOT / "sim" / "report" / "selection.json").read_text())["experiments"]["slew-swing-power"]
            base_records = ", ".join(sorted({f"`{Path(p).stem}`" for p in (sel.values() if isinstance(sel, dict) else [sel])}))
        except (OSError, KeyError, ValueError, AttributeError):
            pass
        md = build_passive_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_version=kver,
            backend_descs=backend_descs, all_results=all_results, verdicts=verdicts, ctrls=ctrls,
            ctrl_bad=ctrl_bad, dut_sha=dut_sha, not_run=not_run, figs=figs, base_records=base_records,
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)
    print(f"wrote {paths['record']}")
    for f in figs:
        v = verdicts[f]
        print(f"  {v.label}: {v.verdict} ({v.n_pass}/{v.n_total}), worst {v.worst_value:.4g} {v.unit} at {_cell_label(v.binding) if v.binding else 'n/a'}")
    rc = 0
    if ctrl_bad:
        print("CONTROL PROBLEMS:")
        for s in ctrl_bad:
            print(f"  - {s}")
        rc = 1
    if args.strict and any(v.verdict == "FAIL" for v in verdicts.values()):
        rc = 1
    return rc


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    bad = 0
    with tempfile.TemporaryDirectory(prefix="ssp-smoke-") as scratch:
        for fig in FIGURES:
            try:
                m, _, problems = run_unit(fig, "smoke", pdk, Path(scratch), None)
            except (KltError, RuntimeError) as exc:
                print(f"SMOKE TEST FAILED ({fig}): {exc}")
                return 1
            if not m.valid or problems:
                print(f"SMOKE TEST FAILED ({fig}): {m.reason or problems}")
                bad += 1
                continue
            val = {"power": f"{m.power_uw:.1f} uW", "slew": f"{m.slew_vus:.2f} V/us", "swing": f"{m.swing_v:.3f} Vpp"}[fig]
            print(f"  {fig}: {val}")
    if bad:
        return 1
    print("smoke test OK (measurements valid; spec verdicts are the full run's job)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one nominal point per figure, local, no record")
    ap.add_argument("--passive-corners", action="store_true",
                    help="opt-in side study (issue #97): RZ x CC passive corners (3x3 independent res/mimcap sections) "
                         "at fs/125C/2.97V, ss/125C/2.97V and nominal, one klt corner-matrix request per figure; mints a "
                         "side-study record the aggregate report never selects")
    ap.add_argument("--backend", help="klt execution backend for the 45-point grids (default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a ratified row misses")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet capacity (never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--figures", default=",".join(FIGURES),
                    help="comma-separated subset of power,slew,swing to run (default: all; one klt request per figure). "
                         "Rows outside the subset are recorded as not measured, never as a pass.")
    args = ap.parse_args(argv)
    figs = tuple(f for f in FIGURES if f in {x.strip() for x in args.figures.split(",")})
    unknown = {x.strip() for x in args.figures.split(",")} - set(FIGURES)
    if unknown or not figs:
        ap.error(f"--figures: choose from {', '.join(FIGURES)} (got {args.figures!r})")

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    if args.passive_corners:
        return run_passive(pdk, args, figs)

    want = g.expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    claim_record_paths(HERE, record)  # fail early if this id was already used
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: {len(want)} points x {len(figs)} figures ({', '.join(figs)}), PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    all_results: dict[str, dict[Key, Metrics] | None] = {}
    all_arts: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    reqs: dict[str, dict] = {}
    not_run: dict[str, str] = {}
    backend_descs: dict[str, str] = {}
    xchk_counts: dict[str, tuple[int, int]] = {}

    with tempfile.TemporaryDirectory(prefix="ssp-") as scratch:
        work = Path(scratch)
        for fig in figs:
            tb = materialise(fig, work / fig, pdk)
            req = make_request(fig, tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
            req["batch"] = batch_block(args)
            reqs[fig] = req
            try:
                report = run_klt_retrying(
                    req, work / fig / "out", args.backend, work / fig,
                    retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
                )
            except KltError as exc:
                not_run[fig] = f"the klt request could not be run: {str(exc)[-400:]}"
                print(f"ERROR: {fig}: {not_run[fig]}", file=sys.stderr)
                all_results[fig] = None
                continue
            results, arts, problems = analyse_report(fig, report, want)
            if problems:
                not_run[fig] = "the 45-point grid did not complete cleanly: " + "; ".join(problems[:6])
                print(f"ERROR: {fig}: {not_run[fig]}", file=sys.stderr)
                all_results[fig] = None
                continue
            all_results[fig], all_arts[fig], reports[fig] = results, arts, report
            remote = (report.get("environment") or {}).get("remote") or {}
            backend_descs[fig] = (
                f"`klt sim` backend `{remote.get('provider', 'local')}`"
                + (f", job id `{remote.get('job_id')}`, {('Spot ' if remote.get('spot') else 'on-demand ')}{remote.get('instance_type')}, "
                   f"runner klt `{remote.get('runner_klt_version')}` vs client `{remote.get('client_klt_version')}` "
                   f"(compatibility `{remote.get('runner_compatibility')}`)" if remote else "")
            )
            names = {"power": ("ivdd_a", "vout_v"), "slew": ("vlo_v", "vhi_v"), "swing": ("vout_max_v", "vout_min_v")}[fig]
            xchk_counts[fig] = (sum(all(a["klt_meas"].get(n) is not None for n in names) for a in arts.values()), len(arts))
            print(f"  {fig}: 45 points ok ({backend_descs[fig]})")

        if not any(r is not None for r in all_results.values()):
            print("ERROR: no figure could be run; NO RECORD WRITTEN.", file=sys.stderr)
            return 2

        ctrls = run_controls(pdk, work, figs)
        ctrl_bad = control_failures(ctrls)
        verdicts = {f: judge_figure(f, all_results.get(f), requested=f in figs) for f in FIGURES}

        corners_dir = HERE / "corners" / record
        corners_dir.mkdir(parents=True, exist_ok=False)
        for fig, arts in all_arts.items():
            fdir = corners_dir / fig
            fdir.mkdir()
            for k, a in arts.items():
                stem = g.point_stem(k)
                if a["log"]:
                    shutil.copyfile(a["log"], fdir / f"{stem}.log")
                if a["deck"]:
                    shutil.copyfile(a["deck"], fdir / f"{stem}.cir")
                save_point_data(fig, all_results[fig][k], fdir / f"{stem}.dat", vcm=k[2] / 2)
            (fdir / "klt-report.json").write_text(json.dumps(sanitise_report(reports[fig]), indent=1))
        # Prove the committed swing data is sufficient: re-read every swing .dat
        # and re-run the extraction; any disagreement is a driver bug and blocks.
        rederived = None
        rederive_bad: list[str] = []
        if all_results.get("swing"):
            n_ok = 0
            for k, m in all_results["swing"].items():
                try:
                    m2 = rederive_swing(corners_dir / "swing" / f"{g.point_stem(k)}.dat")
                except (ValueError, OSError) as exc:
                    rederive_bad.append(f"{g.fmt_key(k)}: {exc}")
                    continue
                if same_swing(m, m2):
                    n_ok += 1
                else:
                    rederive_bad.append(f"{g.fmt_key(k)}: {m2.swing_v:.6f} Vpp ({m2.edge_hi}/{m2.edge_lo}) vs {m.swing_v:.6f} Vpp ({m.edge_hi}/{m.edge_lo})")
            rederived = (n_ok, len(all_results["swing"]), rederive_bad)
        cdir = corners_dir / "controls"
        cdir.mkdir()
        for c in ctrls:
            for fig, fl in c.files.items():
                for kind, src in fl.items():
                    if src and Path(src).is_file():
                        shutil.copyfile(src, cdir / f"{c.name}-{fig}.{'log' if kind == 'log' else 'cir'}")

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        snap = HERE / "netlist-snapshots"
        snap.mkdir(parents=True, exist_ok=True)
        lines = [
            f"* netlist snapshot for record {record} (issue #44)",
            "* Reproduces the measured design: DUT contents, testbenches, conditions.",
        ]
        for fig in figs:
            lines += [f"* ---- conditions: klt sim request ({fig}) ----"]
            lines += ["* " + ln for ln in json.dumps({k: v for k, v in reqs[fig].items() if k != "netlist"}, indent=1).splitlines()]
        lines += [
            "",
            "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
            dut_text,
        ]
        for fig in figs:
            lines += [f"* ---- testbench: sim/slew-swing-power/testbench/tb_{fig}.spice (verbatim) ----", TESTBENCH[fig].read_text()]
            a0 = all_arts.get(fig, {}).get(NOMINAL)
            if a0 and a0["deck"]:
                lines += [f"* ---- klt-generated deck of the nominal point ({fig}) ----"]
                lines += ["* | " + ln for ln in Path(a0["deck"]).read_text().splitlines()] + [""]
        (snap / f"{record}.spice").write_text("\n".join(lines))

        plots = build_plots(all_results, HERE / "records" / f"{record}-plots")
        md = build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_version=kver,
            backend_descs=backend_descs, reports=reports, all_results=all_results, verdicts=verdicts,
            ctrls=ctrls, ctrl_bad=ctrl_bad, plots=plots, dut_sha=dut_sha, not_run=not_run, xchk_counts=xchk_counts,
            figs=figs, rederived=rederived,
        )
        out = HERE / "records" / f"{record}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)

    print(f"wrote {out}")
    for f in figs:
        v = verdicts[f]
        print(f"  {v.label}: {v.verdict} ({v.n_pass}/{v.n_total}), worst {v.worst_value:.4g} {v.unit} at {g.fmt_key(v.binding) if v.binding else 'n/a'}")
    rc = 0
    if rederive_bad:
        print("SWING RE-DERIVATION MISMATCH:")
        for s_ in rederive_bad:
            print(f"  - {s_}")
        rc = 1
    if ctrl_bad:
        print("CONTROL PROBLEMS:")
        for s in ctrl_bad:
            print(f"  - {s}")
        rc = 1
    if not_run:
        rc = 1
    if args.strict and any(v.verdict == "FAIL" for v in verdicts.values()):
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
