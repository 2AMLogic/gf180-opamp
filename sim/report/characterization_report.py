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

Existing records are Markdown only (no structured sidecars), so this module
carries a narrow, tested extraction of the verdict / worst-case lines and
records the sha256 of every source file it read.

Exit codes: 0 ok, 1 --check found a difference, 2 selection / record error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = 1
HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent
DEFAULT_MANIFEST = HERE / "selection.json"
OUT_DIR_REL = "sim/reports"
OUT_NAME = "characterization-report"
DUT_REL = "design/netlist/opamp_two_stage.spice"

EXPERIMENTS = ["gain-gbw-pm", "offset-mc", "noise", "cmrr", "psrr", "slew-swing-power", "input-common-mode"]
#: Experiments whose rows may come from different records: a record may judge
#: only a subset of the rows (e.g. a single-figure re-run), so the manifest
#: entry may be an object {row: record path}, naming the record each row is
#: taken from (rows a record also judged but is not selected for are ignored).
MULTI_RECORD = {"slew-swing-power": ("power", "slew", "swing")}
#: Side-study records that live beside an experiment's grid records but never
#: judge its spec rows (matched on the record's title line), so `--latest`
#: must not select them: the gain-gbw-pm RZ x CC passive-corner study
#: (`run_gain_gbw_pm.py --passive-corners`, issue #70) and the slew/swing/power
#: one (`run_slew_swing_power.py --passive-corners`, issue #97).
STUDY_TITLES = {
    "gain-gbw-pm": ("# gain/GBW/PM passive-corner study",),
    "slew-swing-power": ("# slew/swing/power passive-corner study",),  # issue #97
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
    return (f"{len(c['corners'])} MOS corners ({', '.join(c['corners'])}) x "
            f"{len(c['temps_c'])} T ({', '.join(str(t) for t in c['temps_c'])} C) x "
            f"{len(c['vdd_v'])} VDD ({', '.join(c['vdd_v'])} V) = {c['points']} points")


# --------------------------------------------------------------------------
# per-experiment extraction
# --------------------------------------------------------------------------
#: Cited (never selected) passive-corner side study for gain/GBW/PM (issue #70).
PASSIVE_STUDY_ID = "20261009-234014-55b400c"
PASSIVE_STUDY_REL = f"sim/gain-gbw-pm/records/{PASSIVE_STUDY_ID}.md"
#: The JSON keeps the repo-root path as plain text; render_md() turns it into a
#: link relative to the report directory (like every other source link).
PASSIVE_STUDY_NOTE = {
    "gain": "the separate 27-cell RZ x CC passive-corner study (cited side study, not a selected verdict record) "
            f"`{PASSIVE_STUDY_REL}` does not issue a gain verdict",
    "gbw": "the separate 27-cell RZ x CC passive-corner study (cited side study, not a selected verdict record) "
           f"`{PASSIVE_STUDY_REL}` finds GBW PASS at 24/27 cells and FAIL at 3/27 cells, all at "
           "ss / 125 C / 2.97 V with CC worst (one MOS/T/VDD point, RZ typical/best/worst = 9.663/9.656/9.673 MHz); "
           "worst 9.656 MHz is below the 10 MHz bound",
    "pm": "the separate 27-cell RZ x CC passive-corner study (cited side study, not a selected verdict record) "
          f"`{PASSIVE_STUDY_REL}` finds PM >= 60 deg at only 7/27 cells (53.63 .. 63.17 deg)",
}


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
        rows[key]["bound_value"] = bound
    stretch = re.search(r"open-loop DC gain >= 70 dB holds at (\d+)/(\d+)", text)
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


def extract_offset(text: str, label: str) -> dict:
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
        lim.append("systematic figure of a perfectly matched schematic; mismatch-limited CMRR (Monte Carlo) is not covered")
    lim.append("no ratified bound (DR-3 residual e3)")
    return {"prov": prov, "coverage": cov, "figures": {"CMRR": _worst_table(text, "Worst case over", label)},
            "limitations": lim}


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


EXTRACTORS = {"gain-gbw-pm": extract_gain, "offset-mc": extract_offset, "noise": extract_noise,
              "cmrr": extract_cmrr, "psrr": extract_psrr, "slew-swing-power": extract_ssp,
              ICMR_EXP: extract_icmr}


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
    "slew-swing-power": "sim/slew-swing-power/measurement_config.py",
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
        glob_lim.append("coverage differs between sources (see each row's coverage; e.g. the offset Monte Carlo is "
                        "5 corner points at nominal T/VDD while the AC rows are full 45-point grids)")
    for exp in EXPERIMENTS:
        if not selected_records(exp, exps.get(exp)):
            glob_lim.append(f"no record selected for {exp}: " + (
                "the proposed input common-mode range row carries no measurement (explicitly missing; "
                "nothing is inferred from the spec status text)" if exp == ICMR_EXP
                else "its rows are reported as not measured"))

    spec_rows = parse_spec(root / spec_rel)
    spec_by_key = {r["key"]: r for r in spec_rows}
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
            row["limitations"].append(PASSIVE_STUDY_NOTE[k])
            if k == "gain" and ex["stretch_gain_70db"]:
                row["figures"].append({"label": "stretch >= 70 dB (not a mandatory row), points holding",
                                       "value": ex["stretch_gain_70db"], "corner": None})
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
            if sr["proposed"]:
                # the systematic (mismatch-free) record is NOT the proposed row's statistic: never
                # place its optimistic worst value beside the proposed bound
                proposed_row(row, sr, coverage=ex["coverage"], source=src("cmrr"), extra=[
                    "the figures listed for this row come from the selected systematic (mismatch-free) PVT "
                    "record and are "
                    "information only: they are NOT the statistic of the proposed row, which is stated on a "
                    "mismatch-inclusive basis (see the spec statistical-basis and status columns for the record "
                    "it cites); this report does not ingest that record, and the systematic figures are "
                    "optimistic against it"] + list(ex["limitations"]))
                for t in tbl:
                    row["figures"].append({"label": f"systematic (mismatch-free) CMRR {t['figure']}, lowest "
                                                    "(information only, not the row's statistic)",
                                           "value": f"{t['worst_db']} dB", "corner": t["worst_corner"]})
            else:
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
            if sr["proposed"]:
                proposed_row(row, sr, coverage=ex["coverage"], source=src("psrr"), extra=list(ex["limitations"]))
            else:
                row.update(status="measured-no-bound", worst=f"PSRR+ {dc['worst_db']} dB ({dc['figure']}, lowest)",
                           worst_corner=dc["worst_corner"], coverage=ex["coverage"],
                           limitations=list(ex["limitations"]), source=src("psrr"),
                           points_total=ex["coverage"]["points"])
            for side in ("PSRR+", "PSRR-"):
                for t in ex["figures"][side]:
                    row["figures"].append({"label": f"{side} {t['figure']}, lowest{info}", "value": f"{t['worst_db']} dB",
                                           "corner": t["worst_corner"]})
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
        out_rows.append(row)

    out_rows.append({
        "row": "Post-layout verification (extracted parasitics)", "key": "post-layout",
        "spec_bound": "n/a (not a Sec. 2 table row)", "bound_open": True, "status": "not-measured",
        "verdict": None, "points_pass": None, "points_total": None, "worst": None, "worst_corner": None,
        "figures": [], "coverage": None, "source": None,
        "limitations": ["no committed record: no layout / extraction evidence exists in sim/"],
    })

    verdicts = [r["verdict"] for r in out_rows if r["verdict"]]
    extra = {"archival": True} if archival else {}  # key absent in current-design reports (bytes unchanged)
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
        a(f"| {r['row']} | {bound} | {r['status']} | {verdict} | {pts} | {r['worst'] or '-'} | "
          f"{r['worst_corner'] or '-'} | {sc} |")
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
            l = l.replace(f"`{PASSIVE_STUDY_REL}`",
                          f"[`{PASSIVE_STUDY_REL}`]({link_from_reports(PASSIVE_STUDY_REL)})")
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="selection manifest (default: sim/report/selection.json)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: derived from this file)")
    ap.add_argument("--out-dir", type=Path, default=None, help=f"output directory (default: <root>/{OUT_DIR_REL})")
    ap.add_argument("--check", action="store_true", help="regenerate in memory and compare with the committed output; write nothing")
    ap.add_argument("--latest", action="store_true", help="select the newest record of every experiment (used by characterize.sh)")
    ap.add_argument("--update-manifest", action="store_true", help="with --latest: write the selection to --manifest first")
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
    try:
        if a.latest:
            m = {"experiments": latest_selection(root)}
            if a.update_manifest:
                a.manifest.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
            rep = build(root, m)
            md, js = render_md(rep), render_json(rep)
        else:
            md, js = generate(root, a.manifest, archival=a.archival)
    except ReportError as e:
        print(f"characterization_report: error: {e}", file=sys.stderr)
        return 2
    if a.stdout:
        sys.stdout.write(md)
        return 0
    targets = {out_dir / f"{OUT_NAME}.md": md, out_dir / f"{OUT_NAME}.json": js}
    if not a.archival:
        targets[out_dir / f"{OUT_NAME}.evidence.json"] = render_evidence(js)
    if a.check:
        bad = [str(p) for p, c in targets.items() if not p.is_file() or p.read_bytes() != c.encode("utf-8")]
        if bad:
            print("characterization_report: --check: committed output is stale or missing: " + ", ".join(bad) +
                  "\n  regenerate with: python3 sim/report/characterization_report.py", file=sys.stderr)
            return 1
        print("characterization_report: --check: committed report is up to date")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for p, c in targets.items():
        p.write_text(c)
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
