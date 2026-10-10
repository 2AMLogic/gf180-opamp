#!/usr/bin/env python3
"""Tests for the spec-citation vs selection.json check (issue #112). No simulator.

Every mutation test works on a throw-away copy of the committed spec, records
and selection manifest (never the originals).

    python3 sim/report/test_spec_citation_check.py   # or: python3 -m unittest
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

import spec_citation_check as sc  # noqa: E402

REPO = HERE.parent.parent
SPEC = "spec/target-spec.md"
GAIN_NEW, GAIN_OLD = "20261010-020141-1e51d1c", "20261009-055759-2524b3e"
SWING_NEW, SSP_FULL = "20261009-143715-4d5aa43", "20261009-142137-1dab1db"


def link(exp: str, rid: str) -> str:
    return f"[record `{rid}`](../sim/{exp}/records/{rid}.md)"


def make_root(tmp: Path) -> Path:
    for p in (REPO / "sim").glob("*/records/*.md"):
        dst = tmp / p.relative_to(REPO)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, dst)
    (tmp / "spec").mkdir()
    shutil.copy(REPO / SPEC, tmp / SPEC)
    (tmp / "sim" / "report").mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "selection.json", tmp / "sim" / "report" / "selection.json")
    return tmp


def row_line(text: str, prefix: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith(f"| {prefix}")]
    assert len(lines) == 1, prefix
    return lines[0]


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._td.name))
        self.spec = self.root / SPEC
        self.manifest = self.root / "sim" / "report" / "selection.json"

    def tearDown(self):
        self._td.cleanup()

    def edit_row(self, prefix: str, old: str, new: str):
        text = self.spec.read_text()
        line = row_line(text, prefix)
        self.assertIn(old, line, f"mutation anchor missing in row {prefix!r}")
        self.spec.write_text(text.replace(line, line.replace(old, new, 1)))

    def problems(self):
        return sc.check(self.root, self.manifest)

    def failing_rows(self):
        return sorted({p.split("row '")[1].split("'")[0] for p in self.problems()})


class Committed(Base):
    def test_committed_spec_matches_selection(self):
        self.assertEqual(sc.check(REPO, HERE / "selection.json"), [])
        self.assertEqual(sc.main([]), 0)

    def test_every_measured_row_is_parsed(self):
        rows = {r["key"]: sc.citations(r["status_raw"]) for r in sc.spec_rows((REPO / SPEC).read_text())}
        for k in sc.ROW_EXPERIMENT:
            self.assertIn(k, rows)
            self.assertTrue(rows[k], f"row {k} cites no record")
        # the offset cell contains an escaped `\|mean\|`; its citations sit after it: the selected
        # full-grid record (issue #120), then the nominal record labelled historical
        self.assertEqual([(c["rid"], c["labelled"]) for c in rows["offset"]],
                         [("20261010-083043-ddf96db", False), ("20261009-072205-96bf3cc", True)])
        self.assertEqual(rows["area"], [])

    def test_labelled_passive_and_superseded_citations_are_exempt(self):
        pm = {r["key"]: r for r in sc.spec_rows((REPO / SPEC).read_text())}["pm"]
        got = {c["rid"]: c["labelled"] for c in sc.citations(pm["status_raw"])}
        self.assertEqual(got, {GAIN_NEW: False, "20261009-234014-55b400c": True,
                               "20261009-233341-95dfc2a": True})


class Drift(Base):
    def test_pre_reconciliation_citations_fail(self):
        """The four drifted citations issue #112 reported must each be caught."""
        for row in ("Open-loop DC gain", "GBW", "Phase margin"):
            self.edit_row(row, link("gain-gbw-pm", GAIN_NEW), link("gain-gbw-pm", GAIN_OLD))
        self.edit_row("Output swing", link("slew-swing-power", SWING_NEW), link("slew-swing-power", SSP_FULL))
        self.assertEqual(self.failing_rows(), ["GBW", "Open-loop DC gain", "Output swing", "Phase margin"])
        self.assertEqual(sc.main(["--root", str(self.root)]), 1)

    def test_older_record_labelled_superseded_or_historical_passes(self):
        for word in ("superseded", "historical"):
            with self.subTest(word=word):
                self.spec.write_text((REPO / SPEC).read_text())
                self.edit_row("Open-loop DC gain", link("gain-gbw-pm", GAIN_NEW),
                              f"{link('gain-gbw-pm', GAIN_NEW)}; {word} {link('gain-gbw-pm', GAIN_OLD)}")
                self.assertEqual(self.problems(), [])

    def test_unlabelled_extra_older_citation_fails(self):
        self.edit_row("Open-loop DC gain", link("gain-gbw-pm", GAIN_NEW),
                      f"{link('gain-gbw-pm', GAIN_NEW)}; see also {link('gain-gbw-pm', GAIN_OLD)}")
        self.assertEqual(self.failing_rows(), ["Open-loop DC gain"])

    def test_selection_moves_without_spec(self):
        m = json.loads(self.manifest.read_text())
        m["experiments"]["noise"] = "sim/noise/records/20991231-000000-0000000.md"
        self.manifest.write_text(json.dumps(m))
        self.assertEqual(self.failing_rows(), ["Input-referred noise"])

    def test_selected_record_not_cited(self):
        self.edit_row("PSRR", link("psrr", "20261009-105929-30ec86d"), "a record")
        probs = self.problems()
        self.assertEqual(len(probs), 1)
        self.assertIn("cites no unlabelled psrr record", probs[0])

    def test_nothing_selected_but_cited(self):
        m = json.loads(self.manifest.read_text())
        m["experiments"]["psrr"] = None
        self.manifest.write_text(json.dumps(m))
        self.assertIn("selects no record", " ".join(self.problems()))


class Existence(Base):
    def test_missing_linked_record(self):
        (self.root / "sim/cmrr-mc/records/20261010-035206-978f088.md").unlink()
        probs = self.problems()
        self.assertEqual(len(probs), 1)
        self.assertIn("does not exist", probs[0])

    def test_missing_bare_record_id(self):
        self.edit_row("Phase margin", "`20261009-233341-95dfc2a`", "`20261009-233341-0000000`")
        probs = self.problems()
        self.assertEqual(len(probs), 1)
        self.assertIn("matches no sim/*/records/*.md file", probs[0])

    def test_cross_experiment_citation_is_existence_only(self):
        # the CMRR row's sim/cmrr-mc record is not governed by selection.json
        cmrr = {r["key"]: r for r in sc.spec_rows(self.spec.read_text())}["cmrr"]
        exps = {c["exp"] for c in sc.citations(cmrr["status_raw"])}
        self.assertEqual(exps, {"cmrr", "cmrr-mc"})
        self.assertEqual(self.problems(), [])

    def test_unmapped_row_citing_a_record_fails(self):
        self.edit_row("Area |", "Open [DR-3] — no layout", f"Open [DR-3] — {link('noise', '20261009-082007-68b4567')}")
        self.assertIn("no experiment mapping", " ".join(self.problems()))

    def test_input_errors_exit_2(self):
        self.manifest.unlink()
        self.assertEqual(sc.main(["--root", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
