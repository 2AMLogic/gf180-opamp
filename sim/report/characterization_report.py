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
The follower step-response study (issue #198) is bound-free with no row at
all -- settling and overshoot have no spec row -- and is re-derived offline
from its retained per-point transients into an ungraded section of its own.

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
import tempfile
from pathlib import Path
from extract_mc import (  # noqa: F401
    CMRR_MC_EXP, CMRR_MC_FIGS, DRIFT_RESOLVE_K, GAIN_MC_EXP, GAIN_MC_ROWS, OFFSET_GRID_TITLE, PSRR_MC_EXP,
    PSRR_MC_FIGS, PSRR_MC_SIDES, _drift_entry, _fin, _okey, _pm_key, _worst_table, cmrr_mc_crosscheck,
    extract_cmrr, extract_cmrr_mc, extract_gain_gbw_pm_mc, extract_offset, extract_psrr_mc,
    gain_gbw_pm_mc_crosscheck, offset_drift_view, offset_grid_crosscheck, offset_stats, psrr_mc_crosscheck)
from report_common import (  # noqa: F401
    DEFAULT_MANIFEST, DEFAULT_ROOT, DUT_REL, EXPERIMENTS, HERE, MULTI_RECORD, OUT_DIR_REL, OUT_NAME,
    ReportError, SCHEMA, STUDY_TITLES, _harness, clean_md, common_limitations, coverage_of, coverage_text,
    fmt_point, grid_points, parse_fingerprint, parse_provenance, posix_rel, section, sha256_file, split_cells)
from extract_icmr import (  # noqa: F401
    ICMR_EXP, ICMR_ROW_KEY, _icmr_addendum, extract_icmr, icmr_crosscheck, icmr_evidence, icmr_figures)
from studies import (  # noqa: F401
    SUPPLEMENTARY, _STEP_EDGES, _step_edge, study_observation, study_scope)
from render import (  # noqa: F401
    render_evidence, render_json, render_md)


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
    "gain-gbw-pm-mc": "sim/gain-gbw-pm-mc/measurement_config.py",
    "noise": "sim/noise/measurement_config.py",
    "offset-mc": "sim/offset-mc/measurement_config.py",
    "cmrr": "sim/cmrr/measurement_config.py",
    "cmrr-mc": "sim/cmrr-mc/measurement_config.py",
    "psrr": "sim/psrr/measurement_config.py",
    "psrr-mc": "sim/psrr-mc/measurement_config.py",
    "slew-swing-power": "sim/slew-swing-power/measurement_config.py",
    "input-common-mode": "sim/input-common-mode/measurement_config.py",
    "step-response": "sim/step-response/measurement_config.py",  # issue #198: supplementary step study
}

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
                f"{prov['dut_prefix']}. Rerun the side study (its experiment's runner) to append a new record "
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
            "scope": ex["scope"] if "scope" in ex else study_scope(ex["points"], spec.get("scope", "limited")),
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
                                     "PASS/FAIL verdict is formed and no figure is compared with a bound"
                                     if spec["rows"] else
                                     "none: evidence-only observations with no spec row to inform (settling and "
                                     "overshoot are deliberately unratified); no PASS/FAIL verdict is formed and no "
                                     "figure is compared with any bound"))
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
            rows_txt = ", ".join(SUPPLEMENTARY[k]["rows"])
            glob_lim.append(f"no supplementary study selected for {k}: " +
                            (f"its rows ({rows_txt}) carry no passive-corner observation" if rows_txt else
                             "its ungraded observations are not shown"))
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
