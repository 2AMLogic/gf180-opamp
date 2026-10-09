#!/usr/bin/env python3
"""Tests for the characterization report generator (issue #50). No simulator.

Every mutation test works on a throw-away copy of the committed records and
spec (never the originals).

    python3 sim/report/test_report.py          # or: python3 -m unittest
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import characterization_report as cr  # noqa: E402

REPO = HERE.parent.parent
MANIFEST = HERE / "selection.json"
FULL_SSP_REC = "20261009-142137-1dab1db"  # power + slew + swing (swing data vin/vout only)
SWING_REC = "20261009-143715-4d5aa43"  # swing only, with the M6/M7 saturation vectors
SSP = "sim/slew-swing-power/records/"


def make_root(tmp: Path) -> Path:
    """Copy the committed record Markdown + spec into a scratch repo root."""
    for p in (REPO / "sim").glob("*/records/*.md"):
        dst = tmp / p.relative_to(REPO)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, dst)
    (tmp / "spec").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "spec" / "target-spec.md", tmp / "spec" / "target-spec.md")
    return tmp


def manifest(**over) -> dict:
    m = json.loads(MANIFEST.read_text())
    for k, v in over.items():
        k = k.replace("_", "-") if k != "offset_mc" else "offset-mc"
        if v is None:
            m["experiments"].pop(k, None)
        else:
            m["experiments"][k] = v
    return m


def rows_by_key(rep):
    return {r["key"]: r for r in rep["rows"]}


class CommittedReport(unittest.TestCase):
    def test_deterministic_and_golden(self):
        a = cr.generate(REPO, MANIFEST)
        b = cr.generate(REPO, MANIFEST)
        self.assertEqual(a, b)
        out = REPO / cr.OUT_DIR_REL
        self.assertEqual((out / f"{cr.OUT_NAME}.md").read_text(), a[0], "committed report.md is stale")
        self.assertEqual((out / f"{cr.OUT_NAME}.json").read_text(), a[1], "committed report.json is stale")
        self.assertEqual(cr.main(["--check"]), 0)

    def test_no_timestamps_hosts_or_absolute_paths(self):
        md, js = cr.generate(REPO, MANIFEST)
        for text in (md, js):
            self.assertNotIn(str(REPO), text)
            self.assertNotIn("/home/", text)
            self.assertIsNone(re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", text))
        self.assertEqual(json.loads(js), json.loads(js))
        self.assertTrue(js.endswith("}\n"))

    def test_headline_facts(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        self.assertEqual((r["pm"]["verdict"], r["pm"]["points_pass"], r["pm"]["points_total"]), ("FAIL", 15, 45))
        self.assertEqual(r["pm"]["worst"], "57.34 deg")
        self.assertEqual(r["pm"]["worst_corner"], "fs / 125 C / 2.97 V")
        self.assertEqual((r["gain"]["verdict"], r["gain"]["worst"]), ("PASS", "93.79 dB"))
        self.assertEqual((r["gbw"]["verdict"], r["gbw"]["worst"]), ("PASS", "10.422 MHz"))
        self.assertEqual(rep["dut"]["normalised_sha256_prefix"], "81fbd914f8254a49")
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["record_id"], "20261009-055759-2524b3e")

    def test_slew_swing_power_rows(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        self.assertEqual((r["slew"]["verdict"], r["slew"]["points_pass"], r["slew"]["worst"], r["slew"]["worst_corner"]),
                         ("PASS", 45, "14.51 V/us", "ss / 125 C / 2.97 V"))
        self.assertEqual((r["power"]["verdict"], r["power"]["worst"], r["power"]["worst_corner"]),
                         ("PASS", "310.98 uW", "ff / -40 C / 3.63 V"))
        self.assertEqual(r["power"]["spec_bound"], "≤ 350 uW")
        self.assertEqual((r["swing"]["verdict"], r["swing"]["worst"], r["swing"]["worst_corner"]),
                         ("PASS", "2.46 Vpp", "ss / 125 C / 2.97 V"))
        # swing comes from the record whose committed data re-derives it; slew/power from the full run
        self.assertEqual(r["swing"]["source"]["record_id"], SWING_REC)
        self.assertEqual(r["slew"]["source"]["record_id"], FULL_SSP_REC)
        self.assertEqual(r["swing"]["source"]["experiment"], "slew-swing-power")
        labels = [f["label"] for f in r["swing"]["figures"]]
        self.assertTrue(any("re-derived" in l for l in labels), labels)
        self.assertIn({"label": "stretch >= 2.6 Vpp (not a mandatory row), points holding", "value": "35/45",
                       "corner": None}, r["swing"]["figures"])
        self.assertFalse(any("cannot be re-derived" in l for l in r["swing"]["limitations"]))

    def test_unratified_rows_have_no_verdict(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        for k in ("noise", "offset", "cmrr", "psrr"):
            self.assertEqual(r[k]["status"], "measured-no-bound", k)
            self.assertIsNone(r[k]["verdict"], k)
            self.assertTrue(r[k]["bound_open"], k)
            self.assertTrue(r[k]["worst"], k)
        md = cr.render_md(rep)
        self.assertNotRegex(md, r"(noise|offset|CMRR|PSRR)[^|\n]*\|[^|\n]*\|[^|\n]*\| \*\*(PASS|FAIL)")
        self.assertIn("open (no ratified bound)", md)
        self.assertEqual(r["cmrr"]["worst"].split(" dB")[0], "95.42")
        self.assertIn("5.006", r["offset"]["worst"])

    def test_all_spec_rows_present(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        spec = cr.parse_spec(REPO / "spec" / "target-spec.md")
        keys = [r["key"] for r in rep["rows"]]
        for s in spec:
            self.assertIn(s["key"], keys)
        for k in ("gain", "gbw", "pm", "slew", "noise", "offset", "cmrr", "psrr", "swing", "power", "area", "post-layout"):
            self.assertIn(k, keys)
        r = rows_by_key(rep)
        for k in ("area", "post-layout"):
            self.assertEqual(r[k]["status"], "not-measured", k)
            self.assertIsNone(r[k]["verdict"], k)
        for k in ("slew", "swing", "power"):
            self.assertEqual(r[k]["status"], "measured-verdict", k)

    def test_coverage_differs_and_is_reported(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        self.assertEqual(r["offset"]["coverage"]["points"], 5)
        self.assertEqual(r["offset"]["coverage"]["mc_samples_per_corner"], 300)
        self.assertEqual(r["offset"]["coverage"]["temps_c"], [27])
        for k in ("gain", "noise", "cmrr", "psrr", "slew", "swing", "power"):
            self.assertEqual(r[k]["coverage"]["points"], 45, k)
        self.assertTrue(any("coverage differs" in l for l in rep["limitations"]))
        self.assertTrue(any("PDK revision differs" in l for l in rep["limitations"]))
        self.assertTrue(any("ngspice versions differ" in l for l in rep["limitations"]))
        for s in rep["sources"].values():
            self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$")

    def test_generator_never_runs_a_simulator(self):
        src = (HERE / "characterization_report.py").read_text()
        for bad in ("subprocess", "os.system", "import socket", "urllib", "import requests"):
            self.assertNotIn(bad, src, bad)
        self.assertNotIn("ngspice -b", src)

class Mutations(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))

    def tearDown(self):
        self._t.cleanup()

    def build(self, m=None):
        return cr.build(self.root, m or manifest())

    def edit(self, rel, old, new, count=1):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, count))

    def test_unmodified_copy_matches_committed(self):
        self.assertEqual(cr.render_json(self.build()), cr.generate(REPO, MANIFEST)[1])

    def test_missing_record(self):
        (self.root / "sim/noise/records/20261009-082007-68b4567.md").unlink()
        with self.assertRaisesRegex(cr.ReportError, "selected record is missing"):
            self.build()

    def test_mixed_dut_hash_rejected(self):
        self.edit("sim/cmrr/records/20261009-105631-30ec86d.md", "81fbd914f8254a49", "0123456789abcdef")
        with self.assertRaisesRegex(cr.ReportError, "different DUT versions"):
            self.build()

    def test_mixed_dut_hash_cli_exit_code(self):
        self.edit("sim/psrr/records/20261009-105929-30ec86d.md", "81fbd914f8254a49", "0123456789abcdef")
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        self.assertEqual(cr.main(["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]), 2)
        self.assertFalse((self.root / "o").exists())

    def test_full_hash_vs_prefix_is_compatible(self):
        # gain records the 64-hex hash, the others only the prefix: must still match.
        rep = self.build()
        self.assertEqual(len(rep["sources"]["gain-gbw-pm"]["dut_sha256"]), 64)
        self.assertEqual(len(rep["sources"]["noise"]["dut_sha256"]), 16)

    def test_absent_experiment_is_not_measured(self):
        rep = self.build(manifest(psrr=None, noise=None))
        r = rows_by_key(rep)
        self.assertEqual(r["psrr"]["status"], "not-measured")
        self.assertEqual(r["noise"]["status"], "not-measured")
        self.assertIsNone(r["psrr"]["source"])
        self.assertEqual(r["pm"]["verdict"], "FAIL")
        self.assertNotIn("psrr", rep["sources"])
        self.assertTrue(any("no record selected for psrr" in l for l in rep["limitations"]))

    def test_empty_selection_still_lists_every_row(self):
        rep = self.build({"experiments": {}})
        self.assertTrue(all(r["status"] == "not-measured" for r in rep["rows"]))
        self.assertEqual(len(rep["rows"]), len(cr.parse_spec(self.root / "spec/target-spec.md")) + 1)

    def test_superseded_gain_record_rejected(self):
        for old in ("20260915-221407-1bb9a74", "20261003-030340-7bd8071"):
            with self.assertRaisesRegex(cr.ReportError, "superseded by 20261009-055759-2524b3e"):
                self.build(manifest(gain_gbw_pm=f"sim/gain-gbw-pm/records/{old}.md"))

    def test_superseded_allowed_only_explicitly(self):
        m = manifest(gain_gbw_pm="sim/gain-gbw-pm/records/20261003-030340-7bd8071.md")
        m["allow_superseded"] = ["20261003-030340-7bd8071"]
        # Allowed, but this provisional record carries no verdicts: still an error, never a silent result.
        with self.assertRaises(cr.ReportError):
            self.build(m)

    def test_record_outside_experiment_dir_rejected(self):
        with self.assertRaisesRegex(cr.ReportError, "not a record under"):
            self.build(manifest(noise="sim/cmrr/records/20261009-105631-30ec86d.md"))

    def test_unknown_experiment_in_manifest(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps({"experiments": {"bogus": "x"}}))
        with self.assertRaisesRegex(cr.ReportError, "unknown experiment"):
            cr.load_manifest(mp)

    def test_record_bound_must_match_spec(self):
        self.edit("spec/target-spec.md", "**≥ 60 dB [DR-2]**", "**≥ 65 dB [DR-2]**")
        with self.assertRaisesRegex(cr.ReportError, "not the current ratified bound"):
            self.build()

    def test_verdict_table_must_agree_with_point_table(self):
        self.edit("sim/gain-gbw-pm/records/20261009-055759-2524b3e.md", "| **FAIL** | 15/45 |", "| **FAIL** | 16/45 |")
        with self.assertRaisesRegex(cr.ReportError, "disagrees with its own per-point table"):
            self.build()

    def test_pdk_mismatch_is_flagged_not_fatal(self):
        self.edit("sim/noise/records/20261009-082007-68b4567.md", "c6d73a35f524070e85faff4a6a9eef49553ebc2b",
                  "a" * 40)
        rep = self.build()
        self.assertTrue(any("PDK revision differs" in l for l in rep["limitations"]))

    def test_partial_grid_flagged(self):
        t = (self.root / "sim/cmrr/records/20261009-105631-30ec86d.md").read_text()
        t = "\n".join(l for l in t.splitlines() if not l.startswith("| ss / 125 C / 3.63 V")) + "\n"
        (self.root / "sim/cmrr/records/20261009-105631-30ec86d.md").write_text(t)
        rep = self.build()
        self.assertEqual(rows_by_key(rep)["cmrr"]["coverage"]["points"], 44)
        self.assertTrue(any("partial grid" in l for l in rows_by_key(rep)["cmrr"]["limitations"]))

    def test_check_mode(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        args = ["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]
        self.assertEqual(cr.main(args + ["--check"]), 1)  # nothing committed yet
        self.assertEqual(cr.main(args), 0)
        self.assertEqual(cr.main(args + ["--check"]), 0)
        first = [(self.root / "o" / f"{cr.OUT_NAME}{e}").read_bytes() for e in (".md", ".json")]
        self.assertEqual(cr.main(args), 0)
        self.assertEqual(first, [(self.root / "o" / f"{cr.OUT_NAME}{e}").read_bytes() for e in (".md", ".json")])
        (self.root / "o" / f"{cr.OUT_NAME}.md").write_text("stale\n")
        self.assertEqual(cr.main(args + ["--check"]), 1)

    def test_ssp_single_record_for_all_rows(self):
        # the full run alone: all three rows from it, and its swing is flagged as not re-derivable
        rep = self.build(manifest(slew_swing_power=f"{SSP}{FULL_SSP_REC}.md"))
        r = rows_by_key(rep)
        self.assertEqual({r[k]["source"]["record_id"] for k in ("slew", "swing", "power")}, {FULL_SSP_REC})
        self.assertTrue(any("cannot be re-derived" in l for l in r["swing"]["limitations"]))
        self.assertIn("slew-swing-power", rep["sources"])

    def test_ssp_row_from_a_record_that_did_not_judge_it(self):
        m = manifest(slew_swing_power={"power": f"{SSP}{SWING_REC}.md", "swing": f"{SSP}{SWING_REC}.md"})
        with self.assertRaisesRegex(cr.ReportError, "does not judge that row"):
            self.build(m)

    def test_ssp_unselected_row_is_not_measured(self):
        rep = self.build(manifest(slew_swing_power={"swing": f"{SSP}{SWING_REC}.md"}))
        r = rows_by_key(rep)
        self.assertEqual(r["swing"]["verdict"], "PASS")
        self.assertEqual((r["slew"]["status"], r["power"]["status"]), ("not-measured", "not-measured"))

    def test_ssp_manifest_shape(self):
        mp = self.root / "m.json"
        for bad, msg in (({"gain-gbw-pm": {"gain": "x"}}, "not a per-row object"),
                         ({"slew-swing-power": {"noise": "x"}}, "unknown row"),
                         ({"slew-swing-power": ["x"]}, "expected a record path")):
            mp.write_text(json.dumps({"experiments": bad}))
            with self.assertRaisesRegex(cr.ReportError, msg):
                cr.load_manifest(mp)

    def test_ssp_verdict_table_must_agree_with_point_table(self):
        self.edit(f"{SSP}{FULL_SSP_REC}.md", "| **PASS** | 45/45 | 310.98 uW", "| **PASS** | 45/45 | 300.00 uW")
        with self.assertRaisesRegex(cr.ReportError, "disagrees with its own per-point table"):
            self.build()

    def test_ssp_failed_point_is_counted(self):
        self.edit(f"{SSP}{SWING_REC}.md", "| **PASS** | 45/45 | 2.46 Vpp", "| **PASS** | 44/45 | 2.46 Vpp")
        with self.assertRaisesRegex(cr.ReportError, "disagrees with its own per-point table"):
            self.build()

    def test_ssp_bound_must_match_spec(self):
        self.edit("spec/target-spec.md", "**≤ 350 µW worst-case corner", "**≤ 340 µW worst-case corner")
        with self.assertRaisesRegex(cr.ReportError, "not the current ratified bound"):
            self.build()

    def test_ssp_not_run_rows_are_never_selected(self):
        # the earlier records judged power/swing but left slew NOT RUN: slew can never come from them
        sel = cr.latest_selection(self.root)["slew-swing-power"]
        self.assertEqual(sel, {"power": f"{SSP}{FULL_SSP_REC}.md", "slew": f"{SSP}{FULL_SSP_REC}.md",
                               "swing": f"{SSP}{SWING_REC}.md"})
        ex = cr.extract_ssp((self.root / SSP / "20261009-114727-1dab1db.md").read_text(), "x")
        self.assertNotIn("slew", ex["rows"])

    def test_latest_selection_skips_nothing_superseded(self):
        sel = cr.latest_selection(self.root)
        self.assertEqual(sel["gain-gbw-pm"], "sim/gain-gbw-pm/records/20261009-055759-2524b3e.md")
        self.assertEqual(sel, json.loads(MANIFEST.read_text())["experiments"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
