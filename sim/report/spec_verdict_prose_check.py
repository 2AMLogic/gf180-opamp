#!/usr/bin/env python3
"""Check that live status prose does not contradict the report's verdicts (issue #156).

spec_citation_check.py keeps the record IDs cited in spec/target-spec.md Sec. 2 in step with
sim/report/selection.json; it says nothing about what the prose claims about those records.  After
the #42 retune the spec kept saying "phase margin fails" and manifests/README.md kept saying "one
failed row" while every check stayed green.  This check reads the verdicts the generator already
derived (sim/reports/characterization-report.json) -- never a simulator, never the network -- and
fails when live prose contradicts them:

  1. a Sec. 2 Status cell of a row whose report verdict is PASS must not say fails / failed / FAIL;
  2. in spec/target-spec.md and manifests/README.md, a clause naming a PASS row (gain, GBW, phase
     margin, slew, swing, power) must not say fails / failed / FAIL;
  3. a "failed row(s) (currently N)" count must equal the report's FAIL count.

A clause labelled superseded / historical (the convention spec_citation_check.py honors: the text
back to the previous ';' or '(') is exempt.  The cheapest durable fix is to stop restating verdicts
and counts and defer to the report; this check then has nothing to flag.

    python3 sim/report/spec_verdict_prose_check.py   # exit 0 ok, 1 drift found, 2 input error
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import spec_citation_check as sc  # noqa: E402

DEFAULT_ROOT = HERE.parent.parent
REPORT_REL = "sim/reports/characterization-report.json"
README_REL = "manifests/README.md"

#: How the prose names each graded row (case-insensitive).
ROW_ALIAS = {
    "gain": r"(?:dc\s+)?gain", "gbw": r"gbw", "pm": r"phase\s+margin|pm",
    "slew": r"slew(?:\s+rate)?", "swing": r"(?:output\s+)?swing", "power": r"(?:quiescent\s+)?power",
}
FAIL_RX = re.compile(r"\b(?:fails?|failed|failing)\b|\bFAIL\b", re.I)
#: "failed row (currently one)" / "failed rows (currently 2)"
COUNT_RX = re.compile(r"failed\s+rows?\s*\(\s*currently\s+(\w+)\s*\)", re.I)
NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten".split())}
_CLAUSE_SPLIT = re.compile(r"[;,:.()\n]")


class ProseError(Exception):
    """Report / spec input problem: exit 2."""


def load_report(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise ProseError(f"report not found: {path} (run sim/report/characterization_report.py)")
    except ValueError as e:
        raise ProseError(f"{path}: not valid JSON ({e})")


def _fail_clause(text: str, pos: int) -> bool:
    """True when the clause around `pos` carries a historical / superseded label."""
    return bool(sc.LABEL_RX.search(sc._clause_before(text, pos)))


def _row_aliases(rx: str) -> re.Pattern:
    return re.compile(rf"\b(?:{rx})\b", re.I)


def check(root: Path, report_path: Path | None = None) -> list:
    """Return a list of human-readable problems ([] = prose agrees with the report)."""
    report = load_report(report_path or root / REPORT_REL)
    try:
        passing = {r["key"] for r in report["rows"] if r.get("verdict") == "PASS"}
        n_fail = int(report["summary"]["fail"])
    except (KeyError, TypeError, ValueError) as e:
        raise ProseError(f"report lacks rows[].verdict / summary.fail ({e!r})")
    docs = {}
    for rel in (sc.SPEC_REL, README_REL):
        try:
            docs[rel] = (root / rel).read_text()
        except FileNotFoundError:
            raise ProseError(f"not found: {root / rel}")
    problems = []

    for row in sc.spec_rows(docs[sc.SPEC_REL]):  # 1. Status cells of PASS rows
        if row["key"] not in passing:
            continue
        for m in FAIL_RX.finditer(row["status_raw"]):
            if not _fail_clause(row["status_raw"], m.start()):
                problems.append(f"{sc.SPEC_REL} Sec. 2 row '{row['name']}': Status says '{m.group(0)}' but the "
                                f"report verdict is PASS (fix the prose, or label it 'historical'/'superseded')")

    for rel, text in docs.items():
        offset = 0
        for clause in _CLAUSE_SPLIT.split(text):  # 2. a clause naming a PASS row and saying fail
            start = text.find(clause, offset) if clause else offset
            offset = start + len(clause)
            m = FAIL_RX.search(clause)
            if not m or _fail_clause(text, start + m.start()):
                continue
            named = [k for k in passing if k in ROW_ALIAS and _row_aliases(ROW_ALIAS[k]).search(clause)]
            if named:
                problems.append(f"{rel}: '{clause.strip()}' says '{m.group(0)}' of {'/'.join(sorted(named))}, "
                                f"which the report grades PASS")

        for m in COUNT_RX.finditer(text):  # 3. restated FAIL counts
            word = m.group(1).lower()
            claimed = int(word) if word.isdigit() else NUMBER_WORDS.get(word)
            if claimed != n_fail:
                problems.append(f"{rel}: claims a failed-row count of '{m.group(1)}' but the report's FAIL "
                                f"count is {n_fail} (defer to the report instead of restating it)")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: derived from this file)")
    a = ap.parse_args(argv)
    try:
        problems = check(a.root.resolve())
    except (ProseError, sc.CitationError) as e:
        print(f"spec_verdict_prose_check: error: {e}", file=sys.stderr)
        return 2
    if problems:
        print("spec_verdict_prose_check: status prose contradicts sim/reports/characterization-report.json:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("spec_verdict_prose_check: no status prose contradicts the report's verdicts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
