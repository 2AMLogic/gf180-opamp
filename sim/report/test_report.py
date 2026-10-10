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
ICMR = "sim/input-common-mode"
ICMR_REC = "20261009-222613-871d1a6"
ICMR_MD = f"{ICMR}/records/{ICMR_REC}.md"
ICMR_CSV = f"{ICMR}/corners/{ICMR_REC}/samples.csv"
OFF = "sim/offset-mc"
OFF_GRID_REC = "20261010-083043-ddf96db"  # issue #106: 45-point PVT grid, N=300 per point (selected, issue #120)
OFF_NOM_REC = "20261009-072205-96bf3cc"  # issue #45: 5 corners at 27 C / 3.30 V
OFF_GRID_MD = f"{OFF}/records/{OFF_GRID_REC}.md"
OFF_NOM_MD = f"{OFF}/records/{OFF_NOM_REC}.md"
OFF_GRID_CSV = f"{OFF}/corners/{OFF_GRID_REC}/offset_samples.csv"


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
    # ICMR (issue #90): the retained per-sample evidence and the record addendum are report inputs
    # offset grid (issue #120): its retained per-sample evidence is a report input too
    for p in (list((REPO / ICMR).glob("corners/*/samples.csv")) + list((REPO / ICMR).glob("records/*-addendum/ADDENDUM.md"))
              + list((REPO / OFF).glob("corners/*/offset_samples.csv"))):
        dst = tmp / p.relative_to(REPO)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, dst)
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
        self.assertIn("15.458", r["offset"]["worst"])
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
        self.assertIsNone(r["worst"])
        self.assertEqual(r["source"]["record_id"], ICMR_REC)  # issue #90: measured, source-pinned, still ungraded
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
        # issue #120: the offset row now covers the same 45-point grid as the AC rows
        self.assertEqual(r["offset"]["coverage"]["points"], 45)
        self.assertEqual(r["offset"]["coverage"]["mc_samples_per_point"], 300)
        self.assertEqual(r["offset"]["coverage"]["temps_c"], [-40, 27, 125])
        for k in ("gain", "noise", "cmrr", "psrr", "slew", "swing", "power"):
            self.assertEqual(r[k]["coverage"]["points"], 45, k)
        self.assertFalse(any("coverage differs" in l for l in rep["limitations"]))
        # ... and a nominal offset selection is still disclosed as a coverage difference
        rep2 = cr.build(REPO, manifest(offset_mc=OFF_NOM_MD))
        self.assertTrue(any("coverage differs" in l for l in rep2["limitations"]))
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
        # without the in-row "proposed, not ratified" tag and without an ICMR record, the row is an
        # ordinary unmeasured row
        self.edit("spec/target-spec.md", "[DR-5] — proposed, not ratified**", "[DR-5]**")
        m = manifest(input_common_mode=None)
        r = rows_by_key(self.build(m))["input-common-mode-range"]
        self.assertEqual(r["status"], "not-measured")
        self.assertEqual(self.build(m)["summary"]["proposed_not_graded"], 2)  # CMRR, PSRR still proposed
        # with the ICMR record selected, a ratified ICMR bound is never silently left ungraded (issue #90)
        with self.assertRaisesRegex(cr.ReportError, "no grader for the input common-mode range"):
            self.build()

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

    def test_latest_selection_picks_the_offset_pvt_grid_record(self):
        # issue #120: the full-grid offset record is read by the offset extractor, not a side study
        self.assertNotIn("offset-mc", cr.STUDY_TITLES)
        self.assertEqual(cr.latest_selection(self.root)["offset-mc"], OFF_GRID_MD)




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
        sel = json.loads(MANIFEST.read_text())["experiments"]
        for e, s in rep["sources"].items():
            if e in ("gain-gbw-pm", "offset-mc") and "Measurement fingerprint" in (REPO / sel[e]).read_text():
                self.assertEqual(s["measurement_config"]["status"], "current", e)
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


def stamp_record(root: Path, exp: str, rec_rel: str, figures: tuple | None = None, grid: str | None = None) -> None:
    """Fixture: stamp a copy of a record with the fingerprint header line and
    inputs block its driver would write for the CURRENT configuration in
    `root` (never touches the repo). `figures` selects the measured figures of
    a slew/swing/power record; `grid` overrides the offset grid the inputs name
    (default: "full" for an offset grid record, as its driver writes)."""
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
    if grid is None and text.startswith(cr.OFFSET_GRID_TITLE):
        grid = "full"  # an offset grid record's retained inputs name its grid (issue #106)
    if grid is not None:
        sel = {"grid": grid}
    text = re.sub(r"(?m)^- \*\*Measurement fingerprint\*\*:.*\n", "", text)
    text = re.sub(r"(?ms)^## Measurement fingerprint inputs\n.*?^```\n(\n|\Z)", "", text)
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
    # the selected offset record is the full grid (issue #120): its VCM axis (VDD/2), not TEMP_C
    ("offset-mc", "sim/offset-mc/measurement_config.py", "round(v / 2, 6)", "round(v / 2 + 0.01, 6)", "corners"),
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
        # the ICMR record (issue #90) is not instrumented yet: disclosed as unknown, never "current"
        self.assertEqual(sorted(cr.MEASUREMENT_CONFIG), sorted(e for e in self.sel if e != cr.ICMR_EXP))
        mc = cr.build(REPO, manifest())["sources"][cr.ICMR_EXP]["measurement_config"]
        self.assertEqual(mc["status"], "unknown")
        self.assertIn("not instrumented", mc["detail"])

    def test_committed_legacy_records_stay_unknown(self):
        rep = cr.build(REPO, manifest())
        for e, src in rep["sources"].items():
            if e in ("gain-gbw-pm", "offset-mc", cr.ICMR_EXP):  # ICMR: not instrumented (see above)
                continue
            self.assertEqual(src["measurement_config"]["status"], "unknown", e)
            self.assertIn("predates measurement fingerprinting", src["measurement_config"]["detail"], e)
        # issue #120: the selected offset grid record carries its own fingerprint and is current
        self.assertEqual(rep["sources"]["offset-mc"]["measurement_config"]["status"], "current")
        joined = " ".join(rep["limitations"])
        self.assertNotIn("offset-mc: measurement-configuration freshness unknown", joined)
        for e in ("noise", "cmrr", "psrr"):
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
                    if e not in (exp, "gain-gbw-pm", "offset-mc"):  # both selected records are fingerprinted
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
                    corners = root2 / "sim" / exp / "corners"  # retained evidence follows the record id
                    if (corners / Path(rel).stem).is_dir():
                        shutil.move(corners / Path(rel).stem, corners / "20991231-235959-0000000")
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


class OffsetGridRecord(unittest.TestCase):
    """Issue #120: the committed full-PVT offset Monte Carlo record (issue #106) is selected,
    validated point by point and cross-checked against its retained samples; the nominal
    format stays supported; the row stays measured-no-bound (spec issue #62 owns the bound)."""

    WORST_ROW = "| typical | 27 | 3.30 | 300 | -0.619 | 4.946 | 14.839 | 15.458 | -11.254 | +12.615 | +0.12 | -0.59 |"
    OTHER_ROW = "| ff | 125 | 3.63 | 300 | +0.275 | 4.832 | 14.497 | 14.772 | -12.374 | +11.787 | -0.06 | -0.38 |"

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))

    def tearDown(self):
        self._t.cleanup()

    def edit(self, rel, old, new, count=1):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, count))

    def build(self, m=None):
        return cr.build(self.root, m or manifest())

    def rejects(self, rx, m=None):
        with self.assertRaisesRegex(cr.ReportError, rx):
            self.build(m)

    # ---- the committed row ----

    def test_committed_grid_row(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)["offset"]
        self.assertEqual(json.loads(MANIFEST.read_text())["experiments"]["offset-mc"], OFF_GRID_MD)
        self.assertEqual((r["status"], r["verdict"], r["points_pass"], r["bound_open"]),
                         ("measured-no-bound", None, None, True))
        self.assertEqual(r["points_total"], 45)
        self.assertEqual(r["worst_corner"], "typical / 27 C / 3.30 V")
        self.assertTrue(r["worst"].startswith("|mean| + 3 sigma 15.458 mV"), r["worst"])
        cov = r["coverage"]
        self.assertEqual((cov["points"], cov["mc_samples_per_point"], cov["temps_c"], cov["vdd_v"], cov["corners"]),
                         (45, 300, [-40, 27, 125], ["2.97", "3.30", "3.63"], ["typical", "ff", "ss", "fs", "sf"]))
        self.assertIn("N=300 mismatch samples per point", cr.coverage_text(cov))
        fig = {f["label"]: f for f in r["figures"]}
        ws = fig["worst sigma over the grid"]
        self.assertEqual((ws["value"], ws["corner"]), ("4.946 mV (3 sigma 14.839 mV)", "typical / 27 C / 3.30 V"))
        self.assertEqual(fig["sigma range over the 45 points, N=300 each"]["value"], "4.286 .. 4.946 mV")
        self.assertEqual(sum(1 for l in fig if l.startswith("worst |mean| + 3 sigma at ")), 9)
        self.assertIn("13500 samples at 45 points agree", fig["per-point statistics re-derived from the retained samples"]["value"])
        self.assertTrue(any("#62" in l for l in r["limitations"]))
        src = rep["sources"]["offset-mc"]
        self.assertEqual((src["record_id"], src["measurement_config"]["status"]), (OFF_GRID_REC, "current"))
        self.assertEqual(r["source"]["sha256"], hashlib.sha256((REPO / OFF_GRID_MD).read_bytes()).hexdigest())
        md = cr.render_md(rep)
        line = next(l for l in md.splitlines() if l.startswith("| Input-referred offset |"))
        self.assertEqual(len(cr.split_cells(line.replace("\\|", "/"))), 8, line)  # escaped |mean| keeps 8 columns
        self.assertIn("\\|mean\\| + 3 sigma 15.458 mV", line)
        self.assertNotRegex(line, r"\*\*(PASS|FAIL)\*\*")

    def test_statistics_match_the_driver(self):
        # the report re-implements the driver's stats_of (no import of a simulation driver); pin them together
        sys.path.insert(0, str(REPO / "sim" / "offset-mc"))
        try:
            import run_offset_mc as drv
        finally:
            sys.path.pop(0)
        import csv
        by: dict = {}
        with (REPO / OFF_GRID_CSV).open(newline="") as fh:
            for row in csv.DictReader(fh):
                by.setdefault((row["corner"], row["temperature_c"], row["vdd_v"]), []).append(float(row["offset_v"]))
        for k in list(by)[:5] + list(by)[-2:]:
            a, b = cr.offset_stats(by[k]), drv.stats_of(by[k])
            for mine, theirs in (("mean", b.mean * 1e3), ("sigma", b.sigma * 1e3), ("abs3", b.worst_extreme * 1e3),
                                 ("min", b.vmin * 1e3), ("max", b.vmax * 1e3), ("skew", b.skew), ("ex_kurt", b.ex_kurt)):
                self.assertAlmostEqual(a[mine], theirs, places=9, msg=f"{k} {mine}")

    def test_nominal_format_still_supported(self):
        rep = self.build(manifest(offset_mc=OFF_NOM_MD))
        r = rows_by_key(rep)["offset"]
        self.assertEqual((r["status"], r["verdict"], r["points_total"]), ("measured-no-bound", None, 5))
        self.assertEqual(r["worst"], "sigma 5.006 mV (3 sigma 15.017 mV)")
        self.assertEqual(r["worst_corner"], "sf (corner only; 27 C / 3.30 V)")
        self.assertIn({"label": "worst |mean| + 3 sigma", "value": "15.635 mV", "corner": "sf"}, r["figures"])
        self.assertEqual(r["coverage"]["mc_samples_per_corner"], 300)
        self.assertEqual(rep["sources"]["offset-mc"]["measurement_config"]["status"], "unknown")

    def test_unknown_offset_format_rejected(self):
        self.edit(OFF_GRID_MD, "# Offset Monte Carlo PVT grid record", "# Offset Monte Carlo sweep")
        self.rejects(r"unknown offset record format")

    # ---- point set and sample count ----

    def test_missing_point_rejected(self):
        self.edit(OFF_GRID_MD, self.OTHER_ROW + "\n", "")
        self.rejects(r"per-point table is missing 1 of 45 grid points \(first: ff / 125 C / 3.63 V\)")

    def test_duplicate_point_rejected(self):
        self.edit(OFF_GRID_MD, self.OTHER_ROW + "\n", self.OTHER_ROW + "\n" + self.OTHER_ROW + "\n")
        self.rejects(r"duplicate grid point ff / 125 C / 3.63 V")

    def test_unexpected_point_rejected(self):
        self.edit(OFF_GRID_MD, self.OTHER_ROW, self.OTHER_ROW.replace("| ff | 125 | 3.63 |", "| ff | 85 | 3.63 |"))
        self.rejects(r"unexpected grid point ff / 85 C / 3.63 V")

    def test_insufficient_samples_in_table_rejected(self):
        self.edit(OFF_GRID_MD, self.OTHER_ROW, self.OTHER_ROW.replace("| 300 |", "| 299 |"))
        self.rejects(r"insufficient samples at ff / 125 C / 3.63 V: N=299")

    def test_insufficient_samples_in_retained_evidence_rejected(self):
        p = self.root / OFF_GRID_CSV
        lines = p.read_text().splitlines(keepends=True)
        drop = next(i for i, l in enumerate(lines) if l.startswith("sf,125.0,3.63,"))
        p.write_text("".join(lines[:drop] + lines[drop + 1:]))
        self.rejects(r"insufficient samples in .*offset_samples\.csv at sf / 125 C / 3\.63 V: 299")

    def test_request_below_the_ratified_sample_count_rejected(self):
        # a grid measured at N=200 (fingerprint-consistent) still fails the N >= 300 basis
        self.edit(f"{OFF}/measurement_config.py", "MC_N = 300", "MC_N = 200")
        stamp_record(self.root, "offset-mc", OFF_GRID_MD)
        self.rejects(r"insufficient samples: .*N=200 per point.*N >= 300")

    def test_duplicate_sample_index_rejected(self):
        p = self.root / OFF_GRID_CSV
        lines = p.read_text().splitlines(keepends=True)
        i = next(i for i, l in enumerate(lines) if l.startswith("ss,27.0,2.97,1.485,7,"))
        lines[i + 1] = re.sub(r"^(ss,27\.0,2\.97,1\.485,)8,", r"\g<1>7,", lines[i + 1])
        p.write_text("".join(lines))
        self.rejects(r"duplicate sample index 7 at ss / 27 C / 2\.97 V")

    def test_missing_retained_evidence_rejected(self):
        (self.root / OFF_GRID_CSV).unlink()
        self.rejects(r"retained per-sample evidence is missing: sim/offset-mc/corners/20261010-083043-ddf96db")

    # ---- summaries vs each other and vs the samples ----

    def test_internally_inconsistent_summary_rejected(self):
        self.edit(OFF_GRID_MD, self.WORST_ROW, self.WORST_ROW.replace("| 4.946 |", "| 4.996 |"))
        self.rejects(r"inconsistent summary at typical / 27 C / 3\.30 V")

    def test_summary_disagreeing_with_samples_rejected(self):
        # self-consistent columns, but the skew disagrees with the committed samples
        self.edit(OFF_GRID_MD, self.OTHER_ROW, self.OTHER_ROW.replace("| -0.06 |", "| +0.06 |"))
        self.rejects(r"inconsistent summary at ff / 125 C / 3\.63 V: the record's skew .*retained evidence")

    def test_tampered_sample_rejected(self):
        p = self.root / OFF_GRID_CSV
        lines = p.read_text().splitlines(keepends=True)
        i = next(i for i, l in enumerate(lines) if l.startswith("fs,-40.0,3.3,1.65,0,"))
        cells = lines[i].rstrip("\n").split(",")
        cells[-1] = repr(float(cells[-1]) + 0.002)
        lines[i] = ",".join(cells) + "\n"
        p.write_text("".join(lines))
        self.rejects(r"inconsistent summary at fs / -40 C / 3\.30 V: .*retained evidence")

    def test_wrong_vcm_in_samples_rejected(self):
        self.edit(OFF_GRID_CSV, "typical,-40.0,2.97,1.485,0,", "typical,-40.0,2.97,1.65,0,")
        self.rejects(r"invalid sample 0 at typical / -40 C / 2\.97 V .*not the commanded 1\.485 V")

    def test_headline_worst_disagreeing_rejected(self):
        self.edit(OFF_GRID_MD, "**15.458 mV** at `typical / 27 C / 3.30 V`", "**15.458 mV** at `sf / 27 C / 3.30 V`")
        self.rejects(r"headline worst \|mean\| \+ 3 sigma")
        self.tearDown(); self.setUp()
        self.edit(OFF_GRID_MD, "**Worst sigma over the grid**: 4.946 mV", "**Worst sigma over the grid**: 4.938 mV")
        self.rejects(r"headline worst sigma")

    def test_temperature_supply_table_disagreeing_rejected(self):
        self.edit(OFF_GRID_MD, "| 27 C | 14.843 (fs) | 15.458 (typical) |", "| 27 C | 14.843 (fs) | 15.458 (ff) |")
        self.rejects(r"temperature x supply table at 27 C / 3\.30 V")

    def test_validation_problems_rejected(self):
        self.edit(OFF_GRID_MD, "Result: all 13500 samples valid;", "**Problems:**\n- something;")
        self.rejects(r"extraction validation does not state all 13500 samples valid")

    # ---- freshness ----

    def test_grid_record_without_fingerprint_rejected(self):
        p = self.root / OFF_GRID_MD
        p.write_text(re.sub(r"(?m)^- \*\*Measurement fingerprint\*\*:.*\n", "", p.read_text()))
        self.rejects(r"carries no measurement fingerprint")

    def test_nominal_inputs_on_a_grid_record_rejected(self):
        # hash-consistent nominal inputs on a grid record: the population would be mislabelled
        stamp_record(self.root, "offset-mc", OFF_GRID_MD, grid="nominal")
        self.rejects(r"do not describe a full offset grid")

    def test_stale_grid_configuration_rejected_and_archival_discloses(self):
        self.edit(f"{OFF}/measurement_config.py", "round(v / 2, 6)", "round(v / 2 + 0.01, 6)")
        self.rejects(r"stale measurement configuration.*offset-mc: changed corners\.vcm_v.*needing a rerun: offset-mc")
        rep = cr.build(self.root, manifest(), archival=True)
        self.assertEqual(rep["sources"]["offset-mc"]["measurement_config"]["status"], "stale")

    def test_stale_dut_names_offset(self):
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        self.rejects(r"stale DUT.*Experiments needing a rerun: .*offset-mc")

    def test_mixed_dut_rejected(self):
        self.edit(OFF_GRID_MD, "81fbd914f8254a49", "0123456789abcdef")
        self.rejects(r"different DUT versions")


class ICMREvidence(unittest.TestCase):
    """Issue #90: the committed ICMR record is selected, extracted and cross-checked against its
    retained per-sample evidence, and attached to the proposed row without grading it."""

    PT = "ss / -40 C / 2.97 V"
    NEW = "29991231-235959-0000000"

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))

    def tearDown(self):
        self._t.cleanup()

    def edit(self, rel, old, new, count=1):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, count))

    def build(self, m=None):
        return cr.build(self.root, m or manifest())

    # ---- the committed row ----

    def test_committed_row_is_structured_measured_and_ungraded(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)[cr.ICMR_ROW_KEY]
        self.assertEqual((r["status"], r["verdict"], r["worst"], r["worst_corner"], r["points_total"], r["points_pass"]),
                         ("proposed-not-graded", None, None, None, None, None))
        self.assertTrue(r["spec_bound"].startswith("proposed, not ratified: "))
        src = rep["sources"][cr.ICMR_EXP]
        self.assertEqual(src["path"], ICMR_MD)
        self.assertEqual(src["sha256"], hashlib.sha256((REPO / ICMR_MD).read_bytes()).hexdigest())
        self.assertEqual(r["source"]["sha256"], src["sha256"])
        self.assertEqual(src["dut_sha256"], "81fbd914f8254a49")
        self.assertEqual(src["pdk_open_pdks"], "c6d73a35f524070e85faff4a6a9eef49553ebc2b")
        self.assertEqual(r["coverage"]["points"], 45)
        ic = r["icmr"]
        self.assertFalse(ic["graded"])
        self.assertEqual([(c["low_mv"], c["high_mv"]) for c in ic["common_interval_components"]], [(1185, 2705)])
        comp = ic["component_containing_1v20"]
        self.assertEqual((comp["low_binding_point"], comp["high_binding_point"]), (self.PT, "fs / 125 C / 2.97 V"))
        self.assertEqual((comp["low_bracket_mv"], comp["high_bracket_mv"]), (5, 5))
        self.assertEqual(ic["transition_resolution"]["max_endpoint_bracket_mv"], 5)
        self.assertEqual(ic["explicit_1v20"], {"verdict_in_record": "meets", "points": 45, "pass": 45, "fail": 0,
                                               "invalid": 0})
        sm = ic["smallest_saturation_margin"]
        self.assertEqual((sm["at_1v20"]["device"], sm["at_1v20"]["margin_mv"], sm["at_1v20"]["point"]),
                         ("XM5", 11.1, self.PT))
        self.assertEqual((sm["inside_component"]["margin_mv"], sm["inside_component"]["vcm_mv"]), (0.6, 1185))
        self.assertEqual(len(ic["per_point_intervals"]), 45)
        self.assertEqual(ic["samples"]["total"], 6165)
        rv = ic["retained_evidence"]
        self.assertEqual((rv["path"], rv["samples"], rv["points"]), (ICMR_CSV, 6165, 45))
        self.assertEqual(rv["sha256"], hashlib.sha256((REPO / ICMR_CSV).read_bytes()).hexdigest())
        self.assertEqual(ic["provenance"]["record_sha256"], src["sha256"])
        self.assertTrue(r["figures"])
        self.assertTrue(all("information only" in f["label"] for f in r["figures"]))
        joined = " ".join(r["limitations"])
        for frag in ("not ratified", "follower-biased", "mismatch not covered", "freshness unknown", "addendum"):
            self.assertIn(frag, joined)
        # excluded from ratified compliance counts
        s = rep["summary"]
        self.assertEqual((s["ratified_rows_judged"], s["proposed_not_graded"]), (6, 3))
        md = cr.render_md(rep)
        line = next(l for l in md.splitlines() if l.startswith("| Input common-mode range |"))
        cells = [c.strip() for c in line.strip("|").split("|")]
        self.assertEqual(cells[2:7], ["proposed-not-graded", "none", "-", "-", "-"])
        self.assertIn(ICMR_REC, cells[7])
        self.assertNotIn("1.185", line)
        self.assertIn(f"`{ICMR}/corners/{ICMR_REC}/samples.csv`", md)

    def test_latest_selection_includes_icmr(self):
        self.assertEqual(cr.latest_selection(REPO)[cr.ICMR_EXP], ICMR_MD)
        self.assertEqual(json.loads(MANIFEST.read_text())["experiments"][cr.ICMR_EXP], ICMR_MD)

    # ---- missing evidence ----

    def test_missing_icmr_evidence_stays_explicitly_missing(self):
        rep = self.build(manifest(input_common_mode=None))
        r = rows_by_key(rep)[cr.ICMR_ROW_KEY]
        self.assertEqual(r["status"], "proposed-not-graded")
        self.assertIsNone(r["source"])
        self.assertIsNone(r["coverage"])
        self.assertEqual(r["figures"], [])
        self.assertNotIn("icmr", r)
        self.assertNotIn(cr.ICMR_EXP, rep["sources"])
        self.assertTrue(any("explicitly missing" in l for l in r["limitations"]))
        self.assertTrue(any(l.startswith(f"no record selected for {cr.ICMR_EXP}") and "explicitly missing" in l
                            for l in rep["limitations"]))
        # the spec prose cites a record and numbers; none of it becomes a measurement
        self.assertIn("1.185", r["spec_status"])
        self.assertNotIn("1.185", json.dumps(r["figures"]))
        self.assertEqual(rep["summary"]["proposed_not_graded"], 3)

    def test_missing_retained_evidence_rejected(self):
        (self.root / ICMR_CSV).unlink()
        with self.assertRaisesRegex(cr.ReportError, "retained per-point evidence is missing"):
            self.build()

    # ---- stale DUT ----

    def test_stale_icmr_dut_rejected(self):
        only = {"experiments": {cr.ICMR_EXP: ICMR_MD}}
        self.edit(ICMR_MD, "`81fbd914f8254a49`", "`0123456789abcdef`")
        with self.assertRaisesRegex(cr.ReportError, r"stale DUT.*Experiments needing a rerun: input-common-mode"):
            self.build(only)
        with self.assertRaisesRegex(cr.ReportError, "different DUT versions"):
            self.build()

    def test_netlist_change_names_icmr_for_rerun(self):
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        with self.assertRaisesRegex(cr.ReportError, r"stale DUT.*rerun: [^.]*input-common-mode"):
            self.build()

    # ---- superseded / newer selection ----

    def mint_newer(self, supersedes: bool) -> str:
        """Fixture: a newer copy of the ICMR record (and its retained samples) under a new id."""
        text = (self.root / ICMR_MD).read_text().replace(ICMR_REC, self.NEW)
        if supersedes:
            text = re.sub(r"(?m)^(- \*\*DUT\*\*:.*\n)", lambda m: m.group(1) + f"- **Supersedes**: `{ICMR_REC}`\n",
                          text, count=1)
        rel = f"{ICMR}/records/{self.NEW}.md"
        (self.root / rel).write_text(text)
        dst = self.root / ICMR / "corners" / self.NEW / "samples.csv"
        dst.parent.mkdir(parents=True)
        shutil.copy(self.root / ICMR_CSV, dst)
        return rel

    def test_superseded_icmr_selection_rejected(self):
        self.mint_newer(supersedes=True)
        with self.assertRaisesRegex(cr.ReportError, f"superseded by {self.NEW}"):
            self.build()

    def test_latest_selects_newly_minted_icmr_record(self):
        rel = self.mint_newer(supersedes=True)
        sel = cr.latest_selection(self.root)
        self.assertEqual(sel[cr.ICMR_EXP], rel)
        rep = cr.build(self.root, {"experiments": sel})
        self.assertEqual(rows_by_key(rep)[cr.ICMR_ROW_KEY]["source"]["record_id"], self.NEW)
        self.assertEqual(rep["sources"][cr.ICMR_EXP]["record_id"], self.NEW)

    # ---- malformed / inconsistent interval data ----

    CASES = (
        # (what, old, new, expected error)
        ("headline intersection", "(every contiguous component): [1.185, 2.705] V.",
         "(every contiguous component): [1.180, 2.705] V.", "headline intersection .* disagrees"),
        ("headline flattens a gap", "(every contiguous component): [1.185, 2.705] V.",
         "(every contiguous component): [1.185, 2.400] V, [2.500, 2.705] V.", "headline intersection .* disagrees"),
        ("per-point edge", "| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 |", "| ss / -40 C / 2.97 V | [1.190, 2.785] V | 52 |",
         "disagrees with its own per-point table"),
        ("malformed interval", "| ss / -40 C / 2.97 V | [1.185, 2.785] V |", "| ss / -40 C / 2.97 V | [1.185 .. 2.785] V |",
         "malformed interval"),
        ("low above high", "| ss / -40 C / 2.97 V | [1.185, 2.785] V |", "| ss / -40 C / 2.97 V | [2.785, 1.185] V |",
         "low > high"),
        ("overlapping components", "| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 | 5 mV | fail | 5 mV | fail | pass | [1.185, 2.785] V |",
         ("| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 | 5 mV | fail | 5 mV | fail | pass | [1.185, 2.785] V |\n"
          "| ss / -40 C / 2.97 V | [2.700, 2.900] V | 3 | 5 mV | fail | 5 mV | fail | pass | [1.185, 2.785] V |"),
         "overlap"),
        ("1.20 V status vs interval", "| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 | 5 mV | fail | 5 mV | fail | pass |",
         "| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 | 5 mV | fail | 5 mV | fail | fail |", "1.20 V status"),
        ("bracket", "| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 | 5 mV |", "| ss / -40 C / 2.97 V | [1.185, 2.785] V | 52 | 50 mV |",
         "(exceeds the record's stated refinement|disagrees)"),
        ("binding corner", "low endpoint 1.185 V set by ss / -40 C / 2.97 V", "low endpoint 1.185 V set by ss / 27 C / 2.97 V",
         "binding corners"),
        ("1.20 V count", "**MEETS** (45 pass, 0 fail", "**MEETS** (44 pass, 1 fail", "1.20 V sample line disagrees"),
        ("1.20 V margin", "Smallest device margin at 1.20 V: XM5 +11.1 mV", "Smallest device margin at 1.20 V: XM5 +12.1 mV",
         "smallest 1.20 V margin"),
        ("component margin", "Smallest device margin inside that component: XM5 +0.6 mV",
         "Smallest device margin inside that component: XM5 +1.6 mV", "retained evidence"),
        ("sample counts", "3450 pass, 2245 fail, 470 invalid.", "3451 pass, 2244 fail, 470 invalid.", "retained evidence|holds"),
        ("missing point", "| typical / 27 C / 3.30 V | [1.060, 3.095] V | 84 | 5 mV | fail | 5 mV | fail | pass | [1.060, 3.090] V |\n",
         "", "claims 45 PVT points|explicit 1.20 V table covers"),
    )

    def test_malformed_or_inconsistent_interval_data_rejected(self):
        for what, old, new, err in self.CASES:
            with self.subTest(what):
                self.tearDown(); self.setUp()
                self.edit(ICMR_MD, old, new)
                with self.assertRaisesRegex(cr.ReportError, err):
                    self.build()

    def test_record_disagreeing_with_retained_samples_rejected(self):
        p = self.root / ICMR_CSV
        lines = p.read_text().splitlines(keepends=True)
        i = next(n for n, l in enumerate(lines) if l.startswith("typical,27,3.300,2.000,pass,"))
        lines[i] = lines[i].replace("typical,27,3.300,2.000,pass,pass,", "typical,27,3.300,2.000,fail,fail,", 1)
        # keep the header counts consistent, so only the per-point intervals can disagree
        j = next(n for n, l in enumerate(lines) if l.startswith("typical,27,3.300,0.600,fail,fail,"))
        lines[j] = lines[j].replace("typical,27,3.300,0.600,fail,fail,", "typical,27,3.300,0.600,pass,pass,", 1)
        p.write_text("".join(lines))
        with self.assertRaisesRegex(cr.ReportError, r"typical / 27 C / 3\.30 V disagree with the retained evidence"):
            self.build()

    def test_malformed_retained_samples_rejected(self):
        p = self.root / ICMR_CSV
        t = p.read_text()
        first = t.splitlines()[1]
        p.write_text(t + first + "\n")  # duplicate sample
        with self.assertRaisesRegex(cr.ReportError, "duplicate sample"):
            self.build()

    def test_disjoint_intervals_preserved_not_flattened(self):
        """A point whose passing range splits yields two common components, never one bridged range."""
        old_row = "| typical / 27 C / 3.30 V | [1.060, 3.095] V | 84 | 5 mV | fail | 5 mV | fail | pass | [1.060, 3.090] V |"
        split = ("| typical / 27 C / 3.30 V | [1.060, 2.400] V | 60 | 5 mV | fail | 5 mV | fail | pass | [1.060, 3.090] V |\n"
                 "| typical / 27 C / 3.30 V | [2.500, 3.095] V | 24 | 5 mV | fail | 5 mV | fail | pass | [1.060, 3.090] V |")
        text = (REPO / ICMR_MD).read_text()
        self.assertIn(old_row, text)
        text = text.replace(old_row, split)
        # the record's own headline still claims one range: rejected rather than flattened
        with self.assertRaisesRegex(cr.ReportError, "headline intersection .* disagrees"):
            cr.extract_icmr(text, "fixture")
        text = (text.replace("(every contiguous component): [1.185, 2.705] V.",
                             "(every contiguous component): [1.185, 2.400] V, [2.500, 2.705] V.")
                .replace("Intersection with the 1 mV tolerance: [1.185, 2.705] V;",
                         "Intersection with the 1 mV tolerance: [1.185, 2.400] V, [2.500, 2.705] V;")
                .replace("**Component containing 1.20 V**: [1.185, 2.705] V;", "**Component containing 1.20 V**: [1.185, 2.400] V;")
                .replace("high endpoint 2.705 V set by fs / 125 C / 2.97 V", "high endpoint 2.400 V set by typical / 27 C / 3.30 V")
                .replace("at fs / 125 C / 2.97 V / VCM 2.705 V.", "at fs / 125 C / 2.97 V / VCM 2.400 V.")
                .replace("Interval endpoints (of 90)", "Interval endpoints (of 92)"))
        ex = cr.extract_icmr(text, "fixture")
        self.assertEqual([(c["lo"], c["hi"]) for c in ex["intersection"]], [(1185, 2400), (2500, 2705)])
        self.assertEqual((ex["component"]["lo"], ex["component"]["hi"]), (1185, 2400))
        self.assertEqual(ex["component"]["hi_src"], "typical / 27 C / 3.30 V")
        ex["retained"] = {"path": ICMR_CSV}
        figs = cr.icmr_figures(ex)

        def fig(prefix):
            return next(f["value"] for f in figs if f["label"].startswith(prefix))
        self.assertEqual(fig("conservative common interval over all 45 PVT points"), "[1.185, 2.400] V, [2.500, 2.705] V")
        self.assertTrue(fig("PVT points with disjoint passing intervals").startswith("1 of 45: typical / 27 C / 3.30 V"))

    def test_generation_is_offline_and_deterministic_with_icmr(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        args = ["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]
        self.assertEqual(cr.main(args), 0)
        first = (self.root / "o" / f"{cr.OUT_NAME}.json").read_bytes()
        self.assertEqual(cr.main(args + ["--check"]), 0)
        self.assertEqual(cr.main(args), 0)
        self.assertEqual(first, (self.root / "o" / f"{cr.OUT_NAME}.json").read_bytes())
        self.assertNotIn(str(self.root), first.decode())


if __name__ == "__main__":
    unittest.main(verbosity=1)
