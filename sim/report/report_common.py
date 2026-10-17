"""Split out of characterization_report.py (issue #219); moved verbatim, no logic changes."""
from __future__ import annotations

import hashlib
import json
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
