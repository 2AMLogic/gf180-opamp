#!/usr/bin/env python3
"""Aggregate committed sim/ records into one T1 characterization report.

Reads ONLY committed evidence (Markdown records under sim/<experiment>/records/)
selected by an explicit manifest (sim/report/selection.json) and the ratified
rows of spec/target-spec.md Sec. 2.  It never runs a simulator and never
touches the network; output is deterministic (sorted keys, no timestamps,
hostnames or absolute paths), so two runs on identical inputs are
byte-identical.

Status vocabulary (per spec row):
  measured-verdict    a ratified bound exists AND a record judged it
  measured-no-bound   a record exists but the row's bound is open (no verdict)
  not-measured        no committed record ("no record")
  proposed-not-graded the row's bound is tagged in-row as proposed by a decision
                      record and not ratified ("proposed, not ratified"); the
                      report grades ratified rows only, so it issues no verdict
                      and counts the row neither as judged nor as not measured
                      (the spec's own status text, which cites any record, is
                      reproduced verbatim); a selected record's figures for
                      such a row are listed in the row details as information
                      only, never as a worst value beside the proposed bound
                      (the input common-mode range row, issue #90, carries the
                      selected ICMR record's structured, cross-checked figures
                      this way; without a selected record it stays explicitly
                      missing and nothing is read from the spec status text)

Supplementary side studies (issue #121): passive-corner RZ x CC studies the
manifest selects explicitly under "supplementary" are hashed, parsed and
checked like selected records, and their observations are derived from their
own tables; they are reported in a separate section and as row-detail notes,
never as a verdict, point count or summary count. A bound-free study (issue
#173: the full PSRR RZ x CC study) reads no bound at all while its row is
proposed, not ratified, and is re-derived from its retained per-cell curves.

Existing records are Markdown only (no structured sidecars), so this module
carries a narrow, tested extraction of the verdict / worst-case lines and
records the sha256 of every source file it read.

Exit codes: 0 ok, 1 --check found a difference, 2 selection / record error.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent
DEFAULT_MANIFEST = HERE / "selection.json"
OUT_DIR_REL = "sim/reports"
OUT_NAME = "characterization-report"
DUT_REL = "design/netlist/opamp_two_stage.spice"

EXPERIMENTS = ["gain-gbw-pm", "gain-gbw-pm-mc", "offset-mc", "noise", "cmrr", "cmrr-mc", "psrr", "psrr-mc", "slew-swing-power", "input-common-mode"]
#: Experiments whose rows may come from different records: a record may judge
#: only a subset of the rows (e.g. a single-figure re-run), so the manifest
#: entry may be an object {row: record path}, naming the record each row is
#: taken from (rows a record also judged but is not selected for are ignored).
MULTI_RECORD = {"slew-swing-power": ("power", "slew", "swing")}
#: Side-study records that live beside an experiment's grid records but never
#: judge its spec rows (matched on the record's title line), so `--latest`
#: must not select them: the gain-gbw-pm RZ x CC passive-corner study
#: (`run_gain_gbw_pm.py --passive-corners`, issue #70) and the slew/swing/power
#: one (`run_slew_swing_power.py --passive-corners`, issue #97). The offset
#: Monte Carlo PVT-grid record (`run_offset_mc.py --grid full`, issue #106) is
#: NOT a side study: the offset extractor reads its per-point layout (issue #120).
#: The PSRR RZ x CC study (`run_psrr.py --passive-corners`, issue #155) is a
#: side study too: `--latest` keeps selecting the PSRR grid record, and the
#: study is reported only when selected explicitly under its bound-free
#: SUPPLEMENTARY key (issue #173).
STUDY_TITLES = {
    "gain-gbw-pm": ("# gain/GBW/PM passive-corner study",),
    "slew-swing-power": ("# slew/swing/power passive-corner study",),  # issue #97
    "psrr": ("# PSRR passive-corner study",),  # issue #155; bound-free supplementary adapter, issue #173
}
CORNER_ORDER = ["typical", "ff", "ss", "fs", "sf"]
FULL_GRID = 45


class ReportError(Exception):
    """Selection / record problem: exit 2 with a clear message."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def posix_rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def section(text: str, heading_prefix: str) -> str:
    """Body of the first '## <heading_prefix>...' section ('' if absent)."""
    m = re.search(r"^## " + re.escape(heading_prefix) + r".*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    n = re.search(r"^## ", rest, re.M)
    return rest[: n.start()] if n else rest


def header_of(text: str) -> str:
    m = re.search(r"^## ", text, re.M)
    return text[: m.start()] if m else text


def num(s: str) -> float:
    return float(s.replace("+", ""))


def corner_sort_key(c: str):
    return (CORNER_ORDER.index(c) if c in CORNER_ORDER else len(CORNER_ORDER), c)


def fmt_point(proc: str, t: str, v: str) -> str:
    return f"{proc} / {t} C / {v} V"


# --------------------------------------------------------------------------
# header provenance
# --------------------------------------------------------------------------
def parse_provenance(text: str, label: str) -> dict:
    head = header_of(text)
    dut = re.search(r"^- \*\*DUT\*\*:.*$", head, re.M)
    if not dut:
        raise ReportError(f"{label}: no '- **DUT**' header line (cannot establish DUT identity)")
    hexes = re.findall(r"`([0-9a-f]{64}|[0-9a-f]{16})`", dut.group(0))
    if not hexes:
        raise ReportError(f"{label}: DUT line carries no sha256 / 16-hex prefix")
    dut_hash = hexes[0]
    pdk_line = re.search(r"^- \*\*PDK[^*]*\*\*:.*$", head, re.M)
    pdk = None
    if pdk_line:
        m = re.search(r"open_pdks `([0-9a-f]{40})`", pdk_line.group(0))
        pdk = m.group(1) if m else None
    client_pdk = None
    m = re.search(r"open_pdks ([0-9a-f]{40}) \(search root", head)
    if m:
        client_pdk = m.group(1)
    ngl = re.search(r"ngspice-(\d+)", head)
    eng = re.search(r"(?:engine(?: as run by klt)?:? `|engine `)ngspice (\d+)`", head)
    if not eng:
        eng = re.search(r"`ngspice (\d+)`", head)
    klt = re.search(r"klt `klt ([^`]+)`", head)
    backend = re.search(r"backend `([^`]+)`", head)
    return {
        "dut_sha256": dut_hash,
        "dut_prefix": dut_hash[:16],
        "pdk_open_pdks": pdk,
        "pdk_client_resolved": client_pdk,
        "ngspice_local": ngl.group(1) if ngl else None,
        "ngspice_engine": eng.group(1) if eng else None,
        "klt_client": klt.group(1) if klt else None,
        "backend": backend.group(1) if backend else None,
        "fleet_runner_mismatch": bool(re.search(r"compatibility `mismatch`|runner klt `[^`]+` vs client", head)),
    }


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------
_PT_SLASH = re.compile(r"^\| (\w+) / (-?\d+) C / ([\d.]+) V \|", re.M)
_PT_GAIN = re.compile(r"^\| `(\w+)` \| (-?\d+) \| ([\d.]+) \|", re.M)


def coverage_of(points: set) -> dict:
    return {
        "points": len(points),
        "corners": sorted({p[0] for p in points}, key=corner_sort_key),
        "temps_c": sorted({int(p[1]) for p in points}),
        "vdd_v": sorted({p[2] for p in points}, key=float),
    }


def grid_points(text: str, kind: str) -> set:
    rx = _PT_GAIN if kind == "gain" else _PT_SLASH
    return {m.groups() for m in rx.finditer(text)}


def coverage_text(c: dict) -> str:
    if "mc_samples_per_corner" in c:
        return (f"{len(c['corners'])} MOS corners ({', '.join(c['corners'])}) x "
                f"N={c['mc_samples_per_corner']} mismatch samples at {c['temps_c'][0]} C, "
                f"{c['vdd_v'][0]} V only ({c['points']} corner points)")
    tail = (f", N={c['mc_samples_per_point']} mismatch samples per point" if "mc_samples_per_point" in c else "")
    return (f"{len(c['corners'])} MOS corners ({', '.join(c['corners'])}) x "
            f"{len(c['temps_c'])} T ({', '.join(str(t) for t in c['temps_c'])} C) x "
            f"{len(c['vdd_v'])} VDD ({', '.join(c['vdd_v'])} V) = {c['points']} points" + tail)


# --------------------------------------------------------------------------
# per-experiment extraction
# --------------------------------------------------------------------------
def common_limitations(text: str, prov: dict, cov: dict) -> list:
    lim = []
    if "res_typical" in text:
        lim.append("passives at typical only in this record: it does not sweep RZ/CC passive corners")
    if prov["fleet_runner_mismatch"]:
        lim.append("fleet runner klt version differs from the client's (compatibility mismatch)")
    if cov["points"] != FULL_GRID and "mc_samples_per_corner" not in cov:
        lim.append(f"partial grid: {cov['points']} of {FULL_GRID} PVT points")
    return lim


def extract_gain(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    sec = section(text, "Verdicts")
    rows = {}
    rx = re.compile(r"^\| (Open-loop DC gain|GBW[^|]*|Phase margin[^|]*) \| (>= [^|]+?) \| \*\*(PASS|FAIL)\*\* \| (\d+)/(\d+) \| ([\d.]+) (dB|MHz|deg) \| ([^|]+?) \|$", re.M)
    for m in rx.finditer(sec):
        key = {"O": "gain", "G": "gbw", "P": "pm"}[m.group(1)[0]]
        rows[key] = {
            "bound_text": m.group(2).strip(), "verdict": m.group(3),
            "pass": int(m.group(4)), "total": int(m.group(5)),
            "worst": f"{m.group(6)} {m.group(7)}", "worst_value": float(m.group(6)),
            "worst_corner": m.group(8).strip(),
        }
    if set(rows) != {"gain", "gbw", "pm"}:
        raise ReportError(f"{label}: no gain/GBW/PM verdict table (is this a provisional record without verdicts?)")
    pts = grid_points(section(text, "All 45 points") or text, "gain")
    # Re-derive pass counts and worst values from the per-point table.
    tbl = re.compile(r"^\| `(\w+)` \| (-?\d+) \| ([\d.]+) \| ([\d.]+) \| ([\d.]+) \| (-?[\d.]+) \|", re.M)
    data = [(m.group(1), m.group(2), m.group(3), float(m.group(4)), float(m.group(5)), float(m.group(6)))
            for m in tbl.finditer(text)]
    if len(data) != len(pts):
        raise ReportError(f"{label}: per-point table is inconsistent ({len(data)} rows, {len(pts)} distinct points)")
    for key, idx, tol in (("gain", 3, 0.006), ("gbw", 4, 0.0006), ("pm", 5, 0.006)):
        bound = float(re.search(r"([\d.]+)", rows[key]["bound_text"]).group(1))
        vals = [d[idx] for d in data]
        n_pass = sum(1 for v in vals if v >= bound)
        worst = min(vals)
        r = rows[key]
        if r["total"] != len(data) or r["pass"] != n_pass or abs(worst - r["worst_value"]) > tol:
            raise ReportError(
                f"{label}: {key} verdict table ({r['pass']}/{r['total']}, worst {r['worst_value']}) "
                f"disagrees with its own per-point table ({n_pass}/{len(data)}, worst {worst})")
        derived = "PASS" if n_pass == len(data) else "FAIL"
        if r["verdict"] != derived:
            raise ReportError(
                f"{label}: {key} verdict token {r['verdict']} disagrees with its own per-point table "
                f"({n_pass}/{len(data)} points pass; the token must be {derived})")
        worst_pts = {fmt_point(d[0], d[1], d[2]) for d in data if d[idx] <= worst + tol}
        if r["worst_corner"] not in worst_pts:
            raise ReportError(
                f"{label}: {key} binding corner '{r['worst_corner']}' is not a worst point of its own "
                f"per-point table (worst {worst} at {', '.join(sorted(worst_pts))})")
        rows[key]["bound_value"] = bound
    stretch = re.search(r"open-loop DC gain >= 70 dB holds at (\d+)/(\d+)", text)
    if stretch:
        s_pass, s_total = int(stretch.group(1)), int(stretch.group(2))
        s_derived = sum(1 for d in data if d[3] >= 70.0)
        if s_total != len(data) or s_pass != s_derived:
            raise ReportError(
                f"{label}: stretch count ({s_pass}/{s_total}) disagrees with its own per-point table "
                f"({s_derived}/{len(data)} gain points >= 70 dB)")
    cov = coverage_of(pts)
    lim = common_limitations(text, prov, cov)
    lim.append("provisional gain/GBW/PM records are superseded by this one and are not used as evidence")
    return {"prov": prov, "coverage": cov, "rows": rows, "limitations": lim,
            "stretch_gain_70db": f"{stretch.group(1)}/{stretch.group(2)}" if stretch else None}


def extract_noise(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    pts = grid_points(text, "slash")
    spread = []
    rx = re.compile(r"^- (\d+ Hz - [\d.]+ [kM]?Hz|[\d.]+ Hz - [\d.]+ [kM]?Hz): ([\d.]+) uV \(([^)]+)\) \.\. ([\d.]+) uV \(([^)]+)\)", re.M)
    for m in rx.finditer(section(text, "Integrated input-referred rms")):
        spread.append({"band": m.group(1), "min_uv": m.group(2), "min_corner": m.group(3),
                       "max_uv": m.group(4), "max_corner": m.group(5)})
    if not spread:
        raise ReportError(f"{label}: no integrated-rms spread lines found")
    fl = re.search(r"Over \d+ fitted points: floor ([\d.]+) \.\. ([\d.]+) nV/rtHz; corner ([\d.]+) \.\. ([\d.]+) kHz", text)
    cov = coverage_of(pts)
    lim = common_limitations(text, prov, cov)
    lim.append("no ratified bound and no ratified integration band (DR-3 residual e1); the bands are candidates")
    return {"prov": prov, "coverage": cov, "spread": spread,
            "floor": fl.groups() if fl else None, "limitations": lim}


OFFSET_GRID_TITLE = "# Offset Monte Carlo PVT grid record"
OFFSET_NOMINAL_TITLE = "# Offset Monte Carlo record"
#: Ratified statistical basis of the offset row (spec Sec. 2, [DR-2]): mismatch MC N >= 300.
OFFSET_MIN_N = 300
#: Display precision of the grid record: mV values at 3 decimals, skew / excess kurtosis at 2.
_MV_TOL = 0.0005 + 1e-6
_MOM_TOL = 0.005 + 1e-6


def extract_offset(text: str, label: str) -> dict:
    """Offset Monte Carlo record of either committed format.

    The issue #45 nominal record (five MOS corners at 27 C / 3.30 V) and the
    issue #106 full PVT grid record (`run_offset_mc.py --grid full`) differ in
    population and layout; the title line decides which parser reads it, and a
    record that matches neither is rejected rather than guessed at.
    """
    head = text.lstrip()
    if head.startswith(OFFSET_GRID_TITLE):
        return extract_offset_grid(text, label)
    if head.startswith(OFFSET_NOMINAL_TITLE):
        return extract_offset_nominal(text, label)
    raise ReportError(f"{label}: unknown offset record format (title is neither '{OFFSET_NOMINAL_TITLE}' "
                      f"nor '{OFFSET_GRID_TITLE}')")


def _okey(proc: str, t, v) -> tuple:
    """Typed offset grid point key: (process, T in C as '%g', VDD in V as '%.2f')."""
    return (str(proc), f"{float(t):g}", f"{float(v):.2f}")


def offset_stats(values: list) -> dict:
    """Per-point offset statistics in mV, the driver's definitions
    (`sim/offset-mc/run_offset_mc.py` stats_of: sample sigma (n-1), population
    skew / excess kurtosis); a parity test pins the two together."""
    import math
    import statistics
    n = len(values)
    if n < 2 or not all(math.isfinite(x) for x in values):
        raise ValueError("need at least two finite samples")
    mean = statistics.fmean(values)
    sigma = statistics.stdev(values)
    m2 = sum((x - mean) ** 2 for x in values) / n
    skew = sum((x - mean) ** 3 for x in values) / n / m2 ** 1.5 if m2 > 0 else 0.0
    kurt = sum((x - mean) ** 4 for x in values) / n / m2 ** 2 - 3.0 if m2 > 0 else 0.0
    return {"n": n, "mean": mean * 1e3, "sigma": sigma * 1e3, "three_sigma": 3 * sigma * 1e3,
            "abs3": (abs(mean) + 3 * sigma) * 1e3, "min": min(values) * 1e3, "max": max(values) * 1e3,
            "skew": skew, "ex_kurt": kurt}


_OG_COLS = ("mean", "sigma", "three_sigma", "abs3", "min", "max", "skew", "ex_kurt")


def _offset_worst(stats: dict, order: list) -> dict:
    """Worst point by sigma and by |mean| + 3 sigma (ties: first in grid order, as the driver)."""
    return {"sigma": max(order, key=lambda k: stats[k]["sigma"]),
            "abs3": max(order, key=lambda k: stats[k]["abs3"])}


def extract_offset_grid(text: str, label: str) -> dict:
    """Full PVT grid offset record (issue #106): every (process, T, VDD) point's statistics,
    the worst sigma / worst |mean| + 3 sigma points and the T x VDD worst-corner table,
    each re-derived from the record's own per-point table. The point set and N are those of
    the record's retained measurement-fingerprint inputs (hash-verified), so a missing,
    duplicate or unexpected point, or a point with too few samples, is rejected.

    The retained per-sample evidence (offset_samples.csv) is cross-checked separately
    (`offset_grid_crosscheck`), since it lives beside the record rather than in it.
    """
    prov = parse_provenance(text, label)
    fp = parse_fingerprint(text, label)
    if fp is None:
        raise ReportError(f"{label}: offset grid record carries no measurement fingerprint, so its PVT point set "
                          "and sample count cannot be established")
    inp = fp["inputs"]
    try:
        if inp.get("grid") != "full":
            raise KeyError("grid")
        ax = inp["corners"]
        procs, temps, sups, vcms = ax["process"], ax["temperature_c"], ax["supply_v"], ax["vcm_v"]
        n_want = int(inp["monte_carlo"]["n"])
    except (KeyError, TypeError, ValueError):
        raise ReportError(f"{label}: retained measurement-fingerprint inputs do not describe a full offset grid "
                          "(need \"grid\": \"full\", corners.process/temperature_c/supply_v/vcm_v and monte_carlo.n)") from None
    if len(vcms) != len(sups):
        raise ReportError(f"{label}: retained inputs pair {len(vcms)} VCM values with {len(sups)} supplies")
    order = [_okey(p, t, v) for p in procs for t in temps for v in sups]  # driver's grid order
    if len(set(order)) != len(order):
        raise ReportError(f"{label}: retained inputs repeat a grid axis value")
    if n_want < OFFSET_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the offset row's ratified statistical basis needs N >= {OFFSET_MIN_N}")
    req = re.search(r"^- \*\*Request\*\*:.*?= (\d+) points, `monte_carlo = \{n: (\d+),", header_of(text), re.M)
    if not req or (int(req.group(1)), int(req.group(2))) != (len(order), n_want):
        raise ReportError(f"{label}: request line missing or disagrees with the retained inputs "
                          f"({len(order)} points, N={n_want} per point)")

    # ---- per-point statistics table ----
    sec = section(text, "Offset statistics per grid point")
    lines = [ln for ln in sec.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no per-point table under '## Offset statistics per grid point'")
    stats: dict = {}
    for ln in lines[2:]:
        c = split_cells(ln.replace("\\|", "/"))
        if len(c) != 12:
            raise ReportError(f"{label}: malformed per-point offset row: {ln.strip()}")
        try:
            k = _okey(c[0], c[1], c[2])
            n = int(c[3])
        except ValueError:
            raise ReportError(f"{label}: malformed per-point offset row: {ln.strip()}") from None
        if k in stats:
            raise ReportError(f"{label}: duplicate grid point {fmt_point(*k)} in the per-point table")
        if k not in order:
            raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} (not in the retained grid axes)")
        if n != n_want:
            raise ReportError(f"{label}: insufficient samples at {fmt_point(*k)}: N={n}, the request is N={n_want} "
                              "per point")
        try:
            stats[k] = {"n": n, **{col: float(v.replace("+", "")) for col, v in zip(_OG_COLS, c[4:])}}
        except ValueError:
            raise ReportError(f"{label}: malformed per-point offset row: {ln.strip()}") from None
    missing = [k for k in order if k not in stats]
    if missing:
        raise ReportError(f"{label}: per-point table is missing {len(missing)} of {len(order)} grid points "
                          f"(first: {fmt_point(*missing[0])})")
    for k, s in stats.items():
        if (abs(s["three_sigma"] - 3 * s["sigma"]) > 3 * _MV_TOL + _MV_TOL
                or abs(s["abs3"] - (abs(s["mean"]) + s["three_sigma"])) > 2 * _MV_TOL + _MV_TOL
                or not s["min"] <= s["mean"] <= s["max"] or s["sigma"] <= 0):
            raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)}: its 3 sigma, |mean| + 3 sigma, "
                              "min/max or sigma columns disagree with each other")

    # ---- headline worst points ----
    hl = section(text, "Worst point")
    we = re.search(r"^- \*\*Worst linear 3-sigma offset over the (\d+)-point grid\*\*: \*\*([\d.]+) mV\*\* at "
                   r"`" + _PT + r"` \(mean ([+-][\d.]+), sigma ([\d.]+), 3 sigma ([\d.]+)\)$", hl, re.M)
    ws = re.search(r"^- \*\*Worst sigma over the grid\*\*: ([\d.]+) mV \(3 sigma ([\d.]+)\) at `" + _PT + r"`$", hl, re.M)
    if not (we and ws):
        raise ReportError(f"{label}: no worst-point headline lines (worst linear 3-sigma offset / worst sigma)")
    w = _offset_worst(stats, order)
    ka, ks = _okey(*we.groups()[2:5]), _okey(*ws.groups()[2:5])
    sa, ss_ = stats[w["abs3"]], stats[w["sigma"]]
    if (int(we.group(1)) != len(order) or ka != w["abs3"]
            or any(abs(float(x) - sa[col]) > _MV_TOL for x, col in
                   ((we.group(2), "abs3"), (we.group(6), "mean"), (we.group(7), "sigma"), (we.group(8), "three_sigma")))):
        raise ReportError(f"{label}: headline worst |mean| + 3 sigma ({we.group(2)} mV at {fmt_point(*ka)}) disagrees "
                          f"with its own per-point table ({sa['abs3']:.3f} mV at {fmt_point(*w['abs3'])})")
    if (ks != w["sigma"] or abs(float(ws.group(1)) - ss_["sigma"]) > _MV_TOL
            or abs(float(ws.group(2)) - ss_["three_sigma"]) > _MV_TOL):
        raise ReportError(f"{label}: headline worst sigma ({ws.group(1)} mV at {fmt_point(*ks)}) disagrees with its "
                          f"own per-point table ({ss_['sigma']:.3f} mV at {fmt_point(*w['sigma'])})")

    # ---- T x VDD worst-corner table ----
    tv = section(text, "Linear 3-sigma offset by temperature and supply")
    tv_rows = [split_cells(ln) for ln in tv.splitlines() if ln.startswith("|")][2:]
    by_tv = {}
    for c in tv_rows:
        m = re.fullmatch(r"(-?\d+) C", c[0]) if c else None
        if not m or len(c) != len(sups) + 1:
            raise ReportError(f"{label}: malformed temperature x supply row: {' | '.join(c)}")
        for v, cell in zip(sups, c[1:]):
            mc_ = re.fullmatch(r"([\d.]+) \((\w+)\)", cell)
            if not mc_:
                raise ReportError(f"{label}: malformed temperature x supply cell '{cell}'")
            by_tv[(f"{float(m.group(1)):g}", f"{float(v):.2f}")] = (float(mc_.group(1)), mc_.group(2))
    worst_tv = []
    for t in temps:
        for v in sups:
            tk = (f"{float(t):g}", f"{float(v):.2f}")
            pts = [k for k in order if k[1:] == tk]
            kk = max(pts, key=lambda k: stats[k]["abs3"])
            got = by_tv.get(tk)
            if got is None or got[1] != kk[0] or abs(got[0] - stats[kk]["abs3"]) > _MV_TOL:
                raise ReportError(f"{label}: temperature x supply table at {tk[0]} C / {tk[1]} V "
                                  f"({'missing' if got is None else f'{got[0]:.3f} ({got[1]})'}) disagrees with its own "
                                  f"per-point table ({stats[kk]['abs3']:.3f} ({kk[0]}))")
            worst_tv.append(kk)
    if len(by_tv) != len(temps) * len(sups):
        raise ReportError(f"{label}: temperature x supply table has {len(by_tv)} cells, the grid "
                          f"{len(temps) * len(sups)}")

    n_total = len(order) * n_want
    valid = re.search(r"^Result: all (\d+) samples valid;", section(text, "Extraction validation"), re.M)
    if not valid or int(valid.group(1)) != n_total:
        raise ReportError(f"{label}: extraction validation does not state all {n_total} samples valid "
                          "(the record lists problems, or its count disagrees with the grid)")
    pts = set(order)
    cov = coverage_of(pts)
    cov["mc_samples_per_point"] = n_want
    se = re.search(r"sigma relative standard error ~ ([\d.]+) %", hl)
    lim = common_limitations(text, prov, cov)
    lim.append("mismatch Monte Carlo of the offset only; no numeric bound proposed or judged "
               "(DR-3 residual e2; the bound is spec issue #62's decision)")
    if se:
        lim.append(f"sigma relative standard error ~ {se.group(1)} % at the stated N per point; the worst of "
                   f"{len(order)} points is a maximum over noisy estimates, so it is biased high by a few standard errors")
    return {"prov": prov, "coverage": cov, "grid": True, "order": order, "stats": stats, "n_per_point": n_want,
            "vcm_of": {f"{float(v):.2f}": float(vc) for v, vc in zip(sups, vcms)},
            "worst": w, "worst_tv": worst_tv, "n_total": n_total, "limitations": lim}


def offset_grid_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every point's displayed statistics, and the worst points, from the record's
    retained per-sample evidence (corners/<rid>/offset_samples.csv), within the record's display
    precision. Offline; no simulator."""
    import csv
    import math
    rel = f"sim/offset-mc/corners/{rid}/offset_samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    need = ("corner", "temperature_c", "vdd_v", "vcm_v", "sample_index", "offset_v")
    by: dict = {}
    seen = set()
    seeds: dict = {}  # (process, VDD, sample index) -> {T: mismatch_seed}, for the drift view's independence note
    seeds_known = True
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns {list(need)}")
        seeds_known = "mismatch_seed" in rd.fieldnames
        for r in rd:
            try:
                k = _okey(r["corner"], r["temperature_c"], r["vdd_v"])
                idx, vcm, off = int(r["sample_index"]), float(r["vcm_v"]), float(r["offset_v"])
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["stats"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if seeds_known:
                seeds.setdefault((k[0], k[2], idx), {})[k[1]] = r["mismatch_seed"]
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if not math.isfinite(off) or abs(vcm - ex["vcm_of"][k[2]]) > 1e-6:
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)} (non-finite offset or VCM "
                                  f"{vcm:g} V, not the commanded {ex['vcm_of'][k[2]]:g} V)")
            seen.add((k, idx))
            by.setdefault(k, []).append(off)
    stats = {}
    for k in ex["order"]:
        vals = by.get(k, [])
        if len(vals) != ex["n_per_point"]:
            raise ReportError(f"{label}: insufficient samples in {rel} at {fmt_point(*k)}: {len(vals)}, the record "
                              f"states N={ex['n_per_point']}")
        s = stats[k] = offset_stats(vals)
        rec = ex["stats"][k]
        bad = [col for col in _OG_COLS
               if abs(rec[col] - s[col]) > (_MOM_TOL if col in ("skew", "ex_kurt") else _MV_TOL)]
        if bad:
            raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)}: the record's {', '.join(bad)} "
                              f"disagree(s) with the retained evidence {rel} (e.g. {bad[0]} "
                              f"{rec[bad[0]]:.3f} vs {s[bad[0]]:.3f})")
    w = _offset_worst(stats, ex["order"])
    if w != ex["worst"]:
        raise ReportError(f"{label}: the record's worst points ({fmt_point(*ex['worst']['abs3'])} by |mean| + 3 sigma, "
                          f"{fmt_point(*ex['worst']['sigma'])} by sigma) disagree with the retained evidence {rel}")
    # derived-view inputs (issue #184): the unrounded per-point statistics re-derived above, and whether any
    # sample index reuses its mismatch seed across temperatures (then the per-temperature means are paired)
    ex["sample_stats"] = stats
    ex["seed_independence"] = ("unknown" if not seeds_known else
                               "shared" if any(len(set(d.values())) < len(d) for d in seeds.values())
                               else "independent")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(stats)}


# --------------------------------------------------------------------------
# derived offset temperature drift and systematic-vs-statistical split (issue #184)
# --------------------------------------------------------------------------
#: A drift is called resolved only if |drift| exceeds this many of its own standard errors.
DRIFT_RESOLVE_K = 2.0


def _fin(x: float) -> float:
    """JSON-safe, byte-stable number: finite, rounded, no negative zero."""
    import math
    if not math.isfinite(x):
        raise ReportError("derived offset drift: non-finite value")
    r = round(x, 6)
    return 0.0 if r == 0 else r


def _slope(ts: list, ms: list, ses: list) -> tuple:
    """Least-squares slope (uV/C) of mean offset vs T and its standard error propagated from the
    per-mean standard errors (independent points): slope = sum(w_i m_i), w_i = (T_i - Tbar) / Sxx,
    variance = sum(w_i^2 se_i^2)."""
    import math
    tb = sum(ts) / len(ts)
    sxx = sum((t - tb) ** 2 for t in ts)
    w = [(t - tb) / sxx for t in ts]
    return (sum(wi * m for wi, m in zip(w, ms)), math.sqrt(sum((wi * se) ** 2 for wi, se in zip(w, ses))))


def _drift_entry(d: float, se: float) -> dict:
    return {"uv_per_c": _fin(d), "se_uv_per_c": _fin(se), "resolved": bool(abs(d) > DRIFT_RESOLVE_K * se)}


def offset_drift_view(ex: dict, record: dict) -> dict:
    """Offset temperature drift and systematic / statistical split, derived offline from the
    per-sample statistics already cross-checked against the record (no simulation, no verdict, no
    bound). `record` is the selected record's source entry (path, sha256)."""
    import math
    if not ex.get("grid") or "sample_stats" not in ex:
        return {"available": False,
                "reason": "the selected offset record is not a full PVT grid (single temperature): "
                          "no temperature drift can be derived"}
    st, n = ex["sample_stats"], ex["n_per_point"]
    temps = sorted({float(k[1]) for k in ex["order"]})
    procs = []
    for k in ex["order"]:
        if k[0] not in procs:
            procs.append(k[0])
    keys = sorted({(k[0], k[2]) for k in ex["order"]}, key=lambda q: (procs.index(q[0]), float(q[1])))
    groups = []
    for proc, vdd in keys:
        raw = []
        for t in temps:
            s = st[(proc, f"{t:g}", vdd)]
            mean, sig = s["mean"] * 1e3, s["sigma"] * 1e3  # mV -> uV
            raw.append((t, mean, sig, sig / math.sqrt(s["n"]), s["n"]))
        pts = []
        for t, mean, sig, se, nn in raw:
            a3 = abs(mean) + 3 * sig
            pts.append({"temp_c": _fin(t), "n": nn, "mean_uv": _fin(mean), "sigma_uv": _fin(sig),
                        "se_mean_uv": _fin(se), "abs_mean_uv": _fin(abs(mean)), "three_sigma_uv": _fin(3 * sig),
                        "abs3_uv": _fin(a3), "systematic_fraction": _fin(abs(mean) / a3) if a3 > 0 else None})
        g = {"process": proc, "vdd_v": vdd, "points": pts}
        if len(raw) >= 2:
            ts, ms, ses = [r[0] for r in raw], [r[1] for r in raw], [r[3] for r in raw]
            span = ts[-1] - ts[0]
            g["drift_end_to_end"] = {"from_c": _fin(ts[0]), "to_c": _fin(ts[-1]),
                                     **_drift_entry((ms[-1] - ms[0]) / span, math.hypot(ses[0], ses[-1]) / span)}
            g["drift_fit"] = {"points": len(ts), **_drift_entry(*_slope(ts, ms, ses))}
            g["segments"] = [{"from_c": _fin(ts[i]), "to_c": _fin(ts[i + 1]),
                              **_drift_entry((ms[i + 1] - ms[i]) / (ts[i + 1] - ts[i]),
                                             math.hypot(ses[i], ses[i + 1]) / (ts[i + 1] - ts[i]))}
                             for i in range(len(ts) - 1)]
        else:
            g["drift_end_to_end"] = g["drift_fit"] = None
            g["segments"] = []
        sg = [p_["sigma_uv"] for p_ in pts]
        g["sigma_vs_t"] = {"min_uv": min(sg), "max_uv": max(sg),
                           "max_over_min": _fin(max(sg) / min(sg)) if min(sg) > 0 else None}
        g["worst_point"] = dict(max(pts, key=lambda p_: p_["abs3_uv"]))
        groups.append(g)
    allw = max(groups, key=lambda g: g["worst_point"]["abs3_uv"])
    lim = [f"{len(temps)} temperatures ({', '.join(f'{t:g}' for t in temps)} C) only: the drift is a "
           f"{'two-segment' if len(temps) == 3 else 'piecewise'} description of the measured points, not a fitted "
           "law; nothing is extrapolated below the lowest or above the highest temperature, and curvature inside "
           "a segment is not resolved",
           f"each mean has standard error sigma/sqrt(N) with N={n}; a drift is called resolved only when "
           f"|drift| > {DRIFT_RESOLVE_K:g} of its own standard errors (points treated as independent), "
           "otherwise it is unresolved and its value is a noisy estimate, not a measurement",
           f"{len(groups)} corner/supply groups are examined at the {DRIFT_RESOLVE_K:g}-standard-error threshold, so a "
           "few chance 'resolved' flags are expected even for zero true drift; one flag is not by itself a trend",
           "the systematic part |mean| of a point still contains its own sampling error (standard error "
           "sigma/sqrt(N)); it is not a pure process-systematic offset",
           "evidence only: no verdict, no bound proposed; the offset bound is the spec issue #62 decision"]
    if ex.get("seed_independence") != "independent":
        lim.append(f"mismatch-seed independence across temperatures is {ex.get('seed_independence')}: if samples are "
                   "paired across temperatures the drift standard errors are conservative (too large)")
    return {"available": True, "record": record["path"], "record_sha256": record["sha256"],
            "input": ex["retained"]["path"], "input_sha256": ex["retained"]["sha256"],
            "units": "uV for offsets, uV/C for drifts", "temps_c": [_fin(t) for t in temps], "n_per_point": n,
            "resolution_threshold_se": DRIFT_RESOLVE_K, "seed_independence": ex.get("seed_independence"),
            "groups": groups,
            "worst_overall": {"process": allw["process"], "vdd_v": allw["vdd_v"], **allw["worst_point"]},
            "limitations": lim}


def extract_offset_nominal(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    sec = section(text, "Offset statistics per corner")
    rows = []
    for m in re.finditer(r"^\| (\w+) \| (\d+) \| ([+-][\d.]+) \| ([\d.]+) \| ([\d.]+) \|", sec, re.M):
        rows.append({"corner": m.group(1), "n": int(m.group(2)), "mean_mv": m.group(3),
                     "sigma_mv": m.group(4), "three_sigma_mv": m.group(5)})
    ws = re.search(r"\*\*Worst corner by sigma\*\*: `(\w+)` \(([\d.]+) mV; 3 sigma ([\d.]+) mV\)", sec)
    wm = re.search(r"\*\*Worst corner by \|mean\| \+ 3 sigma\*\*: `(\w+)` \(([\d.]+) mV\)", sec)
    cond = re.search(r"at (-?\d+) C, VDD ([\d.]+) V", header_of(text))
    if not (rows and ws and wm and cond):
        raise ReportError(f"{label}: offset statistics table / worst-corner lines / conditions not found")
    ns = {r["n"] for r in rows}
    cov = {"points": len(rows), "corners": sorted([r["corner"] for r in rows], key=corner_sort_key),
           "temps_c": [int(cond.group(1))], "vdd_v": [cond.group(2)],
           "mc_samples_per_corner": min(ns) if len(ns) == 1 else sorted(ns)}
    se = re.search(r"sigma relative standard error ~ ([\d.]+) %", sec)
    lim = common_limitations(text, prov, cov)
    lim.append(f"nominal T/VDD only ({cond.group(1)} C, {cond.group(2)} V): no temperature or supply axis")
    lim.append("mismatch Monte Carlo of the offset only; no numeric bound proposed (DR-3 residual e2)")
    if se:
        lim.append(f"sigma relative standard error ~ {se.group(1)} % at the stated N")
    return {"prov": prov, "coverage": cov, "stats": rows,
            "worst_sigma": ws.groups(), "worst_abs": wm.groups(), "limitations": lim}


def _worst_table(text: str, heading: str, label: str) -> list:
    sec = section(text, heading)
    out = []
    rx = re.compile(r"^\| ([^|]+?) \| \*\*(?:>= )?([\d.]+)\*\* \| ([^|]+?) \| ([\d.]+) \| (\d+) \|$", re.M)
    for m in rx.finditer(sec):
        out.append({"figure": m.group(1), "worst_db": m.group(2), "worst_corner": m.group(3),
                    "best_db": m.group(4), "missing_points": int(m.group(5))})
    if not out:
        raise ReportError(f"{label}: no worst-case table under '## {heading}'")
    return out


def extract_cmrr(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    cov = coverage_of(grid_points(text, "slash"))
    lim = common_limitations(text, prov, cov)
    if "**Systematic only.**" in text:
        lim.append("systematic figure of a perfectly matched schematic; mismatch-limited CMRR (Monte Carlo) is not covered by this record")
    lim.append("no ratified bound (DR-3 residual e3)")
    return {"prov": prov, "coverage": cov, "figures": {"CMRR": _worst_table(text, "Worst case over", label)},
            "limitations": lim}


# --------------------------------------------------------------------------
# CMRR mismatch Monte Carlo (issue #124): measured, never graded while proposed
# --------------------------------------------------------------------------
CMRR_MC_EXP = "cmrr-mc"
CMRR_MC_MIN_N = 300
#: (record heading, key, samples.csv column suffix), in the record's order
CMRR_MC_FIGS = (("DC plateau (0.1-1 Hz)", "dc", "dc"), ("1 kHz", "1k", "1k"), ("10 kHz", "10k", "10k"),
                ("100 kHz", "100k", "100k"), ("1 MHz", "1m", "1m"),
                ("at f_u (differential unity gain)", "fu", "fu"))
_CM_TOL = 0.011      # dB: two-decimal display of a dB mean / quantile, plus rounding of the inputs
_CM_DERIVE_TOL = 0.03  # dB: a statistic derived from other displayed (rounded) columns
_CM_REL = 0.006      # relative: |Acm| columns are displayed to 3-4 significant digits
_CM_COLS = ("mean", "sigma", "skew", "min", "p1", "p5", "lin3", "db3", "acm_mean", "acm_sigma", "ad_mean")


def _cm_lin3(ad_mean: float, acm_mean: float, acm_sigma: float) -> float:
    import math
    return ad_mean - 20 * math.log10(acm_mean + 3 * acm_sigma)


def _cm_quantile(sorted_vals: list, q: float) -> float:
    k = (len(sorted_vals) - 1) * q
    f = int(k)
    return sorted_vals[f] + (sorted_vals[min(f + 1, len(sorted_vals) - 1)] - sorted_vals[f]) * (k - f)


def cmrr_mc_stats(cmrr: list, acm: list, ad: list) -> dict:
    """The record's per-point statistics from per-sample CMRR (dB), Acm (V/V) and Ad (dB):
    sample sigma (n-1), skewness with the population sigma, linear-interpolated percentiles,
    mean |Acm| and its sample sigma (the driver's definitions)."""
    import statistics
    n = len(cmrr)
    mean, sigma = statistics.fmean(cmrr), statistics.stdev(cmrr)
    sp = statistics.pstdev(cmrr)
    skew = sum((x - mean) ** 3 for x in cmrr) / n / sp ** 3 if sp > 0 else 0.0
    a = [abs(x) for x in acm]
    am, asg = statistics.fmean(a), statistics.stdev(a)
    srt = sorted(cmrr)
    return {"n": n, "mean": mean, "sigma": sigma, "skew": skew, "min": srt[0],
            "p1": _cm_quantile(srt, 0.01), "p5": _cm_quantile(srt, 0.05),
            "lin3": _cm_lin3(statistics.fmean(ad), am, asg), "db3": mean - 3 * sigma,
            "acm_mean": am, "acm_sigma": asg, "ad_mean": statistics.fmean(ad)}


def _cm_close(col: str, rec: float, got: float) -> bool:
    if col in ("acm_mean", "acm_sigma"):
        return abs(rec - got) <= _CM_REL * abs(got) + 1e-12
    return abs(rec - got) <= _CM_TOL


def _cm_worst(stats: dict, order: list) -> dict:
    """Worst point per statistic (ties: first in grid order, as the driver)."""
    return {"lin3": min(order, key=lambda k: stats[k]["lin3"]), "db3": min(order, key=lambda k: stats[k]["db3"]),
            "min": min(order, key=lambda k: stats[k]["min"]), "p5": min(order, key=lambda k: stats[k]["p5"])}


def extract_cmrr_mc(text: str, label: str) -> dict:
    """CMRR mismatch Monte Carlo record (issue #61): every figure's per-point statistics, the
    worst point per figure and the control results, with each linear 3-sigma and dB 3-sigma
    re-derived from the record's own columns. The retained per-sample evidence (samples.csv)
    is cross-checked separately (`cmrr_mc_crosscheck`)."""
    prov = parse_provenance(text, label)
    hd = header_of(text)
    pop = re.search(r"^- \*\*Population\*\*: process ([\w, ]+); T (-?\d+) C; VDD ([\d.]+) V .*?"
                    r"`monte_carlo = \{n: (\d+), seed: (\d+), vary: \"(\w+)\"\}` per grid point -> (\d+) x (\d+) = (\d+) samples",
                    hd, re.M)
    if not pop:
        raise ReportError(f"{label}: no Population line (processes, T, VDD, monte_carlo n / seed / vary)")
    procs = [p.strip() for p in pop.group(1).split(",")]
    t_c, vdd = pop.group(2), pop.group(3)
    n_want, seed, vary = int(pop.group(4)), int(pop.group(5)), pop.group(6)
    n_pts, n_each, n_total = int(pop.group(7)), int(pop.group(8)), int(pop.group(9))
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if n_want < CMRR_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the CMRR mismatch statistic needs N >= {CMRR_MC_MIN_N}")
    if (n_pts, n_each, n_total) != (len(procs), n_want, len(procs) * n_want) or len(set(procs)) != len(procs):
        raise ReportError(f"{label}: Population line is inconsistent ({n_pts} x {n_each} = {n_total} samples for "
                          f"{len(procs)} processes at N={n_want})")
    order = [_okey(p, t_c, vdd) for p in procs]
    sec = section(text, "Statistics per grid point")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    tables = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    if sorted(tables) != sorted(h for h, _, _ in CMRR_MC_FIGS):
        raise ReportError(f"{label}: per-figure tables {sorted(tables)} are not the expected "
                          f"{[h for h, _, _ in CMRR_MC_FIGS]}")
    stats: dict = {}
    for head, key, _ in CMRR_MC_FIGS:
        lines = [ln for ln in tables[head].splitlines() if ln.startswith("|")]
        if len(lines) < 3:
            raise ReportError(f"{label}: no per-point table under '### {head}'")
        st: dict = {}
        for ln in lines[2:]:
            c = split_cells(ln)
            m = re.fullmatch(_PT, c[0]) if c else None
            if not m or len(c) != 13:
                raise ReportError(f"{label}: malformed CMRR row under '### {head}': {ln.strip()}")
            k = _okey(*m.groups())
            try:
                n = int(c[1])
                vals = [float(x.strip("*").replace("+", "")) for x in c[2:]]
            except ValueError:
                raise ReportError(f"{label}: malformed CMRR row under '### {head}': {ln.strip()}") from None
            if k in st:
                raise ReportError(f"{label}: duplicate grid point {fmt_point(*k)} under '### {head}'")
            if k not in order:
                raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} under '### {head}' "
                                  "(not in the Population line)")
            if n != n_want:
                raise ReportError(f"{label}: insufficient samples at {fmt_point(*k)} under '### {head}': N={n}, "
                                  f"the request is N={n_want} per point")
            # columns: mean sigma skew min p1 p5 lin3 db3 acm_mean acm_sigma ad_mean
            st[k] = {"n": n, **dict(zip(_CM_COLS, vals))}
        missing = [k for k in order if k not in st]
        if missing:
            raise ReportError(f"{label}: '### {head}' is missing {len(missing)} of {len(order)} grid points "
                              f"(first: {fmt_point(*missing[0])})")
        for k, s in st.items():
            if (abs(s["lin3"] - _cm_lin3(s["ad_mean"], s["acm_mean"], s["acm_sigma"])) > _CM_DERIVE_TOL
                    or abs(s["db3"] - (s["mean"] - 3 * s["sigma"])) > _CM_DERIVE_TOL
                    or s["sigma"] <= 0 or s["min"] > s["p1"] + _CM_TOL or s["p1"] > s["p5"] + _CM_TOL):
                raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} under '### {head}': its linear "
                                  "3-sigma, dB 3-sigma or percentile columns disagree with its own columns")
        stats[key] = st
    # ---- worst point per figure ----
    wsec = section(text, "Worst point per figure")
    wl = [split_cells(ln) for ln in wsec.splitlines() if ln.startswith("|")][2:]
    worst: dict = {}
    for c in wl:
        if len(c) != 8:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}")
        fig = next((f for f in CMRR_MC_FIGS if f[0] == c[0]), None)
        if fig is None or fig[1] in worst:
            raise ReportError(f"{label}: worst-point table row '{c[0]}' is unexpected or duplicated")
        try:
            vals = {"lin3": float(c[1].strip("*")), "db3": float(c[3]), "min": float(c[5]), "p5": float(c[7])}
        except ValueError:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}") from None
        ats = {"lin3": c[2], "db3": c[4], "min": c[6]}
        st = stats[fig[1]]
        w = _cm_worst(st, order)
        for col, v in vals.items():
            if abs(v - st[w[col]][col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({fig[0]}, {col}: {v}) disagrees with its own per-point "
                                  f"table ({st[w[col]][col]:.2f} at {fmt_point(*w[col])})")
        for col, at in ats.items():
            mm = re.fullmatch(_PT, at)
            if not mm or _okey(*mm.groups()) not in order or abs(st[_okey(*mm.groups())][col] - vals[col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({fig[0]}, {col}) names '{at}', which is not a point "
                                  "holding that worst value in the per-point table")
        worst[fig[1]] = {col: {"value": vals[col], "at": w[col]} for col in ("lin3", "db3", "min", "p5")}
    if sorted(worst) != sorted(k for _, k, _ in CMRR_MC_FIGS):
        raise ReportError(f"{label}: worst-point table does not cover every figure")
    # ---- controls ----
    ctl = section(text, "Controls")
    so = re.search(r"\*\*Switch-off\*\*.*?(?=\*\*Process-only\*\*)", ctl, re.S)
    po = re.search(r"\*\*Process-only\*\*.*?CMRR DC sigma ([\d.]+) dB, offset sigma ([\d.]+) mV", ctl, re.S)
    ib = re.search(r"\*\*Negative control: mirror imbalance\*\*.*?Result: \*\*PASS\*\*", ctl, re.S)
    sh = re.search(r"shift of mean \|Acm\|: ([+-][\d.]+) V/V; standard error of the difference ([\d.]*\d)", ctl)
    if not (so and "Result: **PASS**" in so.group(0) and po and ib and sh):
        raise ReportError(f"{label}: the controls section lacks a passing switch-off control, the process-only result "
                          "or a passing mirror-imbalance negative control")
    devs = [split_cells(ln) for ln in so.group(0).splitlines() if ln.startswith("| ") and "/ 27 C" in ln]
    try:
        dev_dc = max(float(c[-2]) for c in devs)
        dev_fu = max(float(c[-1]) for c in devs)
    except (ValueError, IndexError):
        raise ReportError(f"{label}: malformed switch-off control table") from None
    if not devs:
        raise ReportError(f"{label}: malformed switch-off control table")
    shift, se = float(sh.group(1)), float(sh.group(2))
    if se <= 0 or shift / se <= 3:
        raise ReportError(f"{label}: the mirror-imbalance control does not exceed 3 standard errors "
                          f"({shift} V/V, SE {se})")
    if not re.search(r"^- All blocking checks passed: yes\.", section(text, "Validation summary"), re.M):
        raise ReportError(f"{label}: validation summary does not state 'All blocking checks passed: yes'")
    cov = coverage_of(set(order))
    cov["mc_samples_per_corner"] = n_want
    lim = common_limitations(text, prov, cov)
    lim += [f"temperature and supply are NOT sampled under mismatch: {t_c} C / {vdd} V only (the systematic "
            "cmrr record covers T and VDD for matched devices only)",
            "MOS mismatch only; passives (RZ, CC) at typical with no passive mismatch, resistor and capacitor "
            "mismatch not modelled",
            f"N={n_want} cannot resolve a 0.135 % tail: neither the linear nor the dB 3-sigma figure is a sample "
            "statistic; the empirical minimum and percentiles are the normality cross-check",
            "mismatch Monte Carlo of the CMRR; no numeric bound proposed or judged here (DR-6 is proposed, not ratified)"]
    return {"prov": prov, "coverage": cov, "order": order, "stats": stats, "worst": worst, "n_per_point": n_want,
            "n_total": n_total, "seed": seed, "vary": vary, "t_c": t_c, "vdd": vdd,
            "controls": {"switch_off_max_dev_db_dc_to_1m": dev_dc, "switch_off_max_dev_db_at_fu": dev_fu,
                         "process_only_cmrr_dc_sigma_db": float(po.group(1)),
                         "process_only_offset_sigma_mv": float(po.group(2)),
                         "imbalance_shift_acm_vv": shift, "imbalance_shift_se": se},
            "limitations": lim}


def cmrr_mc_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every figure's per-point statistics, and the worst points, from the record's
    retained per-sample evidence (corners/<rid>/samples.csv), within display precision. Offline."""
    import csv
    import math
    rel = f"sim/cmrr-mc/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    need = ["process", "temperature_c", "vdd_v", "sample_index", "valid"] + \
           [f"cmc_{q}_{s}" for _, _, s in CMRR_MC_FIGS for q in ("cmrr", "ad", "acm")]
    by: dict = {}
    seen = set()
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns it needs")
        for r in rd:
            try:
                k = _okey(r["process"], r["temperature_c"], r["vdd_v"])
                idx, valid = int(r["sample_index"]), int(r["valid"])
                vals = {(q, s): float(r[f"cmc_{q}_{s}"]) for _, _, s in CMRR_MC_FIGS for q in ("cmrr", "ad", "acm")}
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["order"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if valid != 1 or not all(math.isfinite(v) for v in vals.values()):
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)}")
            seen.add((k, idx))
            by.setdefault(k, []).append(vals)
    for k in ex["order"]:
        if len(by.get(k, [])) != ex["n_per_point"]:
            raise ReportError(f"{label}: insufficient samples in {rel} at {fmt_point(*k)}: {len(by.get(k, []))}, the "
                              f"record states N={ex['n_per_point']}")
    for head, key, s in CMRR_MC_FIGS:
        got = {k: cmrr_mc_stats([v[("cmrr", s)] for v in by[k]], [v[("acm", s)] for v in by[k]],
                                [v[("ad", s)] for v in by[k]]) for k in ex["order"]}
        for k in ex["order"]:
            rec = ex["stats"][key][k]
            bad = [c for c in _CM_COLS if not _cm_close(c, rec[c], got[k][c])]
            if bad:
                raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({head}): the record's "
                                  f"{', '.join(bad)} disagree(s) with the retained evidence {rel} (e.g. {bad[0]} "
                                  f"{rec[bad[0]]:g} vs {got[k][bad[0]]:g})")
        w = _cm_worst(got, ex["order"])
        for col in ("lin3", "db3", "min", "p5"):
            if abs(ex["worst"][key][col]["value"] - got[w[col]][col]) > _CM_TOL:
                raise ReportError(f"{label}: the record's worst {col} for {head} disagrees with the retained "
                                  f"evidence {rel}")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(by)}


# --------------------------------------------------------------------------
# PSRR mismatch Monte Carlo (issue #182): measured, never graded while proposed
# --------------------------------------------------------------------------
PSRR_MC_EXP = "psrr-mc"
PSRR_MC_MIN_N = 300
#: (record side label, samples.csv rail suffix, supply-gain symbol), in the record's order
PSRR_MC_SIDES = (("PSRR+", "vdd", "Avdd"), ("PSRR-", "vss", "Avss"))
PSRR_MC_FIGS = CMRR_MC_FIGS  # the same six frequency figures, same headings
PSRR_MC_COLS = _CM_COLS      # mean sigma skew min p1 p5 lin3 db3 |Asupply| mean / sigma, mean Ad (dB)


def _pm_key(side: str, key: str) -> str:
    return f"{side} {key}"


def extract_psrr_mc(text: str, label: str) -> dict:
    """PSRR mismatch Monte Carlo record of either committed format.

    The issue #99 nominal record (five MOS corners at 27 C / 3.30 V) and the
    issue #190 full PVT grid record (`run_psrr_mc.py --grid full`) differ in
    population; the record's retained measurement-fingerprint inputs decide
    which parser reads it (`"grid": "full"`), and a record that matches
    neither is rejected rather than guessed at.
    """
    fp = parse_fingerprint(text, label)
    if fp is not None and fp["inputs"].get("grid") == "full":
        return extract_psrr_mc_grid(text, label, fp)
    return extract_psrr_mc_nominal(text, label)


def _psrr_mc_tables(text: str, label: str, order: list, n_want: int) -> tuple[dict, dict]:
    """Per-side and per-figure per-point statistics of a PSRR mismatch record,
    each row's linear 3-sigma and dB 3-sigma re-derived from the row's own
    columns (shared by the nominal and full-grid parsers)."""
    sec = section(text, "Statistics per grid point")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    tables = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    want = [f"{s} {h}" for s, _, _ in PSRR_MC_SIDES for h, _, _ in PSRR_MC_FIGS]
    if sorted(tables) != sorted(want):
        raise ReportError(f"{label}: per-figure tables {sorted(tables)} are not the expected {want}")
    stats: dict = {}
    knee_dcs: dict = {}
    for sname, _, _ in PSRR_MC_SIDES:
        for head, key, _ in PSRR_MC_FIGS:
            fig_head = f"{sname} {head}"
            lines = [ln for ln in tables[fig_head].splitlines() if ln.startswith("|")]
            if len(lines) < 3:
                raise ReportError(f"{label}: no per-point table under '### {fig_head}'")
            is_dc = key == "dc"
            st: dict = {}
            for ln in lines[2:]:
                c = split_cells(ln)
                m = re.fullmatch(_PT, c[0]) if c else None
                if not m or len(c) != (15 if is_dc else 14):
                    raise ReportError(f"{label}: malformed PSRR row under '### {fig_head}': {ln.strip()}")
                k = _okey(*m.groups())
                try:
                    n = int(c[1])
                    vals = [float(x.strip("*").replace("+", "")) for x in c[2:13]]
                    knee = int(c[14]) if is_dc else 0
                except ValueError:
                    raise ReportError(f"{label}: malformed PSRR row under '### {fig_head}': {ln.strip()}") from None
                if k in st:
                    raise ReportError(f"{label}: duplicate grid point {fmt_point(*k)} under '### {fig_head}'")
                if k not in order:
                    raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} under '### {fig_head}' "
                                      "(not in the record's population)")
                if n != n_want:
                    raise ReportError(f"{label}: insufficient samples at {fmt_point(*k)} under '### {fig_head}': "
                                      f"N={n}, the request is N={n_want} per point")
                st[k] = {"n": n, **dict(zip(PSRR_MC_COLS, vals))}
                if is_dc:
                    knee_dcs[(sname, k)] = knee
            missing = [k for k in order if k not in st]
            if missing:
                raise ReportError(f"{label}: '### {fig_head}' is missing {len(missing)} of {len(order)} grid "
                                  f"points (first: {fmt_point(*missing[0])})")
            for k, s in st.items():
                if (abs(s["lin3"] - _cm_lin3(s["ad_mean"], s["acm_mean"], s["acm_sigma"])) > _CM_DERIVE_TOL
                        or abs(s["db3"] - (s["mean"] - 3 * s["sigma"])) > _CM_DERIVE_TOL
                        or s["sigma"] <= 0 or s["min"] > s["p1"] + _CM_TOL or s["p1"] > s["p5"] + _CM_TOL):
                    raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} under '### {fig_head}': its "
                                      "linear 3-sigma, dB 3-sigma or percentile columns disagree with its own columns")
            stats[_pm_key(sname, key)] = st
    return stats, knee_dcs


def _psrr_mc_worst(text: str, label: str, stats: dict, order: list) -> dict:
    """The worst-point-per-figure table of a PSRR mismatch record, checked
    against its own per-point tables."""
    wsec = section(text, "Worst point per figure")
    wl = [split_cells(ln) for ln in wsec.splitlines() if ln.startswith("|")][2:]
    heads = {f"{s} {h}": _pm_key(s, k) for s, _, _ in PSRR_MC_SIDES for h, k, _ in PSRR_MC_FIGS}
    worst: dict = {}
    for c in wl:
        if len(c) != 8:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}")
        wk = heads.get(c[0])
        if wk is None or wk in worst:
            raise ReportError(f"{label}: worst-point table row '{c[0]}' is unexpected or duplicated")
        try:
            vals = {"lin3": float(c[1].strip("*")), "db3": float(c[3]), "min": float(c[5]), "p5": float(c[7])}
        except ValueError:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}") from None
        ats = {"lin3": c[2], "db3": c[4], "min": c[6]}
        st = stats[wk]
        w = _cm_worst(st, order)
        for col, v in vals.items():
            if abs(v - st[w[col]][col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({c[0]}, {col}: {v}) disagrees with its own per-point "
                                  f"table ({st[w[col]][col]:.2f} at {fmt_point(*w[col])})")
        for col, at in ats.items():
            mm = re.fullmatch(_PT, at)
            if not mm or _okey(*mm.groups()) not in order or abs(st[_okey(*mm.groups())][col] - vals[col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({c[0]}, {col}) names '{at}', which is not a point "
                                  "holding that worst value in the per-point table")
        worst[wk] = {col: {"value": vals[col], "at": w[col]} for col in ("lin3", "db3", "min", "p5")}
    if sorted(worst) != sorted(heads.values()):
        raise ReportError(f"{label}: worst-point table does not cover every side and figure")
    return worst


def _psrr_mc_floor_knees(text: str, label: str, knee_dcs: dict) -> tuple[dict, int]:
    """The numerical-floor counts per side and the near-cancellation knee
    total of a PSRR mismatch record, the latter checked against the knee DC
    counts of its per-point tables."""
    nf = section(text, "Numerical floor and near-cancellation")
    floors = {}
    for sname, _, sym in PSRR_MC_SIDES:
        fm = re.search(rf"^- {re.escape(sname)}: samples with \|{sym}\| below the numerical floor .*?: \*\*(\d+)\*\*;", nf, re.M)
        if not fm:
            raise ReportError(f"{label}: no numerical-floor line for {sname}")
        floors[sname] = int(fm.group(1))
    km = re.search(r"^### Near-cancellation knees \((\d+) sample rails\)", nf, re.M)
    if not km:
        raise ReportError(f"{label}: no near-cancellation knee accounting")
    knees = int(km.group(1))
    if sum(v for (s, _), v in knee_dcs.items() if s == "PSRR+") + sum(v for (s, _), v in knee_dcs.items() if s == "PSRR-") != knees:
        raise ReportError(f"{label}: the knee DC counts of the per-point tables do not sum to the {knees} knee rails")
    return floors, knees


def _psrr_mc_controls(text: str, label: str) -> tuple[float, float, re.Match, dict]:
    """The controls section of a PSRR mismatch record: the passing switch-off
    control (with its deviation table), the process-only result and the
    passing mirror-imbalance negative control."""
    ctl = section(text, "Controls")
    so = re.search(r"\*\*Switch-off\*\*.*?(?=\*\*Process-only\*\*)", ctl, re.S)
    po = re.search(r"\*\*Process-only\*\*.*?PSRR\+ DC sigma ([\d.]+) dB, PSRR- DC sigma ([\d.]+) dB, offset sigma ([\d.]+) mV",
                   ctl, re.S)
    ib = re.search(r"\*\*Negative control: mirror imbalance\*\*.*", ctl, re.S)
    if not (so and "Result: **PASS**" in so.group(0) and po and ib and "Result: **PASS**" in ib.group(0)):
        raise ReportError(f"{label}: the controls section lacks a passing switch-off control, the process-only result "
                          "or a passing mirror-imbalance negative control")
    devs = [split_cells(ln) for ln in so.group(0).splitlines() if ln.startswith("| ") and "/ 27 C" in ln]
    try:
        dev_dc = max(float(c[-3]) for c in devs)
        dev_fu = max(float(c[-2]) for c in devs)
    except (ValueError, IndexError):
        raise ReportError(f"{label}: malformed switch-off control table") from None
    if not devs:
        raise ReportError(f"{label}: malformed switch-off control table")
    imb: dict = {}
    for sname, _, _ in PSRR_MC_SIDES:
        rm = re.search(rf"^\| {re.escape(sname)} DC \| XM3 W [\d.]+u \|.*\| ([+-][\d.]+) / ([\d.]*\d) \|\s*$", ib.group(0), re.M)
        if not rm:
            raise ReportError(f"{label}: no mirror-imbalance row for {sname}")
        shift, se = float(rm.group(1)), float(rm.group(2))
        if se <= 0 or shift / se <= 3:
            raise ReportError(f"{label}: the mirror-imbalance control does not exceed 3 standard errors for {sname} "
                              f"({shift} V/V, SE {se})")
        imb[sname] = {"shift_asup_vv": shift, "shift_se": se}
    return dev_dc, dev_fu, po, imb


def extract_psrr_mc_nominal(text: str, label: str) -> dict:
    """PSRR mismatch Monte Carlo nominal record (issue #99): per side (PSRR+ / PSRR-) and figure, the
    per-point statistics, the worst point per figure, the near-cancellation knee accounting and
    the control results, with each linear 3-sigma and dB 3-sigma re-derived from the record's own
    columns. The retained per-sample evidence (samples.csv) is cross-checked separately
    (`psrr_mc_crosscheck`)."""
    prov = parse_provenance(text, label)
    hd = header_of(text)
    pop = re.search(r"^- \*\*Population\*\*: process ([\w, ]+); T (-?\d+) C; VDD ([\d.]+) V .*?"
                    r"`monte_carlo = \{n: (\d+), seed: (\d+), vary: \"(\w+)\"\}` per grid point -> (\d+) x (\d+) = (\d+) samples",
                    hd, re.M)
    if not pop:
        raise ReportError(f"{label}: no Population line (processes, T, VDD, monte_carlo n / seed / vary)")
    procs = [p.strip() for p in pop.group(1).split(",")]
    t_c, vdd = pop.group(2), pop.group(3)
    n_want, seed, vary = int(pop.group(4)), int(pop.group(5)), pop.group(6)
    n_pts, n_each, n_total = int(pop.group(7)), int(pop.group(8)), int(pop.group(9))
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if n_want < PSRR_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the PSRR mismatch statistic needs N >= {PSRR_MC_MIN_N}")
    if (n_pts, n_each, n_total) != (len(procs), n_want, len(procs) * n_want) or len(set(procs)) != len(procs):
        raise ReportError(f"{label}: Population line is inconsistent ({n_pts} x {n_each} = {n_total} samples for "
                          f"{len(procs)} processes at N={n_want})")
    order = [_okey(p, t_c, vdd) for p in procs]
    stats, knee_dcs = _psrr_mc_tables(text, label, order, n_want)
    # ---- worst point per figure ----
    worst = _psrr_mc_worst(text, label, stats, order)
    # ---- numerical floor and near-cancellation knees ----
    floors, knees = _psrr_mc_floor_knees(text, label, knee_dcs)
    # ---- controls ----
    dev_dc, dev_fu, po, imb = _psrr_mc_controls(text, label)
    if not re.search(r"^- All blocking checks passed: yes\.", section(text, "Validation summary"), re.M):
        raise ReportError(f"{label}: validation summary does not state 'All blocking checks passed: yes'")
    cov = coverage_of(set(order))
    cov["mc_samples_per_corner"] = n_want
    lim = common_limitations(text, prov, cov)
    lim += [f"temperature and supply are NOT sampled under mismatch: {t_c} C / {vdd} V only (the systematic "
            "psrr record covers T and VDD for matched devices only; its four Avss sign-flip points, all at 125 C, "
            "remain systematic-only)",
            "MOS mismatch only; passives (RZ, CC) at typical with no passive mismatch, resistor and capacitor "
            "mismatch not modelled",
            f"N={n_want} cannot resolve a 0.135 % tail: neither the linear nor the dB 3-sigma figure is a sample "
            "statistic; the empirical minimum and percentiles are the normality cross-check",
            f"{knees} near-cancellation knee rail(s) (PSRR+ DC band non-flat but monotonically rising) are accepted "
            "with the 0.1 Hz asymptote as their DC figure",
            "supply rejection of the bias generator is excluded (the ideal 10 uA bias source carries no small-signal "
            "current)",
            "mismatch Monte Carlo of the PSRR; no numeric bound proposed or judged here (DR-6 is proposed, not ratified)"]
    if any(floors.values()):
        lim.append("samples below the numerical floor are clamped, so their PSRR figure is a lower bound: "
                   + ", ".join(f"{s} {n}" for s, n in floors.items()))
    return {"prov": prov, "coverage": cov, "order": order, "stats": stats, "worst": worst, "n_per_point": n_want,
            "n_total": n_total, "seed": seed, "vary": vary, "t_c": t_c, "vdd": vdd, "knee_rails": knees,
            "knee_dcs": {f"{s}|{fmt_point(*k)}": v for (s, k), v in knee_dcs.items()}, "floor_samples": floors,
            "controls": {"switch_off_max_dev_db_dc_to_1m": dev_dc, "switch_off_max_dev_db_at_fu": dev_fu,
                         "process_only_psrr_plus_dc_sigma_db": float(po.group(1)),
                         "process_only_psrr_minus_dc_sigma_db": float(po.group(2)),
                         "process_only_offset_sigma_mv": float(po.group(3)),
                         "imbalance": imb},
            "limitations": lim}


def extract_psrr_mc_grid(text: str, label: str, fp: dict) -> dict:
    """Full PVT grid PSRR mismatch record (issue #190): every (process, T, VDD) point's statistics,
    the worst point per figure and the T x VDD summary tables, each re-derived from the record's
    own columns. The point set and N are those of the record's retained measurement-fingerprint
    inputs (hash-verified), so a missing, duplicate or unexpected point, or a point with too few
    samples, is rejected. The retained per-sample evidence (samples.csv) is cross-checked
    separately (`psrr_mc_crosscheck`)."""
    prov = parse_provenance(text, label)
    inp = fp["inputs"]
    try:
        ax = inp["corners"]
        procs, temps, sups, vcms = ax["process"], ax["temperature_c"], ax["supply_v"], ax["vcm_v"]
        n_want = int(inp["monte_carlo"]["n"])
        seed, vary = int(inp["monte_carlo"]["seed"]), inp["monte_carlo"]["vary"]
    except (KeyError, TypeError, ValueError):
        raise ReportError(f"{label}: retained measurement-fingerprint inputs do not describe a full PSRR grid "
                          "(need \"grid\": \"full\", corners.process/temperature_c/supply_v/vcm_v and "
                          "monte_carlo.n/seed/vary)") from None
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if len(vcms) != len(sups) or [round(float(v) / 2, 6) for v in sups] != [float(x) for x in vcms]:
        raise ReportError(f"{label}: retained inputs' VCM axis is not VDD/2 of the supplies")
    order = [_okey(p, t, v) for p in procs for t in temps for v in sups]  # driver's grid order
    if len(set(order)) != len(order):
        raise ReportError(f"{label}: retained inputs repeat a grid axis value")
    if n_want < PSRR_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the PSRR mismatch statistic needs N >= {PSRR_MC_MIN_N}")
    gl = re.search(r"^- \*\*Grid\*\*: full PVT -- process ([\w, ]+); T ([\d.,\- ]+) C; VDD ([\d., ]+) V "
                   r"\(VCM = VDD/2: ([\d., ]+) V\) = (\d+) points, `monte_carlo = \{n: (\d+), seed: (\d+), "
                   r"vary: \"(\w+)\"\}` per point -> (\d+) x (\d+) = (\d+) samples$", header_of(text), re.M)
    if not gl:
        raise ReportError(f"{label}: no '- **Grid**' line (processes, T and VDD axes, monte_carlo n / seed / vary)")
    temps_g = [f"{float(x):g}" for x in gl.group(2).replace(" ", "").split(",")]
    sups_g = [f"{float(x):.2f}" for x in gl.group(3).replace(" ", "").split(",")]
    vcms_g = [f"{float(x):g}" for x in gl.group(4).replace(" ", "").split(",")]
    if ([p.strip() for p in gl.group(1).split(",")], temps_g, sups_g, vcms_g) != \
            ([str(p) for p in procs], [f"{float(t):g}" for t in temps], [f"{float(v):.2f}" for v in sups],
             [f"{float(x):g}" for x in vcms]) \
            or (int(gl.group(5)), int(gl.group(6)), int(gl.group(7)), gl.group(8)) != \
               (len(order), n_want, seed, vary) \
            or (int(gl.group(9)), int(gl.group(10)), int(gl.group(11))) != (len(order), n_want, len(order) * n_want):
        raise ReportError(f"{label}: Grid line disagrees with the retained inputs "
                          f"({len(order)} points, N={n_want}, seed {seed}, vary \"{vary}\")")
    stats, knee_dcs = _psrr_mc_tables(text, label, order, n_want)
    worst = _psrr_mc_worst(text, label, stats, order)
    floors, knees = _psrr_mc_floor_knees(text, label, knee_dcs)
    dev_dc, dev_fu, po, imb = _psrr_mc_controls(text, label)
    _psrr_mc_grid_tv(text, label, stats, order, temps, sups)
    valid = re.search(r"^Result: all (\d+) samples valid;", section(text, "Extraction validation"), re.M)
    if not valid or int(valid.group(1)) != len(order) * n_want:
        raise ReportError(f"{label}: extraction validation does not state all {len(order) * n_want} samples valid "
                          "(the record lists problems, or its count disagrees with the grid)")
    if not re.search(r"^- All blocking checks passed: yes\.", section(text, "Validation summary"), re.M):
        raise ReportError(f"{label}: validation summary does not state 'All blocking checks passed: yes'")
    cov = coverage_of(set(order))
    cov["mc_samples_per_point"] = n_want
    lim = common_limitations(text, prov, cov)
    lim += ["full PVT grid under mismatch: temperature and supply are sampled (VCM = VDD/2 at every supply), "
            "but the RZ/CC passives stay typical at every point and no passive mismatch is modelled",
            f"N={n_want} cannot resolve a 0.135 % tail: neither the linear nor the dB 3-sigma figure is a sample "
            "statistic; the empirical minimum and percentiles are the normality cross-check",
            f"{knees} near-cancellation knee rail(s) (PSRR+ DC band non-flat but monotonically rising) are accepted "
            "with the 0.1 Hz asymptote as their DC figure",
            "supply rejection of the bias generator is excluded (the ideal 10 uA bias source carries no small-signal "
            "current)",
            "mismatch Monte Carlo of the PSRR over the T/VDD grid; no numeric bound proposed, ratified or judged "
            "at any point (DR-6 is proposed, not ratified, and covers mismatch only at 27 C / 3.30 V)"]
    if any(floors.values()):
        lim.append("samples below the numerical floor are clamped, so their PSRR figure is a lower bound: "
                   + ", ".join(f"{s} {n}" for s, n in floors.items()))
    return {"prov": prov, "coverage": cov, "grid": True, "order": order, "stats": stats, "worst": worst,
            "n_per_point": n_want, "n_total": len(order) * n_want, "seed": seed, "vary": vary,
            "t_c": None, "vdd": None,
            "temps_c": [f"{float(t):g}" for t in temps], "supplies_v": [f"{float(v):.2f}" for v in sups],
            "vcm_of": {f"{float(v):.2f}": float(x) for v, x in zip(sups, vcms)},
            "knee_rails": knees, "knee_dcs": {f"{s}|{fmt_point(*k)}": v for (s, k), v in knee_dcs.items()},
            "floor_samples": floors,
            "controls": {"switch_off_max_dev_db_dc_to_1m": dev_dc, "switch_off_max_dev_db_at_fu": dev_fu,
                         "process_only_psrr_plus_dc_sigma_db": float(po.group(1)),
                         "process_only_psrr_minus_dc_sigma_db": float(po.group(2)),
                         "process_only_offset_sigma_mv": float(po.group(3)),
                         "imbalance": imb},
            "limitations": lim}


def _psrr_mc_grid_tv(text: str, label: str, stats: dict, order: list, temps, sups) -> None:
    """The T x VDD worst-corner tables of a full-grid PSRR record (linear
    3-sigma per DR-6 figure), each cell checked against the per-point table."""
    sec = section(text, "Linear 3-sigma by temperature and supply")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    tables = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    heads = {(s, k): f"{s} {h}" for s, _, _ in PSRR_MC_SIDES for h, k, _ in PSRR_MC_FIGS if k in ("dc", "1k", "10k")}
    if sorted(tables) != sorted(heads.values()):
        raise ReportError(f"{label}: T x VDD tables {sorted(tables)} are not the expected {sorted(heads.values())}")
    for (sname, key), head in heads.items():
        rail = next(r for s, r, _ in PSRR_MC_SIDES if s == sname)
        lines = [ln for ln in tables[head].splitlines() if ln.startswith("|")][2:]
        if len(lines) != len(temps):
            raise ReportError(f"{label}: T x VDD table '{head}' has {len(lines)} rows, the grid has {len(temps)} "
                              "temperatures")
        got: dict = {}
        for ln in lines:
            c = split_cells(ln)
            m = re.fullmatch(r"(-?[\d.]+) C", c[0]) if c else None
            if not m or len(c) != len(sups) + 1:
                raise ReportError(f"{label}: malformed T x VDD row under '{head}': {ln.strip()}")
            for v, cell in zip(sups, c[1:]):
                mc_ = re.fullmatch(r"([\d.]+) \((\w+)\)", cell)
                if not mc_:
                    raise ReportError(f"{label}: malformed T x VDD cell '{cell}' under '{head}'")
                got[(f"{float(m.group(1)):g}", f"{float(v):.2f}")] = (float(mc_.group(1)), mc_.group(2))
        if {t for t, _ in got} != {f"{float(t):g}" for t in temps}:
            raise ReportError(f"{label}: T x VDD table '{head}' does not carry exactly the grid's temperatures")
        for t in temps:
            for v in sups:
                tk = (f"{float(t):g}", f"{float(v):.2f}")
                pts = [k for k in order if k[1] == tk[0] and k[2] == tk[1]]
                kk = min(pts, key=lambda k: stats[_pm_key(sname, key)][k]["lin3"])
                cell = got.get(tk)
                if cell is None or cell[1] != kk[0] or abs(cell[0] - stats[_pm_key(sname, key)][kk]["lin3"]) > _CM_TOL:
                    raise ReportError(f"{label}: T x VDD table '{head}' at {tk[0]} C / {tk[1]} V "
                                      f"({'missing' if cell is None else f'{cell[0]:.2f} ({cell[1]})'}) disagrees "
                                      f"with its own per-point table "
                                      f"({stats[_pm_key(sname, key)][kk]['lin3']:.2f} ({kk[0]}))")


def psrr_mc_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every side's and figure's per-point statistics, the worst points and the knee
    accounting from the record's retained per-sample evidence (corners/<rid>/samples.csv),
    within display precision. Offline."""
    import csv
    import math
    rel = f"sim/psrr-mc/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    qs = [(r, q, s) for _, r, _ in PSRR_MC_SIDES for _, _, s in PSRR_MC_FIGS for q in ("psrr", "asup")]
    need = ["process", "temperature_c", "vdd_v", "sample_index", "valid", "knee_rails"] + \
           [f"pmc_{q}_{r}_{s}" for r, q, s in qs] + [f"pmc_ad_{s}" for _, _, s in PSRR_MC_FIGS] + \
           ["pmc_ad_b0"] + [f"pmc_{r}_aband0" for _, r, _ in PSRR_MC_SIDES]
    if ex.get("grid"):
        need += ["vcm_v"]  # issue #190: the grid record's CSV carries the commanded VCM per point
    by: dict = {}
    seen = set()
    knee_csv = 0
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns it needs")
        for r in rd:
            try:
                k = _okey(r["process"], r["temperature_c"], r["vdd_v"])
                idx, valid = int(r["sample_index"]), int(r["valid"])
                vals = {(q, rr, s): float(r[f"pmc_{q}_{rr}_{s}"]) for rr, q, s in qs}
                vals.update({("ad", None, s): float(r[f"pmc_ad_{s}"]) for _, _, s in PSRR_MC_FIGS})
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["order"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if ex.get("grid") and abs(float(r["vcm_v"]) - ex["vcm_of"][k[2]]) > 1e-6:
                raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} carries VCM {float(r['vcm_v']):g} V, "
                                  f"not the commanded {ex['vcm_of'][k[2]]:g} V (VCM = VDD/2)")
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if valid != 1 or not all(math.isfinite(v) for v in vals.values()):
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)}")
            seen.add((k, idx))
            knees = [x for x in r["knee_rails"].replace(",", " ").split() if x]
            knee_csv += len(knees)
            try:
                for kr in knees:
                    # a knee sample's DC figure is its 0.1 Hz asymptote, not the band mean (the record's definition)
                    a0 = float(r[f"pmc_{kr}_aband0"])
                    vals[("psrr", kr, "dc")] = float(r["pmc_ad_b0"]) - 20 * math.log10(a0)
                    vals[("asup", kr, "dc")] = a0
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed knee sample {idx} at {fmt_point(*k)}") from None
            by.setdefault(k, []).append(vals)
    for k in ex["order"]:
        if len(by.get(k, [])) != ex["n_per_point"]:
            raise ReportError(f"{label}: insufficient samples in {rel} at {fmt_point(*k)}: {len(by.get(k, []))}, the "
                              f"record states N={ex['n_per_point']}")
    if knee_csv != ex["knee_rails"]:
        raise ReportError(f"{label}: the record states {ex['knee_rails']} near-cancellation knee rail(s) but {rel} "
                          f"flags {knee_csv}")
    for sname, rail, _ in PSRR_MC_SIDES:
        for head, key, s in PSRR_MC_FIGS:
            got = {k: cmrr_mc_stats([v[("psrr", rail, s)] for v in by[k]], [v[("asup", rail, s)] for v in by[k]],
                                    [v[("ad", None, s)] for v in by[k]]) for k in ex["order"]}
            sk = _pm_key(sname, key)
            for k in ex["order"]:
                rec = ex["stats"][sk][k]
                bad = [c for c in PSRR_MC_COLS if not _cm_close(c, rec[c], got[k][c])]
                if bad:
                    raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({sname} {head}): the "
                                      f"record's {', '.join(bad)} disagree(s) with the retained evidence {rel} (e.g. "
                                      f"{bad[0]} {rec[bad[0]]:g} vs {got[k][bad[0]]:g})")
            w = _cm_worst(got, ex["order"])
            for col in ("lin3", "db3", "min", "p5"):
                if abs(ex["worst"][sk][col]["value"] - got[w[col]][col]) > _CM_TOL:
                    raise ReportError(f"{label}: the record's worst {col} for {sname} {head} disagrees with the "
                                      f"retained evidence {rel}")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(by)}


# --------------------------------------------------------------------------
# gain/GBW/PM mismatch Monte Carlo (issue #172): measured, never graded here
# --------------------------------------------------------------------------
GAIN_MC_EXP = "gain-gbw-pm-mc"
GAIN_MC_TITLE = "# Gain / GBW / PM mismatch Monte Carlo record"
GAIN_MC_MIN_N = 300
#: The three AC rows a mismatch sample is compared with, in the record's order:
#: (row key, samples.csv metric column, scale from the column to the published unit, unit)
GAIN_MC_ROWS = (("gain", "dc_gain_db", 1.0, "dB"), ("gbw", "gbw_hz", 1e-6, "MHz"), ("pm", "pm_deg", 1.0, "deg"))
#: Display precision of the record's statistics table (dB / deg at 2 decimals, MHz at 3)
_GAIN_MC_TOL = {"dB": 0.005 + 1e-6, "MHz": 0.0005 + 1e-6, "deg": 0.005 + 1e-6}
_GAIN_MC_FRAC_TOL = 0.00005 + 1e-9  # the failure fraction is displayed to 4 decimals
_GAIN_MC_DERIVE_TOL = 0.03  # mean-3sigma re-derived from displayed (rounded) mean and sigma
_GAIN_MC_ROW_RX = re.compile(r"(gain|gbw|pm) \(>= ([\d.]+) (dB|MHz|deg)\)")


def extract_gain_gbw_pm_mc(text: str, label: str) -> dict:
    """Gain/GBW/PM mismatch Monte Carlo record (issue #129): per-corner valid / mean / sigma /
    mean-3sigma / below-bound / invalid / failure-fraction for each of the three AC rows, each
    re-derived from the record's own columns and stated against the ratified bounds. The retained
    per-sample evidence (samples.csv) is cross-checked separately (`gain_gbw_pm_mc_crosscheck`).
    The record grades nothing: the deterministic gain/GBW/PM record stays responsible for the
    graded rows."""
    if not text.lstrip().startswith(GAIN_MC_TITLE):
        raise ReportError(f"{label}: not a '{GAIN_MC_TITLE.lstrip('# ')}' record")
    prov = parse_provenance(text, label)
    pop = re.search(r"^- \*\*Population\*\*: process ([\w, ]+); (-?\d+) C; VDD ([\d.]+) V .*?"
                    r"`monte_carlo = \{n: (\d+), seed: (\d+), vary: \"(\w+)\"\}` -> (\d+) x (\d+) = (\d+) samples",
                    header_of(text), re.M)
    if not pop:
        raise ReportError(f"{label}: no Population line (processes, T, VDD, monte_carlo n / seed / vary)")
    procs = [p.strip() for p in pop.group(1).split(",")]
    t_c, vdd = pop.group(2), pop.group(3)
    n_want, seed, vary = int(pop.group(4)), int(pop.group(5)), pop.group(6)
    n_pts, n_each, n_total = int(pop.group(7)), int(pop.group(8)), int(pop.group(9))
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if n_want < GAIN_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per corner; "
                          f"the mismatch statistic needs N >= {GAIN_MC_MIN_N}")
    if (n_pts, n_each, n_total) != (len(procs), n_want, len(procs) * n_want) or len(set(procs)) != len(procs):
        raise ReportError(f"{label}: Population line is inconsistent ({n_pts} x {n_each} = {n_total} samples for "
                          f"{len(procs)} processes at N={n_want})")
    order = [_okey(p, t_c, vdd) for p in procs]
    sec = section(text, "Statistics per corner")
    lines = [ln for ln in sec.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no statistics table under '## Statistics per corner'")
    stats: dict = {}
    bounds: dict = {}
    units: dict = {}
    for ln in lines[2:]:
        c = split_cells(ln)
        m = re.fullmatch(_PT, c[0]) if c else None
        if not m or len(c) != 9:
            raise ReportError(f"{label}: malformed statistics row: {ln.strip()}")
        k = _okey(*m.groups())
        if k not in order:
            raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} in the statistics table "
                              "(not in the Population line)")
        rm = re.fullmatch(_GAIN_MC_ROW_RX, c[1])
        if not rm:
            raise ReportError(f"{label}: malformed statistics row label '{c[1]}' "
                              "(expected '<row> (>= <bound> <unit>)')")
        rk, bval, bunit = rm.group(1), float(rm.group(2)), rm.group(3)
        if rk in stats.setdefault(k, {}):
            raise ReportError(f"{label}: duplicate {rk} row at {fmt_point(*k)}")
        try:
            valid, n = (int(x) for x in c[2].split("/"))
            mean_s, sigma_s, m3_s = c[3], c[4], c[5]
            mean, sigma, m3 = (float(x.split()[0]) for x in (mean_s, sigma_s, m3_s))
            unit = mean_s.split()[1]
            below, invalid = int(c[6]), int(c[7])
            fm = re.fullmatch(r"(\d+)/(\d+) = ([\d.]+)", c[8])
            fbelow, fden, frac = (int(fm.group(1)), int(fm.group(2)), float(fm.group(3))) if fm else (None, None, None)
        except (ValueError, IndexError):
            raise ReportError(f"{label}: malformed statistics row: {ln.strip()}") from None
        if unit != bunit or units.setdefault(rk, unit) != unit:
            raise ReportError(f"{label}: {rk} row at {fmt_point(*k)} carries unit '{unit}', but its bound is "
                              f"'{bunit}'" if unit != bunit else
                              f"{label}: {rk} rows disagree on their unit ({units[rk]} vs {unit})")
        if n != n_want:
            raise ReportError(f"{label}: fixed denominator violated at {fmt_point(*k)} ({rk}): the table states "
                              f"N={n}, the request is N={n_want} per corner")
        if valid + invalid != n or not (0 <= below <= valid) or invalid < 0:
            raise ReportError(f"{label}: inconsistent population at {fmt_point(*k)} ({rk}): valid {valid} + "
                              f"invalid {invalid} != N {n}, or below-bound {below} exceeds the valid count")
        for fname, fval in (("mean", mean), ("sigma", sigma), ("mean-3sigma", m3)):
            if not math.isfinite(fval):
                raise ReportError(f"{label}: non-finite {fname} at {fmt_point(*k)} ({rk}): the published "
                                  "statistics must be finite")
        if sigma <= 0:
            raise ReportError(f"{label}: non-positive sigma at {fmt_point(*k)} ({rk})")
        if abs(m3 - (mean - 3 * sigma)) > _GAIN_MC_DERIVE_TOL:
            raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({rk}): its mean-3sigma column "
                              "disagrees with its own mean and sigma")
        if (fbelow, fden) != (below + invalid, n) or abs(frac - (below + invalid) / n) > _GAIN_MC_FRAC_TOL:
            raise ReportError(f"{label}: inconsistent failure fraction at {fmt_point(*k)} ({rk}): it does not state "
                              f"({below} + {invalid})/{n} to the displayed precision")
        stats[k][rk] = {"n": n, "valid": valid, "mean": mean, "sigma": sigma, "mean3": m3,
                        "mean_text": mean_s, "sigma_text": sigma_s, "mean3_text": m3_s,
                        "below": below, "invalid": invalid, "fraction": frac, "fraction_text": c[8],
                        "bound": f">= {rm.group(2)} {bunit}", "bound_value": bval}
    missing = [(k, rk) for k in order for rk, _, _, _ in GAIN_MC_ROWS if rk not in stats.get(k, {})]
    if missing:
        k, rk = missing[0]
        raise ReportError(f"{label}: statistics table is missing {len(missing)} of {len(order) * 3} rows "
                          f"(first: {rk} at {fmt_point(*k)})")
    for k in order:  # one shared population per corner: the three rows must agree on it
        vs = {stats[k][rk]["valid"] for rk, _, _, _ in GAIN_MC_ROWS}
        iv = {stats[k][rk]["invalid"] for rk, _, _, _ in GAIN_MC_ROWS}
        if len(vs) > 1 or len(iv) > 1:
            raise ReportError(f"{label}: the three rows disagree on the shared population at {fmt_point(*k)} "
                              f"(valid {sorted(vs)}, invalid {sorted(iv)})")
    bounds = {}
    for rk, _, _, _ in GAIN_MC_ROWS:
        ref = stats[order[0]][rk]
        for k in order:
            if stats[k][rk]["bound"] != ref["bound"]:
                raise ReportError(f"{label}: {rk} bound at {fmt_point(*k)} is '{stats[k][rk]['bound']}', but the "
                                  f"shared bound at {fmt_point(*order[0])} is '{ref['bound']}': the ratified "
                                  "bound must be identical at every corner")
        bounds[rk] = {"text": ref["bound"], "value": ref["bound_value"], "unit": units[rk]}
    cov = coverage_of(set(order))
    cov["mc_samples_per_corner"] = n_want
    n_invalid = sum(next(iter({stats[k][rk]["invalid"] for rk, _, _, _ in GAIN_MC_ROWS})) for k in order)
    lim = common_limitations(text, prov, cov)
    lim += [f"temperature and supply are NOT sampled under mismatch: {t_c} C / {vdd} V only; the deterministic "
            "gain/GBW/PM record covers the full T x VDD grid (matched devices)"]
    if "typical passives" in text:
        lim.append("typical passives only (res_typical, mimcap_typical): no passive-corner axis and no passive "
                   "mismatch")
    lim += ["MOS mismatch only (sw_stat_mismatch = 1); resistor and capacitor mismatch not modelled",
            f"mean - 3 sigma is a descriptive figure from N = {n_want} per corner (sample sigma, ddof = 1): "
            "not a tail estimate and not a yield claim",
            f"N = {n_want} with zero observed failures does not establish yield: T1 signoff item 6 accepts only "
            "yield envelopes and stays unmet",
            "mismatch Monte Carlo of the three AC rows; the ratified bounds are graded by the deterministic "
            "record, and no bound is proposed or relaxed here"]
    if n_invalid:
        lim.append(f"the record discloses an invalid population ({n_invalid} samples across the corners): invalid "
                   "samples are excluded from the statistics and count as failures of all three rows, never "
                   "silently dropped")
    return {"prov": prov, "coverage": cov, "order": order, "stats": stats, "bounds": bounds,
            "n_per_point": n_want, "n_total": n_total, "seed": seed, "vary": vary, "t_c": t_c, "vdd": vdd,
            "n_invalid": n_invalid, "limitations": lim}


def gain_gbw_pm_mc_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every corner's statistics from the record's retained per-sample evidence
    (corners/<rid>/samples.csv) within the record's display precision, and re-count the below-bound
    samples against the record's stated bounds. Validates the identity (exactly the indices
    0..N-1 per corner, each once), the klt status and the finite valid metrics. Offline."""
    import csv
    import math
    import statistics
    rel = f"sim/{GAIN_MC_EXP}/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    need = ["process", "temperature_c", "vdd_v", "sample_index", "seed", "mismatch_seed", "process_seed",
            "klt_status", "valid", "reason"] + [col for _, col, _, _ in GAIN_MC_ROWS]
    by: dict = {}
    idx_by: dict = {}
    seen = set()
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns it needs")
        for r in rd:
            try:
                k = _okey(r["process"], r["temperature_c"], r["vdd_v"])
                idx = int(r["sample_index"])
                int(r["seed"]), int(r["mismatch_seed"]), int(r["process_seed"])
                valid = int(r["valid"])
                vals = {rk: float(r[col]) * scale for rk, col, scale, _ in GAIN_MC_ROWS}
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["order"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if not 0 <= idx < ex["n_per_point"]:
                raise ReportError(f"{label}: {rel}: sample index {idx} at {fmt_point(*k)} is outside the declared "
                                  f"identity 0..{ex['n_per_point'] - 1}")
            if valid not in (0, 1):
                raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} has invalid flag {valid}")
            if valid == 1:
                if r["klt_status"] != "pass":
                    raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} is marked valid but its klt "
                                      f"status is '{r['klt_status']}'")
                if not all(math.isfinite(v) for v in vals.values()):
                    raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} carries a non-finite metric")
            elif not r["reason"].strip():
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)} discloses no reason")
            seen.add((k, idx))
            idx_by.setdefault(k, set()).add(idx)
            if valid == 1:
                by.setdefault(k, []).append(vals)
    for k in ex["order"]:
        missing = sorted(set(range(ex["n_per_point"])) - idx_by.get(k, set()))
        if missing:
            raise ReportError(f"{label}: {rel}: {len(missing)} sample indices missing at {fmt_point(*k)} "
                              f"(first: {missing[0]}; the identity is exactly 0..{ex['n_per_point'] - 1}, each once)")
        valids = by.get(k, [])
        n_invalid = len(idx_by[k]) - len(valids)
        if len(valids) < 2:
            raise ReportError(f"{label}: {rel}: only {len(valids)} valid samples at {fmt_point(*k)}: the corner's "
                              "statistics cannot be re-derived")
        for rk, _, _, unit in GAIN_MC_ROWS:
            rec = ex["stats"][k][rk]
            col = [v[rk] for v in valids]
            below = sum(1 for x in col if x < rec["bound_value"])
            mean, sigma = statistics.fmean(col), statistics.stdev(col)
            tol = _GAIN_MC_TOL[unit]
            for f, fname in (("mean", "mean"), ("sigma", "sigma"), ("mean3", "mean-3sigma")):
                if not math.isfinite(rec[f]):
                    raise ReportError(f"{label}: non-finite {fname} at {fmt_point(*k)} ({rk}): the published "
                                      "statistics must be finite")
            bad = []
            if len(valids) != rec["valid"]:
                bad.append("valid count")
            if n_invalid != rec["invalid"]:
                bad.append("invalid count")
            if abs(rec["mean"] - mean) > tol:
                bad.append("mean")
            if abs(rec["sigma"] - sigma) > tol:
                bad.append("sigma")
            if abs(rec["mean3"] - (mean - 3 * sigma)) > tol:
                bad.append("mean-3sigma")
            if below != rec["below"]:
                bad.append("below-bound count")
            if abs(rec["fraction"] - (below + n_invalid) / ex["n_per_point"]) > _GAIN_MC_FRAC_TOL:
                bad.append("failure fraction")
            if bad:
                raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({rk}): the record's "
                                  f"{', '.join(bad)} disagree(s) with the retained evidence {rel}")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(idx_by)}


def extract_psrr(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    cov = coverage_of(grid_points(text, "slash"))
    lim = common_limitations(text, prov, cov)
    if re.search(r"\*\*Avss changes sign", text):
        lim.append("PSRR-: Avss changes sign across the grid, so low-frequency PSRR- near the flip is a cancellation, "
                   "not design margin; the worst-case figure is the one to use")
    lim.append("systematic figure of a perfectly matched schematic; mismatch-limited PSRR is not covered")
    lim.append("no ratified bound (DR-3 residual e4)")
    return {"prov": prov, "coverage": cov,
            "figures": {"PSRR+": _worst_table(text, "PSRR+ worst case", label),
                        "PSRR-": _worst_table(text, "PSRR- worst case", label)},
            "limitations": lim}


_SSP_ROW = {"Quiescent power": "power", "Slew rate": "slew", "Output swing": "swing"}
#: per-point table column holding each row's judged value ("<value> ok|FAIL")
_SSP_COL = {"power": "Power (uW)", "slew": "Slew min", "swing": "Swing (Vpp)"}


def extract_ssp(text: str, label: str) -> dict:
    """Slew / swing / quiescent power: the ratified rows this record judged.

    A record may judge only a subset of the three rows (`--figures`); rows it
    did not measure (or could not run) are simply absent. Every judged row's
    pass count and worst value are re-derived from the record's own per-point
    table.
    """
    prov = parse_provenance(text, label)
    sec = section(text, "Verdicts")
    rx = re.compile(r"^\| (Quiescent power|Slew rate[^|]*|Output swing) \| ((?:<=|>=) [^|]+?) \| \*\*(PASS|FAIL)\*\* \| "
                    r"(\d+)/(\d+) \| ([\d.]+|n/a) (uW|V/us|Vpp) \| ([^|]+?) \|$", re.M)
    rows = {}
    for m in rx.finditer(sec):
        key = _SSP_ROW[m.group(1).split(" (")[0]]
        rows[key] = {"label": m.group(1), "bound_text": m.group(2).strip(), "verdict": m.group(3),
                     "pass": int(m.group(4)), "total": int(m.group(5)), "worst": f"{m.group(6)} {m.group(7)}",
                     "worst_value": float("nan") if m.group(6) == "n/a" else float(m.group(6)),
                     "worst_corner": m.group(8).strip()}
    if not rows:
        raise ReportError(f"{label}: no judged slew / swing / power row (every figure NOT RUN or not measured?)")
    pt = section(text, "All 45 points")
    lines = [ln for ln in pt.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no per-point table under '## All 45 points'")
    head = split_cells(lines[0])
    body = [split_cells(ln) for ln in lines[2:]]
    for key, r in rows.items():
        col = _SSP_COL[key]
        if col not in head:
            raise ReportError(f"{label}: per-point table has no '{col}' column")
        i = head.index(col)
        pts, vals, n_pass = set(), [], 0
        for c in body:
            cell = c[i]
            if cell == "n/a":
                continue
            pts.add((c[0].strip("`"), c[1], c[2]))
            mv = re.match(r"([\d.]+) (ok|FAIL)$", cell)
            if mv:
                vals.append(float(mv.group(1)))
                n_pass += mv.group(2) == "ok"
        worst = (max if r["bound_text"].startswith("<=") else min)(vals) if vals else float("nan")
        # an invalid point makes the record's worst value n/a (the row FAILs); only then may it be non-finite
        worst_ok = (abs(worst - r["worst_value"]) <= 0.006 if r["worst_value"] == r["worst_value"]
                    else r["verdict"] == "FAIL")
        if r["total"] != len(pts) or r["pass"] != n_pass or not worst_ok:
            raise ReportError(
                f"{label}: {key} verdict table ({r['pass']}/{r['total']}, worst {r['worst_value']}) "
                f"disagrees with its own per-point table ({n_pass}/{len(pts)}, worst {worst})")
        r["bound_value"] = float(re.search(r"([\d.]+)", r["bound_text"]).group(1))
        r["coverage"] = coverage_of(pts)
        r["limitations"] = common_limitations(text, prov, r["coverage"])
    stretch = re.search(r"the >= ([\d.]+) Vpp stretch holds at (\d+)/(\d+)", text)
    if "swing" in rows and stretch:
        rows["swing"]["stretch"] = (stretch.group(1), f"{stretch.group(2)}/{stretch.group(3)}")
    if "swing" in rows:
        if re.search(r"swing re-derivation from the committed `swing/\*\.dat` files .*? (\d+)/\1 points reproduce", text):
            rows["swing"]["rederived"] = True
        else:
            rows["swing"]["limitations"].append(
                "the per-point swing data in this record holds vin/vout only: the M6/M7 saturation vectors that "
                "decide the swing edges are not committed, so the verdict cannot be re-derived from the record")
    cov = next(iter(rows.values()))["coverage"]
    return {"prov": prov, "coverage": cov, "rows": rows, "limitations": common_limitations(text, prov, cov)}


# --------------------------------------------------------------------------
# input common-mode range (issue #90): measured, never graded while proposed
# --------------------------------------------------------------------------
ICMR_EXP = "input-common-mode"
ICMR_ROW_KEY = "input-common-mode-range"
ICMR_TARGET_MV = 1200  # the explicit VCM sample the record grades at every PVT point
_PT = r"(\w+) / (-?\d+) C / ([\d.]+) V"
_IV_RX = re.compile(r"\[(\d+\.\d+), (\d+\.\d+)\] V")


def _mv(s: str) -> int:
    return round(float(s) * 1000)


def _v(mv: int) -> str:
    return f"{mv / 1000:.3f}"


def fmt_ivs(ivs: list) -> str:
    return ", ".join(f"[{_v(i['lo'])}, {_v(i['hi'])}] V" for i in ivs) if ivs else "empty"


def _parse_ivs(cell: str, label: str, what: str) -> list:
    """'[a, b] V, [c, d] V' (disjoint components kept apart) or 'none'/'empty' -> [(lo_mv, hi_mv)]."""
    cell = cell.strip().rstrip(".")
    if cell in ("none", "empty"):
        return []
    found = [(_mv(a), _mv(b)) for a, b in _IV_RX.findall(cell)]
    if not found or _IV_RX.sub("", cell).replace(",", "").strip():
        raise ReportError(f"{label}: malformed interval list in {what}: '{cell}'")
    return found


def _check_disjoint(ivs: list, label: str, what: str) -> None:
    for lo, hi in ivs:
        if lo > hi:
            raise ReportError(f"{label}: inconsistent interval data in {what}: [{_v(lo)}, {_v(hi)}] V has low > high")
    for (_, a_hi), (b_lo, _) in zip(ivs, ivs[1:]):
        if b_lo <= a_hi:
            raise ReportError(f"{label}: inconsistent interval data in {what}: components overlap or are "
                              "out of order (disjoint passing intervals must be listed separately, low to high)")


def _unc(cell: str, label: str) -> int | None:
    cell = cell.strip()
    if cell == "scan edge":
        return None
    m = re.fullmatch(r"(\d+) mV", cell)
    if not m or int(m.group(1)) <= 0:
        raise ReportError(f"{label}: malformed transition bracket '{cell}' in the per-point interval table")
    return int(m.group(1))


def icmr_intersection(points: list, per_point: dict) -> list:
    """Intersection of every point's union of passing intervals, components kept disjoint.

    Mirrors the driver's rule: each endpoint keeps the bracket, neighbour and PVT
    point of the interval that set it (on a tie, the earlier point in grid order).
    A point without any interval empties the result.
    """
    cur = None
    for pt in points:
        tagged = [dict(i, lo_src=fmt_point(*pt), hi_src=fmt_point(*pt)) for i in per_point[pt]]
        if cur is None:
            cur = tagged
            continue
        nxt = []
        for a in cur:
            for b in tagged:
                lo_s = b if b["lo"] > a["lo"] else a
                hi_s = b if b["hi"] < a["hi"] else a
                lo, hi = max(a["lo"], b["lo"]), min(a["hi"], b["hi"])
                if lo <= hi:
                    nxt.append({"lo": lo, "hi": hi, "lo_unc": lo_s["lo_unc"], "hi_unc": hi_s["hi_unc"],
                                "lo_nb": lo_s["lo_nb"], "hi_nb": hi_s["hi_nb"],
                                "lo_src": lo_s["lo_src"], "hi_src": hi_s["hi_src"]})
        cur = sorted(nxt, key=lambda i: i["lo"])
        if not cur:
            return []
    return cur or []


def _grid_order(pts) -> list:
    """Grid order of the driver (process, VDD, T), so endpoint ties resolve the same way."""
    return sorted(pts, key=lambda p: (corner_sort_key(p[0]), float(p[2]), int(p[1])))


def _margin_claim(m, label: str, what: str) -> dict:
    if not m:
        raise ReportError(f"{label}: no '{what}' headline line")
    return m


def extract_icmr(text: str, label: str) -> dict:
    """Follower-biased ICMR record: per-point passing intervals, the conservative common
    interval, its edge-binding corners and brackets, the explicit 1.20 V sample and the
    smallest saturation margins, each re-derived from the record's own per-point tables.

    The retained per-sample evidence (samples.csv) is cross-checked separately
    (`icmr_crosscheck`), since it lives beside the record rather than in it.
    """
    prov = parse_provenance(text, label)
    head = header_of(text)
    ax = re.search(r"VCM scanned 0\.\.VDD at <= (\d+) mV .*?transitions refined to <= (\d+) mV", head)
    smp = re.search(r"^- \*\*Samples\*\*: (\d+) \(PVT point, VCM\) samples x \d+ excitations: "
                    r"(\d+) pass, (\d+) fail, (\d+) invalid\.", head, re.M)
    if not (ax and smp):
        raise ReportError(f"{label}: no VCM scan/refinement axes line or samples line in the record header")
    n_samples, n_pass, n_fail, n_inv = (int(x) for x in smp.groups())
    if n_pass + n_fail + n_inv != n_samples:
        raise ReportError(f"{label}: inconsistent sample counts ({n_pass} + {n_fail} + {n_inv} != {n_samples})")

    # ---- per-point passing intervals (every contiguous component, never bridged) ----
    sec = section(text, "Passing intervals at every PVT point")
    lines = [ln for ln in sec.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no per-point table under '## Passing intervals at every PVT point'")
    per_point, strict, st120, order = {}, {}, {}, []
    for ln in lines[2:]:
        c = split_cells(ln)
        m = re.fullmatch(_PT, c[0]) if c else None
        if not m or len(c) != 9:
            raise ReportError(f"{label}: malformed per-point interval row: {ln.strip()}")
        pt = m.groups()
        what = f"the per-point table at {c[0]}"
        if pt not in per_point:
            order.append(pt)
            per_point[pt] = []
            strict[pt] = _parse_ivs(c[8], label, f"{what} (strict column)")
            _check_disjoint(strict[pt], label, f"{what} (strict column)")
            st120[pt] = c[7]
        elif st120[pt] != c[7] or strict[pt] != _parse_ivs(c[8], label, what):
            raise ReportError(f"{label}: inconsistent interval data in {what}: its rows disagree on the 1.20 V "
                              "status or the strict intervals")
        if c[1] == "none":
            if per_point[pt] or c[2] != "0":
                raise ReportError(f"{label}: inconsistent interval data in {what}: 'none' beside other intervals")
            continue
        ivs = _parse_ivs(c[1], label, what)
        if len(ivs) != 1 or not c[2].isdigit() or int(c[2]) < 1:
            raise ReportError(f"{label}: malformed interval row in {what}: one interval and a sample count expected")
        (lo, hi), = ivs
        per_point[pt].append({"lo": lo, "hi": hi, "n": int(c[2]), "lo_unc": _unc(c[3], label), "lo_nb": c[4],
                              "hi_unc": _unc(c[5], label), "hi_nb": c[6]})
        _check_disjoint([(i["lo"], i["hi"]) for i in per_point[pt]], label, what)
    for pt in order:
        if st120[pt] not in ("pass", "fail", "invalid", "n/a"):
            raise ReportError(f"{label}: unknown 1.20 V status '{st120[pt]}' at {fmt_point(*pt)}")
        inside = any(i["lo"] <= ICMR_TARGET_MV <= i["hi"] for i in per_point[pt])
        if inside != (st120[pt] == "pass"):
            raise ReportError(f"{label}: inconsistent interval data at {fmt_point(*pt)}: 1.20 V status "
                              f"'{st120[pt]}' but 1.20 V is {'inside' if inside else 'outside'} its passing intervals")
    points = _grid_order(order)

    # ---- conservative common interval, re-derived and compared with the headline ----
    inter = icmr_intersection(points, per_point)
    inter_s = icmr_intersection(points, {p: [{"lo": lo, "hi": hi, "lo_unc": None, "hi_unc": None, "lo_nb": "",
                                              "hi_nb": ""} for lo, hi in strict[p]] for p in points})
    hl = section(text, "Headline")
    m = re.search(r"^- \*\*Intersection over the (\d+) PVT points\*\* \(every contiguous component\): (.*)$", hl, re.M)
    if m:
        claimed = _parse_ivs(m.group(2), label, "the headline intersection")
    elif re.search(r"^- \*\*Intersection of the passing ranges over the \d+ PVT points: EMPTY\.\*\*", hl, re.M):
        claimed = []
    else:
        raise ReportError(f"{label}: no headline intersection line")
    if m and int(m.group(1)) != len(points):
        raise ReportError(f"{label}: headline intersection claims {m.group(1)} PVT points; "
                          f"the per-point table has {len(points)}")
    if claimed != [(i["lo"], i["hi"]) for i in inter]:
        raise ReportError(f"{label}: headline intersection {fmt_ivs([{'lo': a, 'hi': b} for a, b in claimed])} "
                          f"disagrees with its own per-point table ({fmt_ivs(inter)})")
    tol = re.search(r"^- Intersection with the 1 mV tolerance: (.*?); strict \(0 mV\): (.*)$", hl, re.M)
    if not tol or _parse_ivs(tol.group(1), label, "the tolerance line") != claimed:
        raise ReportError(f"{label}: tolerance-sensitivity intersection line missing or disagrees with the headline")
    if _parse_ivs(tol.group(2), label, "the strict intersection") != [(i["lo"], i["hi"]) for i in inter_s]:
        raise ReportError(f"{label}: strict (0 mV) intersection disagrees with its own per-point strict intervals "
                          f"({fmt_ivs(inter_s)})")
    comp = next((i for i in inter if i["lo"] <= ICMR_TARGET_MV <= i["hi"]), None)
    cm = re.search(r"^- \*\*Component containing 1\.20 V\*\*: \[([\d.]+), ([\d.]+)\] V; low endpoint ([\d.]+) V set by "
                   + _PT + r" \(bracket (scan edge|\d+ mV) to a `([^`]+)` sample\), high endpoint ([\d.]+) V set by "
                   + _PT + r" \(bracket (scan edge|\d+ mV) to a `([^`]+)` sample\)\.$", hl, re.M)
    if comp is None:
        if cm or not re.search(r"^- \*\*1\.20 V is NOT inside the intersection\*\*", hl, re.M):
            raise ReportError(f"{label}: headline 1.20 V component disagrees with its own per-point table "
                              "(1.20 V is outside every common component)")
    else:
        if not cm:
            raise ReportError(f"{label}: no headline 'Component containing 1.20 V' line")
        g = cm.groups()
        got = {"lo": _mv(g[0]), "hi": _mv(g[1]), "lo_src": fmt_point(*g[3:6]), "lo_unc": _unc(g[6], label),
               "lo_nb": g[7], "hi_src": fmt_point(*g[9:12]), "hi_unc": _unc(g[12], label), "hi_nb": g[13]}
        if _mv(g[2]) != got["lo"] or _mv(g[8]) != got["hi"] or any(got[k] != comp[k] for k in got):
            raise ReportError(f"{label}: headline 1.20 V component (edges / binding corners / brackets) disagrees "
                              f"with its own per-point table ([{_v(comp['lo'])}, {_v(comp['hi'])}] V, low set by "
                              f"{comp['lo_src']}, high set by {comp['hi_src']})")

    # ---- explicit 1.20 V sample at every point ----
    sec = section(text, "Explicit 1.20 V samples")
    t120 = {}
    for ln in [ln for ln in sec.splitlines() if ln.startswith("|")][2:]:
        c = split_cells(ln)
        m = re.fullmatch(_PT, c[0]) if c else None
        if not m or len(c) != 7 or m.groups() in t120:
            raise ReportError(f"{label}: malformed or duplicate explicit 1.20 V row: {ln.strip()}")
        row = {"status": c[1]}
        if c[1] in ("pass", "fail"):
            try:
                row.update(gain_db=float(c[2]), device=c[3], margin_mv=float(c[4]))
            except ValueError:
                raise ReportError(f"{label}: malformed explicit 1.20 V row: {ln.strip()}") from None
        elif c[1] != "invalid":
            raise ReportError(f"{label}: unknown explicit 1.20 V status '{c[1]}' at {c[0]}")
        t120[m.groups()] = row
    if set(t120) != set(points):
        raise ReportError(f"{label}: explicit 1.20 V table covers {len(t120)} points, the interval table "
                          f"{len(points)} (or a different set)")
    for pt in points:
        want = st120[pt] if st120[pt] != "n/a" else "invalid"
        if t120[pt]["status"] != want:
            raise ReportError(f"{label}: inconsistent 1.20 V status at {fmt_point(*pt)}: explicit table "
                              f"'{t120[pt]['status']}', interval table '{st120[pt]}'")
    cnt = {s: sum(1 for r in t120.values() if r["status"] == s) for s in ("pass", "fail", "invalid")}
    verdict = "fails" if cnt["fail"] else ("unknown" if cnt["invalid"] else "meets")
    v = re.search(r"^- \*\*1\.20 V explicit sample at all (\d+) combinations\*\*: \*\*(\w+)\*\* "
                  r"\((\d+) pass, (\d+) fail, (\d+) invalid/missing\)\.", hl, re.M)
    if not v or (int(v.group(1)), v.group(2).lower(), int(v.group(3)), int(v.group(4)), int(v.group(5))) != \
            (len(points), verdict, cnt["pass"], cnt["fail"], cnt["invalid"]):
        raise ReportError(f"{label}: headline 1.20 V sample line disagrees with its explicit 1.20 V table "
                          f"({verdict}: {cnt['pass']} pass, {cnt['fail']} fail, {cnt['invalid']} invalid/missing)")
    valid120 = {p: r for p, r in t120.items() if "margin_mv" in r}
    sm = _margin_claim(re.search(r"^- Smallest device margin at 1\.20 V: (\w+) ([+-][\d.]+) mV \(plateau gain ([\d.]+) dB\) "
                                 r"at " + _PT + r" / VCM 1\.200 V\.$", hl, re.M), label, "Smallest device margin at 1.20 V")
    wg = _margin_claim(re.search(r"^- Worst plateau gain at 1\.20 V: ([\d.]+) dB, limiting (\w+) ([+-][\d.]+) mV at "
                                 + _PT + r" / VCM 1\.200 V\.$", hl, re.M), label, "Worst plateau gain at 1.20 V")
    min_m = min(r["margin_mv"] for r in valid120.values())
    min_g = min(r["gain_db"] for r in valid120.values())
    sm_pt, wg_pt = sm.groups()[3:6], wg.groups()[3:6]
    if (abs(float(sm.group(2)) - min_m) > 0.051 or sm_pt not in valid120
            or abs(valid120[sm_pt]["margin_mv"] - min_m) > 0.051 or valid120[sm_pt]["device"] != sm.group(1)):
        raise ReportError(f"{label}: headline smallest 1.20 V margin ({sm.group(1)} {sm.group(2)} mV at "
                          f"{fmt_point(*sm_pt)}) disagrees with its explicit 1.20 V table (min {min_m:+.1f} mV)")
    if abs(float(wg.group(1)) - min_g) > 0.0051 or wg_pt not in valid120 or abs(valid120[wg_pt]["gain_db"] - min_g) > 0.0051:
        raise ReportError(f"{label}: headline worst 1.20 V plateau gain ({wg.group(1)} dB at {fmt_point(*wg_pt)}) "
                          f"disagrees with its explicit 1.20 V table (min {min_g:.2f} dB)")

    # ---- inside the common component: needs the retained samples (checked by icmr_crosscheck) ----
    in_comp = {}
    if comp is not None:
        g2 = _margin_claim(re.search(r"^- Worst plateau gain inside the 1\.20 V component[^:]*: ([\d.]+) dB, limiting (\w+) "
                                     r"([+-][\d.]+) mV at " + _PT + r" / VCM ([\d.]+) V\.$", hl, re.M),
                           label, "Worst plateau gain inside the 1.20 V component")
        m2 = _margin_claim(re.search(r"^- Smallest device margin inside that component: (\w+) ([+-][\d.]+) mV "
                                     r"\(plateau gain ([\d.]+) dB\) at " + _PT + r" / VCM ([\d.]+) V\.$", hl, re.M),
                           label, "Smallest device margin inside that component")
        in_comp = {"gain": {"gain_db": float(g2.group(1)), "device": g2.group(2), "margin_mv": float(g2.group(3)),
                            "point": g2.groups()[3:6], "vcm_mv": _mv(g2.group(7))},
                   "margin": {"device": m2.group(1), "margin_mv": float(m2.group(2)), "gain_db": float(m2.group(3)),
                              "point": m2.groups()[3:6], "vcm_mv": _mv(m2.group(7))}}
        for k_, c_ in in_comp.items():
            if not comp["lo"] <= c_["vcm_mv"] <= comp["hi"]:
                raise ReportError(f"{label}: headline worst {k_} inside the 1.20 V component sits at VCM "
                                  f"{_v(c_['vcm_mv'])} V, outside the component")

    brackets = [u for p in points for i in per_point[p] for u in (i["lo_unc"], i["hi_unc"]) if u is not None]
    res = int(ax.group(2))
    if brackets and max(brackets) > res:
        raise ReportError(f"{label}: a transition bracket of {max(brackets)} mV exceeds the record's stated "
                          f"refinement (<= {res} mV)")
    n_end = sum(2 * len(per_point[p]) for p in points)
    ends = re.search(r"^- Interval endpoints \(of (\d+)\)", hl, re.M)
    if ends and int(ends.group(1)) != n_end:
        raise ReportError(f"{label}: record counts {ends.group(1)} interval endpoints; its per-point table has {n_end}")
    flagged = re.search(r"^- Passing samples whose saturation margin is negative but inside the 1 mV tolerance: (\d+)\.",
                        hl, re.M)
    cov = coverage_of(set(points))
    lim = common_limitations(text, prov, cov)
    lim += [
        "follower-biased bench (unity-gain-follower DC operating point): the measured range is not an "
        "arbitrary-output-voltage input range and not an offset-accuracy claim",
        "systematic only: perfectly matched schematic, mismatch not covered",
        "sampled coverage: each edge is the nearest verified passing sample; the true transition lies within the "
        f"stated bracket on the non-passing side (initial scan <= {ax.group(1)} mV, transitions refined to <= {res} mV)",
        "the bench-validity tolerances (1 mV saturation tolerance, 0.10 V output tolerance) are not ratified bounds"
        + (f"; {flagged.group(1)} passing samples have a negative saturation margin inside the 1 mV tolerance"
           if flagged else ""),
    ]
    if n_inv:
        lim.append(f"{n_inv} of {n_samples} samples are invalid (never passing, never bridged); see the record's "
                   "validity-failure section")
    return {"prov": prov, "coverage": cov, "limitations": lim, "points": points, "per_point": per_point,
            "strict": strict, "st120": st120, "t120": t120, "intersection": inter, "intersection_strict": inter_s,
            "component": comp, "in_component": in_comp, "verdict_120": verdict, "count_120": cnt,
            "min_margin_120": {"device": sm.group(1), "margin_mv": float(sm.group(2)), "gain_db": float(sm.group(3)),
                               "point": sm_pt},
            "min_gain_120": {"gain_db": float(wg.group(1)), "device": wg.group(2), "margin_mv": float(wg.group(3)),
                             "point": wg_pt},
            "samples": {"total": n_samples, "pass": n_pass, "fail": n_fail, "invalid": n_inv},
            "scan_step_mv": int(ax.group(1)), "refine_step_mv": res, "max_bracket_mv": max(brackets) if brackets else None,
            "n_endpoints": n_end,
            "tolerance_flagged": int(flagged.group(1)) if flagged else None}


def _icmr_runs(samples: list, max_gap_mv: int, key: str) -> list:
    """Contiguous passing runs of one point's samples (sorted by VCM), as the driver forms them."""
    out, i = [], 0
    while i < len(samples):
        if samples[i][key] != "pass":
            i += 1
            continue
        j = i
        while j + 1 < len(samples) and samples[j + 1][key] == "pass" and samples[j + 1]["vcm"] - samples[j]["vcm"] <= max_gap_mv:
            j += 1
        lo_nb = samples[i - 1] if i > 0 else None
        hi_nb = samples[j + 1] if j + 1 < len(samples) else None
        out.append({"lo": samples[i]["vcm"], "hi": samples[j]["vcm"], "n": j - i + 1,
                    "lo_unc": None if lo_nb is None else samples[i]["vcm"] - lo_nb["vcm"],
                    "hi_unc": None if hi_nb is None else hi_nb["vcm"] - samples[j]["vcm"],
                    "lo_nb": "scan edge" if lo_nb is None else lo_nb[key],
                    "hi_nb": "scan edge" if hi_nb is None else hi_nb[key]})
        i = j + 1
    return out


def icmr_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive the record's per-point intervals, 1.20 V statuses and margins from its
    retained per-sample evidence (corners/<rid>/samples.csv). Offline; no simulator."""
    import csv
    rel = f"sim/{ICMR_EXP}/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-point evidence is missing: {rel} (the record's intervals cannot "
                          "be cross-checked)")
    need = ("process", "temp_c", "vdd_v", "vcm_v", "status", "status_strict", "gain_db", "min_margin_mv",
            "limiting_device")
    by_pt: dict = {}
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns {list(need)}")
        seen = set()
        for r in rd:
            try:
                pt = (r["process"], str(int(float(r["temp_c"]))), f"{float(r['vdd_v']):.2f}")
                vcm = _mv(r["vcm_v"])
                s = {"vcm": vcm, "status": r["status"], "status_strict": r["status_strict"],
                     "gain_db": float(r["gain_db"]) if r["gain_db"] else None,
                     "margin_mv": float(r["min_margin_mv"]) if r["min_margin_mv"] else None,
                     "device": r["limiting_device"].upper(), "tol_flag": bool((r.get("tolerance_flags") or "").strip())}
            except (ValueError, KeyError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if (pt, vcm) in seen or s["status"] not in ("pass", "fail", "invalid"):
                raise ReportError(f"{label}: {rel}: duplicate sample or unknown status at {fmt_point(*pt)} / VCM {_v(vcm)} V")
            if s["status"] != "invalid" and (s["gain_db"] is None or s["margin_mv"] is None):
                raise ReportError(f"{label}: {rel}: valid sample without gain/margin at {fmt_point(*pt)} / VCM {_v(vcm)} V")
            seen.add((pt, vcm))
            by_pt.setdefault(pt, []).append(s)
    if set(by_pt) != set(ex["points"]):
        raise ReportError(f"{label}: {rel} covers {len(by_pt)} PVT points; the record's per-point table "
                          f"{len(ex['points'])} (or a different set)")
    total = sum(len(v) for v in by_pt.values())
    counts = {k: sum(1 for v in by_pt.values() for s in v if s["status"] == k) for k in ("pass", "fail", "invalid")}
    if {"total": total, **counts} != ex["samples"]:
        raise ReportError(f"{label}: {rel} holds {total} samples ({counts}); the record header states {ex['samples']}")
    n_flag = sum(1 for v in by_pt.values() for s in v if s["status"] == "pass" and s["tol_flag"])
    if ex["tolerance_flagged"] is not None and n_flag != ex["tolerance_flagged"]:
        raise ReportError(f"{label}: {rel} has {n_flag} passing samples inside the saturation tolerance band; "
                          f"the record states {ex['tolerance_flagged']}")
    for pt, ss in by_pt.items():
        ss.sort(key=lambda s: s["vcm"])
        got = _icmr_runs(ss, ex["scan_step_mv"], "status")
        if got != ex["per_point"][pt]:
            raise ReportError(f"{label}: per-point intervals at {fmt_point(*pt)} disagree with the retained evidence "
                              f"{rel} ({fmt_ivs(got)} with its brackets/neighbours, record {fmt_ivs(ex['per_point'][pt])})")
        got_s = [(i["lo"], i["hi"]) for i in _icmr_runs(ss, ex["scan_step_mv"], "status_strict")]
        if got_s != ex["strict"][pt]:
            raise ReportError(f"{label}: strict (0 mV) intervals at {fmt_point(*pt)} disagree with the retained evidence {rel}")
        s120 = next((s for s in ss if s["vcm"] == ICMR_TARGET_MV), None)
        t = ex["t120"][pt]
        if (s120 or {"status": "invalid"})["status"] != t["status"] or (
                "margin_mv" in t and (abs(s120["margin_mv"] - t["margin_mv"]) > 0.051
                                      or abs(s120["gain_db"] - t["gain_db"]) > 0.0051 or s120["device"] != t["device"])):
            raise ReportError(f"{label}: explicit 1.20 V sample at {fmt_point(*pt)} disagrees with the retained evidence {rel}")
    comp = ex["component"]
    if comp is not None:
        inside = [(pt, s) for pt, ss in by_pt.items() for s in ss
                  if comp["lo"] <= s["vcm"] <= comp["hi"] and s["status"] != "invalid"]
        for k_, field in (("margin", "margin_mv"), ("gain", "gain_db")):
            c_ = ex["in_component"][k_]
            lo_val = min(s[field] for _, s in inside)
            at = next((s for pt, s in inside if pt == c_["point"] and s["vcm"] == c_["vcm_mv"]), None)
            tol_ = 0.051 if field == "margin_mv" else 0.0051
            if (at is None or abs(c_[field] - lo_val) > tol_ or abs(at[field] - lo_val) > tol_
                    or at["device"] != c_["device"]):
                raise ReportError(f"{label}: headline worst {k_} inside the 1.20 V component disagrees with the "
                                  f"retained evidence {rel} (min {lo_val:+.2f})")
    return {"path": rel, "sha256": sha256_file(p), "samples": total, "points": len(by_pt)}


def _icmr_addendum(root: Path, rid: str) -> dict | None:
    rel = f"sim/{ICMR_EXP}/records/{rid}-addendum/ADDENDUM.md"
    p = root / rel
    return {"path": rel, "sha256": sha256_file(p)} if p.is_file() else None


_INFO = " (information only, not graded)"


def _edge(c: dict, side: str) -> str:
    unc = c[f"{side}_unc"]
    return (f"{_v(c[side])} V, bracket {'scan edge' if unc is None else f'{unc} mV'} to a "
            f"{c[f'{side}_nb']} sample")


def icmr_figures(ex: dict) -> list:
    """The ICMR row's measured figures, each labelled information only (the bound is proposed)."""
    F = []

    def add(label, value, corner=None):
        F.append({"label": label + _INFO, "value": value, "corner": corner})

    n = ex["coverage"]["points"]
    add(f"conservative common interval over all {n} PVT points (intersection; disjoint components listed "
        "separately, never bridged)", fmt_ivs(ex["intersection"]))
    comp = ex["component"]
    if comp is None:
        add("component of the common interval containing 1.20 V", "none: 1.20 V is outside every common component")
    else:
        add("component of the common interval containing 1.20 V", fmt_ivs([comp]))
        add("low edge, edge-binding corner", _edge(comp, "lo"), comp["lo_src"])
        add("high edge, edge-binding corner", _edge(comp, "hi"), comp["hi_src"])
    add("transition resolution (largest edge bracket over every per-point interval endpoint)",
        (f"{ex['max_bracket_mv']} mV over {ex['n_endpoints']} endpoints" if ex["max_bracket_mv"] is not None
         else f"no bracketed endpoint ({ex['n_endpoints']} endpoints)")
        + f" (scan <= {ex['scan_step_mv']} mV, refinement <= {ex['refine_step_mv']} mV)")
    c = ex["count_120"]
    add("explicit 1.20 V sample coverage", f"{c['pass']}/{n} pass, {c['fail']} fail, {c['invalid']} invalid/missing "
        f"(record verdict: {ex['verdict_120']})")
    m = ex["min_margin_120"]
    add("smallest saturation margin at 1.20 V", f"{m['device']} {m['margin_mv']:+.1f} mV (plateau gain "
        f"{m['gain_db']:.2f} dB)", fmt_point(*m["point"]))
    g = ex["min_gain_120"]
    add("lowest plateau gain at 1.20 V", f"{g['gain_db']:.2f} dB (limiting {g['device']} {g['margin_mv']:+.1f} mV)",
        fmt_point(*g["point"]))
    if comp is not None:
        m = ex["in_component"]["margin"]
        add("smallest saturation margin inside the 1.20 V component", f"{m['device']} {m['margin_mv']:+.1f} mV at "
            f"VCM {_v(m['vcm_mv'])} V (plateau gain {m['gain_db']:.2f} dB)", fmt_point(*m["point"]))
        g = ex["in_component"]["gain"]
        add("lowest plateau gain inside the 1.20 V component", f"{g['gain_db']:.2f} dB at VCM {_v(g['vcm_mv'])} V "
            f"(limiting {g['device']} {g['margin_mv']:+.1f} mV)", fmt_point(*g["point"]))
    add("common interval with a strict 0 mV saturation tolerance", fmt_ivs(ex["intersection_strict"]))
    dis = [p for p in ex["points"] if len(ex["per_point"][p]) > 1]
    add("PVT points with disjoint passing intervals", f"{len(dis)} of {n}" + (": " + "; ".join(
        f"{fmt_point(*p)} {fmt_ivs(ex['per_point'][p])}" for p in dis) if dis else ""))
    s = ex["samples"]
    add("(PVT point, VCM) samples", f"{s['total']} ({s['pass']} pass, {s['fail']} fail, {s['invalid']} invalid); "
        f"re-derived from the retained {ex['retained']['path']}")
    return F


def icmr_evidence(ex: dict, source: dict) -> dict:
    """Structured, source-pinned ICMR measurement attached to the (ungraded) proposed row."""
    def comp_d(c):
        return {"low_mv": c["lo"], "high_mv": c["hi"], "low_binding_point": c["lo_src"],
                "high_binding_point": c["hi_src"], "low_bracket_mv": c["lo_unc"], "high_bracket_mv": c["hi_unc"],
                "low_neighbour": c["lo_nb"], "high_neighbour": c["hi_nb"]}

    def pt_d(d):
        return {k: (fmt_point(*v) if k == "point" else v) for k, v in d.items()}

    prov_keys = ("dut_sha256", "pdk_open_pdks", "pdk_client_resolved", "ngspice_local", "ngspice_engine",
                 "klt_client", "backend", "fleet_runner_mismatch", "measurement_config")
    return {
        "graded": False,
        "common_interval_components": [comp_d(c) for c in ex["intersection"]],
        "component_containing_1v20": comp_d(ex["component"]) if ex["component"] else None,
        "common_interval_strict_mv": [[c["lo"], c["hi"]] for c in ex["intersection_strict"]],
        "transition_resolution": {"scan_step_mv": ex["scan_step_mv"], "refine_step_mv": ex["refine_step_mv"],
                                  "max_endpoint_bracket_mv": ex["max_bracket_mv"], "endpoints": ex["n_endpoints"]},
        "explicit_1v20": {"verdict_in_record": ex["verdict_120"], "points": ex["coverage"]["points"], **ex["count_120"]},
        "smallest_saturation_margin": {"at_1v20": pt_d(ex["min_margin_120"]),
                                       "inside_component": pt_d(ex["in_component"]["margin"]) if ex["in_component"] else None},
        "lowest_plateau_gain": {"at_1v20": pt_d(ex["min_gain_120"]),
                                "inside_component": pt_d(ex["in_component"]["gain"]) if ex["in_component"] else None},
        "per_point_intervals": [{"point": fmt_point(*p), "intervals_mv": [[i["lo"], i["hi"]] for i in ex["per_point"][p]],
                                 "status_1v20": ex["st120"][p]} for p in ex["points"]],
        "samples": ex["samples"],
        "tolerance_flagged_samples": ex["tolerance_flagged"],
        "provenance": {"record_id": source["record_id"], "record_sha256": source["sha256"],
                       **{k: source[k] for k in prov_keys}},
        "retained_evidence": ex["retained"],
        "addendum": ex["addendum"],
    }


# --------------------------------------------------------------------------
# supplementary passive-corner side studies (issue #121): measured, never graded
# --------------------------------------------------------------------------
#: Side studies the manifest may select under "supplementary" (never under
#: "experiments"): key -> experiment directory, record title and the spec rows
#: whose details cite the study. Their cells are never counted in a row's
#: points or verdict: the studies cover a few MOS/T/VDD points x RZ x CC, not
#: the 45-point grid.
SUPPLEMENTARY = {
    "gain-gbw-pm-passive-corners": {"experiment": "gain-gbw-pm", "title": STUDY_TITLES["gain-gbw-pm"][0],
                                    "rows": ("gain", "gbw", "pm")},
    "slew-swing-power-passive-corners": {"experiment": "slew-swing-power",
                                         "title": STUDY_TITLES["slew-swing-power"][0],
                                         "rows": ("power", "slew", "swing")},
    # issue #150: the same two studies over the FULL 5 MOS x 3 T x 3 VDD x 9 RZ/CC = 405-cell
    # envelope (`--passive-scope full`); same record format, selected under their own keys so the
    # limited three-point evidence stays visible beside them.
    "gain-gbw-pm-passive-corners-full": {"experiment": "gain-gbw-pm", "title": STUDY_TITLES["gain-gbw-pm"][0],
                                         "rows": ("gain", "gbw", "pm"), "scope": "full"},
    "slew-swing-power-passive-corners-full": {"experiment": "slew-swing-power",
                                              "title": STUDY_TITLES["slew-swing-power"][0],
                                              "rows": ("power", "slew", "swing"), "scope": "full"},
    # issue #173: the full 405-cell PSRR study (issue #155). BOUND-FREE: its figures are measured
    # information attached to the PSRR row and are never compared with a bound -- the PSRR row's
    # DR-6 bound is proposed, not ratified, and the adapter refuses to run if that ever changes
    # (it has no grader). Its figures are re-derived from the retained `<cell>.psrr.dat` curves.
    "psrr-passive-corners-full": {"experiment": "psrr", "title": STUDY_TITLES["psrr"][0],
                                  "rows": ("psrr",), "scope": "full", "bound_free": True},
}
PASSIVE_LEVELS = ("typical", "best", "worst")


def _disp_tol(s: str) -> float:
    """Half a unit in the last displayed digit of the number `s` (plus a hair)."""
    d = len(s.split(".")[1]) if "." in s else 0
    return 0.5 * 10 ** -d + 1e-9


def study_provenance(text: str, label: str) -> dict:
    """Provenance of a side-study record: its header plus its '## Conditions' section."""
    head = header_of(text) + section(text, "Conditions")
    prov = parse_provenance(head, label)
    if prov["pdk_open_pdks"] is None:
        m = re.search(r"\(open_pdks ([0-9a-f]{40})\)", head)
        prov["pdk_open_pdks"] = m.group(1) if m else None
    if prov["klt_client"] is None:
        m = re.search(r"; klt klt (\S+)", head)
        prov["klt_client"] = m.group(1) if m else None
    prov["fleet_runner_mismatch"] = prov["fleet_runner_mismatch"] or '"runner_compatibility": "mismatch"' in head
    return prov


def _study_points(text: str, label: str) -> list:
    """The MOS/T/VDD points the study states, in its order; each x 3 RZ x 3 CC cells."""
    m = re.search(r"^- \*\*Points\*\*: (.+?), each x 3 RZ levels x 3 CC levels = (\d+) cells", section(text, "Conditions"),
                  re.M)
    if not m:
        raise ReportError(f"{label}: no '- **Points**' line (MOS/T/VDD points x 3 RZ x 3 CC = N cells) in Conditions")
    pts = []
    for part in m.group(1).split(";"):
        pm = re.fullmatch(_PT, part.strip())
        if not pm:
            raise ReportError(f"{label}: malformed study point '{part.strip()}' in the Points line")
        if pm.groups() in pts:
            raise ReportError(f"{label}: duplicate study point {fmt_point(*pm.groups())} in the Points line")
        pts.append(pm.groups())
    n = len(pts) * len(PASSIVE_LEVELS) ** 2
    if int(m.group(2)) != n:
        raise ReportError(f"{label}: Points line states {m.group(2)} cells, but {len(pts)} points x 3 RZ x 3 CC = {n}")
    return pts


def _study_cells(body: str, label: str, where: str, ncols: int) -> dict:
    """One RZ x CC table: {(rz, cc): cells}; every combination exactly once."""
    lines = [ln for ln in body.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no RZ x CC table under '### {where}'")
    out = {}
    for ln in lines[2:]:
        c = split_cells(ln)
        if len(c) != ncols or c[0] not in PASSIVE_LEVELS or c[1] not in PASSIVE_LEVELS:
            raise ReportError(f"{label}: malformed RZ x CC row under '### {where}': {ln.strip()}")
        if (c[0], c[1]) in out:
            raise ReportError(f"{label}: duplicate cell RZ {c[0]}, CC {c[1]} under '### {where}'")
        out[(c[0], c[1])] = c
    missing = [(r, c) for r in PASSIVE_LEVELS for c in PASSIVE_LEVELS if (r, c) not in out]
    if missing:
        raise ReportError(f"{label}: '### {where}' is missing {len(missing)} of 9 RZ x CC cells "
                          f"(first: RZ {missing[0][0]}, CC {missing[0][1]})")
    return out


def _study_value(cell: str, label: str, where: str) -> tuple:
    if not re.fullmatch(r"\d+(?:\.\d+)?", cell):
        raise ReportError(f"{label}: malformed value '{cell}' under '### {where}'")
    return float(cell), cell


def _study_figure(label: str, fig: dict, vals: dict, flags: dict, claim: dict) -> dict:
    """Re-derive one figure over every cell and compare with the record's summary line.

    `vals` {(pt, rz, cc): (value, display)}, `flags` {(pt, rz, cc): PASS|FAIL} (the record's
    per-cell column), `claim` the summary line's verdict, counts and worst cell. A per-cell
    flag must agree with its value against the bound unless the value is within display
    precision of the bound (a 60.00 deg cell can be a FAIL of 59.996 deg).
    """
    ge = fig["sense"] == ">="
    for k, (v, s) in vals.items():
        want = "PASS" if (v >= fig["bound_value"] if ge else v <= fig["bound_value"]) else "FAIL"
        if flags[k] not in ("PASS", "FAIL") or (flags[k] != want and abs(v - fig["bound_value"]) > _disp_tol(s)):
            raise ReportError(f"{label}: {fig['label']} cell {fmt_point(*k[0])}, RZ {k[1]}, CC {k[2]} reads {s} "
                              f"{fig['unit']} but is marked '{flags[k]}' against {fig['bound_text']}")
    n_pass = sum(1 for f in flags.values() if f == "PASS")
    total = len(vals)
    worst_v = (min if ge else max)(v for v, _ in vals.values())
    at = claim["worst_at"]
    got = {"verdict": "PASS" if n_pass == total else "FAIL", "pass": n_pass, "total": total, "fail": total - n_pass}
    if ({k: claim[k] for k in got} != got or abs(claim["worst"] - worst_v) > _disp_tol(claim["worst_s"])
            or at not in vals or vals[at][0] != worst_v):
        raise ReportError(
            f"{label}: {fig['label']} summary ({claim['verdict']}, {claim['pass']}/{claim['total']} pass, "
            f"{claim['fail']} fail, worst {claim['worst_s']} {fig['unit']} at {fmt_point(*at[0])}, RZ {at[1]}, CC {at[2]}) "
            f"disagrees with its own tables ({got['verdict']}, {n_pass}/{total} pass, worst {worst_v:g} {fig['unit']})")
    if claim.get("best_at") is not None:
        best_v = (max if ge else min)(v for v, _ in vals.values())
        b = claim["best_at"]
        if abs(claim["best"] - best_v) > _disp_tol(claim["best_s"]) or b not in vals or vals[b][0] != best_v:
            raise ReportError(f"{label}: {fig['label']} best cell ({claim['best_s']} {fig['unit']}) disagrees with its "
                              f"own tables (best {best_v:g} {fig['unit']})")
    order = list(vals)
    lo_k = min(order, key=lambda k: vals[k][0])
    hi_k = max(order, key=lambda k: vals[k][0])
    w_k = at
    return {"label": fig["label"], "bound_text": fig["bound_text"], "bound_value": fig["bound_value"],
            "sense": fig["sense"], "unit": fig["unit"], "verdict_in_record": got["verdict"],
            "pass": n_pass, "total": total, "fail": total - n_pass,
            "range": [vals[lo_k][1], vals[hi_k][1]],
            "worst": {"value": vals[w_k][1], "point": fmt_point(*w_k[0]), "rz": w_k[1], "cc": w_k[2]},
            "failing_cells": [{"point": fmt_point(*k[0]), "rz": k[1], "cc": k[2], "value": vals[k][1]}
                              for k in order if flags[k] == "FAIL"]}


def _claim(m, groups: dict) -> dict:
    """Summary-line regex groups -> claim dict (see _study_figure)."""
    g = m.group
    d = {"verdict": g(groups["verdict"]), "pass": int(g(groups["pass"])), "total": int(g(groups["total"])),
         "fail": int(g(groups["fail"])), "worst": float(g(groups["worst"])), "worst_s": g(groups["worst"]),
         "worst_at": ((g(groups["wp"]), g(groups["wp"] + 1), g(groups["wp"] + 2)), g(groups["wp"] + 3),
                      g(groups["wp"] + 4))}
    if "best" in groups and g(groups["best"]):
        d.update(best=float(g(groups["best"])), best_s=g(groups["best"]),
                 best_at=((g(groups["bp"]), g(groups["bp"] + 1), g(groups["bp"] + 2)), g(groups["bp"] + 3),
                          g(groups["bp"] + 4)))
    return d


def _study_sections(text: str, label: str) -> list:
    sec = section(text, "Results")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    if len(parts) < 3:
        raise ReportError(f"{label}: no '### ' tables under '## Results'")
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


_GS_FIGS = {  # gain study: key -> (summary-line name, unit, value column, flag column)
    "pm": ("Phase margin", "deg", 2, 7),
    "gbw": ("GBW", "MHz", 4, 8),
}


def extract_gain_passive_study(text: str, label: str) -> dict:
    """The gain/GBW/PM RZ x CC passive-corner side study (issue #70): PM and GBW per cell,
    re-derived and compared with the record's verdict header. DC gain is not measured."""
    prov = study_provenance(text, label)
    pts = _study_points(text, label)
    head = header_of(text)
    figs, claims = {}, {}
    for key, (name, unit, _, _) in _GS_FIGS.items():
        rx = (r"^  - " + re.escape(name) + r" (>= ([\d.]+) " + unit + r"): \*\*(PASS|FAIL)\*\* -- passes at (\d+)/(\d+) "
              r"study cells \(fails at (\d+)/\d+\); worst ([\d.]+) " + unit + r" at " + _PT + r", RZ (\w+), CC (\w+)"
              r"(?:; best ([\d.]+) " + unit + r" at " + _PT + r", RZ (\w+), CC (\w+))?\.$")
        m = re.search(rx, head, re.M)
        if not m:
            raise ReportError(f"{label}: no '{name}' verdict line in the record header")
        figs[key] = {"label": name, "bound_text": m.group(1), "bound_value": float(m.group(2)), "sense": ">=",
                     "unit": unit}
        claims[key] = _claim(m, {"verdict": 3, "pass": 4, "total": 5, "fail": 6, "worst": 7, "wp": 8,
                                 "best": 13, "bp": 14})
    vals = {k: {} for k in _GS_FIGS}
    flags = {k: {} for k in _GS_FIGS}
    seen = []
    for heading, body in _study_sections(text, label):
        pm = re.fullmatch(_PT, heading)
        if not pm or pm.groups() not in pts:
            raise ReportError(f"{label}: unexpected results table '### {heading}' (not a point of the Points line)")
        if pm.groups() in seen:
            raise ReportError(f"{label}: duplicate results table '### {heading}'")
        seen.append(pm.groups())
        for (rz, cc), c in _study_cells(body, label, heading, 9).items():
            for key, (_, _, vi, fi) in _GS_FIGS.items():
                vals[key][(pm.groups(), rz, cc)] = _study_value(c[vi], label, heading)
                flags[key][(pm.groups(), rz, cc)] = c[fi]
    missing = [p for p in pts if p not in seen]
    if missing:
        raise ReportError(f"{label}: no results table for study point {fmt_point(*missing[0])}")
    figures = {k: _study_figure(label, figs[k], _cell_order(vals[k], pts), flags[k], claims[k]) for k in _GS_FIGS}
    return {"prov": prov, "points": pts, "figures": figures,
            "not_measured": {"gain": "the study measures PM and GBW only (no DC-gain column): no gain observation"}}


_SS_FIGS = {"Quiescent power": "power", "Output swing": "swing"}  # "Slew rate (...)" -> slew


def _ssp_key(name: str) -> str | None:
    return "slew" if name.startswith("Slew rate") else _SS_FIGS.get(name)


def extract_ssp_passive_study(text: str, label: str) -> dict:
    """The slew/swing/power RZ x CC passive-corner side study (issue #97): each measured figure
    per cell, re-derived and compared with the record's verdict header."""
    prov = study_provenance(text, label)
    pts = _study_points(text, label)
    rx = re.compile(r"^  - \*\*(Quiescent power|Slew rate[^:]*|Output swing): (PASS|FAIL)\*\* vs ratified (<=|>=) ([\d.]+) "
                    r"(uW|V/us|Vpp) -- passes at (\d+)/(\d+) cells \(fails at (\d+)\); worst ([\d.]+) \5 at " + _PT +
                    r", RZ (\w+), CC (\w+)\.$", re.M)
    figs, claims = {}, {}
    for m in rx.finditer(header_of(text)):
        key = _ssp_key(m.group(1))
        if key in figs:
            raise ReportError(f"{label}: duplicate '{m.group(1)}' verdict line in the record header")
        figs[key] = {"label": m.group(1), "bound_text": f"{m.group(3)} {m.group(4)} {m.group(5)}",
                     "bound_value": float(m.group(4)), "sense": m.group(3), "unit": m.group(5)}
        claims[key] = _claim(m, {"verdict": 2, "pass": 6, "total": 7, "fail": 8, "worst": 9, "wp": 10})
    if not figs:
        raise ReportError(f"{label}: no measured slew / swing / power verdict line in the record header")
    hrx = re.compile(r"(Quiescent power|Slew rate.*?|Output swing) -- " + _PT + r" \(bound (<=|>=) ([\d.]+) (uW|V/us|Vpp)\)")
    vals = {k: {} for k in figs}
    flags = {k: {} for k in figs}
    seen = set()
    for heading, body in _study_sections(text, label):
        hm = hrx.fullmatch(heading)
        key = _ssp_key(hm.group(1)) if hm else None
        if key is None or key not in figs:
            raise ReportError(f"{label}: unexpected results table '### {heading}' (no matching verdict line)")
        pt = hm.groups()[1:4]
        if pt not in pts:
            raise ReportError(f"{label}: results table '### {heading}' is at a point not in the Points line")
        if (key, pt) in seen:
            raise ReportError(f"{label}: duplicate results table '### {heading}'")
        seen.add((key, pt))
        if (hm.group(5), float(hm.group(6)), hm.group(7)) != (figs[key]["sense"], figs[key]["bound_value"],
                                                               figs[key]["unit"]):
            raise ReportError(f"{label}: '### {heading}' states a bound other than its verdict line's "
                              f"{figs[key]['bound_text']}")
        for (rz, cc), c in _study_cells(body, label, heading, 5).items():
            vals[key][(pt, rz, cc)] = _study_value(c[2], label, heading)
            flags[key][(pt, rz, cc)] = c[4]
    for key in figs:
        missing = [p for p in pts if (key, p) not in seen]
        if missing:
            raise ReportError(f"{label}: no {figs[key]['label']} results table for study point {fmt_point(*missing[0])}")
    figures = {k: _study_figure(label, figs[k], _cell_order(vals[k], pts), flags[k], claims[k]) for k in figs}
    return {"prov": prov, "points": pts, "figures": figures,
            "not_measured": {k: "the study did not measure this figure" for k in ("power", "slew", "swing")
                             if k not in figs}}


def _cell_order(vals: dict, pts: list) -> dict:
    """Cells in study order: point (Points line), then RZ, then CC (typical, best, worst)."""
    return {(p, r, c): vals[(p, r, c)] for p in pts for r in PASSIVE_LEVELS for c in PASSIVE_LEVELS}


# --------------------------------------------------------------------------
# PSRR full passive-corner study (issue #173): bound-free, re-derived from retained curves
# --------------------------------------------------------------------------
#: (figure label, rail, `.psrr.dat` column, frequency, cell-table column, rail label, frequency label)
PSRR_STUDY_FIGS = (("PSRR+ 1 kHz", "vdd", 4, 1e3, 1, "PSRR+", "1 kHz"),
                   ("PSRR+ 10 kHz", "vdd", 4, 1e4, 2, "PSRR+", "10 kHz"),
                   ("PSRR- 1 kHz", "vss", 5, 1e3, 3, "PSRR-", "1 kHz"),
                   ("PSRR- 10 kHz", "vss", 5, 1e4, 4, "PSRR-", "10 kHz"))
#: the record's per-cell table carries the 10 kHz delta vs typical passives (columns 5 / 6)
PSRR_STUDY_DELTA_COL = {"vdd": 5, "vss": 6}
PSRR_STUDY_EXCITATIONS = ("dm", "vdd", "vss")
#: Two cells whose retained values differ by less than this are tied at the retained precision
#: (`np.savetxt` "%.8e": ~1e-6 dB at 100 dB). A claimed binding cell within it of the derived
#: extreme is accepted; anything further is a contradictory extremum.
PSRR_TIE_DB = 1e-5
#: retained-curve values vs the record's rounded display: half a display unit plus this slack
PSRR_DAT_SLACK_DB = 2e-6
#: the study's own statements of the limitations the report must carry (matched devices,
#: bias-generator exclusion, no passive mismatch / layout parasitics, client-vs-fleet provenance)
PSRR_STUDY_LIMITS = (
    ("Matched devices: systematic figures, not mismatch-limited",
     "matched devices: systematic figures of a perfectly matched schematic, not mismatch-limited "
     "(mismatch-inclusive PSRR is not covered)"),
    ("**bias-generator supply sensitivity is excluded**",
     "the ideal 10 uA bias source carries no small-signal current, so bias-generator supply sensitivity is "
     "excluded"),
    ("no RZ/CC mismatch, no layout parasitics",
     "passive corners are the PDK's global `res_*` / `mimcap_*` sections (RZ and CC varied independently): no RZ/CC "
     "mismatch and no layout parasitics"),
)
_PSRR_CELL = re.compile(r"(\w+) / (-?\d+) C / ([\d.]+) V / RZ (\w+) / CC (\w+)")
_PSRR_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_PSRR_DELTA = re.compile(r"[+-]\d+\.\d+")


def _psrr_cell(s: str, label: str, where: str) -> tuple:
    m = _PSRR_CELL.fullmatch(s.strip())
    if not m or m.group(1) not in CORNER_ORDER or m.group(4) not in PASSIVE_LEVELS or m.group(5) not in PASSIVE_LEVELS:
        raise ReportError(f"{label}: malformed cell identity '{s.strip()}' under '{where}'")
    return (m.group(1), m.group(2), m.group(3)), m.group(4), m.group(5)


def _psrr_cell_text(k: tuple) -> str:
    return f"{fmt_point(*k[0])} / RZ {k[1]} / CC {k[2]}"


def _psrr_cell_dict(k: tuple, value: str) -> dict:
    return {"value": value, "point": fmt_point(*k[0]), "rz": k[1], "cc": k[2]}


def _psrr_num(s: str, label: str, where: str, rx=_PSRR_NUM) -> float:
    """A displayed number: finite, plain (no '>=' lower bound, no nan/inf, no 'n/a')."""
    s = s.strip()
    if not rx.fullmatch(s):
        raise ReportError(f"{label}: malformed or nonfinite value '{s}' under '{where}'")
    return float(s)


def _psrr_s(s: str, label: str, where: str, rx=_PSRR_NUM) -> str:
    """`s` itself, once checked to be a finite displayed number."""
    _psrr_num(s, label, where, rx)
    return s.strip()


def _psrr_stem(k: tuple) -> str:
    (mos, t, v), rz, cc = k
    return f"{mos}__r-{rz}__c-{cc}_{float(t):g}c_{float(v):.2f}v"


def _psrr_table(body: str, label: str, where: str, ncols: int) -> list:
    """Data rows of the first Markdown table in `body` (stops at a '### ' subsection)."""
    body = re.split(r"^### ", body, maxsplit=1, flags=re.M)[0]
    lines = [ln for ln in body.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no table under '{where}'")
    rows = []
    for ln in lines[2:]:
        c = split_cells(ln)
        if len(c) != ncols:
            raise ReportError(f"{label}: malformed row under '{where}': {ln.strip()}")
        rows.append(c)
    return rows


def _psrr_population(text: str, label: str) -> tuple:
    """(MOS/T/VDD points, fingerprint) from the record's retained, hash-verified fingerprint inputs."""
    fp = parse_fingerprint(text, label)
    study = (fp or {}).get("inputs", {}).get("passive_study")
    if not study:
        raise ReportError(f"{label}: retains no fingerprinted 'passive_study' inputs (cannot establish the study "
                          "population or check its measurement configuration)")
    if study.get("scope") != "full" or list(study.get("levels", [])) != list(PASSIVE_LEVELS):
        raise ReportError(f"{label}: retained passive_study is scope {study.get('scope')!r} with levels "
                          f"{study.get('levels')}, not the full {list(PASSIVE_LEVELS)} study")
    pts = []
    for p in study.get("points", []):
        try:
            pt = (str(p[0]), f"{float(p[1]):g}", f"{float(p[2]):.2f}")
        except (TypeError, ValueError, IndexError):
            raise ReportError(f"{label}: malformed retained passive_study point {p!r}")
        if pt in pts:
            raise ReportError(f"{label}: duplicate retained passive_study point {fmt_point(*pt)}")
        pts.append(pt)
    n = len(pts) * len(PASSIVE_LEVELS) ** 2
    if study.get("cells") != n:
        raise ReportError(f"{label}: retained passive_study states {study.get('cells')} cells, but {len(pts)} points "
                          f"x 3 RZ x 3 CC = {n}")
    return pts, fp


def _psrr_interp(freq: list, ydb: list, f: float, label: str, where: str) -> float:
    """Linear interpolation in (log10 f, dB), the PSRR runner's spot-frequency rule."""
    if f < freq[0] * (1 - 1e-9) or f > freq[-1] * (1 + 1e-9):
        raise ReportError(f"{label}: {where} does not span {f:g} Hz")
    lx, xs = math.log10(f), [math.log10(x) for x in freq]
    i = bisect.bisect_left(xs, lx)
    if i < len(xs) and abs(xs[i] - lx) < 1e-12:
        return ydb[i]
    i = max(1, min(i, len(xs) - 1))
    return ydb[i - 1] + (ydb[i] - ydb[i - 1]) * (lx - xs[i - 1]) / (xs[i] - xs[i - 1])


def _psrr_read_dat(path: Path, label: str, rel: str) -> tuple:
    """(frequency axis, rows) of one retained `<cell>.psrr.dat`; finite, strictly increasing axis."""
    if not path.is_file():
        raise ReportError(f"{label}: retained per-cell curve is missing: {rel}")
    rows = []
    for ln in path.read_text().splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        try:
            r = [float(x) for x in ln.split()]
        except ValueError:
            raise ReportError(f"{label}: {rel}: malformed line '{ln.strip()}'")
        if len(r) != 6:
            raise ReportError(f"{label}: {rel}: expected 6 columns (freq, Ad, Avdd, Avss, PSRR+, PSRR-), got {len(r)}")
        if not all(math.isfinite(x) for x in r):
            raise ReportError(f"{label}: {rel}: nonfinite value at {r[0]:g} Hz")
        rows.append(r)
    freq = [r[0] for r in rows]
    if len(freq) < 2 or freq[0] <= 0 or any(b <= a for a, b in zip(freq, freq[1:])):
        raise ReportError(f"{label}: {rel}: frequency axis is not positive and strictly increasing")
    return freq, rows


def _psrr_partitions(root: Path, rid: str, want: list, label: str) -> dict:
    """Fleet partition membership: per excitation, the union is exactly the study population."""
    rel = f"sim/psrr/corners/{rid}/partitions.json"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained partition membership is missing: {rel}")
    try:
        d = json.loads(p.read_text())
    except ValueError as e:
        raise ReportError(f"{label}: {rel} is not valid JSON: {e}") from e
    stems = {_psrr_stem(k) for k in want}
    cover = {}
    for mode in PSRR_STUDY_EXCITATIONS:
        parts = (d.get("plan") or {}).get(mode)
        if not parts:
            raise ReportError(f"{label}: {rel} has no '{mode}' partition plan")
        cells = [c for part in parts for c in part.get("cells", [])]
        dup = sorted({c for c in cells if cells.count(c) > 1})
        extra, missing = sorted(set(cells) - stems), sorted(stems - set(cells))
        if dup or extra or missing:
            raise ReportError(f"{label}: {rel}: '{mode}' partitions are not exactly the {len(stems)}-cell population "
                              f"({len(dup)} overlapping, {len(extra)} unexpected, {len(missing)} missing"
                              + (f"; first: {(dup or extra or missing)[0]}" if dup or extra or missing else "") + ")")
        claim = (d.get("coverage") or {}).get(mode)
        if claim != {"cells": len(stems), "overlap": 0, "missing": 0}:
            raise ReportError(f"{label}: {rel}: '{mode}' coverage claim {claim} disagrees with its plan")
        cover[mode] = len(cells)
    return {"path": rel, "sha256": sha256_file(p), "excitations": cover}


def _psrr_check_claim(label: str, what: str, claim_s: str, derived: float, slack: float = PSRR_DAT_SLACK_DB) -> None:
    if abs(float(claim_s) - derived) > _disp_tol(claim_s) + slack:
        raise ReportError(f"{label}: {what} states {claim_s} dB but the retained curves give {derived:.4f} dB")


def _psrr_extreme(label: str, what: str, vals: dict, pick, claim_k: tuple, claim_s: str, order: list,
                  slack: float = PSRR_DAT_SLACK_DB) -> tuple:
    """Check a claimed extreme (value + cell) against the derived one; returns (cell, value).

    The derived extreme is the first cell in study order with the extreme value; a claimed cell is
    accepted when its own retained value is within PSRR_TIE_DB of it (a tie at retained precision)."""
    best_k = pick(order, key=lambda k: vals[k])
    if claim_k not in vals or abs(vals[claim_k] - vals[best_k]) > PSRR_TIE_DB:
        raise ReportError(f"{label}: {what} names {(_psrr_cell_text(claim_k) if claim_k in vals else claim_k)} but "
                          f"the retained curves put it at {_psrr_cell_text(best_k)} ({vals[best_k]:.4f} dB): "
                          "contradictory extremum")
    _psrr_check_claim(label, what, claim_s, vals[best_k], slack)
    return claim_k, vals[best_k]


def extract_psrr_passive_study(text: str, label: str, root: Path, rid: str, primary: dict | None = None) -> dict:
    """The full PSRR RZ x CC passive-corner study (issue #155), bound-free (issue #173).

    Population: the record's retained, hash-verified fingerprint inputs (`passive_study`: the
    full 5 x 3 x 3 grid x 3 RZ x 3 CC = 405 cells); the record's per-cell table must hold exactly
    those cells (missing, duplicate and unexpected cells, malformed or nonfinite values are
    errors). Figures: PSRR+ / PSRR- at 1 kHz and 10 kHz are re-derived at full precision from the
    retained `<cell>.psrr.dat` curves (whose aggregate sha256 is reported), checked against the
    record's rounded per-cell table and its binding-cell / delta tables, and summarised as worst
    and best cell, worst over the typical-passive cells, the passive shift of the worst case and
    the delta versus typical passives at matching PVT. No bound is read and no verdict is formed.
    `primary` (the selected 45-point PSRR record, if any) must agree with the typical-passive
    worst values.
    """
    prov = study_provenance(text, label)
    head = header_of(text)
    if not re.search(r"^- \*\*Status\*\*: measured, \*\*no pass or fail verdict\*\*", head, re.M):
        raise ReportError(f"{label}: the record header does not state 'measured, **no pass or fail verdict**' "
                          "(the bound-free adapter reads no verdict and must not attach one)")
    sm = re.search(r"^- \*\*Scope\*\*: `(\w+)` -- .*?= (\d+) cells per figure", head, re.M)
    if not sm or sm.group(1) != "full":
        raise ReportError(f"{label}: no full-scope '- **Scope**: `full` -- ... = N cells per figure' header line")
    pts, _ = _psrr_population(text, label)
    study_scope(pts, "full")  # the retained population must be the full passive-by-PVT cross product
    want = [(p, r, c) for p in pts for r in PASSIVE_LEVELS for c in PASSIVE_LEVELS]
    if int(sm.group(2)) != len(want):
        raise ReportError(f"{label}: Scope line states {sm.group(2)} cells per figure, the retained population "
                          f"{len(want)}")
    order = sorted(want, key=lambda k: (corner_sort_key(k[0][0]), float(k[0][1]), float(k[0][2]),
                                        PASSIVE_LEVELS.index(k[1]), PASSIVE_LEVELS.index(k[2])))
    # ---- the record's per-cell table: exactly the population ----
    where = f"## All {len(want)} cells"
    body = section(text, f"All {len(want)} cells")
    if not body:
        raise ReportError(f"{label}: no '{where}' table")
    table = {}
    for c in _psrr_table(body, label, where, 11):
        k = _psrr_cell(c[0], label, where)
        if k not in want:
            raise ReportError(f"{label}: unexpected cell {_psrr_cell_text(k)} under '{where}' (not in the retained "
                              "study population)")
        if k in table:
            raise ReportError(f"{label}: duplicate cell {_psrr_cell_text(k)} under '{where}'")
        typ = k[1] == "typical" and k[2] == "typical"
        for i in (1, 2, 3, 4, 7, 8, 9):
            _psrr_num(c[i], label, f"{where}, {_psrr_cell_text(k)}")
        for i in PSRR_STUDY_DELTA_COL.values():
            if typ:
                if c[i] != "typ.":
                    raise ReportError(f"{label}: typical-passive cell {_psrr_cell_text(k)} carries delta '{c[i]}' "
                                      f"under '{where}' (expected 'typ.')")
            else:
                _psrr_num(c[i], label, f"{where}, {_psrr_cell_text(k)}", _PSRR_DELTA)
        table[k] = c
    missing = [k for k in order if k not in table]
    if missing:
        raise ReportError(f"{label}: '{where}' is missing {len(missing)} of {len(want)} cells "
                          f"(first: {_psrr_cell_text(missing[0])})")
    # ---- retained curves: full-precision values, cross-checked against the rounded table ----
    cdir_rel = f"sim/psrr/corners/{rid}"
    vals = {fig[0]: {} for fig in PSRR_STUDY_FIGS}
    digests, axis = [], None
    for k in order:
        name = f"{_psrr_stem(k)}.psrr.dat"
        path = root / cdir_rel / name
        freq, rows = _psrr_read_dat(path, label, f"{cdir_rel}/{name}")
        if axis is None:
            axis = freq
        elif len(freq) != len(axis) or any(abs(a / b - 1) > 1e-6 for a, b in zip(freq, axis)):
            raise ReportError(f"{label}: {cdir_rel}/{name} is on another frequency axis than the other cells")
        digests.append(f"{sha256_file(path)}  {name}\n")
        for flabel, _, col, f, tcol, _, _ in PSRR_STUDY_FIGS:
            v = _psrr_interp(freq, [r[col] for r in rows], f, label, f"{cdir_rel}/{name}")
            vals[flabel][k] = v
            _psrr_check_claim(label, f"'{where}' {flabel} of {_psrr_cell_text(k)}", table[k][tcol], v)
    deltas = {}
    for flabel, rail, *_ in PSRR_STUDY_FIGS:
        deltas[flabel] = {k: vals[flabel][k] - vals[flabel][(k[0], "typical", "typical")]
                          for k in order if (k[1], k[2]) != ("typical", "typical")}
    for flabel, rail, _, f, *_ in PSRR_STUDY_FIGS:
        if f == 1e4:
            for k, d in deltas[flabel].items():
                _psrr_check_claim(label, f"'{where}' {flabel} delta vs typical of {_psrr_cell_text(k)}",
                                  table[k][PSRR_STUDY_DELTA_COL[rail]], d)
    partitions = _psrr_partitions(root, rid, want, label)
    # ---- the record's rounded claims: binding cells and deltas ----
    bwhere = "## Binding cells at 1 kHz and 10 kHz"
    bind = {}
    for c in _psrr_table(section(text, "Binding cells at 1 kHz and 10 kHz"), label, bwhere, 7):
        if c[0] not in vals or c[0] in bind:
            raise ReportError(f"{label}: {'duplicate' if c[0] in bind else 'unexpected'} figure '{c[0]}' under "
                              f"'{bwhere}'")
        bind[c[0]] = c
    dwhere = "## Difference vs typical passives at matching PVT"
    dclaim = {}
    for c in _psrr_table(section(text, "Difference vs typical passives at matching PVT"), label, dwhere, 6):
        if c[0] not in vals or c[0] in dclaim:
            raise ReportError(f"{label}: {'duplicate' if c[0] in dclaim else 'unexpected'} figure '{c[0]}' under "
                              f"'{dwhere}'")
        dclaim[c[0]] = c
    typ_cells = [k for k in order if (k[1], k[2]) == ("typical", "typical")]
    nontyp = [k for k in order if (k[1], k[2]) != ("typical", "typical")]
    figures = {}
    for flabel, rail, _, f, _, rlabel, flab in PSRR_STUDY_FIGS:
        if flabel not in bind or flabel not in dclaim:
            raise ReportError(f"{label}: no '{flabel}' row under '{bwhere if flabel not in bind else dwhere}'")
        v, b, dc = vals[flabel], bind[flabel], dclaim[flabel]
        ws = b[1].replace("*", "")
        _psrr_num(ws, label, bwhere)
        wk, wv = _psrr_extreme(label, f"'{bwhere}' {flabel} worst", v, min, _psrr_cell(b[2], label, bwhere), ws, order)
        bk, bv = _psrr_extreme(label, f"'{bwhere}' {flabel} best", v, max, _psrr_cell(b[4], label, bwhere),
                               _psrr_s(b[3], label, bwhere), order)
        tv = min(v[k] for k in typ_cells)
        _psrr_check_claim(label, f"'{bwhere}' {flabel} worst over the typical-passive cells",
                          _psrr_s(b[5], label, bwhere), tv)
        _psrr_check_claim(label, f"'{bwhere}' {flabel} passive shift of the worst case",
                          _psrr_s(b[6], label, bwhere, _PSRR_DELTA), wv - tv)
        d = deltas[flabel]
        lo_k, lo = _psrr_extreme(label, f"'{dwhere}' {flabel} most negative delta", d, min,
                                 _psrr_cell(dc[2], label, dwhere), _psrr_s(dc[1], label, dwhere, _PSRR_DELTA),
                                 nontyp)
        hi_k, hi = _psrr_extreme(label, f"'{dwhere}' {flabel} most positive delta", d, max,
                                 _psrr_cell(dc[4], label, dwhere), _psrr_s(dc[3], label, dwhere, _PSRR_DELTA),
                                 nontyp)
        mx = max(abs(x) for x in d.values())
        _psrr_check_claim(label, f"'{dwhere}' {flabel} max abs delta", _psrr_s(dc[5], label, dwhere), mx)
        fig = {"label": flabel, "rail": rlabel, "frequency": flab, "unit": "dB", "cells": len(want),
               "worst": _psrr_cell_dict(wk, f"{wv:.2f}"), "best": _psrr_cell_dict(bk, f"{bv:.2f}"),
               "range": [f"{wv:.2f}", f"{bv:.2f}"],
               "typical_passive_worst": {"value": f"{tv:.2f}", "cells": len(typ_cells)},
               "passive_shift_of_worst": f"{wv - tv:+.2f}",
               "delta_vs_typical": {"cells": len(nontyp), "most_negative": _psrr_cell_dict(lo_k, f"{lo:+.3f}"),
                                    "most_positive": _psrr_cell_dict(hi_k, f"{hi:+.3f}"), "max_abs": f"{mx:.3f}"}}
        if primary is not None:
            pw = next((t for t in primary["figures"][rlabel] if t["figure"] == flab), None)
            if pw is None or abs(float(pw["worst_db"]) - tv) > _disp_tol(pw["worst_db"]) + PSRR_DAT_SLACK_DB:
                raise ReportError(f"{label}: {flabel} worst over the typical-passive cells ({tv:.4f} dB) disagrees with "
                                  f"the selected PSRR grid record ({pw['worst_db'] if pw else 'no such figure'} dB)")
            fig["typical_passive_worst"]["primary_record_worst"] = pw["worst_db"]
        figures[flabel] = fig
    # ---- limitations the study states (retained, never invented) ----
    lim = []
    for needle, text_ in PSRR_STUDY_LIMITS:
        if needle not in text:
            raise ReportError(f"{label}: the record does not state its limitation '{needle}'")
        lim.append(text_)
    fm = re.search(r"^- \*\*Fleet provenance limitations\*\*: runner klt (.+?) vs client `klt ([^`]+)`; (.+)$", text, re.M)
    if not fm:
        raise ReportError(f"{label}: no '- **Fleet provenance limitations**' line (client-versus-fleet provenance)")
    runners = [r.strip() for r in fm.group(1).split(",")]
    prov["fleet_runner_mismatch"] = prov["fleet_runner_mismatch"] or any(r != fm.group(2) for r in runners)
    lim.append(f"client-versus-fleet provenance: fleet runner klt {', '.join(runners)} vs client klt {fm.group(2)}; the "
               "fleet runner does not report its PDK revision and klt's provenance.pdk is resolved on the submitting "
               "client (the fleet library is tied to the pinned revision only indirectly, via the record's cross-checks)")
    lim.append("bound-free: the PSRR bound is proposed by DR-6, not ratified, so these figures carry no PASS/FAIL "
               "verdict and are not compared with the proposed bound")
    retained = {"path": cdir_rel, "psrr_dat_files": len(digests),
                "sha256": hashlib.sha256("".join(digests).encode()).hexdigest(),
                "sha256_method": "sha256 of the sha256sum-format listing (hex digest, two spaces, file name; one "
                                 "line per cell, in study order) of every per-cell .psrr.dat curve",
                "partitions": partitions}
    return {"prov": prov, "points": pts, "figures": figures, "not_measured": {}, "limitations": lim,
            "retained": retained,
            "population": {"cells": len(want), "typical_passive_cells": len(typ_cells), "non_typical_cells": len(nontyp),
                           "excitations": list(PSRR_STUDY_EXCITATIONS)}}


SUPPLEMENTARY["gain-gbw-pm-passive-corners"]["extract"] = extract_gain_passive_study
SUPPLEMENTARY["slew-swing-power-passive-corners"]["extract"] = extract_ssp_passive_study
SUPPLEMENTARY["gain-gbw-pm-passive-corners-full"]["extract"] = extract_gain_passive_study
SUPPLEMENTARY["slew-swing-power-passive-corners-full"]["extract"] = extract_ssp_passive_study
SUPPLEMENTARY["psrr-passive-corners-full"]["extract"] = extract_psrr_passive_study  # issue #173: bound-free


FULL_STUDY_POINTS = 45  # 5 MOS corners x 3 temperatures x 3 supplies


def study_scope(pts: list, expect: str | None = None) -> dict:
    """Scope of a side study from its stated points. A study is FULL only when its Points line
    is the complete 5 x 3 x 3 grid; `expect` ('full') makes a mismatch an error so a limited
    record cannot be selected under a full-grid key (or the reverse)."""
    n = len(pts) * len(PASSIVE_LEVELS) ** 2
    mos = {p[0] for p in pts}
    full = (len(pts) == FULL_STUDY_POINTS and len(set(pts)) == FULL_STUDY_POINTS and len(mos) == 5
            and len({p[1] for p in pts}) == 3 and len({p[2] for p in pts}) == 3
            and all(any(p[0] == m and p[1] == t and p[2] == v for p in pts) for m in mos
                    for t in {q[1] for q in pts} for v in {q[2] for q in pts}))
    if expect is not None and (expect == "full") != full:
        raise ReportError(f"side study scope mismatch: selected under a {expect!r} key but its Points line states "
                          f"{len(pts)} points ({'full' if full else 'limited'} scope)")
    if full:
        return {"points": [fmt_point(*p) for p in pts], "rz_levels": list(PASSIVE_LEVELS),
                "cc_levels": list(PASSIVE_LEVELS), "cells": n, "full_pvt_cross_product": True,
                "text": (f"FULL passive-by-PVT cross product: 5 MOS corners x 3 temperatures x 3 supplies "
                         f"({len(pts)} MOS/T/VDD points) x {len(PASSIVE_LEVELS)} RZ levels x {len(PASSIVE_LEVELS)} CC "
                         f"levels ({', '.join(PASSIVE_LEVELS)}) = {n} cells")}
    return {"points": [fmt_point(*p) for p in pts], "rz_levels": list(PASSIVE_LEVELS), "cc_levels": list(PASSIVE_LEVELS),
            "cells": n, "full_pvt_cross_product": False,
            "text": (f"{len(pts)} selected MOS/T/VDD points ({'; '.join(fmt_point(*p) for p in pts)}) x "
                     f"{len(PASSIVE_LEVELS)} RZ levels x {len(PASSIVE_LEVELS)} CC levels "
                     f"({', '.join(PASSIVE_LEVELS)}) = {n} cells; not a full passive-by-PVT cross product")}


def study_observation(f: dict) -> str:
    """One measured figure of a side study, derived from its tables (information only)."""
    w = f["worst"]
    s = (f"{f['label']} {f['bound_text']}: {f['pass']}/{f['total']} cells pass, {f['fail']} fail; range "
         f"{f['range'][0]} .. {f['range'][1]} {f['unit']}; worst {w['value']} {f['unit']} at {w['point']}, "
         f"RZ {w['rz']}, CC {w['cc']}")
    if f["failing_cells"]:
        by: dict = {}
        for c in f["failing_cells"]:
            by.setdefault(c["point"], []).append(c)
        parts = []
        for pt, cs in by.items():
            rz = [lv for lv in PASSIVE_LEVELS if any(c["rz"] == lv for c in cs)]
            cc = [lv for lv in PASSIVE_LEVELS if any(c["cc"] == lv for c in cs)]
            parts.append(f"{pt} {len(cs)}/{len(PASSIVE_LEVELS) ** 2} (RZ {'/'.join(rz)}; CC {'/'.join(cc)}"
                         + (f"; {'/'.join(c['value'] for c in cs)} {f['unit']}" if len(f["failing_cells"]) <= 3 else "")
                         + ")")
        s += "; failing cells: " + ", ".join(parts)
    return s


EXTRACTORS = {"gain-gbw-pm": extract_gain, GAIN_MC_EXP: extract_gain_gbw_pm_mc, "offset-mc": extract_offset,
              "noise": extract_noise, "cmrr": extract_cmrr, "cmrr-mc": extract_cmrr_mc, "psrr": extract_psrr,
              PSRR_MC_EXP: extract_psrr_mc,
              "slew-swing-power": extract_ssp, ICMR_EXP: extract_icmr}


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------
def load_manifest(path: Path) -> dict:
    try:
        m = json.loads(path.read_text())
    except FileNotFoundError:
        raise ReportError(f"selection manifest not found: {path}")
    except json.JSONDecodeError as e:
        raise ReportError(f"selection manifest {path} is not valid JSON: {e}")
    if not isinstance(m, dict) or not isinstance(m.get("experiments"), dict):
        raise ReportError(f"selection manifest {path}: needs an 'experiments' object")
    unknown = sorted(set(m["experiments"]) - set(EXPERIMENTS))
    if unknown:
        raise ReportError(f"selection manifest: unknown experiment(s) {unknown}; known: {EXPERIMENTS}")
    for e, v in m["experiments"].items():
        if isinstance(v, dict):
            if e not in MULTI_RECORD:
                raise ReportError(f"selection manifest: {e} takes one record path, not a per-row object")
            bad = sorted(set(v) - set(MULTI_RECORD[e]))
            if bad:
                raise ReportError(f"selection manifest: {e}: unknown row(s) {bad}; known: {list(MULTI_RECORD[e])}")
        elif v is not None and not isinstance(v, str):
            raise ReportError(f"selection manifest: {e}: expected a record path")
    sup = m.get("supplementary", {})
    if not isinstance(sup, dict):
        raise ReportError(f"selection manifest {path}: 'supplementary' must be an object {{study: record path}}")
    unknown = sorted(set(sup) - set(SUPPLEMENTARY))
    if unknown:
        raise ReportError(f"selection manifest: unknown supplementary study(ies) {unknown}; known: {sorted(SUPPLEMENTARY)}")
    for k, v in sup.items():
        if v is not None and not isinstance(v, str):
            raise ReportError(f"selection manifest: supplementary {k}: expected a record path")
    return m


def selected_records(exp: str, value) -> list:
    """A manifest entry as [(record path, rows or None)]; None = every row the record judged."""
    if not value:
        return []
    if isinstance(value, dict):
        by: dict = {}
        for row in MULTI_RECORD[exp]:
            if value.get(row):
                by.setdefault(value[row], []).append(row)
        return sorted(by.items())
    return [(value, None)]


def latest_selection(root: Path) -> dict:
    sel = {}
    for exp in EXPERIMENTS:
        recs = sorted((root / "sim" / exp / "records").glob("*.md"))
        recs = [p for p in recs
                if not p.read_text().lstrip().startswith(STUDY_TITLES.get(exp, ()) or ("\0",))]
        if exp in MULTI_RECORD:
            # per row, the newest record that judged it; one path when they all agree
            picked: dict = {}
            for p in reversed(recs):
                try:
                    got = EXTRACTORS[exp](p.read_text(), f"{exp}:{p.stem}")["rows"]
                except ReportError:
                    continue
                for row in got:
                    picked.setdefault(row, f"sim/{exp}/records/{p.name}")
            vals = set(picked.values())
            if not picked:
                sel[exp] = None
            elif len(vals) == 1 and set(picked) == set(MULTI_RECORD[exp]):
                sel[exp] = vals.pop()
            else:
                sel[exp] = {r: picked[r] for r in MULTI_RECORD[exp] if r in picked}
            continue
        sel[exp] = f"sim/{exp}/records/{recs[-1].name}" if recs else None
    return sel


def check_superseded(root: Path, rel: str, allow: set) -> str | None:
    """Reject (or warn, when explicitly allowed) a record some sibling supersedes."""
    p = root / rel
    rid = p.stem
    for sib in sorted(p.parent.glob("*.md")):
        if sib == p:
            continue
        for line in sib.read_text().splitlines():
            if "**Supersedes**" in line and f"`{rid}`" in line:
                if rid in allow:
                    return f"{rid} is superseded by {sib.stem} (explicitly allowed by the manifest)"
                raise ReportError(
                    f"{rel}: record {rid} is superseded by {sib.stem}; select the superseding record "
                    f"or list '{rid}' under 'allow_superseded' to use it knowingly")
    return None


# --------------------------------------------------------------------------
# spec rows
# --------------------------------------------------------------------------
SPEC_KEYS = [  # (row-name prefix, key)
    ("Open-loop DC gain", "gain"), ("GBW", "gbw"), ("Phase margin", "pm"), ("Slew rate", "slew"),
    ("Input-referred noise", "noise"), ("Input-referred offset", "offset"), ("CMRR", "cmrr"),
    ("PSRR", "psrr"), ("Output swing", "swing"), ("Quiescent power", "power"), ("Area", "area"),
]


def clean_md(s: str) -> str:
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return re.sub(r"\s+", " ", s.replace("**", "").replace("`", "")).strip()


def split_cells(line: str) -> list:
    # split on pipes not inside backticks
    cells, cur, tick = [], "", False
    for ch in line.strip().strip("|"):
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append(cur.strip()); cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


#: In-row tag of a decision-record proposal that is not yet ratified (e.g. the
#: "[DR-5] — proposed, not ratified" input common-mode range row).
PROPOSED_RX = re.compile(r"\bproposed,? not ratified\b", re.I)


def parse_spec(spec_path: Path) -> list:
    try:
        text = spec_path.read_text()
    except FileNotFoundError:
        raise ReportError(f"spec not found: {spec_path}")
    sec = section(text, "2. Performance targets")
    rows = []
    for line in sec.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| Parameter"):
            continue
        c = split_cells(line)
        if len(c) < 6:
            continue
        name = clean_md(c[0]).split(" (")[0]
        key = next((k for pfx, k in SPEC_KEYS if name.startswith(pfx)), None)
        rows.append({"name": name, "key": key or re.sub(r"\W+", "-", name.lower()),
                     "target_raw": c[1], "target": clean_md(c[1]).split(" — ")[0],
                     "bound_open": "[TBD]" in c[1], "status": clean_md(c[5]),
                     "proposed": bool(PROPOSED_RX.search(clean_md(c[1])))})
    if not rows:
        raise ReportError(f"{spec_path}: no Sec. 2 performance-target table found")
    return rows


def bound_in_spec(record_bound: str, spec_target: str) -> bool:
    b = (record_bound.replace(">=", "≥").replace("<=", "≤").replace(" deg", "°")
         .replace("V/us", "V/µs").replace(" uW", " µW").strip())
    return b in spec_target.replace("**", "")


# --------------------------------------------------------------------------
# measurement-configuration freshness (issue #85)
# --------------------------------------------------------------------------
#: Experiments whose records carry a measurement fingerprint, with the
#: stdlib module (repo-relative) that computes the CURRENT effective inputs.
#: Every other experiment is reported as freshness-unknown until migrated.
MEASUREMENT_CONFIG = {
    "gain-gbw-pm": "sim/gain-gbw-pm/measurement_config.py",
    "noise": "sim/noise/measurement_config.py",
    "offset-mc": "sim/offset-mc/measurement_config.py",
    "cmrr": "sim/cmrr/measurement_config.py",
    "psrr": "sim/psrr/measurement_config.py",
    "psrr-mc": "sim/psrr-mc/measurement_config.py",
    "slew-swing-power": "sim/slew-swing-power/measurement_config.py",
    "input-common-mode": "sim/input-common-mode/measurement_config.py",
}
_FP_LINE = re.compile(r"^- \*\*Measurement fingerprint\*\*: version (\d+), sha256 `([0-9a-f]{64})`", re.M)
_FP_BLOCK = re.compile(r"^## Measurement fingerprint inputs\n.*?^```json\n(.*?)\n```", re.M | re.S)


def parse_fingerprint(text: str, label: str) -> dict | None:
    """The record's own fingerprint claim, verified against its retained inputs.

    None = the record predates fingerprinting (freshness unknown). A record
    that states a fingerprint must retain inputs that hash to it.
    """
    m = _FP_LINE.search(header_of(text))
    if not m:
        return None
    b = _FP_BLOCK.search(text)
    if not b:
        raise ReportError(f"{label}: states a measurement fingerprint but retains no inputs block")
    try:
        inputs = json.loads(b.group(1))
    except ValueError as e:
        raise ReportError(f"{label}: measurement fingerprint inputs are not valid JSON: {e}") from e
    h = _harness().measurement_fingerprint(inputs)
    if h != m.group(2):
        raise ReportError(f"{label}: retained measurement-fingerprint inputs hash to {h[:16]}, "
                          f"not the stated {m.group(2)[:16]} (record altered or corrupt)")
    return {"version": int(m.group(1)), "sha256": m.group(2), "inputs": inputs}


def _harness():
    sys.path.insert(0, str(HERE.parent))
    try:
        import harness
    finally:
        sys.path.pop(0)
    return harness


def current_measurement_inputs(root: Path, exp: str, retained: dict | None = None) -> dict:
    """Effective measurement inputs of `exp` as the checkout at `root` would run them.

    Single-bench modules (gain) expose `TESTBENCH_REL` and `inputs(text)`.
    Later modules expose `TESTBENCHES_REL` (name -> repo-relative bench) and
    `inputs(texts, retained)`; `retained` is the record's own stored inputs,
    from which an experiment with independent figures (slew/swing/power)
    selects the figures that record measured.
    """
    import types
    mod_path = root / MEASUREMENT_CONFIG[exp]
    if not mod_path.is_file():
        raise ReportError(f"{exp}: measurement configuration module is missing: {MEASUREMENT_CONFIG[exp]}")
    h = _harness()  # load the report's own harness first: measurement_config reuses it from sys.modules
    h.purge_config_modules()  # shared sibling configs are re-read from this root, never a cached copy
    # Compile from source (no bytecode cache) so an edited module is never shadowed by a stale .pyc.
    mod = types.ModuleType(f"_mcfg_{exp.replace('-', '_')}")
    mod.__file__ = str(mod_path)
    try:
        exec(compile(mod_path.read_text(), str(mod_path), "exec"), mod.__dict__)
    finally:
        h.purge_config_modules()
    if hasattr(mod, "TESTBENCHES_REL"):
        texts = {}
        for k, rel in mod.TESTBENCHES_REL.items():
            tb = root / rel
            if not tb.is_file():
                raise ReportError(f"{exp}: current testbench is missing: {rel}")
            texts[k] = tb.read_text()
        return mod.inputs(texts, retained)
    tb = root / mod.TESTBENCH_REL
    if not tb.is_file():
        raise ReportError(f"{exp}: current testbench is missing: {mod.TESTBENCH_REL}")
    return mod.inputs(tb.read_text())


def _diff_inputs(old, new, path="") -> list:
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new)):
            out += _diff_inputs(old.get(k), new.get(k), f"{path}{k}.")
        return out
    return [] if old == new else [path.rstrip(".")]


def measurement_freshness(root: Path, exp: str, fp: dict | None, archival: bool) -> dict:
    """Status of one source's measurement-configuration freshness."""
    if fp is None:
        why = ("not instrumented: this experiment's records carry no measurement fingerprint"
               if exp not in MEASUREMENT_CONFIG else
               "this record predates measurement fingerprinting")
        return {"status": "unknown", "detail": why}
    if exp not in MEASUREMENT_CONFIG:
        return {"status": "unknown", "detail": "record carries a fingerprint but the report has no current-configuration source for it",
                "fingerprint": fp["sha256"]}
    cur = current_measurement_inputs(root, exp, fp["inputs"])
    cur_h = _harness().measurement_fingerprint(cur)
    if cur_h == fp["sha256"]:
        return {"status": "current", "fingerprint": fp["sha256"], "version": fp["version"]}
    differs = _diff_inputs(fp["inputs"], cur)
    return {"status": "stale", "fingerprint": fp["sha256"], "version": fp["version"],
            "current_fingerprint": cur_h, "differs": differs}


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def current_dut_sha256(root: Path) -> str:
    """sha256 of the current netlist under the simulation drivers' normaliser.

    Same bytes the drivers hash (`sim/harness.py` normalize_dut_text), so it is
    comparable with the `normalised sha256` every record carries. No simulator.
    """
    path = root / DUT_REL
    if not path.is_file():
        raise ReportError(f"current DUT netlist is missing: {DUT_REL}")
    sys.path.insert(0, str(HERE.parent))
    try:
        import harness
    finally:
        sys.path.pop(0)
    try:
        text = harness.normalize_dut_text(path.read_text())
    except SystemExit as e:  # check_dc_op exits when the subckt is absent
        raise ReportError(f"{DUT_REL}: cannot normalise current netlist: {e}") from e
    return hashlib.sha256(text.encode()).hexdigest()


def build_supplementary(root: Path, manifest: dict, spec_by_key: dict, dut_prefix: str | None, allow: set,
                        archival: bool, spec_rel: str, extracted: dict | None = None) -> dict:
    """Load, check and summarise the explicitly selected side studies (issue #121).

    Each study is hashed and parsed like a selected record (missing file, wrong directory or
    title, superseded record, malformed/duplicate/missing cells, summary-vs-table disagreement
    and a bound other than the ratified one are errors), checked against the selected records'
    DUT and the current netlist, and given the same measurement-configuration freshness policy
    (stale: error unless --archival; unknown: disclosed). The result is information only: it
    never feeds a row's verdict, points or the summary counts.

    A `bound_free` study (issue #173: PSRR) reads no bound at all: instead of validating figures
    against a ratified bound it requires every row it informs to be tagged "proposed, not
    ratified" (no grader exists, so a ratified row is an error, never a silent pass), and its
    adapter also re-derives its figures from retained per-cell curves (hashed) and checks them
    against the selected primary record of the same experiment (`extracted`), if any.
    """
    out = {}
    cur = None if archival else current_dut_sha256(root)
    for key, rel in sorted((manifest.get("supplementary") or {}).items()):
        if not rel:
            continue
        spec = SUPPLEMENTARY[key]
        exp = spec["experiment"]
        label = f"supplementary {key}"
        p = root / rel
        if not p.is_file():
            raise ReportError(f"{label}: selected record is missing: {rel}")
        if p.parent.resolve() != (root / "sim" / exp / "records").resolve() or p.suffix != ".md":
            raise ReportError(f"{label}: {rel} is not a record under sim/{exp}/records/*.md")
        text = p.read_text()
        if not text.lstrip().startswith(spec["title"]):
            raise ReportError(f"{label}: {rel} is not a '{spec['title'].lstrip('# ')}' record "
                              "(a grid record is selected under 'experiments', never as a side study)")
        w = check_superseded(root, rel, allow)
        label = f"{label}:{p.stem}"
        bound_free = bool(spec.get("bound_free"))
        if bound_free:
            ex = spec["extract"](text, label, root, p.stem, (extracted or {}).get(exp))
        else:
            ex = spec["extract"](text, label)
        prov = ex["prov"]
        if dut_prefix is not None and prov["dut_prefix"] != dut_prefix:
            raise ReportError(f"{label}: the side study measures DUT {prov['dut_prefix']}, the selected records "
                              f"{dut_prefix}; select a study of the same netlist (or rerun it)")
        if cur is not None and not cur.startswith(prov["dut_prefix"]):
            raise ReportError(
                f"stale DUT: {label} does not match the current {DUT_REL} (normalised sha256 {cur[:16]}); it measured "
                f"{prov['dut_prefix']}. Rerun the side study (its runner's --passive-corners mode) to append a new record "
                "and select it; existing records are append-only and are not edited. "
                "For a historical (non-signoff) report use --archival with an explicit --out-dir.")
        if bound_free:
            for rk in spec["rows"]:
                sr = spec_by_key.get(rk)
                if sr is None or not sr["proposed"]:
                    raise ReportError(
                        f"{label}: the bound-free side study informs the {rk} row, but {spec_rel} "
                        + ("has no such row" if sr is None else "no longer tags it 'proposed, not ratified'")
                        + "; this report has no grader for the study's figures. Extend the report to grade the "
                        "ratified bound before regenerating it (a verdict is never inferred)")
        for fk, f in ([] if bound_free else ex["figures"].items()):
            sr = spec_by_key.get(fk)
            if sr is None or sr["bound_open"] or not bound_in_spec(f["bound_text"], sr["target_raw"]):
                raise ReportError(f"{label}: study bound '{f['bound_text']}' for {f['label']} is not the current "
                                  f"ratified bound in {spec_rel}" + (f" ('{sr['target']}')" if sr else ""))
        fp = parse_fingerprint(text, label)
        fresh = measurement_freshness(root, exp, fp, archival)
        if fresh["status"] == "stale" and not archival:
            raise ReportError(
                f"stale measurement configuration: {label} was measured with a different bench/analysis configuration "
                f"than the current one (changed {', '.join(fresh['differs'])}). Rerun the side study to append a new "
                "record and select it; existing records are append-only and are not edited. "
                "For a historical (non-signoff) report use --archival with an explicit --out-dir.")
        lim = ["side study: not a selected verdict record; its cells are not counted in any row's points or verdict "
               "nor in the ratified-row summary"]
        if prov["fleet_runner_mismatch"]:
            lim.append("fleet runner klt version differs from the client's (compatibility mismatch)")
        if fresh["status"] == "unknown":
            lim.append("measurement-configuration freshness unknown: " + fresh["detail"] +
                       "; the bench/analysis settings it measured cannot be compared with today's")
        elif fresh["status"] == "stale":
            lim.append(f"measurement configuration differs from the current bench (changed: {', '.join(fresh['differs'])}); "
                       "archival report only")
        if w:
            lim.append(w)
        lim += [l for l in ex.get("limitations", []) if l not in lim]
        if bound_free and fresh["status"] == "unknown":
            # the population and the passive section map are taken from the fingerprint inputs
            raise ReportError(f"{label}: a bound-free side study needs a measurement fingerprint (freshness unknown)")
        out[key] = {
            "graded": False, "experiment": exp, "record_id": p.stem, "path": posix_rel(p, root), "sha256": sha256_file(p),
            "dut_sha256": prov["dut_sha256"],
            "dut_check": {"matches_selected_records": True if dut_prefix is not None else None,
                          "matches_current_netlist": True if cur is not None else None},
            **{k_: prov[k_] for k_ in ("pdk_open_pdks", "ngspice_local", "ngspice_engine", "klt_client", "backend",
                                       "fleet_runner_mismatch")},
            "measurement_config": fresh,
            "scope": study_scope(ex["points"], spec.get("scope", "limited")),
            "rows": list(spec["rows"]),
            "figures": ex["figures"],
            "not_measured": ex["not_measured"],
            "limitations": lim,
        }
        if bound_free:
            out[key].update(bound_free=True, verdict=None, population=ex["population"],
                            retained_evidence=ex["retained"],
                            grading=("none: measured information attached to the "
                                     f"{', '.join(spec['rows'])} row; its bound is proposed, not ratified, so no "
                                     "PASS/FAIL verdict is formed and no figure is compared with a bound"))
    return out


def study_row_note(st: dict, row_key: str) -> str:
    """Row-detail citation of a side study, derived from its tables (never a verdict)."""
    head = (f"supplementary side study `{st['path']}` (not a selected verdict record; outside this row's verdict and "
            f"point count; {st['scope']['cells']} cells = {len(st['scope']['points'])} MOS/T/VDD points x RZ x CC, "
            + ("the full passive-by-PVT cross product)" if st["scope"]["full_pvt_cross_product"]
               else "not a full passive-by-PVT cross product)"))
    if st.get("bound_free"):
        return (f"{head} measures, as information only with no verdict (bound-free: the row's bound is proposed, "
                f"not ratified): " + "; ".join(psrr_study_observation(f) for f in st["figures"].values()))
    if row_key in st["figures"]:
        return f"{head} measures {study_observation(st['figures'][row_key])}"
    return f"{head}: {st['not_measured'].get(row_key, 'no observation for this row')}"


def psrr_study_observation(f: dict) -> str:
    """One bound-free PSRR study figure (issue #173), derived from the retained curves; never a verdict."""
    w, d = f["worst"], f["delta_vs_typical"]
    return (f"{f['label']} worst {w['value']} dB at {w['point']}, RZ {w['rz']}, CC {w['cc']} (best {f['best']['value']} "
            f"dB; worst over the {f['typical_passive_worst']['cells']} typical-passive cells "
            f"{f['typical_passive_worst']['value']} dB, so the passive spread moves the worst case by "
            f"{f['passive_shift_of_worst']} dB; delta vs typical passives at matching PVT "
            f"{d['most_negative']['value']} .. {d['most_positive']['value']} dB over {d['cells']} cells)")


def build(root: Path, manifest: dict, spec_rel: str = "spec/target-spec.md", archival: bool = False) -> dict:
    exps = manifest["experiments"]
    allow = set(manifest.get("allow_superseded", []))
    sources, extracted, warnings = {}, {}, []
    row_source = {}  # multi-record experiments: row key -> source key
    for exp in EXPERIMENTS:
        rels = selected_records(exp, exps.get(exp))
        for rel, want_rows in rels:
            p = root / rel
            if not p.is_file():
                raise ReportError(f"{exp}: selected record is missing: {rel}")
            if p.parent.resolve() != (root / "sim" / exp / "records").resolve() or p.suffix != ".md":
                raise ReportError(f"{exp}: {rel} is not a record under sim/{exp}/records/*.md")
            w = check_superseded(root, rel, allow)
            if w:
                warnings.append(w)
            text = p.read_text()
            ex = EXTRACTORS[exp](text, f"{exp}:{p.stem}")
            if exp == ICMR_EXP:
                ex["retained"] = icmr_crosscheck(root, p.stem, ex, f"{exp}:{p.stem}")
                ex["addendum"] = _icmr_addendum(root, p.stem)
                if ex["addendum"]:
                    ex["limitations"].append(
                        f"the record has an addendum ({ex['addendum']['path']}, sha256 {ex['addendum']['sha256'][:16]}) "
                        "with disclosures and presentation corrections; read it with the record")
            if exp == "offset-mc" and ex.get("grid"):
                ex["retained"] = offset_grid_crosscheck(root, p.stem, ex, f"{exp}:{p.stem}")
            if exp == CMRR_MC_EXP:
                ex["retained"] = cmrr_mc_crosscheck(root, p.stem, ex, f"{exp}:{p.stem}")
            if exp == PSRR_MC_EXP:
                ex["retained"] = psrr_mc_crosscheck(root, p.stem, ex, f"{exp}:{p.stem}")
            if exp == GAIN_MC_EXP:
                ex["retained"] = gain_gbw_pm_mc_crosscheck(root, p.stem, ex, f"{exp}:{p.stem}")
            if w:
                ex["limitations"].append(w)
                for r_ in ex.get("rows", {}).values():
                    if isinstance(r_, dict) and "limitations" in r_:
                        r_["limitations"].append(w)
            ex["text_len"] = len(text)
            fp = parse_fingerprint(text, f"{exp}:{p.stem}")
            fresh = measurement_freshness(root, exp, fp, archival)
            ex["measurement_config"] = fresh
            if fresh["status"] == "unknown":
                note = ("measurement-configuration freshness unknown: " + fresh["detail"] +
                        "; the bench/analysis settings it measured cannot be compared with today's")
                ex["limitations"].append(note)
                for r_ in ex.get("rows", {}).values():
                    if isinstance(r_, dict) and "limitations" in r_:
                        r_["limitations"].append(note)
            skey = exp if len(rels) == 1 else f"{exp} ({p.stem})"
            if exp in MULTI_RECORD:
                if want_rows is not None:
                    missing = [rk for rk in want_rows if rk not in ex["rows"]]
                    if missing:
                        raise ReportError(f"{exp}: {rel} is selected for {missing} but does not judge "
                                          f"{'that row' if len(missing) == 1 else 'those rows'}")
                    ex["rows"] = {rk: ex["rows"][rk] for rk in want_rows}
                for rk in ex["rows"]:
                    row_source[rk] = skey
            extracted[skey] = ex
            pv = ex["prov"]
            sources[skey] = {"record_id": p.stem, "path": posix_rel(p, root), "sha256": sha256_file(p), **pv,
                             "measurement_config": fresh}
            if exp in (CMRR_MC_EXP, PSRR_MC_EXP):
                sources[skey]["retained_evidence"] = ex["retained"]
            if exp == GAIN_MC_EXP:
                sources[skey]["retained_evidence"] = ex["retained"]
    # DUT compatibility: compare on the 16-hex prefix (not all records keep the full hash).
    duts = {}
    for exp, s in sources.items():
        duts.setdefault(s["dut_prefix"], []).append(exp)
    if len(duts) > 1:
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(duts.items()))
        raise ReportError("selected records measure different DUT versions (" + detail +
                          "); regenerate the stale experiment(s) against the same netlist")
    dut_prefix = next(iter(duts), None)
    dut_full = next((s["dut_sha256"] for s in sources.values() if len(s["dut_sha256"]) == 64), None)
    # Current-design gate (issue #75): the selected records must measure the
    # netlist as it is now. Archival mode skips this and is marked as such.
    if not archival:
        cur = current_dut_sha256(root)
        stale = sorted(e for e, s in sources.items() if not cur.startswith(s["dut_prefix"]))
        if stale:
            raise ReportError(
                f"stale DUT: selected records do not match the current {DUT_REL} "
                f"(normalised sha256 {cur[:16]}); records measured {dut_prefix}. "
                "Experiments needing a rerun: " + ", ".join(stale) +
                ". Rerun them (sim/characterize.sh, or each sim/<experiment>/run_*.py) to append new "
                "records, then regenerate the report; existing records are append-only and are not edited. "
                "For a historical (non-signoff) report use --archival with an explicit --out-dir.")

    # Measurement-configuration gate (issue #85), additive to the DUT gate above:
    # fingerprinted evidence must match today's effective bench; unknown is disclosed.
    stale_cfg = sorted(e for e, s in sources.items() if s["measurement_config"]["status"] == "stale")
    if stale_cfg and not archival:
        detail = "; ".join(f"{e}: changed {', '.join(sources[e]['measurement_config']['differs'])}" for e in stale_cfg)
        raise ReportError(
            "stale measurement configuration: selected records were measured with a different bench/analysis "
            f"configuration than the current one ({detail}). Experiments needing a rerun: "
            + ", ".join(e.split(" (")[0] for e in stale_cfg) +
            ". Rerun them (sim/<experiment>/run_*.py) to append new records, then regenerate the report; "
            "existing records are append-only and are not edited. "
            "For a historical (non-signoff) report use --archival with an explicit --out-dir.")

    # report-level provenance limitations
    glob_lim = []
    for e, s in sorted(sources.items()):
        mcs = s["measurement_config"]
        if mcs["status"] == "stale":
            glob_lim.append(f"{e}: measurement configuration differs from the current bench "
                            f"(changed: {', '.join(mcs['differs'])}); archival report only")
        elif mcs["status"] == "unknown":
            glob_lim.append(f"{e}: measurement-configuration freshness unknown ({mcs['detail']}); "
                            "DUT freshness is checked, bench/analysis freshness is not")
    pdks = {}
    for exp, s in sorted(sources.items()):
        for kind in ("pdk_open_pdks", "pdk_client_resolved"):
            if s[kind]:
                pdks.setdefault(s[kind], []).append(f"{exp} ({'recorded' if kind == 'pdk_open_pdks' else 'klt client-resolved'})")
    if len(pdks) > 1:
        glob_lim.append("PDK revision differs between/within sources: " +
                        "; ".join(f"{k[:12]} <- {', '.join(v)}" for k, v in sorted(pdks.items())))
    engs = {}
    for exp, s in sorted(sources.items()):
        for k, lab in (("ngspice_local", "local"), ("ngspice_engine", "klt engine")):
            if s[k]:
                engs.setdefault(s[k], []).append(f"{exp} ({lab})")
    if len(engs) > 1:
        glob_lim.append("ngspice versions differ: " + "; ".join(f"ngspice {k} <- {', '.join(v)}" for k, v in sorted(engs.items())))
    covs = {(e["coverage"]["points"], tuple(e["coverage"]["temps_c"]), tuple(e["coverage"]["vdd_v"])) for e in extracted.values()}
    if len(covs) > 1:
        glob_lim.append("coverage differs between sources (see each row's coverage; e.g. the CMRR mismatch Monte Carlo "
                        "record is 5 corner points at 27 C / 3.30 V while the AC rows are full 45-point grids)")
    for exp in EXPERIMENTS:
        if not selected_records(exp, exps.get(exp)):
            glob_lim.append(f"no record selected for {exp}: " + (
                "the proposed input common-mode range row carries no measurement (explicitly missing; "
                "nothing is inferred from the spec status text)" if exp == ICMR_EXP
                else "the AC rows carry no mismatch-inclusive evidence in this report" if exp == GAIN_MC_EXP
                else "its rows are reported as not measured"))

    spec_rows = parse_spec(root / spec_rel)
    spec_by_key = {r["key"]: r for r in spec_rows}
    supplementary = build_supplementary(root, manifest, spec_by_key, dut_prefix, allow, archival, spec_rel,
                                        extracted)
    for k, st in supplementary.items():
        mcs = st["measurement_config"]
        if mcs["status"] == "stale":
            glob_lim.append(f"supplementary {k}: measurement configuration differs from the current bench "
                            f"(changed: {', '.join(mcs['differs'])}); archival report only")
        elif mcs["status"] == "unknown":
            glob_lim.append(f"supplementary {k}: measurement-configuration freshness unknown ({mcs['detail']}); "
                            "DUT freshness is checked, bench/analysis freshness is not")
    for k in sorted(SUPPLEMENTARY):
        if k not in supplementary:
            glob_lim.append(f"no supplementary study selected for {k}: its rows ("
                            f"{', '.join(SUPPLEMENTARY[k]['rows'])}) carry no passive-corner observation")
    out_rows = []

    def base(sr, **kw):
        d = {"row": sr["name"], "key": sr["key"], "spec_bound": sr["target"] if not sr["bound_open"] else "open",
             "bound_open": sr["bound_open"], "status": "not-measured", "verdict": None, "points_pass": None,
             "points_total": None, "worst": None, "worst_corner": None, "figures": [], "coverage": None,
             "limitations": [], "source": None}
        d.update(kw)
        return d

    def src(skey):
        s = sources[skey]
        exp = skey.split(" (")[0]
        return {"experiment": exp, "record_id": s["record_id"], "path": s["path"], "sha256": s["sha256"]}

    def proposed_row(row, sr, coverage=None, source=None, extra=()):
        """A row whose bound is tagged in-row "proposed, not ratified": never graded.

        No verdict, no worst value or point count beside the bound (a selected record's figures,
        if any, are listed in the row details as information only), counted neither among the
        ratified rows judged nor as not measured; the spec's own status text is quoted verbatim.
        """
        row.update(status="proposed-not-graded", spec_bound=f"proposed, not ratified: {sr['target']}",
                   coverage=coverage, source=source, spec_status=sr["status"])
        row["limitations"] = ["bound proposed by a decision record and not ratified: this report grades "
                              "ratified rows only, so the row is not graded and is counted neither among "
                              "the ratified rows judged nor as not measured; see the spec status column "
                              "for the evidence it cites"] + list(extra)

    def cmrr_mc_row(row, ex, source, srec):
        """Mismatch-inclusive CMRR figures of the CMRR row: separate, labelled, information only.

        Never in `worst`, never a verdict; linear 3-sigma and dB 3-sigma stay distinct figures; the
        population is 27 C / 3.30 V only (T and VDD are not sampled under mismatch).
        """
        rv, ctl = ex["retained"], ex["controls"]
        pop = (f"mismatch-inclusive, {len(ex['order'])} MOS corners at {ex['t_c']} C / {ex['vdd']} V only, "
               f"N={ex['n_per_point']} per corner")
        row["limitations"].append(
            "the mismatch-inclusive figures below come from the selected CMRR mismatch Monte Carlo record "
            f"({source['record_id']}): {len(ex['order'])} MOS corners x N={ex['n_per_point']} = {ex['n_total']} samples, "
            f"seed {ex['seed']}, vary \"{ex['vary']}\", {ex['t_c']} C / {ex['vdd']} V only. They are measured "
            "information, not graded, and are not combined with the systematic PVT figures: the systematic record "
            "covers T and VDD (matched devices), the Monte Carlo record covers mismatch (one T / VDD point)")
        row["limitations"] += [l for l in ex["limitations"] if l not in row["limitations"]]
        row["figures"].append({"label": "mismatch Monte Carlo population (information only, not graded; seed "
                                        f"{ex['seed']}, vary \"{ex['vary']}\", {ex['n_total']} samples)",
                               "value": coverage_text(ex["coverage"]), "corner": None})
        for head, key, _ in CMRR_MC_FIGS:
            w = ex["worst"][key]
            tag = f"{pop}; information only, not graded"
            row["figures"].append({"label": f"mismatch CMRR {head}, linear 3-sigma lowest "
                                            f"(mean Ad - 20 log10(mean|Acm| + 3 sigma|Acm|)) [{tag}]",
                                   "value": f"{w['lin3']['value']:.2f} dB", "corner": fmt_point(*w["lin3"]["at"])})
            row["figures"].append({"label": f"mismatch CMRR {head}, dB 3-sigma lowest (mean - 3 sigma of the dB "
                                            f"values) [{tag}]",
                                   "value": f"{w['db3']['value']:.2f} dB", "corner": fmt_point(*w["db3"]["at"])})
        row["figures"].append({"label": "mismatch CMRR sample minimum over all samples, DC plateau "
                                        f"(empirical, N={ex['n_per_point']} per corner; information only)",
                               "value": f"{ex['worst']['dc']['min']['value']:.2f} dB",
                               "corner": fmt_point(*ex["worst"]["dc"]["min"]["at"])})
        row["figures"].append({"label": "mismatch controls: switch-off max deviation from the systematic record "
                                        "(DC..1 MHz / at f_u; information only)",
                               "value": f"{ctl['switch_off_max_dev_db_dc_to_1m']:.4f} dB / "
                                        f"{ctl['switch_off_max_dev_db_at_fu']:.3f} dB", "corner": None})
        row["figures"].append({"label": "mismatch controls: mirror-imbalance negative control, shift of mean |Acm| "
                                        "at DC (information only)",
                               "value": f"{ctl['imbalance_shift_acm_vv']:+.4f} V/V "
                                        f"({ctl['imbalance_shift_acm_vv'] / ctl['imbalance_shift_se']:.1f} standard errors)",
                               "corner": None})
        row["figures"].append({"label": "mismatch per-point statistics re-derived from the retained samples (information only)",
                               "value": f"{rv['samples']} samples at {rv['points']} points agree with the record "
                                        f"({rv['path']}, sha256 {rv['sha256'][:16]})", "corner": None})
        row["cmrr_mc"] = {
            "basis": "mismatch-inclusive, information only, not graded; linear 3-sigma and dB 3-sigma are separate figures",
            "population": {"processes": [k[0] for k in ex["order"]], "temperature_c": ex["t_c"], "vdd_v": ex["vdd"],
                           "n_per_point": ex["n_per_point"], "samples": ex["n_total"], "seed": ex["seed"],
                           "vary": ex["vary"], "temperature_and_supply_sampled": False},
            "controls": ctl,
            "worst_per_figure": {key: {c: {"value": v["value"], "at": fmt_point(*v["at"])} for c, v in
                                       ex["worst"][key].items()} for _, key, _ in CMRR_MC_FIGS},
            "per_point": {key: {fmt_point(*k): {c: ex["stats"][key][k][c] for c in ("n", "mean", "sigma", "lin3", "db3")}
                                for k in ex["order"]} for _, key, _ in CMRR_MC_FIGS},
            "retained_evidence": rv,
            "source": source,
            "measurement_config": srec["measurement_config"],
        }

    def psrr_mc_row(row, ex, source, srec):
        """Mismatch-inclusive PSRR+ / PSRR- figures of the PSRR row: separate, labelled, information only.

        Never in `worst`, never a verdict; linear 3-sigma and dB 3-sigma stay distinct figures. For a
        nominal record the population is 27 C / 3.30 V only; a full-grid record (issue #190) covers the
        T x VDD envelope with typical passives.
        """
        rv, ctl = ex["retained"], ex["controls"]
        if ex.get("grid"):
            temps, sups = ex["temps_c"], ex["supplies_v"]
            pop = (f"mismatch-inclusive full PVT grid ({len(ex['order'])} points = {len(ex['order']) // (len(temps) * len(sups))} "
                   f"MOS corners x {len(temps)} T x {len(sups)} VDD, VCM = VDD/2), N={ex['n_per_point']} per point")
            scope = (f"the full T/VDD grid ({', '.join(temps)} C x {', '.join(sups)} V); the RZ/CC passives stay "
                     "typical at every point")
            lim_scope = (f"{len(ex['order'])} points x N={ex['n_per_point']} = {ex['n_total']} samples over the full "
                         f"T/VDD grid (T {', '.join(temps)} C; VDD {', '.join(sups)} V; VCM = VDD/2), seed {ex['seed']}, "
                         f"vary \"{ex['vary']}\". They are measured information, not graded, and are not combined with "
                         "the systematic PVT figures: the systematic record covers T and VDD for matched devices, "
                         "this record covers mismatch, and no bound is proposed or judged at any point")
            lim_nom = ("the committed 27 C / 3.30 V mismatch record remains the only mismatch-inclusive evidence "
                       "its spec row cites; this grid proposes nothing new")
        else:
            pop = (f"mismatch-inclusive, {len(ex['order'])} MOS corners at {ex['t_c']} C / {ex['vdd']} V only, "
                   f"N={ex['n_per_point']} per corner")
            scope = f"{ex['t_c']} C / {ex['vdd']} V only"
            lim_scope = (f"{len(ex['order'])} MOS corners x N={ex['n_per_point']} = {ex['n_total']} samples, "
                         f"seed {ex['seed']}, vary \"{ex['vary']}\", {ex['t_c']} C / {ex['vdd']} V only. They are "
                         "measured information, not graded, and are not combined with the systematic PVT figures: "
                         "the systematic record covers T and VDD (matched devices), the Monte Carlo record covers "
                         "mismatch (one T / VDD point)")
            lim_nom = None
        row["limitations"].append(
            "the mismatch-inclusive figures below come from the selected PSRR mismatch Monte Carlo record "
            f"({source['record_id']}): " + lim_scope)
        if lim_nom:
            row["limitations"].append(lim_nom)
        row["limitations"] += [l for l in ex["limitations"] if l not in row["limitations"]]
        row["figures"].append({"label": "mismatch Monte Carlo population (information only, not graded; seed "
                                        f"{ex['seed']}, vary \"{ex['vary']}\", {ex['n_total']} samples)",
                               "value": coverage_text(ex["coverage"]), "corner": None})
        for sname, _, sym in PSRR_MC_SIDES:
            for head, key, _ in PSRR_MC_FIGS:
                w = ex["worst"][_pm_key(sname, key)]
                tag = f"{pop}; information only, not graded"
                row["figures"].append({"label": f"mismatch {sname} {head}, linear 3-sigma lowest "
                                                f"(mean Ad - 20 log10(mean|{sym}| + 3 sigma|{sym}|)) [{tag}]",
                                       "value": f"{w['lin3']['value']:.2f} dB", "corner": fmt_point(*w["lin3"]["at"])})
                row["figures"].append({"label": f"mismatch {sname} {head}, dB 3-sigma lowest (mean - 3 sigma of the "
                                                f"dB values) [{tag}]",
                                       "value": f"{w['db3']['value']:.2f} dB", "corner": fmt_point(*w["db3"]["at"])})
            wm = ex["worst"][_pm_key(sname, "dc")]["min"]
            row["figures"].append({"label": f"mismatch {sname} sample minimum over all samples, DC plateau "
                                            f"(empirical, N={ex['n_per_point']} per {'point' if ex.get('grid') else 'corner'}; information only)",
                                   "value": f"{wm['value']:.2f} dB", "corner": fmt_point(*wm["at"])})
        row["figures"].append({"label": "mismatch controls: switch-off max deviation from the systematic record "
                                        "(DC..1 MHz / at f_u; information only)",
                               "value": f"{ctl['switch_off_max_dev_db_dc_to_1m']:.4f} dB / "
                                        f"{ctl['switch_off_max_dev_db_at_fu']:.3f} dB", "corner": None})
        for sname, _, sym in PSRR_MC_SIDES:
            im = ctl["imbalance"][sname]
            row["figures"].append({"label": f"mismatch controls: mirror-imbalance negative control, shift of mean "
                                            f"|{sym}| at DC ({sname}; information only)",
                                   "value": f"{im['shift_asup_vv']:+.4f} V/V "
                                            f"({im['shift_asup_vv'] / im['shift_se']:.1f} standard errors)",
                                   "corner": None})
        row["figures"].append({"label": "mismatch near-cancellation knee rails (DC figure = 0.1 Hz asymptote; "
                                        "information only)",
                               "value": f"{ex['knee_rails']} of {2 * ex['n_total']} sample rails", "corner": None})
        row["figures"].append({"label": "mismatch per-point statistics re-derived from the retained samples (information only)",
                               "value": f"{rv['samples']} samples at {rv['points']} points agree with the record "
                                        f"({rv['path']}, sha256 {rv['sha256'][:16]})", "corner": None})
        if ex.get("grid"):
            population = {"grid": True, "processes": sorted({k[0] for k in ex["order"]}),
                          "temps_c": ex["temps_c"], "supplies_v": ex["supplies_v"],
                          "vcm_of": ex["vcm_of"], "n_per_point": ex["n_per_point"], "samples": ex["n_total"],
                          "seed": ex["seed"], "vary": ex["vary"], "temperature_and_supply_sampled": True,
                          "passives": "typical at every point"}
        else:
            population = {"processes": [k[0] for k in ex["order"]], "temperature_c": ex["t_c"], "vdd_v": ex["vdd"],
                          "n_per_point": ex["n_per_point"], "samples": ex["n_total"], "seed": ex["seed"],
                          "vary": ex["vary"], "temperature_and_supply_sampled": False}
        row["psrr_mc"] = {
            "basis": "mismatch-inclusive, information only, not graded; linear 3-sigma and dB 3-sigma are separate figures",
            "scope": scope,
            "population": population,
            "controls": ctl,
            "knee_rails": ex["knee_rails"],
            "floor_samples": ex["floor_samples"],
            "worst_per_figure": {sk: {c: {"value": v["value"], "at": fmt_point(*v["at"])} for c, v in w.items()}
                                 for sk, w in ex["worst"].items()},
            "per_point": {sk: {fmt_point(*k): {c: st[k][c] for c in ("n", "mean", "sigma", "lin3", "db3")}
                               for k in ex["order"]} for sk, st in ex["stats"].items()},
            "retained_evidence": rv,
            "source": source,
            "measurement_config": srec["measurement_config"],
        }

    def gain_mc_row(row, ex, source, srec):
        """Mismatch-inclusive gain/GBW/PM figures of a graded AC row: separate, labelled, information only.

        Never in `worst`, never a verdict; the deterministic record stays responsible for the graded
        row; the population is 27 C / 3.30 V only (T and VDD are not sampled under mismatch).
        """
        rv = ex["retained"]
        row["limitations"].append(
            "the mismatch-inclusive figures below come from the selected gain/GBW/PM mismatch Monte Carlo record "
            f"({source['record_id']}): {len(ex['order'])} MOS corners x N={ex['n_per_point']} = {ex['n_total']} "
            f"samples, seed {ex['seed']}, vary \"{ex['vary']}\", {ex['t_c']} C / {ex['vdd']} V only. They are "
            "measured information, not graded, and are not combined with the deterministic PVT figures: the "
            "deterministic record grades the ratified bound over its matched-device PVT grid, the Monte Carlo "
            "record covers mismatch (one T / VDD point)")
        row["limitations"] += [l for l in ex["limitations"] if l not in row["limitations"]]
        row["figures"].append({"label": "mismatch Monte Carlo population (information only, not graded; seed "
                                        f"{ex['seed']}, vary \"{ex['vary']}\", {ex['n_total']} samples)",
                               "value": coverage_text(ex["coverage"]), "corner": None})
        for k in ex["order"]:
            pt = fmt_point(*k)
            for rk, _, _, _ in GAIN_MC_ROWS:
                s = ex["stats"][k][rk]
                row["figures"].append({
                    "label": f"mismatch {rk} at {pt} (bound {s['bound']}; valid {s['valid']}/{s['n']}; "
                             "information only, not graded)",
                    "value": f"mean {s['mean_text']}, sigma {s['sigma_text']}, mean - 3 sigma {s['mean3_text']}; "
                             f"below bound {s['below']}, invalid {s['invalid']}, fail fraction {s['fraction_text']}",
                    "corner": None})
        row["figures"].append({"label": "mismatch per-corner statistics re-derived from the retained samples "
                                        "(information only)",
                               "value": f"{rv['samples']} samples at {rv['points']} corners agree with the record "
                                        f"({rv['path']}, sha256 {rv['sha256'][:16]})", "corner": None})
        row["gain_gbw_pm_mc"] = {
            "basis": "mismatch-inclusive, information only, not graded; the deterministic record grades the "
                     "ratified bound over its matched-device PVT grid",
            "population": {"processes": [k[0] for k in ex["order"]], "temperature_c": ex["t_c"], "vdd_v": ex["vdd"],
                           "n_per_point": ex["n_per_point"], "samples": ex["n_total"], "seed": ex["seed"],
                           "vary": ex["vary"], "temperature_and_supply_sampled": False},
            "bounds": {rk: b["text"] for rk, b in ex["bounds"].items()},
            "per_corner": {fmt_point(*k): {rk: {"n": s["n"], "valid": s["valid"], "mean": s["mean"],
                                                "sigma": s["sigma"], "mean_minus_3sigma": s["mean3"],
                                                "below_bound": s["below"], "invalid": s["invalid"],
                                                "fail_fraction": s["fraction"], "bound": s["bound"]}
                                           for rk, s in ex["stats"][k].items()} for k in ex["order"]},
            "retained_evidence": rv,
            "source": source,
            "measurement_config": srec["measurement_config"],
        }

    gain_mc_shown = False
    for sr in spec_rows:
        k = sr["key"]
        row = base(sr)
        if k in ("gain", "gbw", "pm") and "gain-gbw-pm" in extracted:
            ex = extracted["gain-gbw-pm"]; r = ex["rows"][k]
            if sr["bound_open"] or not bound_in_spec(r["bound_text"], sr["target_raw"]):
                raise ReportError(f"gain-gbw-pm:{sources['gain-gbw-pm']['record_id']}: record bound "
                                  f"'{r['bound_text']}' for {sr['name']} is not the current ratified bound "
                                  f"in {spec_rel} ('{sr['target']}')")
            row.update(status="measured-verdict", verdict=r["verdict"], points_pass=r["pass"],
                       points_total=r["total"], worst=r["worst"], worst_corner=r["worst_corner"],
                       coverage=ex["coverage"], limitations=list(ex["limitations"]), source=src("gain-gbw-pm"))
            row["spec_bound"] = r["bound_text"].replace(">=", "≥")
            if k == "gain" and ex["stretch_gain_70db"]:
                row["figures"].append({"label": "stretch >= 70 dB (not a mandatory row), points holding",
                                       "value": ex["stretch_gain_70db"], "corner": None})
            gm = extracted.get(GAIN_MC_EXP)
            if gm:
                mb = gm["bounds"][k]
                if (mb["value"], mb["unit"]) != (r["bound_value"], r["bound_text"].split()[-1]):
                    raise ReportError(f"gain-gbw-pm-mc:{sources[GAIN_MC_EXP]['record_id']}: record bound "
                                      f"'{mb['text']}' for {sr['name']} is not the current ratified bound "
                                      f"('{r['bound_text']}') the deterministic record grades")
                gain_mc_row(row, gm, src(GAIN_MC_EXP), sources[GAIN_MC_EXP])
                gain_mc_shown = True
        elif k in ("power", "slew", "swing") and k in row_source:
            skey = row_source[k]
            ex = extracted[skey]; r = ex["rows"][k]
            if sr["bound_open"] or not bound_in_spec(r["bound_text"], sr["target_raw"]):
                raise ReportError(f"slew-swing-power:{sources[skey]['record_id']}: record bound "
                                  f"'{r['bound_text']}' for {sr['name']} is not the current ratified bound "
                                  f"in {spec_rel} ('{sr['target']}')")
            row.update(status="measured-verdict", verdict=r["verdict"], points_pass=r["pass"],
                       points_total=r["total"], worst=r["worst"], worst_corner=r["worst_corner"],
                       coverage=r["coverage"], limitations=list(r["limitations"]), source=src(skey))
            row["spec_bound"] = r["bound_text"].replace(">=", "≥").replace("<=", "≤")
            if r.get("stretch"):
                row["figures"].append({"label": f"stretch >= {r['stretch'][0]} Vpp (not a mandatory row), points holding",
                                       "value": r["stretch"][1], "corner": None})
            if r.get("rederived"):
                row["figures"].append({"label": "re-derived from the record's committed per-point data (incl. M6/M7 Vds/Vdsat)",
                                       "value": "all points reproduce", "corner": None})
        elif k == "noise" and "noise" in extracted:
            ex = extracted["noise"]
            top = ex["spread"][0]
            row.update(status="measured-no-bound", worst=f"{top['max_uv']} uV rms ({top['band']}, highest)",
                       worst_corner=top["max_corner"], coverage=ex["coverage"],
                       limitations=list(ex["limitations"]), source=src("noise"),
                       points_total=ex["coverage"]["points"])
            for s_ in ex["spread"]:
                row["figures"].append({"label": f"input-referred rms {s_['band']}, highest",
                                       "value": f"{s_['max_uv']} uV", "corner": s_["max_corner"]})
            if ex["floor"]:
                row["figures"].append({"label": "thermal floor range over the grid",
                                       "value": f"{ex['floor'][0]} .. {ex['floor'][1]} nV/rtHz", "corner": None})
                row["figures"].append({"label": "1/f corner range over the grid",
                                       "value": f"{ex['floor'][2]} .. {ex['floor'][3]} kHz", "corner": None})
        elif k == "offset" and "offset-mc" in extracted and extracted["offset-mc"].get("grid"):
            ex = extracted["offset-mc"]
            st, wa, wsg = ex["stats"], ex["worst"]["abs3"], ex["worst"]["sigma"]
            a3 = st[wa]
            row.update(status="measured-no-bound",
                       worst=f"|mean| + 3 sigma {a3['abs3']:.3f} mV (mean {a3['mean']:+.3f} mV, sigma {a3['sigma']:.3f} mV)",
                       worst_corner=fmt_point(*wa), coverage=ex["coverage"], limitations=list(ex["limitations"]),
                       source=src("offset-mc"), points_total=ex["coverage"]["points"])
            row["figures"].append({"label": "worst sigma over the grid",
                                   "value": f"{st[wsg]['sigma']:.3f} mV (3 sigma {st[wsg]['three_sigma']:.3f} mV)",
                                   "corner": fmt_point(*wsg)})
            sig = [st[q]["sigma"] for q in ex["order"]]
            row["figures"].append({"label": f"sigma range over the {len(sig)} points, N={ex['n_per_point']} each",
                                   "value": f"{min(sig):.3f} .. {max(sig):.3f} mV", "corner": None})
            row["figures"].append({"label": "largest |mean| (systematic plus sampling) over the grid",
                                   "value": f"{max(abs(st[q]['mean']) for q in ex['order']):.3f} mV", "corner": None})
            for q in ex["worst_tv"]:
                row["figures"].append({"label": f"worst |mean| + 3 sigma at {q[1]} C / {q[2]} V (over the MOS corners)",
                                       "value": f"{st[q]['abs3']:.3f} mV", "corner": fmt_point(*q)})
            rv = ex["retained"]
            row["figures"].append({"label": "per-point statistics re-derived from the retained samples",
                                   "value": f"{rv['samples']} samples at {rv['points']} points agree with the record "
                                            f"({rv['path']}, sha256 {rv['sha256'][:16]})", "corner": None})
        elif k == "offset" and "offset-mc" in extracted:
            ex = extracted["offset-mc"]
            c, sg, tsg = ex["worst_sigma"]
            row.update(status="measured-no-bound", worst=f"sigma {sg} mV (3 sigma {tsg} mV)",
                       worst_corner=f"{c} (corner only; {ex['coverage']['temps_c'][0]} C / {ex['coverage']['vdd_v'][0]} V)",
                       coverage=ex["coverage"], limitations=list(ex["limitations"]), source=src("offset-mc"),
                       points_total=ex["coverage"]["points"])
            row["figures"].append({"label": "worst |mean| + 3 sigma", "value": f"{ex['worst_abs'][1]} mV",
                                   "corner": ex["worst_abs"][0]})
            for s_ in ex["stats"]:
                row["figures"].append({"label": f"sigma, N={s_['n']}", "value": f"{s_['sigma_mv']} mV",
                                       "corner": s_["corner"]})
        elif k == "cmrr" and "cmrr" in extracted:
            ex = extracted["cmrr"]
            tbl = ex["figures"]["CMRR"]
            dc = tbl[0]
            mc = extracted.get(CMRR_MC_EXP)
            if sr["proposed"]:
                # the systematic (mismatch-free) record is NOT the proposed row's statistic: never
                # place its optimistic worst value beside the proposed bound
                proposed_row(row, sr, coverage=ex["coverage"], source=src("cmrr"), extra=[
                    "the figures listed for this row come from the selected systematic (mismatch-free) PVT "
                    "record and are "
                    "information only: they are NOT the statistic of the proposed row, which is stated on a "
                    "mismatch-inclusive basis (see the spec statistical-basis and status columns for the record "
                    "it cites); " + (
                        "the selected mismatch Monte Carlo record is listed separately below (27 C / 3.30 V only), and "
                        "the systematic figures are optimistic against it"
                        if mc else "this report does not ingest that record, and the systematic figures are "
                        "optimistic against it")] + list(ex["limitations"]))
                for t in tbl:
                    row["figures"].append({"label": f"systematic (mismatch-free) CMRR {t['figure']}, lowest "
                                                    "(information only, not the row's statistic)",
                                           "value": f"{t['worst_db']} dB", "corner": t["worst_corner"]})
                if mc:
                    cmrr_mc_row(row, mc, src(CMRR_MC_EXP), sources[CMRR_MC_EXP])
            else:
                if mc:
                    raise ReportError("CMRR: the spec row is no longer tagged 'proposed, not ratified', but this report "
                                      "has no grader for the mismatch-inclusive CMRR; extend the report to grade the "
                                      "ratified bound before regenerating it")
                row.update(status="measured-no-bound", worst=f"{dc['worst_db']} dB ({dc['figure']}, lowest)",
                           worst_corner=dc["worst_corner"], coverage=ex["coverage"],
                           limitations=list(ex["limitations"]), source=src("cmrr"),
                           points_total=ex["coverage"]["points"])
                for t in tbl:
                    row["figures"].append({"label": f"CMRR {t['figure']}, lowest", "value": f"{t['worst_db']} dB",
                                           "corner": t["worst_corner"]})
        elif k == "psrr" and "psrr" in extracted:
            ex = extracted["psrr"]
            dc = ex["figures"]["PSRR+"][0]
            info = " (information only, not graded)" if sr["proposed"] else ""
            pmc = extracted.get(PSRR_MC_EXP)
            if sr["proposed"]:
                proposed_row(row, sr, coverage=ex["coverage"], source=src("psrr"), extra=(
                    ["the figures listed first come from the selected systematic (mismatch-free) PVT record and are "
                     "information only; the selected mismatch Monte Carlo record is listed separately below "
                     "(27 C / 3.30 V only), and the systematic figures are optimistic against it"] if pmc else [])
                    + list(ex["limitations"]))
            else:
                if pmc:
                    raise ReportError("PSRR: the spec row is no longer tagged 'proposed, not ratified', but this report "
                                      "has no grader for the mismatch-inclusive PSRR; extend the report to grade "
                                      "the ratified bound before regenerating it")
                row.update(status="measured-no-bound", worst=f"PSRR+ {dc['worst_db']} dB ({dc['figure']}, lowest)",
                           worst_corner=dc["worst_corner"], coverage=ex["coverage"],
                           limitations=list(ex["limitations"]), source=src("psrr"),
                           points_total=ex["coverage"]["points"])
            for side in ("PSRR+", "PSRR-"):
                for t in ex["figures"][side]:
                    row["figures"].append({"label": f"{side} {t['figure']}, lowest{info}", "value": f"{t['worst_db']} dB",
                                           "corner": t["worst_corner"]})
            if pmc and sr["proposed"]:
                psrr_mc_row(row, pmc, src(PSRR_MC_EXP), sources[PSRR_MC_EXP])
        elif k == ICMR_ROW_KEY and ICMR_EXP in extracted:
            if not sr["proposed"]:
                raise ReportError(f"{sr['name']}: the spec row is no longer tagged 'proposed, not ratified', but this "
                                  "report has no grader for the input common-mode range; extend the report to grade "
                                  "the ratified bound before regenerating it")
            ex = extracted[ICMR_EXP]
            proposed_row(row, sr, coverage=ex["coverage"], source=src(ICMR_EXP), extra=[
                "the figures listed for this row come from the selected ICMR record, re-derived from its per-point "
                "tables and cross-checked against its retained per-sample evidence; they are information only and "
                "are not compared with the proposed bound"] + list(ex["limitations"]))
            row["figures"] = icmr_figures(ex)
            row["icmr"] = icmr_evidence(ex, sources[ICMR_EXP])
        elif sr["proposed"]:
            proposed_row(row, sr)
            if k == ICMR_ROW_KEY:
                row["limitations"].append(
                    "no ICMR record selected: this row carries no measurement (explicitly missing); the spec status "
                    "text is quoted verbatim and is not read as a measurement")
        else:
            row["limitations"] = ["no committed record in sim/ for this row"]
            row["spec_status"] = sr["status"]
        for skey, st in supplementary.items():
            if k in SUPPLEMENTARY[skey]["rows"]:
                row["limitations"].append(study_row_note(st, k))
        out_rows.append(row)

    if GAIN_MC_EXP in extracted and not gain_mc_shown:
        raise ReportError(f"gain-gbw-pm-mc: selected but not shown: the graded gain/GBW/PM rows need a selected "
                          "deterministic gain-gbw-pm record beside it")

    out_rows.append({
        "row": "Post-layout verification (extracted parasitics)", "key": "post-layout",
        "spec_bound": "n/a (not a Sec. 2 table row)", "bound_open": True, "status": "not-measured",
        "verdict": None, "points_pass": None, "points_total": None, "worst": None, "worst_corner": None,
        "figures": [], "coverage": None, "source": None,
        "limitations": ["no committed record: no layout / extraction evidence exists in sim/"],
    })

    verdicts = [r["verdict"] for r in out_rows if r["verdict"]]
    extra = {"archival": True} if archival else {}  # key absent in current-design reports (bytes unchanged)
    if "offset-mc" in extracted:  # issue #184: derived, hashed, never graded
        extra["offset_drift"] = offset_drift_view(extracted["offset-mc"], sources["offset-mc"])
    return {**extra,
        "schema": SCHEMA,
        "title": ("gf180-opamp characterization report (ARCHIVAL: not current-design signoff evidence)"
                  if archival else "gf180-opamp characterization report (generated; not evidence)"),
        "dut": {"netlist": "design/netlist/opamp_two_stage.spice", "normalised_sha256_prefix": dut_prefix,
                "normalised_sha256": dut_full},
        "spec": {"path": spec_rel, "sha256": sha256_file(root / spec_rel)},
        "manifest_selection": {e: exps.get(e) for e in EXPERIMENTS},
        "summary": {"ratified_rows_judged": len(verdicts), "pass": verdicts.count("PASS"),
                    "fail": verdicts.count("FAIL"),
                    "not_measured": sum(1 for r in out_rows if r["status"] == "not-measured"),
                    "measured_no_bound": sum(1 for r in out_rows if r["status"] == "measured-no-bound"),
                    "proposed_not_graded": sum(1 for r in out_rows if r["status"] == "proposed-not-graded")},
        "rows": out_rows,
        "sources": {e: {k: v for k, v in s.items() if k != "dut_prefix"} for e, s in sorted(sources.items())},
        "supplementary": supplementary,
        "limitations": glob_lim,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def link_from_reports(path: str) -> str:
    return os.path.relpath(path, OUT_DIR_REL).replace(os.sep, "/")


def mc_cell(m: dict) -> str:
    if m["status"] == "unknown":
        return "unknown"
    return f"{m['status']} (`{m['fingerprint'][:16]}`)"


def sc_link(src: dict) -> str:
    return f"[`{src['record_id']}`]({link_from_reports(src['path'])})"


def render_psrr_study(a, st: dict) -> None:
    """Markdown body of the bound-free PSRR study (issue #173): measured, ungraded."""
    rv, pop = st["retained_evidence"], st["population"]
    pa = rv["partitions"]
    a(f"- **Grading**: {st['grading']}")
    a(f"- **Retained evidence (figures re-derived from it)**: [`{rv['path']}`]({link_from_reports(rv['path'])}): "
      f"{rv['psrr_dat_files']} `<cell>.psrr.dat` curves, aggregate sha256 `{rv['sha256']}` ({rv['sha256_method']}); "
      f"[`{pa['path']}`]({link_from_reports(pa['path'])}) sha256 `{pa['sha256']}`: "
      + ", ".join(f"`{m}` {n} cells" for m, n in pa["excitations"].items())
      + f" (each the exact {pop['cells']}-cell union, no overlap)")
    a(f"- **Population**: {pop['cells']} cells ({pop['typical_passive_cells']} typical-passive, "
      f"{pop['non_typical_cells']} non-typical), each paired from the {', '.join(pop['excitations'])} excitations")
    a("")
    a(f"| figure (information only, no verdict) | worst (dB) | binding cell (MOS / T / VDD / RZ / CC) | best (dB) | "
      f"best cell | worst over the {pop['typical_passive_cells']} typical-passive cells (dB) | passive shift of the "
      f"worst case (dB) | most negative delta vs typical (dB) | at cell | most positive delta vs typical (dB) | "
      f"at cell | max abs delta (dB) |")
    a("|---|---|---|---|---|---|---|---|---|---|---|---|")
    cell = lambda c: f"{c['point']} / RZ {c['rz']} / CC {c['cc']}"
    for f in st["figures"].values():
        d, t = f["delta_vs_typical"], f["typical_passive_worst"]
        a(f"| {f['label']} | {f['worst']['value']} | {cell(f['worst'])} | {f['best']['value']} | {cell(f['best'])} | "
          f"{t['value']} | {f['passive_shift_of_worst']} | {d['most_negative']['value']} | {cell(d['most_negative'])} | "
          f"{d['most_positive']['value']} | {cell(d['most_positive'])} | {d['max_abs']} |")
    a("")
    prim = [f for f in st["figures"].values() if "primary_record_worst" in f["typical_passive_worst"]]
    if prim:
        a("- Typical-passive worst values agree with the selected 45-point PSRR record: "
          + "; ".join(f"{f['label']} {f['typical_passive_worst']['value']} dB vs "
                      f"{f['typical_passive_worst']['primary_record_worst']} dB" for f in prim))


def render_offset_drift(a, od: dict) -> None:
    """Issue #184: derived offset temperature drift and systematic / statistical split."""
    a("")
    a("## Offset temperature drift and systematic-vs-statistical split (derived; not graded)")
    a("")
    if not od["available"]:
        a(f"Not available: {od['reason']}.")
        return
    a(f"Derived offline from the retained per-sample offsets of the selected offset Monte Carlo record "
      f"([`{od['record']}`]({link_from_reports(od['record'])}), sha256 `{od['record_sha256']}`; samples "
      f"[`{od['input']}`]({link_from_reports(od['input'])}), sha256 `{od['input_sha256']}`). No simulation was run, "
      f"no verdict is given and no bound is proposed. Offsets in uV, drifts in uV/C; N={od['n_per_point']} per point; "
      f"every mean carries its standard error sigma/sqrt(N). A drift is *resolved* only if |drift| > "
      f"{od['resolution_threshold_se']:g} standard errors, otherwise *unresolved* (noise, not a measured drift).")
    temps = od["temps_c"]
    a("")
    a("### Mean offset and drift per process corner and supply")
    a("")
    a("| Process | VDD (V) | " + " | ".join(f"mean at {t:g} C (uV)" for t in temps)
      + " | End-to-end drift (uV/C) | Best-fit drift (uV/C) | Segment drifts (uV/C) |")
    a("|---|---|" + "---|" * len(temps) + "---|---|---|")

    def dcell(d):
        if d is None:
            return "n/a"
        return f"{d['uv_per_c']:+.3f} +/- {d['se_uv_per_c']:.3f} ({'resolved' if d['resolved'] else 'unresolved'})"
    for g in od["groups"]:
        a(f"| {g['process']} | {g['vdd_v']} | "
          + " | ".join(f"{p['mean_uv']:+.1f} +/- {p['se_mean_uv']:.1f}" for p in g["points"])
          + f" | {dcell(g['drift_end_to_end'])} | {dcell(g['drift_fit'])} | "
          + ("; ".join(f"{s_['from_c']:g}..{s_['to_c']:g} C: {dcell(s_)}" for s_ in g["segments"]) or "n/a") + " |")
    a("")
    a("### Sigma versus temperature")
    a("")
    a("| Process | VDD (V) | " + " | ".join(f"sigma at {t:g} C (uV)" for t in temps) + " | max/min |")
    a("|---|---|" + "---|" * len(temps) + "---|")
    for g in od["groups"]:
        r = g["sigma_vs_t"]["max_over_min"]
        a(f"| {g['process']} | {g['vdd_v']} | " + " | ".join(f"{p['sigma_uv']:.1f}" for p in g["points"])
          + f" | {'n/a' if r is None else f'{r:.3f}'} |")
    a("")
    a("### Worst |mean| + 3 sigma per process corner and supply, split")
    a("")
    a("| Process | VDD (V) | Temperature (C) | \\|mean\\| systematic (uV) | 3 sigma statistical (uV) | \\|mean\\| + 3 sigma (uV) | Systematic share |")
    a("|---|---|---|---|---|---|---|")
    for g in od["groups"]:
        w = g["worst_point"]
        sh = w["systematic_fraction"]
        a(f"| {g['process']} | {g['vdd_v']} | {w['temp_c']:g} | {w['abs_mean_uv']:.1f} | {w['three_sigma_uv']:.1f} | "
          f"{w['abs3_uv']:.1f} | {'n/a' if sh is None else f'{sh * 100:.1f} %'} |")
    w = od["worst_overall"]
    a("")
    a(f"Worst point of the grid: {w['process']} / {w['temp_c']:g} C / {w['vdd_v']} V, |mean| + 3 sigma = "
      f"{w['abs3_uv']:.1f} uV = {w['abs_mean_uv']:.1f} uV systematic (|mean|) + {w['three_sigma_uv']:.1f} uV statistical "
      f"(3 sigma); mismatch-seed independence across temperatures: {od['seed_independence']}.")
    a("")
    for l in od["limitations"]:
        a(f"- Limitation: {l}")


def render_md(rep: dict) -> str:
    L = []
    a = L.append
    a(f"# {rep['title']}")
    a("")
    a("Generated by `sim/report/characterization_report.py` from the committed records listed under Sources "
      "(selection: `sim/report/selection.json`). Do not edit by hand; regenerate and compare with "
      "`python3 sim/report/characterization_report.py --check`. The records, not this file, are the evidence; "
      "no simulator is run to produce it.")
    a("")
    d = rep["dut"]
    a(f"- **DUT**: `{d['netlist']}` (normalised sha256 prefix `{d['normalised_sha256_prefix']}`"
      + (f", full `{d['normalised_sha256']}`" if d["normalised_sha256"] else "") + ")")
    a(f"- **Spec**: `{rep['spec']['path']}` (sha256 `{rep['spec']['sha256']}`)")
    s = rep["summary"]
    a(f"- **Ratified rows judged**: {s['ratified_rows_judged']} ({s['pass']} PASS, {s['fail']} FAIL); "
      f"{s['measured_no_bound']} measured with the bound open (no verdict); {s['not_measured']} not measured"
      + (f"; {s['proposed_not_graded']} proposed, not ratified (not graded)" if s["proposed_not_graded"] else ""))
    a("")
    a("## Spec rows (spec/target-spec.md Sec. 2)")
    a("")
    a("| Row | Bound | Status | Verdict | Points | Worst value | Binding / worst corner | Source |")
    a("|---|---|---|---|---|---|---|---|")
    for r in rep["rows"]:
        pts = (f"{r['points_pass']}/{r['points_total']}" if r["points_pass"] is not None
               else (str(r["points_total"]) if r["points_total"] else "-"))
        verdict = f"**{r['verdict']}**" if r["verdict"] else "none"
        bound = "open" if r["bound_open"] and r["status"] != "not-measured" else r["spec_bound"]
        if r["bound_open"]:
            bound = "open (no ratified bound)" if r["key"] != "post-layout" else r["spec_bound"]
        sc = f"[`{r['source']['record_id']}`]({link_from_reports(r['source']['path'])})" if r["source"] else "no record"
        if r["status"] == "proposed-not-graded":
            bound = r["spec_bound"]  # carries the "proposed, not ratified: " qualifier
            sc = "not graded (see spec status)"
            if r.get("icmr"):  # source-pinned measurement, still not graded
                sc = f"not graded; measured in {sc_link(r['source'])}"
        cell = lambda s: (s or "-").replace("|", "\\|")  # e.g. the offset row's "|mean| + 3 sigma"
        a(f"| {r['row']} | {bound} | {r['status']} | {verdict} | {pts} | {cell(r['worst'])} | "
          f"{cell(r['worst_corner'])} | {sc} |")
    a("")
    a("## Row details")
    for r in rep["rows"]:
        a("")
        a(f"### {r['row']}")
        a("")
        a(f"- **Status**: {r['status']}" + (f"; verdict **{r['verdict']}**" if r["verdict"] else "; no verdict issued"))
        if r.get("spec_status"):
            a(f"- **Spec status column (verbatim text, not a measurement)**: {r['spec_status']}")
        if r["coverage"]:
            a(f"- **Coverage**: {coverage_text(r['coverage'])}")
        if r["source"]:
            a(f"- **Source**: [`{r['source']['path']}`]({link_from_reports(r['source']['path'])}), "
              f"sha256 `{r['source']['sha256']}`")
        if r.get("icmr"):
            pv = r["icmr"]["provenance"]
            a(f"- **Measured evidence (not graded)**: DUT normalised sha256 `{pv['dut_sha256'][:16]}`; PDK open_pdks "
              f"`{(pv['pdk_open_pdks'] or 'n/a')[:12]}`; ngspice local {pv['ngspice_local'] or 'n/a'} / klt engine "
              f"{pv['ngspice_engine'] or 'n/a'}; klt client {pv['klt_client'] or 'n/a'}; backend "
              f"{pv['backend'] or 'n/a'}; measurement-configuration freshness {mc_cell(pv['measurement_config'])}")
            rv = r["icmr"]["retained_evidence"]
            a(f"- **Retained per-sample evidence (cross-checked)**: [`{rv['path']}`]({link_from_reports(rv['path'])}), "
              f"sha256 `{rv['sha256']}` ({rv['samples']} samples at {rv['points']} PVT points)")
            if r["icmr"]["addendum"]:
                ad = r["icmr"]["addendum"]
                a(f"- **Record addendum**: [`{ad['path']}`]({link_from_reports(ad['path'])}), sha256 `{ad['sha256']}`")
        for f in r["figures"]:
            a(f"- {f['label']}: {f['value']}" + (f" ({f['corner']})" if f["corner"] else ""))
        for l in r["limitations"]:
            for st in rep.get("supplementary", {}).values():
                l = l.replace(f"`{st['path']}`", f"[`{st['path']}`]({link_from_reports(st['path'])})")
            a(f"- Limitation: {l}")
    if rep.get("offset_drift"):
        render_offset_drift(a, rep["offset_drift"])
    if rep.get("supplementary"):
        a("")
        a("## Supplementary passive-corner studies (measured side evidence; not graded)")
        a("")
        a("Selected explicitly under `supplementary` in `sim/report/selection.json`, separately from the records "
          "that grade the spec rows. Every figure below is re-derived from the study's own RZ x CC tables and "
          "checked against its verdict header (a bound-free study: re-derived from its retained per-cell curves and "
          "checked against its rounded tables, with no verdict at all); none is counted in a row's points or verdict "
          "or in the ratified-row summary above.")
        for k, st in rep["supplementary"].items():
            a("")
            a(f"### {k}")
            a("")
            a(f"- **Source**: [`{st['path']}`]({link_from_reports(st['path'])}), sha256 `{st['sha256']}`")
            a(f"- **Scope**: {st['scope']['text']}")
            dc = st["dut_check"]
            a(f"- **Provenance**: DUT normalised sha256 `{st['dut_sha256'][:16]}` ("
              + ("matches the selected records" if dc["matches_selected_records"] else "no selected record to compare")
              + "; " + ("matches the current netlist" if dc["matches_current_netlist"]
                        else "current netlist not checked (archival)")
              + f"); PDK open_pdks `{(st['pdk_open_pdks'] or 'n/a')[:12]}`; ngspice local {st['ngspice_local'] or 'n/a'} / "
              f"klt engine {st['ngspice_engine'] or 'n/a'}; klt client {st['klt_client'] or 'n/a'}; backend "
              f"{st['backend'] or 'n/a'}; measurement-configuration freshness {mc_cell(st['measurement_config'])}")
            a(f"- **Rows it informs (not graded)**: {', '.join(st['rows'])}")
            if st.get("bound_free"):
                render_psrr_study(a, st)
            for fk, f in ([] if st.get("bound_free") else st["figures"].items()):
                a(f"- {study_observation(f)} (record verdict {f['verdict_in_record']}; information only, not graded)")
            for fk, why in sorted(st["not_measured"].items()):
                a(f"- {fk}: {why}")
            for l in st["limitations"]:
                a(f"- Limitation: {l}")
    a("")
    a("## Sources")
    a("")
    a("| Experiment | Record | sha256 | DUT prefix | PDK (open_pdks) | ngspice local / klt engine | klt client | Backend | Measurement config |")
    a("|---|---|---|---|---|---|---|---|---|")
    for e, sr in rep["sources"].items():
        pdk = (sr["pdk_open_pdks"] or "n/a")[:12]
        if sr["pdk_client_resolved"] and sr["pdk_client_resolved"] != sr["pdk_open_pdks"]:
            pdk += f" (client-resolved {sr['pdk_client_resolved'][:12]})"
        a(f"| {e} | [`{sr['record_id']}`]({link_from_reports(sr['path'])}) | `{sr['sha256']}` | "
          f"`{sr['dut_sha256'][:16]}` | `{pdk}` | {sr['ngspice_local'] or 'n/a'} / {sr['ngspice_engine'] or 'n/a'} | "
          f"{sr['klt_client'] or 'n/a'} | {sr['backend'] or 'n/a'} | {mc_cell(sr['measurement_config'])} |")
    a("")
    a("## Report-level limitations")
    a("")
    for l in rep["limitations"] or ["none"]:
        a(f"- {l}")
    for w in rep["warnings"]:
        a(f"- WARNING: {w}")
    a("")
    return "\n".join(L)


def render_json(rep: dict) -> str:
    return json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def report_hash(report_json: str) -> str:
    """sha256 of the exact UTF-8 bytes of the report JSON (final newline included)."""
    return "sha256:" + hashlib.sha256(report_json.encode("utf-8")).hexdigest()


def render_evidence(report_json: str) -> str:
    """Generic evidence wrapper (klt signoff item 8) for the report JSON.

    ``status: pass`` asserts only that the aggregated report was produced and
    verified against its selected records; it does NOT assert that every spec
    row passes.  Per-row verdicts and limitations live in the report itself.
    Only called after build() succeeded, so a generator error never yields a
    passing wrapper.
    """
    rep = json.loads(report_json)
    ev = {
        "kind": "generic",
        "status": "pass",
        "schema_version": 1,
        "subject": "characterization-report",
        "claim": "The aggregated characterization report was generated from its selected committed "
                 "records and verified; this is not a claim that every spec row passes.",
        "report": {"path": f"{OUT_DIR_REL}/{OUT_NAME}.json", "markdown": f"{OUT_DIR_REL}/{OUT_NAME}.md"},
        "report_summary": rep["summary"],
        # issue #173: the hashed side evidence the report attaches to its rows; never graded, never
        # part of report_summary (a bound-free study carries no verdict at all)
        "report_supplementary": {
            k: {"graded": False, "bound_free": bool(st.get("bound_free")), "rows": st["rows"], "path": st["path"],
                "sha256": st["sha256"], "cells": st["scope"]["cells"],
                **({"verdict": None, "retained_evidence_sha256": st["retained_evidence"]["sha256"],
                    "worst_db": {f["label"]: f["worst"]["value"] for f in st["figures"].values()}}
                   if st.get("bound_free") else {})}
            for k, st in rep.get("supplementary", {}).items()},
        # issue #184: derived (not simulated) view of committed evidence; never graded
        **({"report_derived": {"offset_temperature_drift": {
            "graded": False, "verdict": None, "available": rep["offset_drift"]["available"],
            **({"record_sha256": rep["offset_drift"]["record_sha256"], "input": rep["offset_drift"]["input"],
                "input_sha256": rep["offset_drift"]["input_sha256"]} if rep["offset_drift"]["available"] else {})}}}
           if rep.get("offset_drift") else {}),
        "provenance": {"input": {"path": f"{OUT_DIR_REL}/{OUT_NAME}.json",
                                 "content_hash": report_hash(report_json)}},
    }
    return json.dumps(ev, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def generate(root: Path, manifest_path: Path, spec_rel: str = "spec/target-spec.md", archival: bool = False) -> tuple:
    rep = build(root, load_manifest(manifest_path), spec_rel, archival)
    return render_md(rep), render_json(rep)


def publish(files: dict) -> None:
    """Replace each destination with its candidate content, staging first.

    Every file is written to a temporary sibling before any destination is
    touched, so a staging failure leaves all published files unchanged.  The
    final renames are individually atomic but not a crash-atomic transaction.
    """
    staged = []
    try:
        for dest, content in files.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
            staged.append((Path(tmp), dest))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            if dest.exists():
                os.chmod(tmp, dest.stat().st_mode & 0o7777)
            else:
                os.chmod(tmp, 0o644)
        for tmp, dest in staged:
            os.replace(tmp, dest)
        staged = []
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="selection manifest (default: sim/report/selection.json)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: derived from this file)")
    ap.add_argument("--out-dir", type=Path, default=None, help=f"output directory (default: <root>/{OUT_DIR_REL})")
    ap.add_argument("--check", action="store_true", help="regenerate in memory and compare with the committed output; write nothing")
    ap.add_argument("--latest", action="store_true", help="select the newest record of every experiment (used by characterize.sh)")
    ap.add_argument("--update-manifest", action="store_true", help="with --latest: publish the selection to --manifest, only after the candidate report validated and rendered")
    ap.add_argument("--stdout", action="store_true", help="print the Markdown report instead of writing files")
    ap.add_argument("--archival", action="store_true",
                    help="historical report: skip the current-DUT gate. Needs --stdout or an explicit --out-dir, "
                         "writes no evidence sidecar, and cannot be combined with --check or --latest")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    if a.archival:
        if a.check or a.latest or not (a.stdout or a.out_dir):
            print("characterization_report: error: --archival needs --stdout or --out-dir and cannot be "
                  "combined with --check/--latest (it cannot supply current-design signoff evidence)",
                  file=sys.stderr)
            return 2
        if a.out_dir and a.out_dir.resolve() == (root / OUT_DIR_REL).resolve():
            print(f"characterization_report: error: --archival must not write to {OUT_DIR_REL}", file=sys.stderr)
            return 2
    out_dir = (a.out_dir or root / OUT_DIR_REL)
    # Everything is validated and rendered in memory first; nothing is published
    # (selection manifest included) until the whole candidate bundle exists.
    manifest_text = None
    try:
        if a.latest:
            m = {"experiments": latest_selection(root)}
            # side studies are never picked by recency (issue #121): keep the explicit choice
            if a.manifest.is_file():
                sup = load_manifest(a.manifest).get("supplementary")
                if sup:
                    m["supplementary"] = sup
            rep = build(root, m)
            md, js = render_md(rep), render_json(rep)
            if a.update_manifest and not a.check:
                manifest_text = json.dumps(m, indent=2, sort_keys=True) + "\n"
        else:
            md, js = generate(root, a.manifest, archival=a.archival)
        evidence = None if a.archival else render_evidence(js)
    except ReportError as e:
        print(f"characterization_report: error: {e}", file=sys.stderr)
        return 2
    files = {}
    if manifest_text is not None:
        files[a.manifest] = manifest_text
    if a.stdout:
        publish(files)
        sys.stdout.write(md)
        return 0
    targets = {out_dir / f"{OUT_NAME}.md": md, out_dir / f"{OUT_NAME}.json": js}
    if evidence is not None:
        targets[out_dir / f"{OUT_NAME}.evidence.json"] = evidence
    if a.check:
        bad = [str(p) for p, c in targets.items() if not p.is_file() or p.read_bytes() != c.encode("utf-8")]
        if bad:
            print("characterization_report: --check: committed output is stale or missing: " + ", ".join(bad) +
                  "\n  regenerate with: python3 sim/report/characterization_report.py", file=sys.stderr)
            return 1
        print("characterization_report: --check: committed report is up to date")
        return 0
    files.update(targets)
    publish(files)
    for p in targets:
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
