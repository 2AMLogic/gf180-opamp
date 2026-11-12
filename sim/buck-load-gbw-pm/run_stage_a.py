#!/usr/bin/env python3
"""Stage A probe of spec/decision-records/0008 (issue #295): this op-amp into
gf180-buck's real type-III compensation load, ONE corner, local.

    python3 sim/buck-load-gbw-pm/run_stage_a.py           # runs, mints a new record
    python3 sim/buck-load-gbw-pm/run_stage_a.py --smoke   # region check only, no record

Scope (DR-0008 section 4 "Stage A" and "Approval gate", nothing more):
typical MOS + res_typical + mimcap_typical, 27 C, VDD = 3.30 V, 10 uA bias, the
committed `opamp_two_stage` through `subckt_from_export()` (via the shared
harness), the exact consumer network. It is a short, sequential list of single
`ngspice -b` units on one corner (region check x3 VC levels, 1/beta, the
small-signal bench at three VC levels, the Lfb isolation study); it is NOT a
PVT grid, uses no fleet and no `klt sim` batch request. Stage B is out of scope.

It sets no GBW/PM row and relaxes nothing: it reports simulated numbers,
classifies bench faults versus findings exactly as DR-0008 section 4, and routes
the outcome mechanically per section 5. Evidence is append-only: a new record id
every run; the driver refuses to overwrite.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

from harness import (  # noqa: E402
    DUT_INCLUDE_NAME,
    Pdk,
    allocate_record_id,
    claim_record_paths,
    find_pdk,
    guard_dut_core,
    load_dut_text,
    logical_lines,
    ngspice_version,
    DEVICE_MODEL_RE,
)
from check_dc_op import DEVICES, PARAMS, VDS_MARGIN_V  # noqa: E402

TB_DIR = HERE / "testbench"
LOAD_INC = TB_DIR / "consumer_load.inc"
LOAD_INC_CM1P5 = TB_DIR / "consumer_load_cm1p5.inc"
TB_SWEEP_FOLLOWER = TB_DIR / "tb_sweep_follower.spice"
TB_SWEEP_OPEN = TB_DIR / "tb_sweep_open.spice"
TB_REGION = TB_DIR / "tb_region.spice"
TB_AC = TB_DIR / "tb_ac.spice"
TB_BETA = TB_DIR / "tb_beta.spice"

# --------------------------------------------------------------------------
# Conditions (DR-0008 section 4 Stage A)
# --------------------------------------------------------------------------

SECTIONS = ("typical", "res_typical", "mimcap_typical")
TEMP_C = 27.0
VDD = 3.30
AC_ARGS = ("dec", 20, 0.1, 1e9)  # same sweep as sim/gain-gbw-pm

#: Consumer steady-state VC for 1.8 V out (DR-0008 item C3, UNCONFIRMED => "assumed").
#: Derivation, recorded in every record: duty = 1.8 V / 5 V = 0.36 (Vin 5 V nominal,
#: gf180-buck design/compensator.md), ramp 1.18..1.80 V (gf180-buck design/README.md
#: osc_ramp row, simulated) => VC = 1.18 + 0.36 * 0.613 = 1.40 V.
VC_BIAS_ASSUMED = 1.40
VC_OFFSETS = (0.0, -0.15, +0.15)
LFB_NOMINAL = 1e9
LFB_ISOLATION = (1e8, 1e10)  # in addition to the nominal 1e9
LFB_INADEQUATE = 1e4  # the plateau check MUST reject this one

#: Isolation-study tolerances: the same as sim/gain-gbw-pm/README.md.
ISO_GAIN_DB, ISO_FREQ_REL = 0.10, 0.005
ISO_PHASE_DEG = 0.5

PLATEAU_TOL_DB = 0.10  # lowest decade flat (as sim/gain-gbw-pm)
POLARITY_TOL_DEG = 10.0
MIN_POINTS = 20
SPOT_FREQS_HZ = (1e3, 2e4, 1.5e5, 1e6, 2e6, 1e7, 1e8)

#: Run configuration. Defaults reproduce the 0.6 V record of 20261110-084324-e8a2a7b;
#: `configure()` (the --vcm flag) switches to the consumer's new common mode.
CFG = {
    "vcm": 0.6, "rbot": "5k", "load": LOAD_INC, "sweep": False,
    "load_note": ("`2AMLogic/gf180-buck` `design/netlists/compensator.spice`, read 2026-11-10 at "
                  "main `b671d9e38321873caa9574f553d05473844c8bfc`; equal to DR-0008 §2.1 (guarded by the runner)"),
}
CM1P5_LOAD_NOTE = ("`2AMLogic/gf180-buck` PR #132 (closes gf180-buck#131), merge commit "
                   "`003fe17f85ca35fc63c3f9835404b2aa8135951a`, `spec/decision-records/0008-ea-input-common-mode.md` §4: "
                   "VREF = FBN = 1.50 V, Rtop/Rbot = 10 kΩ / 50 kΩ (was 5 kΩ), every compensator value unchanged "
                   "(the consumer's `compensator.spice` at that sha still shows 5 kΩ; the DR text is the source). "
                   "Guarded by the runner; `testbench/consumer_load_cm1p5.inc`")
SWEEP_STEP_V, SWEEP_MAX_V = 0.05, 1.5
SWEEP_OFFSETS_V = (-1e-3, 0.0, 1e-3)
RAIL_MARGIN_V = 0.10


def vcm_s() -> str:
    return f"{CFG['vcm']:g} V"


def configure(vcm: float) -> None:
    """Select the common-mode case. 0.6 V = the original record's network;
    1.5 V = the consumer's DR 0008 (gf180-buck) network plus the input sweep."""
    if abs(vcm - 0.6) < 1e-9:
        CFG.update(vcm=0.6, rbot="5k", load=LOAD_INC, sweep=False)
    elif abs(vcm - 1.5) < 1e-9:
        CFG.update(vcm=1.5, rbot="50k", load=LOAD_INC_CM1P5, sweep=True, load_note=CM1P5_LOAD_NOTE)
    else:
        raise ValueError("--vcm must be 0.6 (original record) or 1.5 (consumer DR 0008 of gf180-buck)")


ERROR_RE = re.compile(
    r"(^|\W)(error|fatal|singular matrix|no convergence|doAnalyses: TRAN|aborted|"
    r"timestep too small|trouble with)",
    re.IGNORECASE,
)
VALUE_RE = re.compile(r"^\s*(\S+)\s*=\s*([-+0-9.eE]+)\s*$")

TB_PARAM_RE = re.compile(r"^\.param\s+lfb=\S+\s+vcb=\S+\s+vref=\S+\s+vbuck=\S+\s*$", re.M)

# C1-C4 (DR-0008 section 3). None answered by the consumer when this runs.
CONSUMER_ITEMS = (
    ("C1", "EA supply is the 3.3 V control rail with the +-10 % band",
     "assumed: 3.30 V only (Stage A is one corner); the band is Stage B's axis"),
    ("C2", "EA reference/feedback node at the stated common mode in every mode incl. start-up",
     "assumed: VREF = FBN = {vcm} as stated by the consumer ({src}); the start-up ramp is covered only by "
     "the DC input sweep (static, not a transient)"),
    ("C3", "PWM-comparator input capacitance on VC and the VC swing/bias needed",
     f"assumed: VC_bias = {VC_BIAS_ASSUMED:.2f} V (derivation in the record); comparator input "
     "approximated by the gate of the consumer's XCPM1 (consumer_load.inc)"),
    ("C4", "R13 PM >= 45 deg, GM >= 10 dB at 100-250 kHz as the grading criteria",
     "assumed: not used to grade anything here; no row is derived in Stage A"),
)


# --------------------------------------------------------------------------
# Source guards (the bench must stay tied to the committed design)
# --------------------------------------------------------------------------


def guard_tb(text: str, *, name: str, need_dut: bool = True) -> list[str]:
    errs: list[str] = []
    code = logical_lines(text)
    n_dut = 0
    for ln in code:
        first = ln.split(None, 1)[0].lower()
        if first.startswith("m"):
            errs.append(f"{name}: hand-declared MOSFET: {ln[:60]}")
        elif first == "xdut":
            n_dut += 1
            if "opamp_two_stage" not in ln.split():
                errs.append(f"{name}: Xdut does not instantiate opamp_two_stage")
        elif first.startswith("x"):
            if first not in ("xload", "xpwm"):
                errs.append(f"{name}: unexpected instance: {ln[:60]}")
        elif DEVICE_MODEL_RE.search(ln) and not ln.startswith("."):
            errs.append(f"{name}: PDK device declared in the bench: {ln[:60]}")
        if re.match(r"\.(lib|temp|control|end)\b", ln, re.I):
            errs.append(f"{name}: bench must be a circuit body; found {ln.split()[0]}")
    if need_dut and n_dut != 1:
        errs.append(f"{name}: expected exactly one Xdut, found {n_dut}")
    if not need_dut and n_dut != 0:
        errs.append(f"{name}: the passive bench must not contain the amplifier")
    targets = {
        m.group(1)
        for ln in code
        for m in [re.match(r"\.include\s+['\"]?([^'\"\s]+)", ln, re.I)]
        if m
    }
    if need_dut and not {"design.ngspice", DUT_INCLUDE_NAME} <= targets:
        errs.append(f"{name}: must .include design.ngspice and {DUT_INCLUDE_NAME}")
    return errs


def guard_load_inc(text: str, rbot: str = "5k") -> list[str]:
    """The consumer network must be exactly the DR-0008 section 2.1 values."""
    want = {
        "rtop": ("vbuck", "fbn", "10k"), "r3": ("vbuck", "x", "204"),
        "c3": ("x", "fbn", "780p"), "rbot": ("fbn", "0", rbot),
        "r2": ("fbn", "z", "4.55k"), "c1": ("z", "vc", "1.75n"),
        "c2": ("fbn", "vc", "35.7p"),
    }
    got = {}
    for ln in logical_lines(text):
        t = ln.split()
        if t[0].lower() in want:
            got[t[0].lower()] = tuple(t[1:4])
    errs = [f"{CFG['load'].name}: {k} is {got.get(k)}, DR-0008 section 2.1 says {v}"
            for k, v in want.items() if got.get(k) != v]
    return errs


# --------------------------------------------------------------------------
# Deck composition and execution
# --------------------------------------------------------------------------


def accessors() -> list[str]:
    return [f"@m.xdut.{d.inst}.m0[{p}]" for d in DEVICES for p in PARAMS]


def wrap_deck(body: str, pdk: Pdk, control: list[str]) -> str:
    lines = [f"* gf180-opamp Stage A (issue #295, DR-0008 section 4)"]
    lines += [f".lib '{pdk.model_lib}' {s}" for s in SECTIONS]
    lines += [f".temp {TEMP_C:g}", "", body, "", ".control", *control, ".endc", ".end", ""]
    return "\n".join(lines)


def stage(work: Path, pdk: Pdk) -> None:
    """Write the per-run work directory (PDK design file, DUT, consumer load)."""
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdk.design_include, work / "design.ngspice")
    dut = load_dut_text()
    errs = guard_dut_core(dut)
    if errs:
        raise RuntimeError("DUT guard failed:\n  " + "\n  ".join(errs))
    (work / DUT_INCLUDE_NAME).write_text(dut)
    inc = CFG["load"].read_text()
    errs = guard_load_inc(inc, CFG["rbot"])
    if errs:
        raise RuntimeError("; ".join(errs))
    (work / "consumer_load.inc").write_text(inc)


def tb_body(path: Path, work: Path, *, need_dut: bool = True, **params) -> str:
    text = path.read_text()
    errs = guard_tb(text, name=path.name, need_dut=need_dut)
    if errs:
        raise RuntimeError("bench guard failed:\n  " + "\n  ".join(errs))
    for key, val in params.items():
        text, n = re.subn(rf"(?<=[\s=]){key}=\S+", f"{key}={val:g}", text, count=1, flags=re.M) \
            if key != "x" else (text, 1)
        if n != 1:
            raise RuntimeError(f"{path.name}: .param {key} not found exactly once")
    for inc in ("design.ngspice", DUT_INCLUDE_NAME, "consumer_load.inc"):
        text = text.replace(f"'{inc}'", f"'{work / inc}'")
    return text


def run_ngspice(deck: str, work: Path, name: str) -> tuple[str, Path]:
    deckfile = work / f"{name}.cir"
    deckfile.write_text(deck)
    proc = subprocess.run(
        ["ngspice", "-b", str(deckfile)], capture_output=True, text=True, timeout=300,
        check=False, cwd=work,
    )
    log = proc.stdout + proc.stderr
    (work / f"{name}.log").write_text(log)
    return log, deckfile


def parse_values(log: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in log.splitlines():
        m = VALUE_RE.match(line)
        if m:
            try:
                out[m.group(1).lower()] = float(m.group(2))
            except ValueError:
                pass
    return out


def log_errors(log: str) -> list[str]:
    return [ln.strip() for ln in log.splitlines() if ERROR_RE.search(ln)]


# --------------------------------------------------------------------------
# Pure analysis (unit-tested without a simulator)
# --------------------------------------------------------------------------


@dataclass
class DevOp:
    name: str
    role: str
    id_a: float
    vgs: float
    vth: float
    vds: float
    vdsat: float
    gm: float
    on: bool
    sat: bool

    @property
    def region(self) -> str:
        return "sat" if (self.on and self.sat) else ("triode" if self.on else "off")


def device_ops(vals: dict[str, float]) -> dict[str, DevOp]:
    """Regions from an operating point, with check_dc_op's definition:
    on = |Vgs| > |Vth|; saturated = |Vds| >= |Vdsat| + VDS_MARGIN_V."""
    out: dict[str, DevOp] = {}
    for d in DEVICES:
        def p(n: str, inst: str = d.inst) -> float:
            return vals.get(f"@m.xdut.{inst}.m0[{n}]", float("nan"))
        vgs, vds, vth, vdsat = abs(p("vgs")), abs(p("vds")), abs(p("vth")), abs(p("vdsat"))
        on = bool(vgs > vth)
        sat = bool(vds >= vdsat + VDS_MARGIN_V)
        out[d.inst] = DevOp(d.inst.upper()[1:], d.role, p("id"), vgs, vth, vds, vdsat,
                            abs(p("gm")), on, sat)
    return out


#: The closure-independent devices of DR-0008 section 4 item 1.
REGION_DEVICES = ("xm1", "xm2", "xm5")


def region_verdict(ops: dict[str, DevOp]) -> tuple[bool, list[str]]:
    """(valid, reasons). Valid = XM1/XM2/XM5 all on and saturated with margin.
    A NaN (missing print) is invalid, never silently valid."""
    reasons: list[str] = []
    for k in REGION_DEVICES:
        o = ops[k]
        vals = (o.id_a, o.vgs, o.vth, o.vds, o.vdsat)
        if not all(math.isfinite(v) for v in vals):
            reasons.append(f"{o.name}: operating point missing")
        elif not o.on:
            reasons.append(f"{o.name} is off (|Vgs| {o.vgs:.3f} V <= |Vth| {o.vth:.3f} V)")
        elif not o.sat:
            reasons.append(
                f"{o.name} not saturated with margin (|Vds| {o.vds:.3f} V < "
                f"|Vdsat| {o.vdsat:.3f} V + {VDS_MARGIN_V:g} V)")
    return (not reasons), reasons


def plateau_check(freq: np.ndarray, h: np.ndarray) -> dict:
    """Lowest decade flat within PLATEAU_TOL_DB, phase within POLARITY_TOL_DEG of
    0 (vinp non-inverting). Same rule as sim/gain-gbw-pm (never 'the peak')."""
    freq = np.asarray(freq, float)
    h = np.asarray(h, complex)
    if freq.size < MIN_POINTS or h.shape != freq.shape:
        return dict(ok=False, reason="too few points or mismatched shapes")
    if not (np.all(np.isfinite(freq)) and np.all(np.isfinite(h))):
        return dict(ok=False, reason="non-finite data")
    if np.any(freq <= 0) or np.any(np.diff(freq) <= 0) or np.any(np.abs(h) <= 0):
        return dict(ok=False, reason="bad frequency axis or zero magnitude")
    db = 20 * np.log10(np.abs(h))
    ph = np.degrees(np.unwrap(np.angle(h)))
    band = freq <= freq[0] * 10.0
    if band.sum() < 3:
        return dict(ok=False, reason="fewer than 3 points in the plateau band")
    spread = float(db[band].max() - db[band].min())
    phase = float((np.mean(ph[band]) + 180) % 360 - 180)
    res = dict(spread_db=spread, phase_deg=phase, dc_db=float(np.mean(db[band])))
    if abs(phase) > POLARITY_TOL_DEG:
        return dict(ok=False, reason=f"plateau phase {phase:.1f} deg is not ~0 (polarity or lagging)", **res)
    if spread > PLATEAU_TOL_DB:
        return dict(ok=False, reason=f"no plateau: lowest decade varies {spread:.3f} dB "
                    f"(> {PLATEAU_TOL_DB} dB; feedback isolation inadequate)", **res)
    return dict(ok=True, reason="", **res)


def level_crossings(freq: np.ndarray, db: np.ndarray, level: float = 0.0) -> list[tuple[float, str]]:
    """All crossings of `level` dB, interpolated linearly in (log10 f, dB);
    direction 'down' or 'up'."""
    out: list[tuple[float, str]] = []
    above = db >= level
    for i in np.where(above[:-1] != above[1:])[0]:
        d0, d1 = db[i], db[i + 1]
        frac = (level - d0) / (d1 - d0)
        lf = math.log10(freq[i]) + frac * (math.log10(freq[i + 1]) - math.log10(freq[i]))
        out.append((10 ** lf, "down" if above[i] else "up"))
    return out


def _interp_log(freq: np.ndarray, y: np.ndarray, f: float) -> float:
    return float(np.interp(math.log10(f), np.log10(freq), y))


def small_signal(freq: np.ndarray, aol: np.ndarray, beta: np.ndarray) -> dict:
    """Aol(f) into the real load against 1/beta(f).

    T = Aol * beta (the loop gain of the compensated amplifier; vinn = beta *
    v(vc)). 'Open-loop crossing of 1/beta' = where |Aol| meets |1/beta| =
    where |T| = 1. Phase of T is unwrapped from the low-frequency end; loop
    margin = 180 + phase(T) at each downward |T| = 1 crossing. It is reported,
    not judged (DR-0008: no row is derived here)."""
    plat = plateau_check(freq, aol)
    T = aol * beta
    db_aol = 20 * np.log10(np.abs(aol))
    db_invb = -20 * np.log10(np.abs(beta))
    db_T = 20 * np.log10(np.abs(T))
    ph_T = np.degrees(np.unwrap(np.angle(T)))
    crossings = []
    for f, d in level_crossings(freq, db_T):
        ph = _interp_log(freq, ph_T, f)
        crossings.append(dict(f_hz=f, direction=d, phase_deg=ph, margin_deg=180.0 + ph))
    aol0 = level_crossings(freq, db_aol)
    spots = [dict(f_hz=f, aol_db=_interp_log(freq, db_aol, f), invb_db=_interp_log(freq, db_invb, f),
                  t_db=_interp_log(freq, db_T, f), aol_phase_deg=_interp_log(
                      freq, np.degrees(np.unwrap(np.angle(aol))), f))
             for f in SPOT_FREQS_HZ if freq[0] <= f <= freq[-1]]
    return dict(plateau=plat, crossings=crossings, aol_unity=aol0, spots=spots,
                db_aol=db_aol, db_invb=db_invb, db_T=db_T, ph_T=ph_T)


def isolation_verdict(runs: dict[float, dict | None]) -> list[str]:
    """Problems with the Lfb isolation study (empty = ok). `runs` maps Lfb to the
    `small_signal` result (None = the run failed). Lfb in LFB_ISOLATION and the
    nominal must agree (DC gain within ISO_GAIN_DB; first downward |T| = 1
    crossing within ISO_FREQ_REL; its loop phase within ISO_PHASE_DEG); the
    deliberately inadequate LFB_INADEQUATE must be REJECTED by the plateau check."""
    problems: list[str] = []
    ref = runs.get(LFB_NOMINAL)
    if ref is None or not ref["plateau"]["ok"]:
        return [f"nominal Lfb {LFB_NOMINAL:g} has no valid plateau"]
    ref_x = next((c for c in ref["crossings"] if c["direction"] == "down"), None)
    for lfb in (*LFB_ISOLATION,):
        r = runs.get(lfb)
        if r is None or not r["plateau"]["ok"]:
            problems.append(f"Lfb {lfb:g}: run failed or plateau rejected")
            continue
        if abs(r["plateau"]["dc_db"] - ref["plateau"]["dc_db"]) > ISO_GAIN_DB:
            problems.append(f"Lfb {lfb:g}: DC gain differs by more than {ISO_GAIN_DB} dB")
        x = next((c for c in r["crossings"] if c["direction"] == "down"), None)
        if (x is None) != (ref_x is None):
            problems.append(f"Lfb {lfb:g}: |T| = 1 crossing presence differs from nominal")
        elif x is not None:
            if abs(x["f_hz"] / ref_x["f_hz"] - 1) > ISO_FREQ_REL:
                problems.append(f"Lfb {lfb:g}: crossing frequency differs by more than {ISO_FREQ_REL:.1%}")
            if abs(x["phase_deg"] - ref_x["phase_deg"]) > ISO_PHASE_DEG:
                problems.append(f"Lfb {lfb:g}: loop phase at crossing differs by more than {ISO_PHASE_DEG} deg")
    bad = runs.get(LFB_INADEQUATE)
    if bad is not None and bad["plateau"]["ok"]:
        problems.append(f"inadequate Lfb {LFB_INADEQUATE:g} was NOT rejected by the plateau check")
    return problems


def classify(*, region_valid: bool, servo_ok: bool, iso_problems: list[str],
             plateau_ok: bool, ac_region_reproduces: bool) -> dict:
    """Bench fault versus finding, exactly DR-0008 section 4.

    FINDING only when the closure-independent region check itself shows the pair
    or tail not valid at the run's VCM. BENCH FAULT (not a finding, not routed
    into section 5 item 3) when the servo does not converge, the isolation
    study or plateau check fails, or the small-signal bench reports an invalid
    region the region check does not reproduce."""
    faults: list[str] = []
    if not servo_ok:
        faults.append("servo did not converge")
    if iso_problems:
        faults.append("Lfb isolation study failed: " + "; ".join(iso_problems))
    if not plateau_ok:
        faults.append("plateau check failed")
    if not ac_region_reproduces:
        faults.append("small-signal bench region differs from the closure-independent region check")
    if not region_valid:
        return dict(kind="finding", stop=True, faults=faults)
    if faults:
        return dict(kind="bench-fault", stop=False, faults=faults)
    return dict(kind="valid", stop=False, faults=[])


def route(kind: str, vcm: float = 0.6) -> str:
    """DR-0008 section 5, mechanically."""
    if kind == "finding":
        return ("Section 5 item 3 / the Stage A stop rule: the amplifier is not in a valid operating "
                f"region at VCM = {vcm:g} V. Stage B is NOT run on an invalid point. The later record "
                f"names the re-size (or an input-stage change for a {vcm:g} V common mode) and which "
                "ratified rows it re-opens; item 4 (a consumer-side common-mode change) is equally "
                "valid and is the consumer's decision. Nothing here edits the table. The consumer's "
                "verdict stays `unknown` (item 5) until the later record.")
    if kind == "bench-fault":
        return ("A bench fault, not a finding (DR-0008 section 4): fix the bench and re-run under "
                "the same approval. Nothing is routed into section 5.")
    return ("The region check is valid. Stage B (the 45-point batch) needs a separate operator "
            "approval citing DR-0008's PR, the C1-C4 answers (or their recorded `assumed` status) "
            "and a pre-submit estimate (DR-0008 section 4 Approval gate). Section 5 item 5: no row "
            "is opened and the consumer's verdict stays `unknown` (not `meets`, not `fails`).")


# --------------------------------------------------------------------------
# Simulation steps
# --------------------------------------------------------------------------


@dataclass
class RegionRun:
    vc: float
    ok_run: bool
    errors: list[str]
    ops: dict[str, DevOp]
    nodes: dict[str, float]
    valid: bool
    reasons: list[str]


NODE_PRINTS = ("v(xdut.tail)", "v(xdut.n1)", "v(xdut.n2)", "v(xdut.nz)", "v(vc)", "v(fbn)", "v(vinp)")


def op_control() -> list[str]:
    return ["op", "print " + " ".join(NODE_PRINTS), *(f"print {a}" for a in accessors())]


def run_region(work: Path, pdk: Pdk, vc: float, name: str) -> RegionRun:
    body = tb_body(TB_REGION, work, vcb=vc, vref=CFG["vcm"])
    log, _ = run_ngspice(wrap_deck(body, pdk, op_control()), work, name)
    vals = parse_values(log)
    errs = log_errors(log)
    ops = device_ops(vals)
    valid, reasons = region_verdict(ops)
    ran = all(n in vals for n in NODE_PRINTS[:3])
    return RegionRun(vc, ran and not errs, errs, ops,
                     {n: vals.get(n, float("nan")) for n in NODE_PRINTS}, valid and ran and not errs,
                     reasons if ran else ["operating point not produced"] + errs[:2])


def ac_only_control(raw: Path, vectors: str) -> list[str]:
    typ, ppd, f0, f1 = AC_ARGS
    return [f"ac {typ} {ppd} {f0:g} {f1:g}", "set filetype=ascii", f"write {raw} {vectors}"]


def ac_control(raw: Path, vectors: str) -> list[str]:
    return [*op_control(), *ac_only_control(raw, vectors)]


def read_raw(path: Path, names: tuple[str, ...]) -> dict[str, np.ndarray]:
    gg = _gain_driver()
    vec = gg.parse_ascii_complex_raw(path.read_text())
    for n in ("frequency", *names):
        if n not in vec:
            raise ValueError(f"rawfile has no {n} vector")
    return vec


def _gain_driver():
    from harness import load_sibling
    return load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")


@dataclass
class AcRun:
    name: str
    lfb: float
    vc: float
    ok_run: bool
    errors: list[str]
    ops: dict[str, DevOp] = field(default_factory=dict)
    nodes: dict[str, float] = field(default_factory=dict)
    freq: np.ndarray | None = None
    aol: np.ndarray | None = None
    result: dict | None = None
    note: str = ""


def run_ac(work: Path, pdk: Pdk, name: str, lfb: float, vc: float, beta: np.ndarray | None,
           freq_beta: np.ndarray | None) -> AcRun:
    body = tb_body(TB_AC, work, lfb=lfb, vcb=vc, vref=CFG["vcm"])
    raw = work / f"{name}.raw"
    log, _ = run_ngspice(wrap_deck(body, pdk, ac_control(raw, "v(vc) v(vinp) v(fbn)")), work, name)
    vals = parse_values(log)
    errs = log_errors(log)
    r = AcRun(name, lfb, vc, False, errs, device_ops(vals),
              {n: vals.get(n, float("nan")) for n in NODE_PRINTS})
    if errs or not raw.exists():
        r.note = "no result: " + ("; ".join(errs[:2]) or "no rawfile written")
        return r
    try:
        vec = read_raw(raw, ("v(vc)", "v(vinp)", "v(fbn)"))
    except ValueError as exc:
        r.note = f"unparseable rawfile: {exc}"
        return r
    freq = vec["frequency"].real
    vdiff = vec["v(vinp)"] - vec["v(fbn)"]
    if np.any(np.abs(vdiff) < 1e-9):
        r.note = "differential input phasor ~0: stimulus not reaching the DUT"
        return r
    r.freq, r.aol = freq, vec["v(vc)"] / vdiff
    r.ok_run = True
    if beta is not None:
        if not np.allclose(freq, freq_beta, rtol=1e-9):
            r.ok_run = False
            r.note = "frequency axis differs from the 1/beta run"
            return r
        r.result = small_signal(freq, r.aol, beta)
    return r


def run_beta(work: Path, pdk: Pdk) -> tuple[np.ndarray, np.ndarray, list[str]]:
    body = tb_body(TB_BETA, work, need_dut=False)
    raw = work / "beta.raw"
    log, _ = run_ngspice(wrap_deck(body, pdk, ac_only_control(raw, "v(vc) v(fbn)")), work, "beta")
    errs = log_errors(log)
    vec = read_raw(raw, ("v(vc)", "v(fbn)"))
    return vec["frequency"].real, vec["v(fbn)"] / vec["v(vc)"], errs


def save_dat(path: Path, header: str, freq: np.ndarray, **cols: np.ndarray) -> None:
    lines = [f"# {header}", "# freq_hz " + " ".join(f"re({k}) im({k})" for k in cols)]
    for i, f in enumerate(freq):
        lines.append(f"{f:.9e} " + " ".join(f"{c[i].real:.9e} {c[i].imag:.9e}" for c in cols.values()))
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# DC input sweep (consumer question: both inputs below the 1.20 V ICMR floor)
# --------------------------------------------------------------------------

#: Voltages only. Device-parameter accessors (`@m...[id]`) are NOT updated per point by `dc` in
#: ngspice (a first attempt printed one constant value for every point), so no currents are reported.
SWEEP_VECTORS = ("v(vc)", "v(xdut.tail)")


@dataclass
class SweepRun:
    name: str
    ok_run: bool
    note: str
    vin: np.ndarray | None = None
    cols: dict[str, np.ndarray] | None = None


def parse_wrdata(text: str, nvec: int) -> tuple[np.ndarray, list[np.ndarray]]:
    """ngspice `wrdata` writes (x, y) column pairs per vector, one row per point."""
    rows = [ln.split() for ln in text.splitlines() if ln.strip()]
    arr = np.array([[float(x) for x in r] for r in rows])
    if arr.ndim != 2 or arr.shape[1] != 2 * nvec:
        raise ValueError(f"wrdata has shape {arr.shape}, want (*, {2 * nvec})")
    return arr[:, 0], [arr[:, 2 * i + 1] for i in range(nvec)]


def rail_flags(vc: np.ndarray, vdd: float = VDD, margin: float = RAIL_MARGIN_V) -> list[str]:
    """'low' / 'high' / 'ok' per point: within `margin` of a rail or not. NaN is 'undefined'."""
    out = []
    for v in vc:
        if not math.isfinite(v):
            out.append("undefined")
        elif v < margin:
            out.append("low")
        elif v > vdd - margin:
            out.append("high")
        else:
            out.append("ok")
    return out


def run_sweep(work: Path, pdk: Pdk, tb: Path, name: str, **params) -> SweepRun:
    body = tb_body(tb, work, **params)
    dat = work / f"{name}.dat"
    control = [f"dc vin 0 {SWEEP_MAX_V:g} {SWEEP_STEP_V:g}", "option numdgt=7",
               f"wrdata {dat} " + " ".join(SWEEP_VECTORS)]
    log, _ = run_ngspice(wrap_deck(body, pdk, control), work, name)
    errs = log_errors(log)
    if errs or not dat.exists():
        return SweepRun(name, False, "no result: " + ("; ".join(errs[:2]) or "no data file written"))
    try:
        vin, cols = parse_wrdata(dat.read_text(), len(SWEEP_VECTORS))
    except ValueError as exc:
        return SweepRun(name, False, f"unparseable wrdata: {exc}")
    return SweepRun(name, True, "", vin, dict(zip(SWEEP_VECTORS, cols)))


def sweep_table(r: SweepRun, every: int = 2) -> list[str]:
    rows = ["| Vin (V) | v(vc) (V) | rail | tail (V) |", "|---|---|---|---|"]
    flags = rail_flags(r.cols["v(vc)"])
    for i in range(0, len(r.vin), every):
        c = r.cols
        rows.append(f"| {r.vin[i]:.2f} | {c['v(vc)'][i]:.4f} | {flags[i]} | {c['v(xdut.tail)'][i]:.4f} |")
    return rows


def sweep_summary(r: SweepRun) -> str:
    vc = r.cols["v(vc)"]
    fl = rail_flags(vc)
    low = [f"{v:.2f}" for v, f in zip(r.vin, fl) if f != "ok"]
    below = r.vin < 1.20 - 1e-9
    return (f"v(vc) over the sweep: min {np.nanmin(vc):.4f} V, max {np.nanmax(vc):.4f} V; points within "
            f"{RAIL_MARGIN_V:g} V of a rail (0 / {VDD:g} V): "
            + (", ".join(low) if low else "none") + f"; points below 1.20 V: {int(below.sum())} of {len(r.vin)}")


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def fmt_hz(v: float) -> str:
    if not math.isfinite(v):
        return "n/a"
    for s, u in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(v) >= s:
            return f"{v / s:.4g} {u}"
    return f"{v:.4g} Hz"


def region_table(ops: dict[str, DevOp], only: tuple[str, ...] | None = None) -> list[str]:
    rows = ["| dev | role | ID (uA) | abs Vgs (V) | abs Vth (V) | abs Vds (V) | abs Vdsat (V) | gm (uS) | region |",
            "|---|---|---|---|---|---|---|---|---|"]
    for k, o in ops.items():
        if only and k not in only:
            continue
        rows.append(f"| {o.name} | {o.role} | {o.id_a * 1e6:.3f} | {o.vgs:.4f} | {o.vth:.4f} | "
                    f"{o.vds:.4f} | {o.vdsat:.4f} | {o.gm * 1e6:.2f} | {o.region} |")
    return rows


def build_record(*, record: str, stamp: datetime, pdk: Pdk, regions: list[RegionRun],
                 nominal: AcRun | None, vc_runs: list[AcRun], iso: dict[float, AcRun],
                 iso_problems: list[str], cls: dict, beta_errs: list[str], argv: list[str],
                 sha: str, sweeps: list[SweepRun] | None = None) -> str:
    L: list[str] = []
    a = L.append
    nom_vc = VC_BIAS_ASSUMED
    a(f"# Stage A probe: this op-amp into gf180-buck's compensation load — record `{record}`")
    a("")
    a(f"- **Date**: {stamp:%Y-%m-%d %H:%M:%S} UTC")
    a(f"- **Issue / decision record**: #295; `spec/decision-records/0008-buck-compensation-load-gbw-pm-row.md` §4 Stage A")
    a(f"- **Repo commit**: `{sha}` (working tree at run time; see `netlist-snapshots/{record}.spice` for the exact bench and DUT)")
    a(f"- **Conditions**: typical MOS + `res_typical` + `mimcap_typical`, {TEMP_C:g} °C, VDD = {VDD:g} V, "
      f"ibias 10 µA ideal, one corner, local `ngspice -b` units (no `klt sim` batch, no fleet)")
    a(f"- **PDK**: {pdk.variant} (open_pdks {pdk.version}, via {pdk.source}); **ngspice**: {ngspice_version()}")
    a(f"- **Command**: `python3 sim/buck-load-gbw-pm/run_stage_a.py {' '.join(argv)}`".rstrip())
    a(f"- **Common mode / Rbot**: VREF = FBN = {vcm_s()}, Rbot = {CFG['rbot']}"
      + ("" if CFG["vcm"] == 0.6 else "; this supersedes the 0.6 V case of record `20261110-084324-e8a2a7b` "
         "(untouched, append-only)"))
    a(f"- **Consumer load of record**: {CFG['load_note']}")
    a("")
    a("> Every number below is a **simulated output at one corner, pre-layout, ideal consumer passives**. "
      "It sets **no GBW/PM row**, relaxes nothing, and the ratified 10 MHz / 2 pF row is untouched. "
      "Nothing here is measured hardware.")
    a("")
    a("## Verdict of this probe")
    a("")
    kind = cls["kind"]
    label = {"finding": f"FINDING — invalid operating region at VCM = {vcm_s()} (stop rule applies)",
             "bench-fault": "BENCH FAULT — not a finding; fix and re-run under the same approval",
             "valid": "Region check valid; no bench fault"}[kind]
    a(f"**{label}**")
    a("")
    for f in cls["faults"]:
        a(f"- bench fault: {f}")
    a("")
    if kind == "finding":
        a("**Caveat on §2–§3 (read before quoting any number):** the closure-independent region check shows the "
          f"input pair or tail not on and saturated at VCM = {vcm_s()} (§1 tabulates which), and the small-signal "
          "bench sits at the same invalid point (its own operating point is "
          "tabulated in §2). Its Aol(f), crossings and loop margins therefore describe a degraded, sub-threshold "
          "amplifier, **not** this design's capability; they are reported because Stage A asks for them, are "
          "insensitive to `VC_bias` here, and are **not** a basis for any GBW/PM number or a verdict on the "
          "consumer's loop (DR-0008 §4 stop rule, §5 item 5).")
        a("")
    a("**Routing (DR-0008 §5, mechanical):** " + route(kind, CFG["vcm"]))
    a("")
    a("## C1–C4 status (DR-0008 §3)")
    a("")
    a("| # | Item | Status in this record |")
    a("|---|---|---|")
    src = ("gf180-buck design/compensator.md" if CFG["vcm"] == 0.6
           else "gf180-buck PR #132, DR 0008 §3")
    for c, item, status in CONSUMER_ITEMS:
        a(f"| {c} | {item} | {status.format(vcm=vcm_s(), src=src) if '{vcm}' in status else status} |")
    a("")
    a(f"`VC_bias` = {nom_vc:.2f} V **(assumed)**: duty = 1.8 V / 5 V (Vin 5 V nominal, gf180-buck "
      f"`design/compensator.md`) = 0.36; ramp 1.18–1.80 V (0.613 V p-p, gf180-buck `design/README.md` osc_ramp "
      f"row) ⇒ 1.18 + 0.36 × 0.613 = 1.40 V. Results are also reported at ±0.15 V.")
    a("")
    a("## 1. Region check (closure-independent)")
    a("")
    a(f"`VC` forced by an ideal source, `VREF` = `FBN` = {vcm_s()}, `.op` (bench `tb_region.spice`). XM1/XM2/XM5 do not "
      "depend on `VC`; regions use `design/check_dc_op.py`'s definition (on = |Vgs| > |Vth|; "
      f"saturated = |Vds| ≥ |Vdsat| + {VDS_MARGIN_V * 1e3:g} mV).")
    a("")
    for rr in regions:
        a(f"### VC forced at {rr.vc:.2f} V — {'valid' if rr.valid else 'NOT VALID'}")
        a("")
        a(f"Internal nodes: " + ", ".join(
            f"`{n}` = {rr.nodes[n]:.4f} V" for n in NODE_PRINTS[:5]))
        a("")
        a("\n".join(region_table(rr.ops, REGION_DEVICES if rr is not regions[0] else None)))
        a("")
        for r_ in rr.reasons:
            a(f"- {r_}")
        for e in rr.errors[:3]:
            a(f"- ngspice: `{e}`")
        a("")
    a("The pair + tail rows (XM1, XM2, XM5) are the Stage A question: whether the input pair conducts at "
      f"VCM = {vcm_s()} against the proposed ICMR of DR-0005 (1.20 V ≤ VCM ≤ 2.70 V). Rows for the other devices at the "
      "first VC level are shown for completeness and depend on the forced `VC`.")
    a("")
    a("## 2. Small-signal bench (DC-closed, AC-open)")
    a("")
    a("Aol(f) = v(vc) / (v(vinp) − v(fbn)) into the real load (+ PWM-comparator gate); 1/β(f) = v(vc)/v(fbn) from "
      "the passive network alone; T = Aol·β; the 'open-loop crossing of 1/β' is the downward |T| = 1 crossing. "
      "Loop margin = 180° + unwrapped phase(T) at the crossing — reported, not judged.")
    a("")

    def ac_section(r: AcRun, title: str) -> None:
        a(f"### {title}")
        a("")
        if not r.ok_run or r.result is None:
            a(f"No result: {r.note or 'run failed'}")
            for e in r.errors[:3]:
                a(f"- ngspice: `{e}`")
            a("")
            return
        res = r.result
        pl = res["plateau"]
        a(f"- plateau check (lowest decade): {'pass' if pl['ok'] else 'REJECTED — ' + pl['reason']}; "
          f"spread {pl.get('spread_db', float('nan')):.4f} dB, phase {pl.get('phase_deg', float('nan')):.2f}°")
        a(f"- Aol DC gain: {pl.get('dc_db', float('nan')):.2f} dB")
        a("- Aol 0 dB crossings (amplifier alone in the load): " +
          (", ".join(f"{fmt_hz(f)} ({d})" for f, d in res["aol_unity"]) or "none"))
        if res["crossings"]:
            a("- |T| = 1 crossings (|Aol| meets |1/β|): " + "; ".join(
                f"{fmt_hz(c['f_hz'])} ({c['direction']}, phase {c['phase_deg']:.1f}°, "
                f"180°+phase = {c['margin_deg']:.1f}°)" for c in res["crossings"]))
        else:
            a("- |T| = 1 crossings: none in 0.1 Hz – 1 GHz")
        a("")
        a("| f | Aol (dB) | 1/β (dB) | Aol − 1/β = T (dB) | Aol phase (°) |")
        a("|---|---|---|---|---|")
        for s in res["spots"]:
            a(f"| {fmt_hz(s['f_hz'])} | {s['aol_db']:.2f} | {s['invb_db']:.2f} | {s['t_db']:.2f} | "
              f"{s['aol_phase_deg']:.1f} |")
        a("")

    if nominal:
        ac_section(nominal, f"VC_bias = {nom_vc:.2f} V (assumed), Lfb = {LFB_NOMINAL:g} H")
        a("Operating point of this bench, for the region cross-check against §1 "
          f"(`tail` = {nominal.nodes.get('v(xdut.tail)', float('nan')):.4f} V, "
          f"`vc` = {nominal.nodes.get('v(vc)', float('nan')):.4f} V, "
          f"`fbn` = {nominal.nodes.get('v(fbn)', float('nan')):.4f} V):")
        a("")
        a("\n".join(region_table(nominal.ops, REGION_DEVICES)))
        a("")
    for r in vc_runs:
        ac_section(r, f"VC_bias = {r.vc:.2f} V ({r.vc - nom_vc:+.2f} V), Lfb = {r.lfb:g} H")
    a("## 3. Lfb isolation study")
    a("")
    a(f"Nominal Lfb = {LFB_NOMINAL:g} H at VC_bias = {nom_vc:.2f} V; Lfb ∈ {{1e8, 1e10}} must agree "
      f"(Aol DC gain within {ISO_GAIN_DB} dB, |T| = 1 crossing within {ISO_FREQ_REL:.1%}, loop phase within "
      f"{ISO_PHASE_DEG}°); a deliberately inadequate Lfb = {LFB_INADEQUATE:g} H must be rejected by the plateau check.")
    a("")
    a("| Lfb (H) | run | plateau check | Aol DC (dB) | first downward \\|T\\|=1 | note |")
    a("|---|---|---|---|---|---|")
    for lfb, r in iso.items():
        if r.result is None:
            a(f"| {lfb:g} | failed | – | – | – | {r.note} |")
            continue
        pl = r.result["plateau"]
        x = next((c for c in r.result["crossings"] if c["direction"] == "down"), None)
        a(f"| {lfb:g} | ok | {'pass' if pl['ok'] else 'rejected'} | {pl.get('dc_db', float('nan')):.3f} | "
          f"{fmt_hz(x['f_hz']) if x else 'none'} | {pl['reason'] if not pl['ok'] else ''} |")
    a("")
    a("Isolation problems: " + ("none" if not iso_problems else "; ".join(iso_problems)))
    a("")
    if beta_errs:
        a("1/β run reported: " + "; ".join(f"`{e}`" for e in beta_errs[:3]))
        a("")
    if sweeps:
        a("## 4. DC input sweep: output behaviour with both inputs below / across the 1.20 V ICMR floor")
        a("")
        a("Consumer question (gf180-buck#131 / PR #132): the reference ramps 0 → 1.5 V in ~250 µs, about 80 % of it "
          "below the 1.20 V proposed ICMR; is the amplifier's output defined and non-railing there, or not defined? "
          f"This is a **static DC sweep** (`dc vin 0 {SWEEP_MAX_V:g} {SWEEP_STEP_V:g}`, typical / 27 °C / 3.30 V, one "
          "local unit per bench), not a transient: it says nothing about the 250 µs dynamics. Rows every "
          f"{2 * SWEEP_STEP_V:g} V; `rail` = within {RAIL_MARGIN_V:g} V of 0 or {VDD:g} V. It reports numbers only: "
          "no limit, no row, no verdict on the consumer.")
        a("")
        for r in sweeps:
            a(f"### {r.name}")
            a("")
            if not r.ok_run:
                a(f"No result: {r.note}")
                a("")
                continue
            a(sweep_summary(r))
            a("")
            a("\n".join(sweep_table(r)))
            a("")
        a("- Voltages only: the tail node `v(xdut.tail)` shows where the input pair's common source sits (it follows "
          "the input and sits at ~0 V while the input is low); device currents are not reported because ngspice does not update device-parameter accessors "
          "per `dc` point.")
        a("- In the follower, a `low` flag at Vin ≤ 0.15 V is the output equal to its own near-0 V input (not a "
          "fault by itself); the open-loop rows show the railing.")
        a("- `follower`: VC tied to the inverting input (DC-closed). It is NOT the consumer's type-III network; that "
          "network has no DC feedback through the amplifier (C1, C2 open), so the consumer's own DC output is set by "
          "its other paths.")
        a("- `open vd=…`: no feedback, both inputs at the swept level, differential offset `vd`: where the output "
          "goes for a small input difference of either sign when the input stage is cut off or conducting.")
        a("")
        lim = "## 5. Limits of this record"
    else:
        lim = "## 4. Limits of this record"
    a(lim)
    a("")
    a("- One corner (typical / 27 °C / 3.30 V): no PVT statement of any kind. Stage B is not run and is not approved here.")
    if CFG["vcm"] != 0.6:
        a("- The 1.50 V common-mode margin to the true (measured) low ICMR edge across corners is not established here: "
          "that is Stage B / a later record.")
    a("- Consumer passives are ideal and typical; the PWM-comparator input is an approximation (C3 assumed); "
      "the amplifier's own input capacitance on `FBN` is inside Aol but not in the passive 1/β run (femtofarad-class "
      "against C2 = 35.7 pF).")
    a("- No GBW or PM number is proposed. The quantities above are outputs of this one bench; the later record "
      "derives any row from Stage B per DR-0008 §5.")
    a("- The data (`corners/<id>/*.dat`: frequency and real/imaginary parts) and logs are committed so the numbers "
      "above can be re-derived without a simulator.")
    a("")
    return "\n".join(a_ for a_ in L) + "\n"


def build_snapshot(record: str, pdk: Pdk) -> str:
    parts = [f"* Stage A snapshot {record} (issue #295): everything needed to reproduce the bench",
             "", f"*==== {CFG['load'].name} (staged as consumer_load.inc) ====", CFG["load"].read_text()]
    benches = (TB_REGION, TB_AC, TB_BETA) + ((TB_SWEEP_FOLLOWER, TB_SWEEP_OPEN) if CFG["sweep"] else ())
    for t in benches:
        parts += [f"*==== {t.name} ====", t.read_text()]
    parts += ["*==== DUT (committed export through subckt_from_export) ====", load_dut_text()]
    return "\n".join(parts)


def plots(out: Path, nominal: AcRun) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    out.mkdir(parents=True, exist_ok=True)
    res = nominal.result
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.semilogx(nominal.freq, res["db_aol"], label="|Aol| into the real load")
    ax.semilogx(nominal.freq, res["db_invb"], label="|1/β|")
    ax.set_xlabel("Hz"); ax.set_ylabel("dB"); ax.grid(True, which="both", alpha=.3); ax.legend()
    ax.set_title(f"Stage A, typical / 27 C / 3.30 V, VCM {CFG['vcm']:g} V, VC_bias 1.40 V (assumed)")
    p = out / "aol_vs_invbeta.png"
    fig.savefig(p, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return [p.name]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def smoke(pdk: Pdk) -> int:
    with tempfile.TemporaryDirectory(prefix="buck-stage-a-") as scratch:
        work = Path(scratch)
        stage(work, pdk)
        rr = run_region(work, pdk, VC_BIAS_ASSUMED, "region")
        print(f"smoke: region check {'valid' if rr.valid else 'NOT valid'} at VCM = {vcm_s()}")
        for r_ in rr.reasons:
            print("  -", r_)
        return 0 if rr.ok_run else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="region check only; writes nothing")
    ap.add_argument("--vcm", type=float, default=0.6,
                    help="EA common mode: 0.6 (default, the original record's network) or 1.5 "
                         "(gf180-buck DR 0008: Rbot 50k; also runs the DC input sweep)")
    args = ap.parse_args(argv)
    try:
        configure(args.vcm)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if shutil.which("ngspice") is None:
        print("ngspice not found", file=sys.stderr)
        return 2
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=True)
    with tempfile.TemporaryDirectory(prefix="buck-stage-a-") as scratch:
        work = Path(scratch)
        stage(work, pdk)

        regions = [run_region(work, pdk, VC_BIAS_ASSUMED + o, f"region_{i}") for i, o in enumerate(VC_OFFSETS)]
        freq_b, beta, beta_errs = run_beta(work, pdk)
        nominal = run_ac(work, pdk, "ac_nominal", LFB_NOMINAL, VC_BIAS_ASSUMED, beta, freq_b)
        vc_runs = [run_ac(work, pdk, f"ac_vc{o:+.2f}", LFB_NOMINAL, VC_BIAS_ASSUMED + o, beta, freq_b)
                   for o in VC_OFFSETS[1:]]
        iso: dict[float, AcRun] = {LFB_NOMINAL: nominal}
        for lfb in (*LFB_ISOLATION, LFB_INADEQUATE):
            iso[lfb] = run_ac(work, pdk, f"ac_lfb{lfb:g}", lfb, VC_BIAS_ASSUMED, beta, freq_b)
        iso_problems = isolation_verdict({k: v.result for k, v in iso.items()})

        sweeps: list[SweepRun] = []
        if CFG["sweep"]:
            sweeps.append(run_sweep(work, pdk, TB_SWEEP_FOLLOWER, "sweep_follower"))
            for vd in SWEEP_OFFSETS_V:
                sweeps.append(run_sweep(work, pdk, TB_SWEEP_OPEN, f"sweep_open_vd{vd * 1e3:g}mV", vd=vd))
            for r in sweeps:
                r.name = {"sweep_follower": "follower (VC tied to inverting input)"}.get(
                    r.name, r.name.replace("sweep_open_vd", "open loop, vd = ").replace("mV", " mV"))

        servo_ok = all(r.ok_run for r in (nominal, *vc_runs))
        plateau_ok = all(r.result and r.result["plateau"]["ok"] for r in (nominal, *vc_runs))
        region_valid = regions[0].valid
        # The small-signal bench must not report an invalid pair/tail the region check lacks.
        _, ac_reasons = region_verdict(nominal.ops) if nominal.ops else (False, ["no op"])
        ac_region_valid = not ac_reasons
        cls = classify(region_valid=region_valid, servo_ok=servo_ok, iso_problems=iso_problems,
                       plateau_ok=plateau_ok,
                       ac_region_reproduces=(ac_region_valid == region_valid) or not servo_ok)

        bad_sweeps = [r.name for r in sweeps if not r.ok_run]
        if bad_sweeps:
            cls["faults"].append("DC input sweep failed: " + ", ".join(bad_sweeps))
            if cls["kind"] == "valid":
                cls["kind"] = "bench-fault"

        for p in ("corners", "plots"):
            paths[p].mkdir(parents=True, exist_ok=True)
        for r in (nominal, *vc_runs, *[v for k, v in iso.items() if k != LFB_NOMINAL]):
            if r.freq is not None:
                save_dat(paths["corners"] / f"{r.name}.dat", f"{record} {r.name}: Aol=v(vc)/(v(vinp)-v(fbn)), "
                         f"Lfb={r.lfb:g} VC_bias={r.vc:g}", r.freq, aol=r.aol)
        save_dat(paths["corners"] / "beta.dat", f"{record}: beta=v(fbn)/v(vc), passive network alone", freq_b, beta=beta)
        for f in (*work.glob("*.log"), *work.glob("sweep_*.dat")):
            shutil.copyfile(f, paths["corners"] / f.name)
        for f in work.glob("*.cir"):
            shutil.copyfile(f, paths["corners"] / f.name)
        if nominal.result is not None:
            plots(paths["plots"], nominal)
        paths["snapshot"].write_text(build_snapshot(record, pdk))
        from harness import git_short_sha
        md = build_record(record=record, stamp=stamp, pdk=pdk, regions=regions, nominal=nominal,
                          vc_runs=vc_runs, iso=iso, iso_problems=iso_problems, cls=cls,
                          beta_errs=beta_errs, argv=sys.argv[1:], sha=git_short_sha(REPO_ROOT),
                          sweeps=sweeps)
        paths["record"].write_text(md)

    print(f"record: {paths['record'].relative_to(REPO_ROOT)}")
    print(f"result: {cls['kind']}" + (f" ({'; '.join(cls['faults'])})" if cls["faults"] else ""))
    return 0 if cls["kind"] != "bench-fault" else 1


if __name__ == "__main__":
    sys.exit(main())
