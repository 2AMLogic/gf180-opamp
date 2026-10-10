#!/usr/bin/env python3
"""Input-referred noise of the committed sized schematic across the full
ratified PVT grid (issue #46; tracker #7; DR-3 residual (e1)).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_noise.spice`) declares no transistor and is the gain
bench's closed-DC-loop / open-AC-loop arrangement (CL = 2 pF, Ibias = 10 uA),
so the AC source on `vinp` IS the amplifier's differential input and ngspice's
`inoise` (output noise divided by the gain from that source) is the
differential-input-referred noise.

What it runs
------------
The 45-point grid `process {typical, ff, ss, fs, sf} x temperature {-40, 27,
125 C} x supply {2.97, 3.30, 3.63 V}` is ONE `klt sim` request (a `corners`
block), one `.noise` sweep (0.1 Hz - 10 MHz, 20 points/decade) per point.
Which backend executes it is `klt`'s decision (`--backend`, the request's
`backend`, or `$KLT_SIM_BACKEND`; the Spot batch fleet on a dispatch worker).
This script never launches an ngspice grid itself and never falls back to a
local grid when a batch submit fails -- it stops with the error and writes no
record. (Only single-unit controls run locally.)

`klt sim` writes the rawfile of the *current* plot, which after `.noise` is
the integrated-total plot, not the spectrum. The request therefore appends two
ngspice commands to the analysis line (`klt` places `analysis.args` verbatim
in its `.control` block): `print noise2.inoise_total noise2.onoise_total`
(ngspice's own integrated totals land in the retained log) and
`setplot noise1` (so klt's `write` dumps the spectrum plot).

Per point it extracts, from the retained spectrum:

  * input-referred density at 10 Hz, 100 Hz, 1 kHz, 10 kHz, 100 kHz;
  * integrated rms over 100 Hz - 1 MHz (the sg13g2-opamp twin precedent) and
    three alternative bands, by EXACT piecewise power-law integration of the
    density^2 between sweep points (log-log linear interpolation; exact for
    any 1/f^a and flat segment, so independent of the sweep density);
  * thermal floor and 1/f corner from a fit  S(f) = Sw + K / f^a  of the
    density^2 over 1 Hz - 10 MHz (relative-error weights; `a` scanned
    0.5..1.5 with linear least squares for Sw, K). The corner is the
    frequency where the 1/f power equals the thermal power, (K/Sw)^(1/a).

Validation, each blocking the record when violated:

  * sweep sanity (finite, positive, monotonic, full grid, >= 20 pts/decade);
  * integral of the output spectrum vs ngspice's `onoise_total` (<= 0.5 %);
  * input reference: onoise/inoise vs the gain bench's committed |vout/vdiff|
    at the same points (<= 0.05 dB) -- proves the input reference is the
    differential input with the loop open;
  * ngspice `inoise_total` vs our integral at the same sweep (<= 8 %: ngspice
    integrates the input-referred total with a sweep-density-dependent rule,
    shown to converge to our value in the dense nominal control);
  * dense nominal control (200 pts/dec, local single unit) vs the 20 pts/dec
    grid result (<= 0.5 % on the primary band) and vs ngspice inoise_total
    (<= 1 %);
  * feedback-isolation study (Lfb = Cfb in 1e8, 1e10): spot densities and the
    primary-band rms must not move (<= 0.5 %).

The result is MEASURED: no pass or fail verdict is issued because the noise
row's bound and integration band are not ratified. `spec/target-spec.md` is
never edited.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<process>_<T>c_<vdd>v.{log,dat,cir}   one triple per point
    corners/<rid>/klt-report.json                         sanitised klt report
    corners/<rid>/controls/...                            dense + isolation controls
    netlist-snapshots/<rid>.spice                         DUT + testbench + conditions
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/noise/run_noise.py                  # full grid + record
    python3 sim/noise/run_noise.py --smoke          # one nominal local point, no record
    python3 sim/noise/run_noise.py --backend local  # force a backend

Exit status: 0 when the evidence is complete and validated; 2 when the grid
could not run or failed validation (no record is written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
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
    find_pdk,
    klt_version,
    load_dut_text,
    ngspice_version,
    remote_of,
    run_klt,
    run_klt_retrying,
    load_sibling,
    sanitise_report,
    stage_workdir,
)

# The gain driver owns the committed-DUT guards, the request shape and the grid
# bookkeeping (the klt wrapper itself is in `harness`); reuse them unchanged so both benches stay structurally identical.
G = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")

TESTBENCH = HERE / "testbench" / "tb_noise.spice"

# One source for everything the fingerprint covers (issue #89).
mc = load_sibling("noise_measurement_config", "sim/noise/measurement_config.py")
CORNERS = mc.CORNERS
TEMPS_C = mc.TEMPS_C
SUPPLIES_V = mc.SUPPLIES_V
NOMINAL = G.NOMINAL
Key = G.Key
fmt_key = G.fmt_key
point_key = G.point_key
point_stem = G.point_stem
expected_keys = G.expected_keys

# --------------------------------------------------------------------------
# Sweep, spot frequencies, bands
# --------------------------------------------------------------------------

F_START, F_STOP, PPD = mc.F_START, mc.F_STOP, mc.PPD
DENSE_PPD = mc.DENSE_PPD
SPOT_HZ = mc.SPOT_HZ
BANDS = mc.BANDS
PRIMARY = mc.PRIMARY
FIT_LO, FIT_HI = mc.FIT_LO, mc.FIT_HI
FIT_RESID_MAX = 0.05  # rms relative residual of S for the floor/corner fit to be reported
FLICKER_VISIBLE = 10.0  # 1/f power at 1 Hz must exceed this multiple of the floor

# Validation tolerances.
TOL_ONOISE_REL = 0.005
TOL_GAIN_DB = 0.05
TOL_INOISE_GRID_REL = 0.08
TOL_DENSE_VS_GRID_REL = 0.005
TOL_DENSE_INOISE_REL = 0.01
TOL_ISO_REL = 0.005
MIN_PPD = 20
ISOLATION_VALUES = mc.ISOLATION_VALUES
ANALYSIS_TAIL = mc.ANALYSIS_TAIL
analysis_args = mc.analysis_args


# --------------------------------------------------------------------------
# Testbench materialisation (guards shared with the gain bench)
# --------------------------------------------------------------------------

_PARAM_RE = re.compile(r"^\.param\s+lfb=\S+\s+cfb=\S+\s*$", re.M)


def guard_testbench(text: str) -> list[str]:
    errs = G.guard_testbench(text)
    code = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("*")]
    if not any(re.match(r"^Vcm\s+vinp\s+0\s+dc\s+\S+\s+ac\s+1\s*$", ln, re.I) for ln in code):
        errs.append("testbench must drive the differential input with `Vcm vinp 0 dc ... ac 1`")
    if not any(re.match(r"^Lfb\s+vout\s+vinn\s+\{lfb\}\s*$", ln, re.I) for ln in code):
        errs.append("testbench lost the Lfb DC feedback inductor (vout -> vinn)")
    if not any(re.match(r"^Cfb\s+vinn\s+0\s+\{cfb\}\s*$", ln, re.I) for ln in code):
        errs.append("testbench lost the Cfb AC ground on vinn")
    if not any(re.match(r"^CL\s+vout\s+0\s+2p\s*$", ln, re.I) for ln in code):
        errs.append("testbench lost CL = 2 pF on vout")
    if not any(re.match(r"^Ibias\s+vdd\s+ibias\s+dc\s+10u\s*$", ln, re.I) for ln in code):
        errs.append("testbench lost Ibias = 10 uA")
    return errs


def materialise(work: Path, pdk: Pdk, *, lfb: float | None = None, dut_text: str | None = None) -> Path:
    tb = stage_workdir(work, pdk, TESTBENCH.read_text(), guard_tb=guard_testbench,
                       guard_dut=G.guard_dut, dut_text=dut_text)
    if lfb is not None:
        tb, n = _PARAM_RE.subn(f".param lfb={lfb:g} cfb={lfb:g}", tb)
        if n != 1:
            raise RuntimeError("testbench .param lfb/cfb line not found exactly once")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt request
# --------------------------------------------------------------------------


def noise_request(netlist: Path, pdk: Pdk, corners, temps, supplies, *, ppd: int = PPD) -> dict:
    return {
        "netlist": str(netlist),
        "engine": "ngspice",
        "models": {"pdk": pdk.variant, "lib": G.MODEL_LIB},
        "corners": {
            "process": G.process_axis(list(corners)),
            # vdd and vcm sweep together by index: VCM tracks VDD/2.
            "supply_v": {"vdd": list(supplies), "vcm": [round(v / 2, 6) for v in supplies]},
            "temperature_c": list(temps),
        },
        "analysis": {"kind": "noise", "args": analysis_args(ppd)},
        # klt supports no `.meas noise`; the cross-checks come from ngspice's
        # own totals (log) and the retained spectrum.
        "measurements": [],
        "options": {"timeout_s": 300, "keep_artifacts": True, "waveforms": True},
    }


# --------------------------------------------------------------------------
# Rawfile / log parsing
# --------------------------------------------------------------------------


@dataclass
class Spectrum:
    freq: np.ndarray
    inoise: np.ndarray  # V/sqrt(Hz), input-referred to Vcm
    onoise: np.ndarray  # V/sqrt(Hz), at vout


def parse_noise_raw(text: str) -> Spectrum:
    """Parse an ngspice ASCII real rawfile of the `noise1` spectrum plot.

    Raises ValueError on anything malformed; bad data must fail loudly.
    """
    if "Flags: real" not in text:
        raise ValueError("rawfile is not a real-valued noise spectrum")
    head, sep, body = text.partition("Values:")
    if not sep:
        raise ValueError("rawfile has no Values section")
    m = re.search(r"No\. Variables:\s*(\d+)", head)
    p = re.search(r"No\. Points:\s*(\d+)", head)
    if not m or not p:
        raise ValueError("rawfile header incomplete")
    nvar, npts = int(m.group(1)), int(p.group(1))
    names = re.findall(r"^\t\d+\t(\S+)\t", head.split("Variables:", 1)[1], re.M)
    if len(names) != nvar:
        raise ValueError(f"rawfile declares {nvar} variables, found {len(names)}")
    toks = body.split()
    if len(toks) != npts * (nvar + 1):
        raise ValueError(f"rawfile has {len(toks)} tokens, expected {npts} x {nvar + 1}")
    try:
        arr = np.array([float(t) for t in toks]).reshape(npts, nvar + 1)[:, 1:]
    except ValueError as exc:
        raise ValueError(f"unparseable rawfile value: {exc}") from exc
    if not np.all(np.isfinite(arr)):
        raise ValueError("rawfile contains non-finite values")
    cols = {n.lower(): arr[:, i] for i, n in enumerate(names)}
    for need in ("frequency", "inoise_spectrum", "onoise_spectrum"):
        if need not in cols:
            raise ValueError(f"rawfile has no {need} vector (plot is not the noise spectrum)")
    return Spectrum(cols["frequency"], cols["inoise_spectrum"], cols["onoise_spectrum"])


_TOTAL_RE = re.compile(r"^noise2\.(inoise_total|onoise_total)\s*=\s*(\S+)\s*$", re.M)


def parse_totals(log: str) -> dict[str, float]:
    """ngspice's integrated totals from the retained log."""
    out: dict[str, float] = {}
    for name, val in _TOTAL_RE.findall(log):
        try:
            out[name] = float(val)
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def interp_loglog(freq: np.ndarray, dens: np.ndarray, f: float) -> float:
    """Density at `f` by log-log linear interpolation; ValueError outside the sweep."""
    if f < freq[0] * (1 - 1e-9) or f > freq[-1] * (1 + 1e-9):
        raise ValueError(f"{f:g} Hz is outside the swept range [{freq[0]:g}, {freq[-1]:g}]")
    return float(np.exp(np.interp(math.log(f), np.log(freq), np.log(dens))))


def integrate_power(freq: np.ndarray, dens: np.ndarray, lo: float, hi: float) -> float:
    """Integral of density^2 over [lo, hi] (V^2), exact for piecewise power laws."""
    if not (freq[0] * (1 - 1e-9) <= lo < hi <= freq[-1] * (1 + 1e-9)):
        raise ValueError(f"band {lo:g}-{hi:g} Hz is outside the swept range")
    inner = (freq > lo * (1 + 1e-9)) & (freq < hi * (1 - 1e-9))
    f = np.concatenate([[lo], freq[inner], [hi]])
    d = np.array([interp_loglog(freq, dens, x) for x in f])
    s = d * d
    total = 0.0
    for f1, f2, s1, s2 in zip(f[:-1], f[1:], s[:-1], s[1:]):
        r = f2 / f1
        alpha = math.log(s2 / s1) / math.log(r)
        total += s1 * f1 * math.log(r) if abs(alpha + 1) < 1e-9 else s1 * f1 / (alpha + 1) * (r ** (alpha + 1) - 1)
    return total


def integrate_power_full(freq: np.ndarray, dens: np.ndarray) -> float:
    return integrate_power(freq, dens, float(freq[0]), float(freq[-1]))


@dataclass
class FloorFit:
    ok: bool
    note: str = ""
    sw: float = float("nan")  # thermal floor power, V^2/Hz
    k: float = float("nan")  # flicker coefficient: K / f^a, V^2/Hz at 1 Hz
    a: float = float("nan")
    corner_hz: float = float("nan")
    resid: float = float("nan")
    floor_nv: float = float("nan")
    flicker_ratio_1hz: float = float("nan")  # flicker power / thermal power at 1 Hz
    flicker_visible: bool = False


def fit_floor(freq: np.ndarray, dens: np.ndarray) -> FloorFit:
    """Fit S = Sw + K / f^a over [FIT_LO, FIT_HI]; see the module docstring."""
    m = (freq >= FIT_LO * (1 - 1e-9)) & (freq <= FIT_HI * (1 + 1e-9))
    f, s = freq[m], dens[m] ** 2
    if len(f) < 10:
        return FloorFit(False, "too few points in the fit range")
    w = 1.0 / s
    best = None
    for a in np.arange(0.5, 1.5001, 0.005):
        A = np.column_stack([np.ones_like(f), f ** (-a)]) * w[:, None]
        coef, *_ = np.linalg.lstsq(A, s * w, rcond=None)
        res = float(np.sqrt(np.mean((A @ coef / w / s - 1) ** 2)))
        if best is None or res < best[0]:
            best = (res, float(a), coef)
    res, a, (sw, k) = best
    if sw <= 0:
        return FloorFit(False, "fit gives a non-positive thermal floor", a=a, resid=res)
    if k <= 0:
        # No flicker component: the spectrum is flat; report the floor only.
        return FloorFit(res <= FIT_RESID_MAX, "no 1/f component resolved" if res <= FIT_RESID_MAX
                        else f"poor fit (rms residual {res:.1%} of S)", sw=sw, a=a, k=0.0, resid=res,
                        floor_nv=math.sqrt(sw) * 1e9, flicker_ratio_1hz=0.0)
    ratio = k / sw
    return FloorFit(
        res <= FIT_RESID_MAX,
        "" if res <= FIT_RESID_MAX else f"poor fit (rms residual {res:.1%} of S)",
        sw=sw, k=k, a=a, corner_hz=ratio ** (1.0 / a), resid=res,
        floor_nv=math.sqrt(sw) * 1e9, flicker_ratio_1hz=ratio,
        flicker_visible=ratio >= FLICKER_VISIBLE,
    )


@dataclass
class PointResult:
    spot_nv: dict[float, float]
    band_uv: dict[str, float]
    fit: FloorFit
    integrated_in_v: float
    integrated_out_v: float


def extract(spec: Spectrum) -> PointResult:
    f, di = spec.freq, spec.inoise
    return PointResult(
        spot_nv={x: interp_loglog(f, di, x) * 1e9 for x in SPOT_HZ},
        band_uv={name: math.sqrt(integrate_power(f, di, lo, hi)) * 1e6 for name, lo, hi in BANDS},
        fit=fit_floor(f, di),
        integrated_in_v=math.sqrt(integrate_power_full(f, di)),
        integrated_out_v=math.sqrt(integrate_power_full(f, spec.onoise)),
    )


def sweep_problems(spec: Spectrum, ppd: int, label: str) -> list[str]:
    f = spec.freq
    errs: list[str] = []
    n_expect = int(round(math.log10(F_STOP / F_START) * ppd)) + 1
    if len(f) != n_expect:
        errs.append(f"{label}: {len(f)} sweep points, expected {n_expect}")
    if not np.all(np.diff(f) > 0):
        errs.append(f"{label}: frequency axis not strictly increasing")
    if abs(f[0] / F_START - 1) > 1e-6 or abs(f[-1] / F_STOP - 1) > 1e-6:
        errs.append(f"{label}: sweep {f[0]:g}-{f[-1]:g} Hz is not {F_START:g}-{F_STOP:g} Hz")
    if np.any(spec.inoise <= 0) or np.any(spec.onoise <= 0):
        errs.append(f"{label}: non-positive noise density")
    pts_per_dec = (len(f) - 1) / math.log10(f[-1] / f[0]) if len(f) > 1 else 0
    if pts_per_dec < MIN_PPD - 0.5:
        errs.append(f"{label}: only {pts_per_dec:.1f} points per decade")
    return errs


# --------------------------------------------------------------------------
# Per-point validation
# --------------------------------------------------------------------------


def gain_db_from_gain_bench(gdir: Path, k: Key, freq: np.ndarray) -> np.ndarray | None:
    d = G.load_gain_bench(gdir, k)
    if d is None:
        return None
    if d.shape[0] < len(freq) or np.any(np.abs(d[: len(freq), 0] / freq - 1) > 1e-6):
        return None
    return 20 * np.log10(np.abs(d[: len(freq), 1] + 1j * d[: len(freq), 2]))


def gain_crosscheck(k: Key, spec: Spectrum, gdir: Path | None) -> tuple[float | None, list[str]]:
    """Max |20log(onoise/inoise) - gain-bench dB| over the sweep, and problems."""
    ours = 20 * np.log10(spec.onoise / spec.inoise)
    if gdir is None:
        return None, []
    ref = gain_db_from_gain_bench(gdir, k, spec.freq)
    if ref is None:
        return None, [f"{fmt_key(k)}: gain-bench data for the input-reference check missing or on another grid"]
    dev = float(np.max(np.abs(ours - ref)))
    if dev > TOL_GAIN_DB:
        return dev, [f"{fmt_key(k)}: onoise/inoise differs from the gain bench by up to {dev:.3f} dB (> {TOL_GAIN_DB} dB)"]
    return dev, []


def totals_crosscheck(k: Key, res: PointResult, totals: dict[str, float]) -> tuple[dict, list[str]]:
    bad: list[str] = []
    info: dict = {}
    on, inn = totals.get("onoise_total"), totals.get("inoise_total")
    if on is None or inn is None:
        return info, [f"{fmt_key(k)}: ngspice integrated totals missing from the log"]
    info["onoise_rel"] = res.integrated_out_v / on - 1
    info["inoise_rel"] = res.integrated_in_v / inn - 1
    if abs(info["onoise_rel"]) > TOL_ONOISE_REL:
        bad.append(f"{fmt_key(k)}: our integral of the output spectrum differs from ngspice onoise_total by "
                   f"{info['onoise_rel']:+.2%} (> {TOL_ONOISE_REL:.1%})")
    if abs(info["inoise_rel"]) > TOL_INOISE_GRID_REL:
        bad.append(f"{fmt_key(k)}: our integral of the input spectrum differs from ngspice inoise_total by "
                   f"{info['inoise_rel']:+.2%} (> {TOL_INOISE_GRID_REL:.0%})")
    return info, bad


def analyse_report(report: dict, want: list[Key], gdir: Path | None):
    """Per-point results + artifacts from a klt report; list of failure strings."""
    problems: list[str] = []
    results: dict[Key, PointResult] = {}
    arts: dict[Key, dict] = {}
    seen: dict[Key, dict] = {}
    for c in report.get("corners", []):
        k = point_key(c)
        if k in seen:
            problems.append(f"duplicate result for {fmt_key(k)}")
        seen[k] = c
    for k in want:
        c = seen.get(k)
        if c is None:
            problems.append(f"missing result for {fmt_key(k)}")
            continue
        diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
        a = c.get("artifacts") or {}
        raw, logp = a.get("raw"), a.get("log")
        if not raw or not Path(raw).is_file():
            problems.append(f"simulation failed for {fmt_key(k)}: {diag or 'no rawfile retained'}")
            continue
        if not logp or not Path(logp).is_file():
            problems.append(f"no ngspice log retained for {fmt_key(k)}")
            continue
        try:
            spec = parse_noise_raw(Path(raw).read_text())
            res = extract(spec)
        except ValueError as exc:
            problems.append(f"malformed data for {fmt_key(k)}: {exc}")
            continue
        sp = sweep_problems(spec, PPD, fmt_key(k))
        totals = parse_totals(Path(logp).read_text())
        tinfo, tbad = totals_crosscheck(k, res, totals)
        gdev, gbad = gain_crosscheck(k, spec, gdir)
        problems += sp + tbad + gbad
        results[k] = res
        arts[k] = {"spec": spec, "log": logp, "deck": a.get("deck"), "totals": totals,
                   "tinfo": tinfo, "gain_dev_db": gdev}
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"unexpected extra points: {sorted(extra)}")
    return results, arts, problems


# --------------------------------------------------------------------------
# PDK flicker-model audit
# --------------------------------------------------------------------------

AUDIT_MODELS = ("nfet_03v3", "pfet_03v3")


@dataclass
class ModelAudit:
    lib: str
    revision: str
    sections_with_noise_corner: dict[str, bool]
    fnoicor: str
    models: dict[str, dict]  # prefix -> {n, fnoimod, ef, noia, noib, noic}
    noise_params: dict[str, str]


def audit_pdk(pdk: Pdk) -> ModelAudit:
    """Read the pinned model library: do the MOS models carry flicker noise?"""
    lines = pdk.model_lib.read_text().splitlines()
    sections: dict[str, bool] = {}
    cur: str | None = None
    for ln in lines:
        m = re.match(r"^\s*\.lib\s+(\w+)\s*$", ln, re.I)
        if m:
            cur = m.group(1).lower()
            sections.setdefault(cur, False)
            continue
        if re.match(r"^\s*\.endl\b", ln, re.I):
            cur = None
            continue
        if cur and re.match(r"^\s*\.lib\s+'sm141064\.ngspice'\s+noise_corner\s*$", ln, re.I):
            sections[cur] = True
    noise_params: dict[str, str] = {}
    in_nc = False
    for ln in lines:
        if re.match(r"^\s*\.lib\s+noise_corner\s*$", ln, re.I):
            in_nc = True
            continue
        if in_nc and re.match(r"^\s*\.endl\b", ln, re.I):
            break
        m = re.match(r"^\s*\+?\s*((?:nfet|pfet)_03v3_no\w+)\s*=\s*'(.*)'", ln)
        if in_nc and m:
            noise_params[m.group(1)] = m.group(2).strip()
    models: dict[str, dict] = {}
    cur_model: str | None = None
    for ln in lines:
        m = re.match(r"^\.model\s+((?:nfet|pfet)_03v3)\.\d+\s", ln, re.I)
        if m:
            cur_model = m.group(1)
            d = models.setdefault(cur_model, {"n": 0, "fnoimod": set(), "ef": set(), "noia": set(), "noib": set(), "noic": set()})
            d["n"] += 1
            continue
        if re.match(r"^\.(model|lib|endl|subckt)\b", ln, re.I):
            cur_model = None
            continue
        if cur_model:
            m = re.match(r"^\+\s*(fnoimod|ef|noia|noib|noic)\s*=\s*(\S+)", ln, re.I)
            if m:
                models[cur_model][m.group(1).lower()].add(m.group(2))
    design = pdk.design_include.read_text()
    fm = re.search(r"^\+\s*fnoicor\s*=\s*(\S+)", design, re.M)
    models = {k: {kk: (sorted(vv) if isinstance(vv, set) else vv) for kk, vv in v.items()} for k, v in models.items()}
    return ModelAudit(
        lib=str(pdk.model_lib.name), revision=pdk.version,
        sections_with_noise_corner={s: sections.get(s, False) for s in CORNERS},
        fnoicor=fm.group(1) if fm else "unknown",
        models=models, noise_params=noise_params,
    )


# --------------------------------------------------------------------------
# Local single-unit controls
# --------------------------------------------------------------------------


@dataclass
class ControlRun:
    name: str
    desc: str
    spec: Spectrum | None = None
    res: PointResult | None = None
    totals: dict = field(default_factory=dict)
    error: str = ""
    files: dict = field(default_factory=dict)
    report: dict | None = None


def run_local_control(name: str, desc: str, pdk: Pdk, work: Path, *, ppd: int = PPD, lfb: float | None = None) -> ControlRun:
    """One nominal-corner noise unit, run LOCALLY through `klt sim`."""
    run = ControlRun(name, desc)
    try:
        wd = work / name
        tb = materialise(wd, pdk, lfb=lfb)
        req = noise_request(tb, pdk, [NOMINAL[0]], [NOMINAL[1]], [NOMINAL[2]], ppd=ppd)
        rep = run_klt(req, wd / "out", "local", wd)
        run.report = rep
        cs = rep.get("corners", [])
        if len(cs) != 1:
            run.error = f"expected one corner, got {len(cs)}"
            return run
        a = cs[0].get("artifacts") or {}
        if not a.get("raw") or not Path(a["raw"]).is_file():
            run.error = "no rawfile retained"
            return run
        run.spec = parse_noise_raw(Path(a["raw"]).read_text())
        run.res = extract(run.spec)
        run.totals = parse_totals(Path(a["log"]).read_text()) if a.get("log") else {}
        run.files = {"log": a.get("log"), "deck": a.get("deck")}
        run.error = "; ".join(sweep_problems(run.spec, ppd, name))
    except (KltError, ValueError, RuntimeError) as exc:
        run.error = str(exc)
    return run


def run_controls(pdk: Pdk, work: Path) -> tuple[ControlRun, list[ControlRun]]:
    dense = run_local_control("dense", f"nominal point, {DENSE_PPD} points/decade", pdk, work, ppd=DENSE_PPD)
    iso = [run_local_control(f"iso_lfb{v:g}", f"nominal point, Lfb = Cfb = {v:g}", pdk, work, lfb=v)
           for v in ISOLATION_VALUES]
    return dense, iso


def control_failures(dense: ControlRun, iso: list[ControlRun], nominal: PointResult | None) -> list[str]:
    bad: list[str] = []
    pname = BANDS[PRIMARY][0]
    if dense.error or dense.res is None:
        bad.append(f"dense control failed: {dense.error}")
    else:
        if nominal is not None:
            d = dense.res.band_uv[pname] / nominal.band_uv[pname] - 1
            if abs(d) > TOL_DENSE_VS_GRID_REL:
                bad.append(f"dense control moves the {pname} rms by {d:+.2%} vs the {PPD} pts/dec grid (> {TOL_DENSE_VS_GRID_REL:.1%})")
        inn = dense.totals.get("inoise_total")
        if inn is None:
            bad.append("dense control: ngspice inoise_total missing")
        elif abs(dense.res.integrated_in_v / inn - 1) > TOL_DENSE_INOISE_REL:
            bad.append(f"dense control: our integral differs from ngspice inoise_total by "
                       f"{dense.res.integrated_in_v / inn - 1:+.2%} (> {TOL_DENSE_INOISE_REL:.0%})")
    for r in iso:
        if r.error or r.res is None:
            bad.append(f"isolation control {r.name} failed: {r.error}")
            continue
        if nominal is None:
            continue
        for x in SPOT_HZ:
            d = r.res.spot_nv[x] / nominal.spot_nv[x] - 1
            if abs(d) > TOL_ISO_REL:
                bad.append(f"{r.name}: density at {x:g} Hz moves {d:+.2%} (> {TOL_ISO_REL:.1%})")
        d = r.res.band_uv[pname] / nominal.band_uv[pname] - 1
        if abs(d) > TOL_ISO_REL:
            bad.append(f"{r.name}: {pname} rms moves {d:+.2%} (> {TOL_ISO_REL:.1%})")
    return bad


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------


def build_plots(arts: dict[Key, dict], results: dict[Key, PointResult], plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    fig, ax = plt.subplots(figsize=(8, 5))
    for k, a in arts.items():
        s = a["spec"]
        ax.loglog(s.freq, s.inoise * 1e9, lw=0.6, alpha=0.5, color="0.6")
    for c in CORNERS:
        k = (c, 27.0, 3.30)
        if k in arts:
            s = arts[k]["spec"]
            ax.loglog(s.freq, s.inoise * 1e9, lw=1.4, label=f"{c} 27C 3.30V")
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("input-referred noise (nV/rtHz)")
    ax.set_title("Input-referred noise density: 45 points (grey), 27 C / 3.30 V corners (colour)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = plot_dir / "noise-density.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    out.append(p.name)

    fig, ax = plt.subplots(figsize=(8, 4))
    names = [b[0] for b in BANDS]
    for i, n in enumerate(names):
        vals = [r.band_uv[n] for r in results.values()]
        ax.scatter([i] * len(vals), vals, s=12, alpha=0.6)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=8)
    ax.set_ylabel("integrated input-referred rms (uV)")
    ax.set_title("Integrated rms per band, all 45 points")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = plot_dir / "noise-bands.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    out.append(p.name)
    return out


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def worst_best(results: dict[Key, PointResult], getter):
    items = [(getter(r), k) for k, r in results.items() if math.isfinite(getter(r))]
    return max(items), min(items)


def build_record(*, record, stamp, pdk, ngspice, kver, report, results, arts, audit: ModelAudit,
                 dense: ControlRun, iso: list[ControlRun], control_bad: list[str], plots, dut_sha,
                 gdir: Path | None, wall_s: float) -> str:
    L: list[str] = []
    add = L.append
    remote = remote_of(report)
    nom = results.get(NOMINAL)
    add(f"# Noise record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; commit `{record.rsplit('-', 1)[-1]}`; issue #46")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include `{dut_sha[:16]}`), unchanged")
    for ln in mc.fingerprint_lines({"bench": TESTBENCH.read_text()}):
        add(ln)
    add(f"- **PDK**: {pdk.variant} (open_pdks `{pdk.version}`); tools: ngspice local `{ngspice}`, klt `{kver}`")
    if remote:
        add(f"- **Execution**: `klt sim` backend `{remote.get('provider')}`, job `{remote.get('job_id', remote.get('job'))}`, "
            f"{'Spot ' if remote.get('spot') else ''}{remote.get('instance_type', '')}; "
            f"runner klt `{remote.get('runner_klt_version')}` vs client `{remote.get('client_klt_version')}`")
    else:
        add("- **Execution**: `klt sim` local backend (no `environment.remote` in the report)")
    add(f"- **Request**: the {len(results)}-point grid (5 MOS corners x 3 T x 3 VDD, vcm = VDD/2) as ONE `klt sim` "
        f"request, `.noise v(vout) Vcm dec {PPD} {F_START:g} {F_STOP:g}`, wall time {wall_s:.0f} s (client-side)")
    add("- **Passive sections**: every corner uses `res_typical` + `mimcap_typical`; RZ/CC passive spread is NOT swept.")
    add("")
    add("## Claim")
    add("")
    add("**Measured, no pass or fail verdict: the row's bound is not ratified** (DR-3 residual (e1)); neither is the "
        "integration band. The bands below are candidates so the choice can be argued from data. "
        "`spec/target-spec.md` is untouched.")
    add("")
    fit_ok = [r.fit for r in results.values() if r.fit.ok]
    vis = [r.fit.flicker_visible for r in results.values()]
    add("## Flicker (1/f) noise is modelled and visible")
    add("")
    if all(vis):
        add(f"**Not thermal-only.** The 1/f term exceeds the thermal floor by >= {FLICKER_VISIBLE:g}x at 1 Hz at all "
            f"{len(vis)} points; the fitted 1/f corner and floor are reported below. A figure rated against the row "
            "may therefore use the flicker-inclusive numbers.")
    else:
        add(f"**WARNING: at {len(vis) - sum(vis)} of {len(vis)} points no 1/f rise is resolved (flicker power at 1 Hz < "
            f"{FLICKER_VISIBLE:g}x the floor).** Where that holds the figure is thermal-only and does not rate the row "
            "(DR-2 section (c)).")
    add("")
    add("### Model audit (pinned library, read by the driver)")
    add("")
    add(f"- Library `{audit.lib}`, open_pdks `{audit.revision}`; `fnoicor = {audit.fnoicor}` in `design.ngspice` "
        "(0 = as-extracted, 1 = worst-case flicker).")
    add("- Corner sections that pull in `.lib noise_corner`: "
        + ", ".join(f"`{s}` {'yes' if v else '**NO**'}" for s, v in audit.sections_with_noise_corner.items()) + ".")
    for k, v in sorted(audit.noise_params.items()):
        add(f"  - `{k}` = `{v}`")
    add("")
    add("| model | .model cards | fnoimod | ef | noia | noib | noic |")
    add("|---|---|---|---|---|---|---|")
    for name, d in sorted(audit.models.items()):
        add(f"| `{name}` | {d['n']} | {', '.join(d['fnoimod'])} | {', '.join(d['ef'])} | "
            f"{', '.join(d['noia'])} | {', '.join(d['noib'])} | {', '.join(d['noic'])} |")
    add("")
    add("`fnoimod = 1` selects the BSIM4 unified flicker model (`noia/noib/noic`, exponent `ef`), so MOS flicker noise "
        "is part of every `.noise` run; thermal noise is on by default and the resistor RZ contributes thermal noise only.")
    add("")
    add("## Nominal point (typical, 27 C, 3.30 V)")
    add("")
    if nom:
        add("| quantity | value |")
        add("|---|---|")
        for x in SPOT_HZ:
            add(f"| density @ {x:g} Hz | {nom.spot_nv[x]:.2f} nV/rtHz |")
        for name, _, _ in BANDS:
            add(f"| rms {name} | {nom.band_uv[name]:.3f} uV |")
        f = nom.fit
        add(f"| thermal floor | {f.floor_nv:.2f} nV/rtHz |")
        add(f"| 1/f corner | {f.corner_hz / 1e3:.1f} kHz |" if math.isfinite(f.corner_hz) else "| 1/f corner | n/a |")
    add("")
    add("## Spot densities (nV/rtHz, input-referred) -- all points")
    add("")
    add("| point | " + " | ".join(f"{x:g} Hz" for x in SPOT_HZ) + " |")
    add("|---|" + "---|" * len(SPOT_HZ))
    for k in sorted(results):
        r = results[k]
        add(f"| {fmt_key(k)} | " + " | ".join(f"{r.spot_nv[x]:.2f}" for x in SPOT_HZ) + " |")
    add("")
    add("## Integrated input-referred rms (uV) -- all points")
    add("")
    add("Exact piecewise power-law integration of density^2 between sweep points.")
    add("")
    add("| point | " + " | ".join(b[0] for b in BANDS) + " |")
    add("|---|" + "---|" * len(BANDS))
    for k in sorted(results):
        r = results[k]
        add(f"| {fmt_key(k)} | " + " | ".join(f"{r.band_uv[b[0]]:.3f}" for b in BANDS) + " |")
    add("")
    add("Spread across the grid (min .. max):")
    add("")
    for name, _, _ in BANDS:
        (hi, khi), (lo, klo) = worst_best(results, lambda r, n=name: r.band_uv[n])
        add(f"- {name}: {lo:.3f} uV ({fmt_key(klo)}) .. {hi:.3f} uV ({fmt_key(khi)}); max/min = {hi / lo:.2f}")
    add("")
    add("## Thermal floor and 1/f corner")
    add("")
    add("Method: least-squares fit of S(f) = Sw + K / f^a to the density^2 over "
        f"{FIT_LO:g} Hz - {FIT_HI:g} Hz with relative-error weights (exponent `a` scanned 0.5..1.5, step 0.005, "
        "Sw and K from linear least squares). Thermal floor = sqrt(Sw); corner = (K/Sw)^(1/a), where the 1/f power equals the "
        f"thermal power. A fit is reported only when its rms relative residual of S is <= {FIT_RESID_MAX:.0%}.")
    add("")
    add("| point | floor (nV/rtHz) | corner (kHz) | a | K/Sw at 1 Hz | fit resid |")
    add("|---|---|---|---|---|---|")
    for k in sorted(results):
        f = results[k].fit
        if f.ok and math.isfinite(f.corner_hz):
            add(f"| {fmt_key(k)} | {f.floor_nv:.2f} | {f.corner_hz / 1e3:.1f} | {f.a:.3f} | {f.flicker_ratio_1hz:.3g} | {f.resid:.2%} |")
        else:
            add(f"| {fmt_key(k)} | n/a | n/a | {f.a:.3f} | - | {f.resid:.2%} ({f.note}) |")
    if fit_ok:
        c = [f.corner_hz for f in fit_ok if math.isfinite(f.corner_hz)]
        fl = [f.floor_nv for f in fit_ok]
        if c:
            add("")
            add(f"Over {len(fit_ok)} fitted points: floor {min(fl):.2f} .. {max(fl):.2f} nV/rtHz; "
                f"corner {min(c) / 1e3:.1f} .. {max(c) / 1e3:.1f} kHz.")
    add("")
    add("## Extraction validation")
    add("")
    on_dev = [abs(a["tinfo"]["onoise_rel"]) for a in arts.values() if "onoise_rel" in a["tinfo"]]
    in_dev = [a["tinfo"]["inoise_rel"] for a in arts.values() if "inoise_rel" in a["tinfo"]]
    gdev = [a["gain_dev_db"] for a in arts.values() if a["gain_dev_db"] is not None]
    add(f"- **Output total**: our integral of `onoise_spectrum` vs ngspice `onoise_total` (printed in each point's log): "
        f"max |dev| = {max(on_dev):.3%} (tolerance {TOL_ONOISE_REL:.1%}).")
    add(f"- **Input total**: our integral of `inoise_spectrum` vs ngspice `inoise_total`: "
        f"{min(in_dev):+.2%} .. {max(in_dev):+.2%} at {PPD} pts/dec (tolerance {TOL_INOISE_GRID_REL:.0%}). "
        "ngspice integrates the input-referred total with a rule that depends on the sweep density; it converges "
        "to our value when the sweep is refined (dense control below), so our integral is the reported one.")
    if gdev:
        add(f"- **Input reference**: 20 log10(onoise/inoise) from the noise run vs the gain bench's committed "
            f"|vout/vdiff| (`sim/gain-gbw-pm/corners/{gdir.name}`) at the same frequencies, all {len(gdev)} points: "
            f"max deviation {max(gdev):.4f} dB (tolerance {TOL_GAIN_DB} dB). The noise is therefore referred to the "
            "differential input with the loop open, as in the gain bench.")
    else:
        add("- **Input reference**: gain-bench data not available; NOT CHECKED.")
    add(f"- **Sweep**: {PPD} points/decade over {F_START:g} Hz - {F_STOP:g} Hz (161 points); every band edge lies on "
        "or is log-log interpolated between sweep points.")
    add("")
    add("### Controls (local single units, nominal point)")
    add("")
    pname = BANDS[PRIMARY][0]
    if dense.res and nom:
        inn = dense.totals.get("inoise_total")
        add(f"- **Dense sweep** ({DENSE_PPD} pts/dec): {pname} rms {dense.res.band_uv[pname]:.4f} uV vs "
            f"{nom.band_uv[pname]:.4f} uV on the grid ({dense.res.band_uv[pname] / nom.band_uv[pname] - 1:+.3%}); "
            f"our full-range integral vs ngspice `inoise_total`: "
            f"{(dense.res.integrated_in_v / inn - 1) if inn else float('nan'):+.3%} "
            f"(grid: {arts[NOMINAL]['tinfo'].get('inoise_rel', float('nan')):+.2%}).")
    else:
        add(f"- **Dense sweep**: FAILED ({dense.error})")
    for r in iso:
        if r.res and nom:
            d = max(abs(r.res.spot_nv[x] / nom.spot_nv[x] - 1) for x in SPOT_HZ)
            add(f"- **Isolation** `{r.desc}`: max spot-density change {d:.3%}, {pname} rms change "
                f"{r.res.band_uv[pname] / nom.band_uv[pname] - 1:+.3%}.")
        else:
            add(f"- **Isolation** `{r.desc}`: FAILED ({r.error})")
    for b in control_bad:
        add(f"- CONTROL PROBLEM: {b}")
    add("")
    add("## Reproduce")
    add("")
    add("```")
    add("python3 sim/noise/run_noise.py            # full grid + a new record")
    add("python3 sim/noise/run_noise.py --smoke    # one nominal local point")
    add("```")
    add("")
    add("Evidence: `corners/" + record + "/` (per point `.dat` = freq, inoise, onoise; ngspice `.log` with the "
        "integrated totals; klt-generated `.cir` deck), `netlist-snapshots/" + record + ".spice`.")
    add("")
    L.extend(mc.inputs_section({"bench": TESTBENCH.read_text()}))
    for p in plots:
        add(f"![{p}]({record}-plots/{p})")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="noise-smoke-") as scratch:
        run = run_local_control("smoke", "nominal", pdk, Path(scratch))
    if run.error or run.res is None:
        print(f"SMOKE TEST FAILED: {run.error}")
        return 1
    r = run.res
    inn, on = run.totals.get("inoise_total"), run.totals.get("onoise_total")
    if on is None or abs(r.integrated_out_v / on - 1) > TOL_ONOISE_REL:
        print(f"SMOKE TEST FAILED: output-total cross-check (ours {r.integrated_out_v:g}, ngspice {on})")
        return 1
    print("  density: " + ", ".join(f"{x:g} Hz {r.spot_nv[x]:.1f} nV" for x in SPOT_HZ))
    print("  rms: " + ", ".join(f"{n} {v:.2f} uV" for n, v in r.band_uv.items()))
    print("smoke test OK (extraction cross-checked; the full run records the evidence)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one nominal point, local, no record")
    ap.add_argument("--backend", help="klt execution backend for the 45-point grid "
                    "(default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None,
                    help="forward batch.runner_version_check (only meaningful on the batch backend)")
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None,
                    help="forward batch.capacity_wait_s")
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet capacity "
                    "(never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    import time

    want = expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    for sub in (HERE / "records" / f"{record}.md", HERE / "corners" / record,
                HERE / "netlist-snapshots" / f"{record}.spice"):
        if sub.exists():
            print(f"ERROR: {sub} already exists; evidence is append-only", file=sys.stderr)
            return 2
    ngspice, kver = ngspice_version(), klt_version()
    gdir = G.latest_gain_dir()
    print(f"record {record}: {len(want)} points, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    with tempfile.TemporaryDirectory(prefix="noise-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        req = noise_request(tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
        req["batch"] = batch_block(args)
        t0 = time.time()
        try:
            report = run_klt_retrying(
                req, work / "grid" / "out", args.backend, work / "grid",
                retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
            )
        except KltError as exc:
            print(f"ERROR: the grid request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        wall = time.time() - t0
        results, arts, problems = analyse_report(report, want, gdir)
        if problems:
            print("ERROR: the 45-point grid did not complete cleanly; NO RECORD WRITTEN:", file=sys.stderr)
            for p in problems[:40]:
                print(f"  - {p}", file=sys.stderr)
            return 2

        dense, iso = run_controls(pdk, work)
        control_bad = control_failures(dense, iso, results.get(NOMINAL))
        if control_bad:
            print("ERROR: validation controls failed; NO RECORD WRITTEN:", file=sys.stderr)
            for p in control_bad:
                print(f"  - {p}", file=sys.stderr)
            return 2
        audit = audit_pdk(pdk)

        corners_dir = HERE / "corners" / record
        corners_dir.mkdir(parents=True, exist_ok=False)
        for k, a in arts.items():
            stem = point_stem(k)
            shutil.copyfile(a["log"], corners_dir / f"{stem}.log")
            if a["deck"]:
                shutil.copyfile(a["deck"], corners_dir / f"{stem}.cir")
            s = a["spec"]
            np.savetxt(corners_dir / f"{stem}.dat", np.column_stack([s.freq, s.inoise, s.onoise]),
                       header="freq_hz inoise_V_per_rtHz onoise_V_per_rtHz")
        (corners_dir / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))
        cdir = corners_dir / "controls"
        cdir.mkdir()
        for r in [dense, *iso]:
            for kind, src in r.files.items():
                if src and Path(src).is_file():
                    shutil.copyfile(src, cdir / f"{r.name}.{kind}" if kind != "deck" else cdir / f"{r.name}.cir")
            if r.spec is not None:
                np.savetxt(cdir / f"{r.name}.dat", np.column_stack([r.spec.freq, r.spec.inoise, r.spec.onoise]),
                           header="freq_hz inoise_V_per_rtHz onoise_V_per_rtHz")

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        snap = HERE / "netlist-snapshots"
        snap.mkdir(parents=True, exist_ok=True)
        deck0 = ""
        first = arts.get(NOMINAL)
        if first and first["deck"]:
            deck0 = Path(first["deck"]).read_text()
        (snap / f"{record}.spice").write_text(
            "\n".join(
                [
                    f"* netlist snapshot for record {record} (issue #46)",
                    "* Reproduces the measured design: DUT contents, testbench, conditions.",
                    "* ---- conditions: klt sim request (grid) ----",
                    *("* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
                    "",
                    "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised "
                    "(file opamp_two_stage.dut.spice) ----",
                    dut_text,
                    "* ---- testbench: sim/noise/testbench/tb_noise.spice (verbatim) ----",
                    TESTBENCH.read_text(),
                    "* ---- klt-generated deck of the nominal point (corner.cir) ----",
                    *("* | " + ln for ln in deck0.splitlines()),
                    "",
                ]
            )
        )

        plots = build_plots(arts, results, HERE / "records" / f"{record}-plots")
        md = build_record(record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, report=report,
                          results=results, arts=arts, audit=audit, dense=dense, iso=iso,
                          control_bad=control_bad, plots=plots, dut_sha=dut_sha, gdir=gdir, wall_s=wall)
        out = HERE / "records" / f"{record}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)

    print(f"wrote {out}")
    nom = results.get(NOMINAL)
    if nom:
        print("  nominal: " + ", ".join(f"{n} {v:.2f} uV" for n, v in nom.band_uv.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
