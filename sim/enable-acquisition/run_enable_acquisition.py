#!/usr/bin/env python3
"""Supply and external-bias enable acquisition of the committed sized schematic
across the full ratified PVT grid (issue #191). EVIDENCE ONLY.

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_enable.spice`) declares no transistor.

What it runs
------------
Three enable scenarios (simultaneous rail+bias enable; supply-first with a
delayed bias; bias disable / re-enable at fixed supply) x a fast / slow
diagnostic ramp pair x the 45-point grid `process {typical, ff, ss, fs, sf} x
temperature {-40, 27, 125 C} x supply {2.97, 3.30, 3.63 V}`. Each (scenario,
ramp) is ONE `klt sim` request (a `corners` block), plus one 45-point DC
request for the powered steady reference. Which backend executes them is
`klt`'s decision (`--backend`, or `$KLT_SIM_BACKEND`); on a dispatch worker
that is the Spot batch fleet. This script never launches ngspice itself for a
grid and never falls back to a local grid when a batch submit fails: no
record is written and the error is printed. Only the single-unit controls
(nominal point) run locally.

Per point and enable event it extracts, from the rawfile klt retains:

  * the initial state (rail, bias, input and output all at ~0 at t = 0: the
    run must NOT start at the powered operating point; a run that does is
    INVALID, never an instant acquisition),
  * the powered steady follower level v_ref, from a SEPARATE DC solution of the
    same bench with the enables fixed on (never from the run being judged),
  * the acquisition time: from the instant the event's last ramp completes to
    the LAST exit of |vout - v_ref| from a +-1 % diagnostic band, interpolated
    linearly. An acquisition is claimed only when the output then STAYS in the
    band to the end of the window, drifts by <= DRIFT_FRAC * band across the
    final reference window and the in-band tail lasts >= MIN_DWELL_FRAC of the
    window; otherwise the event is CENSORED / NOT ACQUIRED (rec = inf), ranked
    as the worst result and never turned into a finite time,
  * peak excursions (over/undershoot about v_ref after the ramps complete, and
    the output's excess over the instantaneous rail).

Any censored event is re-run over a window LONG_WINDOW_FACTOR times longer
BEFORE it is described as persistent; both outcomes are recorded.

There is NO startup spec row (`spec/target-spec.md`), and this experiment adds
none and imposes none: ramp rates and windows are diagnostic experiment
conditions, not consumer guarantees, and a consumer-loop startup or a
bias-generator is outside this study.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<scenario>_<ramp>/<process>_<T>c_<vdd>v.{log,cir,dat.gz}   one set per point
    corners/<rid>/long_<scenario>_<ramp>/...   long-window follow-up points (same set + report)
    corners/<rid>/reference/...      powered steady DC reference
    corners/<rid>/*/klt-report.json  sanitised klt reports
    corners/<rid>/controls/...       single-unit controls
    netlist-snapshots/<rid>.spice
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/enable-acquisition/run_enable_acquisition.py              # full grid + record
    python3 sim/enable-acquisition/run_enable_acquisition.py --smoke      # one nominal point per scenario, local, NO record
    python3 sim/enable-acquisition/run_enable_acquisition.py --pilot      # timing pilot: one local nominal run, NO record

Exit status: 0 when the evidence is complete; 1 otherwise; 2 when a grid could
not be run (no record written).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import time
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
    load_sibling,
    ngspice_version,
    remote_of,
    run_klt,
    run_klt_retrying,
    sanitise_report,
    stage_workdir,
)

g = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")
ssp = load_sibling("slew_swing_power_driver", "sim/slew-swing-power/run_slew_swing_power.py")
sr = load_sibling("step_response_driver", "sim/step-response/run_step_response.py")
mc = load_sibling("enable_acquisition_measurement_config", "sim/enable-acquisition/measurement_config.py")

TESTBENCH = HERE / "testbench" / "tb_enable.spice"

CORNERS, TEMPS_C, SUPPLIES_V = mc.CORNERS, mc.TEMPS_C, mc.SUPPLIES_V
NOMINAL = g.NOMINAL
REF_WINDOW, BAND_FRAC, DRIFT_FRAC = mc.REF_WINDOW, mc.BAND_FRAC, mc.DRIFT_FRAC
MIN_DWELL_FRAC, SAMPLE_BEFORE = mc.MIN_DWELL_FRAC, mc.SAMPLE_BEFORE
INIT_MAX_V, INIT_MAX_RAIL_FRAC = mc.INIT_MAX_V, mc.INIT_MAX_RAIL_FRAC
FINAL_RAIL_TOL_V, REF_LEVEL_TOL_V = mc.FINAL_RAIL_TOL_V, mc.REF_LEVEL_TOL_V
GBW_MIN_HZ = mc.GBW_MIN_HZ

MIN_WINDOW_SAMPLES = 50
MAX_GAP_STEPS = 5  # a sample gap larger than this many tstep inside an event window = missing samples
XCHK_LEVEL_V = 20e-3  # cross-check tolerance against ngspice's own `.meas`
#: cl-x5 must acquire later than nominal by more than this (absolute acquisition instants).
CL_MARGIN_S = 2e-9

Key = g.Key

#: Traces retained (committed per point, gzip).
TRACES = ("v(vdd)", "v(rb)", "v(ibias)", "v(vinp)", "v(vout)", "v(xdut.n1)", "v(xdut.n2)", "v(xdut.nz)", "v(xdut.tail)")
REQUIRED = ("time", "v(vdd)", "v(rs)", "v(rb)", "v(ibias)", "v(vinp)", "v(vout)")
DAT_COLS = ("time_s", "vdd_v", "rs", "rb", "ibias_v", "vinp_v", "vout_v", "n1_v", "n2_v", "nz_v", "tail_v")
_RAW_ORDER = ("time", "v(vdd)", "v(rs)", "v(rb)", "v(ibias)", "v(vinp)", "v(vout)",
              "v(xdut.n1)", "v(xdut.n2)", "v(xdut.nz)", "v(xdut.tail)")
DAT_HEADER = " ".join(DAT_COLS)


# --------------------------------------------------------------------------
# Testbench materialisation (guards shared with the gain driver)
# --------------------------------------------------------------------------

_BIAS_RE = re.compile(r"^Bibias\s+vdd\s+ibias\s+I\s*=\s*\S+\*v\(rb\)[ \t]*$", re.M)
_CL_RE = re.compile(r"^CL\s+vout\s+0\s+\S+[ \t]*$", re.M)
_RS_RE = re.compile(r"^Vrs\s+rs\s+0\s+pwl\(.*\)[ \t]*$", re.M)
_RB_RE = re.compile(r"^Vrb\s+rb\s+0\s+pwl\(.*\)[ \t]*$", re.M)
_PARAM_NAMES = ("t_vdd", "tr_s", "t_ib", "tr_b", "t_off", "t_on2")


def _sub1(rx: re.Pattern, repl: str, text: str, what: str) -> str:
    out, n = rx.subn(lambda _m: repl, text)
    if n != 1:
        raise RuntimeError(f"testbench {what} line not found exactly once")
    return out


def _fmt_t(x: float) -> str:
    return format(x, ".12g")


def apply_schedule(tb: str, sched: dict) -> str:
    """Substitute the six schedule `.param` lines (each must match exactly once)."""
    for name in _PARAM_NAMES:
        tb = _sub1(re.compile(rf"^\.param\s+{name}=\S+[ \t]*$", re.M), f".param {name}={_fmt_t(sched['params'][name])}",
                   tb, f".param {name}")
    return tb


def materialise(work: Path, pdk: Pdk, sched: dict, *, ibias_a: float | None = None, cl_f: float | None = None,
                powered_start: bool = False, dc_reference: bool = False, dut_text: str | None = None) -> Path:
    """Write the per-run work directory; return the testbench path.

    The committed testbench is used verbatim except for the two `.include`
    targets, the schedule `.param` values and, for a control only, an explicit
    bias or CL override. `powered_start` (control) / `dc_reference` (the
    separate powered steady solution) replace the two envelope sources by
    DC 1 -- the run then DOES start at the powered operating point, which the
    initial-state check must reject for a transient and which is what the
    reference solution needs.
    """
    tb = stage_workdir(work, pdk, TESTBENCH.read_text(), guard_tb=g.guard_testbench,
                       guard_dut=g.guard_dut, dut_text=dut_text, tb_label=TESTBENCH.name)
    tb = apply_schedule(tb, sched)
    if ibias_a is not None:
        tb = _sub1(_BIAS_RE, f"Bibias vdd ibias I = {ibias_a:g}*v(rb)", tb, "Bibias")
    if cl_f is not None:
        tb = _sub1(_CL_RE, f"CL vout 0 {cl_f:g}", tb, "CL")
    if powered_start or dc_reference:
        tb = _sub1(_RS_RE, "Vrs rs 0 dc 1", tb, "Vrs")
        tb = _sub1(_RB_RE, "Vrb rb 0 dc 1", tb, "Vrb")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


def make_request(netlist: Path, pdk: Pdk, sched: dict, corners, temps, supplies) -> dict:
    """One `corners` transient request for a schedule."""
    req = g.ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = mc.analysis(sched)
    req["measurements"] = mc.measurements(sched)
    req["options"] = {"timeout_s": 900, "keep_artifacts": True, "waveforms": True}
    return req


def make_reference_request(netlist: Path, pdk: Pdk, corners, temps, supplies) -> dict:
    """The powered steady DC solution of every point (envelopes fixed on): a
    one-step DC sweep of the bias envelope (`.meas dc ... AT=` works on any runner)."""
    req = g.ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = {"kind": "dc", "args": "Vrb 1 1.1 0.1"}
    req["measurements"] = [
        {"name": "vref_v", "spice": ".meas dc vref_v FIND v(vout) AT=1", "unit": "V"},
        {"name": "vinp_v", "spice": ".meas dc vinp_v FIND v(vinp) AT=1", "unit": "V"},
        {"name": "vdd_v", "spice": ".meas dc vdd_v FIND v(vdd) AT=1", "unit": "V"},
    ]
    req["options"] = {"timeout_s": 300, "keep_artifacts": True}
    return req


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class Event:
    """One enable event. `acq_s` is seconds from the instant its last ramp
    completes (negative: already in band before the ramp finished); `math.inf`
    means CENSORED / NOT ACQUIRED within the window; `nan` is unevaluated."""

    name: str
    t_start: float
    t_complete: float
    t_end: float
    acq_s: float = float("nan")
    t_acq: float = float("nan")  # absolute instant of the last band exit (nan when censored)
    why: str = ""  # censoring reason
    v_final: float = float("nan")  # output at the end of the window
    drift: float = float("nan")  # drift across the final reference window, in bands
    dwell_s: float = float("nan")  # in-band tail duration
    over_mv: float = float("nan")  # peak output excess over v_ref after the ramps complete
    under_mv: float = float("nan")  # peak output shortfall below v_ref after the ramps complete
    rail_excess_mv: float = float("nan")  # peak (vout - instantaneous vdd) over the event, clamped at 0, mV
    vout_peak: float = float("nan")

    @property
    def censored(self) -> bool:
        return math.isinf(self.acq_s)


@dataclass
class Metrics:
    valid: bool
    reason: str = ""
    events: list = field(default_factory=list)
    v_ref: float = float("nan")
    init: dict = field(default_factory=dict)
    vec: dict = field(default_factory=dict, repr=False)

    @property
    def acq(self) -> float:
        """Worst event acquisition; inf when any event is censored."""
        return max(e.acq_s for e in self.events) if self.valid and self.events else float("nan")

    @property
    def censored(self) -> bool:
        return self.valid and any(e.censored for e in self.events)


def _bad(reason: str, **kw) -> Metrics:
    return Metrics(valid=False, reason=reason, **kw)


def extract_event(t, vo, vd, ev: Event, v_ref: float, tstep: float) -> tuple[Event | None, str]:
    """Acquisition figures of one event window, or (None, reason) when the
    window cannot be judged (too few samples / gaps)."""
    t0, tc, t1 = ev.t_start, ev.t_complete, ev.t_end
    ref_start = t1 - REF_WINDOW
    sel = (t >= t0) & (t <= t1)
    if np.count_nonzero(sel) < MIN_WINDOW_SAMPLES:
        return None, f"{ev.name}: fewer than {MIN_WINDOW_SAMPLES} samples in the window"
    ts = t[sel]
    if np.max(np.diff(ts)) > MAX_GAP_STEPS * tstep or ts[0] > t0 + MAX_GAP_STEPS * tstep or ts[-1] < t1 - MAX_GAP_STEPS * tstep:
        return None, f"{ev.name}: missing samples (a gap larger than {MAX_GAP_STEPS} sample intervals in the window)"
    tw = np.concatenate([[t0], t[(t > t0) & (t < t1)], [t1]])
    vw = np.interp(tw, t, vo)
    band = BAND_FRAC * abs(v_ref)
    err = vw - v_ref
    drift = abs(float(np.interp(t1, t, vo) - np.interp(ref_start, t, vo))) / band
    out = np.nonzero(np.abs(err) > band)[0]
    v_final = float(np.interp(t1, t, vo))
    post = tw >= tc
    over = max(0.0, float(err[post].max())) * 1e3
    under = max(0.0, -float(err[post].min())) * 1e3
    rail = max(0.0, float(np.max(vw - np.interp(tw, t, vd)))) * 1e3  # 0 when vout never exceeds the rail
    base = dict(v_final=v_final, drift=drift, over_mv=over, under_mv=under, rail_excess_mv=rail,
                vout_peak=float(vw.max()))
    out_ev = Event(ev.name, t0, tc, t1, **base)
    if out.size == 0:
        t_acq = t0  # never left the band inside the window
    else:
        i = int(out[-1])
        if i + 1 >= tw.size or tw[i] >= ref_start:
            out_ev.acq_s, out_ev.why = math.inf, "outside the band at the end of the window"
            return out_ev, ""
        a, b = abs(err[i]), abs(err[i + 1])
        f = (a - band) / (a - b) if a != b else 0.0
        t_acq = float(tw[i] + f * (tw[i + 1] - tw[i]))
    if drift > DRIFT_FRAC:
        out_ev.acq_s, out_ev.why = math.inf, f"drifts {drift:.2f} bands across the final reference window (slow tail)"
        return out_ev, ""
    dwell = t1 - t_acq
    if dwell < MIN_DWELL_FRAC * (t1 - tc):
        out_ev.acq_s, out_ev.why = math.inf, f"in band for only {dwell * 1e9:.0f} ns before the window ends (single crossing)"
        return out_ev, ""
    out_ev.acq_s, out_ev.t_acq, out_ev.dwell_s = t_acq - tc, t_acq, dwell
    return out_ev, ""


def extract_enable(vec: dict[str, np.ndarray], sched: dict, vdd_final: float, v_ref: float) -> Metrics:
    """Acquisition of every event of one run (see the module docstring).

    Method:
      1. Validate the run: required traces present, >= 100 samples, strictly
         increasing time, run reaches tstop; the steady reference v_ref must
         be finite and within REF_LEVEL_TOL_V of the ideal input VDD/2
         (invalid reference otherwise).
      2. INITIAL STATE: at t = 0 the rail, input, output and bias-mirror node
         must each be below INIT_MAX_V and both enable envelopes at ~0 -- the
         run must start unpowered and unbiased, never at the final powered
         operating point.
      3. FINAL STATE: the rail must reach vdd_final (FINAL_RAIL_TOL_V) over the
         last reference window, the bias envelope must be fully on at the end
         of each event window and fully off while a bias-disable is applied.
      4. Per event: last exit of |vout - v_ref| from the +-BAND_FRAC band
         (see `extract_event`); CENSORED when still outside at the end, when
         drifting, or when the in-band tail is a single crossing.
    """
    for need in REQUIRED:
        if need not in vec:
            return _bad(f"rawfile has no {need} vector")
    t = vec["time"]
    if t.size < 100 or np.any(np.diff(t) <= 0):
        return _bad("fewer than 100 samples or time not strictly increasing")
    if t[-1] < sched["tstop"] * 0.999:
        return _bad(f"transient ends at {t[-1]:.3g} s, before {sched['tstop']:g} s")
    if not math.isfinite(v_ref):
        return _bad("invalid steady reference (missing / non-finite)")
    if abs(v_ref - vdd_final / 2) > REF_LEVEL_TOL_V:
        return _bad(f"invalid steady reference {v_ref:.4f} V vs ideal input {vdd_final / 2:.4f} V (follower not following)")
    vd, vo, rs, rb = vec["v(vdd)"], vec["v(vout)"], vec["v(rs)"], vec["v(rb)"]
    init = {"vdd": float(vd[0]), "vinp": float(vec["v(vinp)"][0]), "vout": float(vo[0]),
            "ibias": float(vec["v(ibias)"][0]), "rs": float(rs[0]), "rb": float(rb[0])}
    if (max(abs(init[k]) for k in ("vdd", "vinp", "vout", "ibias")) > INIT_MAX_V
            or max(abs(init["rs"]), abs(init["rb"])) > INIT_MAX_RAIL_FRAC):
        return _bad("initial state is not unpowered/unbiased (vdd %.3f V, vout %.3f V, ibias %.3f V, "
                    "envelopes %.2f/%.2f at t=0): the run started at/near the powered operating point"
                    % (init["vdd"], init["vout"], init["ibias"], init["rs"], init["rb"]), init=init, vec=vec)
    ends = [e[3] for e in sched["events"]]
    rail_final = sr.window_mean(t, vd, sched["tstop"] - REF_WINDOW, sched["tstop"])
    if abs(rail_final - vdd_final) > FINAL_RAIL_TOL_V:
        return _bad(f"rail did not reach its final level ({rail_final:.3f} V vs {vdd_final:.3f} V)", init=init, vec=vec)
    for name, _t0, _tc, t1 in sched["events"]:
        if sr.window_mean(t, rb, t1 - REF_WINDOW, t1) < 1 - INIT_MAX_RAIL_FRAC:
            return _bad(f"{name}: bias envelope not fully on at the end of the window", init=init, vec=vec)
    if sched["t_off"] < sched["tstop"]:
        lo, hi = sched["t_off"] + sched["tr_b"], sched["t_on2"]
        if sr.window_mean(t, rb, lo, hi) > INIT_MAX_RAIL_FRAC:
            return _bad("bias was not fully removed during the disable interval", init=init, vec=vec)
    events = []
    for name, t0, tc, t1 in sched["events"]:
        e, why = extract_event(t, vo, vd, Event(name, t0, tc, t1), v_ref, sched["tstep"])
        if e is None:
            return _bad(why, init=init, vec=vec)
        events.append(e)
    return Metrics(valid=True, events=events, v_ref=v_ref, init=init, vec=vec)


# --------------------------------------------------------------------------
# Report handling
# --------------------------------------------------------------------------


def _meas(c: dict) -> dict:
    return {x["name"]: x.get("value") for x in c.get("measurements", [])}


def crosscheck(k: Key, vals: dict, m: Metrics, sched: dict) -> list[str]:
    t = m.vec["time"]
    t_end = sched["tstop"] - SAMPLE_BEFORE
    mine = {"vfinal_v": float(np.interp(t_end, t, m.vec["v(vout)"])),
            "vdd_final_v": float(np.interp(t_end, t, m.vec["v(vdd)"])),
            "vout_max_v": float(m.vec["v(vout)"].max())}
    bad = []
    for name, ref in mine.items():
        v = vals.get(name)
        if v is not None and abs(v - ref) > XCHK_LEVEL_V:
            bad.append(f"{g.fmt_key(k)}: ngspice {name} {v:.5f} V vs rawfile {ref:.5f} V")
    return bad


def _read_raw(c: dict, k: Key, problems: list[str]):
    diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
    raw = (c.get("artifacts") or {}).get("raw")
    if not raw or not Path(raw).is_file():
        problems.append(f"simulation failed for {g.fmt_key(k)}: {diag or 'no rawfile retained'}")
        return None
    try:
        return ssp.parse_ascii_real_raw(Path(raw).read_text())
    except ValueError as exc:
        problems.append(f"malformed data for {g.fmt_key(k)}: {exc}")
        return None


def _index(report: dict, want: list[Key], problems: list[str]) -> dict:
    seen: dict[Key, dict] = {}
    for c in report.get("corners", []):
        k = g.point_key(c)
        if k in seen:
            problems.append(f"duplicate result for {g.fmt_key(k)}")
        seen[k] = c
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"unexpected extra points: {sorted(extra)}")
    return seen


def analyse_reference(report: dict, want: list[Key]) -> tuple[dict, dict, list[str]]:
    """Powered steady reference per point: {key: v_ref}, artifacts, problems."""
    problems: list[str] = []
    seen = _index(report, want, problems)
    refs, arts = {}, {}
    for k in want:
        c = seen.get(k)
        if c is None:
            problems.append(f"missing reference result for {g.fmt_key(k)}")
            continue
        vals = _meas(c)
        v = vals.get("vref_v")
        if v is None or not math.isfinite(v):
            diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
            problems.append(f"reference failed for {g.fmt_key(k)}: {diag or 'no vref_v measurement'}")
            continue
        refs[k] = float(v)
        arts[k] = {"log": (c.get("artifacts") or {}).get("log"), "deck": (c.get("artifacts") or {}).get("deck"), "klt_meas": vals}
    return refs, arts, problems


def analyse_report(report: dict, want: list[Key], sched: dict, refs: dict) -> tuple[dict, dict, list[str]]:
    """Per-point metrics + artifacts from a transient klt report; failure strings."""
    problems: list[str] = []
    results: dict[Key, Metrics] = {}
    arts: dict[Key, dict] = {}
    seen = _index(report, want, problems)
    for k in want:
        c = seen.get(k)
        if c is None:
            problems.append(f"missing result for {g.fmt_key(k)}")
            continue
        vec = _read_raw(c, k, problems)
        if vec is None:
            continue
        vals = _meas(c)
        m = extract_enable(vec, sched, k[2], refs.get(k, float("nan")))
        if m.valid:
            problems += crosscheck(k, vals, m, sched)
        results[k] = m
        arts[k] = {"log": (c.get("artifacts") or {}).get("log"), "deck": (c.get("artifacts") or {}).get("deck"),
                   "klt_meas": vals}
    return results, arts, problems


# --------------------------------------------------------------------------
# Committed per-point data (re-derivable)
# --------------------------------------------------------------------------


def _columns(vec: dict) -> np.ndarray:
    n = vec["time"].size
    return np.column_stack([vec.get(name, np.full(n, np.nan)) for name in _RAW_ORDER])


def save_point_data(m: Metrics, path: Path) -> None:
    """Every rawfile sample of the point (gzip text): time, supply, envelopes,
    bias node, input, output and the internal nodes (n1 mirror, n2 second-stage
    gate, nz Miller node, tail). Enough for `rederive()` to reproduce the
    extraction from the committed file alone."""
    if not m.vec:
        with gzip.open(path, "wt") as fh:
            fh.write(f"# INVALID: {m.reason}\n")
        return
    head = ([] if m.valid else [f"INVALID: {m.reason}"]) + [DAT_HEADER]
    with gzip.open(path, "wt") as fh:
        # 10 significant digits on every column: at %.6e the voltage quantisation (~1 uV) moved a
        # slowly-settling (late, long-window) acquisition instant by > 0.01 ns on re-derivation.
        np.savetxt(fh, _columns(m.vec), header="\n".join(head), fmt="%.9e")


def rederive(path: Path, sched: dict, vdd_final: float, v_ref: float) -> Metrics:
    """Re-run the extraction on a committed per-point data file."""
    with gzip.open(path, "rt") as fh:
        text = fh.read()
    if not any(ln[1:].strip() == DAT_HEADER for ln in text.splitlines() if ln.startswith("#")):
        raise ValueError(f"{path}: no `{DAT_HEADER}` column header")
    d = np.loadtxt(text.splitlines(), ndmin=2)
    if d.shape[1] != len(DAT_COLS):
        raise ValueError(f"{path}: {d.shape[1]} columns, expected {len(DAT_COLS)}")
    vec = {name: d[:, i] for i, name in enumerate(_RAW_ORDER)}
    return extract_enable(vec, sched, vdd_final, v_ref)


def same_result(a: Metrics, b: Metrics) -> bool:
    """True when two extractions agree (validity, per-event acquisition to
    0.01 ns, censored status, peaks to 1e-3 mV)."""
    if a.valid != b.valid:
        return False
    if not a.valid:
        return True
    for ea, eb in zip(a.events, b.events):
        if ea.censored != eb.censored or (not ea.censored and abs(ea.acq_s - eb.acq_s) > 1e-11):
            return False
        for f in ("over_mv", "under_mv", "rail_excess_mv"):
            if abs(getattr(ea, f) - getattr(eb, f)) > 1e-3:
                return False
    return True


def persist_points(sub: Path, results: dict, arts: dict, report: dict) -> None:
    """One case's committed evidence: per-point logs, decks and `.dat.gz`
    traces, plus the sanitised klt report. Used for the primary grids and for
    the long-window follow-ups alike (a follow-up keeps its evidence too)."""
    for k, a in arts.items():
        stem = g.point_stem(k)
        if a["log"]:
            shutil.copyfile(a["log"], sub / f"{stem}.log")
        if a["deck"]:
            shutil.copyfile(a["deck"], sub / f"{stem}.cir")
        save_point_data(results[k], sub / f"{stem}.dat.gz")
    (sub / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))


def check_rederive(sub: Path, sched: dict, results: dict, refs: dict, label: str,
                   rbad: list[str]) -> tuple[int, int]:
    """Re-extract every persisted point of `sub` from its committed `.dat.gz`
    trace and compare with the in-memory metrics; mismatch / unreadable points
    are appended to `rbad` (prefixed `label`). Returns (n_ok, n_all)."""
    n_ok = n_all = 0
    for k, m in results.items():
        n_all += 1
        try:
            m2 = rederive(sub / f"{g.point_stem(k)}.dat.gz", sched, k[2], refs[k])
        except (ValueError, OSError) as exc:
            rbad.append(f"{label} {g.fmt_key(k)}: {exc}")
            continue
        if same_result(m, m2):
            n_ok += 1
        else:
            rbad.append(f"{label} {g.fmt_key(k)}")
    return n_ok, n_all


def snapshot_case_lines(label: str, req: dict, sched: dict) -> list[str]:
    """The netlist-snapshot comment block recording one grid request and its
    schedule (follow-up requests are recorded the same way as the primaries)."""
    return [f"* ---- case {label} ----",
            *("* " + ln for ln in
              json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
            "* schedule .param values: " + json.dumps(sched["params"])]


# --------------------------------------------------------------------------
# Summary (evidence; no verdict)
# --------------------------------------------------------------------------


def fmt_acq(v: float) -> str:
    """Acquisition time in ns; `not acquired` for inf."""
    if isinstance(v, float) and math.isinf(v):
        return "not acquired"
    if not (isinstance(v, float) and math.isfinite(v)):
        return "n/a"
    return format(v * 1e9, ".1f")


@dataclass
class Summary:
    n_total: int
    n_invalid: int
    worst: dict  # event -> (seconds or inf, key)
    censored: dict  # event -> [keys]
    worst_over: tuple
    worst_under: tuple


def summarise(results: dict[Key, Metrics]) -> Summary:
    valid = {k: m for k, m in results.items() if m.valid}
    names = sorted({e.name for m in valid.values() for e in m.events}, key=lambda n: ["enable", "re-enable"].index(n))
    worst, cens = {}, {}
    for n in names:
        rows = [(next(e for e in m.events if e.name == n), k) for k, m in valid.items()]
        worst[n] = max(((e.acq_s, k) for e, k in rows), default=(float("nan"), None), key=lambda x: x[0])
        cens[n] = [k for e, k in rows if e.censored]
    allev = [(e, k) for k, m in valid.items() for e in m.events]
    wo = max(((e.over_mv, k) for e, k in allev), default=(float("nan"), None), key=lambda x: x[0])
    wu = max(((e.under_mv, k) for e, k in allev), default=(float("nan"), None), key=lambda x: x[0])
    return Summary(len(results), len(results) - len(valid), worst, cens, wo, wu)


# --------------------------------------------------------------------------
# Single-unit controls (local, nominal point)
# --------------------------------------------------------------------------


@dataclass
class ControlRun:
    name: str
    description: str
    metrics: Metrics | None = None
    error: str = ""
    files: dict = field(default_factory=dict)


def run_unit(name: str, pdk: Pdk, work: Path, sched: dict, overrides: dict, v_ref: float | None = None):
    """The nominal point as its own single-unit `klt sim`, LOCAL (allowed by the
    host rules; no grid ever takes this path). `v_ref` None -> derived from a
    nominal local DC reference solve (also single-unit local)."""
    proc, temp, vdd = NOMINAL
    if v_ref is None:
        v_ref = nominal_reference(pdk, work, sched)
    wd = work / name
    tb = materialise(wd, pdk, sched, **overrides)
    req = make_request(tb, pdk, sched, [proc], [temp], [vdd])
    rep = run_klt(req, wd / "out", "local", wd)
    res, arts, problems = analyse_report(rep, [NOMINAL], sched, {NOMINAL: v_ref})
    if NOMINAL not in res:
        raise KltError("; ".join(problems))
    return res[NOMINAL], arts[NOMINAL], problems


def nominal_reference(pdk: Pdk, work: Path, sched: dict) -> float:
    wd = work / "nominal-reference"
    tb = materialise(wd, pdk, sched, dc_reference=True)
    proc, temp, vdd = NOMINAL
    rep = run_klt(make_reference_request(tb, pdk, [proc], [temp], [vdd]), wd / "out", "local", wd)
    refs, _, problems = analyse_reference(rep, [NOMINAL])
    if NOMINAL not in refs:
        raise KltError("; ".join(problems))
    return refs[NOMINAL]


CONTROL_EXTRA = (
    ("powered-start", "envelopes fixed ON (the run starts AT the powered operating point; the extraction must "
                      "reject it as an invalid initial state, never report an acquisition)", {"powered_start": True}),
)


def run_controls(pdk: Pdk, work: Path) -> list[ControlRun]:
    sched = mc.schedule(*mc.CONTROL_CASE)
    out = []
    try:
        v_ref = nominal_reference(pdk, work, sched)
    except (KltError, KeyError, RuntimeError) as exc:
        return [ControlRun(n, d, error=f"reference: {str(exc)[:200]}") for n, d, _ in (*mc.CONTROLS, *CONTROL_EXTRA)]
    for name, desc, ov in (*mc.CONTROLS, *CONTROL_EXTRA):
        cr = ControlRun(name, desc)
        try:
            m, a, _ = run_unit(name, pdk, work, sched, ov, v_ref)
            cr.metrics, cr.files = m, {"log": a["log"], "deck": a["deck"]}
        except (KltError, KeyError, RuntimeError) as exc:
            cr.error = str(exc)[:300]
        out.append(cr)
    return out


def control_failures(ctrls: list[ControlRun]) -> list[str]:
    """Reasons the controls do NOT behave as the method requires."""
    bad = []
    by = {c.name: c for c in ctrls}
    nom, big, dead, pw = by.get("nominal"), by.get("cl-x5"), by.get("ibias-zero"), by.get("powered-start")
    for c in (nom, big):
        if c is None:
            continue
        if c.metrics is None:
            bad.append(f"{c.name}: did not simulate ({c.error})")
        elif not c.metrics.valid:
            bad.append(f"{c.name}: invalid ({c.metrics.reason})")
    if nom and big and nom.metrics and big.metrics and nom.metrics.valid and big.metrics.valid:
        tn, tb = nom.metrics.events[-1].t_acq, big.metrics.events[-1].t_acq
        if not (math.isfinite(tn) and math.isfinite(tb) and tb > tn + CL_MARGIN_S):
            bad.append(f"cl-x5: acquisition instant {tb * 1e9 if math.isfinite(tb) else tb:.1f} ns is not clearly after "
                       f"nominal {tn * 1e9 if math.isfinite(tn) else tn:.1f} ns -- the extraction does not see the heavier load")
    if dead is None or dead.metrics is None:
        bad.append(f"ibias-zero: did not simulate ({dead.error if dead else 'missing'})")
    elif not dead.metrics.valid:
        bad.append(f"ibias-zero: invalid ({dead.metrics.reason}) -- the dead-bias control must be a valid, censored run")
    elif any(math.isfinite(e.acq_s) for e in dead.metrics.events):
        bad.append("ibias-zero: a dead amplifier produced an acquired output -- the extraction cannot be trusted")
    if pw is None or pw.metrics is None:
        bad.append(f"powered-start: did not simulate ({pw.error if pw else 'missing'})")
    elif pw.metrics.valid:
        bad.append("powered-start: a run initialised at the powered operating point was accepted as valid")
    return bad


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------


def build_plots(case_results: dict, plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for (scenario, ramp), (sched, results) in case_results.items():
        m = results.get(NOMINAL)
        if m is None or not m.vec:
            continue
        fig, ax = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
        t = m.vec["time"] * 1e6
        ax[0].plot(t, m.vec["v(vdd)"], label="vdd")
        ax[0].plot(t, m.vec["v(vout)"], label="vout")
        ax[0].plot(t, m.vec["v(vinp)"], label="vinp", linestyle=":")
        if m.valid:
            ax[0].axhline(m.v_ref, color="grey", linestyle="--", linewidth=0.7, label="v_ref (DC)")
        ax[0].set_ylabel("V")
        ax[0].legend(ncol=4, fontsize=8)
        ax[0].set_title(f"{scenario} / {ramp} ramp: nominal point (typical, 27 C, 3.30 V)")
        ax[1].plot(t, m.vec["v(rb)"], label="bias envelope")
        for nm in ("v(ibias)", "v(xdut.n1)", "v(xdut.n2)", "v(xdut.nz)"):
            if nm in m.vec:
                ax[1].plot(t, m.vec[nm], label=nm, linewidth=0.9)
        ax[1].set_xlabel("time (us)")
        ax[1].set_ylabel("V")
        ax[1].legend(ncol=5, fontsize=8)
        for a in ax:
            a.grid(True, alpha=0.3)
        fig.tight_layout()
        fn = f"nominal_{scenario}_{ramp}.png"
        fig.savefig(plot_dir / fn, dpi=100)
        plt.close(fig)
        names.append(fn)
        fig, ax1 = plt.subplots(figsize=(9, 4))
        for k, mm in results.items():
            if mm.vec:
                ax1.plot(mm.vec["time"] * 1e6, mm.vec["v(vout)"], linewidth=1.2 if k == NOMINAL else 0.4,
                         alpha=1.0 if k == NOMINAL else 0.5)
        ax1.set_xlabel("time (us)")
        ax1.set_ylabel("vout (V)")
        ax1.set_title(f"{scenario} / {ramp} ramp: vout, all {len(results)} PVT points (nominal bold)")
        ax1.grid(True, alpha=0.3)
        fig.tight_layout()
        fn = f"vout_all45_{scenario}_{ramp}.png"
        fig.savefig(plot_dir / fn, dpi=100)
        plt.close(fig)
        names.append(fn)
    return names


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def _fmt(v: float, spec: str = ".2f") -> str:
    return "n/a" if not (isinstance(v, float) and math.isfinite(v)) else format(v, spec)


def _acq_ns(v: float) -> str:
    return fmt_acq(v) + (" ns" if isinstance(v, float) and math.isfinite(v) else "")


def build_record(*, record, stamp, pdk, ngspice, klt_ver, backend_descs, case_results, long_results, refs, ctrls,
                 ctrl_bad, plots, dut_sha, rederived, ref_remote) -> str:
    L: list[str] = []
    add = L.append
    tau_max = 1 / (2 * math.pi * GBW_MIN_HZ)
    add(f"# supply / external-bias enable acquisition study -- record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    add("- **Issue**: #191 -- supply and external-bias enable acquisition across the PVT grid.")
    add("- **Status**: measured schematic observations, **no pass or fail verdict, no startup bound**. "
        "`spec/target-spec.md` has **no startup row** and this record judges none and adds none; a bound would need a "
        "`spec/` decision record. Nothing in `spec/` is touched.")
    add("- **What this is NOT**: not a consumer startup guarantee, not a supply/bias slew requirement, not "
        "specification compliance. The ramp durations, gap/hold times, windows and the +-1 % band are "
        "**diagnostic experiment conditions** chosen to expose the block's behaviour. A bias-source generator and a "
        "consumer-loop startup are outside this study (externally supplied ideal bias stays the block boundary).")
    add("- **Claim (evidence only, no verdict)**: the committed sized schematic (`design/opamp_two_stage.sch`, export "
        "`design/netlist/opamp_two_stage.spice`, instantiated as `opamp_two_stage`), wired as a unity-gain follower, "
        "starts from a true unpowered / unbiased state (rail 0 V, bias 0 A, input 0 V, output 0 V) and acquires its "
        "powered follower level when the rail and the external 10 uA bias are enabled in three sequences, each with a "
        "fast / slow diagnostic ramp, at every one of the 45 PVT points.")
    # Summary
    for (scenario, ramp), (sched, results) in case_results.items():
        s = summarise(results)
        parts = []
        for n, (v, k) in s.worst.items():
            parts.append(f"{n}: worst **{_acq_ns(v)}** at {g.fmt_key(k) if k else 'n/a'}"
                         + (f", NOT ACQUIRED at {len(s.censored[n])} point(s)" if s.censored[n] else ", all acquired"))
        add(f"- **{scenario} / {ramp}**: {s.n_total - s.n_invalid}/{s.n_total} points measured validly "
            f"({s.n_invalid} invalid); " + "; ".join(parts) + ".")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (via {pdk.source})")
    add(f"- **Tools**: ngspice (local: {ngspice}), klt `{klt_ver}`")
    add("- **Execution**: each (scenario, ramp) is ONE 45-point `klt sim` corner-matrix request, plus one 45-point DC "
        "reference request:")
    for label, d in backend_descs.items():
        add(f"  - {label}: {d}")
    n_ok, n_all, bad = rederived
    add(f"  - Re-derivation from the committed `corners/{record}/*/*.dat.gz` files (re-read from disk, extraction re-run): "
        f"{n_ok}/{n_all} points reproduce validity, every acquisition time and the censored status"
        + (" exactly." if not bad else "; **MISMATCH** at " + "; ".join(bad[:6]) + " (driver exits non-zero)."))
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body verbatim; "
        f"normalised sha256 `{dut_sha}`), snapshotted in full in `netlist-snapshots/{record}.spice`")
    for ln in mc.fingerprint_lines(TESTBENCH.read_text()):
        add(ln)
    add("- **Corner matrix run**:")
    add(f"  - Process (MOS): {', '.join(CORNERS)}")
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add("  - FINAL supply VDD: " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V)
        + " (final VCM = VDD/2: 1.485 / 1.65 / 1.815 V; the input tracks the ramped rail at VDD/2 throughout)")
    add("  - final ibias = 10 uA; CL = 2 pF on `vout`, no other load")
    add("- **Passive-section policy**: every MOS corner is paired with the SAME `res_typical` and `mimcap_typical` "
        "sections; no independent RZ / CC corner coverage is claimed.")
    add("- **Statistical convention**: N/A -- deterministic process/temperature/supply corners; no mismatch or Monte Carlo.")
    add("")
    add("## Conditions")
    add("")
    add("- **Initial state** (`tb_enable.spice`): both 0..1 envelope sources start at 0, so at t = 0 the rail, the input "
        "(`v(vinp) = VCM_final * envelope`), the output and the bias are all at 0 (`.tran` without `uic`; the t = 0 "
        "solution is the trivial unpowered state). The extraction REJECTS a point whose first samples are not "
        f"unpowered/unbiased (|v| <= {INIT_MAX_V:g} V on vdd, vinp, vout and the bias-mirror node, envelopes <= "
        f"{INIT_MAX_RAIL_FRAC:g}); the `powered-start` control below proves it does.")
    add("- **Final powered levels**: rail = the corner supply (2.97 / 3.30 / 3.63 V), input = VDD/2, bias = 10 uA, "
        "per point as listed in the tables; the traces end at those levels and the extraction requires the rail to "
        f"reach the final value within {FINAL_RAIL_TOL_V * 1e3:g} mV and the bias envelope to be fully on.")
    add("- **Steady reference v_ref**: a SEPARATE DC solution of the same bench with both enable envelopes fixed on "
        "(`reference/` below), one per point -- never read off the run being judged. It must be within "
        f"{REF_LEVEL_TOL_V * 1e3:g} mV of the ideal input VDD/2, else the point is INVALID (invalid reference).")
    add("- **Diagnostic schedules** (every value below is an experiment condition, not a requirement):")
    add("")
    add("| Scenario | Ramp | rail ramp (start, duration) | bias enable (start, duration) | bias disable / re-enable starts | "
        "events (ramps complete -> window end) | tstop | tstep |")
    add("|---|---|---|---|---|---|---|---|")
    for (scenario, ramp), (sched, _) in case_results.items():
        p = sched["params"]
        off = f"{p['t_off'] * 1e6:g} us / {p['t_on2'] * 1e6:g} us" if p["t_off"] < sched["tstop"] else "none"
        evs = "; ".join(f"{n}: {tc * 1e6:g} -> {t1 * 1e6:g} us" for n, _t0, tc, t1 in sched["events"])
        add(f"| {scenario} | {ramp} | {p['t_vdd'] * 1e6:g} us, {p['tr_s'] * 1e9:g} ns | {p['t_ib'] * 1e6:g} us, "
            f"{p['tr_b'] * 1e9:g} ns | {off} | {evs} | {sched['tstop'] * 1e6:g} us | {sched['tstep'] * 1e9:g} ns |")
    add("")
    for sc, d in mc.SCENARIO_DESC.items():
        add(f"  - `{sc}`: {d}")
    add(f"- **Acquisition**: time from the instant an event's LAST ramp completes to the last exit of |vout - v_ref| "
        f"from a +-{BAND_FRAC:.0%} diagnostic band about v_ref (interpolated; negative = already in band before the "
        "ramps finished). It is claimed only when the output then STAYS in the band to the end of the window, drifts by "
        f"<= {DRIFT_FRAC:g} bands across the final {REF_WINDOW * 1e9:g} ns, and the in-band tail lasts >= "
        f"{MIN_DWELL_FRAC:.0%} of the window -- **no recovery is inferred from a single tolerance crossing**. Otherwise "
        "the event is CENSORED and shown as **not acquired** (ranked worst, never given a finite time).")
    add(f"- **Window vs small-signal GBW**: the ratified GBW >= {GBW_MIN_HZ / 1e6:g} MHz gives tau <= "
        f"{tau_max * 1e9:.1f} ns; each observation window is {mc.WINDOW_T / tau_max:.0f} tau.")
    add(f"- **Unresolved points** are re-run over a {mc.LONG_WINDOW_FACTOR}x longer window before any persistent-failure "
        "statement (section below).")
    add("")
    add("## Results by scenario (evidence; no bound applies)")
    add("")
    for (scenario, ramp), (sched, results) in case_results.items():
        s = summarise(results)
        add(f"### {scenario} / {ramp}")
        add("")
        names = [e[0] for e in sched["events"]]
        add("Acquisition in ns after the event's ramps complete; peaks in mV about v_ref after the ramps complete; "
            "`rail+` is the peak excess of vout over the instantaneous rail (0 when vout stays at or below it).")
        add("")
        hdr = "| Process | T (C) | VDD (V) | v_ref (V) | " + " | ".join(
            f"{n} acq (ns) | {n} over/under (mV) | {n} rail+ (mV)" for n in names) + " | note |"
        add(hdr)
        add("|" + "---|" * (hdr.count("|") - 1))
        keyorder = lambda k: (CORNERS.index(k[0]), k[2], k[1])  # noqa: E731
        for k in sorted(results, key=keyorder):
            m = results[k]
            if not m.valid:
                add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | {_fmt(refs.get(k, float('nan')), '.4f')} | "
                    + " | ".join("n/a | n/a | n/a" for _ in names) + f" | INVALID: {m.reason} |")
                continue
            cells = []
            notes = []
            for e in m.events:
                cells += [fmt_acq(e.acq_s), f"{e.over_mv:.1f} / {e.under_mv:.1f}", f"{e.rail_excess_mv:.1f}"]
                if e.censored:
                    notes.append(f"{e.name}: {e.why}")
            add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | {m.v_ref:.4f} | " + " | ".join(cells) + f" | {'; '.join(notes)} |")
        add("")
        for n in names:
            v, k = s.worst[n]
            add(f"- {n}: worst {_acq_ns(v)} at {g.fmt_key(k) if k else 'n/a'}; "
                + (f"**NOT ACQUIRED at {len(s.censored[n])} point(s)**: " + ", ".join(g.fmt_key(x) for x in s.censored[n][:8])
                   if s.censored[n] else "every valid point acquired inside its window"))
        add(f"- worst post-ramp overshoot {_fmt(s.worst_over[0])} mV at {g.fmt_key(s.worst_over[1]) if s.worst_over[1] else 'n/a'}; "
            f"worst undershoot {_fmt(s.worst_under[0])} mV at {g.fmt_key(s.worst_under[1]) if s.worst_under[1] else 'n/a'}.")
        add("")
    add("## Long-window follow-up of unresolved points")
    add("")
    if not long_results:
        add("No event was censored in any scenario: no long-window follow-up was needed.")
    else:
        add(f"Every censored event was re-run over a {mc.LONG_WINDOW_FACTOR}x longer observation window; an event that "
            "acquires only there is reported as acquired LATE (the primary table above keeps it censored).")
        add("")
        add("| Scenario | Ramp | Process | T (C) | VDD (V) | event | long-window result |")
        add("|---|---|---|---|---|---|---|")
        for (scenario, ramp), (lsched, lres) in long_results.items():
            for k, m in lres.items():
                if not m.valid:
                    add(f"| {scenario} | {ramp} | `{k[0]}` | {k[1]:g} | {k[2]:.2f} | all | INVALID: {m.reason} |")
                    continue
                for e in m.events:
                    add(f"| {scenario} | {ramp} | `{k[0]}` | {k[1]:g} | {k[2]:.2f} | {e.name} | "
                        + (f"still not acquired after {(e.t_end - e.t_complete) * 1e6:g} us ({e.why})" if e.censored
                           else f"acquired late, {_acq_ns(e.acq_s)} after the ramps complete") + " |")
        add("")
        add("Committed follow-up evidence (decks, logs, `.dat.gz` traces, sanitised klt reports, and the follow-up "
            "requests in the netlist snapshot): "
            + ", ".join(f"`sim/enable-acquisition/corners/{record}/long_{scenario}_{ramp}/`"
                        for (scenario, ramp) in long_results)
            + ". The re-derivation count above includes these files.")
    add("")
    add("## Controls (single local units at the nominal point, simultaneous / fast; separate from the grid evidence)")
    add("")
    add("| Control | Condition | outcome |")
    add("|---|---|---|")
    for c in ctrls:
        m = c.metrics
        if m is None:
            out = f"not simulated: {c.error[:100]}"
        elif not m.valid:
            out = f"INVALID: {m.reason[:140]}"
        else:
            e = m.events[-1]
            out = f"acq {fmt_acq(e.acq_s)}{' ns' if math.isfinite(e.acq_s) else ''}, last band exit at {_fmt(e.t_acq * 1e9, '.1f')} ns"
        add(f"| `{c.name}` | {c.description} | {out} |")
    add("")
    add("Required behaviour: nominal and cl-x5 measure validly and cl-x5 (CL = 10 pF) acquires later than nominal "
        f"by > {CL_MARGIN_S * 1e9:g} ns (the measurement responds to circuit loading); ibias-zero (dead bias) never "
        "yields an acquired output; powered-start is rejected as an invalid initial state.")
    if ctrl_bad:
        add("")
        add("**CONTROL PROBLEMS** (driver exits non-zero):")
        for x in ctrl_bad:
            add(f"- {x}")
    add("")
    L.extend(mc.inputs_section(TESTBENCH.read_text()))
    add("## Plots")
    add("")
    for p in plots:
        add(f"- `sim/enable-acquisition/records/{record}-plots/{p}`")
    add("")
    add("## Links")
    add("")
    add("- Testbench: `sim/enable-acquisition/testbench/tb_enable.spice`")
    add("- Run script: `sim/enable-acquisition/run_enable_acquisition.py`; extraction/guard tests: "
        "`sim/enable-acquisition/test_enable_acquisition.py`")
    add(f"- Netlist snapshot (DUT + testbench + conditions): `sim/enable-acquisition/netlist-snapshots/{record}.spice`")
    add(f"- Per-point logs, decks, traces, reference, sanitised klt reports and controls: `sim/enable-acquisition/corners/{record}/`")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #191)")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def describe_backend(report: dict) -> str:
    remote = remote_of(report)
    return (
        f"`klt sim` backend `{remote.get('provider', 'local')}`"
        + (f", job id `{remote.get('job_id')}`, {('Spot ' if remote.get('spot') else 'on-demand ')}{remote.get('instance_type')}, "
           f"runner klt `{remote.get('runner_klt_version')}` vs client `{remote.get('client_klt_version')}` "
           f"(compatibility `{remote.get('runner_compatibility')}`)" if remote else "")
    )


def print_point(m: Metrics) -> None:
    print(f"  v_ref {m.v_ref:.4f} V, initial vdd/vout/ibias {m.init['vdd']:.3f}/{m.init['vout']:.3f}/{m.init['ibias']:.3f} V")
    for e in m.events:
        print(f"  {e.name}: acquisition {fmt_acq(e.acq_s)}{' ns' if math.isfinite(e.acq_s) else ''}"
              f"{' (' + e.why + ')' if e.censored else ''}, over {e.over_mv:.1f} / under {e.under_mv:.1f} mV, "
              f"rail+ {e.rail_excess_mv:.1f} mV")


def smoke(pdk: Pdk) -> int:
    """Record-free: the nominal point of EACH scenario (fast ramp), local, one
    steady-reference solve. Writes nothing; judges nothing."""
    print(f"smoke test: {NOMINAL} only, local, one fast-ramp run per scenario, PDK={pdk.path}")
    rc = 0
    with tempfile.TemporaryDirectory(prefix="enable-smoke-") as scratch:
        for scenario in mc.SCENARIOS:
            sched = mc.schedule(scenario, "fast")
            try:
                m, _, problems = run_unit(f"smoke-{scenario}", pdk, Path(scratch), sched, {})
            except (KltError, RuntimeError) as exc:
                print(f"SMOKE TEST FAILED ({scenario}): {exc}")
                return 1
            if not m.valid or problems:
                print(f"SMOKE TEST FAILED ({scenario}): {m.reason or problems}")
                rc = 1
                continue
            print(f"{scenario}:")
            print_point(m)
    if rc == 0:
        print("smoke test OK (measurements valid; this experiment records evidence, it judges nothing)")
    return rc


def pilot(pdk: Pdk) -> int:
    """Timing / runtime pilot: one local nominal run per case, wall time and
    sample count; writes nothing."""
    with tempfile.TemporaryDirectory(prefix="enable-pilot-") as scratch:
        for scenario, ramp in mc.cases():
            sched = mc.schedule(scenario, ramp)
            t0 = time.time()
            try:
                m, _, problems = run_unit(f"pilot-{scenario}-{ramp}", pdk, Path(scratch), sched, {})
            except (KltError, RuntimeError) as exc:
                print(f"{scenario}/{ramp}: FAILED {str(exc)[:200]}")
                continue
            n = m.vec["time"].size if m.vec else 0
            print(f"{scenario}/{ramp}: tstop {sched['tstop'] * 1e6:g} us, tstep {sched['tstep'] * 1e9:g} ns, rows {n}, "
                  f"wall (incl. reference solve) {time.time() - t0:.1f} s, valid {m.valid} {m.reason}")
            if m.valid:
                print_point(m)
    return 0


def _run_grid(req: dict, label: str, work: Path, args, retries_ok: bool = True) -> dict:
    return run_klt_retrying(req, work / "out", args.backend, work, retries=args.batch_submit_retries,
                            wait_s=args.batch_retry_wait_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one nominal point per scenario, local, no record")
    ap.add_argument("--pilot", action="store_true", help="timing pilot: one local nominal run per case, no record")
    ap.add_argument("--backend", help="klt execution backend for the grids (default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet capacity (never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    if args.pilot:
        return pilot(pdk)

    want = g.expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record)
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: {len(want)} points x {len(mc.cases())} cases + reference, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    def fail(msg: str) -> int:
        print(f"ERROR: {msg}", file=sys.stderr)
        print("NO RECORD WRITTEN.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="enable-") as scratch:
        work = Path(scratch)
        backend_descs: dict[str, str] = {}
        # 1. the powered steady reference (separate DC solution)
        sched0 = mc.schedule(*mc.cases()[0])
        tb = materialise(work / "reference", pdk, sched0, dc_reference=True)
        req = make_reference_request(tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
        req["batch"] = batch_block(args)
        try:
            ref_report = _run_grid(req, "reference", work / "reference", args)
        except KltError as exc:
            return fail(f"the 45-point reference request could not be run: {str(exc)[-600:]}")
        refs, ref_arts, problems = analyse_reference(ref_report, want)
        if problems:
            return fail("the reference grid did not complete cleanly: " + "; ".join(problems[:8]))
        backend_descs["reference (DC)"] = describe_backend(ref_report)
        print(f"  reference: 45 points ok ({backend_descs['reference (DC)']})")

        # 2. the transient cases
        case_results: dict = {}
        case_arts: dict = {}
        case_reports: dict = {}
        for scenario, ramp in mc.cases():
            sched = mc.schedule(scenario, ramp)
            tbp = materialise(work / f"{scenario}-{ramp}", pdk, sched)
            req = make_request(tbp, pdk, sched, CORNERS, TEMPS_C, SUPPLIES_V)
            req["batch"] = batch_block(args)
            try:
                rep = _run_grid(req, f"{scenario}/{ramp}", work / f"{scenario}-{ramp}", args)
            except KltError as exc:
                return fail(f"the {scenario}/{ramp} 45-point klt request could not be run: {str(exc)[-600:]}")
            results, arts, problems = analyse_report(rep, want, sched, refs)
            if problems:
                return fail(f"the {scenario}/{ramp} grid did not complete cleanly: " + "; ".join(problems[:8]))
            case_results[(scenario, ramp)] = (sched, results)
            case_arts[(scenario, ramp)] = arts
            case_reports[(scenario, ramp)] = (req, rep)
            backend_descs[f"{scenario} / {ramp}"] = describe_backend(rep)
            print(f"  {scenario}/{ramp}: 45 points ok ({backend_descs[f'{scenario} / {ramp}']})")

        # 3. long-window follow-up of any censored event (batch, one superset request per case)
        long_results: dict = {}
        long_arts: dict = {}
        long_reports: dict = {}
        for (scenario, ramp), (sched, results) in case_results.items():
            unresolved = [k for k, m in results.items() if m.censored]
            if not unresolved:
                continue
            lsched = mc.schedule(scenario, ramp, mc.WINDOW_T * mc.LONG_WINDOW_FACTOR)
            procs = [c for c in CORNERS if any(k[0] == c for k in unresolved)]
            temps = [x for x in TEMPS_C if any(k[1] == x for k in unresolved)]
            sups = [x for x in SUPPLIES_V if any(k[2] == x for k in unresolved)]
            tbl = materialise(work / f"long-{scenario}-{ramp}", pdk, lsched)
            req = make_request(tbl, pdk, lsched, procs, temps, sups)
            req["batch"] = batch_block(args)
            try:
                rep = _run_grid(req, "long", work / f"long-{scenario}-{ramp}", args)
            except KltError as exc:
                return fail(f"the long-window follow-up {scenario}/{ramp} could not be run: {str(exc)[-600:]}")
            wantl = g.expected_keys(procs, temps, sups)
            lres, larts, problems = analyse_report(rep, wantl, lsched, refs)
            if problems:
                return fail(f"the long-window follow-up {scenario}/{ramp} did not complete cleanly: " + "; ".join(problems[:8]))
            long_results[(scenario, ramp)] = (lsched, {k: lres[k] for k in unresolved})
            long_arts[(scenario, ramp)] = {k: larts[k] for k in unresolved}
            long_reports[(scenario, ramp)] = (req, rep)
            backend_descs[f"long-window {scenario} / {ramp}"] = describe_backend(rep)
            print(f"  long-window {scenario}/{ramp}: {len(unresolved)} unresolved point(s) re-run")

        ctrls = run_controls(pdk, work)
        ctrl_bad = control_failures(ctrls)

        cdir = paths["corners"]
        cdir.mkdir(parents=True, exist_ok=False)
        rdir = cdir / "reference"
        rdir.mkdir()
        for k, a in ref_arts.items():
            stem = g.point_stem(k)
            for kind, ext in (("log", "log"), ("deck", "cir")):
                if a[kind]:
                    shutil.copyfile(a[kind], rdir / f"{stem}.{ext}")
        (rdir / "klt-report.json").write_text(json.dumps(sanitise_report(ref_report), indent=1))
        (rdir / "vref.json").write_text(json.dumps({g.fmt_key(k): v for k, v in sorted(refs.items())}, indent=1))
        n_ok, n_all, rbad = 0, 0, []
        for (scenario, ramp), (sched, results) in case_results.items():
            sub = cdir / f"{scenario}_{ramp}"
            sub.mkdir()
            _req, rep = case_reports[(scenario, ramp)]
            persist_points(sub, results, case_arts[(scenario, ramp)], rep)
            a, b = check_rederive(sub, sched, results, refs, f"{scenario}/{ramp}", rbad)
            n_ok += a
            n_all += b
        for (scenario, ramp), (lsched, lres) in long_results.items():
            lsub = cdir / f"long_{scenario}_{ramp}"
            lsub.mkdir()
            _lreq, lrep = long_reports[(scenario, ramp)]
            persist_points(lsub, lres, long_arts[(scenario, ramp)], lrep)
            a, b = check_rederive(lsub, lsched, lres, refs, f"long {scenario}/{ramp}", rbad)
            n_ok += a
            n_all += b
        ccdir = cdir / "controls"
        ccdir.mkdir()
        for c in ctrls:
            for kind, src in c.files.items():
                if src and Path(src).is_file():
                    shutil.copyfile(src, ccdir / f"{c.name}.{'log' if kind == 'log' else 'cir'}")

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        lines = [f"* netlist snapshot for record {record} (issue #191)",
                 "* Reproduces the measured design: DUT contents, testbench, conditions.",
                 "* ---- conditions: klt sim requests (one per case; netlist path elided) ----"]
        for (scenario, ramp), (req, _rep) in case_reports.items():
            lines += snapshot_case_lines(f"{scenario}/{ramp}", req, mc.schedule(scenario, ramp))
        for (scenario, ramp), (lreq, _lrep) in long_reports.items():
            lines += snapshot_case_lines(f"long-window {scenario}/{ramp}", lreq, long_results[(scenario, ramp)][0])
        lines += ["", "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
                  dut_text, "* ---- testbench: sim/enable-acquisition/testbench/tb_enable.spice (verbatim; schedule .param lines are "
                  "substituted per case as listed above) ----", TESTBENCH.read_text()]
        a0 = case_arts[mc.cases()[0]].get(NOMINAL)
        if a0 and a0["deck"]:
            lines += ["* ---- klt-generated deck of the nominal point, simultaneous / fast ----"]
            lines += ["* | " + ln for ln in Path(a0["deck"]).read_text().splitlines()] + [""]
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join(lines))

        plots = build_plots(case_results, paths["plots"])
        md = build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_ver=kver, backend_descs=backend_descs,
            case_results=case_results, long_results=long_results, refs=refs, ctrls=ctrls, ctrl_bad=ctrl_bad,
            plots=plots, dut_sha=dut_sha, rederived=(n_ok, n_all, rbad), ref_remote=remote_of(ref_report),
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    rc = 0
    for (scenario, ramp), (sched, results) in case_results.items():
        s = summarise(results)
        for n, (v, k) in s.worst.items():
            print(f"  {scenario}/{ramp} {n}: worst {_acq_ns(v)} at {g.fmt_key(k) if k else 'n/a'}; censored {len(s.censored[n])}; invalid {s.n_invalid}")
        if s.n_invalid:
            rc = 1
    if rbad:
        print("RE-DERIVATION MISMATCH: " + "; ".join(rbad[:6]))
        rc = 1
    if ctrl_bad:
        print("CONTROL PROBLEMS:")
        for x in ctrl_bad:
            print(f"  - {x}")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
