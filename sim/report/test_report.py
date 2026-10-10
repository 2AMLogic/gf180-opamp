#!/usr/bin/env python3
"""Tests for the characterization report generator (issue #50). No simulator.

Every mutation test works on a throw-away copy of the committed records and
spec (never the originals).

    python3 sim/report/test_report.py          # or: python3 -m unittest
"""
from __future__ import annotations

import hashlib
import json
import os
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
    net = tmp / "design" / "netlist"
    net.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "design" / "netlist" / "opamp_two_stage.spice", net / "opamp_two_stage.spice")
    for exp in ("gain-gbw-pm", "noise", "offset-mc", "cmrr", "psrr", "slew-swing-power"):
        mc = tmp / "sim" / exp
        (mc / "testbench").mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "sim" / exp / "measurement_config.py", mc / "measurement_config.py")
        for tb in (REPO / "sim" / exp / "testbench").glob("*.spice"):
            shutil.copy(tb, mc / "testbench" / tb.name)
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

    def test_passive_study_cited_only_for_gain_gbw_pm(self):
        """Issue #95: typical-passive limitation scoped per record; study cited for gain/GBW/PM only."""
        rep = json.loads((REPO / cr.OUT_DIR_REL / f"{cr.OUT_NAME}.json").read_text())
        rows = rows_by_key(rep)
        study = (REPO / cr.PASSIVE_STUDY_REL).read_text()
        self.assertIn("passes at 24/27 study cells (fails at 3/27)", study)
        self.assertIn("passes at 7/27 study cells", study)
        for k in ("gain", "gbw", "pm"):
            lim = " ".join(rows[k]["limitations"])
            self.assertIn(cr.PASSIVE_STUDY_ID, lim)
            self.assertNotIn("are not swept", lim)
        gbw = " ".join(rows["gbw"]["limitations"])
        for frag in ("24/27", "3/27", "9.656"):
            self.assertIn(frag, gbw)
        self.assertIn("7/27", " ".join(rows["pm"]["limitations"]))
        for k in ("slew", "swing", "power", "noise"):
            if k in rows:
                lim = " ".join(rows[k]["limitations"])
                self.assertIn("passives at typical only", lim)
                self.assertNotIn(cr.PASSIVE_STUDY_ID, lim)
        self.assertEqual(rows["gbw"]["verdict"], "PASS")
        self.assertEqual(rows["pm"]["verdict"], "FAIL")

    def test_md_links_resolve_from_report_dir(self):
        """Issue #95 review: md links (incl. the passive study) resolve relative to sim/reports/."""
        md_path = REPO / cr.OUT_DIR_REL / f"{cr.OUT_NAME}.md"
        md = md_path.read_text()
        study_link = os.path.relpath(cr.PASSIVE_STUDY_REL, cr.OUT_DIR_REL).replace(os.sep, "/")
        self.assertIn(f"]({study_link})", md)
        self.assertTrue((md_path.parent / study_link).resolve().is_file())
        targets = re.findall(r"\]\(([^)\s]+)\)", md)
        self.assertTrue(targets)
        for t in targets:
            if "://" in t or t.startswith("#"):
                continue
            self.assertTrue((md_path.parent / t.split("#")[0]).resolve().exists(),
                            f"broken md link from {cr.OUT_DIR_REL}: {t}")

    def test_evidence_sidecar_binds_report_bytes(self):
        out = REPO / cr.OUT_DIR_REL
        raw = (out / f"{cr.OUT_NAME}.json").read_bytes()
        want = "sha256:" + hashlib.sha256(raw).hexdigest()
        ev = json.loads((out / f"{cr.OUT_NAME}.evidence.json").read_text())
        self.assertEqual(ev["kind"], "generic")
        self.assertEqual(ev["status"], "pass")
        self.assertEqual(ev["report"]["path"], "sim/reports/characterization-report.json")
        self.assertEqual(ev["provenance"]["input"]["content_hash"], want)
        man = json.loads((REPO / "manifests/gf180-opamp.json").read_text())
        self.assertEqual(man["evidence"]["8"], {"file": "sim/reports/characterization-report.evidence.json",
                                                "content_hash": want})
        # honest wrapper: failures stay visible in the report, not hidden by 'pass'
        rep = json.loads(raw)
        self.assertGreater(rep["summary"]["fail"], 0)
        self.assertEqual(ev["report_summary"], rep["summary"])
        self.assertNotIn("/home/", json.dumps(ev))
        self.assertIsNone(re.search(r"\d{4}-\d\d-\d\dT", json.dumps(ev)))

    def test_evidence_deterministic_two_dirs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertEqual(cr.main(["--out-dir", a]), 0)
            self.assertEqual(cr.main(["--out-dir", b]), 0)
            for e in (".md", ".json", ".evidence.json"):
                self.assertEqual((Path(a) / f"{cr.OUT_NAME}{e}").read_bytes(), (Path(b) / f"{cr.OUT_NAME}{e}").read_bytes())

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
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["record_id"], "20261010-020141-1e51d1c")

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
        for k in ("noise", "offset"):
            self.assertEqual(r[k]["status"], "measured-no-bound", k)
            self.assertIsNone(r[k]["verdict"], k)
            self.assertTrue(r[k]["bound_open"], k)
            self.assertTrue(r[k]["worst"], k)
        # CMRR/PSRR carry DR-6 bounds tagged "proposed, not ratified": never graded either
        for k in ("cmrr", "psrr"):
            self.assertEqual(r[k]["status"], "proposed-not-graded", k)
            self.assertIsNone(r[k]["verdict"], k)
            self.assertFalse(r[k]["bound_open"], k)
            self.assertTrue(r[k]["spec_bound"].startswith("proposed, not ratified: "), k)
            self.assertIn("[DR-6]", r[k]["spec_bound"], k)
        md = cr.render_md(rep)
        self.assertNotRegex(md, r"(noise|offset|CMRR|PSRR)[^|\n]*\|[^|\n]*\|[^|\n]*\| \*\*(PASS|FAIL)")
        self.assertIn("open (no ratified bound)", md)
        self.assertIn("5.006", r["offset"]["worst"])
        # the systematic CMRR record stays visible, but only as information, never as the row's statistic
        sys_dc = r["cmrr"]["figures"][0]  # first row of the record's worst-case table: the DC plateau
        self.assertEqual(sys_dc["value"].split(" dB")[0], "95.42")
        self.assertIn("not the row's statistic", sys_dc["label"])
        self.assertTrue(any("NOT the statistic of the proposed row" in l for l in r["cmrr"]["limitations"]))

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

    def test_proposed_row_is_not_graded(self):
        # DR-5's ICMR row is a proposal: never a verdict, never "no record", never counted as judged
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)["input-common-mode-range"]
        self.assertEqual(r["status"], "proposed-not-graded")
        self.assertIsNone(r["verdict"])
        self.assertIsNone(r["source"])
        self.assertIn("20261009-222613-871d1a6", r["spec_status"])
        s = rep["summary"]
        self.assertEqual(s["proposed_not_graded"], 3)  # ICMR [DR-5], CMRR and PSRR [DR-6]
        self.assertEqual(s["ratified_rows_judged"], sum(1 for x in rep["rows"] if x["verdict"]))
        self.assertEqual(s["not_measured"], sum(1 for x in rep["rows"] if x["status"] == "not-measured"))
        md = cr.render_md(rep)
        line = next(l for l in md.splitlines() if l.startswith("| Input common-mode range |"))
        self.assertIn("proposed, not ratified", line)
        self.assertNotIn("no record", line)
        self.assertNotIn("not-measured", line)
        self.assertIn("3 proposed, not ratified (not graded)", md)

    def test_proposed_rows_backed_by_a_selected_record_are_not_graded(self):
        # DR-6: CMRR and PSRR have selected records, yet a proposed bound is never graded and the
        # record's worst value is never placed beside it (the systematic CMRR figure is optimistic)
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        md = cr.render_md(rep)
        for k, name in (("cmrr", "CMRR"), ("psrr", "PSRR")):
            row = r[k]
            self.assertEqual(row["status"], "proposed-not-graded", k)
            self.assertIsNone(row["verdict"], k)
            self.assertIsNone(row["worst"], k)
            self.assertIsNone(row["worst_corner"], k)
            self.assertIsNone(row["points_total"], k)
            self.assertIsNotNone(row["source"], k)  # still traceable, just not graded
            self.assertIn(k, rep["sources"])
            self.assertIn("DR-6", row["spec_status"], k)
            self.assertTrue(row["figures"], k)
            self.assertTrue(all("information only" in f["label"] for f in row["figures"]), k)
            line = next(l for l in md.splitlines() if l.startswith(f"| {name} |"))
            cells = [c.strip() for c in line.strip("|").split("|")]
            self.assertTrue(cells[1].startswith("proposed, not ratified: "), line)
            self.assertEqual(cells[2], "proposed-not-graded", line)
            self.assertEqual(cells[3], "none", line)
            self.assertEqual(cells[4:7], ["-", "-", "-"], line)
            self.assertNotIn("dB (", cells[5])
        self.assertIn("20261010-035206-978f088", r["cmrr"]["spec_status"])  # the mismatch-inclusive record
        cmrr_line = next(l for l in md.splitlines() if l.startswith("| CMRR |"))
        self.assertNotIn("95.42", cmrr_line)

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

GAIN = "sim/gain-gbw-pm/records/"
TB = "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice"
MCFG = "sim/gain-gbw-pm/measurement_config.py"
OLD_GAIN_REC = "20261009-055759-2524b3e"


def fingerprint_record(root: Path, rec: str) -> None:
    """Fixture: stamp a copy of a gain record with the fingerprint block the
    driver would write for the CURRENT bench in `root` (never touches the repo)."""
    import types
    sys.path.insert(0, str(REPO / "sim"))
    mod = types.ModuleType("_mcfg_fixture")
    mod.__file__ = str(root / MCFG)
    exec(compile((root / MCFG).read_text(), str(root / MCFG), "exec"), mod.__dict__)
    sys.path.pop(0)
    tb = (root / TB).read_text()
    p = root / GAIN / f"{rec}.md"
    text = p.read_text()
    text = re.sub(r"(?m)^- \*\*Measurement fingerprint\*\*:.*\n", "", text)
    text = re.sub(r"(?ms)^## Measurement fingerprint inputs\n.*?^```\n\n", "", text)
    text = text.replace("- **Corner matrix run**:", "\n".join(mod.fingerprint_lines(tb)) + "\n- **Corner matrix run**:", 1)
    text = text.replace("## Plots", "\n".join(mod.inputs_section(tb)) + "\n## Plots", 1)
    p.write_text(text)


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

    NET = "design/netlist/opamp_two_stage.spice"

    def test_stale_dut_width_change_rejected(self):
        # all records retained; only the current netlist changes (issue #75)
        p = self.root / self.NET
        t = p.read_text()
        self.assertIn("W=72u", t)
        p.write_text(t.replace("W=72u", "W=73u", 1))
        with self.assertRaisesRegex(cr.ReportError, r"stale DUT.*Experiments needing a rerun: cmrr, gain-gbw-pm"):
            self.build()

    def test_stale_dut_check_cli_fails_and_writes_nothing(self):
        p = self.root / self.NET
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        out = self.root / "o"
        self.assertEqual(cr.main(["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(out), "--check"]), 2)
        self.assertFalse(out.exists())

    def test_wrapper_and_whitespace_normalisation_passes(self):
        p = self.root / self.NET
        t = p.read_text()
        self.assertRegex(t, r"(?m)^\*\*\.subckt")
        t2 = re.sub(r"(?m)^\*\*\.(subckt|ends)", r".\1", t)  # already-uncommented wrapper
        t2 = "\n".join(l + "  " if l.startswith("M") else l for l in t2.splitlines()) + "\n"
        self.assertNotEqual(t, t2)
        p.write_text(t2)
        self.assertEqual(cr.render_json(self.build()), cr.generate(REPO, MANIFEST)[1])

    def test_archival_marks_report_and_skips_gate(self):
        p = self.root / self.NET
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        rep = cr.build(self.root, manifest(), archival=True)
        self.assertTrue(rep["archival"])
        self.assertIn("ARCHIVAL", cr.render_md(rep))

    def test_archival_cannot_supply_signoff_outputs(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        base = ["--root", str(self.root), "--manifest", str(mp), "--archival"]
        self.assertEqual(cr.main(base), 2)  # needs explicit destination
        self.assertEqual(cr.main(base + ["--out-dir", str(self.root / "o"), "--check"]), 2)
        self.assertEqual(cr.main(base + ["--out-dir", str(self.root / cr.OUT_DIR_REL)]), 2)
        out = self.root / "o"
        self.assertEqual(cr.main(base + ["--out-dir", str(out)]), 0)
        self.assertFalse((out / f"{cr.OUT_NAME}.evidence.json").exists())

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
        # PSRR's bound is proposed (DR-6): without a record it stays "proposed, not graded",
        # never a "not measured" row of the ratified table
        self.assertEqual(r["psrr"]["status"], "proposed-not-graded")
        self.assertEqual(r["psrr"]["figures"], [])
        self.assertEqual(r["noise"]["status"], "not-measured")
        self.assertIsNone(r["psrr"]["source"])
        self.assertEqual(r["pm"]["verdict"], "FAIL")
        self.assertNotIn("psrr", rep["sources"])
        self.assertTrue(any("no record selected for psrr" in l for l in rep["limitations"]))

    def test_empty_selection_still_lists_every_row(self):
        rep = self.build({"experiments": {}})
        proposed = ("input-common-mode-range", "cmrr", "psrr")
        self.assertTrue(all(r["status"] == "not-measured" for r in rep["rows"] if r["key"] not in proposed))
        for k in proposed:
            self.assertEqual(rows_by_key(rep)[k]["status"], "proposed-not-graded", k)
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
        self.edit("sim/gain-gbw-pm/records/20261010-020141-1e51d1c.md", "| **FAIL** | 15/45 |", "| **FAIL** | 16/45 |")
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
        exts = (".md", ".json", ".evidence.json")
        first = [(self.root / "o" / f"{cr.OUT_NAME}{e}").read_bytes() for e in exts]
        self.assertEqual(cr.main(args), 0)
        self.assertEqual(first, [(self.root / "o" / f"{cr.OUT_NAME}{e}").read_bytes() for e in exts])
        (self.root / "o" / f"{cr.OUT_NAME}.md").write_text("stale\n")
        self.assertEqual(cr.main(args + ["--check"]), 1)
        self.assertEqual(cr.main(args), 0)
        ev = self.root / "o" / f"{cr.OUT_NAME}.evidence.json"
        ev.write_text(ev.read_text().replace('"pass"', '"fail"'))
        self.assertEqual(cr.main(args + ["--check"]), 1)
        ev.unlink()
        self.assertEqual(cr.main(args + ["--check"]), 1)

    def test_evidence_regenerates_after_source_mutation(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        args = ["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]
        self.assertEqual(cr.main(args), 0)
        rec = self.root / "sim/cmrr/records/20261009-105631-30ec86d.md"
        rec.write_text(rec.read_text() + "\nextra line\n")
        self.assertEqual(cr.main(args + ["--check"]), 1)

    def test_no_passing_sidecar_on_generator_error(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        args = ["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]
        (self.root / "sim/cmrr/records/20261009-105631-30ec86d.md").unlink()
        self.assertEqual(cr.main(args), 2)
        self.assertFalse((self.root / "o").exists())
        self.assertEqual(cr.main(args + ["--check"]), 2)

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

    def test_proposed_classification_follows_the_in_row_tag(self):
        # without the in-row "proposed, not ratified" tag the row is an ordinary unmeasured row
        self.edit("spec/target-spec.md", "[DR-5] — proposed, not ratified**", "[DR-5]**")
        r = rows_by_key(self.build())["input-common-mode-range"]
        self.assertEqual(r["status"], "not-measured")
        self.assertEqual(self.build()["summary"]["proposed_not_graded"], 2)  # CMRR, PSRR still proposed

    def test_proposed_dr6_classification_follows_the_in_row_tag(self):
        # without the tag, a CMRR/PSRR row backed by its record reverts to measured-no-bound with the
        # record's worst value (the pre-DR-6 behaviour); with it, the row is not graded
        self.edit("spec/target-spec.md", "≥ 50 dB each at 10 kHz [DR-6] — proposed, not ratified**",
                  "≥ 50 dB each at 10 kHz [DR-6]**")
        r = rows_by_key(self.build())
        self.assertEqual(r["psrr"]["status"], "measured-no-bound")
        self.assertTrue(r["psrr"]["worst"].startswith("PSRR+ "))
        self.assertEqual(r["cmrr"]["status"], "proposed-not-graded")
        self.assertIsNone(r["cmrr"]["worst"])

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
        self.assertEqual(sel["gain-gbw-pm"], "sim/gain-gbw-pm/records/20261010-020141-1e51d1c.md")
        self.assertEqual(sel, json.loads(MANIFEST.read_text())["experiments"])

    def test_latest_selection_ignores_side_study_records(self):
        # a newer passive-corner study record (issue #70) is not a grid record
        rec = self.root / "sim/gain-gbw-pm/records/29991231-235959-0000000.md"
        rec.write_text("# gain/GBW/PM passive-corner study (RZ x CC) -- record 29991231-235959-0000000\n")
        self.assertEqual(cr.latest_selection(self.root)["gain-gbw-pm"],
                         "sim/gain-gbw-pm/records/20261010-020141-1e51d1c.md")




class MeasurementConfigFreshness(unittest.TestCase):
    """Issue #85: measurement-configuration fingerprint alongside DUT identity."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))
        self.gain = json.loads(MANIFEST.read_text())["experiments"]["gain-gbw-pm"]
        self.rec = Path(self.gain).stem
        self.m = manifest()

    def tearDown(self):
        self._t.cleanup()

    def edit(self, rel, old, new):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, 1))

    def stamped(self):
        fingerprint_record(self.root, self.rec)

    def test_unfingerprinted_records_disclosed_as_unknown(self):
        rep = cr.build(self.root, manifest())
        for e, s in rep["sources"].items():
            if e == "gain-gbw-pm" and "Measurement fingerprint" in (REPO / self.gain).read_text():
                continue
            self.assertEqual(s["measurement_config"]["status"], "unknown", e)
        joined = " ".join(rep["limitations"])
        self.assertIn("cmrr: measurement-configuration freshness unknown", joined)
        self.assertIn("freshness unknown", cr.render_md(rep))

    def test_matching_fingerprint_is_current(self):
        self.stamped()
        rep = cr.build(self.root, self.m)
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["measurement_config"]["status"], "current")
        self.assertFalse(any(l.startswith("gain-gbw-pm: measurement-configuration") for l in rep["limitations"]))

    def test_changed_load_bias_or_analysis_invalidates_with_unchanged_dut(self):
        for rel, old, new, what in (
            (TB, "CL vout 0 2p", "CL vout 0 5p", "CL"),
            (TB, "dc 10u", "dc 12u", "Ibias"),
            (MCFG, "AC_FSTOP, AC_PPD = 0.1, 1e9, 20", "AC_FSTOP, AC_PPD = 0.1, 1e9, 10", "AC"),
        ):
            with self.subTest(what):
                self.setUp(); self.stamped()
                self.edit(rel, old, new)
                with self.assertRaisesRegex(cr.ReportError,
                                            r"stale measurement configuration.*Experiments needing a rerun: gain-gbw-pm"):
                    cr.build(self.root, self.m)
                self.tearDown()

    def test_stale_config_names_changed_inputs_and_archival_still_works(self):
        self.stamped()
        self.edit(TB, "CL vout 0 2p", "CL vout 0 5p")
        with self.assertRaisesRegex(cr.ReportError, r"changed bench"):
            cr.build(self.root, self.m)
        rep = cr.build(self.root, self.m, archival=True)
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["measurement_config"]["status"], "stale")
        self.assertTrue(any("archival report only" in l for l in rep["limitations"]))

    def test_formatting_comments_and_paths_do_not_change_fingerprint(self):
        self.stamped()
        t = (self.root / TB).read_text()
        t2 = "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl   vout  0   2p   ; load")
        t2 = t2.replace("'design.ngspice'", "'/some/other/checkout/work/design.ngspice'")
        (self.root / TB).write_text(t2)
        rep = cr.build(self.root, self.m)
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["measurement_config"]["status"], "current")

    def test_fingerprint_independent_of_record_id_and_checkout(self):
        self.stamped()
        a = cr.build(self.root, self.m)["sources"]["gain-gbw-pm"]["measurement_config"]["fingerprint"]
        with tempfile.TemporaryDirectory() as d:
            root2 = make_root(Path(d) / "elsewhere")
            fingerprint_record(root2, self.rec)
            b = cr.build(root2, self.m)["sources"]["gain-gbw-pm"]["measurement_config"]["fingerprint"]
        self.assertEqual(a, b)

    def test_tampered_inputs_block_rejected(self):
        self.stamped()
        self.edit(GAIN + self.rec + ".md", "cl vout 0 2p", "cl vout 0 3p")
        with self.assertRaisesRegex(cr.ReportError, r"retained measurement-fingerprint inputs hash"):
            cr.build(self.root, self.m)

    def test_fingerprint_line_without_inputs_rejected(self):
        self.stamped()
        p = self.root / GAIN / f"{self.rec}.md"
        p.write_text(re.sub(r"(?ms)^## Measurement fingerprint inputs\n.*?^```\n\n", "", p.read_text()))
        with self.assertRaisesRegex(cr.ReportError, r"retains no inputs block"):
            cr.build(self.root, self.m)

    def test_dut_gate_still_applies(self):
        self.stamped()
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        with self.assertRaisesRegex(cr.ReportError, r"stale DUT"):
            cr.build(self.root, self.m)


def stamp_record(root: Path, exp: str, rec_rel: str, figures: tuple | None = None) -> None:
    """Fixture: stamp a copy of a record with the fingerprint header line and
    inputs block its driver would write for the CURRENT configuration in
    `root` (never touches the repo). `figures` selects the measured figures of
    a slew/swing/power record."""
    import types
    h = cr._harness()
    mpath = root / cr.MEASUREMENT_CONFIG[exp]
    h.purge_config_modules()
    mod = types.ModuleType("_mcfg_fixture")
    mod.__file__ = str(mpath)
    exec(compile(mpath.read_text(), str(mpath), "exec"), mod.__dict__)
    h.purge_config_modules()
    texts = {k: (root / rel).read_text() for k, rel in mod.TESTBENCHES_REL.items()}
    sel = {"figures": {f: True for f in figures}} if figures else None
    p = root / rec_rel
    text = p.read_text()
    text = re.sub(r"(?m)^- \*\*Measurement fingerprint\*\*:.*\n", "", text)
    text = re.sub(r"(?ms)^## Measurement fingerprint inputs\n.*?^```\n\n", "", text)
    text, n = re.subn(r"(?m)^(- \*\*DUT\*\*:.*\n)", lambda m: m.group(1) + "\n".join(mod.fingerprint_lines(texts, sel)) + "\n", text, count=1)
    assert n == 1
    p.write_text(text.rstrip("\n") + "\n\n" + "\n".join(mod.inputs_section(texts, sel)))


EXP_BENCH = {
    "noise": "sim/noise/testbench/tb_noise.spice",
    "offset-mc": "sim/offset-mc/testbench/tb_offset_mc.spice",
    "cmrr": "sim/cmrr/testbench/tb_cmrr.spice",
    "psrr": "sim/psrr/testbench/tb_psrr.spice",
}
#: (experiment, file, old, new, changed-input group named in the rejection)
STALE_CASES = [
    ("noise", EXP_BENCH["noise"], "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("noise", "sim/noise/measurement_config.py", "F_START, F_STOP, PPD = 0.1, 1e7, 20", "F_START, F_STOP, PPD = 0.1, 1e7, 10", "analysis"),
    ("noise", "sim/noise/measurement_config.py", '("100 Hz - 1 MHz", 100.0, 1e6),', '("100 Hz - 1 MHz", 100.0, 2e6),', "bands"),
    ("noise", "sim/noise/measurement_config.py", "ISOLATION_VALUES = (1e8, 1e10)", "ISOLATION_VALUES = (1e8, 1e11)", "controls"),
    ("offset-mc", EXP_BENCH["offset-mc"], ".param sw_stat_mismatch=1", ".param sw_stat_mismatch=0", "bench"),
    ("offset-mc", "sim/offset-mc/measurement_config.py", "MC_N = 300", "MC_N = 200", "monte_carlo"),
    ("offset-mc", "sim/offset-mc/measurement_config.py", "MC_SEED = 45", "MC_SEED = 46", "monte_carlo"),
    ("offset-mc", "sim/offset-mc/measurement_config.py", 'MC_VARY = "mismatch"', 'MC_VARY = "process"', "monte_carlo"),
    ("offset-mc", "sim/offset-mc/measurement_config.py", "TEMP_C = 27.0", "TEMP_C = 85.0", "corners"),
    ("cmrr", EXP_BENCH["cmrr"], "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("cmrr", "sim/cmrr/measurement_config.py", '"cm": {"acp": 1.0, "acn": 1.0},', '"cm": {"acp": 1.0, "acn": 0.5},', "excitation"),
    ("cmrr", "sim/cmrr/measurement_config.py", 'SERVO_NOMINAL = {"rsv": 1e9, "csv": 1e9}', 'SERVO_NOMINAL = {"rsv": 1e8, "csv": 1e9}', "excitation"),
    ("cmrr", "sim/gain-gbw-pm/measurement_config.py", "AC_FSTOP, AC_PPD = 0.1, 1e9, 20", "AC_FSTOP, AC_PPD = 0.1, 1e9, 10", "analysis"),
    ("psrr", EXP_BENCH["psrr"], "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("psrr", "sim/psrr/measurement_config.py", '"vss": {**QUIET, "acss": 1.0},', '"vss": {**QUIET, "acss": 2.0},', "excitation"),
    ("psrr", "sim/psrr/measurement_config.py", "FEEDTHROUGH_R = 100e3", "FEEDTHROUGH_R = 50e3", "controls"),
    ("psrr", "sim/cmrr/measurement_config.py", "ISOLATION_CSV = (1e7, 1e11)", "ISOLATION_CSV = (1e7, 1e12)", "controls"),
]
CLEAN_CASES = [
    # (experiment, replacement producing an equivalent bench)
    ("noise", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl   vout  0   2p ; load")),
    ("offset-mc", lambda t: "* extra comment\n\n" + t.replace("Ibias vdd ibias dc 10u", "ibias  vdd ibias   dc 10u $ bias")),
    ("cmrr", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
    ("psrr", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
]


class MigratedExperimentFreshness(unittest.TestCase):
    """Issue #89: noise, offset MC, CMRR, PSRR and slew/swing/power join the
    measurement-configuration gate."""

    SSP_REC = "sim/slew-swing-power/records/" + FULL_SSP_REC + ".md"
    SWING_REC = "sim/slew-swing-power/records/" + SWING_REC + ".md"

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))
        self.sel = json.loads(MANIFEST.read_text())["experiments"]
        self.m = manifest()

    def tearDown(self):
        self._t.cleanup()

    def fresh(self):
        """A pristine scratch root (for subTest loops that mutate it)."""
        self._t.cleanup()
        self.setUp()

    def edit(self, rel, old, new):
        p = self.root / rel
        t = p.read_text()
        self.assertEqual(t.count(old), 1, f"{rel}: {old!r} must occur exactly once")
        p.write_text(t.replace(old, new, 1))

    def stamp(self, exp):
        stamp_record(self.root, exp, self.sel[exp])

    def test_all_selected_experiments_are_registered(self):
        self.assertEqual(sorted(cr.MEASUREMENT_CONFIG), sorted(self.sel))

    def test_committed_legacy_records_stay_unknown(self):
        rep = cr.build(REPO, manifest())
        for e, src in rep["sources"].items():
            if e == "gain-gbw-pm":
                continue
            self.assertEqual(src["measurement_config"]["status"], "unknown", e)
            self.assertIn("predates measurement fingerprinting", src["measurement_config"]["detail"], e)
        joined = " ".join(rep["limitations"])
        for e in ("noise", "offset-mc", "cmrr", "psrr"):
            self.assertIn(f"{e}: measurement-configuration freshness unknown", joined)
        self.assertIn("slew-swing-power (", joined)

    def test_stamped_records_are_current_and_others_stay_unknown(self):
        for exp in EXP_BENCH:
            with self.subTest(exp):
                self.fresh()
                self.stamp(exp)
                rep = cr.build(self.root, self.m)
                self.assertEqual(rep["sources"][exp]["measurement_config"]["status"], "current")
                for e, src in rep["sources"].items():
                    if e not in (exp, "gain-gbw-pm"):
                        self.assertEqual(src["measurement_config"]["status"], "unknown", e)
                self.assertFalse(any(l.startswith(f"{exp}: measurement-configuration") for l in rep["limitations"]))

    def test_changed_input_with_unchanged_dut_is_rejected_naming_experiment_and_group(self):
        for exp, rel, old, new, group in STALE_CASES:
            with self.subTest(f"{exp}: {group}: {old[:30]}"):
                self.fresh()
                self.stamp(exp)
                self.edit(rel, old, new)
                with self.assertRaisesRegex(
                        cr.ReportError,
                        rf"stale measurement configuration.*{re.escape(exp)}: changed [^;]*{group}.*"
                        rf"Experiments needing a rerun: {re.escape(exp)}"):
                    cr.build(self.root, self.m)

    def test_archival_discloses_stale_config(self):
        for exp, rel, old, new, group in STALE_CASES[::4]:
            with self.subTest(exp):
                self.fresh()
                self.stamp(exp)
                self.edit(rel, old, new)
                rep = cr.build(self.root, self.m, archival=True)
                self.assertEqual(rep["sources"][exp]["measurement_config"]["status"], "stale")
                self.assertTrue(any(l.startswith(f"{exp}: measurement configuration differs") and "archival report only" in l
                                    for l in rep["limitations"]))

    def test_formatting_comments_and_workspace_paths_do_not_change_fingerprint(self):
        for exp, f in CLEAN_CASES:
            with self.subTest(exp):
                self.fresh()
                self.stamp(exp)
                p = self.root / EXP_BENCH[exp]
                t = f(p.read_text()).replace("'design.ngspice'", "'/some/other/checkout/work/design.ngspice'")
                self.assertNotEqual(t, p.read_text())
                p.write_text(t)
                rep = cr.build(self.root, self.m)
                self.assertEqual(rep["sources"][exp]["measurement_config"]["status"], "current")

    def test_fingerprint_independent_of_record_id_and_checkout(self):
        for exp in EXP_BENCH:
            with self.subTest(exp):
                self.fresh()
                self.stamp(exp)
                a = cr.build(self.root, self.m)["sources"][exp]["measurement_config"]["fingerprint"]
                with tempfile.TemporaryDirectory() as d:
                    root2 = make_root(Path(d) / "elsewhere")
                    rel = self.sel[exp]
                    other = rel.replace(Path(rel).stem, "20991231-235959-0000000")
                    shutil.move(root2 / rel, root2 / other)
                    stamp_record(root2, exp, other)
                    m2 = manifest(**{exp.replace("-", "_"): other})
                    b = cr.build(root2, m2)["sources"][exp]["measurement_config"]["fingerprint"]
                self.assertEqual(a, b)

    def test_tampered_inputs_block_rejected(self):
        for exp in EXP_BENCH:
            with self.subTest(exp):
                self.fresh()
                self.stamp(exp)
                p = self.root / self.sel[exp]
                t = p.read_text()
                self.assertIn('"experiment":"' + exp + '"', t)
                p.write_text(t.replace('"fingerprint_version":1', '"fingerprint_version":1,"extra":0', 1))
                with self.assertRaisesRegex(cr.ReportError, r"retained measurement-fingerprint inputs hash"):
                    cr.build(self.root, self.m)

    def test_dut_gate_still_applies(self):
        self.stamp("noise")
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        with self.assertRaisesRegex(cr.ReportError, r"stale DUT"):
            cr.build(self.root, self.m)

    # ---- slew / swing / power: per-figure selection ----

    def ssp_key(self, rec):
        return f"slew-swing-power ({Path(rec).stem})"

    def test_ssp_full_record_stale_names_the_changed_figure(self):
        for rel, old, new, fig in (
            ("sim/slew-swing-power/testbench/tb_slew.spice", "CL vout 0 2p", "CL vout 0 3p", "slew"),
            ("sim/slew-swing-power/testbench/tb_power.spice", "Ibias vdd ibias dc 10u", "Ibias vdd ibias dc 12u", "power"),
            ("sim/slew-swing-power/measurement_config.py", "SWING_VIN_STEP_V = 5e-3", "SWING_VIN_STEP_V = 1e-2", "swing"),
            ("sim/slew-swing-power/measurement_config.py", "SLEW_STEP_V = 1.0 ", "SLEW_STEP_V = 0.8 ", "slew"),
        ):
            with self.subTest(f"{fig}: {old[:25]}"):
                self.fresh()
                stamp_record(self.root, "slew-swing-power", self.SSP_REC, ("power", "slew", "swing"))
                self.edit(rel, old, new)
                with self.assertRaisesRegex(cr.ReportError, rf"slew-swing-power \([^)]*\): changed [^;]*figures\.{fig}\."):
                    cr.build(self.root, self.m)

    def test_ssp_swing_only_record_is_not_invalidated_by_other_figures(self):
        stamp_record(self.root, "slew-swing-power", self.SWING_REC, ("swing",))
        self.edit("sim/slew-swing-power/testbench/tb_slew.spice", "CL vout 0 2p", "CL vout 0 3p")
        self.edit("sim/slew-swing-power/testbench/tb_power.spice", "Ibias vdd ibias dc 10u", "Ibias vdd ibias dc 12u")
        self.edit("sim/slew-swing-power/measurement_config.py", "SLEW_STEP_V = 1.0 ", "SLEW_STEP_V = 0.8 ")
        rep = cr.build(self.root, self.m)
        self.assertEqual(rep["sources"][self.ssp_key(self.SWING_REC)]["measurement_config"]["status"], "current")
        # ... while its own figure's bench still invalidates it
        self.edit("sim/slew-swing-power/testbench/tb_swing.spice", "Rf vout vinn 1Meg", "Rf vout vinn 2Meg")
        with self.assertRaisesRegex(cr.ReportError, r"changed [^;]*figures\.swing\.bench"):
            cr.build(self.root, self.m)

    def test_ssp_swing_only_record_retains_only_swing(self):
        stamp_record(self.root, "slew-swing-power", self.SWING_REC, ("swing",))
        fp = cr.parse_fingerprint((self.root / self.SWING_REC).read_text(), "swing")
        self.assertEqual(sorted(fp["inputs"]["figures"]), ["swing"])

    def test_ssp_formatting_does_not_change_fingerprint(self):
        stamp_record(self.root, "slew-swing-power", self.SSP_REC, ("power", "slew", "swing"))
        p = self.root / "sim/slew-swing-power/testbench/tb_slew.spice"
        p.write_text("* more\n\n" + p.read_text().replace("CL vout 0 2p", "cl  vout  0 2p ; x")
                     .replace("'design.ngspice'", "'/elsewhere/design.ngspice'"))
        rep = cr.build(self.root, self.m)
        self.assertEqual(rep["sources"][self.ssp_key(self.SSP_REC)]["measurement_config"]["status"], "current")

    def test_ssp_archival_discloses_stale(self):
        stamp_record(self.root, "slew-swing-power", self.SSP_REC, ("power", "slew", "swing"))
        self.edit("sim/slew-swing-power/testbench/tb_slew.spice", "CL vout 0 2p", "CL vout 0 3p")
        rep = cr.build(self.root, self.m, archival=True)
        self.assertEqual(rep["sources"][self.ssp_key(self.SSP_REC)]["measurement_config"]["status"], "stale")
        self.assertTrue(any("archival report only" in l and "figures.slew.bench" in l for l in rep["limitations"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
