#!/usr/bin/env python3
"""Power-supply rejection (PSRR+ / PSRR-) of the committed sized schematic
across the full ratified PVT grid (issue #39; tracker #7 items 5 and 9; DR-3
residual (e4)).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_psrr.spice`) declares no transistor, exposes the
DUT's `vss` port as a driven node, and uses the CMRR bench's DC servo.

What it runs
------------
Three excitations of the same bench, each the 45-point grid `process
{typical, ff, ss, fs, sf} x temperature {-40, 27, 125 C} x supply {2.97, 3.30,
3.63 V}` as ONE `klt sim` corner request (three requests):

  * `dm`   differential input, +0.5 / -0.5 V AC, rails AC-quiet  -> Ad
  * `vdd`  1 V AC on the vdd rail only                            -> Avdd
  * `vss`  1 V AC on the DUT's vss rail only                      -> Avss

Input-referred PSRR+ = 20 log10 |Ad / Avdd|, PSRR- = 20 log10 |Ad / Avss|,
with Asupply = vout / v(rail) from the actual rail phasor. This is NOT the
output supply feedthrough -20 log10 |Asupply|, which is reported separately
and labelled as such. References: inputs, the DC servo, CL and Vdd are
referenced to simulator ground; the DUT's vss is driven (PSRR-); the ideal
10 uA bias source carries no small-signal current, so bias-generator supply
rejection is EXCLUDED.

Which backend executes the grids is `klt`'s decision (`--backend`, the
request's `backend`, `$KLT_SIM_BACKEND`; the Spot batch fleet on a dispatch
worker). No local grid fallback; single-unit studies run locally.

Reported per point: PSRR+ and PSRR- at "DC" (the verified 0.1-1 Hz plateau,
or flagged unavailable), at 1 kHz, 10 kHz, 100 kHz, 1 MHz and at the
differential unity-gain frequency f_u (linear interpolation in (log10 f, dB));
the worst-case corner of each figure.

The result is MEASURED: no pass or fail verdict is issued because the row's
bound is not ratified. `spec/target-spec.md` is never edited.

Usage:
    python3 sim/psrr/run_psrr.py                  # full grid + record
    python3 sim/psrr/run_psrr.py --smoke          # nominal point, local, no record
    python3 sim/psrr/run_psrr.py --backend local  # force a backend

Exit status: 0 when the evidence is complete and validated; 2 when a grid
could not run or failed validation (no record is written).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))

from harness import Pdk, allocate_record_id, find_pdk, ngspice_version  # noqa: E402

# The CMRR driver owns the servo bench guards, excitation checks, rejection
# summaries, klt plumbing and evidence writers; reuse them unchanged.
if "cmrr_driver" in sys.modules:
    C = sys.modules["cmrr_driver"]
else:
    _spec = importlib.util.spec_from_file_location("cmrr_driver", REPO_ROOT / "sim" / "cmrr" / "run_cmrr.py")
    C = importlib.util.module_from_spec(_spec)
    sys.modules["cmrr_driver"] = C
    _spec.loader.exec_module(C)
G = C.G

TESTBENCH = HERE / "testbench" / "tb_psrr.spice"

CORNERS, TEMPS_C, SUPPLIES_V, NOMINAL = C.CORNERS, C.TEMPS_C, C.SUPPLIES_V, C.NOMINAL
Key = C.Key
fmt_key, point_stem = C.fmt_key, C.point_stem
SPOT_HZ = C.SPOT_HZ
db = C.db

QUIET = {"acp": 0.0, "acn": 0.0, "acdd": 0.0, "acss": 0.0}
MODES = {
    "dm": {**QUIET, "acp": 0.5, "acn": -0.5},
    "vdd": {**QUIET, "acdd": 1.0},
    "vss": {**QUIET, "acss": 1.0},
}
RAILS = {"vdd": "v(vdd)", "vss": "v(vss)"}
NEED = ("v(vout)", "v(vinp)", "v(vinn)", "v(vdd)", "v(vss)")
LABEL = {"vdd": "PSRR+", "vss": "PSRR-"}

#: Supply runs: both input pins must stay AC-quiet to this (V per V of rail).
INPUT_QUIET_V = 1e-9
#: ... and the differential leak's contribution |Ad vd| must be this small a
#: fraction of the measured output.
INPUT_LEAK_FRAC = 1e-6
#: Feedthrough fixture (stimulus-fixture negative control): a resistor from
#: the driven rail to vout, appended to the bench only for the control.
FEEDTHROUGH_R = 100e3


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def guard_testbench(text: str) -> list[str]:
    errs = C.guard_common(text)
    code = C.code_lines(text)
    C._require(code, r"^Xdut\s+vdd\s+vss\s+vinp\s+vinn\s+vout\s+ibias\s+opamp_two_stage$",
               "Xdut must be `Xdut vdd vss vinp vinn vout ibias opamp_two_stage` (vss a driven node, not 0)", errs)
    C._require(code, r"^Vss\s+vss\s+0\s+dc\s+0\s+ac\s+\{acss\}$", "testbench lost `Vss vss 0 dc 0 ac {acss}`", errs)
    C._require(code, r"^Vdd\s+vdd\s+0\s+dc\s+\S+\s+ac\s+\{acdd\}$", "testbench lost `Vdd vdd 0 dc ... ac {acdd}`", errs)
    for ln in code:
        if re.match(r"^(CL|Csv|Esb|Vcm|Vnac)\b", ln, re.I) and re.search(r"\svss\b", ln, re.I):
            errs.append(f"stimulus/load must be referenced to ground, not vss: {ln[:60]}")
    return errs


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class PsrrPoint:
    valid: bool
    reason: str = ""
    freq: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    ad: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    asup: dict = field(default_factory=dict)  # rail -> complex array
    ad_dc_db: float = float("nan")
    fu_hz: float = float("nan")
    rej: dict = field(default_factory=dict)  # rail -> Rejection
    feedthrough_db: dict = field(default_factory=dict)  # rail -> -20log|Asupply| at the plateau
    exc_err: float = float("nan")
    input_quiet: float = float("nan")
    leak_frac: float = float("nan")


def check_supply_run(vec: dict, rail: str) -> tuple[float, float, list[str]]:
    """Supply run: the driven rail at 1 V, the other rail and both inputs AC-quiet."""
    other = "vss" if rail == "vdd" else "vdd"
    err = float(max(np.max(np.abs(vec[RAILS[rail]] - 1)), np.max(np.abs(vec[RAILS[other]]))))
    quiet = float(max(np.max(np.abs(vec["v(vinp)"])), np.max(np.abs(vec["v(vinn)"]))))
    bad = []
    if err > C.EXC_TOL:
        bad.append(f"[{rail}] rail excitation off by {err:.3g} V (> {C.EXC_TOL:g}): wrong rail driven or other rail not quiet")
    if quiet > INPUT_QUIET_V:
        bad.append(f"[{rail}] inputs not AC-quiet ({quiet:.3g} V > {INPUT_QUIET_V:g}): servo isolation or reference")
    return err, quiet, bad


def extract_psrr(dm_vec: dict, sup_vecs: dict[str, dict], floor: float, *, n_expect: int | None = C.N_FREQ) -> PsrrPoint:
    try:
        freq = np.asarray(dm_vec["frequency"].real, dtype=float)
        C.check_axis(freq, n_expect)
        for rail, v in sup_vecs.items():
            C.same_axis(freq, np.asarray(v["frequency"].real, dtype=float))
            C.finite(*(v[n] for n in NEED))
        C.finite(*(dm_vec[n] for n in NEED))
        dm = C.Excitation.from_vec(dm_vec)
        dm_err, bad = C.check_dm(dm)
        rails_quiet = float(max(np.max(np.abs(dm_vec["v(vdd)"])), np.max(np.abs(dm_vec["v(vss)"]))))
        if rails_quiet > C.EXC_TOL:
            bad.append(f"[dm] rails not AC-quiet ({rails_quiet:.3g} V)")
        errs, quiets = [dm_err], []
        for rail, v in sup_vecs.items():
            e, q, b = check_supply_run(v, rail)
            errs.append(e)
            quiets.append(q)
            bad += b
        if bad:
            return PsrrPoint(False, "; ".join(bad), freq=freq)
        ad = dm.vout / dm.vd
        m = G.extract_metrics(freq, ad)
        if not m.valid:
            return PsrrPoint(False, f"differential response invalid: {m.reason}", freq=freq)
        pt = PsrrPoint(True, freq=freq, ad=ad, ad_dc_db=m.dc_gain_db, fu_hz=m.gbw_hz,
                       exc_err=max(errs), input_quiet=max(quiets) if quiets else 0.0)
        leak = 0.0
        band = freq <= freq[0] * C.PLATEAU_DECADE_HI
        for rail, v in sup_vecs.items():
            x = C.Excitation.from_vec(v)
            leak = max(leak, float(np.max(np.abs(ad * x.vd) / np.maximum(np.abs(x.vout), floor))))
            a = v["v(vout)"] / v[RAILS[rail]]
            pt.asup[rail] = a
            pt.rej[rail] = C.summarise_rejection(freq, ad, a, m.gbw_hz, floor)
            pt.feedthrough_db[rail] = float(-np.mean(db(np.maximum(np.abs(a), floor))[band]))
        pt.leak_frac = leak
        if leak > INPUT_LEAK_FRAC:
            return PsrrPoint(False, f"input leak contributes {leak:.3g} of the supply-run output (> {INPUT_LEAK_FRAC:g})",
                             freq=freq)
        return pt
    except (C.ExtractionError, KeyError) as exc:
        return PsrrPoint(False, str(exc))


# --------------------------------------------------------------------------
# Studies and controls (local, nominal)
# --------------------------------------------------------------------------


@dataclass
class Studies:
    nominal: PsrrPoint | None
    floor: float
    floor_resid: float
    iso: list
    inadequate: PsrrPoint | None
    feedthrough: dict  # rail -> PsrrPoint
    runs: list
    problems: list[str]


def run_one(pdk, work, tag, desc, params, **kw):
    return C.run_local(tag, desc, pdk, work, params, testbench=TESTBENCH, guard=guard_testbench, need=NEED, **kw)


def psrr_triple(pdk, work, tag, desc, floor, *, over=None, fixture_for: dict | None = None):
    """dm + vdd + vss local units -> PsrrPoint. `fixture_for` (fixture lines)
    is appended to ALL three runs of a control, so Ad and Asupply are measured
    on the same fixture."""
    over = over or {}
    runs = {}
    for mode, mp in MODES.items():
        runs[mode] = run_one(pdk, work, f"{tag}-{mode}", f"{desc} ({mode})", C.with_servo(mp, **over),
                             fixture=tuple(fixture_for or ()))
    if any(r.vec is None for r in runs.values()):
        return PsrrPoint(False, "did not simulate: " + "; ".join(r.error for r in runs.values() if r.error)), list(runs.values())
    pt = extract_psrr(runs["dm"].vec, {"vdd": runs["vdd"].vec, "vss": runs["vss"].vec}, floor)
    return pt, list(runs.values())


def run_psrr_studies(pdk: Pdk, work: Path) -> Studies:
    probs: list[str] = []
    runs = []
    # Numerical floor: both rails together vs each alone.
    b = run_one(pdk, work, "floor-both", "acdd = acss = 1", C.with_servo({**QUIET, "acdd": 1.0, "acss": 1.0}))
    p = run_one(pdk, work, "floor-vdd-only", "acdd = 1", C.with_servo(MODES["vdd"]))
    n = run_one(pdk, work, "floor-vss-only", "acss = 1", C.with_servo(MODES["vss"]))
    runs += [b, p, n]
    resid, err = C.superposition_floor(b, [p, n])
    if err:
        probs.append(err)
        floor = C.FLOOR_MIN
    else:
        floor = max(C.FLOOR_MARGIN * resid, C.FLOOR_MIN)
    nominal, r = psrr_triple(pdk, work, "nominal", "nominal servo", floor)
    runs += r
    if not nominal.valid:
        probs.append(f"local nominal triple invalid: {nominal.reason}")
    iso = []
    for c in C.ISOLATION_CSV:
        pt, r = psrr_triple(pdk, work, f"iso-csv{c:g}", f"servo tau = {C.SERVO_NOMINAL['rsv'] * c:g} s", floor,
                            over={"csv": c})
        runs += r
        iso.append((f"tau = {C.SERVO_NOMINAL['rsv'] * c:g} s", pt))
    inad, r = psrr_triple(pdk, work, "iso-inadequate", "servo tau = 1e-3 s (inadequate)", floor,
                          over={"csv": C.ISOLATION_INADEQUATE_CSV})
    runs += r
    feed = {}
    for rail in RAILS:
        pt, r = psrr_triple(pdk, work, f"control-feedthrough-{rail}", f"fixture Rft {rail} vout {FEEDTHROUGH_R:g}",
                            floor, fixture_for=(f"Rft {rail} vout {FEEDTHROUGH_R:g}",))
        runs += r
        feed[rail] = pt

    if nominal.valid:
        for label, pt in iso:
            if not pt.valid:
                probs.append(f"isolation {label}: invalid ({pt.reason})")
                continue
            for rail in RAILS:
                a, bb = pt.rej[rail], nominal.rej[rail]
                d = max(abs(a.low_db - bb.low_db), abs(a.at_fu_db - bb.at_fu_db),
                        *(abs(a.spot_db[f] - bb.spot_db[f]) for f in SPOT_HZ))
                if d > C.ISOLATION_TOL_DB:
                    probs.append(f"isolation {label}: {LABEL[rail]} moves {d:.4f} dB (> {C.ISOLATION_TOL_DB} dB)")
    if inad.valid:
        probs.append("inadequate servo isolation was NOT detected by the excitation checks")
    for rail, pt in feed.items():
        if not pt.valid:
            probs.append(f"feedthrough control ({rail}) did not produce a valid measurement: {pt.reason}")
        elif nominal.valid:
            drop = nominal.rej[rail].low_db - pt.rej[rail].low_db
            if drop < C.CONTROL_MIN_DROP_DB:
                probs.append(f"feedthrough control ({rail}) lowered {LABEL[rail]} by only {drop:.2f} dB "
                             f"(< {C.CONTROL_MIN_DROP_DB} dB)")
    return Studies(nominal, floor, resid if not err else float("nan"), iso, inad, feed, runs, probs)


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def build_record(*, record, stamp, pdk, ngspice, kver, reports, walls, points: dict[Key, PsrrPoint], per_mode,
                 agree_v, studies: Studies, gdir, gdev, local_vs_grid, plots, dut_sha) -> str:
    L: list[str] = []
    add = L.append
    add(f"# PSRR record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; commit `{record.rsplit('-', 1)[-1]}`; issue #39")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include `{dut_sha[:16]}`), "
        f"unchanged; snapshot `netlist-snapshots/{record}.spice`")
    for ln in C.pdk_lines(pdk, reports):
        add(ln)
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`, numpy `{np.__version__}`")
    add("- **Execution**: three `klt sim` corner requests (one per excitation), 45 points each:")
    for ln in C.execution_lines(reports, walls):
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
    add("**Measured, no pass or fail verdict: the row's bound is not ratified** (DR-3 residual (e4)). "
        "`spec/target-spec.md` and the decision records are untouched; a proposed bound citing this record is a "
        "separate decision-record issue for a human to ratify.")
    add("")
    add("**Definitions and references.** Input-referred PSRR+/- = 20 log10 |Ad / Asupply|, Asupply = vout / v(rail) "
        "with one rail driven (1 V AC) at a time, the other rail and both inputs AC-quiet. Inputs (`Vcm`, the servo), "
        "the load CL and `Vdd` are referenced to simulator ground; for PSRR- the DUT's `vss` port is a driven node "
        "(`Vss vss 0 dc 0 ac 1`), so only the DUT's ground rail moves. The output supply feedthrough "
        "-20 log10 |Asupply| is a different quantity and is reported separately below. The ideal 10 uA bias source "
        "has zero small-signal current: **bias-generator supply rejection is excluded** (an on-chip reference would "
        "add its own). Matched devices: the figures are systematic, not mismatch-limited.")
    add("")
    for rail in RAILS:
        vals = {k: p.rej[rail] for k, p in points.items()}
        add(f"## {LABEL[rail]} worst case over the 45 points")
        add("")
        C.worst_table(add, vals, LABEL[rail])
        add("")
        add(f"- Plateau verified at {sum(r.plateau_ok for r in vals.values())}/{len(vals)} points; lower bounds "
            f"(numerical floor): {sum(r.lower_bound for r in vals.values())}/{len(vals)}.")
        for ln in C.cancellation_notes(vals, LABEL[rail], f"A{rail}"):
            add(ln)
        fts = [(p.feedthrough_db[rail], k) for k, p in points.items()]
        add(f"- Output supply feedthrough -20 log10 |A{rail}| at the plateau (NOT PSRR): {min(fts)[0]:.2f} dB "
            f"({fmt_key(min(fts)[1])}) .. {max(fts)[0]:.2f} dB ({fmt_key(max(fts)[1])}).")
        add("")
    add(f"- f_u over the grid: {min(p.fu_hz for p in points.values()) / 1e6:.3f} .. "
        f"{max(p.fu_hz for p in points.values()) / 1e6:.3f} MHz; Ad plateau "
        f"{min(p.ad_dc_db for p in points.values()):.2f} .. {max(p.ad_dc_db for p in points.values()):.2f} dB.")
    add("")
    add("## Method")
    add("")
    add(f"- `.ac dec {C.AC_PPD:g} {C.AC_FSTART:g} {C.AC_FSTOP:g}` ({C.N_FREQ} points) per point and excitation "
        "(`dm`, `vdd`, `vss`), the CMRR bench's DC servo (unity-buffer DC point, open loop at every swept frequency, "
        "vout unloaded).")
    add("- Ad = v(vout) / (v(vinp) - v(vinn)) of the `dm` run (actual phasors); Asupply = v(vout) / v(rail) of the "
        "supply run (actual rail phasor).")
    add(f"- **DC** = mean PSRR over {C.AC_FSTART:g}-{C.AC_FSTART * C.PLATEAU_DECADE_HI:g} Hz, valid only if the "
        f"PSRR and |Asupply| are flat there to {C.PLATEAU_TOL_DB} dB (else reported unavailable). Spot values and the "
        "value at f_u: linear interpolation in (log10 f, dB); f_u = first descending 0 dB crossing of |Ad| (the gain "
        "driver's extraction).")
    add(f"- **Numerical floor**: |Asupply| below {C.FLOOR_MARGIN:g} x the demonstrated superposition residual "
        f"(= {studies.floor:.3g} V/V) is clamped and the figure reported as a lower bound (`>=`).")
    add(f"- **Blocking validation**: 45/45 points per excitation, identical frequency axes, finite data; `dm`: vd = 1 V, "
        f"vc = 0, rails quiet (all to {C.EXC_TOL:g} V); supply runs: driven rail = 1 V, other rail quiet "
        f"(to {C.EXC_TOL:g} V), both inputs quiet to {INPUT_QUIET_V:g} V and the input-leak term |Ad vd| below "
        f"{INPUT_LEAK_FRAC:g} of the output; operating point; Ad vs the gain bench; local vs grid; studies/controls.")
    add("")
    add("## Excitation and cross-checks")
    add("")
    add(f"- Max excitation error {max(p.exc_err for p in points.values()):.3g} V; max input phasor in the supply runs "
        f"{max(p.input_quiet for p in points.values()):.3g} V; max input-leak fraction "
        f"{max(p.leak_frac for p in points.values()):.3g}.")
    if gdev:
        add(f"- **Ad vs the gain bench** (`sim/gain-gbw-pm/corners/{gdir.name}`), all {len(gdev)} points, 0.1 Hz to "
            f"f_u: max deviation {max(gdev.values()):.2e} dB (tolerance {C.TOL_GAIN_BENCH_DB} dB). The gain bench "
            "drives vinp alone, so its response is Ad + Acm/2; below f_u the Acm/2 term is negligible (the CMRR "
            "record compares the exact Ad + Acm/2 over the whole sweep).")
    else:
        add("- **Ad vs the gain bench**: committed gain-bench data not available; NOT CHECKED.")
    if local_vs_grid is not None:
        add(f"- **Local nominal unit vs the grid's nominal point**: PSRR+/- differ by at most {local_vs_grid:.4f} dB "
            f"(tolerance {C.TOL_LOCAL_VS_GRID_DB} dB).")
    add("")
    C.op_section(add, per_mode, agree_v)
    for rail in RAILS:
        add(f"## {LABEL[rail]} at all 45 points (dB, input-referred)")
        add("")
        C.figure_table(add, {k: p.rej[rail] for k, p in points.items()}, LABEL[rail])
        add("")
    add("## Ad, Avdd, Avss at the plateau (dB)")
    add("")
    add("| point | Ad | Avdd | Avss | PSRR+ DC | PSRR- DC |")
    add("|---|---|---|---|---|---|")
    for k in sorted(points, key=lambda k: (CORNERS.index(k[0]), k[2], k[1])):
        p = points[k]
        add(f"| {fmt_key(k)} | {p.ad_dc_db:.2f} | {-p.feedthrough_db['vdd']:.2f} | {-p.feedthrough_db['vss']:.2f} | "
            f"{C.fmt_db(p.rej['vdd'].dc_db, p.rej['vdd'].lower_bound)} | "
            f"{C.fmt_db(p.rej['vss'].dc_db, p.rej['vss'].lower_bound)} |")
    add("")
    add("## Studies and negative controls (local single units, typical / 27 C / 3.30 V)")
    add("")
    s = studies
    nom = s.nominal
    add(f"- **Numerical floor (superposition)**: |vout(both rails) - vout(vdd only) - vout(vss only)| max "
        f"{s.floor_resid:.3g} V; floor = max({C.FLOOR_MARGIN:g} x that, {C.FLOOR_MIN:g}) = {s.floor:.3g} V/V. Grid "
        f"|Asupply| minimum: {min(float(np.min(np.abs(a))) for p in points.values() for a in p.asup.values()):.3g} V/V.")
    add("")
    add("| study / control | condition | outcome | PSRR+ 0.1 Hz | PSRR+ at f_u | PSRR- 0.1 Hz | PSRR- at f_u | criterion |")
    add("|---|---|---|---|---|---|---|---|")

    def row(name, cond, pt, crit):
        if pt is None:
            add(f"| {name} | {cond} | not run | | | | | {crit} |")
        elif pt.valid:
            a, b = pt.rej["vdd"], pt.rej["vss"]
            add(f"| {name} | {cond} | valid | {a.low_db:.3f} | {a.at_fu_db:.3f} | {b.low_db:.3f} | {b.at_fu_db:.3f} | {crit} |")
        else:
            add(f"| {name} | {cond} | **rejected**: {pt.reason[:160]} | | | | | {crit} |")

    row("isolation (nominal)", "tau = 1e18 s", nom, "reference")
    for label, pt in s.iso:
        row("isolation", label, pt, f"moves <= {C.ISOLATION_TOL_DB} dB")
    row("inadequate isolation", "tau = 1e-3 s", s.inadequate, "must be rejected")
    for rail, pt in s.feedthrough.items():
        row(f"**negative control**: {rail} feedthrough", f"fixture `Rft {rail} vout {FEEDTHROUGH_R:g}`", pt,
            f"{LABEL[rail]} drops >= {C.CONTROL_MIN_DROP_DB:g} dB")
    add("")
    if nom and nom.valid:
        for rail, pt in s.feedthrough.items():
            if pt.valid:
                add(f"- {rail} feedthrough fixture: {LABEL[rail]} at 0.1 Hz {pt.rej[rail].low_db:.2f} dB vs "
                    f"{nom.rej[rail].low_db:.2f} dB (drop {nom.rej[rail].low_db - pt.rej[rail].low_db:.2f} dB). The "
                    "fixture is a resistor appended to the bench for the control only (all three excitations of the "
                    "control carry it); the production DUT and the baseline bench are unchanged. Diagnostic only.")
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
    add("python3 sim/psrr/run_psrr.py            # full grid + a new record")
    add("python3 sim/psrr/run_psrr.py --smoke    # nominal point, local, no record")
    add("```")
    add("")
    add(f"- Per point and excitation: `corners/{record}/<point>.<dm|vdd|vss>.{{dat,log,cir}}` (actual vout, vinp, "
        "vinn, vdd, vss phasors; log with the printed operating point; klt deck); derived curves `<point>.psrr.dat` "
        "(freq, |Ad|, |Avdd|, |Avss|, PSRR+, PSRR- in dB); sanitised klt reports; studies/controls under `controls/`.")
    add("- Testbench `sim/psrr/testbench/tb_psrr.spice`; driver `sim/psrr/run_psrr.py`; tests `sim/psrr/test_psrr.py`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #39)")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="psrr-smoke-") as scratch:
        pt, runs = psrr_triple(pdk, Path(scratch), "smoke", "nominal", C.FLOOR_MIN)
    for r in runs:
        if r.op is not None and r.op.problems:
            print("SMOKE TEST FAILED: " + "; ".join(r.op.problems))
            return 1
    if not pt.valid:
        print(f"SMOKE TEST FAILED: {pt.reason}")
        return 1
    for rail in RAILS:
        c = pt.rej[rail]
        print(f"  {LABEL[rail]}: DC {C.fmt_db(c.dc_db)} dB, " + ", ".join(f"{f:g} Hz {c.spot_db[f]:.1f}" for f in SPOT_HZ)
              + f", at f_u {c.at_fu_db:.1f} dB")
    print("smoke test OK (all three excitations ran and validated; the full run records the evidence)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    C.add_common_args(ap)
    args = ap.parse_args(argv)
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    err = C.require_plotting()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    want = C.expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = G.claim_record_paths(HERE, record)
    ngspice, kver = ngspice_version(), G.klt_version()
    gdir = C.latest_gain_dir()
    print(f"record {record}: 3 excitations x {len(want)} points, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    with C.work_dir(args, "psrr-") as scratch:
        work = Path(scratch)
        try:
            reports, reqs, walls = C.run_grid_modes(pdk, work, args, MODES, testbench=TESTBENCH, guard=guard_testbench)
        except C.KltError as exc:
            print(f"ERROR: a grid request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        per_mode, problems = {}, []
        for mode, rep in reports.items():
            per_mode[mode], p = C.analyse_mode_report(rep, want, mode, NEED)
            problems += p
        agree_v, p = C.op_agreement(per_mode, want)
        problems += p

        studies = run_psrr_studies(pdk, work)
        problems += studies.problems
        points: dict[Key, PsrrPoint] = {}
        gdev: dict[Key, float] = {}
        for k in want:
            if any(k not in per_mode[m] for m in MODES):
                continue
            pt = extract_psrr(per_mode["dm"][k]["vec"], {r: per_mode[r][k]["vec"] for r in RAILS}, studies.floor)
            if not pt.valid:
                problems.append(f"{fmt_key(k)}: invalid: {pt.reason}")
                continue
            points[k] = pt
            d = C.gain_bench_dev_db(gdir, k, pt.freq, pt.ad, fmax=pt.fu_hz)
            if gdir is not None:
                if d is None:
                    problems.append(f"{fmt_key(k)}: gain-bench data missing or on another frequency grid")
                else:
                    gdev[k] = d
                    if d > C.TOL_GAIN_BENCH_DB:
                        problems.append(f"{fmt_key(k)}: |Ad| differs from the gain bench by {d:.3f} dB")
        local_vs_grid = None
        if NOMINAL in points and studies.nominal and studies.nominal.valid:
            ds = []
            for rail in RAILS:
                a, b = points[NOMINAL].rej[rail], studies.nominal.rej[rail]
                ds += [abs(a.low_db - b.low_db), abs(a.at_fu_db - b.at_fu_db), *(abs(a.spot_db[f] - b.spot_db[f]) for f in SPOT_HZ)]
            local_vs_grid = max(ds)
            if local_vs_grid > C.TOL_LOCAL_VS_GRID_DB:
                problems.append(f"local nominal unit and the grid's nominal point differ by {local_vs_grid:.3f} dB")
        if problems or len(points) != len(want):
            print("ERROR: the PSRR evidence did not validate; NO RECORD WRITTEN:", file=sys.stderr)
            for p in problems[:60]:
                print(f"  - {p}", file=sys.stderr)
            return 2

        corners_dir = paths["corners"]
        corners_dir.mkdir(parents=True)
        for mode in MODES:
            for k, a in per_mode[mode].items():
                C.save_mode_point(corners_dir, k, mode, a, NEED)
            (corners_dir / f"klt-report.{mode}.json").write_text(json.dumps(G.sanitise_report(reports[mode]), indent=1))
        for k, p in points.items():
            np.savetxt(corners_dir / f"{point_stem(k)}.psrr.dat",
                       np.column_stack([p.freq, db(p.ad), db(p.asup["vdd"]), db(p.asup["vss"]),
                                        p.rej["vdd"].curve_db, p.rej["vss"].curve_db]), fmt="%.8e",
                       header="freq_hz Ad_dB Avdd_dB Avss_dB PSRRplus_dB PSRRminus_dB (Asupply clamped at the floor)")
        C.save_local_runs(corners_dir / "controls", studies.runs)

        dut_sha = hashlib.sha256(C.load_dut_text().encode()).hexdigest()
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        deck0 = Path(per_mode["dm"][NOMINAL]["deck"]).read_text() if per_mode["dm"][NOMINAL].get("deck") else ""
        C.write_snapshot(paths["snapshot"], record, "issue #39", TESTBENCH, reqs, deck0)
        ctl = HERE / "netlist-snapshots" / f"{record}-controls.spice"
        ctl.write_text("\n".join([
            f"* control snapshot for record {record} (issue #39) -- NOT the baseline bench",
            "* The production DUT is unchanged in every PSRR control. Stimulus-fixture controls:",
            *(f"*   control-feedthrough-{r}: appended line `Rft {r} vout {FEEDTHROUGH_R:g}` (all three excitations)"
              for r in RAILS),
            f"*   inadequate isolation: csv={C.ISOLATION_INADEQUATE_CSV:g}; isolation study: csv in {C.ISOLATION_CSV}",
            "* ---- the baseline testbench these fixtures are appended to ----",
            *("* | " + ln for ln in TESTBENCH.read_text().splitlines()),
            "",
        ]))

        nomp = points[NOMINAL]
        plots = C.build_plots_rejection(
            {k: p.freq for k, p in points.items()},
            {LABEL[r]: {k: p.rej[r].curve_db for k, p in points.items()} for r in RAILS},
            nomp.ad, {"|Avdd|": db(nomp.asup["vdd"]), "|Avss|": db(nomp.asup["vss"]),
                      "PSRR+": nomp.rej["vdd"].curve_db, "PSRR-": nomp.rej["vss"].curve_db},
            paths["plots"], "dB")
        md = build_record(record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, reports=reports,
                          walls=walls, points=points, per_mode=per_mode, agree_v=agree_v, studies=studies,
                          gdir=gdir, gdev=gdev, local_vs_grid=local_vs_grid, plots=plots, dut_sha=dut_sha)
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    for rail in RAILS:
        for which, lbl in C.FIGURES:
            v, k, _ = C.worst_case({kk: p.rej[rail] for kk, p in points.items()}, which)
            print(f"  worst {LABEL[rail]} {lbl}: {C.fmt_db(v)} dB at {fmt_key(k) if k else 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
