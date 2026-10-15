#!/usr/bin/env python3
"""Input-overdrive / output-saturation recovery of the committed sized schematic
across the full ratified PVT grid (issue #183). EVIDENCE ONLY.

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_overdrive.spice`) declares no transistor.

What it runs
------------
The 45-point grid `process {typical, ff, ss, fs, sf} x temperature {-40, 27,
125 C} x supply {2.97, 3.30, 3.63 V}` is ONE `klt sim` request (a `corners`
block). Which backend executes it is `klt`'s decision (`--backend`, or
`$KLT_SIM_BACKEND`); on a dispatch worker that is the Spot batch fleet. This
script never launches ngspice itself for the grid and never falls back to a
local grid when a batch submit fails: no record is written and the error is
printed. Only the three single-unit controls (nominal point) run locally.

Per point, for the HIGH and the LOW overdrive of a unity-gain follower with
CL = 2 pF, it extracts from the rawfile klt retains:

  * whether the output actually saturated (it must travel at least
    MIN_TRAVEL_V in the overdriven direction and be essentially stopped at
    the end of the hold -- residual velocity <= SAT_MAX_RATE_V_US, ~1/30 of
    the nominal slew rate; a point that did not saturate is INVALID, never a
    fast recovery),
  * the recovery time: from the removal edge (input back at VCM) to the last
    exit of |vout - v_ref| from a +-1 % band about the linear output level
    v_ref (the pre-overdrive value), interpolated linearly,
  * whether the output LATCHED: still outside the band at the end of the
    recovery window, drifting within it (a slow tail), or settled at a
    different level (hysteresis) -- all reported as "not recovered" and
    ranked as the worst result, never as a fast recovery,
  * overshoot past the linear level, and any wrong-way (deeper into
    saturation) first move, as evidence.

There is NO spec row for overload recovery (`spec/target-spec.md`), and this
experiment adds none: it records numbers, names the worst points and states
explicitly whether any point latched. A bound would need a `spec/` decision
record.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<process>_<T>c_<vdd>v.{log,cir,dat}   one set per point
    corners/<rid>/klt-report.json                       sanitised klt report
    corners/<rid>/controls/...                          single-unit controls
    netlist-snapshots/<rid>.spice                       DUT + testbench + conditions
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/overdrive-recovery/run_overdrive_recovery.py              # full grid + record
    python3 sim/overdrive-recovery/run_overdrive_recovery.py --smoke      # one nominal point, local, no record

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
# Window-mean and last-exit-settling helpers of the step bench, reused unchanged.
sr = load_sibling("step_response_driver", "sim/step-response/run_step_response.py")
# One source for everything the fingerprint covers.
mc = load_sibling("overdrive_recovery_measurement_config", "sim/overdrive-recovery/measurement_config.py")

TESTBENCH = HERE / "testbench" / "tb_overdrive.spice"

CORNERS = mc.CORNERS
TEMPS_C = mc.TEMPS_C
SUPPLIES_V = mc.SUPPLIES_V
NOMINAL = g.NOMINAL
IBIAS_A = mc.IBIAS_A
CL_F = mc.CL_F

OD_UP_V = mc.OD_UP_V
OD_DN_V = mc.OD_DN_V
RISE_T = mc.RISE_T
FALL_T = mc.FALL_T
OD_LO_T = mc.OD_LO_T
RISE2_T = mc.RISE2_T
TSTOP = mc.TSTOP
TSTEP = mc.TSTEP
EDGE_T = mc.EDGE_T
SAMPLE_BEFORE = mc.SAMPLE_BEFORE
REF_WINDOW = mc.REF_WINDOW
MIN_TRAVEL_V = mc.MIN_TRAVEL_V
SAT_MAX_RATE_V_US = mc.SAT_MAX_RATE_V_US
BAND_FRAC = mc.BAND_FRAC
DRIFT_FRAC = mc.DRIFT_FRAC
GBW_MIN_HZ = mc.GBW_MIN_HZ

# Validity (not fingerprinted): the follower must actually follow at VCM and
# the overdrive must actually saturate the output.
LEVEL_TOL_V = 0.02  # |vout - vinp| over the pre-overdrive reference window
MIN_RECOVERY_SAMPLES = 50

# Cross-check tolerance against ngspice's own `.meas` (about one recovery band).
XCHK_LEVEL_V = 20e-3

Key = g.Key  # (process, temperature_c, supply_v)

#: The two overdrive polarities: (name, overdrive starts, overdrive removed,
#: next bench event bounding the recovery window, rail the output clips at).
PHASES = (
    ("high", RISE_T, FALL_T, OD_LO_T, "vdd"),
    ("low", OD_LO_T, RISE2_T, None, "0"),
)


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
class Phase:
    """One overdrive polarity. `rec_s` is seconds from the removal edge;
    `math.inf` means the output did not return to the band within the
    recovery window (latch, slow tail or a wrong final level)."""

    name: str
    v_sat: float = float("nan")  # saturated output level (hold-window time average)
    v_ref: float = float("nan")  # linear output level (pre-overdrive window time average)
    v_final: float = float("nan")  # output at the end of the recovery window
    band_v: float = float("nan")  # recovery half-band, volts (BAND_FRAC of |v_ref|)
    rec_s: float = float("nan")
    drift: float = float("nan")  # output drift across the final reference window, in bands
    overshoot_mv: float = float("nan")  # furthest excursion past v_ref, mV
    preshoot_mv: float = float("nan")  # wrong-way first move (deeper into saturation), mV

    @property
    def latched(self) -> bool:
        return math.isinf(self.rec_s)


@dataclass
class Metrics:
    valid: bool
    reason: str = ""
    high: Phase | None = None
    low: Phase | None = None
    x: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    y: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    vin: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)

    def phases(self) -> list[Phase]:
        return [p for p in (self.high, self.low) if p is not None]

    @property
    def recovery(self) -> float:
        """Worse-polarity recovery time; inf when any polarity did not recover."""
        return max(p.rec_s for p in self.phases()) if self.valid else float("nan")

    @property
    def latched(self) -> bool:
        return self.valid and any(p.latched for p in self.phases())

    @property
    def overshoot_mv(self) -> float:
        return max(p.overshoot_mv for p in self.phases()) if self.valid else float("nan")


def _bad(reason: str, **kw) -> Metrics:
    return Metrics(valid=False, reason=reason, **kw)


def extract_phase(t: np.ndarray, vo: np.ndarray, vi: np.ndarray, name: str, t_od: float,
                  t_removed: float, t_next: float | None) -> tuple[Phase | None, str]:
    """One polarity's figures, or (None, reason) when the bench was not what it
    must be (the follower did not follow at VCM, or the output did not saturate)."""
    ref_end = (t_next - SAMPLE_BEFORE) if t_next is not None else float(t[-1])
    ref_start = ref_end - REF_WINDOW

    def at(tt: float, arr: np.ndarray) -> float:
        return float(np.interp(tt, t, arr))

    # Linear reference: mean vout over the last REF_WINDOW before the overdrive
    # starts; the follower must be following there (vout = vinp).
    w0, w1 = t_od - SAMPLE_BEFORE - REF_WINDOW, t_od - SAMPLE_BEFORE
    v_ref = sr.window_mean(t, vo, w0, w1)
    vin_ref = sr.window_mean(t, vi, w0, w1)
    if abs(v_ref - vin_ref) > LEVEL_TOL_V:
        return None, (f"{name}: output not at the input before the overdrive "
                      f"(vout {v_ref:.4f} V vs vinp {vin_ref:.4f} V)")
    if np.count_nonzero((t >= ref_start) & (t <= ref_end)) < 5:
        return None, f"{name}: fewer than 5 samples in the reference window"
    # Saturation: mean and residual velocity over the last REF_WINDOW of the
    # hold; the output must have travelled deep in the overdriven direction
    # AND be essentially stopped (vs the ~14.5 V/us slew that drove it there)
    # -- the landing on the current-limited limits is soft (the negative clip
    # limit sits ~0.5 V above ground), so a flatness or fraction-of-rail test
    # would be meaningless.
    h0, h1 = t_removed - SAMPLE_BEFORE - REF_WINDOW, t_removed - SAMPLE_BEFORE
    v_sat = sr.window_mean(t, vo, h0, h1)
    rate_v_us = abs(float(np.interp(h1, t, vo) - np.interp(h0, t, vo))) / (h1 - h0) * 1e-6  # V/s -> V/us
    travel = (v_sat - v_ref) if name == "high" else (v_ref - v_sat)
    if travel < MIN_TRAVEL_V or rate_v_us > SAT_MAX_RATE_V_US:
        return None, (f"{name}: output did not saturate (travelled {travel:.3f} V of the demanded "
                      f"{MIN_TRAVEL_V:.1f} V+ and still moving {rate_v_us:.2f} V/us at removal, need "
                      f">= {MIN_TRAVEL_V:.1f} V within {SAT_MAX_RATE_V_US:g} V/us)")
    inner = (t > t_removed) & (t < ref_end)
    if np.count_nonzero(inner) < MIN_RECOVERY_SAMPLES:
        return None, f"{name}: fewer than {MIN_RECOVERY_SAMPLES} samples in the recovery window"
    travel = v_ref - v_sat  # signed; the output must move this far back to v_ref
    band_v = BAND_FRAC * abs(v_ref)
    # Recovery window [t_removed, ref_end] with interpolated end points.
    tw = np.concatenate([[t_removed], t[inner], [ref_end]])
    vw = np.interp(tw, t, vo)
    yw = (vw - v_sat) / travel  # normalised: 0 = saturated level, 1 = linear value
    band_n = band_v / abs(travel)
    drift = abs(at(ref_end, vo) - at(ref_start, vo)) / band_v  # in bands
    rec = (sr.settling_time(tw, yw - 1.0, band_n, t_removed, ref_start)
           if drift <= DRIFT_FRAC else math.inf)
    overshoot_mv = max(0.0, float(yw.max()) - 1.0) * abs(travel) * 1e3
    preshoot_mv = max(0.0, -float(yw.min())) * abs(travel) * 1e3
    ph = Phase(name=name, v_sat=v_sat, v_ref=v_ref, v_final=at(ref_end, vo), band_v=band_v,
               rec_s=rec, drift=drift, overshoot_mv=overshoot_mv, preshoot_mv=preshoot_mv)
    return ph, ""


def extract_recovery(vec: dict[str, np.ndarray]) -> Metrics:
    """High- and low-overdrive recovery of the follower (see the module docstring).

    Method, per polarity:
      1. Validate the run: >= 100 samples, strictly increasing time, run
         reaches TSTOP.
      2. v_ref = mean vout over the last REF_WINDOW before the overdrive;
         must equal vinp there to LEVEL_TOL_V (the follower follows), else the
         point is INVALID.
      3. v_sat = mean vout over the last REF_WINDOW of the saturation hold;
         the output must have travelled >= MIN_TRAVEL_V in the overdriven
         direction and be essentially stopped (|dvout/dt| over that window
         <= SAT_MAX_RATE_V_US), else INVALID (the overdrive never drove the
         output to its output-stage limit, so there is no recovery to
         measure).
      4. y = (vout - v_sat) / (v_ref - v_sat) over the recovery window
         [removal edge, reference-window end]. Recovery time = time from the
         removal edge to the last exit of |y - 1| from the band
         (BAND_FRAC of |v_ref|, as a fraction of the travel), interpolated
         linearly; `inf` ("not recovered") when the output is still outside
         the band inside the reference window, drifts by more than
         DRIFT_FRAC bands across it (a slow tail the window mean would
         otherwise absorb), or settles at a level more than one band from
         v_ref (hysteresis -- y never returns to 1).
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
    phases = {}
    for name, t_od, t_removed, t_next, _rail in PHASES:
        ph, why = extract_phase(t, vo, vi, name, t_od, t_removed, t_next)
        if ph is None:
            return _bad(why, **common)
        phases[name] = ph
    return Metrics(valid=True, high=phases["high"], low=phases["low"], **common)


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
    hold_hi = (t >= RISE_T) & (t <= FALL_T - SAMPLE_BEFORE)
    hold_lo = (t >= OD_LO_T) & (t <= RISE2_T - SAMPLE_BEFORE)
    mine = {
        "vpre_v": m.high.v_ref,
        "vsat_hi_v": float(vo[hold_hi].max()),
        "vsat_lo_v": float(vo[hold_lo].min()),
        "vfinal_v": float(np.interp(TSTOP - SAMPLE_BEFORE, t, vo)),
    }
    bad = []
    for name, ref in mine.items():
        v = vals.get(name)
        if v is not None and abs(v - ref) > XCHK_LEVEL_V:
            bad.append(f"{g.fmt_key(k)}: ngspice {name} {v:.5f} V vs rawfile {ref:.5f} V")
    return bad


def analyse_report(report: dict, want: list[Key]) -> tuple[dict, dict, list[str]]:
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
        m = extract_recovery(vec)
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
    return extract_recovery({"time": d[:, 0], "v(vout)": d[:, 1], "v(vinp)": d[:, 2]})


def same_result(a: Metrics, b: Metrics) -> bool:
    """True when two extractions agree (validity, recovery times to 0.01 ns,
    recovered/not-recovered status, overshoot to 1e-4 mV, saturation and
    reference levels to 0.1 mV)."""
    if a.valid != b.valid:
        return False
    if not a.valid:
        return True
    for pa, pb in zip(a.phases(), b.phases()):
        if math.isinf(pa.rec_s) != math.isinf(pb.rec_s) or (not math.isinf(pa.rec_s) and abs(pa.rec_s - pb.rec_s) > 1e-11):
            return False
        for f in ("v_sat", "v_ref", "v_final"):
            if abs(getattr(pa, f) - getattr(pb, f)) > 1e-4:
                return False
        if abs(pa.overshoot_mv - pb.overshoot_mv) > 1e-4 or abs(pa.preshoot_mv - pb.preshoot_mv) > 1e-4:
            return False
    return True


# --------------------------------------------------------------------------
# Summary (evidence; no verdict)
# --------------------------------------------------------------------------


def fmt_rec(v: float) -> str:
    """Recovery time in ns; `not recovered` for inf."""
    if isinstance(v, float) and math.isinf(v):
        return "not recovered"
    if not (isinstance(v, float) and math.isfinite(v)):
        return "n/a"
    return format(v * 1e9, ".1f")


@dataclass
class Summary:
    n_total: int
    n_invalid: int
    worst_rec: tuple  # (seconds or inf, key) worse polarity
    worst_rec_phase: dict  # phase -> (seconds or inf, key)
    worst_os: tuple  # (mV, key)
    latched: list  # keys with a not-recovered polarity
    not_recovered: dict  # phase -> [keys]


def summarise(results: dict[Key, Metrics]) -> Summary:
    """Worst points over the grid. A not-recovered (latched) polarity is the
    worst possible recovery result; invalid points are counted and excluded
    from the worsts."""
    valid = {k: m for k, m in results.items() if m.valid}
    worst_rec = max(((m.recovery, k) for k, m in valid.items()), default=(float("nan"), None), key=lambda t: t[0])
    worst_phase, not_recovered = {}, {}
    for name in ("high", "low"):
        worst_phase[name] = max(((getattr(m, name).rec_s, k) for k, m in valid.items()),
                                default=(float("nan"), None), key=lambda t: t[0])
        not_recovered[name] = [k for k, m in valid.items() if math.isinf(getattr(m, name).rec_s)]
    worst_os = max(((m.overshoot_mv, k) for k, m in valid.items()), default=(float("nan"), None), key=lambda t: t[0])
    latched = [k for k, m in valid.items() if m.latched]
    return Summary(len(results), len(results) - len(valid), worst_rec, worst_phase, worst_os, latched, not_recovered)


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
    nom, big, dead = by.get("nominal"), by.get("cl-x5"), by.get("ibias-zero")
    for c in (nom, big):
        if c is None:
            continue
        if c.metrics is None:
            bad.append(f"{c.name}: did not simulate ({c.error})")
        elif not c.metrics.valid:
            bad.append(f"{c.name}: invalid ({c.metrics.reason})")
    if nom and big and nom.metrics and big.metrics and nom.metrics.valid and big.metrics.valid:
        if not big.metrics.recovery > nom.metrics.recovery + 50e-9:
            bad.append(f"cl-x5: recovery {fmt_rec(big.metrics.recovery)} is not clearly above nominal "
                       f"{fmt_rec(nom.metrics.recovery)} -- the extraction does not see the heavier load")
    if dead and dead.metrics is not None and dead.metrics.valid and math.isfinite(dead.metrics.recovery):
        bad.append("ibias-zero: a dead amplifier produced a recovered overdrive -- the extraction cannot be trusted")
    return bad


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------


def build_plots(results: dict[Key, Metrics], plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    for ax, (name, t_od, t_removed, _, _) in zip(axes, PHASES):
        for k, m in results.items():
            if not m.valid:
                continue
            ph = getattr(m, name)
            sel = (m.x >= t_removed) & (m.x <= t_removed + 700e-9)
            y = (m.y[sel] - ph.v_sat) / (ph.v_ref - ph.v_sat)
            ax.plot((m.x[sel] - t_removed) * 1e9, y, linewidth=1.3 if k == NOMINAL else 0.5,
                    alpha=1.0 if k == NOMINAL else 0.5)
        bn = BAND_FRAC * abs(results[NOMINAL].high.v_ref) / abs(results[NOMINAL].high.v_ref - results[NOMINAL].high.v_sat) \
            if NOMINAL in results and results[NOMINAL].valid else BAND_FRAC
        for v in (1 + bn, 1 - bn):
            ax.axhline(v, color="grey", linestyle=":", linewidth=0.8)
        ax.set_xlabel(f"time after the overdrive-{name} removal edge (ns)")
        ax.set_title(f"{name} overdrive: all {len(results)} PVT points (nominal bold)")
        ax.grid(True, alpha=0.3)
    axes[0].set_ylabel("normalised output (vout - v_sat) / (v_ref - v_sat)")
    fig.tight_layout()
    fig.savefig(plot_dir / "recovery_all45.png", dpi=110)
    plt.close(fig)
    return ["recovery_all45.png"]


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def _fmt(v: float, spec: str = ".2f") -> str:
    return "n/a" if not (isinstance(v, float) and math.isfinite(v)) else format(v, spec)


def build_record(*, record, stamp, pdk, ngspice, klt_ver, backend_desc, report, results, ctrls, ctrl_bad,
                 plots, dut_sha, xchk_count, rederived) -> str:
    L: list[str] = []
    add = L.append
    s = summarise(results)
    tau_max = 1 / (2 * math.pi * GBW_MIN_HZ)
    hold = FALL_T - RISE_T
    win_hi = OD_LO_T - SAMPLE_BEFORE - FALL_T
    win_lo = TSTOP - RISE2_T
    add(f"# input-overdrive / output-saturation recovery study -- record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    add("- **Issue**: #183 -- input-overdrive / output-saturation recovery across the PVT grid.")
    add("- **Status**: measured, **no pass or fail verdict**. `spec/target-spec.md` has **no recovery row** and this "
        "record judges none; a bound would need a `spec/` decision record. Nothing in `spec/` is touched.")
    add(
        "- **Claim (evidence only, no verdict)**: the committed sized schematic (`design/opamp_two_stage.sch`, export "
        "`design/netlist/opamp_two_stage.spice`, instantiated as `opamp_two_stage`), wired as the unity-gain follower "
        "the error-amp slots in `gf180-ldo` / `gf180-bandgap` (`spec/target-spec.md` §5) use, is driven BEYOND the "
        "rails so the output clips at each rail in turn; when the overdrive is removed the recovery time to a "
        "+-1 % band about the linear output level is measured across the full ratified PVT grid -- 5 MOS corners x 3 "
        "temperatures x 3 supplies = 45 points -- together with an explicit latch check (output not back inside the "
        "band at the end of the recovery window, drifting inside it, or settling at a different level). For a "
        "two-stage Miller amplifier this probes compensation-node and second-stage recovery after saturation."
    )
    add(f"- **Result summary**: {s.n_total - s.n_invalid}/{s.n_total} points measured validly ({s.n_invalid} invalid); "
        f"worst recovery **{fmt_rec(s.worst_rec[0])}{' ns' if math.isfinite(s.worst_rec[0]) else ''}** (worse polarity) at "
        f"{g.fmt_key(s.worst_rec[1]) if s.worst_rec[1] else 'n/a'}; "
        + "; ".join(f"worst {name} **{fmt_rec(v)}{' ns' if math.isfinite(v) else ''}** at "
                    f"{g.fmt_key(k) if k else 'n/a'}" for name, (v, k) in s.worst_rec_phase.items())
        + f"; worst recovery overshoot **{_fmt(s.worst_os[0])} mV** at {g.fmt_key(s.worst_os[1]) if s.worst_os[1] else 'n/a'}; "
        + ("**LATCHED: none** -- every valid point returned to the band within its window"
           if not s.latched else f"**LATCHED at {len(s.latched)} point(s)**: " + ", ".join(g.fmt_key(k) for k in s.latched))
        + ".")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (via {pdk.source})")
    env = report.get("environment") or {}
    add(f"- **Tools**: ngspice (local: {ngspice}; engine as run by klt: `{env.get('engine')} {env.get('engine_version')}`), klt `{klt_ver}`")
    add(f"- **Execution**: the 45 points are ONE `klt sim` corner-matrix request: {backend_desc}")
    add(f"  - `.meas` cross-checks (pre-overdrive level, high-hold maximum, low-hold minimum, final level) returned for "
        f"{xchk_count[0]}/{xchk_count[1]} points and agreed with the rawfile extraction within {XCHK_LEVEL_V * 1e3:g} mV "
        "(a disagreement blocks the record).")
    n_ok, n_all, bad = rederived
    add(f"  - Re-derivation from the committed `corners/{record}/*.dat` files (re-read from disk, extraction re-run): "
        f"{n_ok}/{n_all} points reproduce validity, both recovery times, the recovered/not-recovered status and the "
        "saturation levels"
        + (" exactly." if not bad else "; **MISMATCH** at " + "; ".join(bad[:6]) + " (driver exits non-zero)."))
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body verbatim; "
        f"normalised sha256 `{dut_sha}`), snapshotted in full in `netlist-snapshots/{record}.spice`")
    for ln in mc.fingerprint_lines(TESTBENCH.read_text()):
        add(ln)
    add("- **Corner matrix run**:")
    add(f"  - Process (MOS): {', '.join(CORNERS)}")
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add("  - Supply VDD: " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V)
        + " (VCM tracks VDD/2: 1.485 / 1.65 / 1.815 V; the input rests there between overdrives)")
    add("  - ibias = 10 uA into `ibias`; CL = 2 pF on `vout`, no other load")
    add("- **Passive-section policy**: every MOS corner is paired with the SAME `res_typical` and `mimcap_typical` "
        "sections; no independent RZ / CC corner coverage is claimed.")
    add("- **Statistical convention**: N/A -- deterministic process/temperature/supply corners; no mismatch or Monte Carlo.")
    add("")
    add("## Conditions")
    add("")
    add("- **Points**: the full ratified 45-point PVT grid -- 5 MOS corners x 3 temperatures x 3 supplies = 45 points.")
    add(f"- **Stimulus** (`tb_overdrive.spice`): the input rests at VCM; from {RISE_T * 1e9:g} ns it is driven to "
        f"VCM + {OD_UP_V:g} V (1 ns edge), which is above the VDD rail at every grid supply, saturating the output "
        f"high for {hold * 1e9:g} ns; at {FALL_T * 1e9:g} ns the overdrive is removed (1 ns edge back to VCM); from "
        f"{OD_LO_T * 1e9:g} ns the input is driven to VCM - {OD_DN_V:g} V, below ground at every grid supply, "
        f"saturating the output low for {(RISE2_T - OD_LO_T) * 1e9:g} ns; at {RISE2_T * 1e9:g} ns it is removed; run "
        f"ends at {TSTOP * 1e9:g} ns; `.tran {TSTEP:g} {TSTOP:g}`.")
    add("- **Reference level**: v_ref = mean vout over the last "
        f"{REF_WINDOW * 1e9:g} ns before the overdrive starts (the linear follower output; the systematic offset "
        "therefore does not enter).")
    add(f"- **Recovery band**: +-{BAND_FRAC:.0%} of |v_ref| (the issue's '+-1 % of the linear output').")
    add(f"- **Saturation requirement**: across the last {REF_WINDOW * 1e9:g} ns of each hold the output must have "
        f"travelled >= {MIN_TRAVEL_V:g} V in the overdriven direction and be essentially stopped -- residual "
        f"velocity <= {SAT_MAX_RATE_V_US:g} V/us, ~1/30 of the nominal slew rate (the landing on the "
        "current-limited output-stage limits is soft: residual creep 0.06-0.12 V/us across the grid, and the "
        "negative clip limit sits ~0.5 V above ground, so a flatness or fraction-of-rail test would be "
        "meaningless); else the point is INVALID, never a fast recovery.")
    add(f"- **Latch**: a polarity is **not recovered** (and the point is flagged latched) when it is still outside "
        f"the band at the end of the {win_hi * 1e9:g} ns (high) / {win_lo * 1e9:g} ns (low) recovery window, when it "
        f"drifts by more than {DRIFT_FRAC:g} bands across the final reference window (a slow tail), or when it "
        "settles more than one band from v_ref (hysteresis). A recovery time is the time from the removal edge to the "
        "LAST exit from the band, so a transient tolerance crossing that leaves the band again never counts as "
        "recovered.")
    add("")
    add("## Method")
    add("")
    add(f"- **Window vs small-signal GBW**: the ratified GBW >= {GBW_MIN_HZ / 1e6:g} MHz gives a closed-loop time constant "
        f"tau <= 1/(2 pi GBW) = {tau_max * 1e9:.1f} ns; the saturation hold is {hold * 1e9:g} ns = {hold / tau_max:.0f} tau "
        f"(the compensation node and second stage settle into the saturated state) and each recovery window is >= "
        f"{min(win_hi, win_lo) / tau_max:.0f} tau.")
    add("- Per point the reported recovery time is the WORSE of the high and low polarities; both polarities are in "
        "the table. Recovery is measured from the start of the 1 ns removal ramp.")
    add("- **Overshoot** is the furthest excursion past v_ref (mV, normalised to the v_sat -> v_ref travel); "
        "**preshoot** is a wrong-way first move deeper into saturation. Evidence only.")
    add("")
    add("## Worst points (evidence; no bound applies)")
    add("")
    add("| Figure | Worst value | Point (process / T / VDD) |")
    add("|---|---|---|")
    add(f"| Recovery, high overdrive | {fmt_rec(s.worst_rec_phase['high'][0])}{' ns' if math.isfinite(s.worst_rec_phase['high'][0]) else ''} | "
        f"{g.fmt_key(s.worst_rec_phase['high'][1]) if s.worst_rec_phase['high'][1] else 'n/a'} |")
    add(f"| Recovery, low overdrive | {fmt_rec(s.worst_rec_phase['low'][0])}{' ns' if math.isfinite(s.worst_rec_phase['low'][0]) else ''} | "
        f"{g.fmt_key(s.worst_rec_phase['low'][1]) if s.worst_rec_phase['low'][1] else 'n/a'} |")
    add(f"| Recovery, worse polarity | {fmt_rec(s.worst_rec[0])}{' ns' if math.isfinite(s.worst_rec[0]) else ''} | "
        f"{g.fmt_key(s.worst_rec[1]) if s.worst_rec[1] else 'n/a'} |")
    add(f"| Recovery overshoot | {_fmt(s.worst_os[0])} mV | {g.fmt_key(s.worst_os[1]) if s.worst_os[1] else 'n/a'} |")
    if s.latched:
        add(f"| Latched points | {len(s.latched)}/{s.n_total - s.n_invalid} | "
            + ", ".join(g.fmt_key(k) for k in s.latched[:8]) + (" ..." if len(s.latched) > 8 else "") + " |")
    else:
        add("| Latched points | 0 (none) | every valid point returned to the band within its window |")
    if s.n_invalid:
        add(f"| Invalid measurements | {s.n_invalid}/{s.n_total} | see the table notes |")
    add("")
    add("## All 45 points")
    add("")
    add("Recovery times in ns from the removal edge; v_sat is the saturated output level; overshoot / preshoot in mV "
        "past v_ref. `latch` names the polarities that did NOT return to the band within their window.")
    add("")
    add("| Process | T (C) | VDD (V) | v_sat hi (V) | v_sat lo (V) | rec hi (ns) | rec lo (ns) | worse (ns) | "
        "overshoot hi / lo (mV) | preshoot max (mV) | latch | note |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    keyorder = lambda k: (CORNERS.index(k[0]), k[2], k[1])  # noqa: E731
    for k in sorted(results, key=keyorder):
        m = results[k]
        if not m.valid:
            add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | INVALID: {m.reason} |")
            continue
        hi, lo = m.high, m.low
        latch = "" if not m.latched else ("high+low" if hi.latched and lo.latched else ("high" if hi.latched else "low"))
        add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | {hi.v_sat:.3f} | {lo.v_sat:.3f} | {fmt_rec(hi.rec_s)} | "
            f"{fmt_rec(lo.rec_s)} | {fmt_rec(m.recovery)} | {hi.overshoot_mv:.1f} / {lo.overshoot_mv:.1f} | "
            f"{max(hi.preshoot_mv, lo.preshoot_mv):.1f} | {latch or 'none'} | |")
    add("")
    add("## Controls (single local units at the nominal point; recorded separately from the grid evidence)")
    add("")
    add("| Control | Condition | rec hi (ns) | rec lo (ns) | latch | note |")
    add("|---|---|---|---|---|---|")
    for c in ctrls:
        m = c.metrics
        if m is None:
            add(f"| `{c.name}` | {c.description} | not simulated | | | {c.error[:100]} |")
        elif not m.valid:
            add(f"| `{c.name}` | {c.description} | INVALID | | | {m.reason[:120]} |")
        else:
            add(f"| `{c.name}` | {c.description} | {fmt_rec(m.high.rec_s)} | {fmt_rec(m.low.rec_s)} | "
                f"{'yes' if m.latched else 'no'} | |")
    add("")
    add("Required behaviour: nominal and cl-x5 measure validly; cl-x5 (CL = 10 pF) recovers slower than nominal by "
        "> 50 ns (the extraction sees the heavier load); ibias-zero never yields a valid recovered overdrive.")
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
        add(f"- `sim/overdrive-recovery/records/{record}-plots/{p}`")
    add("")
    add("## Links")
    add("")
    add("- Testbench: `sim/overdrive-recovery/testbench/tb_overdrive.spice`")
    add("- Run script: `sim/overdrive-recovery/run_overdrive_recovery.py`; extraction/guard tests: "
        "`sim/overdrive-recovery/test_overdrive_recovery.py`")
    add(f"- Netlist snapshot (DUT + testbench + conditions): `sim/overdrive-recovery/netlist-snapshots/{record}.spice`")
    add(f"- Per-point logs, decks and data, the sanitised klt report and the control runs: `sim/overdrive-recovery/corners/{record}/`")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #183)")
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
    for p in m.phases():
        print(f"  {p.name}: v_sat {p.v_sat:.3f} V, v_ref {p.v_ref:.4f} V, band +-{p.band_v * 1e3:.1f} mV, "
              f"recovery {fmt_rec(p.rec_s)}{' ns' if math.isfinite(p.rec_s) else ''}, "
              f"overshoot {p.overshoot_mv:.1f} mV, preshoot {p.preshoot_mv:.1f} mV, latched {p.latched}")


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="overdrive-smoke-") as scratch:
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

    with tempfile.TemporaryDirectory(prefix="overdrive-") as scratch:
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
            f"* netlist snapshot for record {record} (issue #183)",
            "* Reproduces the measured design: DUT contents, testbench, conditions.",
            "* ---- conditions: klt sim request ----",
        ]
        lines += ["* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()]
        lines += ["", "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
                  dut_text, "* ---- testbench: sim/overdrive-recovery/testbench/tb_overdrive.spice (verbatim) ----", TESTBENCH.read_text()]
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
            xchk_count=xchk_count, rederived=(n_ok, len(results), rbad),
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    s = summarise(results)
    print(f"  worst recovery {fmt_rec(s.worst_rec[0])}{' ns' if math.isfinite(s.worst_rec[0]) else ''} (worse polarity) at "
          f"{g.fmt_key(s.worst_rec[1]) if s.worst_rec[1] else 'n/a'}")
    for name in ("high", "low"):
        v, k = s.worst_rec_phase[name]
        print(f"  worst {name} recovery {fmt_rec(v)}{' ns' if math.isfinite(v) else ''} at {g.fmt_key(k) if k else 'n/a'}")
    print(f"  worst recovery overshoot {_fmt(s.worst_os[0])} mV; latched {len(s.latched)}; invalid {s.n_invalid}")
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
