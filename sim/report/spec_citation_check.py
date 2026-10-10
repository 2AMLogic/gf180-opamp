#!/usr/bin/env python3
"""Check that spec/target-spec.md Sec. 2 cites the records selection.json selects (issue #112).

The Status column of the Sec. 2 performance-target table cites, by hand, the
sim/ record behind each measured result; sim/report/selection.json is the
machine-readable statement of which record is selected per experiment (and,
for slew-swing-power, per row).  This check reads both -- never a simulator,
never the network -- and fails when they drift apart:

  1. every record cited in a Status cell (a Markdown link to
     sim/<experiment>/records/<id>.md, or a bare backticked record id) must
     exist as a committed record file;
  2. every cited record of the row's own experiment must be the record
     selection.json selects for that row, UNLESS the citation is explicitly
     labelled in its clause (the text back to the previous ';' or '(') as
     superseded / historical evidence or as the passive-corner study
     (``supersedes `<id>`` ``, ``historical``, ``passive-corner [record ...]``);
  3. a row whose experiment has a selected record must cite it, and a row must
     not cite unlabelled records of an experiment selection.json selects
     nothing for.

The CMRR row's mismatch Monte-Carlo record under sim/cmrr-mc/ is selected
beside the systematic one (issue #124) and is governed the same way (ROW_ALSO).
Records of any other experiment cited as supporting evidence are not governed
by selection.json and are checked for existence only.

    python3 sim/report/spec_citation_check.py   # exit 0 ok, 1 drift found, 2 input error
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import characterization_report as cr  # noqa: E402

DEFAULT_ROOT = HERE.parent.parent
SPEC_REL = "spec/target-spec.md"

#: Sec. 2 row key (as characterization_report keys the rows) -> (experiment,
#: per-row selection key or None).  Rows absent here cite no record (area).
ROW_EXPERIMENT = {
    "gain": ("gain-gbw-pm", None), "gbw": ("gain-gbw-pm", None), "pm": ("gain-gbw-pm", None),
    "slew": ("slew-swing-power", "slew"), "swing": ("slew-swing-power", "swing"),
    "power": ("slew-swing-power", "power"),
    "noise": ("noise", None), "offset": ("offset-mc", None),
    "cmrr": ("cmrr", None), "psrr": ("psrr", None),
    cr.ICMR_ROW_KEY: ("input-common-mode", None),
}

#: Rows that also cite a second experiment's selected record (issue #124: the CMRR row's mismatch
#: Monte Carlo record, selected for the report beside the systematic one); governed like the primary.
ROW_ALSO = {"cmrr": (("cmrr-mc", None),)}

RID = r"\d{8}-\d{6}-[0-9a-f]{7}"
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*?/sim/([\w.-]+)/records/(" + RID + r")\.md)\)")
_BARE = re.compile(r"`(" + RID + r")`")
#: An in-clause label exempting a citation from the selected-record comparison.
LABEL_RX = re.compile(r"\b(supersed\w*|historical|passive-corner)\b", re.I)


class CitationError(Exception):
    """Spec / manifest input problem: exit 2."""


def spec_rows(text: str) -> list:
    """Sec. 2 table rows as {name, key, status_raw}."""
    sec = cr.section(text, "2. Performance targets")
    rows = []
    for line in sec.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| Parameter"):
            continue
        # an escaped pipe (`\|mean\|` in the offset row) is cell text, not a column break
        c = [s.replace("\0", "\\|") for s in cr.split_cells(line.replace("\\|", "\0"))]
        if len(c) < 6:
            continue
        name = cr.clean_md(c[0]).split(" (")[0]
        key = next((k for pfx, k in cr.SPEC_KEYS if name.startswith(pfx)), None)
        rows.append({"name": name, "key": key or re.sub(r"\W+", "-", name.lower()), "status_raw": c[5]})
    return rows


def _clause_before(cell: str, pos: int) -> str:
    cut = max(cell.rfind(";", 0, pos), cell.rfind("(", 0, pos))
    return cell[cut + 1:pos]


def citations(cell: str) -> list:
    """Every record cited in a Status cell: {rid, exp (None for a bare id), href, labelled}."""
    out, spans = [], []
    for m in _LINK.finditer(cell):
        spans.append(m.span())
        out.append({"rid": m.group(4), "exp": m.group(3), "href": m.group(2), "pos": m.start(),
                    "labelled": bool(LABEL_RX.search(_clause_before(cell, m.start())))})
    for m in _BARE.finditer(cell):
        if any(a <= m.start() < b for a, b in spans):
            continue  # the link text of a citation already collected
        out.append({"rid": m.group(1), "exp": None, "href": None, "pos": m.start(),
                    "labelled": bool(LABEL_RX.search(_clause_before(cell, m.start())))})
    return sorted(out, key=lambda c: c["pos"])


def selected_for(manifest: dict, exp: str, sub: str | None) -> str | None:
    v = manifest["experiments"].get(exp)
    if isinstance(v, dict):
        v = v.get(sub) if sub else None
    return Path(v).stem if v else None


def check(root: Path, manifest_path: Path, spec_rel: str = SPEC_REL) -> list:
    """Return a list of human-readable problems ([] = spec citations agree with the selection)."""
    spec_path = root / spec_rel
    try:
        text = spec_path.read_text()
    except FileNotFoundError:
        raise CitationError(f"spec not found: {spec_path}")
    try:
        manifest = cr.load_manifest(manifest_path)
    except cr.ReportError as e:
        raise CitationError(str(e))
    rows = spec_rows(text)
    if not rows:
        raise CitationError(f"{spec_path}: no Sec. 2 performance-target table found")
    problems = []
    for row in rows:
        cites = citations(row["status_raw"])
        where = f"{spec_rel} Sec. 2 row '{row['name']}'"
        for c in cites:
            if c["href"] is not None:
                p = (spec_path.parent / c["href"]).resolve()
                if not p.is_file():
                    problems.append(f"{where}: cited record {c['href']} does not exist")
            else:
                hits = sorted(root.glob(f"sim/*/records/{c['rid']}.md"))
                if not hits:
                    problems.append(f"{where}: cited record id {c['rid']} matches no sim/*/records/*.md file")
                elif len(hits) == 1:
                    c["exp"] = hits[0].parent.parent.name
        if row["key"] not in ROW_EXPERIMENT:
            if cites:
                problems.append(f"{where}: cites record(s) but has no experiment mapping "
                                f"(add the row to ROW_EXPERIMENT in {Path(__file__).name})")
            continue
        for exp, sub in (ROW_EXPERIMENT[row["key"]],) + ROW_ALSO.get(row["key"], ()):
            sel = selected_for(manifest, exp, sub)
            primary = [c for c in cites if c["exp"] == exp and not c["labelled"]]
            sel_desc = f"{exp}" + (f" ({sub})" if sub else "")
            for c in primary:
                if sel is None:
                    problems.append(f"{where}: cites {exp} record {c['rid']} but sim/report/selection.json "
                                    f"selects no record for {sel_desc}")
                elif c["rid"] != sel:
                    problems.append(f"{where}: cites {exp} record {c['rid']} but sim/report/selection.json "
                                    f"selects {sel} for {sel_desc} (cite the selected record, or label the "
                                    f"older citation 'superseded'/'historical' in its clause)")
            if sel is not None and not primary:
                problems.append(f"{where}: sim/report/selection.json selects {exp} record {sel} for "
                                f"{sel_desc}, but the Status cell cites no unlabelled {exp} record")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: derived from this file)")
    ap.add_argument("--manifest", type=Path, default=None, help="selection manifest (default: <root>/sim/report/selection.json)")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    try:
        problems = check(root, a.manifest or root / "sim" / "report" / "selection.json")
    except CitationError as e:
        print(f"spec_citation_check: error: {e}", file=sys.stderr)
        return 2
    if problems:
        print("spec_citation_check: target-spec.md Sec. 2 citations disagree with sim/report/selection.json:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("spec_citation_check: every Sec. 2 record citation exists and matches sim/report/selection.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
