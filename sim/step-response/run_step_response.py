#!/usr/bin/env python3
"""Closed-loop unity-gain follower small-step response of the committed sized
schematic across the full ratified PVT grid (issue #113). EVIDENCE ONLY.

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_step.spice`) declares no transistor.

What it runs
------------
The 45-point grid `process {typical, ff, ss, fs, sf} x temperature {-40, 27,
125 C} x supply {2.97, 3.30, 3.63 V}` is ONE `klt sim` request (a `corners`
block). Which backend executes it is `klt`'s decision (`--backend`, or
`$KLT_SIM_BACKEND`); on a dispatch worker that is the Spot batch fleet. This
script never launches ngspice itself for the grid and never falls back to a
local grid when a batch submit fails: no record is written and the error is
printed. Only the three single-unit controls (nominal point) run locally.

Per point, for the rising AND the falling 100 mV edge of a unity-gain
follower with CL = 2 pF, it extracts from the rawfile klt retains:

  * overshoot (%) past the settled level, and preshoot (%) the wrong way first,
  * settling time to 1 % and to 0.1 % of the realised step (from the input
    edge to the last exit from the band; "not settled" when the output is still
    outside the band inside the reference window at the end of the edge window),
  * whether the response is monotonic (no reversal or preshoot larger than
    MONO_TOL of the step),
  * the 10-90 % rise time, reported only to show how slew-affected a 100 mV
    step is.

There is NO spec row for settling or overshoot (`spec/target-spec.md`), and
this experiment adds none: it records numbers and identifies the worst points,
it never judges them. A bound would need a `spec/` decision record.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<process>_<T>c_<vdd>v.{log,cir,dat}   one set per point
    corners/<rid>/klt-report.json                       sanitised klt report
    corners/<rid>/controls/...                          single-unit controls
    netlist-snapshots/<rid>.spice                       DUT + testbench + conditions
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/step-response/run_step_response.py              # full grid + record
    python3 sim/step-response/run_step_response.py --smoke      # one nominal point, local, no record

Exit status: 0 when the evidence is complete (the grid ran with every point
present, the cross-checks agree, the committed data re-derive and the controls
behave); 1 otherwise; 2 when the grid could not be run (no record written).
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
    load_sibling,
    ngspice_version,
    remote_of,
    run_klt,
    run_klt_retrying,
    sanitise_report,
    stage_workdir,
)

# Shared DUT/testbench source guards, grid bookkeeping and request skeleton:
# ONE copy, in the gain driver (also used by every other experiment).
g = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")
# The real-rawfile parser of the slew bench, reused unchanged.
ssp = load_sibling("slew_swing_power_driver", "sim/slew-swing-power/run_slew_swing_power.py")
# One source for everything the fingerprint covers.
mc = load_sibling("step_response_measurement_config", "sim/step-response/measurement_config.py")

TESTBENCH = HERE / "testbench" / "tb_step.spice"

CORNERS = mc.CORNERS
TEMPS_C = mc.TEMPS_C
SUPPLIES_V = mc.SUPPLIES_V
NOMINAL = g.NOMINAL
IBIAS_A = mc.IBIAS_A
CL_F = mc.CL_F

STEP_V = mc.STEP_V
RISE_T = mc.RISE_T
FALL_T = mc.FALL_T
TSTOP = mc.TSTOP
TSTEP = mc.TSTEP
EDGE_T = mc.EDGE_T
SAMPLE_BEFORE = mc.SAMPLE_BEFORE
REF_WINDOW = mc.REF_WINDOW
SETTLE_BANDS = mc.SETTLE_BANDS
MONO_TOL = mc.MONO_TOL
DRIFT_FRAC = mc.DRIFT_FRAC
GBW_MIN_HZ = mc.GBW_MIN_HZ

# Validity (not fingerprinted): the follower must actually follow.
LEVEL_TOL_V = 0.02  # |vout - vinp| at the pre-edge instant and over the reference window
STEP_REL_TOL = 0.2  # realised step must be STEP_V within +-20 %
MIN_EDGE_SAMPLES = 50

# Cross-check tolerance against ngspice's own `.meas` (0.5 % of the 100 mV step).
XCHK_LEVEL_V = 0.5e-3

Key = g.Key  # (process, temperature_c, supply_v)

#: Edges of the bench: (name, input edge time, end of the edge window, direction).
EDGES = (("rise", RISE_T, FALL_T, +1), ("fall", FALL_T, None, -1))


# --------------------------------------------------------------------------
# Testbench materialisation (guards shared with the gain driver)
# --------------------------------------------------------------------------

_IBIAS_RE = re.compile(r"^Ibias\s+vdd\s+ibias\s+dc\s+\S+\s*$", re.M)
_CL_RE = re.compile(r"^CL\s+vout\s+0\s+\S+\s*$", re.M)


def materialise(work: Path, pdk: Pdk, *, ibias_a: float | None = None, cl_f: float | None = None,
                dut_text: str | None = None) -> Path:
    """Write the per-run work directory; return the testbench path.

    The committed testbench is used verbatim except for the two `.include`
    targets (rewritten to absolute paths in `work`) and, for a control only,
    an explicit `Ibias` or `CL` override. `dut_text` replaces the committed DUT
    only to prove the stale-DUT guard (it must then be rejected).
    """
    tb = stage_workdir(work, pdk, TESTBENCH.read_text(), guard_tb=g.guard_testbench,
                       guard_dut=g.guard_dut, dut_text=dut_text, tb_label=TESTBENCH.name)
    if ibias_a is not None:
        tb, n = _IBIAS_RE.subn(f"Ibias vdd ibias dc {ibias_a:g}", tb)
        if n != 1:
            raise RuntimeError("testbench Ibias line not found exactly once")
    if cl_f is not None:
        tb, n = _CL_RE.subn(f"CL vout 0 {cl_f:g}", tb)
        if n != 1:
            raise RuntimeError("testbench CL line not found exactly once")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


def make_request(netlist: Path, pdk: Pdk, corners, temps, supplies) -> dict:
    """One 45-point (or single-point) `corners` request."""
    req = g.ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = mc.analysis()
    req["measurements"] = mc.measurements()
    req["options"] = {"timeout_s": 600, "keep_artifacts": True, "waveforms": True}
    return req


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class Edge:
    """One edge's response. Settling times are seconds from the input edge;
    `math.inf` means "not settled within the window"."""

    name: str
    v0: float = float("nan")  # pre-edge output level
    vfinal: float = float("nan")  # settled output level (reference-window time average)
    step_v: float = float("nan")  # vfinal - v0 (signed)
    overshoot_pct: float = float("nan")
    preshoot_pct: float = float("nan")
    reversal_pct: float = float("nan")  # largest fall back from the running extremum
    drift_pct: float = float("nan")  # |vout(ref end) - vout(ref start)| across the reference window
    monotonic: bool = False
    ts: dict = field(default_factory=dict)  # {band: seconds or inf}
    t10_90: float = float("nan")
    peak_v: float = float("nan")  # extreme output level in the step direction


@dataclass
class Metrics:
    valid: bool
    reason: str = ""
    rise: Edge | None = None
    fall: Edge | None = None
    x: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    y: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    vin: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)

    def edges(self) -> list[Edge]:
        return [e for e in (self.rise, self.fall) if e is not None]

    @property
    def overshoot_pct(self) -> float:
        return max(e.overshoot_pct for e in self.edges()) if self.valid else float("nan")

    def settling(self, band: float) -> float:
        return max(e.ts[band] for e in self.edges()) if self.valid else float("nan")

    @property
    def monotonic(self) -> bool:
        return self.valid and all(e.monotonic for e in self.edges())


def _bad(reason: str, **kw) -> Metrics:
    return Metrics(valid=False, reason=reason, **kw)


def _cross(t: np.ndarray, y: np.ndarray, level: float, i0: int) -> float | None:
    """Interpolated time of the first upward crossing of `level` at/after sample i0."""
    for i in range(i0, y.size - 1):
        if y[i] < level <= y[i + 1]:
            return float(t[i] + (level - y[i]) / (y[i + 1] - y[i]) * (t[i + 1] - t[i]))
    return None


def settling_time(t: np.ndarray, err: np.ndarray, band: float, t_edge: float, ref_start: float) -> float:
    """Time from `t_edge` to the last exit of |err| from `band` (err normalised to the step).

    `t`/`err` cover the edge window only. Interpolated linearly between the
    last sample outside the band and the next one. Returns `math.inf` when the
    output is still outside the band inside the reference window
    (`t >= ref_start`): the response has not settled within the window.
    """
    out = np.nonzero(np.abs(err) > band)[0]
    if out.size == 0:
        return 0.0
    i = int(out[-1])
    if t[i] >= ref_start or i + 1 >= t.size:
        return math.inf
    a, b = abs(err[i]), abs(err[i + 1])
    f = (a - band) / (a - b) if a != b else 0.0
    return float(t[i] + f * (t[i + 1] - t[i]) - t_edge)


def window_mean(t: np.ndarray, v: np.ndarray, t0: float, t1: float) -> float:
    """Time average of the piecewise-linear waveform over [t0, t1] (trapezoid,
    end points interpolated): independent of which samples fall on the window
    boundary and of ngspice's non-uniform time step."""
    inner = (t > t0) & (t < t1)
    tt = np.concatenate([[t0], t[inner], [t1]])
    vv = np.interp(tt, t, v)
    return float(np.sum((vv[1:] + vv[:-1]) * np.diff(tt)) / 2 / (t1 - t0))


def extract_edge(t: np.ndarray, vo: np.ndarray, vi: np.ndarray, name: str, t_edge: float,
                 t_next: float | None, direction: int) -> tuple[Edge | None, str]:
    """One edge's figures, or (None, reason) when the follower did not follow."""
    ref_end = (t_next - SAMPLE_BEFORE) if t_next is not None else float(t[-1])
    ref_start = ref_end - REF_WINDOW

    def at(tt: float, arr: np.ndarray) -> float:
        return float(np.interp(tt, t, arr))

    t_pre = t_edge - SAMPLE_BEFORE
    v0 = at(t_pre, vo)
    if abs(v0 - at(t_pre, vi)) > LEVEL_TOL_V:
        return None, f"{name}: output not at the input before the edge (vout {v0:.4f} V vs vinp {at(t_pre, vi):.4f} V)"
    if np.count_nonzero((t >= ref_start) & (t <= ref_end)) < 5:
        return None, f"{name}: fewer than 5 samples in the reference window"
    vfinal = window_mean(t, vo, ref_start, ref_end)
    vin_ref = window_mean(t, vi, ref_start, ref_end)
    if abs(vfinal - vin_ref) > LEVEL_TOL_V:
        return None, f"{name}: output did not reach the input (vout {vfinal:.4f} V vs vinp {vin_ref:.4f} V at the end of the window)"
    step = vfinal - v0
    if direction * step < (1 - STEP_REL_TOL) * STEP_V or direction * step > (1 + STEP_REL_TOL) * STEP_V:
        return None, f"{name}: realised step {step * 1e3:+.2f} mV is not {direction * STEP_V * 1e3:+.0f} mV +-{STEP_REL_TOL:.0%}"
    inner = (t > t_edge) & (t < ref_end)
    if np.count_nonzero(inner) < MIN_EDGE_SAMPLES:
        return None, f"{name}: fewer than {MIN_EDGE_SAMPLES} samples in the edge window"
    # Edge window [t_edge, ref_end] with interpolated end points (independent of
    # which samples land exactly on a boundary).
    tw = np.concatenate([[t_edge], t[inner], [ref_end]])
    vw = np.interp(tw, t, vo)
    yw = (vw - v0) / step  # normalised: 0 before, 1 settled
    drift = abs(at(ref_end, vo) - at(ref_start, vo)) / abs(step)
    overshoot = max(0.0, float(yw.max()) - 1.0)
    preshoot = max(0.0, -float(yw.min()))
    reversal = float(np.max(np.maximum.accumulate(yw) - yw))
    t10, t90 = _cross(tw, yw, 0.1, 0), _cross(tw, yw, 0.9, 0)
    e = Edge(
        name=name, v0=v0, vfinal=vfinal, step_v=step,
        overshoot_pct=100 * overshoot, preshoot_pct=100 * preshoot, reversal_pct=100 * reversal,
        drift_pct=100 * drift,
        monotonic=reversal <= MONO_TOL and preshoot <= MONO_TOL,
        ts={b: (settling_time(tw, yw - 1.0, b, t_edge, ref_start) if drift <= DRIFT_FRAC * b else math.inf)
            for b in SETTLE_BANDS},
        t10_90=(t90 - t10) if (t10 is not None and t90 is not None and t90 > t10) else float("nan"),
        peak_v=float(vw.max() if direction > 0 else vw.min()),
    )
    return e, ""


def extract_step(vec: dict[str, np.ndarray]) -> Metrics:
    """Rising- and falling-edge response of the follower (see the module docstring).

    Method, per edge:
      1. Validate: >= 100 samples, strictly increasing time, run reaches TSTOP.
      2. v0 = vout SAMPLE_BEFORE ahead of the edge; must equal vinp to
         LEVEL_TOL_V. vfinal = time-averaged vout over the reference window (the last
         REF_WINDOW of the edge window, ending SAMPLE_BEFORE ahead of the next
         edge, or at the end of the run); must equal the mean vinp there to
         LEVEL_TOL_V; the realised step vfinal - v0 must be STEP_V +- 20 % in
         the edge's direction. Otherwise the point is INVALID.
      3. y = (vout - v0) / (vfinal - v0) over [edge, reference-window end].
         overshoot = max(y) - 1; preshoot = -min(y); reversal = the largest
         fall of y below its running maximum; monotonic iff reversal and
         preshoot are both <= MONO_TOL.
      4. settling time to band b = time from the input edge to the last
         sample with |y - 1| > b, interpolated to the band; `inf` ("not
         settled") when that sample lies inside the reference window, or when
         the output drifts by more than DRIFT_FRAC * b across the reference
         window (a slow tail the window mean would otherwise absorb).
    """
    for need in ("time", "v(vout)", "v(vinp)"):
        if need not in vec:
            return _bad(f"rawfile has no {need} vector")
    t, vo, vi = vec["time"], vec["v(vout)"], vec["v(vinp)"]
    if t.size < 100 or np.any(np.diff(t) <= 0):
        return _bad("fewer than 100 samples or time not strictly increasing")
    if t[-1] < TSTOP * 0.999:
        return _bad(f"transient ends at {t[-1]:.3g} s, before {TSTOP:g} s")
    common = dict(x=t, y=vo, vin=vi)
    edges = {}
    for name, t_edge, t_next, direction in EDGES:
        e, why = extract_edge(t, vo, vi, name, t_edge, t_next, direction)
        if e is None:
            return _bad(why, **common)
        edges[name] = e
    return Metrics(valid=True, rise=edges["rise"], fall=edges["fall"], **common)


# --------------------------------------------------------------------------
# Report handling
# --------------------------------------------------------------------------


def _meas(c: dict) -> dict:
    return {x["name"]: x.get("value") for x in c.get("measurements", [])}


def crosscheck(k: Key, vals: dict, m: Metrics) -> list[str]:
    """Compare ngspice's own `.meas` values with the rawfile extraction.

    A `.meas` that produced no value is skipped (the extraction is the
    authority); a value that DISAGREES blocks the record.
    """
    t, vo = m.x, m.y
    rise_win = (t >= RISE_T) & (t <= FALL_T - SAMPLE_BEFORE)
    fall_win = t >= FALL_T
    mine = {
        "vlo_v": m.rise.v0,
        "vhi_v": m.fall.v0,
        "vmax_rise_v": float(vo[rise_win].max()),
        "vmin_fall_v": float(vo[fall_win].min()),
    }
    bad = []
    for name, ref in mine.items():
        v = vals.get(name)
        if v is not None and abs(v - ref) > XCHK_LEVEL_V:
            bad.append(f"{g.fmt_key(k)}: ngspice {name} {v:.5f} V vs rawfile {ref:.5f} V")
    return bad


def analyse_report(report: dict, want: list[Key]):
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
        if not raw or not Path(raw).is_file():
            problems.append(f"simulation failed for {g.fmt_key(k)}: {diag or 'no rawfile retained'}")
            continue
        try:
            vec = ssp.parse_ascii_real_raw(Path(raw).read_text())
        except ValueError as exc:
            problems.append(f"malformed data for {g.fmt_key(k)}: {exc}")
            continue
        vals = _meas(c)
        m = extract_step(vec)
        if m.valid:
            problems += crosscheck(k, vals, m)
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


# --------------------------------------------------------------------------
# Committed per-point data (re-derivable)
# --------------------------------------------------------------------------

DAT_HEADER = "time_s vout_v vinp_v"


def save_point_data(m: Metrics, path: Path) -> None:
    """Every rawfile sample of the point: `time_s vout_v vinp_v` (enough for
    `rederive()` to reproduce the extraction from the committed file alone)."""
    if m.x.size == 0:
        path.write_text(f"# INVALID: {m.reason}\n")
        return
    head = ([] if m.valid else [f"INVALID: {m.reason}"]) + [DAT_HEADER]
    np.savetxt(path, np.column_stack([m.x, m.y, m.vin]), header="\n".join(head), fmt=("%.9e", "%.10e", "%.10e"))


def rederive(path: Path) -> Metrics:
    """Re-run the extraction on a committed per-point data file."""
    names = None
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        if line[1:].strip() == DAT_HEADER:
            names = DAT_HEADER.split()
    if names is None:
        raise ValueError(f"{path}: no `{DAT_HEADER}` column header")
    d = np.loadtxt(path, ndmin=2)
    if d.shape[1] != 3:
        raise ValueError(f"{path}: {d.shape[1]} columns, expected 3")
    return extract_step({"time": d[:, 0], "v(vout)": d[:, 1], "v(vinp)": d[:, 2]})


def same_result(a: Metrics, b: Metrics) -> bool:
    """True when two extractions agree (validity, monotonic flags, overshoot to
    1e-4 %, settling times to 0.01 ns, settled/not-settled status)."""
    if a.valid != b.valid:
        return False
    if not a.valid:
        return True
    for ea, eb in zip(a.edges(), b.edges()):
        if ea.monotonic != eb.monotonic or abs(ea.overshoot_pct - eb.overshoot_pct) > 1e-4:
            return False
        for band in SETTLE_BANDS:
            x, y = ea.ts[band], eb.ts[band]
            if math.isinf(x) != math.isinf(y) or (not math.isinf(x) and abs(x - y) > 1e-11):
                return False
    return True


# --------------------------------------------------------------------------
# Summary (evidence; no verdict)
# --------------------------------------------------------------------------


def band_label(b: float) -> str:
    return f"{b * 100:g} %"


@dataclass
class Summary:
    n_total: int
    n_invalid: int
    n_monotonic: int
    nonmonotonic: list  # keys
    worst_overshoot: tuple  # (value %, key)
    worst_ts: dict  # band -> (seconds or inf, key)
    not_settled: dict  # band -> [keys]


def summarise(results: dict[Key, Metrics]) -> Summary:
    """Worst points over the grid. A not-settled point is the worst possible
    settling result; invalid points are counted and excluded from the worsts."""
    valid = {k: m for k, m in results.items() if m.valid}
    worst_os = max(((m.overshoot_pct, k) for k, m in valid.items()), default=(float("nan"), None), key=lambda t: t[0])
    worst_ts, not_settled = {}, {}
    for b in SETTLE_BANDS:
        worst_ts[b] = max(((m.settling(b), k) for k, m in valid.items()), default=(float("nan"), None), key=lambda t: t[0])
        not_settled[b] = [k for k, m in valid.items() if math.isinf(m.settling(b))]
    nonmono = [k for k, m in valid.items() if not m.monotonic]
    return Summary(len(results), len(results) - len(valid), len(valid) - len(nonmono), nonmono,
                   worst_os, worst_ts, not_settled)


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


def run_unit(name: str, pdk: Pdk, work: Path, overrides: dict):
    """The nominal point as its own single-unit `klt sim`, LOCAL (allowed by
    the host rules; the 45-point grid never takes this path)."""
    wd = work / name
    tb = materialise(wd, pdk, **overrides)
    proc, temp, vdd = NOMINAL
    req = make_request(tb, pdk, [proc], [temp], [vdd])
    rep = run_klt(req, wd / "out", "local", wd)
    res, arts, problems = analyse_report(rep, [NOMINAL])
    if NOMINAL not in res:
        raise KltError("; ".join(problems))
    return res[NOMINAL], arts[NOMINAL], problems


def run_controls(pdk: Pdk, work: Path) -> list[ControlRun]:
    out = []
    for name, desc, ov in mc.CONTROLS:
        cr = ControlRun(name, desc)
        try:
            m, a, _ = run_unit(name, pdk, work, ov)
            cr.metrics, cr.files = m, {"log": a["log"], "deck": a["deck"]}
        except (KltError, KeyError, RuntimeError) as exc:
            cr.error = str(exc)[:300]
        out.append(cr)
    return out


def control_failures(ctrls: list[ControlRun]) -> list[str]:
    """Reasons the controls do NOT behave as the method requires."""
    bad = []
    by = {c.name: c for c in ctrls}
    nom, big, dead = by.get("nominal"), by.get("cl-x10"), by.get("ibias-zero")
    for c in (nom, big):
        if c is None:
            continue
        if c.metrics is None:
            bad.append(f"{c.name}: did not simulate ({c.error})")
        elif not c.metrics.valid:
            bad.append(f"{c.name}: invalid ({c.metrics.reason})")
    if nom and big and nom.metrics and big.metrics and nom.metrics.valid and big.metrics.valid:
        if not big.metrics.overshoot_pct > nom.metrics.overshoot_pct + 1.0:
            bad.append(f"cl-x10: overshoot {big.metrics.overshoot_pct:.2f} % is not clearly above nominal "
                       f"{nom.metrics.overshoot_pct:.2f} % -- the extraction does not see added ringing")
        if big.metrics.monotonic:
            bad.append("cl-x10: reported monotonic despite the reduced phase margin")
    if dead and dead.metrics is not None and dead.metrics.valid and math.isfinite(dead.metrics.settling(SETTLE_BANDS[0])):
        bad.append("ibias-zero: a dead amplifier produced a settled step -- the extraction cannot be trusted")
    return bad


# --------------------------------------------------------------------------
# Cross-reference: small-signal GBW / PM of the selected gain record
# --------------------------------------------------------------------------

_GAIN_ROW_RE = re.compile(r"^\| `(\w+)` \| (-?[\d.]+) \| ([\d.]+) \| [\d.]+ \| ([\d.]+) \| (-?[\d.]+) \|")


def load_gain_pm(root: Path = REPO_ROOT) -> tuple[str, dict]:
    """(record path, {Key: (GBW MHz, PM deg)}) from the gain record that
    `sim/report/selection.json` selects; ("", {}) when unavailable."""
    try:
        rel = json.loads((root / "sim" / "report" / "selection.json").read_text())["experiments"]["gain-gbw-pm"]
        text = (root / rel).read_text()
    except (OSError, KeyError, ValueError, TypeError):
        return "", {}
    out = {}
    for ln in text.splitlines():
        mm = _GAIN_ROW_RE.match(ln)
        if mm:
            out[(mm.group(1), float(mm.group(2)), float(mm.group(3)))] = (float(mm.group(4)), float(mm.group(5)))
    return rel, out


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------


def build_plots(results: dict[Key, Metrics], plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    for ax, (name, t_edge, _, _) in zip(axes, EDGES):
        for k, m in results.items():
            if not m.valid:
                continue
            e = getattr(m, name)
            sel = (m.x >= t_edge) & (m.x <= t_edge + 300e-9)
            y = (m.y[sel] - e.v0) / e.step_v
            ax.plot((m.x[sel] - t_edge) * 1e9, y, linewidth=1.3 if k == NOMINAL else 0.5, alpha=1.0 if k == NOMINAL else 0.5)
        for b in SETTLE_BANDS[:1]:
            ax.axhline(1 + b, color="grey", linestyle=":", linewidth=0.8)
            ax.axhline(1 - b, color="grey", linestyle=":", linewidth=0.8)
        ax.set_xlabel(f"time after the {name} edge (ns)")
        ax.set_title(f"{name}: all {len(results)} PVT points (nominal bold)")
        ax.grid(True, alpha=0.3)
    axes[0].set_ylabel("normalised output (vout - v0) / step")
    fig.tight_layout()
    fig.savefig(plot_dir / "step_all45.png", dpi=110)
    plt.close(fig)
    return ["step_all45.png"]


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def _fmt(v: float, spec: str = ".2f") -> str:
    return "n/a" if not (isinstance(v, float) and math.isfinite(v)) else format(v, spec)


def fmt_ts(v: float) -> str:
    """Settling time in ns; `not settled` for inf."""
    if isinstance(v, float) and math.isinf(v):
        return "not settled"
    return _fmt(v * 1e9 if isinstance(v, float) else float("nan"), ".1f")


def build_record(*, record, stamp, pdk, ngspice, klt_ver, backend_desc, report, results, ctrls, ctrl_bad,
                 plots, dut_sha, xchk_count, rederived, gain_pm) -> str:
    L: list[str] = []
    add = L.append
    s = summarise(results)
    gain_rel, pm = gain_pm
    tau_max = 1 / (2 * math.pi * GBW_MIN_HZ)
    edge_win = FALL_T - RISE_T
    add(f"# Record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    add(
        "- **Claim (evidence only, no verdict)**: closed-loop small-step response of the unity-gain follower "
        "(CL = 2 pF, 100 mV step about VCM) of the **committed sized schematic** (`design/opamp_two_stage.sch`, "
        "export `design/netlist/opamp_two_stage.spice`, instantiated as `opamp_two_stage`) across the full ratified "
        "PVT grid -- 5 MOS corners x 3 temperatures x 3 supplies = 45 points: overshoot, 1 % and 0.1 % settling "
        "time and monotonicity, rising and falling edge. `spec/target-spec.md` has **no settling or overshoot "
        "row** and this record judges none; a bound would need a `spec/` decision record (issue #113)."
    )
    add(f"- **Result summary**: {s.n_total - s.n_invalid}/{s.n_total} points measured validly ({s.n_invalid} invalid); "
        f"worst overshoot **{_fmt(s.worst_overshoot[0])} %** at {g.fmt_key(s.worst_overshoot[1]) if s.worst_overshoot[1] else 'n/a'}; "
        + "; ".join(f"worst {band_label(b)} settling **{fmt_ts(s.worst_ts[b][0])}{'' if math.isinf(s.worst_ts[b][0]) or not math.isfinite(s.worst_ts[b][0]) else ' ns'}** at "
                    f"{g.fmt_key(s.worst_ts[b][1]) if s.worst_ts[b][1] else 'n/a'}" for b in SETTLE_BANDS)
        + f"; monotonic at {s.n_monotonic}/{s.n_total - s.n_invalid} valid points.")
    add(f"- **Context**: issue #42 (phase margin below the ratified 60 deg at 30/45 points). The PM / GBW columns below "
        f"are copied from the selected gain record `{gain_rel or 'n/a'}` for side-by-side reading; they are not re-measured here.")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (via {pdk.source})")
    env = report.get("environment") or {}
    add(f"- **Tools**: ngspice (local: {ngspice}; engine as run by klt: `{env.get('engine')} {env.get('engine_version')}`), klt `{klt_ver}`")
    add(f"- **Execution**: the 45 points are ONE `klt sim` corner-matrix request: {backend_desc}")
    add(f"  - `.meas` cross-checks (pre-edge levels, rising-window maximum, falling-window minimum) returned for "
        f"{xchk_count[0]}/{xchk_count[1]} points and agreed with the rawfile extraction within {XCHK_LEVEL_V * 1e3:g} mV "
        "(a disagreement blocks the record).")
    n_ok, n_all, bad = rederived
    add(f"  - Re-derivation from the committed `corners/{record}/*.dat` files (re-read from disk, extraction re-run): "
        f"{n_ok}/{n_all} points reproduce validity, overshoot, both settling times and the monotonic flag"
        + (" exactly." if not bad else "; **MISMATCH** at " + "; ".join(bad[:6]) + " (driver exits non-zero)."))
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body verbatim; "
        f"normalised sha256 `{dut_sha}`), snapshotted in full in `netlist-snapshots/{record}.spice`")
    for ln in mc.fingerprint_lines(TESTBENCH.read_text()):
        add(ln)
    add("- **Corner matrix run**:")
    add(f"  - Process (MOS): {', '.join(CORNERS)}")
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add("  - Supply VDD: " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V)
        + " (VCM tracks VDD/2: 1.485 / 1.65 / 1.815 V; the step is +-50 mV about it)")
    add("  - ibias = 10 uA into `ibias`; CL = 2 pF on `vout`, no other load")
    add("- **Passive-section policy**: every MOS corner is paired with the SAME `res_typical` and `mimcap_typical` "
        "sections; no independent RZ / CC corner coverage is claimed (the PM-relevant passives are at typical).")
    add("- **Statistical convention**: N/A -- deterministic process/temperature/supply corners; no mismatch or Monte Carlo.")
    add("")
    add("## Method")
    add("")
    add(f"- Input: VCM - 50 mV until {RISE_T * 1e9:g} ns, a {EDGE_T * 1e9:g} ns rising edge to VCM + 50 mV, a falling "
        f"edge back at {FALL_T * 1e9:g} ns, run to {TSTOP * 1e9:g} ns; `.tran {TSTEP:g} {TSTOP:g}` "
        "(ngspice's internal step is bounded by the same 1 ns).")
    add(f"- **Window vs small-signal GBW**: the ratified GBW >= {GBW_MIN_HZ / 1e6:g} MHz gives a closed-loop time constant "
        f"tau <= 1/(2 pi GBW) = {tau_max * 1e9:.1f} ns; each edge window is {edge_win * 1e9:g} ns = "
        f"{edge_win / tau_max:.0f} tau (a single-pole response reaches 0.1 % in 6.9 tau = {6.9 * tau_max * 1e9:.0f} ns). "
        f"The settled level is the time-averaged output over the last {REF_WINDOW * 1e9:g} ns of each window "
        f"(ending {SAMPLE_BEFORE * 1e9:g} ns before the next edge, or at the end of the run).")
    add(f"- **Levels**: v0 = vout {SAMPLE_BEFORE * 1e9:g} ns before the edge; vfinal = reference-window time average. Both must equal "
        f"vinp to {LEVEL_TOL_V * 1e3:g} mV and the realised step must be {STEP_V * 1e3:g} mV +-{STEP_REL_TOL:.0%}, else the "
        "point is INVALID. y = (vout - v0) / (vfinal - v0).")
    add("- **Overshoot** = max(y) - 1 (%, 0 if never above); **preshoot** = -min(y) (wrong-way start). "
        f"**Monotonic** iff the largest fall of y below its running maximum and the preshoot are both <= {MONO_TOL:.1%} of the step.")
    add("- **Settling time** to band b = time from the input edge (start of the 1 ns ramp) to the last exit of |y - 1| "
        "from b, interpolated linearly; **not settled** if the output is still outside b inside the reference window, "
        f"or drifts by more than {DRIFT_FRAC:g} x b across it (a slow tail hidden in the window average); reported as "
        "such and counted as the worst settling result. Largest reference-window drift over the grid: "
        + _fmt(max((e.drift_pct for m in results.values() if m.valid for e in m.edges()), default=float("nan")), ".2g")
        + " % of the step.")
    add("- **10-90 % time** is reported only to show how far a 100 mV step is from small-signal behaviour.")
    add("- Per point the reported overshoot and settling times are the WORSE of the rising and falling edges; the "
        "per-edge values are in the table.")
    add("")
    add("## Worst points (evidence; no bound applies)")
    add("")
    add("| Figure | Worst value | Point (process / T / VDD) |")
    add("|---|---|---|")
    add(f"| Overshoot (worse edge) | {_fmt(s.worst_overshoot[0])} % | {g.fmt_key(s.worst_overshoot[1]) if s.worst_overshoot[1] else 'n/a'} |")
    for b in SETTLE_BANDS:
        v, k = s.worst_ts[b]
        unit = "" if not math.isfinite(v) else " ns"
        extra = f" ({len(s.not_settled[b])} points not settled)" if s.not_settled[b] else ""
        add(f"| Settling to {band_label(b)} (worse edge) | {fmt_ts(v)}{unit}{extra} | {g.fmt_key(k) if k else 'n/a'} |")
    add(f"| Non-monotonic points | {len(s.nonmonotonic)}/{s.n_total - s.n_invalid} | "
        + (", ".join(g.fmt_key(k) for k in s.nonmonotonic[:6]) + (" ..." if len(s.nonmonotonic) > 6 else "") if s.nonmonotonic else "none") + " |")
    if s.n_invalid:
        add(f"| Invalid measurements | {s.n_invalid}/{s.n_total} | see the table notes |")
    if pm:
        add("")
        valid = [(m.overshoot_pct, pm[k][1], k) for k, m in results.items() if m.valid and k in pm]
        if valid:
            lo_pm = min(valid, key=lambda t: t[1])
            add(f"The lowest-PM point of the gain record ({g.fmt_key(lo_pm[2])}, PM {lo_pm[1]:.2f} deg) shows "
                f"{lo_pm[0]:.2f} % overshoot here. Points with PM < 60 deg in the gain record: overshoot "
                + _fmt(min((o for o, p, _ in valid if p < 60), default=float("nan"))) + " .. "
                + _fmt(max((o for o, p, _ in valid if p < 60), default=float("nan"))) + " %; points with PM >= 60 deg: "
                + _fmt(min((o for o, p, _ in valid if p >= 60), default=float("nan"))) + " .. "
                + _fmt(max((o for o, p, _ in valid if p >= 60), default=float("nan"))) + " %.")
    add("")
    add("## All 45 points")
    add("")
    add("Settling times in ns from the input edge; overshoot / preshoot in % of the realised step; rise = rising edge, "
        "fall = falling edge. PM / GBW are from the gain record named above.")
    add("")
    add("| Process | T (C) | VDD (V) | Overshoot rise / fall (%) | Preshoot max (%) | ts 1 % rise / fall (ns) | "
        "ts 0.1 % rise / fall (ns) | Monotonic | 10-90 % rise / fall (ns) | PM (deg) | GBW (MHz) | note |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    keyorder = lambda k: (CORNERS.index(k[0]), k[2], k[1])  # noqa: E731
    for k in sorted(results, key=keyorder):
        m = results[k]
        p = pm.get(k)
        pmc = (f"{p[1]:.2f}", f"{p[0]:.3f}") if p else ("n/a", "n/a")
        if not m.valid:
            add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | n/a | n/a | n/a | n/a | n/a | n/a | {pmc[0]} | {pmc[1]} | INVALID: {m.reason} |")
            continue
        r, f = m.rise, m.fall
        b1, b2 = SETTLE_BANDS
        add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | {r.overshoot_pct:.2f} / {f.overshoot_pct:.2f} | "
            f"{max(r.preshoot_pct, f.preshoot_pct):.2f} | {fmt_ts(r.ts[b1])} / {fmt_ts(f.ts[b1])} | "
            f"{fmt_ts(r.ts[b2])} / {fmt_ts(f.ts[b2])} | {'yes' if m.monotonic else 'no'} | "
            f"{_fmt(r.t10_90 * 1e9, '.1f')} / {_fmt(f.t10_90 * 1e9, '.1f')} | {pmc[0]} | {pmc[1]} | |")
    add("")
    add("## Controls (single local units at the nominal point; recorded separately from the grid evidence)")
    add("")
    add("| Control | Condition | Overshoot (%) | ts 1 % (ns) | ts 0.1 % (ns) | Monotonic | note |")
    add("|---|---|---|---|---|---|---|")
    for c in ctrls:
        m = c.metrics
        if m is None:
            add(f"| `{c.name}` | {c.description} | not simulated | | | | {c.error[:100]} |")
        elif not m.valid:
            add(f"| `{c.name}` | {c.description} | INVALID | | | | {m.reason[:120]} |")
        else:
            add(f"| `{c.name}` | {c.description} | {m.overshoot_pct:.2f} | {fmt_ts(m.settling(SETTLE_BANDS[0]))} | "
                f"{fmt_ts(m.settling(SETTLE_BANDS[1]))} | {'yes' if m.monotonic else 'no'} | |")
    add("")
    add("Required behaviour: nominal and cl-x10 measure validly; cl-x10 (lower phase margin) overshoots more than "
        "nominal by > 1 percentage point and is non-monotonic (the extraction sees added ringing); ibias-zero never "
        "yields a valid, 1 %-settled step.")
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
        add(f"- `sim/step-response/records/{record}-plots/{p}`")
    add("")
    add("## Links")
    add("")
    add("- Testbench: `sim/step-response/testbench/tb_step.spice`")
    add("- Run script: `sim/step-response/run_step_response.py`; extraction/guard tests: `sim/step-response/test_step_response.py`")
    add(f"- Netlist snapshot (DUT + testbench + conditions): `sim/step-response/netlist-snapshots/{record}.spice`")
    add(f"- Per-point logs, decks and data, the sanitised klt report and the control runs: `sim/step-response/corners/{record}/`")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #113)")
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
    for e in m.edges():
        print(f"  {e.name}: step {e.step_v * 1e3:+.2f} mV, overshoot {e.overshoot_pct:.2f} %, preshoot {e.preshoot_pct:.2f} %, "
              f"ts1% {fmt_ts(e.ts[SETTLE_BANDS[0]])}, ts0.1% {fmt_ts(e.ts[SETTLE_BANDS[1]])} (ns), "
              f"10-90 {e.t10_90 * 1e9:.1f} ns, monotonic {e.monotonic}")


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="step-smoke-") as scratch:
        try:
            m, _, problems = run_unit("smoke", pdk, Path(scratch), {})
        except (KltError, RuntimeError) as exc:
            print(f"SMOKE TEST FAILED: {exc}")
            return 1
    if not m.valid or problems:
        print(f"SMOKE TEST FAILED: {m.reason or problems}")
        return 1
    print_point(m)
    print("smoke test OK (measurement valid; this experiment records evidence, it judges nothing)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one nominal point, local, no record")
    ap.add_argument("--backend", help="klt execution backend for the 45-point grid (default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet capacity (never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    want = g.expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record)  # fail early if this id was already used
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: {len(want)} points, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    with tempfile.TemporaryDirectory(prefix="step-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        req = make_request(tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
        req["batch"] = batch_block(args)
        try:
            report = run_klt_retrying(req, work / "grid" / "out", args.backend, work / "grid",
                                      retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
        except KltError as exc:
            # Never fall back to a local grid.
            print(f"ERROR: the 45-point klt request could not be run: {str(exc)[-600:]}", file=sys.stderr)
            print("NO RECORD WRITTEN.", file=sys.stderr)
            return 2
        results, arts, problems = analyse_report(report, want)
        if problems:
            print("ERROR: the 45-point grid did not complete cleanly: " + "; ".join(problems[:8]), file=sys.stderr)
            print("NO RECORD WRITTEN.", file=sys.stderr)
            return 2
        backend_desc = describe_backend(report)
        print(f"  45 points ok ({backend_desc})")
        names = [x["name"] for x in mc.measurements()]
        xchk_count = (sum(all(a["klt_meas"].get(n) is not None for n in names) for a in arts.values()), len(arts))

        ctrls = run_controls(pdk, work)
        ctrl_bad = control_failures(ctrls)

        cdir = paths["corners"]
        cdir.mkdir(parents=True, exist_ok=False)
        for k, a in arts.items():
            stem = g.point_stem(k)
            if a["log"]:
                shutil.copyfile(a["log"], cdir / f"{stem}.log")
            if a["deck"]:
                shutil.copyfile(a["deck"], cdir / f"{stem}.cir")
            save_point_data(results[k], cdir / f"{stem}.dat")
        (cdir / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))
        n_ok, rbad = 0, []
        for k, m in results.items():
            try:
                m2 = rederive(cdir / f"{g.point_stem(k)}.dat")
            except (ValueError, OSError) as exc:
                rbad.append(f"{g.fmt_key(k)}: {exc}")
                continue
            if same_result(m, m2):
                n_ok += 1
            else:
                rbad.append(g.fmt_key(k))
        ccdir = cdir / "controls"
        ccdir.mkdir()
        for c in ctrls:
            for kind, src in c.files.items():
                if src and Path(src).is_file():
                    shutil.copyfile(src, ccdir / f"{c.name}.{'log' if kind == 'log' else 'cir'}")

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        lines = [
            f"* netlist snapshot for record {record} (issue #113)",
            "* Reproduces the measured design: DUT contents, testbench, conditions.",
            "* ---- conditions: klt sim request ----",
        ]
        lines += ["* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()]
        lines += ["", "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
                  dut_text, "* ---- testbench: sim/step-response/testbench/tb_step.spice (verbatim) ----", TESTBENCH.read_text()]
        a0 = arts.get(NOMINAL)
        if a0 and a0["deck"]:
            lines += ["* ---- klt-generated deck of the nominal point ----"]
            lines += ["* | " + ln for ln in Path(a0["deck"]).read_text().splitlines()] + [""]
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join(lines))

        plots = build_plots(results, paths["plots"])
        md = build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_ver=kver, backend_desc=backend_desc,
            report=report, results=results, ctrls=ctrls, ctrl_bad=ctrl_bad, plots=plots, dut_sha=dut_sha,
            xchk_count=xchk_count, rederived=(n_ok, len(results), rbad), gain_pm=load_gain_pm(),
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    s = summarise(results)
    print(f"  worst overshoot {_fmt(s.worst_overshoot[0])} % at {g.fmt_key(s.worst_overshoot[1]) if s.worst_overshoot[1] else 'n/a'}")
    for b in SETTLE_BANDS:
        v, k = s.worst_ts[b]
        print(f"  worst {band_label(b)} settling {fmt_ts(v)}{' ns' if math.isfinite(v) else ''} at {g.fmt_key(k) if k else 'n/a'}")
    print(f"  monotonic at {s.n_monotonic}/{s.n_total - s.n_invalid}; invalid {s.n_invalid}")
    rc = 0
    if rbad:
        print("RE-DERIVATION MISMATCH: " + "; ".join(rbad[:6]))
        rc = 1
    if ctrl_bad:
        print("CONTROL PROBLEMS:")
        for x in ctrl_bad:
            print(f"  - {x}")
        rc = 1
    if s.n_invalid:
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
