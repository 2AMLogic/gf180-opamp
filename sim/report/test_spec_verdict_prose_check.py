#!/usr/bin/env python3
"""Tests for the spec verdict-prose vs report check (issue #156). No simulator.

Every mutation test works on a throw-away copy of the committed spec, README and report JSON.

    python3 sim/report/test_spec_verdict_prose_check.py   # or: python3 -m unittest
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import spec_verdict_prose_check as vp  # noqa: E402

REPO = HERE.parent.parent
SPEC, README, REPORT = "spec/target-spec.md", "manifests/README.md", "sim/reports/characterization-report.json"

#: The two sentences that went stale after the #42 retune (verbatim from the pre-#154 text).
STALE_SPEC = "(three have now been judged by a committed record: gain and GBW pass, phase margin fails)"
STALE_README = "passes — the report still shows its failed row (currently one), the"
FIXED_SPEC = ("(which rows have been judged by a committed record, and their verdicts, are given by the "
              "generated characterization report, not restated here)")
FIXED_README = "passes — the report may still show failed rows (see the report for the current count), the"


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        for rel in (SPEC, README, REPORT):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.root / rel)

    def tearDown(self):
        self._td.cleanup()

    def put(self, rel: str, text: str):
        (self.root / rel).write_text(text)

    def append(self, rel: str, extra: str):
        p = self.root / rel
        p.write_text(p.read_text() + "\n" + extra + "\n")

    def set_fail_count(self, n: int):
        p = self.root / REPORT
        d = json.loads(p.read_text())
        d["summary"]["fail"] = n
        p.write_text(json.dumps(d))

    def problems(self):
        return vp.check(self.root)


class Committed(Base):
    def test_committed_prose_agrees_with_report(self):
        self.assertEqual(vp.check(REPO), [])
        self.assertEqual(vp.main([]), 0)

    def test_report_has_pass_rows_and_no_fail(self):
        d = json.loads((REPO / REPORT).read_text())
        self.assertTrue([r for r in d["rows"] if r.get("verdict") == "PASS"])
        self.assertEqual(d["summary"]["fail"], 0)


class Stale(Base):
    def test_todays_two_stale_sentences_fail(self):
        """Fixture reproducing the pre-#154 spec and README sentences: both are caught."""
        self.append(SPEC, STALE_SPEC)
        self.append(README, STALE_README)
        probs = self.problems()
        self.assertEqual(len(probs), 2, probs)
        self.assertTrue(any(p.startswith(SPEC) and "phase margin" in p for p in probs))
        self.assertTrue(any(p.startswith(README) and "failed-row count of 'one'" in p for p in probs))
        self.assertEqual(vp.main(["--root", str(self.root)]), 1)

    def test_corrected_text_passes(self):
        self.append(SPEC, FIXED_SPEC)
        self.append(README, FIXED_README)
        self.assertEqual(self.problems(), [])

    def test_status_cell_saying_fails_on_pass_row(self):
        p = self.root / SPEC
        text = p.read_text()
        line = next(ln for ln in text.splitlines() if ln.startswith("| Phase margin"))
        self.put(SPEC, text.replace(line, line.replace("measured: PASS", "measured: FAIL", 1)))
        probs = self.problems()
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("row 'Phase margin'", probs[0])

    def test_historical_or_superseded_label_exempts(self):
        for word in ("historical", "superseded"):
            with self.subTest(word=word):
                self.put(SPEC, (REPO / SPEC).read_text())
                self.append(SPEC, f"({word}: phase margin fails before the retune)")
                self.assertEqual(self.problems(), [])

    def test_fail_on_unrelated_clause_is_not_flagged(self):
        self.append(README, "The build fails on a stale manifest.")
        self.assertEqual(self.problems(), [])


class Counts(Base):
    def test_matching_count_passes(self):
        self.append(README, "The report shows its failed rows (currently 0).")
        self.assertEqual(self.problems(), [])

    def test_count_differs_from_report(self):
        self.set_fail_count(2)
        self.append(README, "The report shows its failed row (currently one).")
        probs = self.problems()
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("FAIL count is 2", probs[0])


class Inputs(Base):
    def test_missing_report_exits_2(self):
        (self.root / REPORT).unlink()
        self.assertEqual(vp.main(["--root", str(self.root)]), 2)

    def test_malformed_report_exits_2(self):
        self.put(REPORT, "{}")
        self.assertEqual(vp.main(["--root", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
