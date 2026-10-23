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

Opt-in RZ x CC passive-corner study (issue #155): `--passive-corners
--passive-scope full` runs the 45 points x 3 RZ x 3 CC = 405 cells, each with
the three excitations (1,215 responses), as bounded fleet corner-matrix
requests (one per excitation and MOS corner, or per temperature as well when a
measured partition approaches 1800 s), pairs them by complete cell identity
and writes a standalone study record (no verdict, not report-integrated). The
default behaviour above is unchanged without the flag.

Usage:
    python3 sim/psrr/run_psrr.py                  # full grid + record
    python3 sim/psrr/run_psrr.py --smoke          # nominal point, local, no record
    python3 sim/psrr/run_psrr.py --backend local  # force a backend
    python3 sim/psrr/run_psrr.py --passive-corners --passive-scope full  # 405-cell study record

Exit status: 0 when the evidence is complete and validated; 2 when a grid
could not run or failed validation (no record is written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))

import harness  # noqa: E402
from harness import (  # noqa: E402
    KltError,
    Pdk,
    allocate_record_id,
    batch_block,
    claim_run_paths,
    finalize_run_evidence,
    find_pdk,
    klt_version,
    load_dut_text,
    ngspice_version,
    load_sibling,
    remote_of,
    sanitise_report,
)
# Shared RZ x CC passive-corner helpers (issues #70, #97, #150), used unchanged.
from passive_corners import (  # noqa: E402
    MIM_FACTOR,
    PASSIVE_LEVELS,
    RES_FACTOR,
    SCOPE_CELLS,
    apply_passive_matrix,
    deck_section_problems,
    key_set_problems,
    passive_combos,
    passive_expected_keys,
    passive_name,
    scope_description,
    scope_points,
    split_passive_key,
)

# The CMRR driver owns the servo bench guards, excitation checks, rejection
# summaries, grid-request runner and evidence writers; reuse them unchanged
# (the klt wrapper itself is in `harness`).
C = load_sibling("cmrr_driver", "sim/cmrr/run_cmrr.py")
G = C.G

# One source for everything the fingerprint covers (issue #89).
mc = load_sibling("psrr_measurement_config", "sim/psrr/measurement_config.py")

TESTBENCH = HERE / "testbench" / "tb_psrr.spice"

CORNERS, TEMPS_C, SUPPLIES_V, NOMINAL = C.CORNERS, C.TEMPS_C, C.SUPPLIES_V, C.NOMINAL
Key = C.Key
fmt_key, point_stem = C.fmt_key, C.point_stem
SPOT_HZ = C.SPOT_HZ
db = C.db

QUIET = mc.QUIET
MODES = mc.MODES
RAILS = {"vdd": "v(vdd)", "vss": "v(vss)"}
NEED = ("v(vout)", "v(vinp)", "v(vinn)", "v(vdd)", "v(vss)")
LABEL = {"vdd": "PSRR+", "vss": "PSRR-"}

#: Supply runs: both input pins must stay AC-quiet to this (V per V of rail).
INPUT_QUIET_V = 1e-9
#: ... and the differential leak's contribution |Ad vd| must be this small a
#: fraction of the measured output.
INPUT_LEAK_FRAC = 1e-6
#: Feedthrough fixture resistor (value lives in measurement_config.py).
FEEDTHROUGH_R = mc.FEEDTHROUGH_R


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
    for ln in mc.fingerprint_lines({"bench": TESTBENCH.read_text()}):
        add(ln)
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
    studies_section(add, studies, points)
    L.extend(mc.inputs_section({"bench": TESTBENCH.read_text()}))
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


def studies_section(add, studies: Studies, points: dict) -> None:
    """The local studies / negative-controls block (shared by the grid record
    and the passive-corner study record)."""
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


# --------------------------------------------------------------------------
# Opt-in RZ x CC passive-corner study (issue #155)
# --------------------------------------------------------------------------
#
# `--passive-corners --passive-scope full`: the 45 MOS/T/VDD points x 3 RZ x
# 3 CC = 405 cells, each with the dm, vdd and vss excitations (1,215 AC
# responses). The passive levels are the PDK's own `res_*` / `mimcap_*`
# sections, loaded per cell through klt's process axis exactly as the gain and
# slew/swing/power studies do (`sim/passive_corners.py`, unchanged); the DUT
# and the bench are the committed ones. The cells go to the fleet as bounded
# corner-matrix requests: one per (excitation, MOS corner) = 81 cells, or per
# (excitation, MOS corner, temperature) = 27 cells when a measured partition's
# execution approaches SPLIT_REFERENCE_S. A standalone study record is written
# beside the grid records (title STUDY_TITLE); it issues no verdict.

STUDY_TITLE = "# PSRR passive-corner study (RZ x CC)"
#: Only the full envelope is offered for PSRR (issue #155).
STUDY_SCOPES = ("full",)
#: The frequencies the study reports by name (DR-6 proposes its bound at 10 kHz).
STUDY_FREQS = (1e3, 1e4)
PARTITION_SPLITS = ("auto", "mos", "mos-temp")
#: The fleet job limit observed in #140.
FLEET_JOB_LIMIT_S = 3600.0
#: Issue #155: when the measured execution of the first (preflight) partition
#: reaches SPLIT_AT_FRACTION x SPLIT_REFERENCE_S, the remaining partitions are
#: split by temperature (27-cell requests).
SPLIT_REFERENCE_S = 1800.0
SPLIT_AT_FRACTION = 0.5
#: Typical-passive cells vs the matching default PSRR grid record.
XCHECK_DEFAULT_TOL_DB = C.TOL_LOCAL_VS_GRID_DB
#: Figures compared in the default-record cross-check: 0.1 Hz and the spots.
XCHECK_FIGURES = ("low", *SPOT_HZ)


class StudyError(RuntimeError):
    """The study cannot publish; `problems` lists every blocking reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:5]))
        self.problems = list(problems)


@dataclass(frozen=True)
class Partition:
    """One bounded fleet request's population: one MOS corner, some temperatures."""

    name: str
    mos: str
    temps: tuple
    points: tuple  # (mos, T, VDD) study points; x 9 RZ/CC combos = the cells

    @property
    def keys(self) -> list:
        return passive_expected_keys(list(self.points))

    @property
    def slug(self) -> str:
        return self.name.replace("@", "_at_")


def partition_plan(points, split: str) -> list[Partition]:
    """`mos`: one partition per MOS corner; `mos-temp`: per MOS corner and temperature."""
    if split not in ("mos", "mos-temp"):
        raise ValueError(f"unknown partition split {split!r}")
    out = []
    for m in dict.fromkeys(p[0] for p in points):
        mp = [p for p in points if p[0] == m]
        if split == "mos":
            out.append(Partition(m, m, tuple(sorted({p[1] for p in mp})), tuple(mp)))
            continue
        for t in sorted({p[1] for p in mp}):
            out.append(Partition(f"{m}@{t:g}C", m, (t,), tuple(p for p in mp if p[1] == t)))
    return out


def build_schedule(points, split: str, preflight: Partition | None = None) -> dict[str, list[Partition]]:
    """Partitions per excitation. A completed preflight partition (dm) is kept
    and the `split` partitions of its MOS corner are dropped for dm."""
    plan: dict[str, list[Partition]] = {m: [] for m in MODES}
    if preflight is not None:
        plan["dm"].append(preflight)
    for m in MODES:
        for p in partition_plan(points, split):
            if preflight is not None and m == "dm" and p.mos == preflight.mos:
                continue
            plan[m].append(p)
    return plan


def plan_problems(plan: dict[str, list[Partition]], want: list) -> list[str]:
    """Exact coverage per excitation: the partitions' union is `want`, no overlap."""
    probs = []
    extra = sorted(set(plan) - set(MODES))
    if extra:
        probs.append(f"unexpected excitation(s) in the plan: {extra}")
    for mode in MODES:
        parts = plan.get(mode) or []
        if not parts:
            probs.append(f"[{mode}] no partitions planned")
            continue
        names = [p.name for p in parts]
        if len(set(names)) != len(names):
            probs.append(f"[{mode}] a partition is planned twice")
        probs += [f"[{mode}] partition union: {p}" for p in key_set_problems([k for p in parts for k in p.keys], want)]
    return probs


def request_cells(req: dict) -> list:
    """The cells a corner-matrix request expands to (process x T x VDD minus `exclude`)."""
    ex = {(e["process"], float(e["temperature_c"]), float(e["supply_v"]["vdd"])) for e in req.get("exclude", [])}
    c = req["corners"]
    return [(p["name"], float(t), float(v)) for p in c["process"] for v in c["supply_v"]["vdd"]
            for t in c["temperature_c"] if (p["name"], float(t), float(v)) not in ex]


def partition_request(tb: Path, pdk: Pdk, part: Partition) -> dict:
    """ONE corner-matrix request for a partition: the PSRR request (sweep + OP
    print) with the MOS x RZ x CC process axis of `sim/passive_corners.py`.
    Verified to expand to exactly the partition's cells before submission."""
    vdds = sorted({p[2] for p in part.points})
    req = apply_passive_matrix(C.ac_request(tb, pdk, [], list(part.temps), vdds), list(part.points))
    probs = key_set_problems(request_cells(req), part.keys)
    if probs:
        raise StudyError([f"request for partition {part.name}: {p}" for p in probs])
    return req


def unit_entry(mode: str, part: Partition, attempt: int, submit: int, wall: float, rep: dict | None,
               outcome: str, preflight: bool) -> dict:
    """One line of the execution log (retained in the record and partitions.json)."""
    r = remote_of(rep) if rep else {}
    return {
        "excitation": mode, "partition": part.name, "cells": len(part.keys), "attempt": attempt, "submit": submit,
        "preflight": preflight, "provider": r.get("provider") or ("local" if rep else None),
        "job_id": r.get("job_id"), "state": r.get("state"), "instance_type": r.get("instance_type"),
        "spot": r.get("spot"), "elapsed_s": r.get("elapsed_seconds"), "wall_s": round(wall, 1),
        "runner_klt": r.get("runner_klt_version"), "outcome": outcome,
    }


def exec_seconds(entry: dict) -> float:
    """Fleet execution time of a unit (`environment.remote.elapsed_seconds`),
    or the client wall time when the backend reports none."""
    try:
        return float(entry["elapsed_s"])
    except (KeyError, TypeError, ValueError):
        return float(entry["wall_s"])


def analyse_unit(rep: dict, part: Partition, mode: str) -> tuple[dict, list[str]]:
    """Per-cell results of one partition report + blocking problems: exactly
    the partition's cells, each with rawfile/log/OP (shared CMRR analysis), and
    a generated deck that loads the MOS/RZ/CC sections its name encodes."""
    per, probs = C.analyse_mode_report(rep, part.keys, mode, NEED)
    for k, a in per.items():
        a["key"] = k
        a["partition"] = part.name
        deck = a.get("deck")
        if not deck or not Path(deck).is_file():
            probs.append(f"[{mode}] no generated deck retained for {fmt_key(k)}; cannot verify its sections")
        else:
            probs += [f"[{mode}] {p}" for p in deck_section_problems(k, Path(deck).read_text())]
    if rep.get("errored"):
        probs.append(f"[{mode}/{part.name}] klt reports {rep['errored']} errored corner(s)")
    return per, probs


def run_unit(runner, req: dict, work: Path, args, mode: str, part: Partition, log: list[dict], *,
             preflight: bool = False) -> tuple[dict, dict]:
    """Submit one (excitation, partition) request and accept it only if it is
    complete and valid. A capacity refusal is re-submitted (`--batch-submit-
    retries`, nothing ran); a failed or invalid result is re-run as a whole
    partition (`--partition-retries`), never merged cell by cell. Every try is
    appended to `log`. Raises StudyError when no attempt is acceptable."""
    last: list[str] = []
    for attempt in range(1, args.partition_retries + 2):
        submit = 0
        rep = None
        while True:
            submit += 1
            out = work / mode / f"out-{part.slug}-a{attempt}-s{submit}"
            t0 = time.time()
            try:
                rep = runner(req, out, args.backend, work / mode)
                break
            except KltError as exc:
                msg = str(exc)
                cap = any(t in msg for t in harness._TRANSIENT_SUBMIT)
                log.append(unit_entry(mode, part, attempt, submit, time.time() - t0, None,
                                      ("submit refused (fleet capacity)" if cap else "submit failed") + f": {msg[-240:]}",
                                      preflight))
                if cap and submit <= args.batch_submit_retries:
                    print(f"    capacity refusal; re-submitting in {args.batch_retry_wait_s:g} s", flush=True)
                    time.sleep(args.batch_retry_wait_s)
                    continue
                last = [f"[{mode}/{part.name}] klt sim failed: {msg[-400:]}"]
                break
        if rep is None:
            continue
        wall = time.time() - t0
        per, probs = analyse_unit(rep, part, mode)
        if not probs:
            log.append(unit_entry(mode, part, attempt, submit, wall, rep, "accepted", preflight))
            return per, rep
        log.append(unit_entry(mode, part, attempt, submit, wall, rep,
                              f"rejected ({len(probs)} problem(s)): {probs[0][:200]}", preflight))
        last = probs
    raise StudyError(last)


def merge_units(units: list[tuple[str, Partition, dict]], want: list) -> tuple[dict, list[str]]:
    """Per-excitation union of the accepted partitions. Blocking: a cell from
    a partition that does not contain it, a cell returned twice, and any
    missing / unexpected cell against the full population."""
    per_mode: dict[str, dict] = {m: {} for m in MODES}
    owner: dict[str, dict] = {m: {} for m in MODES}
    probs: list[str] = []
    for mode, part, per in units:
        if mode not in per_mode:
            probs.append(f"unexpected excitation {mode!r} from partition {part.name}")
            continue
        mine = set(part.keys)
        for k, a in per.items():
            if k not in mine:
                probs.append(f"[{mode}] {fmt_key(k)} returned by partition {part.name}, which does not contain it")
                continue
            if k in per_mode[mode]:
                probs.append(f"[{mode}] {fmt_key(k)} returned by partitions {owner[mode][k]} and {part.name}")
                continue
            per_mode[mode][k] = a
            owner[mode][k] = part.name
    for m in MODES:
        probs += [f"[{m}] {p}" for p in key_set_problems(list(per_mode[m]), want)]
    return per_mode, probs


def cell_label(k: Key) -> str:
    mos, r, c = split_passive_key(k)
    return f"{mos} / {k[1]:g} C / {k[2]:.2f} V / RZ {r} / CC {c}"


def cell_sort(k: Key) -> tuple:
    mos, r, c = split_passive_key(k)
    return (CORNERS.index(mos), k[1], k[2], PASSIVE_LEVELS.index(r), PASSIVE_LEVELS.index(c))


def typical_of(k: Key) -> Key:
    """The typical-RZ / typical-CC cell at the same MOS / T / VDD."""
    mos, _, _ = split_passive_key(k)
    return (passive_name(mos, "typical", "typical"), k[1], k[2])


def pair_cells(per_mode: dict, want: list, floor: float) -> tuple[dict, list[str]]:
    """dm + vdd + vss of the SAME complete (MOS, T, VDD, RZ, CC) identity ->
    PsrrPoint. Missing excitations, a response recorded for another cell and
    any extraction failure are blocking (`extract_psrr` checks the axes,
    finiteness, the excitation of every run and the input leak)."""
    points: dict = {}
    probs: list[str] = []
    for k in want:
        got = {m: per_mode.get(m, {}).get(k) for m in MODES}
        miss = [m for m, a in got.items() if a is None]
        if miss:
            probs.append(f"{cell_label(k)}: no {'/'.join(miss)} response; cannot pair")
            continue
        wrong = {m: a.get("key") for m, a in got.items() if a.get("key") != k}
        if wrong:
            probs.append(f"{cell_label(k)}: response(s) recorded for another cell "
                         f"({', '.join(f'{m}: {v}' for m, v in wrong.items())}); cross-cell pairing refused")
            continue
        pt = extract_psrr(got["dm"]["vec"], {r: got[r]["vec"] for r in RAILS}, floor)
        if not pt.valid:
            probs.append(f"{cell_label(k)}: invalid: {pt.reason}")
            continue
        points[k] = pt
    return points, probs


def figure_of(pt: PsrrPoint, rail: str, which) -> float:
    r = pt.rej[rail]
    return r.low_db if which == "low" else r.figure(which if isinstance(which, str) else f"{which:g}")


def study_extremes(points: dict, rail: str, which) -> dict:
    """Worst (lowest) and best value of one figure over the cells, with the cells."""
    vals = [(figure_of(points[k], rail, which), k) for k in sorted(points, key=cell_sort)]
    vals = [t for t in vals if math.isfinite(t[0])]
    if not vals:
        return {"worst": (float("nan"), None), "best": (float("nan"), None), "missing": len(points)}
    # Ties resolve to the first cell in table order (min/max keep the first extreme).
    worst = min(vals, key=lambda t: t[0])
    best = max(vals, key=lambda t: t[0])
    return {"worst": worst, "best": best, "missing": len(points) - len(vals)}


def deltas_vs_typical(points: dict, rail: str, which) -> dict:
    """{cell: figure - figure of the typical-passive cell at the same PVT} for
    every non-typical cell (both values finite)."""
    out = {}
    for k, p in points.items():
        tk = typical_of(k)
        if tk == k or tk not in points:
            continue
        a, b = figure_of(p, rail, which), figure_of(points[tk], rail, which)
        if math.isfinite(a) and math.isfinite(b):
            out[k] = a - b
    return out


def default_reference(dut_sha: str, root: Path = HERE) -> tuple[str, Path] | None:
    """Newest ordinary PSRR grid record measured on this DUT with the current
    default configuration (DUT hash and default fingerprint both match), and
    its corner directory; None if there is none."""
    fp = mc.fingerprint({"bench": TESTBENCH.read_text()})
    for rec in sorted((root / "records").glob("*.md"), reverse=True):
        text = rec.read_text()
        if not text.startswith("# PSRR record"):
            continue
        if f"`{dut_sha[:16]}`" not in text or f"sha256 `{fp}`" not in text:
            continue
        cdir = root / "corners" / rec.stem
        if cdir.is_dir():
            return rec.stem, cdir
    return None


#: The committed `<point>.psrr.dat` axis went through `np.savetxt` (~7 significant
#: digits, up to ~4e-9 relative); log spacing is ~1.2 %, so 1e-6 still pins the axis.
AXIS_TEXT_RTOL = 1e-6


def default_crosscheck(points: dict, cdir: Path) -> tuple[float, dict, list[str]]:
    """Typical-passive cells vs the default grid record's `<point>.psrr.dat`
    at 0.1 Hz and every spot frequency, both rails. Returns (max |dev| dB,
    {cell: dev}, problems)."""
    devs: dict = {}
    probs: list[str] = []
    for k, p in points.items():
        if typical_of(k) != k:
            continue
        mos, _, _ = split_passive_key(k)
        f = cdir / f"{point_stem((mos, k[1], k[2]))}.psrr.dat"
        if not f.is_file():
            probs.append(f"default-record cross-check: {f.name} missing")
            continue
        d = np.loadtxt(f)
        if d.shape[0] != len(p.freq) or np.any(np.abs(d[:, 0] / p.freq - 1) > AXIS_TEXT_RTOL):
            probs.append(f"default-record cross-check: {f.name} is on another frequency axis")
            continue
        dev = 0.0
        for rail, col in (("vdd", 4), ("vss", 5)):
            for which in XCHECK_FIGURES:
                ref = float(d[0, col]) if which == "low" else C.interp_db(d[:, 0], d[:, col], which)
                dev = max(dev, abs(figure_of(p, rail, which) - ref))
        devs[k] = dev
        if dev > XCHECK_DEFAULT_TOL_DB:
            probs.append(f"{cell_label(k)}: differs from the default grid record by {dev:.4f} dB "
                         f"(> {XCHECK_DEFAULT_TOL_DB} dB)")
    return (max(devs.values()) if devs else float("nan")), devs, probs


def gain_passive_reference(want: list, dut_sha: str, base: Path | None = None) -> Path | None:
    """Newest gain-bench passive-corner corner directory holding every study
    cell, whose record states this DUT hash (the #150 full study)."""
    base = base or REPO_ROOT / "sim" / "gain-gbw-pm"
    if not (base / "corners").is_dir():
        return None
    for d in sorted((p for p in (base / "corners").iterdir() if p.is_dir()), reverse=True):
        rec = base / "records" / f"{d.name}.md"
        if not rec.is_file() or dut_sha not in rec.read_text():
            continue
        if all((d / f"{point_stem(k)}.dat").is_file() for k in want):
            return d
    return None


def study_problem_report(problems: list[str]) -> None:
    print(f"ERROR: the PSRR passive-corner study did not validate ({len(problems)} problem(s)); "
          "NO RECORD WRITTEN:", file=sys.stderr)
    for p in problems[:80]:
        print(f"  - {p}", file=sys.stderr)
    if len(problems) > 80:
        print(f"  ... and {len(problems) - 80} more", file=sys.stderr)


def _fmt_cell_val(pt: PsrrPoint, rail: str, which) -> str:
    return C.fmt_db(figure_of(pt, rail, which), pt.rej[rail].lower_bound)


def _lbl(which) -> str:
    if which == "low":
        return "0.1 Hz"
    if which == "dc":
        return "DC plateau"
    if which == "fu":
        return "at f_u"
    f = float(which)
    return f"{f / 1e3:g} kHz" if f < 1e6 else f"{f / 1e6:g} MHz"


def build_study_record(*, record, stamp, pdk, ngspice, kver, scope, want: list, points: dict, per_mode, agree_v,
                       studies: Studies, log: list[dict], plan: dict, sched: dict, reports: dict, dut_sha,
                       default_ref, default_dev, gdir, gdev, local_vs_grid) -> str:
    L: list[str] = []
    add = L.append
    n = len(want)
    retained = mc.study_retained(scope)
    texts = {"bench": TESTBENCH.read_text()}
    add(f"{STUDY_TITLE} -- record {record}")
    add("")
    add(f"- **Date (UTC)**: {stamp:%Y-%m-%d %H:%M:%S}; commit `{record.rsplit('-', 1)[-1]}`")
    add("- **Issue**: #155 -- standalone RZ x CC x PVT PSRR evidence. **Not integrated into the characterization "
        "report** (no `selection.json` key, no report parser; that is a separate proposal).")
    add(f"- **Scope**: `{scope}` -- {scope_description(scope)}; each cell with the `dm`, `vdd` and `vss` "
        f"excitations = {n * len(MODES)} AC responses, paired by complete (MOS, T, VDD, RZ, CC) identity.")
    add("- **Status**: measured, **no pass or fail verdict**. The PSRR bound is proposed (DR-6, \">= 50 dB each at "
        "10 kHz\"), NOT ratified; `spec/target-spec.md` and the decision records are untouched.")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (normalised sha256 `{dut_sha}`), unchanged; snapshot "
        f"`netlist-snapshots/{record}.spice`. The passive levels are PDK sections loaded per cell, not netlist edits.")
    for ln in mc.fingerprint_lines(texts, retained):
        add(ln)
    for ln in C.pdk_lines(pdk, reports):
        add(ln)
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`, numpy `{np.__version__}`")
    acc = [e for e in log if e["outcome"] == "accepted"]
    add(f"- **Execution**: {len(acc)} accepted `klt sim` corner-matrix requests ({len(log)} submissions in total); "
        f"partition split `{sched['used_split']}` (requested `{sched['requested_split']}`); see 'Fleet execution'.")
    add("")
    add("## Claim")
    add("")
    add("**Measured, no verdict.** How the RZ (`ppolyf_u_1k`) and CC (`cap_mim_2f0_m4m5_noshield`) PDK corners "
        "move input-referred PSRR+ and PSRR- of the committed schematic, at every MOS corner / temperature / supply of "
        "the 45-point grid. Definitions, references and the bench are those of the PSRR grid record (Ad from the "
        "differential run, Asupply = vout / v(rail) with one rail driven at a time).")
    add("")
    add("**Limitations (unchanged by this study).** Schematic level. Matched devices: systematic figures, not "
        "mismatch-limited (see `sim/psrr-mc/`). The ideal 10 uA bias source carries no small-signal current, so "
        "**bias-generator supply sensitivity is excluded**. The passive corners are the PDK's global `res_*` / "
        "`mimcap_*` sections (RZ and CC varied independently; no RZ/CC mismatch, no layout parasitics). The bound "
        "is proposed only.")
    add("")
    add("## Binding cells at 1 kHz and 10 kHz (both rails)")
    add("")
    typ = {k: p for k, p in points.items() if typical_of(k) == k}
    add(f"| figure | worst over {n} cells (dB) | binding cell (MOS / T / VDD / RZ / CC) | best (dB) | best cell | "
        f"worst over the {len(typ)} typical-passive cells (dB) | passive spread moves the worst case by (dB) |")
    add("|---|---|---|---|---|---|---|")
    for rail in RAILS:
        for f in STUDY_FREQS:
            e, et = study_extremes(points, rail, f), study_extremes(typ, rail, f)
            (wv, wk), (bv, bk), (tv, _) = e["worst"], e["best"], et["worst"]
            add(f"| {LABEL[rail]} {_lbl(f)} | **{_fmt_cell_val(points[wk], rail, f)}** | {cell_label(wk)} | "
                f"{_fmt_cell_val(points[bk], rail, f)} | {cell_label(bk)} | {C.fmt_db(tv)} | {wv - tv:+.2f} |")
    add("")
    add("## Difference vs typical passives at matching PVT (1 kHz, 10 kHz)")
    add("")
    add(f"Delta = cell value - value of the typical-RZ / typical-CC cell at the same MOS / T / VDD ({n - len(typ)} "
        "non-typical cells). Negative = the passive corner lowers PSRR.")
    add("")
    add("| figure | most negative delta (dB) | at cell | most positive delta (dB) | at cell | max abs delta (dB) |")
    add("|---|---|---|---|---|---|")
    for rail in RAILS:
        for f in STUDY_FREQS:
            d = deltas_vs_typical(points, rail, f)
            lo = min(d.items(), key=lambda t: (t[1], cell_sort(t[0])))
            hi = max(d.items(), key=lambda t: t[1])
            add(f"| {LABEL[rail]} {_lbl(f)} | {lo[1]:+.3f} | {cell_label(lo[0])} | {hi[1]:+.3f} | "
                f"{cell_label(hi[0])} | {max(abs(v) for v in d.values()):.3f} |")
    add("")
    add(f"### Per RZ x CC combination (over the {len(typ)} MOS / T / VDD points)")
    add("")
    add("| RZ | CC | PSRR+ 1 kHz worst | PSRR+ 10 kHz worst | PSRR- 1 kHz worst | PSRR- 10 kHz worst | "
        "dPSRR+ 10 kHz range | dPSRR- 10 kHz range |")
    add("|---|---|---|---|---|---|---|---|")
    for r, c in passive_combos():
        sub = {k: p for k, p in points.items() if split_passive_key(k)[1:] == (r, c)}
        cells = []
        for rail in RAILS:
            for f in STUDY_FREQS:
                v, k = study_extremes(sub, rail, f)["worst"]
                cells.append(f"{C.fmt_db(v)} ({fmt_key((split_passive_key(k)[0], k[1], k[2]))})")
        rng = []
        for rail in RAILS:
            d = [v for kk, v in deltas_vs_typical(points, rail, 1e4).items() if kk in sub]
            rng.append(f"{min(d):+.3f} .. {max(d):+.3f}" if d else "reference")
        add(f"| {r} ({RES_FACTOR[r]:g}x) | {c} ({MIM_FACTOR[c]:g}x) | " + " | ".join(cells) + " | "
            + " | ".join(rng) + " |")
    add("")
    for rail in RAILS:
        add(f"## {LABEL[rail]} worst case over the {n} cells (every figure)")
        add("")
        add(f"| {LABEL[rail]} figure | worst (dB) | binding cell | best (dB) | best cell | cells without the figure |")
        add("|---|---|---|---|---|---|")
        for which, lbl in C.FIGURES:
            e = study_extremes(points, rail, which)
            (wv, wk), (bv, bk) = e["worst"], e["best"]
            add(f"| {lbl} | **{C.fmt_db(wv)}** | {cell_label(wk) if wk else 'n/a'} | {C.fmt_db(bv)} | "
                f"{cell_label(bk) if bk else 'n/a'} | {e['missing']} |")
        vals = {k: p.rej[rail] for k, p in points.items()}
        add("")
        add(f"- Plateau verified at {sum(r.plateau_ok for r in vals.values())}/{n} cells; lower bounds "
            f"(numerical floor): {sum(r.lower_bound for r in vals.values())}/{n}.")
        for ln in C.cancellation_notes(vals, LABEL[rail], f"A{rail}"):
            add(ln)
        add("")
    add(f"- f_u over the cells: {min(p.fu_hz for p in points.values()) / 1e6:.3f} .. "
        f"{max(p.fu_hz for p in points.values()) / 1e6:.3f} MHz; Ad plateau "
        f"{min(p.ad_dc_db for p in points.values()):.2f} .. {max(p.ad_dc_db for p in points.values()):.2f} dB.")
    add("")
    add("## Passive-section policy (swept)")
    add("")
    add("RZ (`ppolyf_u_1k`) uses `res_typical` / `res_ff` (best, 1000-200 ohm = 0.8x) / `res_ss` (worst, 1000+200 "
        "ohm = 1.2x); CC (`cap_mim_2f0_m4m5_noshield`) uses `mimcap_typical` / `mimcap_ff` (best, mim_corner_2p0fF = "
        "0.9) / `mimcap_ss` (worst, 1.1). All nine combinations at every MOS / T / VDD point. Each cell's klt process "
        "entry is `<mos>__r-<rz>__c-<cc>` with sections `[<mos>, res_<..>, mimcap_<..>]` (`sim/passive_corners.py`); "
        "every generated deck was checked to load exactly those sections. \"best\"/\"worst\" are the PDK ff/ss "
        "labels, not a claim about PSRR; the tables decide.")
    add("")
    add("## Fleet execution")
    add("")
    pf = sched.get("preflight")
    if pf:
        add(f"- **Preflight**: `{pf['excitation']}` partition `{pf['partition']}` ({pf['cells']} cells) measured "
            f"{pf['measured_s']:.0f} s of execution; split threshold {SPLIT_AT_FRACTION:g} x {SPLIT_REFERENCE_S:g} s = "
            f"{SPLIT_AT_FRACTION * SPLIT_REFERENCE_S:g} s (fleet job limit {FLEET_JOB_LIMIT_S:g} s, #140) -> split "
            f"`{sched['used_split']}`.")
    else:
        add(f"- **Partition split** `{sched['used_split']}` was requested explicitly (no preflight decision).")
    for mode in MODES:
        parts = plan[mode]
        add(f"- **`{mode}` coverage**: {len(parts)} partitions ({', '.join(f'{p.name}: {len(p.keys)}' for p in parts)}"
            f") -> union {len({k for p in parts for k in p.keys})} cells, "
            f"{sum(len(p.keys) for p in parts) - len({k for p in parts for k in p.keys})} overlapping, "
            f"{len(set(want) - {k for p in parts for k in p.keys})} missing (exactly the {n}-cell population).")
    ex = [exec_seconds(e) for e in acc]
    add(f"- Accepted requests: execution {min(ex):.0f} .. {max(ex):.0f} s (sum {sum(ex):.0f} s); client wall "
        f"{min(e['wall_s'] for e in acc):.0f} .. {max(e['wall_s'] for e in acc):.0f} s.")
    add("")
    add("| # | excitation | partition | cells | attempt / submit | outcome | backend | job | instance | state | "
        "execution (s) | wall (s) |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, e in enumerate(log, 1):
        add(f"| {i} | `{e['excitation']}` | {e['partition']}{' (preflight)' if e['preflight'] else ''} | {e['cells']} | "
            f"{e['attempt']} / {e['submit']} | {e['outcome']} | {e['provider'] or '-'} | `{e['job_id'] or '-'}` | "
            f"{('Spot ' if e['spot'] else '') + (e['instance_type'] or '-')} | {e['state'] or '-'} | "
            f"{e['elapsed_s'] if e['elapsed_s'] is not None else '-'} | {e['wall_s']:.0f} |")
    add("")
    runners = sorted({e["runner_klt"] for e in acc if e["runner_klt"]})
    add(f"- **Fleet provenance limitations**: runner klt {', '.join(runners) or 'n/a'} vs client `{kver}`; the fleet "
        "runner does not report its PDK revision, and klt's `provenance.pdk` is resolved on the submitting client. "
        "The fleet library is tied to the pinned revision indirectly: the local pinned nominal triple reproduces the "
        "grid's nominal typical-passive cell, the typical-passive cells reproduce the default PSRR grid record, and Ad "
        "reproduces the gain bench's passive study (Cross-checks).")
    add("")
    add("## Cross-checks")
    add("")
    if default_ref:
        add(f"- **Typical-passive cells vs the default PSRR grid record** `{default_ref}` (same DUT hash, same default "
            f"measurement fingerprint): {len(default_dev)}/{len(typ)} typical-passive cells, PSRR+ and PSRR- at 0.1 Hz and "
            f"{', '.join(_lbl(f) for f in SPOT_HZ)}: max deviation {max(default_dev.values()):.2e} dB "
            f"(tolerance {XCHECK_DEFAULT_TOL_DB} dB). Their sections `[<mos>, res_typical, mimcap_typical]` are the "
            "default grid's.")
    else:
        add("- **Typical-passive cells vs the default PSRR grid record**: no record with this DUT hash and the current "
            "default fingerprint; NOT CHECKED.")
    if gdev:
        add(f"- **Ad vs the gain bench's passive study** (`sim/gain-gbw-pm/corners/{gdir.name}`), all {len(gdev)} "
            f"cells, 0.1 Hz to f_u: max deviation {max(gdev.values()):.2e} dB (tolerance {C.TOL_GAIN_BENCH_DB} dB). "
            "Same cells and sections; the gain bench measures Ad + Acm/2 (negligible below f_u).")
    else:
        add("- **Ad vs the gain bench's passive study**: no gain passive-corner data for every cell on this DUT; "
            "NOT CHECKED.")
    if local_vs_grid is not None:
        add(f"- **Local nominal triple vs the grid's typical / 27 C / 3.30 V typical-passive cell**: PSRR+/- differ "
            f"by at most {local_vs_grid:.4f} dB (tolerance {C.TOL_LOCAL_VS_GRID_DB} dB).")
    add(f"- Max excitation error {max(p.exc_err for p in points.values()):.3g} V; max input phasor in the supply "
        f"runs {max(p.input_quiet for p in points.values()):.3g} V; max input-leak fraction "
        f"{max(p.leak_frac for p in points.values()):.3g}.")
    add("")
    C.op_section(add, per_mode, agree_v)
    add("## Method and blocking validation")
    add("")
    add(f"- `.ac dec {C.AC_PPD:g} {C.AC_FSTART:g} {C.AC_FSTOP:g}` ({C.N_FREQ} points) with the operating-point print, "
        "the PSRR bench's DC servo, per cell and excitation; extraction (`extract_psrr`), plateau / spot / f_u "
        "interpolation and the near-cancellation notes are the PSRR grid record's.")
    add(f"- Numerical floor {studies.floor:.3g} V/V from the local superposition study: |Asupply| below it is clamped "
        "and the figure reported as a lower bound (`>=`).")
    add(f"- **Blocking** (any failure writes no record): every request expands to exactly its partition's cells "
        f"before submission; per excitation the accepted partitions cover exactly the {n} cells with no overlap; "
        "every cell has a rawfile, a log with the operating point, and a deck loading exactly the sections its name "
        "encodes; |vout - VCM| <= 100 mV and all three excitations at the same DC vout (1 uV); identical, finite "
        f"frequency axes; `dm`: vd = 1 V, vc = 0, rails quiet; supply runs: driven rail = 1 V, other rail quiet, "
        f"inputs quiet to {INPUT_QUIET_V:g} V, input-leak term below {INPUT_LEAK_FRAC:g}; the local studies and "
        "controls; and the cross-checks above.")
    add("")
    studies_section(add, studies, points)
    add(f"## All {n} cells (dB, input-referred)")
    add("")
    add("| cell (MOS / T / VDD / RZ / CC) | PSRR+ 1 kHz | PSRR+ 10 kHz | PSRR- 1 kHz | PSRR- 10 kHz | "
        "dPSRR+ 10 kHz vs typ. | dPSRR- 10 kHz vs typ. | PSRR+ DC | PSRR- DC | f_u (MHz) | note |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    dp = {rail: deltas_vs_typical(points, rail, 1e4) for rail in RAILS}
    for k in sorted(points, key=cell_sort):
        p = points[k]
        note = []
        for rail in RAILS:
            r = p.rej[rail]
            if not r.plateau_ok:
                note.append(f"{LABEL[rail]} no plateau (0.1 Hz {r.low_db:.2f})")
            if r.lower_bound:
                note.append(f"{LABEL[rail]} lower bound")
        add(f"| {cell_label(k)} | {_fmt_cell_val(p, 'vdd', 1e3)} | {_fmt_cell_val(p, 'vdd', 1e4)} | "
            f"{_fmt_cell_val(p, 'vss', 1e3)} | {_fmt_cell_val(p, 'vss', 1e4)} | "
            + " | ".join(f"{dp[rail][k]:+.3f}" if k in dp[rail] else "typ." for rail in RAILS)
            + f" | {C.fmt_db(p.rej['vdd'].dc_db, p.rej['vdd'].lower_bound)} | "
            f"{C.fmt_db(p.rej['vss'].dc_db, p.rej['vss'].lower_bound)} | {p.fu_hz / 1e6:.3f} | {'; '.join(note)} |")
    add("")
    L.extend(mc.inputs_section(texts, retained))
    add("## Reproduce / evidence")
    add("")
    add("```")
    add("python3 sim/psrr/run_psrr.py --passive-corners --passive-scope full \\")
    add("        --batch-runner-version-check warn --batch-submit-retries 10")
    add("```")
    add("")
    add(f"- Per cell and excitation: `corners/{record}/<cell>.<dm|vdd|vss>.{{dat,log,cir}}` (actual vout, vinp, vinn, "
        "vdd, vss phasors; log with the printed operating point; klt deck); derived curves `<cell>.psrr.dat` (freq, "
        "|Ad|, |Avdd|, |Avss|, PSRR+, PSRR- in dB); the accepted sanitised klt reports "
        "`klt-report.<excitation>.<partition>.json`; `partitions.json` (plan, membership, every submission with "
        "job id, state and timings); local studies/controls under `controls/`.")
    add(f"- Snapshots: `netlist-snapshots/{record}.spice` (DUT, testbench, every partition request), "
        f"`netlist-snapshots/{record}-controls.spice` (fixture controls).")
    add("- Runner `sim/psrr/run_psrr.py`; shared passive helpers `sim/passive_corners.py`; tests "
        "`sim/psrr/test_psrr.py`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #155)")
    add("")
    return "\n".join(L)


def partitions_json(plan: dict, sched: dict, log: list[dict], want: list) -> dict:
    return {
        "issue": 155,
        "population": {"cells": len(want), "excitations": list(MODES), "responses": len(want) * len(MODES)},
        "split": {"requested": sched["requested_split"], "used": sched["used_split"], "preflight": sched["preflight"],
                  "threshold_s": SPLIT_AT_FRACTION * SPLIT_REFERENCE_S, "fleet_job_limit_s": FLEET_JOB_LIMIT_S},
        "plan": {m: [{"partition": p.name, "mos": p.mos, "temperatures_c": list(p.temps),
                      "cells": [point_stem(k) for k in p.keys]} for p in parts] for m, parts in plan.items()},
        "coverage": {m: {"cells": len({k for p in parts for k in p.keys}),
                         "overlap": sum(len(p.keys) for p in parts) - len({k for p in parts for k in p.keys}),
                         "missing": len(set(want) - {k for p in parts for k in p.keys})} for m, parts in plan.items()},
        "submissions": log,
    }


def run_passive_study(pdk: Pdk, args, *, runner=None, studies_fn=None, root: Path = HERE,
                      gain_base: Path | None = None, points_pvt: list | None = None) -> int:
    """`--passive-corners --passive-scope full`. Exit 0 with a new record, or 2
    with NO record (any submit failure or blocking validation problem).

    `runner` (klt), `studies_fn`, `root` (evidence directory), `gain_base` and
    `points_pvt` (a reduced population) are injection points for the offline
    tests only; the CLI always uses the full scope population."""
    runner = runner or harness.run_klt
    studies_fn = studies_fn or run_psrr_studies
    scope = args.passive_scope
    if points_pvt is None:
        points_pvt = scope_points(scope)
        assert len(passive_expected_keys(points_pvt)) == SCOPE_CELLS[scope], scope
    want = passive_expected_keys(points_pvt)
    dut_sha = hashlib.sha256(load_dut_text().encode()).hexdigest()
    # Prove the planned partitions cover the population exactly BEFORE any submission.
    for split in ("mos", "mos-temp"):
        probs = plan_problems(build_schedule(points_pvt, split), want)
        if probs:
            study_problem_report(probs)
            return 2
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_run_paths(root, record, plots=False)
    ngspice, kver = ngspice_version(), klt_version()
    print(f"record {record}: PSRR passive-corner study ({scope}): {len(want)} cells x {len(MODES)} excitations = "
          f"{len(want) * len(MODES)} responses; split {args.partition_split}; PDK={pdk.path}; klt {kver}", flush=True)

    with C.work_dir(args, "psrr-passive-") as scratch:
        work = Path(scratch)
        # Local nominal studies/controls first (numerical floor, servo isolation,
        # feedthrough controls): a failing control costs no fleet time.
        print("  local studies and controls (nominal, typical passives)...", flush=True)
        studies = studies_fn(pdk, work)
        if studies.problems:
            study_problem_report(studies.problems)
            return 2
        log: list[dict] = []
        units: list[tuple[str, Partition, dict]] = []
        reports: dict[str, dict] = {}
        reqs: dict[str, dict] = {}
        try:
            tbs = {m: C.materialise(work / m, pdk, C.with_servo(mp), testbench=TESTBENCH, guard=guard_testbench)
                   for m, mp in MODES.items()}

            def do(mode: str, part: Partition, pre: bool = False) -> dict:
                req = partition_request(tbs[mode], pdk, part)
                req["batch"] = batch_block(args)
                label = f"{mode}.{part.slug}"
                print(f"  [{len(reports) + 1}] `{mode}` partition {part.name} ({len(part.keys)} cells)"
                      f"{' -- preflight' if pre else ''}...", flush=True)
                per, rep = run_unit(runner, req, work, args, mode, part, log, preflight=pre)
                e = log[-1]
                print(f"      accepted: job {e['job_id'] or 'local'}, execution {exec_seconds(e):.0f} s, "
                      f"wall {e['wall_s']:.0f} s", flush=True)
                units.append((mode, part, per))
                reports[label] = rep
                reqs[label] = req
                return e

            preflight = None
            if args.partition_split == "auto":
                first = partition_plan(points_pvt, "mos")[0]
                measured = exec_seconds(do("dm", first, True))
                used = "mos-temp" if measured >= SPLIT_AT_FRACTION * SPLIT_REFERENCE_S else "mos"
                preflight = {"excitation": "dm", "partition": first.name, "cells": len(first.keys),
                             "measured_s": measured, "split": used}
                print(f"  preflight: {measured:.0f} s for {len(first.keys)} cells -> split `{used}`", flush=True)
                plan = build_schedule(points_pvt, used, first)
            else:
                used = args.partition_split
                plan = build_schedule(points_pvt, used)
            probs = plan_problems(plan, want)
            if probs:
                raise StudyError(probs)
            done = {(m, p.name) for m, p, _ in units}
            for mode, parts in plan.items():
                for part in parts:
                    if (mode, part.name) not in done:
                        do(mode, part)
        except KltError as exc:
            print(f"ERROR: a partition request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            _print_log(log)
            return 2
        except StudyError as exc:
            study_problem_report(exc.problems)
            _print_log(log)
            return 2
        sched = {"requested_split": args.partition_split, "used_split": used, "preflight": preflight}

        per_mode, problems = merge_units(units, want)
        agree_v, p = C.op_agreement(per_mode, want)
        problems += p
        points, p = pair_cells(per_mode, want, studies.floor)
        problems += p
        default_ref, default_dev = None, {}
        ref = default_reference(dut_sha, root)
        if ref is not None:
            _, default_dev, p = default_crosscheck(points, ref[1])
            problems += p
            default_ref = ref[0]
        gdir = gain_passive_reference(want, dut_sha, gain_base)
        gdev: dict = {}
        if gdir is not None:
            for k, pt in points.items():
                d = C.gain_bench_dev_db(gdir, k, pt.freq, pt.ad, fmax=pt.fu_hz)
                if d is None:
                    problems.append(f"{cell_label(k)}: gain-bench passive data on another frequency grid")
                    continue
                gdev[k] = d
                if d > C.TOL_GAIN_BENCH_DB:
                    problems.append(f"{cell_label(k)}: |Ad| differs from the gain bench by {d:.3f} dB")
        local_vs_grid = None
        nom = (passive_name(NOMINAL[0], "typical", "typical"), NOMINAL[1], NOMINAL[2])
        if nom in points and studies.nominal and studies.nominal.valid:
            ds = []
            for rail in RAILS:
                a, b = points[nom].rej[rail], studies.nominal.rej[rail]
                ds += [abs(a.low_db - b.low_db), abs(a.at_fu_db - b.at_fu_db),
                       *(abs(a.spot_db[f] - b.spot_db[f]) for f in SPOT_HZ)]
            local_vs_grid = max(ds)
            if local_vs_grid > C.TOL_LOCAL_VS_GRID_DB:
                problems.append(f"local nominal triple and the grid's nominal typical-passive cell differ by "
                                f"{local_vs_grid:.3f} dB")
        if problems or len(points) != len(want):
            study_problem_report(problems or [f"only {len(points)}/{len(want)} cells paired"])
            _print_log(log)
            return 2

        cdir = paths["corners"]
        cdir.mkdir(parents=True)
        for mode in MODES:
            for k, a in per_mode[mode].items():
                C.save_mode_point(cdir, k, mode, a, NEED)
        for label, rep in reports.items():
            (cdir / f"klt-report.{label}.json").write_text(json.dumps(sanitise_report(rep), indent=1))
        (cdir / "partitions.json").write_text(json.dumps(partitions_json(plan, sched, log, want), indent=1))
        for k, pt in points.items():
            np.savetxt(cdir / f"{point_stem(k)}.psrr.dat",
                       np.column_stack([pt.freq, db(pt.ad), db(pt.asup["vdd"]), db(pt.asup["vss"]),
                                        pt.rej["vdd"].curve_db, pt.rej["vss"].curve_db]), fmt="%.8e",
                       header="freq_hz Ad_dB Avdd_dB Avss_dB PSRRplus_dB PSRRminus_dB (Asupply clamped at the floor)")
        C.save_local_runs(cdir / "controls", studies.runs)
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        nom_a = per_mode["dm"].get(nom) or per_mode["dm"][sorted(per_mode["dm"], key=cell_sort)[0]]
        deck0 = Path(nom_a["deck"]).read_text() if nom_a.get("deck") else ""
        C.write_snapshot(paths["snapshot"], record, "issue #155, PSRR RZ x CC passive-corner study", TESTBENCH,
                         reqs, deck0)
        (root / "netlist-snapshots" / f"{record}-controls.spice").write_text("\n".join([
            f"* control snapshot for record {record} (issue #155) -- NOT the baseline bench",
            "* Local nominal studies/controls (typical passives), as in the PSRR grid record:",
            *(f"*   control-feedthrough-{r}: appended line `Rft {r} vout {FEEDTHROUGH_R:g}` (all three excitations)"
              for r in RAILS),
            f"*   inadequate isolation: csv={C.ISOLATION_INADEQUATE_CSV:g}; isolation study: csv in {C.ISOLATION_CSV}",
            "* ---- the baseline testbench these fixtures are appended to ----",
            *("* | " + ln for ln in TESTBENCH.read_text().splitlines()),
            "",
        ]))
        md = build_study_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, scope=scope, want=want, points=points,
            per_mode=per_mode, agree_v=agree_v, studies=studies, log=log, plan=plan, sched=sched, reports=reports,
            dut_sha=dut_sha, default_ref=default_ref, default_dev=default_dev, gdir=gdir, gdev=gdev,
            local_vs_grid=local_vs_grid)
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)
        finalize_run_evidence(paths)

    print(f"wrote {paths['record']}")
    for rail in RAILS:
        for f in STUDY_FREQS:
            v, k = study_extremes(points, rail, f)["worst"]
            print(f"  worst {LABEL[rail]} {_lbl(f)}: {C.fmt_db(v)} dB at {cell_label(k)}")
    return 0


def _print_log(log: list[dict]) -> None:
    if not log:
        return
    print("  submissions so far:", file=sys.stderr)
    for e in log:
        print(f"    {e['excitation']}/{e['partition']} a{e['attempt']}s{e['submit']}: {e['outcome'][:160]} "
              f"(job {e['job_id']}, state {e['state']}, wall {e['wall_s']} s)", file=sys.stderr)


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
    ap.add_argument("--passive-corners", action="store_true",
                    help="opt-in (issue #155): standalone RZ x CC passive-corner PSRR study instead of the grid "
                    "record; requires --passive-scope full")
    ap.add_argument("--passive-scope", choices=STUDY_SCOPES, default=None,
                    help="with --passive-corners: 'full' = 5 MOS x 3 T x 3 VDD x 3 RZ x 3 CC = 405 cells, each with "
                    "the dm/vdd/vss excitations (1,215 responses)")
    ap.add_argument("--partition-split", choices=PARTITION_SPLITS, default="auto",
                    help="study fleet partitions: 'mos' = one 81-cell request per excitation and MOS corner; "
                    "'mos-temp' = 27-cell requests per excitation, MOS corner and temperature; 'auto' (default) runs "
                    f"one 'mos' partition first and switches to 'mos-temp' when its execution reaches "
                    f"{SPLIT_AT_FRACTION * SPLIT_REFERENCE_S:g} s")
    ap.add_argument("--partition-retries", type=int, default=1,
                    help="study: re-run a whole partition up to N times when its result is incomplete or invalid "
                    "(every attempt is recorded; default 1)")
    args = ap.parse_args(argv)
    study_only = args.passive_scope is not None or args.partition_split != "auto" or args.partition_retries != 1
    if args.passive_corners and args.passive_scope is None:
        ap.error("--passive-corners requires --passive-scope full")
    if study_only and not args.passive_corners:
        ap.error("--passive-scope / --partition-split / --partition-retries require --passive-corners")
    if args.passive_corners and args.smoke:
        ap.error("--smoke cannot be combined with --passive-corners")
    if args.partition_retries < 0:
        ap.error("--partition-retries must be >= 0")
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    if args.passive_corners:
        return run_passive_study(pdk, args)
    err = C.require_plotting()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    want = C.expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_run_paths(HERE, record)
    ngspice, kver = ngspice_version(), klt_version()
    gdir = G.latest_gain_dir()
    print(f"record {record}: 3 excitations x {len(want)} points, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    with C.work_dir(args, "psrr-") as scratch:
        work = Path(scratch)
        try:
            reports, reqs, walls = C.run_grid_modes(pdk, work, args, MODES, testbench=TESTBENCH, guard=guard_testbench)
        except KltError as exc:
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
            (corners_dir / f"klt-report.{mode}.json").write_text(json.dumps(sanitise_report(reports[mode]), indent=1))
        for k, p in points.items():
            np.savetxt(corners_dir / f"{point_stem(k)}.psrr.dat",
                       np.column_stack([p.freq, db(p.ad), db(p.asup["vdd"]), db(p.asup["vss"]),
                                        p.rej["vdd"].curve_db, p.rej["vss"].curve_db]), fmt="%.8e",
                       header="freq_hz Ad_dB Avdd_dB Avss_dB PSRRplus_dB PSRRminus_dB (Asupply clamped at the floor)")
        C.save_local_runs(corners_dir / "controls", studies.runs)

        dut_sha = hashlib.sha256(load_dut_text().encode()).hexdigest()
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
        finalize_run_evidence(paths)

    print(f"wrote {paths['record']}")
    for rail in RAILS:
        for which, lbl in C.FIGURES:
            v, k, _ = C.worst_case({kk: p.rej[rail] for kk, p in points.items()}, which)
            print(f"  worst {LABEL[rail]} {lbl}: {C.fmt_db(v)} dB at {fmt_key(k) if k else 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
