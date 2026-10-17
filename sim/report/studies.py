"""Split out of characterization_report.py (issue #219); moved verbatim, no logic changes."""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
from pathlib import Path
from report_common import (
    CORNER_ORDER, ReportError, STUDY_TITLES, corner_sort_key, fmt_point, header_of, parse_fingerprint,
    parse_provenance, section, sha256_file, split_cells)
from extract_icmr import (
    _PT)


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
    # issue #198: the follower step-response record. BOUND-FREE with NO rows at all: settling and
    # overshoot have no spec row (not even a proposed one), so there is nothing to attach the
    # figures to -- they are shown as an ungraded section of their own. Every figure is re-derived
    # offline from the retained per-point `.dat` transients and checked against the record's
    # rounded tables; not-settled results stay explicit (a named status, never zero).
    "step-response-follower": {"experiment": "step-response", "title": "# Record ", "rows": (),
                               "bound_free": True},
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


# --------------------------------------------------------------------------
# supplementary follower step-response study (issue #198): measured, never graded
# --------------------------------------------------------------------------
#: Validity thresholds of the step extraction (NOT fingerprinted, per the step
#: measurement_config docstring): the follower must actually follow. Mirrors
#: `sim/step-response/run_step_response.py` exactly.
_STEP_LEVEL_TOL_V = 0.02     # |vout - vinp| at the pre-edge instant and over the reference window
_STEP_STEP_REL_TOL = 0.2     # realised step must be STEP_V within +-20 %
_STEP_MIN_EDGE_SAMPLES = 50
_STEP_EDGES = (("rise", "rise_t", "fall_t", +1), ("fall", "fall_t", None, -1))
#: Aggregate retained-evidence hash method (same convention as the PSRR study).
_STEP_DAT_METHOD = ("sha256 of the sha256sum-format listing (hex digest, two spaces, file name; one line "
                    "per point, in study order) of every per-point .dat transient")
_STEP_CACHE: dict = {}  # (rid, record sha256, evidence sha256) -> extract result (treat as read-only)


def _step_read_dat(path: Path, label: str, rel: str) -> tuple:
    """(time, vout, vinp) of one retained per-point `.dat`; 3 finite columns. Structural
    validity (sample count, monotonic time, run length) is the runner's per-point INVALID
    rule and is judged by the caller, so an INVALID point (even a header-only file) is
    counted as invalid rather than rejected; a malformed or non-finite line is an error."""
    if not path.is_file():
        raise ReportError(f"{label}: retained per-point transient is missing: {rel}")
    cols: list = [[], [], []]
    for ln in path.read_text().splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        c = ln.split()
        if len(c) != 3:
            raise ReportError(f"{label}: {rel}: expected 3 columns (time_s, vout_v, vinp_v), got {len(c)}")
        try:
            r = [float(x) for x in c]
        except ValueError:
            raise ReportError(f"{label}: {rel}: malformed line '{ln.strip()}'") from None
        if not all(math.isfinite(x) for x in r):
            raise ReportError(f"{label}: {rel}: nonfinite value at {r[0]:.3g} s")
        for acc, x in zip(cols, r):
            acc.append(x)
    return cols


def _step_interp(t: list, v: list, tt: float) -> float:
    """Linear interpolation at `tt` (clamped at the ends), `np.interp`'s rule."""
    if tt <= t[0]:
        return v[0]
    if tt >= t[-1]:
        return v[-1]
    lo, hi = 0, len(t) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if t[mid] <= tt:
            lo = mid
        else:
            hi = mid
    return v[lo] + (v[lo + 1] - v[lo]) * (tt - t[lo]) / (t[lo + 1] - t[lo])


def _step_window_mean(t: list, v: list, t0: float, t1: float) -> float:
    """Time average of the piecewise-linear waveform over [t0, t1] (trapezoid, end points
    interpolated) -- the runner's `window_mean`, in plain Python."""
    tt = [t0] + [x for x in t if t0 < x < t1] + [t1]
    vv = [_step_interp(t, v, x) for x in tt]
    return sum((vv[i + 1] + vv[i]) * (tt[i + 1] - tt[i]) for i in range(len(tt) - 1)) / 2 / (t1 - t0)


def _step_settling(t: list, err: list, band: float, t_edge: float, ref_start: float) -> float:
    """Time from `t_edge` to the last exit of |err| from `band`, interpolated; `math.inf`
    ("not settled") when the output is still outside the band inside the reference window."""
    last = -1
    for i, e in enumerate(err):
        if abs(e) > band:
            last = i
    if last < 0:
        return 0.0
    if t[last] >= ref_start or last + 1 >= len(t):
        return math.inf
    a, b = abs(err[last]), abs(err[last + 1])
    f = (a - band) / (a - b) if a != b else 0.0
    return t[last] + f * (t[last + 1] - t[last]) - t_edge


def _step_edge(t: list, vo: list, vi: list, t_edge: float, t_next, direction: int,
               p: dict) -> dict | None:
    """One edge's figures re-derived from a retained transient, or None when the follower
    did not follow (the point is INVALID, never a fast settle). `p` = fingerprint params."""
    ref_end = (t_next - p["sample_before"]) if t_next is not None else float(t[-1])
    ref_start = ref_end - p["ref_window"]
    t_pre = t_edge - p["sample_before"]
    v0 = _step_interp(t, vo, t_pre)
    if abs(v0 - _step_interp(t, vi, t_pre)) > _STEP_LEVEL_TOL_V:
        return None
    if sum(1 for x in t if ref_start <= x <= ref_end) < 5:
        return None
    vfinal = _step_window_mean(t, vo, ref_start, ref_end)
    vin_ref = _step_window_mean(t, vi, ref_start, ref_end)
    if abs(vfinal - vin_ref) > _STEP_LEVEL_TOL_V:
        return None
    step = vfinal - v0
    if direction * step < (1 - _STEP_STEP_REL_TOL) * p["step_v"] or \
            direction * step > (1 + _STEP_STEP_REL_TOL) * p["step_v"]:
        return None
    inner = [i for i, x in enumerate(t) if t_edge < x < ref_end]
    if len(inner) < _STEP_MIN_EDGE_SAMPLES:
        return None
    tw = [t_edge] + [t[i] for i in inner] + [ref_end]
    yw = [(_step_interp(t, vo, x) - v0) / step for x in tw]
    drift = abs(_step_interp(t, vo, ref_end) - _step_interp(t, vo, ref_start)) / abs(step)
    run, reversal = yw[0], 0.0
    for y in yw:
        run = max(run, y)
        reversal = max(reversal, run - y)
    t10 = next((tw[i] + (0.1 - yw[i]) / (yw[i + 1] - yw[i]) * (tw[i + 1] - tw[i])
                for i in range(len(yw) - 1) if yw[i] < 0.1 <= yw[i + 1]), None)
    t90 = next((tw[i] + (0.9 - yw[i]) / (yw[i + 1] - yw[i]) * (tw[i + 1] - tw[i])
                for i in range(len(yw) - 1) if yw[i] < 0.9 <= yw[i + 1]), None)
    preshoot = max(0.0, -min(yw))
    return {"overshoot_pct": 100 * max(0.0, max(yw) - 1.0), "preshoot_pct": 100 * preshoot,
            "monotonic": reversal <= p["mono_tol"] and preshoot <= p["mono_tol"],
            "ts": {b: (_step_settling(tw, [y - 1.0 for y in yw], b, t_edge, ref_start)
                       if drift <= p["drift_frac"] * b else math.inf) for b in p["settle_bands"]},
            "t10_90": (t90 - t10) if (t10 is not None and t90 is not None and t90 > t10) else float("nan")}


def _step_extreme(label: str, what: str, vals: dict, claim_s: str, claim_pt: tuple | None,
                  tol: float) -> float:
    """Verify a rounded worst-value claim against the re-derived per-point values.

    `vals` {point: float} (all finite), `claim_s` the displayed number, `claim_pt` the claimed
    binding point. The claimed point must BE an extremum of the re-derived values to display
    precision (ties accepted); the extreme must round to the claim. Returns the derived extreme."""
    extreme = max(vals.values())
    at = sorted((k for k, v in vals.items() if v == extreme),
                key=lambda k: (corner_sort_key(k[0]), float(k[1]), float(k[2])))
    if claim_pt is None or claim_pt not in vals or vals[claim_pt] < extreme - tol:
        raise ReportError(f"{label}: {what} names {fmt_point(*claim_pt) if claim_pt else 'n/a'} but the retained "
                          f"transients put the worst at {fmt_point(*at[0])} (contradictory extremum)")
    if abs(float(claim_s) - extreme) > tol:
        raise ReportError(f"{label}: {what} states {claim_s} but the retained transients give {extreme:.10g}")
    return extreme


def extract_step_response_study(text: str, label: str, root: Path, rid: str,
                                primary: dict | None = None) -> dict:
    """The follower step-response record (issue #113), ungraded (issue #198).

    Population: the record's retained, hash-verified fingerprint inputs (`corners`: the full
    5 x 3 x 3 = 45-point PVT grid); the record's per-point table must hold exactly those points
    (missing, duplicate and unexpected points are errors). Every figure is re-derived offline
    from the retained `corners/<rid>/<point>.dat` transients (whose aggregate sha256 is
    reported) with the record's own fingerprinted timing/band parameters, checked against the
    record's rounded per-point table and its summary / worst-point claims. Not-settled results
    stay explicit (the named status, never zero); invalid points are counted and excluded from
    the worsts. No bound is read and no verdict is formed.
    """
    prov = study_provenance(text, label)
    head = header_of(text)
    if not re.search(r"^- \*\*Claim \(evidence only, no verdict\)\*\*: closed-loop small-step response", head, re.M):
        raise ReportError(f"{label}: the record header does not claim 'evidence only, no verdict' for the "
                          "closed-loop small-step response (the ungraded adapter reads no verdict and must not "
                          "attach one)")
    fp = parse_fingerprint(text, label)
    cors = (fp or {}).get("inputs", {}).get("corners") or {}
    pts = []
    for proc in cors.get("process", []):
        for t in cors.get("temperature_c", []):
            for v in cors.get("supply_v", []):
                pt = (str(proc), f"{float(t):g}", f"{float(v):.2f}")
                if pt in pts:
                    raise ReportError(f"{label}: duplicate retained corner point {fmt_point(*pt)}")
                pts.append(pt)
    if len(pts) != FULL_STUDY_POINTS:
        raise ReportError(f"{label}: the retained fingerprint population is {len(pts)} points, not the full "
                          f"5 MOS x 3 T x 3 VDD = {FULL_STUDY_POINTS} grid (the adapter re-derives a full-grid record)")
    order = sorted(pts, key=lambda k: (corner_sort_key(k[0]), float(k[1]), float(k[2])))
    p = fp["inputs"]["params"]
    for key in ("step_v", "rise_t", "fall_t", "tstop", "sample_before", "ref_window", "mono_tol", "drift_frac"):
        if key not in p:
            raise ReportError(f"{label}: retained fingerprint params carry no '{key}'")
    p = {**p, "settle_bands": [float(b) for b in p["settle_bands"]]}
    # the header's Corner-matrix prose must agree with the fingerprinted population
    hm = re.search(r"^- \*\*Corner matrix run\*\*:\n(?:  - .*\n)+", head, re.M)
    want_procs = ", ".join(cors["process"])
    want_ts = ", ".join(f"{float(t):g}" for t in cors["temperature_c"])
    want_vdd = ", ".join(f"{float(v):.2f}" for v in cors["supply_v"])
    hdr_err = (f"{label}: the header's corner-matrix line disagrees with the retained fingerprint "
               f"population ({want_procs} x {want_ts} C x {want_vdd} V)")
    if not hm:
        raise ReportError(f"{label}: the record header carries no '- **Corner matrix run**:' block")
    hprocs = re.search(r"^  - Process \(MOS\): (.+)$", hm.group(0), re.M)
    hts = re.search(r"^  - Temperature: (.+)$", hm.group(0), re.M)
    hvdd = re.search(r"^  - Supply VDD: ([^(\n]+?)\s*(?:\(.*\))?$", hm.group(0), re.M)
    if not (hprocs and hts and hvdd):
        raise ReportError(hdr_err + " (a Process / Temperature / Supply VDD line is missing)")

    def _unit_list(txt: str, unit: str, fmt: str) -> str:
        try:
            return ", ".join(format(float(x.strip().removesuffix(unit).strip()), fmt) for x in txt.split(","))
        except ValueError:
            raise ReportError(hdr_err) from None
    if (hprocs.group(1).strip() != want_procs or _unit_list(hts.group(1), "C", "g") != want_ts
            or _unit_list(hvdd.group(1), "V", ".2f") != want_vdd):
        raise ReportError(hdr_err)
    # ---- the record's per-point table: exactly the population ----
    where = f"All {len(pts)} points"
    body = section(text, where)
    if not body:
        raise ReportError(f"{label}: no '## {where}' table")
    table = {}
    for ln in body.splitlines():
        if not ln.startswith("|"):
            continue
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) != 12 or c[0] in ("Process", "") or set(c[0]) <= {"-"}:
            continue
        k = (c[0].strip("`"), c[1], c[2])
        if k not in pts:
            raise ReportError(f"{label}: unexpected point {fmt_point(*k)} under '{where}' (not in the retained "
                              "fingerprint population)")
        if k in table:
            raise ReportError(f"{label}: duplicate point {fmt_point(*k)} under '{where}'")
        table[k] = c
    missing = [k for k in order if k not in table]
    if missing:
        raise ReportError(f"{label}: '{where}' is missing {len(missing)} of {len(pts)} points "
                          f"(first: {fmt_point(*missing[0])})")
    # ---- retained transients: full-precision values, cross-checked against the rounded table ----
    # Hash the retained evidence first (cheap): identical record + evidence content reuses the
    # previously derived result (the suite re-builds the same study many times; the result is a
    # pure function of this content). Treat the cached dict as read-only.
    cdir_rel = f"sim/step-response/corners/{rid}"
    digests = []
    for k in order:
        name = f"{k[0]}_{k[1]}c_{k[2]}v.dat"
        path = root / cdir_rel / name
        if not path.is_file():
            raise ReportError(f"{label}: retained per-point transient is missing: {cdir_rel}/{name}")
        digests.append(f"{sha256_file(path)}  {name}\n")
    ck = (rid, hashlib.sha256(text.encode()).hexdigest(),
          hashlib.sha256("".join(digests).encode()).hexdigest())
    if ck in _STEP_CACHE:
        return _STEP_CACHE[ck]
    derived = {}
    for k in order:
        name = f"{k[0]}_{k[1]}c_{k[2]}v.dat"
        t, vo, vi = _step_read_dat(root / cdir_rel / name, label, f"{cdir_rel}/{name}")
        # the runner's whole-point validity rules (extract_step): a point failing them is
        # INVALID (counted, excluded from the worsts), never an error and never a fast settle
        if len(t) < 100 or any(b <= a for a, b in zip(t, t[1:])):
            derived[k] = {"invalid": ["fewer than 100 samples or time not strictly increasing"], "edges": {}}
            continue
        if t[-1] < p["tstop"] * 0.999:
            derived[k] = {"invalid": [f"transient ends at {t[-1]:.3g} s, before {p['tstop']:g} s"], "edges": {}}
            continue
        edges = {n: _step_edge(t, vo, vi, p[e0], p[e1] if e1 else None, d, p)
                 for n, e0, e1, d in _STEP_EDGES}
        derived[k] = {"invalid": [n for n, e in edges.items() if e is None], "edges": edges}

    def claim(what: str, cell: str, value: float, unit_tol: float, scale: float = 1.0) -> None:
        """The record's rounded display `cell` must agree with the re-derived `value`."""
        value = value * scale
        got = "n/a" if not math.isfinite(value) else format(value, ".10g")
        if cell == "not settled":
            raise ReportError(f"{label}: {what} states 'not settled' but the retained transients give {got}")
        try:
            v = float(cell)
        except ValueError:
            raise ReportError(f"{label}: malformed cell '{cell}' ({what}) under '{where}'") from None
        if abs(v - value) > unit_tol + 1e-9:
            raise ReportError(f"{label}: {what} states {cell} but the retained transients give {got}")

    disp = lambda v, nd: "n/a" if not (isinstance(v, float) and math.isfinite(v)) else f"{v:.{nd}f}"
    ns = lambda v: "not settled" if (isinstance(v, float) and math.isinf(v)) else disp(v * 1e9, 1)
    os_pts, os_r_pts, os_f_pts = {}, {}, {}
    ts_pts = {b: {} for b in p["settle_bands"]}
    n_invalid, nonmono = 0, []
    for k in order:
        c, d = table[k], derived[k]
        pt = fmt_point(*k)
        if d["invalid"]:
            n_invalid += 1
            if not c[11].startswith("INVALID:"):
                raise ReportError(f"{label}: {pt} is INVALID in the retained transients ({d['invalid'][0]}) but the "
                                  f"table reports values (note: '{c[11][:60]}')")
            for i in (3, 4, 5, 6, 7, 8):
                if c[i] != "n/a":
                    raise ReportError(f"{label}: invalid point {pt} carries a value in column {i} of '{where}'")
            continue
        if c[11].startswith("INVALID:"):
            raise ReportError(f"{label}: {pt} is marked '{c[11][:60]}' but the retained transients measure it validly")
        r, f = d["edges"]["rise"], d["edges"]["fall"]
        os_pts[k] = max(r["overshoot_pct"], f["overshoot_pct"])
        os_r_pts[k], os_f_pts[k] = r["overshoot_pct"], f["overshoot_pct"]
        o_r, o_f = c[3].split(" / ")
        claim(f"rising overshoot of {pt}", o_r, r["overshoot_pct"], _disp_tol(o_r))
        claim(f"falling overshoot of {pt}", o_f, f["overshoot_pct"], _disp_tol(o_f))
        ps = c[4]
        try:
            pv = float(ps)
        except ValueError:
            raise ReportError(f"{label}: malformed preshoot cell '{ps}' under '{where}'") from None
        if abs(pv - max(r["preshoot_pct"], f["preshoot_pct"])) > _disp_tol(ps) + 1e-9:
            raise ReportError(f"{label}: {where} preshoot of {pt} states {ps} but the retained transients give "
                              f"{disp(max(r['preshoot_pct'], f['preshoot_pct']), 2)}")
        for col, band in ((5, p["settle_bands"][0]), (6, p["settle_bands"][1])):
            cells = c[col].split(" / ")
            if len(cells) != 2:
                raise ReportError(f"{label}: malformed settling cell '{c[col]}' under '{where}' ({pt})")
            ts_pts[band][k] = max(r["ts"][band], f["ts"][band])
            for edge, cell in zip((r, f), cells):
                if math.isinf(edge["ts"][band]):
                    if cell != "not settled":
                        raise ReportError(f"{label}: {pt} does not settle to {band:.1%} of the step (retained "
                                          f"transients) but the table states '{cell}' -- a not-settled result must "
                                          "stay explicit, never a number or zero")
                else:
                    claim(f"settling to {band:.1%} of {pt}", cell, edge["ts"][band],
                          _disp_tol(cell) + 1e-10, 1e9)
        if c[7] != ("yes" if r["monotonic"] and f["monotonic"] else "no"):
            raise ReportError(f"{label}: {where} monotonic flag of {pt} states '{c[7]}' but the retained transients "
                              f"give '{'yes' if r['monotonic'] and f['monotonic'] else 'no'}'")
        if not (r["monotonic"] and f["monotonic"]):
            nonmono.append(k)
        t109 = c[8].split(" / ")
        claim(f"10-90 % rising time of {pt}", t109[0], r["t10_90"] * 1e9, _disp_tol(t109[0]))
        claim(f"10-90 % falling time of {pt}", t109[1], f["t10_90"] * 1e9, _disp_tol(t109[1]))
        for cell, unit in ((c[9], "deg"), (c[10], "MHz")):  # copied PM / GBW columns: displayable or n/a
            if cell != "n/a" and not re.fullmatch(r"-?\d+(?:\.\d+)?", cell):
                raise ReportError(f"{label}: malformed copied column value '{cell}' ({unit}) under '{where}' ({pt})")
    n_valid = len(order) - n_invalid
    # ---- the record's rounded summary claims: summary line and worst-points table ----
    sm = re.search(r"^- \*\*Result summary\*\*: (\d+)/(\d+) points measured validly \((\d+) invalid\); "
                   r"worst overshoot \*\*([^*]+) %\*\* at (.+?); worst (\S+(?: \S+)?) settling \*\*([^*]+)\*\* at "
                   r"(.+?); worst (\S+(?: \S+)?) settling \*\*([^*]+)\*\* at (.+?); monotonic at (\d+)/(\d+) valid "
                   r"points\.", head, re.M)
    if not sm:
        raise ReportError(f"{label}: no '- **Result summary**' line of the expected shape (valid/total/invalid, "
                          "worst overshoot, worst settling to both bands, monotonic count)")
    n_sm_valid, n_sm_total, n_sm_invalid = int(sm.group(1)), int(sm.group(2)), int(sm.group(3))
    if (n_sm_valid, n_sm_total, n_sm_invalid) != (n_valid, len(order), n_invalid):
        raise ReportError(f"{label}: Result summary states {n_sm_valid}/{n_sm_total} valid ({n_sm_invalid} invalid) "
                          f"but the retained transients give {n_valid}/{len(order)} valid ({n_invalid} invalid)")

    def _pt(s: str, what: str) -> tuple:
        m = re.fullmatch(r"(\w+) / (-?[\d.]+) C / ([\d.]+) V", s.strip())
        if not m or (m.group(1), m.group(2), m.group(3)) not in pts:
            raise ReportError(f"{label}: {what} names '{s.strip()}', not a point of the retained population")
        return (m.group(1), m.group(2), m.group(3))

    worst_os = max(os_pts.values()) if os_pts else float("nan")
    if os_pts:
        _step_extreme(label, "the summary's worst overshoot", os_pts, sm.group(4),
                      _pt(sm.group(5), "the summary's worst-overshoot point"), _disp_tol(sm.group(4)) + 1e-9)
    not_settled = {b: [k for k in ts_pts[b] if math.isinf(ts_pts[b][k])] for b in p["settle_bands"]}
    mono_n = n_valid - len(nonmono)
    if int(sm.group(12)) != mono_n or int(sm.group(13)) != n_valid:
        raise ReportError(f"{label}: Result summary states monotonic at {sm.group(12)}/{sm.group(13)} valid points "
                          f"but the retained transients give {mono_n}/{n_valid}")
    bands_txt = {b: f"{b * 100:g} %" for b in p["settle_bands"]}
    for gi, b in ((6, p["settle_bands"][0]), (9, p["settle_bands"][1])):
        raw, at = sm.group(gi + 1), sm.group(gi + 2)
        val = raw[:-3] if raw.endswith(" ns") else raw  # the number, without its displayed unit
        wk = _pt(at, f"the summary's worst settling-to-{bands_txt[b]} point")
        finite = {k: v * 1e9 for k, v in ts_pts[b].items() if math.isfinite(v)}  # ns, as displayed
        if val == "not settled":
            if not not_settled[b] or wk not in not_settled[b]:
                raise ReportError(f"{label}: Result summary states worst {bands_txt[b]} settling 'not settled' at "
                                  f"{fmt_point(*wk)} but the retained transients give {len(not_settled[b])} "
                                  "not-settled points")
        else:
            if not finite or not_settled[b]:
                raise ReportError(f"{label}: Result summary states worst {bands_txt[b]} settling {val} ns but the "
                                  f"retained transients give {len(not_settled[b])} not-settled points")
            _step_extreme(label, f"the summary's worst settling to {bands_txt[b]}", finite, val, wk,
                          _disp_tol(val) + 1e-10)
    wp_body = section(text, "Worst points (evidence; no bound applies)")
    wp = {}
    for ln in (wp_body.splitlines() if wp_body else []):
        if not ln.startswith("|"):
            continue
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) == 3 and c[0] not in ("Figure", "") and not set(c[0]) <= {"-"}:
            wp[c[0]] = c
    for b in p["settle_bands"]:
        row = wp.get(f"Settling to {bands_txt[b]} (worse edge)")
        if row is None:
            raise ReportError(f"{label}: no 'Settling to {bands_txt[b]} (worse edge)' row under '## Worst points'")
        m = re.fullmatch(r"(not settled|[\d.]+)(?: ns)?(?: \((\d+) points? not settled\))?", row[1])
        if not m:
            raise ReportError(f"{label}: malformed worst-settling cell '{row[1]}' under '## Worst points'")
        wk = _pt(row[2], f"the Worst-points table's {bands_txt[b]} settling point")
        if m.group(1) == "not settled":
            if not not_settled[b] or wk not in not_settled[b]:
                raise ReportError(f"{label}: '## Worst points' states {bands_txt[b]} settling 'not settled' at "
                                  f"{fmt_point(*wk)} but the retained transients disagree")
        else:
            finite = {k: v * 1e9 for k, v in ts_pts[b].items() if math.isfinite(v)}  # ns, as displayed
            if not finite or not_settled[b]:
                raise ReportError(f"{label}: '## Worst points' states a {bands_txt[b]} settling time but the retained "
                                  f"transients give {len(not_settled[b])} not-settled points")
            _step_extreme(label, f"the Worst-points table's settling to {bands_txt[b]}", finite, m.group(1), wk,
                          _disp_tol(m.group(1)) + 1e-10)
        n_ns = int(m.group(2) or 0)
        if n_ns != len(not_settled[b]):
            raise ReportError(f"{label}: '## Worst points' states {n_ns} points not settled to {bands_txt[b]} but "
                              f"the retained transients give {len(not_settled[b])}")
    os_row = wp.get("Overshoot (worse edge)")
    if os_row is None:
        raise ReportError(f"{label}: no 'Overshoot (worse edge)' row under '## Worst points'")
    if os_pts:
        om = re.fullmatch(r"([\d.]+|n/a) %", os_row[1])
        if not om or om.group(1) == "n/a":
            raise ReportError(f"{label}: malformed worst-overshoot cell '{os_row[1]}' under '## Worst points'")
        _step_extreme(label, "the Worst-points table's worst overshoot", os_pts, om.group(1),
                      _pt(os_row[2], "the Worst-points table's overshoot point"),
                      _disp_tol(om.group(1)))
    nm_row = wp.get("Non-monotonic points")
    if nm_row is None:
        raise ReportError(f"{label}: no 'Non-monotonic points' row under '## Worst points'")
    nm = re.fullmatch(r"(\d+)/(\d+)", nm_row[1])
    if not nm or int(nm.group(1)) != len(nonmono) or int(nm.group(2)) != n_valid:
        raise ReportError(f"{label}: '## Worst points' states {nm_row[1]} non-monotonic points but the retained "
                          f"transients give {len(nonmono)}/{n_valid}")
    listed = re.findall(r"(\w+) / (-?[\d.]+) C / ([\d.]+) V", nm_row[2])
    for lp in listed:
        if lp not in nonmono:
            raise ReportError(f"{label}: '## Worst points' lists {fmt_point(*lp)} as non-monotonic but the retained "
                              "transients disagree")
    # ---- figures: deterministic descriptive observations (information only, no verdict) ----
    def worst_of(vals: dict, inf_ok: bool = False) -> dict:
        if not vals:
            return {"value": "n/a", "point": None}
        key = (lambda v: (math.isinf(v), v)) if inf_ok else (lambda v: v)
        wk = max(vals, key=lambda k: key(vals[k]))
        return {"value": ns(vals[wk]) if inf_ok else disp(vals[wk], 2), "point": fmt_point(*wk)}

    ns_sorted = lambda ks: [fmt_point(*k) for k in sorted(ks, key=lambda q: (corner_sort_key(q[0]),
                                                                             float(q[1]), float(q[2])))]
    figures = {
        "Overshoot (worse edge)": {"unit": "% of the realised step", **worst_of(os_pts),
                                   "range": [disp(min(os_pts.values()), 2), disp(worst_os, 2)] if os_pts else []},
        "Overshoot rising edge": {"unit": "% of the realised step", **worst_of(os_r_pts)},
        "Overshoot falling edge": {"unit": "% of the realised step", **worst_of(os_f_pts)},
        **{f"Settling to {bands_txt[b]} (worse edge)":
               {"unit": "ns from the input edge", "band": bands_txt[b],
                **worst_of(ts_pts[b], inf_ok=True),
                "not_settled": {"count": len(not_settled[b]), "points": ns_sorted(not_settled[b]),
                                "status": ("not settled within the edge window, or drifting across the reference "
                                           "window; reported as the named status, never zero")}}
           for b in p["settle_bands"]},
        "Monotonicity": {"unit": "points", "non_monotonic": len(nonmono), "of_valid": n_valid,
                         "points": ns_sorted(nonmono)[:6], "list_complete": len(nonmono) <= 6},
        "Validity": {"unit": "points", "invalid": n_invalid, "of": len(order)},
    }
    retained = {"path": cdir_rel, "dat_files": len(digests),
                "sha256": hashlib.sha256("".join(digests).encode()).hexdigest(),
                "sha256_method": _STEP_DAT_METHOD}
    out = {"prov": prov, "points": pts, "scope": {
               "points": [fmt_point(*k) for k in order], "cells": len(order), "full_pvt_cross_product": True,
               "text": (f"FULL ratified PVT grid: {len(cors['process'])} MOS corners x "
                        f"{len(cors['temperature_c'])} temperatures x {len(cors['supply_v'])} supplies = "
                        f"{len(order)} points, one follower step measurement (both edges) per point; no RZ/CC "
                        "passive cross product (the passives sit at typical)")},
           "figures": figures, "not_measured": {},
           "limitations": [
               "evidence only: spec/target-spec.md has no settling or overshoot row and this study proposes no "
               "bound; a settling/overshoot bound would need a spec/ decision record",
               "per point the reported overshoot and settling figures are the worse of the rising and falling "
               "edges; both edges are re-derived from the retained transients",
               "per-point `.meas` cross-checks and control runs were the record's own gates; this adapter "
               "re-derives the extraction from the retained `.dat` transients only",
           ],
           "retained": retained,
           "population": {"points": len(order), "valid": n_valid, "invalid": n_invalid,
                          "monotonic": mono_n, "not_settled": {bands_txt[b]: len(not_settled[b])
                                                               for b in p["settle_bands"]},
                          "edges_per_point": ["rise", "fall"]}}
    _STEP_CACHE[ck] = out
    return out


SUPPLEMENTARY["step-response-follower"]["extract"] = extract_step_response_study  # issue #198: ungraded


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
