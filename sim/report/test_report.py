#!/usr/bin/env python3
"""Tests for the characterization report generator (issue #50). No simulator.

Every mutation test works on a throw-away copy of the committed records and
spec (never the originals).

    python3 sim/report/test_report.py          # or: python3 -m unittest
"""
from __future__ import annotations

import hashlib
import json
import math
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
FULL_SSP_REC = "20261010-161852-0127eb7"  # power + slew + swing, swing re-derivable (selected, issue #42 retune)
SWING_REC = FULL_SSP_REC  # the revised-design run judges all three figures in one record
LEGACY_FULL_SSP_REC = "20261009-142137-1dab1db"  # pre-retune DUT: swing data vin/vout only (not re-derivable)
LEGACY_SWING_REC = "20261009-143715-4d5aa43"  # pre-retune DUT: swing only, with the M6/M7 saturation vectors
LEGACY_RECS = {"noise": "sim/noise/records/20261009-082007-68b4567.md", "cmrr": "sim/cmrr/records/20261009-105631-30ec86d.md",
               "psrr": "sim/psrr/records/20261009-105929-30ec86d.md"}  # pre-retune, unfingerprinted
SSP = "sim/slew-swing-power/records/"
GMC = "sim/gain-gbw-pm-mc"
GMC_REC = "20261011-072005-9c9b0f1"  # issue #129 record, selected by issue #172
GMC_MD = f"{GMC}/records/{GMC_REC}.md"
GMC_CSV = f"{GMC}/corners/{GMC_REC}/samples.csv"
ICMR = "sim/input-common-mode"
ICMR_REC = "20261010-164310-0127eb7"
ICMR_MD = f"{ICMR}/records/{ICMR_REC}.md"
ICMR_CSV = f"{ICMR}/corners/{ICMR_REC}/samples.csv"
OFF = "sim/offset-mc"
CMC = "sim/cmrr-mc"
CMC_REC = "20261010-202454-e4be1ac"  # issue #61 record, selected by issue #124
CMC_MD = f"{CMC}/records/{CMC_REC}.md"
CMC_CSV = f"{CMC}/corners/{CMC_REC}/samples.csv"
PMC = "sim/psrr-mc"
PMC_REC = "20261011-145434-286b06c"  # issue #99 record, selected by issue #182
PMC_MD = f"{PMC}/records/{PMC_REC}.md"
PMC_CSV = f"{PMC}/corners/{PMC_REC}/samples.csv"
OFF_GRID_REC = "20261010-185141-e4be1ac"  # issue #106: 45-point PVT grid, N=300 per point (selected, issue #120)
OFF_NOM_REC = "20261009-072205-96bf3cc"  # issue #45: 5 corners at 27 C / 3.30 V
OFF_GRID_MD = f"{OFF}/records/{OFF_GRID_REC}.md"
OFF_NOM_MD = f"{OFF}/records/{OFF_NOM_REC}.md"
OFF_GRID_CSV = f"{OFF}/corners/{OFF_GRID_REC}/offset_samples.csv"
GAIN_STUDY_MD = "sim/gain-gbw-pm/records/20261010-161720-0127eb7.md"  # issue #70 side study (supplementary, #121)
SSP_STUDY_MD = "sim/slew-swing-power/records/20261010-162823-0127eb7.md"  # issue #97 side study (supplementary, #121)
PSRR = "sim/psrr"
PSRR_STUDY_REC = "20261011-102729-6612014"  # issue #155 full RZ x CC x PVT study (bound-free supplementary, #173)
PSRR_STUDY_MD = f"{PSRR}/records/{PSRR_STUDY_REC}.md"
PSRR_STUDY_DIR = f"{PSRR}/corners/{PSRR_STUDY_REC}"
STEP = "sim/step-response"
STEP_REC = "20261011-140956-cb03235"  # issue #113 retuned follower step record (ungraded supplementary, #198)
STEP_PRE_RETUNE_REC = "20261010-082214-a2cc91f"  # issue #113 first record: pre-retune DUT
STEP_MD = f"{STEP}/records/{STEP_REC}.md"
STEP_DIR = f"{STEP}/corners/{STEP_REC}"
STEP_FIX_DAT = f"{STEP_DIR}/typical_27c_3.30v.dat"  # not-settled fixture point


def make_root(tmp: Path) -> Path:
    """Copy the committed record Markdown + spec into a scratch repo root."""
    for p in (REPO / "sim").glob("*/records/*.md"):
        dst = tmp / p.relative_to(REPO)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, dst)
    net = tmp / "design" / "netlist"
    net.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "design" / "netlist" / "opamp_two_stage.spice", net / "opamp_two_stage.spice")
    # psrr/measurement_config.py loads the shared passive helpers (issue #155)
    shutil.copy(REPO / "sim" / "passive_corners.py", tmp / "sim" / "passive_corners.py")
    for exp in ("gain-gbw-pm", "gain-gbw-pm-mc", "noise", "offset-mc", "cmrr", "cmrr-mc", "psrr", "psrr-mc", "slew-swing-power",
                "input-common-mode",
                "step-response"):  # step-response: the supplementary follower study's fingerprint source (issue #198)
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
              + list((REPO / OFF).glob("corners/*/offset_samples.csv"))
              + list((REPO / CMC).glob("corners/*/samples.csv"))  # CMRR mismatch MC (issue #124)
              + list((REPO / PMC).glob("corners/*/samples.csv"))  # PSRR mismatch MC (issue #182)
              # full PSRR passive study (issue #173): its retained per-cell curves and partition membership
              + [q for d in (REPO / PSRR).glob("corners/*/partitions.json")
                 for q in [d, *d.parent.glob("*.psrr.dat")]]
              + list((REPO / GMC).glob("corners/*/samples.csv"))  # gain/GBW/PM mismatch MC (issue #172)
              # follower step-response study (issue #198): its retained per-point transients
              + list((REPO / "sim/step-response").glob("corners/*/*.dat"))):
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

    def test_passive_studies_cited_only_for_their_rows(self):
        """Issues #95/#121: typical-passive limitation scoped per record; each selected side study is
        cited (derived, not hard-coded) for its own rows only."""
        rep = json.loads((REPO / cr.OUT_DIR_REL / f"{cr.OUT_NAME}.json").read_text())
        rows = rows_by_key(rep)
        gain_id, ssp_id = Path(GAIN_STUDY_MD).stem, Path(SSP_STUDY_MD).stem
        for k in ("gain", "gbw", "pm"):
            lim = " ".join(rows[k]["limitations"])
            self.assertIn(gain_id, lim)
            self.assertNotIn(ssp_id, lim)
            self.assertNotIn("are not swept", lim)
        gbw = " ".join(rows["gbw"]["limitations"])
        for frag in ("27/27 cells pass, 0 fail", "10.726"):
            self.assertIn(frag, gbw)
        self.assertIn("27/27 cells pass, 0 fail; range 64.47 .. 84.93 deg", " ".join(rows["pm"]["limitations"]))
        for k in ("slew", "swing", "power"):
            lim = " ".join(rows[k]["limitations"])
            self.assertIn("passives at typical only", lim)
            self.assertIn(ssp_id, lim)
            self.assertNotIn(gain_id, lim)
        self.assertIn("worst 14.25 V/us at ss / 125 C / 2.97 V, RZ best, CC worst", " ".join(rows["slew"]["limitations"]))
        noise = " ".join(rows["noise"]["limitations"])
        self.assertIn("passives at typical only", noise)
        self.assertNotIn(gain_id, noise)
        self.assertNotIn(ssp_id, noise)
        self.assertEqual(rows["gbw"]["verdict"], "PASS")
        self.assertEqual(rows["pm"]["verdict"], "PASS")

    def test_md_links_resolve_from_report_dir(self):
        """Issue #95 review: md links (incl. the passive studies) resolve relative to sim/reports/."""
        md_path = REPO / cr.OUT_DIR_REL / f"{cr.OUT_NAME}.md"
        md = md_path.read_text()
        for rel in (GAIN_STUDY_MD, SSP_STUDY_MD):
            study_link = os.path.relpath(rel, cr.OUT_DIR_REL).replace(os.sep, "/")
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
        # honest wrapper: the sidecar mirrors the report's own summary (any failing row stays visible
        # in the report, not hidden by 'pass'; after the issue #42 retune the committed report has none)
        rep = json.loads(raw)
        verdicts = [x.get("verdict") for x in rep["rows"] if x["status"] == "measured-verdict"]
        self.assertEqual(rep["summary"]["fail"], verdicts.count("FAIL"))
        self.assertEqual(rep["summary"]["pass"], verdicts.count("PASS"))
        self.assertEqual(rep["summary"]["ratified_rows_judged"], len(verdicts))
        self.assertGreater(len(verdicts), 0)
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
        self.assertEqual((r["pm"]["verdict"], r["pm"]["points_pass"], r["pm"]["points_total"]), ("PASS", 45, 45))
        self.assertEqual(r["pm"]["worst"], "71.99 deg")
        self.assertEqual(r["pm"]["worst_corner"], "fs / 125 C / 2.97 V")
        self.assertEqual((r["gain"]["verdict"], r["gain"]["worst"]), ("PASS", "93.79 dB"))
        self.assertEqual((r["gbw"]["verdict"], r["gbw"]["worst"]), ("PASS", "11.910 MHz"))
        self.assertEqual(rep["dut"]["normalised_sha256_prefix"], "33b58dac62c747de")
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["record_id"], "20261010-161514-0127eb7")

    def test_slew_swing_power_rows(self):
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        self.assertEqual((r["slew"]["verdict"], r["slew"]["points_pass"], r["slew"]["worst"], r["slew"]["worst_corner"]),
                         ("PASS", 45, "15.50 V/us", "ss / 125 C / 2.97 V"))
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
        self.assertIn("20261010-164310-0127eb7", r["spec_status"])
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
        self.assertIn("20261010-202454-e4be1ac", r["cmrr"]["spec_status"])  # the mismatch-inclusive record
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
        # issue #124: the CMRR mismatch Monte Carlo record (5 corner points at 27 C / 3.30 V) is a
        # different population from the 45-point AC grids, and the report says so; the gain/GBW/PM
        # mismatch record (#172) covers the same 5-corner population
        self.assertEqual(r["cmrr"]["coverage"]["points"], 45)
        self.assertEqual(r["gain"]["coverage"]["points"], 45)
        self.assertTrue(any("coverage differs" in l for l in rep["limitations"]))
        without = cr.build(REPO, manifest(cmrr_mc=None, gain_gbw_pm_mc=None, psrr_mc=None))
        self.assertFalse(any("coverage differs" in l for l in without["limitations"]))
        # ... and a nominal offset selection is still disclosed as a coverage difference
        # (the committed nominal offset record measures the pre-retune DUT, whose offset is unchanged by
        # RZ/CC; retarget its DUT prefix in a scratch copy so it can be selected beside the current records)
        with tempfile.TemporaryDirectory() as d:
            root2 = make_root(Path(d))
            p2 = root2 / OFF_NOM_MD
            p2.write_text(p2.read_text().replace("81fbd914f8254a49", "33b58dac62c747de"))
            rep2 = cr.build(root2, manifest(offset_mc=OFF_NOM_MD, cmrr_mc=None, gain_gbw_pm_mc=None))
        self.assertTrue(any("coverage differs" in l for l in rep2["limitations"]))
        # the revised-design records were all re-measured at one pinned PDK revision and ngspice, so
        # neither difference is disclosed any more (test_pdk_mismatch_is_flagged_not_fatal covers the flag);
        # the gain/GBW/PM mismatch campaign (#129) ran on the batch fleet with a local ngspice 42
        # beside its klt engine 46: a real cross-source difference, disclosed and named
        self.assertFalse(any("PDK revision differs" in l for l in rep["limitations"]))
        ng = next(l for l in rep["limitations"] if l.startswith("ngspice versions differ"))
        self.assertIn("ngspice 42 <- gain-gbw-pm-mc (local)", ng)
        # the deterministic record's local runs sit in the ngspice-46 group (experiments are listed
        # alphabetically after "ngspice 46 <-", so match inside that group, not on the prefix)
        self.assertIn("gain-gbw-pm (local)", ng.split("ngspice 46 <-", 1)[1])
        for s in rep["sources"].values():
            self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$")

    def test_generator_never_runs_a_simulator(self):
        src = (HERE / "characterization_report.py").read_text()
        for bad in ("subprocess", "os.system", "import socket", "urllib", "import requests"):
            self.assertNotIn(bad, src, bad)
        self.assertNotIn("ngspice -b", src)

GAIN = "sim/gain-gbw-pm/records/"
GAIN_REC = "20261010-161514-0127eb7"  # selected 45-point grid record (issue #168 fixtures)
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
        with self.assertRaisesRegex(cr.ReportError, r"stale DUT.*Experiments needing a rerun: cmrr, cmrr-mc, gain-gbw-pm"):
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
        (self.root / "sim/noise/records/20261010-163507-0127eb7.md").unlink()
        with self.assertRaisesRegex(cr.ReportError, "selected record is missing"):
            self.build()

    def test_mixed_dut_hash_rejected(self):
        self.edit("sim/cmrr/records/20261010-163601-0127eb7.md", "33b58dac62c747de", "0123456789abcdef")
        with self.assertRaisesRegex(cr.ReportError, "different DUT versions"):
            self.build()

    def test_mixed_dut_hash_cli_exit_code(self):
        self.edit("sim/psrr/records/20261010-163854-0127eb7.md", "33b58dac62c747de", "0123456789abcdef")
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
        self.assertEqual(r["pm"]["verdict"], "PASS")
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
        self.edit("sim/gain-gbw-pm/records/20261010-161514-0127eb7.md", "| **PASS** | 45/45 | 71.99 deg", "| **PASS** | 44/45 | 71.99 deg")
        with self.assertRaisesRegex(cr.ReportError, "disagrees with its own per-point table"):
            self.build()

    def test_gain_verdict_token_flip_rejected(self):
        # counts and worst value still agree; only the PASS token is flipped (issue #168)
        self.edit(f"{GAIN}{GAIN_REC}.md", "| **PASS** | 45/45 | 93.79 dB", "| **FAIL** | 45/45 | 93.79 dB")
        with self.assertRaisesRegex(cr.ReportError, "verdict token FAIL disagrees"):
            self.build()

    def test_gain_false_pass_with_failing_point_rejected(self):
        # one PM point drops below 60 deg and the count/worst are fixed up to match,
        # so the only remaining contradiction is the false PASS token
        self.edit(f"{GAIN}{GAIN_REC}.md",
                  "| `fs` | 125 | 2.97 | 94.10 | 12.726 | 71.99 | ok | ok | ok | ok | |",
                  "| `fs` | 125 | 2.97 | 94.10 | 12.726 | 59.99 | ok | ok | ok | FAIL | |")
        self.edit(f"{GAIN}{GAIN_REC}.md", "| **PASS** | 45/45 | 71.99 deg | fs / 125 C / 2.97 V |",
                  "| **PASS** | 44/45 | 59.99 deg | fs / 125 C / 2.97 V |")
        with self.assertRaisesRegex(cr.ReportError, "verdict token PASS disagrees"):
            self.build()

    def test_gain_binding_corner_non_worst_rejected(self):
        # typical / 27 C / 3.30 V is a mid-grid point, not the minimum of any row
        self.edit(f"{GAIN}{GAIN_REC}.md", "| **PASS** | 45/45 | 93.79 dB | ss / 125 C / 2.97 V |",
                  "| **PASS** | 45/45 | 93.79 dB | typical / 27 C / 3.30 V |")
        with self.assertRaisesRegex(cr.ReportError, "binding corner .* is not a worst point"):
            self.build()

    def test_gain_tied_minimum_binding_corner_accepted(self):
        # fs / 125 C / 3.30 V is lowered into an exact display tie with the worst point:
        # a genuinely tied point is a valid binding corner
        self.edit(f"{GAIN}{GAIN_REC}.md", "| `fs` | 125 | 3.30 | 94.91 | 12.775 | 72.04 |",
                  "| `fs` | 125 | 3.30 | 94.91 | 12.775 | 71.99 |")
        self.edit(f"{GAIN}{GAIN_REC}.md", "| **PASS** | 45/45 | 71.99 deg | fs / 125 C / 2.97 V |",
                  "| **PASS** | 45/45 | 71.99 deg | fs / 125 C / 3.30 V |")
        rep = self.build()
        self.assertEqual(rows_by_key(rep)["pm"]["worst_corner"], "fs / 125 C / 3.30 V")

    def test_gain_tie_within_display_tolerance_accepted(self):
        # 71.994 is within the documented display tolerance of the 71.99 minimum
        self.edit(f"{GAIN}{GAIN_REC}.md", "| `fs` | 125 | 3.30 | 94.91 | 12.775 | 72.04 |",
                  "| `fs` | 125 | 3.30 | 94.91 | 12.775 | 71.994 |")
        self.edit(f"{GAIN}{GAIN_REC}.md", "| **PASS** | 45/45 | 71.99 deg | fs / 125 C / 2.97 V |",
                  "| **PASS** | 45/45 | 71.99 deg | fs / 125 C / 3.30 V |")
        self.build()

    def test_gain_stretch_count_rejected(self):
        self.edit(f"{GAIN}{GAIN_REC}.md", "open-loop DC gain >= 70 dB holds at 45/45 points.",
                  "open-loop DC gain >= 70 dB holds at 44/45 points.")
        with self.assertRaisesRegex(cr.ReportError, "stretch count .* disagrees"):
            self.build()

    def test_gain_stretch_total_rejected(self):
        self.edit(f"{GAIN}{GAIN_REC}.md", "open-loop DC gain >= 70 dB holds at 45/45 points.",
                  "open-loop DC gain >= 70 dB holds at 45/44 points.")
        with self.assertRaisesRegex(cr.ReportError, "stretch count .* disagrees"):
            self.build()

    def test_gain_verdict_token_cli_fails_and_writes_nothing(self):
        # issue #147 behavior: a rejected record must fail publication leaving no bundle behind
        self.edit(f"{GAIN}{GAIN_REC}.md", "| **PASS** | 45/45 | 93.79 dB", "| **FAIL** | 45/45 | 93.79 dB")
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        out = self.root / "o"
        self.assertEqual(cr.main(["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(out)]), 2)
        self.assertFalse(out.exists())

    def test_pdk_mismatch_is_flagged_not_fatal(self):
        self.edit("sim/noise/records/20261010-163507-0127eb7.md", "c6d73a35f524070e85faff4a6a9eef49553ebc2b",
                  "a" * 40)
        rep = self.build()
        self.assertTrue(any("PDK revision differs" in l for l in rep["limitations"]))

    def test_partial_grid_flagged(self):
        t = (self.root / "sim/cmrr/records/20261010-163601-0127eb7.md").read_text()
        t = "\n".join(l for l in t.splitlines() if not l.startswith("| ss / 125 C / 3.63 V")) + "\n"
        (self.root / "sim/cmrr/records/20261010-163601-0127eb7.md").write_text(t)
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
        rec = self.root / "sim/cmrr/records/20261010-163601-0127eb7.md"
        rec.write_text(rec.read_text() + "\nextra line\n")
        self.assertEqual(cr.main(args + ["--check"]), 1)

    def test_no_passing_sidecar_on_generator_error(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        args = ["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]
        (self.root / "sim/cmrr/records/20261010-163601-0127eb7.md").unlink()
        self.assertEqual(cr.main(args), 2)
        self.assertFalse((self.root / "o").exists())
        self.assertEqual(cr.main(args + ["--check"]), 2)

    def test_ssp_single_record_for_all_rows(self):
        # the full run alone: all three rows from it (the revised design's record re-derives its swing)
        rep = self.build(manifest(slew_swing_power=f"{SSP}{FULL_SSP_REC}.md"))
        r = rows_by_key(rep)
        self.assertEqual({r[k]["source"]["record_id"] for k in ("slew", "swing", "power")}, {FULL_SSP_REC})
        self.assertFalse(any("cannot be re-derived" in l for l in r["swing"]["limitations"]))
        self.assertIn("slew-swing-power", rep["sources"])
        # the pre-retune full run (archival: its DUT is no longer the current one): swing not re-derivable
        old = cr.build(self.root, {"experiments": {"slew-swing-power": f"{SSP}{LEGACY_FULL_SSP_REC}.md"}}, archival=True)
        ro = rows_by_key(old)
        self.assertEqual({ro[k]["source"]["record_id"] for k in ("slew", "swing", "power")}, {LEGACY_FULL_SSP_REC})
        self.assertTrue(any("cannot be re-derived" in l for l in ro["swing"]["limitations"]))

    def test_ssp_row_from_a_record_that_did_not_judge_it(self):
        # (the swing-only record is a pre-retune one: archival, as its DUT is no longer the current one)
        m = {"experiments": {"slew-swing-power": {"power": f"{SSP}{LEGACY_SWING_REC}.md", "swing": f"{SSP}{LEGACY_SWING_REC}.md"}}}
        with self.assertRaisesRegex(cr.ReportError, "does not judge that row"):
            cr.build(self.root, m, archival=True)

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
        # issue #173: with the bound-free PSRR study selected, a ratified PSRR bound is never silently
        # left ungraded by it (the study has no grader); the primary-record behaviour is unchanged
        with self.assertRaisesRegex(cr.ReportError, "no grader for the study's figures"):
            self.build(manifest(psrr_mc=None))
        # issue #182: likewise the PSRR mismatch record has no grader for a ratified PSRR bound
        m = manifest()
        m["supplementary"].pop("psrr-passive-corners-full")
        with self.assertRaisesRegex(cr.ReportError, "no grader for the mismatch-inclusive PSRR"):
            self.build(m)
        m = manifest(psrr_mc=None)
        m["supplementary"].pop("psrr-passive-corners-full")
        r = rows_by_key(self.build(m))
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
        self.assertEqual(sel, f"{SSP}{FULL_SSP_REC}.md")  # the newest record judges all three figures
        ex = cr.extract_ssp((self.root / SSP / "20261009-114727-1dab1db.md").read_text(), "x")
        self.assertNotIn("slew", ex["rows"])

    def test_latest_selection_skips_nothing_superseded(self):
        sel = cr.latest_selection(self.root)
        self.assertEqual(sel["gain-gbw-pm"], "sim/gain-gbw-pm/records/20261010-161514-0127eb7.md")
        self.assertEqual(sel, json.loads(MANIFEST.read_text())["experiments"])

    def test_latest_selection_ignores_side_study_records(self):
        # a newer passive-corner study record (issue #70) is not a grid record
        rec = self.root / "sim/gain-gbw-pm/records/29991231-235959-0000000.md"
        rec.write_text("# gain/GBW/PM passive-corner study (RZ x CC) -- record 29991231-235959-0000000\n")
        self.assertEqual(cr.latest_selection(self.root)["gain-gbw-pm"],
                         "sim/gain-gbw-pm/records/20261010-161514-0127eb7.md")

    def test_latest_selection_ignores_the_psrr_passive_study(self):
        # issue #155: the PSRR RZ x CC study lives beside the PSRR grid records but is never selected
        # as one by recency; issue #173: it is reported only under its explicit bound-free key
        want = json.loads(MANIFEST.read_text())["experiments"]["psrr"]
        rec = self.root / "sim/psrr/records/29991231-235959-0000000.md"
        rec.write_text("# PSRR passive-corner study (RZ x CC) -- record 29991231-235959-0000000\n")
        self.assertEqual(cr.latest_selection(self.root)["psrr"], want)
        self.assertEqual([k for k, v in cr.SUPPLEMENTARY.items() if v["experiment"] == "psrr"],
                         ["psrr-passive-corners-full"])
        self.assertTrue(cr.SUPPLEMENTARY["psrr-passive-corners-full"]["bound_free"])

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
            if e in (cr.CMRR_MC_EXP, cr.GAIN_MC_EXP, cr.ICMR_EXP):
                # registered (issue #197 / #180) but the committed records predate their fingerprint:
                # disclosed as unknown, never "current", and never given a back-filled fingerprint
                self.assertEqual(s["measurement_config"]["status"], "unknown", e)
                self.assertIn("predates measurement fingerprinting", s["measurement_config"]["detail"], e)
                continue
            self.assertEqual(s["measurement_config"]["status"], "current", e)
        ic = rep["sources"][cr.ICMR_EXP]["measurement_config"]
        self.assertEqual(ic["status"], "unknown")
        self.assertIn("predates measurement fingerprinting", ic["detail"])
        joined = " ".join(rep["limitations"])
        self.assertIn(f"{cr.ICMR_EXP}: measurement-configuration freshness unknown", joined)
        self.assertIn("freshness unknown", cr.render_md(rep))
        # a pre-fingerprinting record (the pre-retune cmrr, archival: its DUT is not the current one)
        # is still disclosed as unknown, never "current"
        old = cr.build(self.root, {"experiments": {"cmrr": LEGACY_RECS["cmrr"]}}, archival=True)
        self.assertEqual(old["sources"]["cmrr"]["measurement_config"]["status"], "unknown")
        self.assertIn("cmrr: measurement-configuration freshness unknown", " ".join(old["limitations"]))

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
                                            r"stale measurement configuration.*Experiments needing a rerun: [\w, -]*gain-gbw-pm"):
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
    "cmrr-mc": "sim/cmrr-mc/testbench/tb_cmrr_mc.spice",
    "gain-gbw-pm-mc": "sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice",
    "psrr": "sim/psrr/testbench/tb_psrr.spice",
    "input-common-mode": "sim/input-common-mode/testbench/tb_input_common_mode.spice",
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
    # cmrr-mc (issue #197): bench, mismatch population, stimulus, analysis, figures, models
    ("cmrr-mc", EXP_BENCH["cmrr-mc"], "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", "MC_N = 300", "MC_N = 200", "monte_carlo"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", "MC_SEED = 45", "MC_SEED = 46", "monte_carlo"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", 'MC_VARY = "mismatch"', 'MC_VARY = "process"', "monte_carlo"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", 'DM_PARAMS = {"acp": 0.5, "acn": -0.5,', 'DM_PARAMS = {"acp": 0.4, "acn": -0.5,', "excitation"),
    ("cmrr-mc", "sim/cmrr/measurement_config.py", 'SERVO_NOMINAL = {"rsv": 1e9, "csv": 1e9}', 'SERVO_NOMINAL = {"rsv": 1e8, "csv": 1e9}', "excitation"),
    ("cmrr-mc", "sim/gain-gbw-pm/measurement_config.py", "AC_FSTOP, AC_PPD = 0.1, 1e9, 20", "AC_FSTOP, AC_PPD = 0.1, 1e9, 10", "analysis"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", "FLOOR = 2.62e-10", "FLOOR = 2.0e-10", "figures"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", "CONTROL_IMB_N = 300", "CONTROL_IMB_N = 200", "controls"),
    ("cmrr-mc", "sim/cmrr-mc/measurement_config.py", "TEMP_C = 27.0", "TEMP_C = 30.0", "corners"),
    ("cmrr-mc", "sim/gain-gbw-pm/measurement_config.py", 'MODEL_LIB = "libs.tech/ngspice/sm141064.ngspice"',
     'MODEL_LIB = "libs.tech/ngspice/sm141064.other.ngspice"', "models"),
    # gain-gbw-pm-mc (issue #197): bench (the gain bench plus the switch), population, MC request, analysis, transport
    ("gain-gbw-pm-mc", EXP_BENCH["gain-gbw-pm-mc"], "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm-mc/measurement_config.py", "MC_N = 300", "MC_N = 200", "monte_carlo"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm-mc/measurement_config.py", "MC_SEED = 45", "MC_SEED = 46", "monte_carlo"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm-mc/measurement_config.py", 'MC_VARY = "mismatch"', 'MC_VARY = "process"', "monte_carlo"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm-mc/measurement_config.py", "TEMPS_C = [27.0]", "TEMPS_C = [27.0, 125.0]", "corners"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm-mc/measurement_config.py", '"set numdgt=15",', '"set numdgt=12",', "analysis"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm/measurement_config.py", "AC_FSTOP, AC_PPD = 0.1, 1e9, 20", "AC_FSTOP, AC_PPD = 0.1, 1e9, 10", "analysis"),
    ("gain-gbw-pm-mc", "sim/gain-gbw-pm/measurement_config.py", 'PASSIVE_SECTIONS = ("res_typical", "mimcap_typical")',
     'PASSIVE_SECTIONS = ("res_typical", "mimcap_slow")', "corners"),
    ("psrr", EXP_BENCH["psrr"], "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("psrr", "sim/psrr/measurement_config.py", '"vss": {**QUIET, "acss": 1.0},', '"vss": {**QUIET, "acss": 2.0},', "excitation"),
    ("psrr", "sim/psrr/measurement_config.py", "FEEDTHROUGH_R = 100e3", "FEEDTHROUGH_R = 50e3", "controls"),
    ("psrr", "sim/cmrr/measurement_config.py", "ISOLATION_CSV = (1e7, 1e11)", "ISOLATION_CSV = (1e7, 1e12)", "controls"),
    # input-common-mode (issue #180): bench, scan/refinement policy, analysis and controls
    ("input-common-mode", "sim/input-common-mode/testbench/tb_input_common_mode.spice", "CL vout 0 2p", "CL vout 0 3p", "bench"),
    ("input-common-mode", "sim/input-common-mode/measurement_config.py", "SCAN_STEP_MV = 50", "SCAN_STEP_MV = 25", "scan"),
    ("input-common-mode", "sim/input-common-mode/measurement_config.py", "MAX_REFINE_ROUNDS = 3", "MAX_REFINE_ROUNDS = 4", "refinement"),
    ("input-common-mode", "sim/input-common-mode/measurement_config.py",
     "AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, 10.0, _GAIN.AC_PPD",
     "AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, 12.0, _GAIN.AC_PPD", "analysis"),
    ("input-common-mode", "sim/input-common-mode/measurement_config.py", "UNEQUAL_CM = _CMRR.UNEQUAL_CM",
     'UNEQUAL_CM = {"acp": 1.0, "acn": 0.98}', "controls"),
    ("input-common-mode", "sim/input-common-mode/measurement_config.py", "MODEL_LIB = _GAIN.MODEL_LIB",
     'MODEL_LIB = "libs.tech/ngspice/sm141064.other.ngspice"', "models"),
]
CLEAN_CASES = [
    # (experiment, replacement producing an equivalent bench)
    ("noise", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl   vout  0   2p ; load")),
    ("offset-mc", lambda t: "* extra comment\n\n" + t.replace("Ibias vdd ibias dc 10u", "ibias  vdd ibias   dc 10u $ bias")),
    ("cmrr-mc", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
    ("gain-gbw-pm-mc", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
    ("cmrr", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
    ("psrr", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
    ("input-common-mode", lambda t: "* extra comment\n\n" + t.replace("CL vout 0 2p", "cl  vout 0   2p ; load")),
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
        # input-common-mode registered since issue #180; its committed records predate the
        # fingerprint and are disclosed as unknown. The mismatch MC campaigns (cmrr-mc #124,
        # gain-gbw-pm-mc #172) are uninstrumented by policy: no measurement fingerprint exists,
        # freshness is disclosed as unknown. step-response (issue #198) is registered for its
        # SUPPLEMENTARY study; no experiment of that name is selected under "experiments".
        self.assertEqual(sorted(cr.MEASUREMENT_CONFIG), sorted(["step-response"] + list(self.sel)))
        mc = cr.build(REPO, manifest())["sources"][cr.ICMR_EXP]["measurement_config"]
        self.assertEqual(mc["status"], "unknown")
        self.assertIn("predates measurement fingerprinting", mc["detail"])

    def test_committed_legacy_records_stay_unknown(self):
        # the selected (revised-design) records are all fingerprinted and current, except the
        # ICMR, CMRR-MC and gain/GBW/PM-MC records: they predate their experiments' instrumentation
        # (issues #180, #197).
        rep = cr.build(REPO, manifest())
        for e, src in rep["sources"].items():
            if e in (cr.CMRR_MC_EXP, cr.GAIN_MC_EXP, cr.ICMR_EXP):  # committed records predate the fingerprint
                self.assertEqual(src["measurement_config"]["status"], "unknown", e)
                self.assertIn("predates measurement fingerprinting", src["measurement_config"]["detail"], e)
                continue
            self.assertEqual(src["measurement_config"]["status"], "current", e)
        gmc = rep["sources"][cr.GAIN_MC_EXP]["measurement_config"]
        self.assertEqual(gmc["status"], "unknown")
        self.assertIn("predates measurement fingerprinting", gmc["detail"])
        self.assertNotIn("fingerprint", gmc)  # no fabricated historical fingerprint
        joined = " ".join(rep["limitations"])
        for e in ("noise", "cmrr", "psrr", "offset-mc", "gain-gbw-pm"):
            self.assertNotIn(f"{e}: measurement-configuration freshness unknown", joined)
        old = cr.build(REPO, {"experiments": {**{e: p for e, p in LEGACY_RECS.items()},
                                              "slew-swing-power": f"{SSP}{LEGACY_FULL_SSP_REC}.md"}}, archival=True)
        for e, src in old["sources"].items():
            self.assertEqual(src["measurement_config"]["status"], "unknown", e)
            self.assertIn("predates measurement fingerprinting", src["measurement_config"]["detail"], e)
        ojoined = " ".join(old["limitations"])
        for e in ("noise", "cmrr", "psrr"):
            self.assertIn(f"{e}: measurement-configuration freshness unknown", ojoined)
        self.assertIn("slew-swing-power (", ojoined)

    def test_stamped_records_are_current_and_others_stay_unknown(self):
        for exp in EXP_BENCH:
            with self.subTest(exp):
                self.fresh()
                self.stamp(exp)
                rep = cr.build(self.root, self.m)
                self.assertEqual(rep["sources"][exp]["measurement_config"]["status"], "current")
                for e, src in rep["sources"].items():
                    if e == exp:
                        continue
                    if e in (cr.CMRR_MC_EXP, cr.GAIN_MC_EXP, cr.ICMR_EXP):  # committed record predates fingerprinting
                        self.assertEqual(src["measurement_config"]["status"], "unknown", e)
                        self.assertIn("predates measurement fingerprinting", src["measurement_config"]["detail"])
                    else:  # every other selected record is fingerprinted
                        self.assertEqual(src["measurement_config"]["status"], "current", e)
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
                        rf"Experiments needing a rerun: [\w, -]*{re.escape(exp)}"):
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
        # one record judging all three figures is keyed by the experiment alone (no per-row split)
        return "slew-swing-power"

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
                with self.assertRaisesRegex(cr.ReportError, rf"slew-swing-power(?: \([^)]*\))?: changed [^;]*figures\.{fig}\."):
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
        # (the committed nominal record measures the pre-retune DUT, whose offset RZ/CC do not change:
        # retarget its DUT prefix in the scratch copy so it can be selected beside the current records)
        self.edit(OFF_NOM_MD, "81fbd914f8254a49", "33b58dac62c747de")
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
        self.rejects(r"retained per-sample evidence is missing: sim/offset-mc/corners/20261010-185141-e4be1ac")

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
        self.edit(OFF_GRID_MD, "33b58dac62c747de", "0123456789abcdef")
        self.rejects(r"different DUT versions")


class OffsetDriftView(unittest.TestCase):
    """Issue #184: the derived offset temperature drift / systematic-vs-statistical split. Synthetic
    samples with a known drift and noise are checked against independently computed expected values;
    the committed view is checked for provenance, byte-reproducibility and freshness."""

    TEMPS = (-40.0, 27.0, 125.0)  # unevenly spaced, as the committed grid
    N = 100

    @staticmethod
    def samples(mean_v: float, sigma_v: float, n: int = 100) -> list:
        # n/2 pairs at mean +/- sigma: sample sigma is exactly sigma * sqrt(n / (n - 1))
        return [mean_v + sigma_v * z for z in ([-1.0, 1.0] * (n // 2))]

    def make_ex(self, groups: dict, n: int = 100) -> dict:
        """groups: {(process, vdd): {T: (mean_V, sigma_V)}} -> the extractor dict the view reads."""
        order, stats = [], {}
        for (proc, vdd), pts in groups.items():
            for t, (m, s) in pts.items():
                k = cr._okey(proc, t, vdd)
                order.append(k)
                stats[k] = cr.offset_stats(self.samples(m, s, n))
        return {"grid": True, "order": order, "sample_stats": stats, "n_per_point": n,
                "seed_independence": "independent", "retained": {"path": "x.csv", "sha256": "0" * 64}}

    REC = {"path": "r.md", "sha256": "1" * 64}

    def lin(self, drift_uv_per_c: float, sigma_v: float, mean27_uv: float = 50.0) -> dict:
        return {t: ((mean27_uv + drift_uv_per_c * (t - 27.0)) * 1e-6, sigma_v) for t in self.TEMPS}

    def view(self, groups: dict, n: int = 100) -> dict:
        return cr.offset_drift_view(self.make_ex(groups, n), self.REC)

    def test_known_drifts_units_and_uncertainties(self):
        import math
        sig_v = 1e-3
        v = self.view({("typical", "3.30"): self.lin(+2.0, sig_v), ("ff", "3.30"): self.lin(-1.5, sig_v),
                       ("ss", "3.30"): self.lin(0.0, sig_v)})
        sig_uv = sig_v * 1e6 * math.sqrt(self.N / (self.N - 1))  # exact sample sigma of the +/- pairs
        se = sig_uv / math.sqrt(self.N)
        sxx = (-40 - 37 - 1 / 3) ** 2 + (27 - 37 - 1 / 3) ** 2 + (125 - 37 - 1 / 3) ** 2
        self.assertEqual(v["temps_c"], [-40.0, 27.0, 125.0])
        for g, d in zip(v["groups"], (2.0, -1.5, 0.0)):
            e2e, fit = g["drift_end_to_end"], g["drift_fit"]
            self.assertAlmostEqual(e2e["uv_per_c"], d, places=5)
            self.assertAlmostEqual(fit["uv_per_c"], d, places=5)
            self.assertAlmostEqual(e2e["se_uv_per_c"], se * math.sqrt(2) / 165.0, places=5)
            self.assertAlmostEqual(fit["se_uv_per_c"], se / math.sqrt(sxx), places=5)
            self.assertEqual([s_["from_c"] for s_ in g["segments"]], [-40.0, 27.0])
            for s_ in g["segments"]:
                self.assertAlmostEqual(s_["uv_per_c"], d, places=5)
            for p in g["points"]:
                self.assertAlmostEqual(p["sigma_uv"], sig_uv, places=4)
                self.assertAlmostEqual(p["se_mean_uv"], se, places=4)
                self.assertAlmostEqual(p["abs_mean_uv"], abs(50.0 + d * (p["temp_c"] - 27.0)), places=4)
                self.assertAlmostEqual(p["three_sigma_uv"], 3 * sig_uv, places=3)
                self.assertAlmostEqual(p["abs3_uv"], p["abs_mean_uv"] + p["three_sigma_uv"], places=3)
                self.assertAlmostEqual(p["systematic_fraction"], p["abs_mean_uv"] / p["abs3_uv"], places=5)
        # the worst |mean| + 3 sigma point of the +2 uV/C group is its hottest (largest |mean|)
        self.assertEqual(v["groups"][0]["worst_point"]["temp_c"], 125.0)
        self.assertEqual(v["groups"][1]["worst_point"]["temp_c"], -40.0)
        self.assertEqual((v["worst_overall"]["process"], v["worst_overall"]["temp_c"]), ("typical", 125.0))

    def test_unequal_segments_are_two_segment_not_extrapolated(self):
        # kinked mean: flat to 27 C, then +10 uV/C: segments differ, no value outside the grid is produced
        pts = {-40.0: (0.0, 1e-3), 27.0: (0.0, 1e-3), 125.0: (980e-6, 1e-3)}
        g = self.view({("typical", "3.30"): pts})["groups"][0]
        self.assertAlmostEqual(g["segments"][0]["uv_per_c"], 0.0, places=6)
        self.assertAlmostEqual(g["segments"][1]["uv_per_c"], 10.0, places=5)
        self.assertAlmostEqual(g["drift_end_to_end"]["uv_per_c"], 980 / 165, places=5)
        self.assertNotAlmostEqual(g["drift_fit"]["uv_per_c"], g["drift_end_to_end"]["uv_per_c"], places=2)
        self.assertTrue(any("two-segment" in l and "extrapolated" in l for l in self.view({("a", "3.30"): pts})["limitations"]))

    def test_resolution_threshold(self):
        quiet = self.view({("typical", "3.30"): self.lin(5.0, 0.2e-3)})["groups"][0]
        noisy = self.view({("typical", "3.30"): self.lin(5.0, 20e-3)})["groups"][0]
        for key in ("drift_end_to_end", "drift_fit"):
            self.assertTrue(quiet[key]["resolved"], key)
            self.assertFalse(noisy[key]["resolved"], key)
            self.assertAlmostEqual(quiet[key]["uv_per_c"], noisy[key]["uv_per_c"], places=5)
        # the documented threshold: |drift| just above / below K standard errors
        se = noisy["drift_end_to_end"]["se_uv_per_c"]
        k = cr.DRIFT_RESOLVE_K
        self.assertEqual(cr._drift_entry(k * se * 1.01, se)["resolved"], True)
        self.assertEqual(cr._drift_entry(k * se * 0.99, se)["resolved"], False)
        self.assertEqual(cr._drift_entry(-k * se * 1.01, se)["resolved"], True)

    def test_row_order_invariance_and_group_isolation(self):
        g1, g2 = self.lin(2.0, 1e-3), self.lin(-3.0, 2e-3, mean27_uv=-20.0)
        a = self.view({("typical", "3.30"): g1, ("ff", "3.63"): g2})
        rev = {("ff", "3.63"): dict(reversed(list(g2.items()))), ("typical", "3.30"): dict(reversed(list(g1.items())))}
        ex = self.make_ex(rev)
        ex["order"] = list(reversed(ex["order"]))
        ex["sample_stats"] = dict(reversed(list(ex["sample_stats"].items())))
        b = cr.offset_drift_view(ex, self.REC)
        key = lambda g: (g["process"], g["vdd_v"])
        self.assertEqual({key(g): g for g in a["groups"]}, {key(g): g for g in b["groups"]})
        self.assertEqual(a["worst_overall"], b["worst_overall"])
        # changing one corner/supply leaves the others untouched
        c = self.view({("typical", "3.30"): g1, ("ff", "3.63"): self.lin(9.0, 2e-3)})
        self.assertEqual(a["groups"][0], c["groups"][0])
        self.assertNotEqual(a["groups"][1], c["groups"][1])

    def test_sigma_versus_temperature(self):
        pts = {-40.0: (0.0, 1e-3), 27.0: (0.0, 2e-3), 125.0: (0.0, 4e-3)}
        g = self.view({("typical", "3.30"): pts})["groups"][0]
        self.assertAlmostEqual(g["sigma_vs_t"]["max_over_min"], 4.0, places=5)
        self.assertEqual(g["worst_point"]["temp_c"], 125.0)
        self.assertAlmostEqual(g["worst_point"]["systematic_fraction"], 0.0, places=9)

    def test_defined_handling_of_degenerate_data(self):
        # zero spread and zero mean: no NaN / Infinity anywhere, share undefined (None), drift exactly zero, unresolved
        v = self.view({("typical", "3.30"): {t: (0.0, 0.0) for t in self.TEMPS}})
        json.dumps(v, allow_nan=False)
        g = v["groups"][0]
        self.assertIsNone(g["worst_point"]["systematic_fraction"])
        self.assertIsNone(g["sigma_vs_t"]["max_over_min"])
        self.assertEqual((g["drift_fit"]["uv_per_c"], g["drift_fit"]["resolved"]), (0.0, False))
        # zero spread but a real drift: zero standard error, resolved, still finite
        v = self.view({("typical", "3.30"): self.lin(1.0, 0.0)})
        json.dumps(v, allow_nan=False)
        self.assertTrue(v["groups"][0]["drift_end_to_end"]["resolved"])
        # one temperature only: no drift, defined
        g = self.view({("typical", "3.30"): {27.0: (1e-4, 1e-3)}})["groups"][0]
        self.assertEqual((g["drift_end_to_end"], g["drift_fit"], g["segments"]), (None, None, []))
        # nominal-only selection: unavailable, with a reason
        self.assertFalse(cr.offset_drift_view({"grid": False}, self.REC)["available"])
        # non-finite and too-few samples are refused, never rendered
        with self.assertRaises(cr.ReportError):
            cr._fin(float("nan"))
        with self.assertRaises(ValueError):
            cr.offset_stats([float("nan"), 0.0])
        with self.assertRaises(ValueError):
            cr.offset_stats([0.0])

    # ---- the committed view ----

    def committed(self):
        return cr.build(REPO, cr.load_manifest(MANIFEST))

    def test_committed_view_provenance_and_consistency(self):
        rep = self.committed()
        od = rep["offset_drift"]
        self.assertTrue(od["available"])
        self.assertEqual((od["n_per_point"], od["temps_c"], len(od["groups"])), (300, [-40.0, 27.0, 125.0], 15))
        self.assertEqual(od["input_sha256"], hashlib.sha256((REPO / OFF_GRID_CSV).read_bytes()).hexdigest())
        self.assertEqual(od["record_sha256"], hashlib.sha256((REPO / OFF_GRID_MD).read_bytes()).hexdigest())
        self.assertEqual(od["seed_independence"], "independent")
        # agrees with the row's own worst point (15.458 mV at typical / 27 C / 3.30 V)
        w = od["worst_overall"]
        self.assertEqual((w["process"], w["temp_c"], w["vdd_v"]), ("typical", 27.0, "3.30"))
        self.assertAlmostEqual(w["abs3_uv"] / 1e3, 15.458, places=3)
        self.assertAlmostEqual(w["abs_mean_uv"] + w["three_sigma_uv"], w["abs3_uv"], places=3)
        # no verdict anywhere; the ratified-row summary is untouched
        self.assertNotIn("\"verdict\"", json.dumps(od))
        self.assertEqual(rows_by_key(rep)["offset"]["verdict"], None)
        md = cr.render_md(rep)
        self.assertIn("## Offset temperature drift and systematic-vs-statistical split (derived; not graded)", md)
        self.assertIn("| typical | 3.30 | 27 | 618.7 | 14839.4 | 15458.1 | 4.0 % |", md)
        ev = json.loads(cr.render_evidence(cr.render_json(rep)))
        self.assertEqual(ev["report_derived"]["offset_temperature_drift"]["input_sha256"], od["input_sha256"])
        self.assertFalse(ev["report_derived"]["offset_temperature_drift"]["graded"])

    def test_byte_reproducible_and_committed_up_to_date(self):
        a = cr.generate(REPO, MANIFEST)
        b = cr.generate(REPO, MANIFEST)
        self.assertEqual(a, b)
        out = REPO / cr.OUT_DIR_REL
        self.assertEqual((out / "characterization-report.md").read_text(), a[0])
        self.assertEqual((out / "characterization-report.json").read_text(), a[1])
        self.assertEqual((out / "characterization-report.evidence.json").read_text(), cr.render_evidence(a[1]))

    def test_editing_committed_drift_output_fails_check(self):
        out = REPO / cr.OUT_DIR_REL
        with tempfile.TemporaryDirectory() as d:
            for f in out.glob("characterization-report*"):
                shutil.copy(f, d)
            self.assertEqual(cr.main(["--check", "--out-dir", d]), 0)
            j = Path(d) / "characterization-report.json"
            rep = json.loads(j.read_text())
            rep["offset_drift"]["groups"][0]["drift_fit"]["uv_per_c"] += 0.5
            j.write_text(cr.render_json(rep))
            self.assertEqual(cr.main(["--check", "--out-dir", d]), 1)

    def test_mutated_samples_are_rejected_or_rehashed(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(Path(d))
            base = cr.build(root, manifest())["offset_drift"]
            # a tampered sample no longer agrees with the record's summary: refused, not silently re-derived
            p = root / OFF_GRID_CSV
            lines = p.read_text().splitlines(keepends=True)
            i = next(i for i, l in enumerate(lines) if l.startswith("fs,-40.0,3.3,1.65,0,"))
            c = lines[i].rstrip("\n").split(",")
            c[-1] = repr(float(c[-1]) + 0.002)
            lines[i] = ",".join(c) + "\n"
            p.write_text("".join(lines))
            with self.assertRaisesRegex(cr.ReportError, r"inconsistent summary"):
                cr.build(root, manifest())
            # non-finite sample
            lines[i] = ",".join(c[:-1] + ["nan"]) + "\n"
            p.write_text("".join(lines))
            with self.assertRaisesRegex(cr.ReportError, r"invalid sample 0"):
                cr.build(root, manifest())
            # shared mismatch seeds across temperatures are disclosed (values untouched)
            p.write_text((REPO / OFF_GRID_CSV).read_text())
            t = p.read_text().splitlines(keepends=True)
            a = next(i for i, l in enumerate(t) if l.startswith("typical,-40.0,2.97,1.485,0,"))
            b = next(i for i, l in enumerate(t) if l.startswith("typical,27.0,2.97,1.485,0,"))
            cb = t[b].split(",")
            cb[6] = t[a].split(",")[6]
            t[b] = ",".join(cb)
            p.write_text("".join(t))
            od = cr.build(root, manifest())["offset_drift"]
            self.assertEqual(od["seed_independence"], "shared")
            self.assertEqual(od["groups"][0]["drift_fit"], base["groups"][0]["drift_fit"])
            self.assertNotEqual(od["input_sha256"], base["input_sha256"])

    def test_nominal_only_selection_reports_drift_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_root(Path(d))
            p = root / OFF_NOM_MD
            p.write_text(p.read_text().replace("81fbd914f8254a49", "33b58dac62c747de"))
            rep = cr.build(root, manifest(offset_mc=OFF_NOM_MD))
            self.assertFalse(rep["offset_drift"]["available"])
            self.assertIn("Not available", cr.render_md(rep))


class CmrrMismatchRecord(unittest.TestCase):
    """Issue #124: the committed CMRR mismatch Monte Carlo record (issue #61) is selected, re-derived
    from its own tables and its retained samples, and shown beside (never merged with) the
    systematic PVT figures of the proposed CMRR row. No verdict, no worst value."""

    DC_TYP = "| typical / 27 C / 3.30 V | 300 | 96.06 | 9.07 | +1.45 | 80.81 | 82.54 | 84.94 | **82.71** |"
    DC_FF = "| ff / 27 C / 3.30 V | 300 | 96.53 | 8.96 | +1.10 |"

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

    def test_committed_selection_and_row(self):
        sel = json.loads(MANIFEST.read_text())["experiments"]
        self.assertEqual(sel["cmrr-mc"], CMC_MD)
        self.assertEqual(sel["cmrr"], "sim/cmrr/records/20261010-163601-0127eb7.md")
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)["cmrr"]
        # the row stays proposed-not-graded: no verdict, no worst value, no point counts
        self.assertEqual((r["status"], r["verdict"], r["worst"], r["worst_corner"], r["points_pass"]),
                         ("proposed-not-graded", None, None, None, None))
        self.assertEqual(r["coverage"]["points"], 45)  # the systematic grid still covers T and VDD
        self.assertEqual(r["source"]["experiment"], "cmrr")
        sysf = [f for f in r["figures"] if f["label"].startswith("systematic (mismatch-free)")]
        self.assertEqual(sysf[0]["value"], "95.42 dB")
        self.assertEqual(len(sysf), 6)
        mc = r["cmrr_mc"]
        self.assertEqual(mc["population"], {"processes": ["typical", "ff", "ss", "fs", "sf"], "temperature_c": "27",
                                            "vdd_v": "3.30", "n_per_point": 300, "samples": 1500, "seed": 45,
                                            "vary": "mismatch", "temperature_and_supply_sampled": False})
        w = mc["worst_per_figure"]
        self.assertEqual((w["dc"]["lin3"]["value"], w["dc"]["db3"]["value"]), (82.71, 68.84))
        self.assertEqual(w["10k"]["lin3"]["value"], 82.65)
        self.assertEqual(w["fu"]["lin3"]["at"], "ss / 27 C / 3.30 V")
        self.assertEqual(w["dc"]["min"]["at"], "sf / 27 C / 3.30 V")
        self.assertEqual(set(mc["per_point"]), {"dc", "1k", "10k", "100k", "1m", "fu"})
        self.assertEqual(mc["controls"]["imbalance_shift_acm_vv"], 0.5461)
        self.assertEqual(mc["retained_evidence"]["samples"], 1500)
        self.assertEqual(mc["retained_evidence"]["points"], 5)
        self.assertEqual(rep["sources"]["cmrr-mc"]["retained_evidence"], mc["retained_evidence"])
        self.assertRegex(rep["sources"]["cmrr-mc"]["retained_evidence"]["sha256"], r"^[0-9a-f]{64}$")
        # linear and dB 3-sigma are distinct, labelled figures; the mismatch population is stated
        lin = [f for f in r["figures"] if "linear 3-sigma" in f["label"]]
        db = [f for f in r["figures"] if "dB 3-sigma" in f["label"]]
        self.assertEqual((len(lin), len(db)), (6, 6))
        self.assertEqual((lin[0]["value"], db[0]["value"]), ("82.71 dB", "68.84 dB"))
        for f in lin + db:
            self.assertIn("27 C / 3.30 V only", f["label"])
            self.assertIn("information only, not graded", f["label"])
        lim = " ".join(r["limitations"])
        self.assertIn("NOT the statistic of the proposed row", lim)
        self.assertIn("temperature and supply are NOT sampled under mismatch", lim)
        self.assertIn("seed 45", lim)
        self.assertIn("no numeric bound proposed or judged", lim)
        # the 77.1 dB PVT estimate of the decision record is an estimate: never a measured figure here
        # (the spec status column is quoted verbatim and labels that figure an estimate; skip it)
        text = json.dumps({k: v for k, v in r.items() if k != "spec_status"}) + "\n".join(
            l for l in cr.render_md(rep).splitlines() if "Spec status column" not in l and "| Proposed [DR-6]" not in l)
        self.assertNotRegex(text, r"(?<![\d.])77\.1\d* dB")
        self.assertEqual(rep["sources"]["cmrr-mc"]["measurement_config"]["status"], "unknown")
        self.assertTrue(any(l.startswith("cmrr-mc: measurement-configuration freshness unknown")
                            for l in rep["limitations"]))

    def test_markdown_shows_both_blocks(self):
        md = cr.render_md(cr.build(REPO, cr.load_manifest(MANIFEST)))
        self.assertIn("5 MOS corners (typical, ff, ss, fs, sf) x N=300 mismatch samples at 27 C, 3.30 V only", md)
        self.assertIn("systematic (mismatch-free) CMRR DC plateau", md)
        self.assertIn("mismatch CMRR DC plateau (0.1-1 Hz), linear 3-sigma lowest", md)
        self.assertIn("| cmrr-mc |", md)
        cmrr_line = next(l for l in md.splitlines() if l.startswith("| CMRR |"))
        self.assertIn("proposed-not-graded", cmrr_line)
        for frag in ("82.71", "68.84", "77.1 dB"):
            self.assertNotIn(frag, cmrr_line)

    def test_systematic_only_selection_still_builds(self):
        rep = self.build(manifest(cmrr_mc=None))
        r = rows_by_key(rep)["cmrr"]
        self.assertEqual(r["status"], "proposed-not-graded")
        self.assertNotIn("cmrr_mc", r)
        self.assertNotIn("cmrr-mc", rep["sources"])
        self.assertTrue(any("this report does not ingest that record" in l for l in r["limitations"]))
        self.assertTrue(any(l.startswith("no record selected for cmrr-mc") for l in rep["limitations"]))

    def test_unmodified_copy_matches_committed(self):
        self.assertEqual(cr.render_json(self.build()), cr.generate(REPO, MANIFEST)[1])

    # ---- the record's own summaries ----

    def test_altered_lin3s_cell_rejected(self):
        self.edit(CMC_MD, self.DC_TYP, self.DC_TYP.replace("**82.71**", "**83.71**"))
        self.rejects(r"inconsistent summary at typical / 27 C / 3\.30 V under '### DC plateau")

    def test_altered_db3s_cell_rejected(self):
        self.edit(CMC_MD, "| **82.71** | 68.84 |", "| **82.71** | 60.84 |")
        self.rejects(r"inconsistent summary at typical / 27 C / 3\.30 V under '### DC plateau")

    def test_altered_worst_table_rejected(self):
        self.edit(CMC_MD, "| DC plateau (0.1-1 Hz) | **82.71** |", "| DC plateau (0.1-1 Hz) | **81.71** |")
        self.rejects(r"worst-point table \(DC plateau \(0\.1-1 Hz\), lin3: 81\.71\) disagrees")

    def test_worst_table_wrong_point_rejected(self):
        self.edit(CMC_MD, "| **82.71** | typical / 27 C / 3.30 V | 68.84 |", "| **82.71** | ff / 27 C / 3.30 V | 68.84 |")
        self.rejects(r"names 'ff / 27 C / 3\.30 V'")

    def test_summary_consistent_with_itself_but_not_with_samples_rejected(self):
        # edit the mean and the dB 3-sigma together so the record is self-consistent: only the
        # retained samples can catch it
        self.edit(CMC_MD, "| ff / 27 C / 3.30 V | 300 | 96.53 | 8.96 | +1.10 | 81.42 | 83.26 | 85.67 | **83.01** | 69.65 |",
                  "| ff / 27 C / 3.30 V | 300 | 97.03 | 8.96 | +1.10 | 81.42 | 83.26 | 85.67 | **83.01** | 70.15 |")
        self.rejects(r"disagree\(s\) with the retained evidence .*samples\.csv")

    # ---- population ----

    def test_malformed_sample_count_rejected(self):
        self.edit(CMC_MD, "| typical / 27 C / 3.30 V | 300 | 96.06 |", "| typical / 27 C / 3.30 V | 299 | 96.06 |")
        self.rejects(r"insufficient samples at typical / 27 C / 3\.30 V under '### DC plateau.*N=299")

    def test_duplicate_point_rejected(self):
        self.edit(CMC_MD, self.DC_FF, self.DC_FF.replace("| ff /", "| typical /"))
        self.rejects(r"duplicate grid point typical / 27 C / 3\.30 V")

    def test_missing_point_rejected(self):
        p = self.root / CMC_MD
        p.write_text("\n".join(l for l in p.read_text().split("\n") if not l.startswith(self.DC_FF)))
        self.rejects(r"missing 1 of 5 grid points \(first: ff / 27 C / 3\.30 V\)")

    def test_unexpected_point_rejected(self):
        self.edit(CMC_MD, self.DC_FF, self.DC_FF.replace("| ff / 27 C", "| ff / 125 C"))
        self.rejects(r"unexpected grid point ff / 125 C / 3\.30 V")

    def test_n_below_ratified_basis_rejected(self):
        self.edit(CMC_MD, "n: 300, seed: 45", "n: 200, seed: 45")
        self.rejects(r"insufficient samples: the record's Monte Carlo request is N=200")

    def test_non_mismatch_population_rejected(self):
        self.edit(CMC_MD, 'vary: "mismatch"}` per grid point', 'vary: "process"}` per grid point')
        self.rejects(r"varies 'process'")

    def test_failed_control_rejected(self):
        self.edit(CMC_MD, "Result: **PASS**", "Result: **FAIL**", count=2)
        self.rejects(r"controls section lacks a passing")

    def test_failed_blocking_checks_rejected(self):
        self.edit(CMC_MD, "All blocking checks passed: yes.", "All blocking checks passed: NO.")
        self.rejects(r"All blocking checks passed: yes")

    # ---- retained evidence and selection ----

    def test_missing_record_rejected(self):
        (self.root / CMC_MD).unlink()
        self.rejects(r"cmrr-mc: selected record is missing")

    def test_missing_samples_csv_rejected(self):
        (self.root / CMC_CSV).unlink()
        self.rejects(r"retained per-sample evidence is missing: .*samples\.csv")

    def test_tampered_samples_csv_rejected(self):
        t = (self.root / CMC_CSV).read_text().split("\n")
        cols = t[0].split(",")
        i = cols.index("cmc_cmrr_dc")
        r = t[1].split(",")
        r[i] = str(float(r[i]) + 100.0)
        t[1] = ",".join(r)
        (self.root / CMC_CSV).write_text("\n".join(t))
        self.rejects(r"disagree\(s\) with the retained evidence")

    def test_short_samples_csv_rejected(self):
        t = (self.root / CMC_CSV).read_text().split("\n")
        (self.root / CMC_CSV).write_text("\n".join(t[:5] + t[6:]))
        self.rejects(r"insufficient samples in .*samples\.csv at typical")

    def test_invalid_sample_rejected(self):
        t = (self.root / CMC_CSV).read_text().split("\n")
        i = t[0].split(",").index("valid")
        r = t[1].split(",")
        r[i] = "0"
        t[1] = ",".join(r)
        (self.root / CMC_CSV).write_text("\n".join(t))
        self.rejects(r"invalid sample 0 at typical")

    def test_record_outside_its_experiment_directory_rejected(self):
        self.rejects(r"is not a record under sim/cmrr-mc/records", manifest(cmrr_mc="sim/cmrr/records/20261009-105631-30ec86d.md"))

    # ---- DUT identity and hash binding ----

    def test_stale_dut_names_cmrr_mc(self):
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        self.rejects(r"stale DUT.*Experiments needing a rerun: [^.]*cmrr-mc")
        self.assertEqual(cr.build(self.root, manifest(), archival=True)["sources"]["cmrr-mc"]["record_id"], CMC_REC)

    def test_mixed_dut_names_cmrr_mc(self):
        self.edit(CMC_MD, "33b58dac62c747de", "0123456789abcdef")
        self.rejects(r"different DUT versions .*0123456789abcdef: cmrr-mc")

    def test_record_edit_changes_sha_and_check_fails(self):
        out = self.root / cr.OUT_DIR_REL
        out.mkdir(parents=True, exist_ok=True)
        for n in ("md", "json", "evidence.json"):
            shutil.copy(REPO / cr.OUT_DIR_REL / f"{cr.OUT_NAME}.{n}", out / f"{cr.OUT_NAME}.{n}")
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        base = ["--root", str(self.root), "--manifest", str(mp), "--check"]
        self.assertEqual(cr.main(base), 0)
        before = self.build()["sources"]["cmrr-mc"]["sha256"]
        self.edit(CMC_MD, "N = 300 cannot resolve", "N = 301 cannot resolve")  # prose only: one byte
        self.assertNotEqual(self.build()["sources"]["cmrr-mc"]["sha256"], before)
        self.assertEqual(cr.main(base), 1)


class PsrrMismatchRecord(unittest.TestCase):
    """Issue #182: the committed PSRR mismatch Monte Carlo record (issue #99) is selected, re-derived
    from its own tables and its retained samples, and shown beside (never merged with) the systematic
    PVT figures of the proposed PSRR row. No verdict, no worst value."""

    P_DC_TYP = "| typical / 27 C / 3.30 V | 300 | 101.09 | 3.35 | +1.80 | 94.79 | 95.43 | 97.11 | **94.78** | 91.04 |"
    P_DC_FF = "| ff / 27 C / 3.30 V | 300 | 101.47 |"

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

    def test_committed_selection_and_row(self):
        # no 45-point grid record is committed yet (issue #190 fleet campaign outstanding), so the
        # nominal record is still the selected one
        sel = json.loads(MANIFEST.read_text())["experiments"]
        self.assertEqual(sel["psrr-mc"], PMC_MD)
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)["psrr"]
        self.assertEqual((r["status"], r["verdict"], r["worst"], r["worst_corner"], r["points_pass"]),
                         ("proposed-not-graded", None, None, None, None))
        self.assertEqual(r["coverage"]["points"], 45)  # the systematic grid still covers T and VDD
        self.assertEqual(r["source"]["experiment"], "psrr")
        mc = r["psrr_mc"]
        self.assertEqual(mc["population"], {"processes": ["typical", "ff", "ss", "fs", "sf"], "temperature_c": "27",
                                            "vdd_v": "3.30", "n_per_point": 300, "samples": 1500, "seed": 45,
                                            "vary": "mismatch", "temperature_and_supply_sampled": False})
        w = mc["worst_per_figure"]
        self.assertEqual(w["PSRR+ 10k"]["lin3"], {"value": 63.59, "at": "ss / 27 C / 3.30 V"})
        self.assertEqual(w["PSRR- dc"]["lin3"], {"value": 82.59, "at": "sf / 27 C / 3.30 V"})
        self.assertEqual(w["PSRR- dc"]["db3"]["value"], 67.72)
        self.assertEqual(w["PSRR- dc"]["min"]["value"], 79.46)
        self.assertEqual(len(w), 12)
        self.assertEqual(mc["knee_rails"], 4)
        self.assertEqual(mc["controls"]["imbalance"]["PSRR-"]["shift_asup_vv"], 0.7145)
        self.assertEqual((mc["retained_evidence"]["samples"], mc["retained_evidence"]["points"]), (1500, 5))
        self.assertEqual(rep["sources"]["psrr-mc"]["retained_evidence"], mc["retained_evidence"])
        self.assertEqual(rep["sources"]["psrr-mc"]["measurement_config"]["status"], "current")
        lin = [f for f in r["figures"] if "linear 3-sigma" in f["label"]]
        db = [f for f in r["figures"] if "dB 3-sigma" in f["label"]]
        self.assertEqual((len(lin), len(db)), (12, 12))
        for f in lin + db:
            self.assertIn("27 C / 3.30 V only", f["label"])
            self.assertIn("information only, not graded", f["label"])
        lim = " ".join(r["limitations"])
        self.assertIn("temperature and supply are NOT sampled under mismatch", lim)
        self.assertIn("4 near-cancellation knee rail(s)", lim)
        self.assertIn("supply rejection of the bias generator is excluded", lim)
        self.assertIn("no numeric bound proposed or judged", lim)

    def test_markdown_shows_both_blocks(self):
        md = cr.render_md(self.build(manifest(psrr_mc=PMC_MD)))
        self.assertIn("mismatch PSRR+ DC plateau (0.1-1 Hz), linear 3-sigma lowest", md)
        self.assertIn("mismatch PSRR- 10 kHz, dB 3-sigma lowest", md)
        self.assertIn("| psrr-mc |", md)
        line = next(l for l in md.splitlines() if l.startswith("| PSRR |"))
        self.assertIn("proposed-not-graded", line)
        self.assertNotIn("63.59", line)

    def test_systematic_only_selection_still_builds(self):
        rep = self.build(manifest(psrr_mc=None))
        r = rows_by_key(rep)["psrr"]
        self.assertEqual(r["status"], "proposed-not-graded")
        self.assertNotIn("psrr_mc", r)
        self.assertNotIn("psrr-mc", rep["sources"])

    def test_unmodified_copy_matches_committed(self):
        self.assertEqual(cr.render_json(self.build()), cr.generate(REPO, MANIFEST)[1])

    def test_altered_lin3s_cell_rejected(self):
        self.edit(PMC_MD, self.P_DC_TYP, self.P_DC_TYP.replace("**94.78**", "**95.78**"))
        self.rejects(r"inconsistent summary at typical / 27 C / 3\.30 V under '### PSRR\+ DC plateau", manifest(psrr_mc=PMC_MD))

    def test_altered_worst_table_rejected(self):
        self.edit(PMC_MD, "| PSRR+ 10 kHz | **63.59** |", "| PSRR+ 10 kHz | **62.59** |")
        self.rejects(r"worst-point table \(PSRR\+ 10 kHz, lin3: 62\.59\) disagrees", manifest(psrr_mc=PMC_MD))

    def test_summary_consistent_with_itself_but_not_with_samples_rejected(self):
        self.edit(PMC_MD, "| typical / 27 C / 3.30 V | 300 | 101.09 | 3.35 | +1.80 | 94.79 | 95.43 | 97.11 | **94.78** | 91.04 |",
                  "| typical / 27 C / 3.30 V | 300 | 101.59 | 3.35 | +1.80 | 94.79 | 95.43 | 97.11 | **94.78** | 91.54 |")
        self.rejects(r"disagree\(s\) with the retained evidence .*samples\.csv", manifest(psrr_mc=PMC_MD))

    def test_malformed_sample_count_rejected(self):
        self.edit(PMC_MD, "| typical / 27 C / 3.30 V | 300 | 101.09 |", "| typical / 27 C / 3.30 V | 299 | 101.09 |")
        self.rejects(r"insufficient samples at typical / 27 C / 3\.30 V under '### PSRR\+ DC plateau.*N=299", manifest(psrr_mc=PMC_MD))

    def test_missing_point_rejected(self):
        p = self.root / PMC_MD
        p.write_text("\n".join(l for l in p.read_text().split("\n") if not l.startswith(self.P_DC_FF)))
        self.rejects(r"missing 1 of 5 grid points \(first: ff / 27 C / 3\.30 V\)", manifest(psrr_mc=PMC_MD))

    def test_n_below_basis_and_non_mismatch_rejected(self):
        self.edit(PMC_MD, "n: 300, seed: 45", "n: 200, seed: 45")
        self.rejects(r"insufficient samples: the record's Monte Carlo request is N=200", manifest(psrr_mc=PMC_MD))

    def test_failed_control_rejected(self):
        self.edit(PMC_MD, "Result: **PASS**", "Result: **FAIL**", count=2)
        self.rejects(r"controls section lacks a passing", manifest(psrr_mc=PMC_MD))

    def test_knee_accounting_must_agree(self):
        self.edit(PMC_MD, "### Near-cancellation knees (4 sample rails)", "### Near-cancellation knees (5 sample rails)")
        self.rejects(r"do not sum to the 5 knee rails", manifest(psrr_mc=PMC_MD))

    def test_missing_samples_csv_rejected(self):
        (self.root / PMC_CSV).unlink()
        self.rejects(r"retained per-sample evidence is missing: .*samples\.csv", manifest(psrr_mc=PMC_MD))

    def test_tampered_samples_csv_rejected(self):
        t = (self.root / PMC_CSV).read_text().split("\n")
        i = t[0].split(",").index("pmc_psrr_vss_1k")
        r = t[1].split(",")
        r[i] = str(float(r[i]) + 100.0)
        t[1] = ",".join(r)
        (self.root / PMC_CSV).write_text("\n".join(t))
        self.rejects(r"disagree\(s\) with the retained evidence", manifest(psrr_mc=PMC_MD))

    def test_knee_flags_in_samples_csv_must_agree(self):
        t = (self.root / PMC_CSV).read_text().split("\n")
        i = t[0].split(",").index("knee_rails")
        r = t[1].split(",")
        r[i] = "vdd"
        t[1] = ",".join(r)
        (self.root / PMC_CSV).write_text("\n".join(t))
        self.rejects(r"knee rail|disagree\(s\) with the retained evidence", manifest(psrr_mc=PMC_MD))

    def test_stale_dut_names_psrr_mc(self):
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        self.rejects(r"stale DUT.*Experiments needing a rerun: [^.]*psrr-mc", manifest(psrr_mc=PMC_MD))

    def test_record_edit_changes_sha_and_check_fails(self):
        out = self.root / cr.OUT_DIR_REL
        out.mkdir(parents=True, exist_ok=True)
        for n in ("md", "json", "evidence.json"):
            shutil.copy(REPO / cr.OUT_DIR_REL / f"{cr.OUT_NAME}.{n}", out / f"{cr.OUT_NAME}.{n}")
        m = manifest(psrr_mc=PMC_MD)
        mp = self.root / "m.json"
        mp.write_text(json.dumps(m))
        base = ["--root", str(self.root), "--manifest", str(mp), "--check"]
        self.assertEqual(cr.main(base), 0)
        before = self.build(m)["sources"]["psrr-mc"]["sha256"]
        self.edit(PMC_MD, "N = 300 cannot resolve", "N = 301 cannot resolve")
        self.assertNotEqual(self.build(m)["sources"]["psrr-mc"]["sha256"], before)
        self.assertEqual(cr.main(base), 1)


class GainGbwPmMismatchRecord(unittest.TestCase):
    """Issue #172: the committed gain/GBW/PM mismatch Monte Carlo record (issue #129) is selected,
    re-derived from its retained samples, and shown beside (never merged with) the deterministic
    graded AC rows. No verdict, no worst value, no yield claim."""

    TYP_GAIN = "| typical / 27 C / 3.30 V | gain (>= 60.00 dB) | 300 / 300 | 95.98 dB | 0.05 dB | 95.83 dB | 0 | 0 | 0/300 = 0.0000 |"
    TYP_GBW = "| typical / 27 C / 3.30 V | gbw (>= 10.000 MHz) | 300 / 300 | 16.417 MHz | 0.239 MHz | 15.700 MHz | 0 | 0 | 0/300 = 0.0000 |"
    TYP_PM = "| typical / 27 C / 3.30 V | pm (>= 60.00 deg) | 300 / 300 | 77.60 deg | 0.21 deg | 76.98 deg | 0 | 0 | 0/300 = 0.0000 |"
    SS_GBW = "| ss / 27 C / 3.30 V | gbw (>= 10.000 MHz) | 300 / 300 | 15.451 MHz | 0.225 MHz | 14.776 MHz | 0 | 0 | 0/300 = 0.0000 |"
    FF_GAIN = "| ff / 27 C / 3.30 V | gain (>= 60.00 dB) | 300 / 300 | 96.09 dB | 0.05 dB | 95.93 dB | 0 | 0 | 0/300 = 0.0000 |"
    SAMPLE0 = "typical,27,3.30,0,1240223581,1073930998,250201949,pass,1,,96.01688145891897"
    SAMPLE1 = "typical,27,3.30,1,1176304945,1947764510,250201949,pass,1,,95.98895491623537"

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

    def edit_csv(self, old, new, count=1):
        self.edit(GMC_CSV, old, new, count)

    def build(self, m=None):
        return cr.build(self.root, m or manifest())

    def rejects(self, rx, m=None):
        with self.assertRaisesRegex(cr.ReportError, rx):
            self.build(m)

    # ---- the committed rows ----

    def test_committed_selection_and_rows(self):
        sel = json.loads(MANIFEST.read_text())["experiments"]
        self.assertEqual(sel["gain-gbw-pm-mc"], GMC_MD)
        rep = cr.build(REPO, cr.load_manifest(MANIFEST))
        r = rows_by_key(rep)
        # the graded AC rows stay deterministic: same verdict, same 45-point counts, same worst values
        for k, worst, corner in (("gain", "93.79 dB", "ss / 125 C / 2.97 V"),
                                 ("gbw", "11.910 MHz", "ss / 125 C / 2.97 V"),
                                 ("pm", "71.99 deg", "fs / 125 C / 2.97 V")):
            row = r[k]
            self.assertEqual((row["status"], row["verdict"], row["points_pass"], row["points_total"]),
                             ("measured-verdict", "PASS", 45, 45), k)
            self.assertEqual((row["worst"], row["worst_corner"]), (worst, corner), k)
            self.assertEqual(row["source"]["experiment"], "gain-gbw-pm", k)
        # summary counts unchanged: the mismatch evidence is not a verdict, point or row
        self.assertEqual(rep["summary"], {"ratified_rows_judged": 6, "pass": 6, "fail": 0,
                                          "not_measured": 2, "measured_no_bound": 2,
                                          "proposed_not_graded": 3})
        gmc = r["gain"]["gain_gbw_pm_mc"]
        self.assertEqual(gmc["population"], {"processes": ["typical", "ff", "ss", "fs", "sf"],
                                             "temperature_c": "27", "vdd_v": "3.30", "n_per_point": 300,
                                             "samples": 1500, "seed": 45, "vary": "mismatch",
                                             "temperature_and_supply_sampled": False})
        self.assertEqual(gmc["bounds"], {"gain": ">= 60.00 dB", "gbw": ">= 10.000 MHz", "pm": ">= 60.00 deg"})
        self.assertEqual(set(gmc["per_corner"]), {f"{p} / 27 C / 3.30 V" for p in ("typical", "ff", "ss", "fs", "sf")})
        self.assertEqual(gmc["per_corner"]["ss / 27 C / 3.30 V"]["gbw"]["mean_minus_3sigma"], 14.776)
        self.assertEqual(gmc["per_corner"]["fs / 27 C / 3.30 V"]["pm"]["mean_minus_3sigma"], 75.75)
        for pt, stats in gmc["per_corner"].items():
            for rk, s in stats.items():
                self.assertEqual((s["n"], s["valid"], s["below_bound"], s["invalid"], s["fail_fraction"]),
                                 (300, 300, 0, 0, 0.0), f"{pt}/{rk}")
        rv = gmc["retained_evidence"]
        self.assertEqual((rv["samples"], rv["points"]), (1500, 5))
        self.assertRegex(rv["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(rep["sources"]["gain-gbw-pm-mc"]["retained_evidence"], rv)
        self.assertEqual(rep["sources"]["gain-gbw-pm-mc"]["measurement_config"]["status"], "unknown")
        self.assertEqual(gmc["measurement_config"], rep["sources"]["gain-gbw-pm-mc"]["measurement_config"])
        # every row carries the population, its per-corner statistics and the honest disclosures
        for k in ("gain", "gbw", "pm"):
            figs = r[k]["figures"]
            self.assertEqual(len([f for f in figs if f["label"].startswith("mismatch ")]), 17, k)
            for f in figs:
                if f["label"].startswith("mismatch ") and "population" not in f["label"] \
                        and "re-derived" not in f["label"]:
                    self.assertIn("information only, not graded", f["label"], k)
            lim = " ".join(r[k]["limitations"])
            self.assertIn("not graded, and are not combined with the deterministic PVT figures", lim, k)
            self.assertIn("temperature and supply are NOT sampled under mismatch", lim, k)
            self.assertIn("typical passives only", lim, k)
            self.assertIn("fleet runner klt version differs", lim, k)
            self.assertIn("mean - 3 sigma is a descriptive figure", lim, k)
            self.assertIn("does not establish yield: T1 signoff item 6", lim, k)
            self.assertIn("measurement-configuration freshness unknown", lim, k)

    def test_markdown_shows_the_mismatch_block(self):
        md = cr.render_md(cr.build(REPO, cr.load_manifest(MANIFEST)))
        self.assertIn("5 MOS corners (typical, ff, ss, fs, sf) x N=300 mismatch samples at 27 C, 3.30 V only", md)
        self.assertIn("mismatch gbw at ss / 27 C / 3.30 V", md)
        self.assertIn("mean 15.451 MHz, sigma 0.225 MHz, mean - 3 sigma 14.776 MHz", md)
        self.assertIn("samples.csv, sha256 ", md)
        self.assertIn("| gain-gbw-pm-mc |", md)

    def test_unmodified_copy_matches_committed(self):
        self.assertEqual(cr.render_json(self.build()), cr.generate(REPO, MANIFEST)[1])

    def test_absent_selection_still_builds(self):
        rep = self.build(manifest(gain_gbw_pm_mc=None))
        r = rows_by_key(rep)
        for k in ("gain", "gbw", "pm"):
            self.assertNotIn("gain_gbw_pm_mc", r[k])
        self.assertNotIn("gain-gbw-pm-mc", rep["sources"])
        self.assertTrue(any(l.startswith("no record selected for gain-gbw-pm-mc") for l in rep["limitations"]))
        self.assertTrue(any("the AC rows carry no mismatch-inclusive evidence" in l
                            for l in rep["limitations"]))

    def test_selection_without_the_deterministic_record_rejected(self):
        self.rejects(r"gain-gbw-pm-mc: selected but not shown", manifest(gain_gbw_pm=None))

    # ---- the record's own statistics table ----

    def test_altered_mean_and_mean3_rejected_by_samples(self):
        # mean and mean-3sigma moved together (self-consistent): only the retained samples catch it
        self.edit(GMC_MD, self.SS_GBW,
                  self.SS_GBW.replace("15.451 MHz", "15.951 MHz").replace("14.776 MHz", "15.276 MHz"))
        self.rejects(r"inconsistent summary at ss / 27 C / 3\.30 V \(gbw\): the record's .* disagree\(s\) "
                     r"with the retained evidence .*samples\.csv")

    def test_altered_mean3sigma_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("95.83 dB", "96.83 dB"))
        self.rejects(r"inconsistent summary at typical / 27 C / 3\.30 V \(gain\): its mean-3sigma column "
                     r"disagrees with its own mean and sigma")

    def test_altered_fail_fraction_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("0/300 = 0.0000", "0/300 = 0.1000"))
        self.rejects(r"inconsistent failure fraction at typical / 27 C / 3\.30 V \(gain\)")

    def test_altered_valid_count_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("300 / 300", "299 / 300"))
        self.rejects(r"inconsistent population at typical / 27 C / 3\.30 V \(gain\): valid 299 \+ invalid 0 != N 300")

    def test_fixed_denominator_violated_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("| 300 / 300 |", "| 300 / 250 |"))
        self.rejects(r"fixed denominator violated at typical / 27 C / 3\.30 V \(gain\): the table states N=250, "
                     r"the request is N=300")

    def test_missing_corner_row_rejected(self):
        p = self.root / GMC_MD
        lines = p.read_text().splitlines(keepends=True)
        p.write_text("".join(ln for ln in lines if not ln.startswith("| sf / 27 C / 3.30 V | gain ")))
        self.rejects(r"statistics table is missing 1 of 15 rows \(first: gain at sf / 27 C / 3\.30 V\)")

    def test_missing_entire_corner_rejected(self):
        p = self.root / GMC_MD
        lines = p.read_text().splitlines(keepends=True)
        p.write_text("".join(ln for ln in lines if not ln.startswith("| sf / 27 C / 3.30 V |")))
        self.rejects(r"statistics table is missing 3 of 15 rows \(first: gain at sf / 27 C / 3\.30 V\)")

    def test_duplicate_row_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN + "\n", self.TYP_GAIN + "\n" + self.TYP_GAIN + "\n")
        self.rejects(r"duplicate gain row at typical / 27 C / 3\.30 V")

    def test_unexpected_point_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN,
                  self.TYP_GAIN.replace("typical / 27 C", "tt / 27 C", 1) + "\n" + self.TYP_GAIN)
        self.rejects(r"unexpected grid point tt / 27 C / 3\.30 V in the statistics table")

    def test_malformed_row_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("95.98 dB", "dB"))
        self.rejects(r"malformed statistics row")

    def test_malformed_row_label_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("gain (>= 60.00 dB)", "gain (>= 60)"))
        self.rejects(r"malformed statistics row label")

    def test_inconsistent_unit_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("95.98 dB", "95.98 deg"))
        self.rejects(r"gain row at typical / 27 C / 3\.30 V carries unit 'deg', but its bound is 'dB'")

    def test_non_finite_mean_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("95.98 dB", "nan dB", 1))
        self.rejects(r"non-finite mean at typical / 27 C / 3\.30 V \(gain\): the published statistics must be finite")

    def test_non_finite_sigma_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("0.05 dB", "nan dB", 1))
        self.rejects(r"non-finite sigma at typical / 27 C / 3\.30 V \(gain\): the published statistics "
                     r"must be finite")

    def test_non_finite_mean3sigma_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("95.83 dB", "nan dB", 1))
        self.rejects(r"non-finite mean-3sigma at typical / 27 C / 3\.30 V \(gain\): the published statistics "
                     r"must be finite")

    # ---- population ----

    def test_n_below_basis_rejected(self):
        self.edit(GMC_MD, "monte_carlo = {n: 300, seed: 45", "monte_carlo = {n: 200, seed: 45")
        self.rejects(r"insufficient samples: .* N=200 per corner; the mismatch statistic needs N >= 300")

    def test_population_count_inconsistent_rejected(self):
        self.edit(GMC_MD, "-> 5 x 300 = 1500 samples", "-> 5 x 300 = 1600 samples")
        self.rejects(r"Population line is inconsistent \(5 x 300 = 1600 samples for 5 processes at N=300\)")

    def test_duplicate_process_rejected(self):
        self.edit(GMC_MD, "process typical, ff, ss, fs, sf;", "process typical, ff, ss, fs, ff;")
        self.rejects(r"Population line is inconsistent")

    def test_non_mismatch_population_rejected(self):
        self.edit(GMC_MD, 'vary: "mismatch"', 'vary: "process"')
        self.rejects(r"varies 'process', not the mismatch population")

    def test_missing_population_line_rejected(self):
        self.edit(GMC_MD, "- **Population**: process typical", "- **Subjects**: process typical")
        self.rejects(r"no Population line")

    # ---- bounds ----

    def test_bound_must_match_the_ratified_bound(self):
        # every corner carries the (uniformly wrong) 70 dB bound: the cross-corner agreement gate
        # passes and the shared bound itself is rejected against the ratified spec bound
        self.edit(GMC_MD, "gain (>= 60.00 dB)", "gain (>= 70.00 dB)", 5)
        self.rejects(r"gain-gbw-pm-mc:20261011-072005-9c9b0f1: record bound '>= 70\.00 dB' for Open-loop DC gain "
                     r"is not the current ratified bound \('>= 60 dB'\)")

    def test_stale_bound_unit_rejected(self):
        self.edit(GMC_MD, self.SS_GBW, self.SS_GBW.replace("gbw (>= 10.000 MHz)", "gbw (>= 10.000 dB)"))
        self.rejects(r"gbw row at ss / 27 C / 3\.30 V carries unit 'MHz', but its bound is 'dB'")

    def test_non_first_corner_bound_mismatch_rejected(self):
        # the shared bounds were previously taken from the first corner only: a bound retuned on a
        # later corner escaped both the ratified-bound comparison (which sees only the shared bound)
        # and the retained-sample crosscheck (whose below-bound count moved with the altered bound)
        self.edit(GMC_MD, self.FF_GAIN, self.FF_GAIN.replace("gain (>= 60.00 dB)", "gain (>= 70.00 dB)"))
        self.rejects(r"gain bound at ff / 27 C / 3\.30 V is '>= 70\.00 dB', but the shared bound at "
                     r"typical / 27 C / 3\.30 V is '>= 60\.00 dB': the ratified bound must be identical "
                     r"at every corner")

    # ---- the retained samples ----

    def test_missing_samples_csv_rejected(self):
        (self.root / GMC_CSV).unlink()
        self.rejects(r"retained per-sample evidence is missing: sim/gain-gbw-pm-mc/corners/20261011-072005-9c9b0f1/samples\.csv")

    def test_tampered_sample_rejected(self):
        self.edit_csv(self.SAMPLE0, self.SAMPLE0.replace("96.01688145891897", "91.01688145891897"))
        self.rejects(r"inconsistent summary at typical / 27 C / 3\.30 V \(gain\): the record's .* disagree\(s\) "
                     r"with the retained evidence")

    def test_duplicate_sample_index_rejected(self):
        rows = (self.root / GMC_CSV).read_text().splitlines(keepends=True)
        (self.root / GMC_CSV).write_text("\n".join(rows + [rows[1]]))
        self.rejects(r"duplicate sample index 0 at typical / 27 C / 3\.30 V")

    def test_missing_sample_index_rejected(self):
        rows = (self.root / GMC_CSV).read_text().splitlines(keepends=True)
        (self.root / GMC_CSV).write_text("".join(r for r in rows if not r.startswith("typical,27,3.30,299,")))
        self.rejects(r"1 sample indices missing at typical / 27 C / 3\.30 V \(first: 299; the identity is "
                     r"exactly 0\.\.299, each once\)")

    def test_index_out_of_range_rejected(self):
        self.edit_csv("typical,27,3.30,299,", "typical,27,3.30,300,")
        self.rejects(r"sample index 300 at typical / 27 C / 3\.30 V is outside the declared identity 0\.\.299")

    def test_non_finite_metric_rejected(self):
        self.edit_csv("96.01688145891897,16259883.348597879", "nan,16259883.348597879")
        self.rejects(r"sample 0 at typical / 27 C / 3\.30 V carries a non-finite metric")

    def test_malformed_sample_row_rejected(self):
        self.edit_csv("96.01688145891897,16259883.348597879", "96.01688145891897,16.259e6x")
        self.rejects(r"malformed sample row")

    def test_invalid_sample_rejected(self):
        self.edit_csv("250201949,pass,1,,96.01688145891897", "250201949,pass,0,,96.01688145891897")
        self.rejects(r"invalid sample 0 at typical / 27 C / 3\.30 V discloses no reason")

    def test_klt_status_not_pass_rejected(self):
        self.edit_csv("250201949,pass,1,,96.01688145891897", "250201949,fail,1,,96.01688145891897")
        self.rejects(r"marked valid but its klt status is 'fail'")

    def test_missing_columns_rejected(self):
        rows = (self.root / GMC_CSV).read_text().splitlines()
        bad = [ln for ln in rows]
        bad[0] = bad[0].replace(",dc_gain_db", "")
        bad[1] = ",".join(c for c in bad[1].split(",")[:-2])
        (self.root / GMC_CSV).write_text("\n".join(bad) + "\n")
        self.rejects(r"lacks the columns it needs")

    def test_invalid_population_disclosed_not_dropped(self):
        # one sample retires with a reason; the record's typical rows are updated consistently
        # (all three rows: valid 299/300, invalid 1, fraction 1/300): the report builds and
        # discloses the invalid population
        new1 = self.SAMPLE1.replace(",pass,1,,", ",pass,0,no plateau,")
        self.edit_csv(self.SAMPLE1, new1)
        for row in (self.TYP_GAIN, self.TYP_GBW, self.TYP_PM):
            new = (row.replace("300 / 300 |", "299 / 300 |")
                   .replace("| 0 | 0 | 0/300 = 0.0000", "| 0 | 1 | 1/300 = 0.0033"))
            if row is self.TYP_GBW:  # the published figures are over the valid samples only
                new = new.replace("15.700 MHz", "15.699 MHz")
            self.edit(GMC_MD, row, new)
        rep = self.build()
        r = rows_by_key(rep)["gain"]["gain_gbw_pm_mc"]
        self.assertEqual((r["per_corner"]["typical / 27 C / 3.30 V"]["gain"]["valid"],
                          r["per_corner"]["typical / 27 C / 3.30 V"]["gain"]["invalid"]), (299, 1))
        self.assertTrue(any("discloses an invalid population (1 samples" in l
                            for l in rows_by_key(rep)["gain"]["limitations"]))

    def test_rows_disagreeing_on_the_shared_population_rejected(self):
        self.edit(GMC_MD, self.TYP_GAIN, self.TYP_GAIN.replace("300 / 300", "299 / 300")
                  .replace("| 0 | 0 | 0/300 = 0.0000", "| 0 | 1 | 1/300 = 0.0033"))
        self.rejects(r"the three rows disagree on the shared population at typical / 27 C / 3\.30 V")

    # ---- DUT identity and hash binding ----

    def test_stale_dut_names_gain_gbw_pm_mc(self):
        p = self.root / "design/netlist/opamp_two_stage.spice"
        p.write_text(p.read_text().replace("W=72u", "W=73u", 1))
        self.rejects(r"stale DUT.*Experiments needing a rerun: [^.]*gain-gbw-pm-mc")
        self.assertEqual(cr.build(self.root, manifest(), archival=True)["sources"][cr.GAIN_MC_EXP]["record_id"],
                         GMC_REC)

    def test_mixed_dut_names_gain_gbw_pm_mc(self):
        self.edit(GMC_MD, "33b58dac62c747de", "0123456789abcdef")
        self.rejects(r"different DUT versions .*0123456789abcdef: gain-gbw-pm-mc")


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
        self.assertEqual(src["dut_sha256"], "33b58dac62c747de")
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
        for frag in ("not ratified", "follower-biased", "mismatch not covered", "freshness unknown"):
            self.assertIn(frag, joined)
        # the revised-design record carries its disclosures itself: no separate addendum (the pre-retune one has)
        self.assertIsNone(ic["addendum"])
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

    # ---- measurement-configuration freshness (issue #180) ----

    def test_committed_record_freshness_is_unknown_and_disclosed(self):
        # the committed records predate the experiment's instrumentation: availability and
        # structure are unaffected, the configuration freshness is just unknown
        rep = self.build()
        mcst = rep["sources"][cr.ICMR_EXP]["measurement_config"]
        self.assertEqual(mcst["status"], "unknown")
        self.assertIn("predates measurement fingerprinting", mcst["detail"])
        self.assertIn(f"{cr.ICMR_EXP}: measurement-configuration freshness unknown",
                      " ".join(rep["limitations"]))

    def test_stamped_record_is_current(self):
        stamp_record(self.root, cr.ICMR_EXP, ICMR_MD)
        rep = self.build()
        self.assertEqual(rep["sources"][cr.ICMR_EXP]["measurement_config"]["status"], "current")
        self.assertNotIn(f"{cr.ICMR_EXP}: measurement-configuration freshness unknown",
                         " ".join(rep["limitations"]))

    def test_setting_change_rejects_the_stamped_selection_and_archival_discloses(self):
        cases = (
            (f"{ICMR}/testbench/tb_input_common_mode.spice", "CL vout 0 2p", "CL vout 0 3p", "bench"),
            (f"{ICMR}/measurement_config.py", "SCAN_STEP_MV = 50", "SCAN_STEP_MV = 25", "scan"),
            (f"{ICMR}/measurement_config.py", "MAX_REFINE_ROUNDS = 3", "MAX_REFINE_ROUNDS = 4", "refinement"),
            (f"{ICMR}/measurement_config.py",
             "AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, 10.0, _GAIN.AC_PPD",
             "AC_FSTART, AC_FSTOP, AC_PPD = _GAIN.AC_FSTART, 12.0, _GAIN.AC_PPD", "analysis"),
        )
        for rel, old, new, group in cases:
            with self.subTest(group):
                with tempfile.TemporaryDirectory() as d:
                    root = make_root(Path(d))
                    stamp_record(root, cr.ICMR_EXP, ICMR_MD)
                    p = root / rel
                    t = p.read_text()
                    self.assertEqual(t.count(old), 1, rel)
                    p.write_text(t.replace(old, new, 1))
                    with self.assertRaisesRegex(
                            cr.ReportError,
                            rf"stale measurement configuration.*input-common-mode: changed [^;]*{group}"
                            rf".*Experiments needing a rerun: [\w, -]*input-common-mode"):
                        cr.build(root, manifest())
                    rep = cr.build(root, manifest(), archival=True)
                    self.assertEqual(rep["sources"][cr.ICMR_EXP]["measurement_config"]["status"], "stale")
                    self.assertTrue(any(
                        l.startswith("input-common-mode: measurement configuration differs") and "archival report only" in l
                        for l in rep["limitations"]))

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
        self.edit(ICMR_MD, "`33b58dac62c747de`", "`0123456789abcdef`")
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
        ("sample counts", "3450 pass, 2247 fail, 468 invalid.", "3451 pass, 2246 fail, 468 invalid.", "retained evidence|holds"),
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


class SupplementaryPassiveStudies(unittest.TestCase):
    """Issue #121: the explicitly selected passive-corner side studies are hashed, parsed and
    checked, and their observations are derived from their tables -- outside every verdict."""

    GKEY, SKEY = "gain-gbw-pm-passive-corners", "slew-swing-power-passive-corners"
    GFULL, SFULL = "gain-gbw-pm-passive-corners-full", "slew-swing-power-passive-corners-full"  # issue #150
    PFULL = "psrr-passive-corners-full"  # issue #173 (bound-free)
    STEP = "step-response-follower"  # issue #198 (bound-free, no rows; own test class below)
    ALL_KEYS = {GKEY, SKEY, GFULL, SFULL, PFULL, STEP}

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))

    def tearDown(self):
        self._t.cleanup()

    def build(self, m=None, **kw):
        return cr.build(self.root, m or manifest(), **kw)

    def edit(self, rel, old, new, count=1):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, count))

    def rejects(self, rx, m=None, **kw):
        with self.assertRaisesRegex(cr.ReportError, rx):
            self.build(m, **kw)

    def sup(self, **over):
        m = manifest()
        for k, v in over.items():
            if v is None:
                m["supplementary"].pop(k, None)
            else:
                m["supplementary"][k] = v
        return m

    # ---- positive ----
    def test_committed_selection_is_explicit(self):
        sel = json.loads(MANIFEST.read_text())
        sup = sel["supplementary"]
        self.assertEqual({k: sup[k] for k in (self.GKEY, self.SKEY)}, {self.GKEY: GAIN_STUDY_MD, self.SKEY: SSP_STUDY_MD})
        self.assertEqual(set(sup), self.ALL_KEYS)
        for rel in sup.values():  # never also a verdict source
            self.assertNotIn(rel, json.dumps(sel["experiments"]))

    def test_both_studies_hashed_with_exact_scope(self):
        rep = self.build()
        for key, rel in ((self.GKEY, GAIN_STUDY_MD), (self.SKEY, SSP_STUDY_MD)):
            st = rep["supplementary"][key]
            self.assertFalse(st["graded"])
            self.assertEqual(st["path"], rel)
            self.assertEqual(st["sha256"], hashlib.sha256((REPO / rel).read_bytes()).hexdigest())
            self.assertEqual(st["dut_check"], {"matches_selected_records": True, "matches_current_netlist": True})
            sc = st["scope"]
            self.assertEqual(sc["cells"], 27)
            self.assertFalse(sc["full_pvt_cross_product"])
            self.assertEqual(sc["points"], ["fs / 125 C / 2.97 V", "ss / 125 C / 2.97 V", "typical / 27 C / 3.30 V"])
            self.assertIn("not a full passive-by-PVT cross product", sc["text"])
            self.assertEqual(st["measurement_config"]["status"], "unknown")
            self.assertTrue(any("freshness unknown" in l for l in st["limitations"]))
            self.assertTrue(any(l.startswith(f"supplementary {key}: measurement-configuration freshness unknown")
                                for l in rep["limitations"]))

    def test_full_grid_studies_are_405_cell_full_scope(self):
        """Issue #150: the full-envelope records are selected under their own keys, parsed with
        the same strictness, and described as the full passive-by-PVT cross product."""
        rep = self.build()
        for key in (self.GFULL, self.SFULL):
            st = rep["supplementary"][key]
            self.assertFalse(st["graded"])
            sc = st["scope"]
            self.assertEqual(sc["cells"], 405)
            self.assertEqual(len(sc["points"]), 45)
            self.assertTrue(sc["full_pvt_cross_product"])
            self.assertIn("FULL passive-by-PVT cross product", sc["text"])
            for f in st["figures"].values():
                self.assertEqual(f["total"], 405)
                self.assertEqual(f["pass"] + f["fail"], 405)
        # the limited studies keep their 27-cell scope beside them
        self.assertEqual(rep["supplementary"][self.GKEY]["scope"]["cells"], 27)
        self.assertFalse(rep["supplementary"][self.GKEY]["scope"]["full_pvt_cross_product"])

    def test_scope_must_match_the_selected_key(self):
        sel = json.loads(MANIFEST.read_text())["supplementary"]
        self.rejects(r"scope mismatch", self.sup(**{self.GKEY: sel[self.GFULL]}))
        self.rejects(r"scope mismatch", self.sup(**{self.GFULL: sel[self.GKEY]}))

    def test_incomplete_full_study_rejected(self):
        rel = json.loads(MANIFEST.read_text())["supplementary"][self.GFULL]
        p = self.root / rel
        t = p.read_text()
        i = t.index("\n### ", t.index("## Results"))
        j = t.index("\n### ", i + 5)
        p.write_text(t[:i] + t[j:])  # drop one point's table
        self.rejects(r"no results table for study point")

    def test_gain_study_observations_derived_from_tables(self):
        f = self.build()["supplementary"][self.GKEY]["figures"]
        self.assertEqual((f["gbw"]["pass"], f["gbw"]["total"], f["gbw"]["fail"]), (27, 27, 0))
        self.assertEqual((f["pm"]["pass"], f["pm"]["total"]), (27, 27))
        self.assertEqual(f["pm"]["range"], ["64.47", "84.93"])
        self.assertEqual(f["gbw"]["worst"], {"value": "10.726", "point": "ss / 125 C / 2.97 V", "rz": "best", "cc": "worst"})
        self.assertEqual(f["gbw"]["failing_cells"], [])
        self.assertNotIn("gain", f)

    def test_ssp_study_observations_derived_from_tables(self):
        f = self.build()["supplementary"][self.SKEY]["figures"]
        self.assertEqual(sorted(f), ["power", "slew", "swing"])
        for k in f:
            self.assertEqual((f[k]["pass"], f[k]["total"], f[k]["verdict_in_record"]), (27, 27, "PASS"))
        self.assertEqual(f["slew"]["worst"]["value"], "14.25")
        self.assertEqual(f["power"]["range"], ["239.2", "273.7"])
        self.assertEqual(f["swing"]["worst"]["value"], "2.465")

    def test_studies_never_change_verdicts_points_or_counts(self):
        with_s = self.build()
        without = self.build(self.sup(**{k: None for k in self.ALL_KEYS}))
        self.assertEqual(with_s["summary"], without["summary"])
        self.assertEqual(without["supplementary"], {})
        for a, b in zip(with_s["rows"], without["rows"]):
            for k in ("status", "verdict", "points_pass", "points_total", "worst", "worst_corner", "coverage"):
                self.assertEqual(a[k], b[k], (a["key"], k))
        self.assertTrue(any(l.startswith(f"no supplementary study selected for {self.GKEY}")
                            for l in without["limitations"]))
        self.assertTrue(any(l == f"no supplementary study selected for {self.STEP}: its ungraded observations "
                                "are not shown" for l in without["limitations"]))
        for k in ("gbw", "pm", "slew"):
            self.assertFalse(any("side study" in l for l in rows_by_key(without)[k]["limitations"]))

    def test_markdown_section_and_determinism(self):
        md1, js1 = cr.generate(self.root, MANIFEST)
        md2, js2 = cr.generate(self.root, MANIFEST)
        self.assertEqual((md1, js1), (md2, js2))
        self.assertIn("## Supplementary side studies (measured side evidence; not graded)", md1)
        for rel in (GAIN_STUDY_MD, SSP_STUDY_MD):
            self.assertIn(hashlib.sha256((REPO / rel).read_bytes()).hexdigest(), md1)

    # ---- provenance hash and numeric changes ----
    def test_changing_a_study_changes_its_hash_and_the_report(self):
        before = cr.render_json(self.build())
        self.edit(GAIN_STUDY_MD, "## Artifacts", "## Artifacts\n\n- note appended in a scratch copy")
        rep = self.build()
        self.assertNotEqual(rep["supplementary"][self.GKEY]["sha256"],
                            json.loads(before)["supplementary"][self.GKEY]["sha256"])
        self.assertNotEqual(cr.render_json(rep), before)

    def test_consistent_numeric_change_is_reflected_not_stale_prose(self):
        # move the worst PM cell in both the table and the header: the derived text follows
        self.edit(GAIN_STUDY_MD, "| best | best | 64.47 |", "| best | best | 63.90 |")
        self.edit(GAIN_STUDY_MD, "worst 64.47 deg at fs", "worst 63.90 deg at fs")
        # limited study only: the full-envelope study (issue #150) legitimately still reads 64.47
        rep = self.build(self.sup(**{self.GFULL: None, self.SFULL: None}))
        pm = " ".join(rows_by_key(rep)["pm"]["limitations"])
        self.assertIn("range 63.90 .. 84.93 deg", pm)
        self.assertNotIn("64.47", pm)

    def test_table_change_contradicting_the_header_rejected(self):
        self.edit(GAIN_STUDY_MD, "| best | best | 64.47 |", "| best | best | 63.90 |")
        self.rejects(r"Phase margin summary .* disagrees with its own tables")

    def test_cell_flip_contradicting_counts_rejected(self):
        self.edit(GAIN_STUDY_MD, "| 10.726 | -9.9 | 0.909 | PASS | PASS |", "| 9.726 | -9.9 | 0.909 | PASS | FAIL |")
        self.rejects(r"GBW summary .*27/27 pass.* disagrees with its own tables \(FAIL, 26/27 pass")

    def test_cell_flag_contradicting_its_value_rejected(self):
        self.edit(GAIN_STUDY_MD, "| 10.726 | -9.9 | 0.909 | PASS | PASS |", "| 9.726 | -9.9 | 0.909 | PASS | PASS |")
        self.rejects(r"GBW cell ss / 125 C / 2.97 V, RZ best, CC worst reads 9.726 MHz but is marked 'PASS'")

    def test_header_count_disagreeing_rejected(self):
        self.edit(GAIN_STUDY_MD, "GBW >= 10 MHz: **PASS** -- passes at 27/27 study cells (fails at 0/27)",
                  "GBW >= 10 MHz: **PASS** -- passes at 26/27 study cells (fails at 1/27)")
        self.rejects(r"GBW summary .* disagrees with its own tables")

    def test_ssp_summary_disagreeing_rejected(self):
        self.edit(SSP_STUDY_MD, "worst 14.25 V/us at ss / 125 C / 2.97 V, RZ best, CC worst",
                  "worst 14.6 V/us at ss / 125 C / 2.97 V, RZ best, CC worst")
        self.rejects(r"Slew rate .* summary .* disagrees with its own tables")

    def test_ssp_worst_cell_misnamed_rejected(self):
        self.edit(SSP_STUDY_MD, "worst 14.25 V/us at ss / 125 C / 2.97 V, RZ best, CC worst",
                  "worst 14.25 V/us at ss / 125 C / 2.97 V, RZ best, CC best")
        self.rejects(r"Slew rate .* summary .* disagrees with its own tables")

    # ---- malformed / missing ----
    def test_missing_study_rejected(self):
        (self.root / SSP_STUDY_MD).unlink()
        self.rejects(r"supplementary slew-swing-power-passive-corners: selected record is missing")

    def test_duplicate_cell_rejected(self):
        self.edit(GAIN_STUDY_MD, "| worst | worst | 79.16 |", "| best | best | 79.16 |")
        self.rejects(r"duplicate cell RZ best, CC best under '### fs / 125 C / 2.97 V'")

    def test_missing_cell_rejected(self):
        self.edit(SSP_STUDY_MD, "| worst | worst | 2.893 | -0.0 | PASS |\n", "")
        self.rejects(r"'### Output swing -- typical / 27 C / 3.30 V .*' is missing 1 of 9 RZ x CC cells")

    def test_malformed_cell_rejected(self):
        self.edit(GAIN_STUDY_MD, "| best | best | 64.47 |", "| best | best | 64.4x |")
        self.rejects(r"malformed value '64.4x'")

    def test_unknown_level_rejected(self):
        self.edit(GAIN_STUDY_MD, "| worst | worst | 79.16 |", "| worst | slow | 79.16 |")
        self.rejects(r"malformed RZ x CC row")

    def test_missing_point_table_rejected(self):
        p = self.root / GAIN_STUDY_MD
        t = p.read_text()
        a, b = t.index("### typical / 27 C / 3.30 V"), t.index("## Sensitivity")
        p.write_text(t[:a] + t[b:])
        self.rejects(r"no results table for study point typical / 27 C / 3.30 V")

    def test_missing_header_line_rejected(self):
        self.edit(GAIN_STUDY_MD, "  - GBW >= 10 MHz: **PASS**", "  - GBW at least 10 MHz: **PASS**")
        self.rejects(r"no 'GBW' verdict line")

    # ---- selection / provenance ----
    def test_stale_or_mixed_dut_rejected(self):
        self.edit(SSP_STUDY_MD, "33b58dac62c747de979b33423bb54148cab3eb334fd9fab6051b7ed99226e41b", "0" * 64)
        self.rejects(r"side study measures DUT 0000000000000000, the selected records 33b58dac62c747de")

    def test_stale_dut_without_primary_selection_rejected(self):
        self.edit(SSP_STUDY_MD, "33b58dac62c747de979b33423bb54148cab3eb334fd9fab6051b7ed99226e41b", "0" * 64)
        m = self.sup(**{self.GKEY: None})
        m["experiments"] = {}
        self.rejects(r"stale DUT: supplementary slew-swing-power-passive-corners", m)
        st = self.build(m, archival=True)["supplementary"][self.SKEY]
        self.assertEqual(st["dut_check"], {"matches_selected_records": None, "matches_current_netlist": None})

    def test_grid_record_as_study_rejected(self):
        self.rejects(r"is not a 'gain/GBW/PM passive-corner study' record",
                     self.sup(**{self.GKEY: "sim/gain-gbw-pm/records/20261010-020141-1e51d1c.md"}))

    def test_study_from_other_experiment_dir_rejected(self):
        self.rejects(r"is not a record under sim/slew-swing-power/records",
                     self.sup(**{self.SKEY: GAIN_STUDY_MD}))

    def test_superseded_study_rejected(self):
        self.rejects(r"superseded by 20261009-234014-55b400c",
                     self.sup(**{self.GKEY: "sim/gain-gbw-pm/records/20261009-233341-95dfc2a.md"}))

    def test_unknown_study_key_rejected(self):
        p = self.root / "sel.json"
        m = manifest()
        m["supplementary"]["noise-passive-corners"] = "x.md"
        p.write_text(json.dumps(m))
        with self.assertRaisesRegex(cr.ReportError, r"unknown supplementary study"):
            cr.load_manifest(p)

    def test_study_bound_must_be_the_ratified_bound(self):
        # internally consistent study, judged against a bound other than the ratified >= 10 V/us
        self.edit(SSP_STUDY_MD, "vs ratified >= 10 V/us", "vs ratified >= 12 V/us")
        self.edit(SSP_STUDY_MD, "(bound >= 10 V/us)", "(bound >= 12 V/us)", count=3)
        self.rejects(r"study bound '>= 12 V/us' for Slew rate .* is not the current ratified bound")

    def test_stale_fingerprint_follows_policy_and_unknown_is_disclosed(self):
        h = cr._harness()
        cur = cr.current_measurement_inputs(self.root, "gain-gbw-pm")
        for inputs, want in ((cur, "current"), (dict(cur, analysis="changed-in-a-scratch-copy"), "stale")):
            with self.subTest(want=want):
                p = self.root / GAIN_STUDY_MD
                orig = p.read_text()
                text = orig.replace("- **Issue**:", "\n".join(h.fingerprint_header_lines(inputs, "test")) + "\n- **Issue**:", 1)
                p.write_text(text + "\n" + "\n".join(h.fingerprint_inputs_section(inputs, "x")))
                if want == "stale":
                    self.rejects(r"stale measurement configuration: supplementary gain-gbw-pm-passive-corners")
                    rep = self.build(archival=True)
                    self.assertTrue(any(l.startswith(f"supplementary {self.GKEY}: measurement configuration differs")
                                        for l in rep["limitations"]))
                else:
                    rep = self.build()
                self.assertEqual(rep["supplementary"][self.GKEY]["measurement_config"]["status"], want)
                p.write_text(orig)

    def test_latest_update_manifest_keeps_explicit_studies(self):
        import contextlib
        import io
        mp = self.root / "sel.json"
        mp.write_text(MANIFEST.read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cr.main(["--root", str(self.root), "--manifest", str(mp), "--latest",
                                      "--update-manifest", "--stdout"]), 0)
        self.assertEqual(json.loads(mp.read_text()), json.loads(MANIFEST.read_text()))


class PsrrFullPassiveStudy(unittest.TestCase):
    """Issue #173: the full 405-cell PSRR RZ x CC study (issue #155) is explicitly selected, hashed
    and re-derived from its retained curves as BOUND-FREE information attached to the PSRR row:
    no verdict while DR-6 is proposed, primary selection / graded counts / bounds unchanged."""

    KEY = "psrr-passive-corners-full"
    WORST_10K = "| PSRR+ 10 kHz | **61.09** | ss / 125 C / 2.97 V / RZ worst / CC worst |"
    CELL = "| typical / -40 C / 2.97 V / RZ typical / CC best | 86.11 | 66.32 | 104.83 | 104.64 | +0.897 | +0.032 |"
    BINDING_DAT = f"{PSRR_STUDY_DIR}/ss__r-worst__c-worst_125c_2.97v.psrr.dat"

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))

    def tearDown(self):
        self._t.cleanup()

    def build(self, m=None, **kw):
        return cr.build(self.root, m or manifest(), **kw)

    def edit(self, rel, old, new, count=1):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, count))

    def rejects(self, rx, m=None, **kw):
        with self.assertRaisesRegex(cr.ReportError, rx):
            self.build(m, **kw)

    def without(self):
        m = manifest()
        m["supplementary"].pop(self.KEY)
        return m

    # ---- positive: selected, hashed, displayed ----
    def test_explicitly_selected_beside_the_unchanged_primary_record(self):
        sel = json.loads(MANIFEST.read_text())
        self.assertEqual(sel["supplementary"][self.KEY], PSRR_STUDY_MD)
        self.assertEqual(sel["experiments"]["psrr"], "sim/psrr/records/20261010-163854-0127eb7.md")  # 45-point primary
        self.assertNotIn(PSRR_STUDY_MD, json.dumps(sel["experiments"]))

    def test_hashed_population_and_retained_evidence(self):
        st = self.build()["supplementary"][self.KEY]
        self.assertFalse(st["graded"])
        self.assertTrue(st["bound_free"])
        self.assertIsNone(st["verdict"])
        self.assertEqual(st["rows"], ["psrr"])
        self.assertEqual(st["sha256"], hashlib.sha256((REPO / PSRR_STUDY_MD).read_bytes()).hexdigest())
        self.assertEqual(st["dut_check"], {"matches_selected_records": True, "matches_current_netlist": True})
        self.assertEqual(st["measurement_config"]["status"], "current")
        self.assertEqual((st["scope"]["cells"], len(st["scope"]["points"]), st["scope"]["full_pvt_cross_product"]),
                         (405, 45, True))
        self.assertEqual(st["population"], {"cells": 405, "typical_passive_cells": 45, "non_typical_cells": 360,
                                            "excitations": ["dm", "vdd", "vss"]})
        rv = st["retained_evidence"]
        self.assertEqual((rv["path"], rv["psrr_dat_files"]), (PSRR_STUDY_DIR, 405))
        # independent recomputation of the aggregate hash over every retained curve
        names = sorted(p.name for p in (REPO / PSRR_STUDY_DIR).glob("*.psrr.dat"))
        self.assertEqual(len(names), 405)
        listing = {n: f"{hashlib.sha256((REPO / PSRR_STUDY_DIR / n).read_bytes()).hexdigest()}  {n}\n" for n in names}
        order = [f"{m}__r-{r}__c-{c}_{t}c_{v}v.psrr.dat" for m in ("typical", "ff", "ss", "fs", "sf")
                 for t in ("-40", "27", "125") for v in ("2.97", "3.30", "3.63")
                 for r in ("typical", "best", "worst") for c in ("typical", "best", "worst")]
        self.assertEqual(rv["sha256"], hashlib.sha256("".join(listing[n] for n in order).encode()).hexdigest())
        pj = f"{PSRR_STUDY_DIR}/partitions.json"
        self.assertEqual(rv["partitions"], {"path": pj, "sha256": hashlib.sha256((REPO / pj).read_bytes()).hexdigest(),
                                            "excitations": {"dm": 405, "vdd": 405, "vss": 405}})

    def test_both_rails_both_frequencies_binding_cells_and_deltas(self):
        f = self.build()["supplementary"][self.KEY]["figures"]
        self.assertEqual(list(f), ["PSRR+ 1 kHz", "PSRR+ 10 kHz", "PSRR- 1 kHz", "PSRR- 10 kHz"])
        cell = lambda c: (c["value"], c["point"], c["rz"], c["cc"])
        self.assertEqual(cell(f["PSRR+ 10 kHz"]["worst"]), ("61.09", "ss / 125 C / 2.97 V", "worst", "worst"))
        self.assertEqual(cell(f["PSRR+ 1 kHz"]["worst"]), ("81.02", "ss / 125 C / 2.97 V", "worst", "worst"))
        self.assertEqual(cell(f["PSRR- 1 kHz"]["worst"]), ("101.01", "ss / -40 C / 2.97 V", "best", "worst"))
        self.assertEqual(cell(f["PSRR- 10 kHz"]["worst"]), ("100.88", "ss / -40 C / 2.97 V", "best", "worst"))
        self.assertEqual(cell(f["PSRR- 10 kHz"]["best"]), ("116.24", "ff / 125 C / 3.30 V", "best", "best"))
        p10 = f["PSRR+ 10 kHz"]
        self.assertEqual(p10["typical_passive_worst"], {"value": "61.90", "cells": 45, "primary_record_worst": "61.90"})
        self.assertEqual(p10["passive_shift_of_worst"], "-0.81")
        d = p10["delta_vs_typical"]
        self.assertEqual((d["cells"], d["max_abs"]), (360, "0.899"))
        self.assertEqual(cell(d["most_negative"]), ("-0.815", "ff / 125 C / 3.63 V", "worst", "worst"))
        self.assertEqual(cell(d["most_positive"]), ("+0.899", "ff / 125 C / 3.63 V", "best", "best"))
        self.assertEqual(f["PSRR- 1 kHz"]["delta_vs_typical"]["most_negative"]["value"], "-0.625")
        for fig in f.values():  # no bound, no verdict, no pass counts
            for k in ("bound_text", "bound_value", "verdict", "verdict_in_record", "pass", "fail", "failing_cells"):
                self.assertNotIn(k, fig)

    def test_no_verdict_and_graded_counts_and_bounds_unchanged(self):
        with_s, without = self.build(), self.build(self.without())
        self.assertEqual(with_s["summary"], without["summary"])
        for a, b in zip(with_s["rows"], without["rows"]):
            for k in ("status", "verdict", "points_pass", "points_total", "worst", "worst_corner", "coverage",
                      "spec_bound", "source", "figures"):
                self.assertEqual(a[k], b[k], (a["key"], k))
        r = rows_by_key(with_s)["psrr"]
        self.assertEqual((r["status"], r["verdict"], r["points_total"]), ("proposed-not-graded", None, None))
        self.assertEqual(r["coverage"]["points"], 45)
        self.assertEqual(r["source"]["record_id"], "20261010-163854-0127eb7")
        note = next(l for l in r["limitations"] if PSRR_STUDY_REC in l)
        self.assertIn("no verdict", note)
        self.assertIn("PSRR+ 10 kHz worst 61.09 dB at ss / 125 C / 2.97 V, RZ worst, CC worst", note)
        self.assertNotIn(PSRR_STUDY_REC, " ".join(rows_by_key(with_s)["cmrr"]["limitations"]))
        md = cr.render_md(with_s)
        sec = md.split(f"### {self.KEY}", 1)[1].split("\n### ", 1)[0]
        self.assertIsNone(re.search(r"\b(PASS|FAIL)\b", sec.replace("PASS/FAIL verdict", "")), sec)
        self.assertIn("| PSRR+ 10 kHz | 61.09 | ss / 125 C / 2.97 V / RZ worst / CC worst |", sec)
        for frag in ("bias-generator supply sensitivity is excluded", "no RZ/CC mismatch and no layout parasitics",
                     "matched devices", "fleet runner klt 0.5.0 vs client klt"):
            self.assertIn(frag, sec)
        self.assertNotIn(PSRR_STUDY_REC, cr.render_md(without))

    def test_evidence_sidecar_lists_the_study_ungraded(self):
        ev = json.loads(cr.render_evidence(cr.render_json(self.build())))
        s = ev["report_supplementary"][self.KEY]
        self.assertEqual((s["graded"], s["bound_free"], s["verdict"], s["cells"], s["path"]),
                         (False, True, None, 405, PSRR_STUDY_MD))
        self.assertEqual(s["worst_db"], {"PSRR+ 1 kHz": "81.02", "PSRR+ 10 kHz": "61.09", "PSRR- 1 kHz": "101.01",
                                         "PSRR- 10 kHz": "100.88"})
        ev0 = json.loads(cr.render_evidence(cr.render_json(self.build(self.without()))))
        self.assertEqual(ev["report_summary"], ev0["report_summary"])
        self.assertNotIn(self.KEY, ev0["report_supplementary"])

    def test_ratified_psrr_row_has_no_grader(self):
        self.edit("spec/target-spec.md", "≥ 50 dB each at 10 kHz [DR-6] — proposed, not ratified**",
                  "≥ 50 dB each at 10 kHz [DR-6]**")
        self.rejects(r"no longer tags it 'proposed, not ratified'; this report has no grader")

    # ---- population: missing / duplicate / unexpected cells ----
    def test_missing_cell_rejected(self):
        self.edit(PSRR_STUDY_MD, self.CELL, "")
        self.rejects(r"'## All 405 cells' is missing 1 of 405 cells \(first: typical / -40 C / 2.97 V / RZ typical / CC best\)")

    def test_duplicate_cell_rejected(self):
        self.edit(PSRR_STUDY_MD, self.CELL, self.CELL.replace("CC best", "CC typical"))
        self.rejects(r"duplicate cell typical / -40 C / 2.97 V / RZ typical / CC typical")

    def test_unexpected_cell_rejected(self):
        self.edit(PSRR_STUDY_MD, self.CELL, self.CELL.replace("2.97 V", "2.50 V"))
        self.rejects(r"unexpected cell typical / -40 C / 2.50 V / RZ typical / CC best")

    def test_unknown_level_or_corner_rejected(self):
        self.edit(PSRR_STUDY_MD, self.CELL, self.CELL.replace("CC best", "CC slow"))
        self.rejects(r"malformed cell identity")

    def test_missing_retained_curve_rejected(self):
        (self.root / self.BINDING_DAT).unlink()
        self.rejects(r"retained per-cell curve is missing: .*ss__r-worst__c-worst_125c_2.97v.psrr.dat")

    def test_partition_union_must_be_exact(self):
        p = self.root / PSRR_STUDY_DIR / "partitions.json"
        d = json.loads(p.read_text())
        d["plan"]["vss"][2]["cells"].pop(0)
        p.write_text(json.dumps(d))
        self.rejects(r"'vss' partitions are not exactly the 405-cell population \(0 overlapping, 0 unexpected, 1 missing")
        d["plan"]["vss"][2]["cells"].append(d["plan"]["vss"][0]["cells"][0])
        p.write_text(json.dumps(d))
        self.rejects(r"'vss' partitions are not exactly the 405-cell population \(1 overlapping")

    # ---- nonfinite / disagreeing values ----
    def test_nonfinite_table_value_rejected(self):
        self.edit(PSRR_STUDY_MD, self.CELL, self.CELL.replace("| 66.32 |", "| nan |"))
        self.rejects(r"malformed or nonfinite value 'nan'")

    def test_lower_bound_table_value_rejected(self):
        self.edit(PSRR_STUDY_MD, self.CELL, self.CELL.replace("| 66.32 |", "| >= 66.32 |"))
        self.rejects(r"malformed or nonfinite value '>= 66.32'")

    def test_nonfinite_retained_value_rejected(self):
        p = self.root / self.BINDING_DAT
        lines = p.read_text().splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith("1.00000000e+03"))
        lines[i] = " ".join(lines[i].split()[:4] + ["nan"] + lines[i].split()[5:])
        p.write_text("\n".join(lines) + "\n")
        self.rejects(r"ss__r-worst__c-worst_125c_2.97v.psrr.dat: nonfinite value at 1000 Hz")

    def test_tampered_retained_curve_rejected(self):
        p = self.root / self.BINDING_DAT
        lines = p.read_text().splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith("1.00000000e+04"))
        c = lines[i].split()
        c[4] = f"{float(c[4]) + 0.5:.8e}"
        lines[i] = " ".join(c)
        p.write_text("\n".join(lines) + "\n")
        self.rejects(r"PSRR\+ 10 kHz of ss / 125 C / 2.97 V / RZ worst / CC worst states 61.09 dB but the retained "
                     r"curves give 61.5876")

    # ---- contradictory extrema vs the rounded header claims ----
    def test_misnamed_binding_cell_rejected(self):
        self.edit(PSRR_STUDY_MD, self.WORST_10K, self.WORST_10K.replace("RZ worst / CC worst", "RZ typical / CC typical"))
        self.rejects(r"PSRR\+ 10 kHz worst names ss / 125 C / 2.97 V / RZ typical / CC typical but the retained curves "
                     r"put it at ss / 125 C / 2.97 V / RZ worst / CC worst .*contradictory extremum")

    def test_wrong_worst_value_rejected(self):
        self.edit(PSRR_STUDY_MD, self.WORST_10K, self.WORST_10K.replace("**61.09**", "**61.19**"))
        self.rejects(r"PSRR\+ 10 kHz worst states 61.19 dB but the retained curves give 61.0876")

    def test_contradictory_delta_extreme_rejected(self):
        row = "| PSRR- 1 kHz | -0.625 | typical / 125 C / 3.63 V / RZ worst / CC worst |"
        self.edit(PSRR_STUDY_MD, row, row.replace("RZ worst / CC worst", "RZ best / CC worst"))
        self.rejects(r"PSRR- 1 kHz most negative delta names .*RZ best / CC worst.*contradictory extremum")

    def test_wrong_typical_passive_worst_rejected(self):
        self.edit(PSRR_STUDY_MD, "| 66.77 | ff / -40 C / 3.63 V / RZ best / CC best | 61.90 | -0.81 |",
                  "| 66.77 | ff / -40 C / 3.63 V / RZ best / CC best | 62.90 | -0.81 |")
        self.rejects(r"PSRR\+ 10 kHz worst over the typical-passive cells states 62.90 dB")

    def test_disagreement_with_the_primary_record_rejected(self):
        self.edit("sim/psrr/records/20261010-163854-0127eb7.md", "| 10 kHz | **61.90** |", "| 10 kHz | **61.95** |")
        self.rejects(r"PSRR\+ 10 kHz worst over the typical-passive cells \(61.8991 dB\) disagrees with the selected "
                     r"PSRR grid record \(61.95 dB\)")

    def test_missing_or_duplicate_figure_row_rejected(self):
        self.edit(PSRR_STUDY_MD, self.WORST_10K, self.WORST_10K.replace("PSRR+ 10 kHz", "PSRR+ 1 kHz"))
        self.rejects(r"duplicate figure 'PSRR\+ 1 kHz'")

    def test_record_claiming_a_verdict_rejected(self):
        self.edit(PSRR_STUDY_MD, "- **Status**: measured, **no pass or fail verdict**.", "- **Status**: **PASS**.")
        self.rejects(r"does not state 'measured, \*\*no pass or fail verdict\*\*'")

    def test_missing_limitation_statement_rejected(self):
        self.edit(PSRR_STUDY_MD, "**bias-generator supply sensitivity is excluded**", "bias handled")
        self.rejects(r"does not state its limitation '\*\*bias-generator supply sensitivity is excluded\*\*'")

    # ---- DUT and measurement-configuration freshness ----
    def test_wrong_dut_rejected(self):
        self.edit(PSRR_STUDY_MD, "33b58dac62c747de979b33423bb54148cab3eb334fd9fab6051b7ed99226e41b", "0" * 64)
        self.rejects(r"supplementary psrr-passive-corners-full:20261011-102729-6612014: the side study measures DUT "
                     r"0000000000000000, the selected records 33b58dac62c747de")

    def test_stale_dut_without_primary_selection_rejected(self):
        self.edit(PSRR_STUDY_MD, "33b58dac62c747de979b33423bb54148cab3eb334fd9fab6051b7ed99226e41b", "0" * 64)
        m = {"experiments": {}, "supplementary": {self.KEY: PSRR_STUDY_MD}}
        self.rejects(r"stale DUT: supplementary psrr-passive-corners-full", m)

    def test_stale_passive_section_map_rejected_and_archival_discloses(self):
        # the passive factors / sections are fingerprinted (issue #155): a changed map stales ONLY this study
        self.edit("sim/passive_corners.py", 'RES_FACTOR = {"typical": 1.0, "best": 0.8, "worst": 1.2}',
                  'RES_FACTOR = {"typical": 1.0, "best": 0.85, "worst": 1.2}')
        self.rejects(r"stale measurement configuration: supplementary psrr-passive-corners-full:20261011-102729-6612014 "
                     r"was measured .*passive_study\.rz\.best\.factor")
        rep = self.build(archival=True)
        self.assertEqual(rep["supplementary"][self.KEY]["measurement_config"]["status"], "stale")
        self.assertTrue(any(l.startswith(f"supplementary {self.KEY}: measurement configuration differs")
                            for l in rep["limitations"]))
        self.assertEqual(rep["sources"]["psrr"]["measurement_config"]["status"], "current")  # primary unaffected

    def test_stale_bench_rejected(self):
        self.edit("sim/psrr/testbench/tb_psrr.spice", "CL vout 0 2p", "CL vout 0 3p")
        self.rejects(r"stale measurement configuration")
        self.rejects(r"stale measurement configuration: supplementary psrr-passive-corners-full:.*bench",
                     {"experiments": {}, "supplementary": {self.KEY: PSRR_STUDY_MD}})
        mc = self.build(archival=True)["supplementary"][self.KEY]["measurement_config"]
        self.assertEqual(mc["status"], "stale")
        self.assertIn("bench", mc["differs"])

    def test_tampered_fingerprint_inputs_rejected(self):
        self.edit(PSRR_STUDY_MD, '"cells":405,"levels"', '"cells":404,"levels"')
        self.rejects(r"retained measurement-fingerprint inputs hash to .* \(record altered or corrupt\)")

    def test_historical_artifacts_untouched_by_generation(self):
        before = {p: p.read_bytes() for p in [self.root / PSRR_STUDY_MD, *(self.root / PSRR_STUDY_DIR).iterdir()]}
        cr.render_md(self.build())
        self.assertEqual(before, {p: p.read_bytes() for p in before})


class StepExtractionHelpers(unittest.TestCase):
    """Issue #198: the offline re-derivation helpers on synthetic waveforms (no record needed):
    invalid paths return None (never a fast settle), not-settled stays `math.inf`, and the
    drift rule hides nothing inside the reference window."""

    TSTOP, RISE, FALL, SB, RW = 1.1e-6, 100e-9, 600e-9, 10e-9, 50e-9
    P = {"step_v": 0.1, "rise_t": RISE, "fall_t": FALL, "tstop": TSTOP, "sample_before": SB,
         "ref_window": RW, "settle_bands": [0.01, 0.001], "mono_tol": 0.001, "drift_frac": 0.1}

    def wave(self, tau=15e-9, amp=0.1, fall_amp=None):
        """Ideal first-order follower: vi steps `amp` about 1.6 V at RISE and back at FALL;
        vo tracks it with time constant `tau` (`fall_amp` overrides the falling excursion)."""
        fall_amp = -amp if fall_amp is None else fall_amp
        r, f = self.RISE, self.FALL
        t = [k * 1e-9 for k in range(1101)]

        def vo(x):
            if x < r:
                return 1.6 - amp / 2
            if x < f:
                return 1.6 + amp / 2 - amp * math.exp(-(x - r) / tau)
            return 1.6 + amp / 2 + fall_amp * (1 - math.exp(-(x - f) / tau))

        def vi(x):
            return 1.6 - amp / 2 if (x < r or x >= f) else 1.6 + amp / 2
        return t, [vo(x) for x in t], [vi(x) for x in t]

    def edge(self, t, vo, vi, name="fall"):
        n, e0, e1, d = next(e for e in cr._STEP_EDGES if e[0] == name)
        return cr._step_edge(t, vo, vi, self.P[e0], self.P[e1] if e1 else None, d, self.P)

    def test_ideal_first_order_response_settles_monotonically(self):
        e = self.edge(*self.wave())
        self.assertTrue(e["monotonic"])
        for b in self.P["settle_bands"]:
            self.assertLess(e["ts"][b], math.inf)
        self.assertAlmostEqual(e["overshoot_pct"], 0.0, places=9)

    def test_output_not_at_the_input_before_the_edge_is_invalid(self):
        t, vo, vi = self.wave()
        e = self.edge(t, [v - 0.05 for v in vo], vi)  # dead amplifier: output 50 mV off pre-edge
        self.assertIsNone(e)

    def test_output_never_reaching_the_input_is_invalid(self):
        t, vo, vi = self.wave(fall_amp=0.1)  # falling edge drives the output AWAY from the input
        self.assertIsNone(self.edge(t, vo, vi))

    def test_wrong_step_size_is_invalid(self):
        t, vo, vi = self.wave(amp=0.05)  # 50 mV step: outside the required 100 mV +-20 %
        self.assertIsNone(self.edge(t, vo, vi))

    def test_still_outside_the_band_in_the_reference_window_is_not_settled(self):
        t, vo, vi = self.wave(tau=200e-9)  # tau = 200 ns: far from settled inside the 500 ns window
        e = self.edge(t, vo, vi)
        self.assertEqual(e["ts"][0.001], math.inf)

    def test_slow_tail_in_the_reference_window_is_not_settled(self):
        # settles visually, then drifts by > DRIFT_FRAC * band across the reference window
        t, vo, vi = self.wave()

        def drift(tt):
            return -3e-5 * max(0.0, (tt - 1.0e-6) / 1.0e-7)  # -30 uV across the last 100 ns
        vo = [v + drift(x) for v, x in zip(vo, t)]
        e = self.edge(t, vo, vi)
        self.assertEqual(e["ts"][0.001], math.inf)  # 0.03 % drift > 0.1 x 0.1 % band
        self.assertLess(e["ts"][0.01], math.inf)    # 1 % band tolerates the small drift

    def test_ringing_beyond_the_monotonic_tolerance_flags_non_monotonic(self):
        t, vo, vi = self.wave()

        def ring(tt):
            return 8e-3 * math.exp(-(tt - self.RISE) / 40e-9) * math.sin(2 * math.pi * (tt - self.RISE) / 60e-9) \
                if tt >= self.RISE else 0.0
        vo = [v + ring(x) for v, x in zip(vo, t)]
        e = self.edge(t, vo, vi, "rise")
        self.assertFalse(e["monotonic"])
        self.assertGreater(e["overshoot_pct"], 0.0)


class StepResponseFollowerStudy(unittest.TestCase):
    """Issue #198: the follower step-response record is explicitly selected, hashed and
    re-derived offline from its retained per-point transients as UNGRADED evidence with no
    spec row at all: primary selection / ratified counts / verdicts unchanged, not-settled
    results explicit, pre-retune DUT and changed configuration rejected."""

    KEY = "step-response-follower"

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))

    def tearDown(self):
        self._t.cleanup()

    def build(self, m=None, **kw):
        return cr.build(self.root, m or manifest(), **kw)

    def edit(self, rel, old, new, count=1):
        p = self.root / rel
        t = p.read_text()
        self.assertIn(old, t)
        p.write_text(t.replace(old, new, count))

    def rejects(self, rx, m=None, **kw):
        with self.assertRaisesRegex(cr.ReportError, rx):
            self.build(m, **kw)

    def without(self):
        m = manifest()
        m["supplementary"].pop(self.KEY)
        return m

    def not_settled_root(self):
        """A consistent record+evidence pair whose worst result is NOT SETTLED: +200 uV held on
        vout inside the falling edge's reference window of one point, with the record's rounded
        table, summary line and worst-points table repaired to what the mutated transient shows."""
        p = self.root / STEP_FIX_DAT
        lines = p.read_text().splitlines()
        out = []
        for ln in lines:
            if not ln.startswith("#") and ln.strip():
                c = ln.split()
                if float(c[0]) >= 1.06e-6:
                    c[1] = f"{float(c[1]) + 2.0e-4:.10e}"
                    ln = " ".join(c)
            out.append(ln)
        p.write_text("\n".join(out) + "\n")
        self.edit(STEP_MD,
                  "| `typical` | 27 | 3.30 | 0.00 / 0.00 | 0.00 | 44.6 / 41.9 | 67.2 / 64.0 | yes | 17.1 / 15.4 | 77.60 | 16.403 | |",
                  "| `typical` | 27 | 3.30 | 0.00 / 0.16 | 0.00 | 44.6 / not settled | 67.2 / not settled | no | 17.1 / 15.3 | 77.60 | 16.403 | |")
        self.edit(STEP_MD,
                  "worst 1 % settling **47.1 ns** at sf / -40 C / 2.97 V; worst 0.1 % settling **72.2 ns** at "
                  "sf / -40 C / 2.97 V; monotonic at 33/45 valid points.",
                  "worst 1 % settling **not settled** at typical / 27 C / 3.30 V; worst 0.1 % settling "
                  "**not settled** at typical / 27 C / 3.30 V; monotonic at 32/45 valid points.")
        self.edit(STEP_MD, "| Settling to 1 % (worse edge) | 47.1 ns | sf / -40 C / 2.97 V |",
                  "| Settling to 1 % (worse edge) | not settled (1 point not settled) | typical / 27 C / 3.30 V |")
        self.edit(STEP_MD, "| Settling to 0.1 % (worse edge) | 72.2 ns | sf / -40 C / 2.97 V |",
                  "| Settling to 0.1 % (worse edge) | not settled (1 point not settled) | typical / 27 C / 3.30 V |")
        self.edit(STEP_MD,
                  "| Non-monotonic points | 12/45 | typical / 125 C / 2.97 V, typical / 125 C / 3.30 V, "
                  "typical / 125 C / 3.63 V, ff / -40 C / 2.97 V, ff / -40 C / 3.30 V, ff / -40 C / 3.63 V ... |",
                  "| Non-monotonic points | 13/45 | typical / 27 C / 3.30 V, typical / 125 C / 2.97 V, "
                  "typical / 125 C / 3.30 V, typical / 125 C / 3.63 V, ff / -40 C / 2.97 V, ff / -40 C / 3.30 V ... |")

    # ---- positive: selected, hashed, displayed ----
    def test_explicitly_selected_beside_the_unchanged_primary_records(self):
        sel = json.loads(MANIFEST.read_text())
        self.assertEqual(sel["supplementary"][self.KEY], STEP_MD)
        self.assertNotIn(STEP_MD, json.dumps(sel["experiments"]))
        self.assertEqual(sel["experiments"]["gain-gbw-pm"], "sim/gain-gbw-pm/records/20261010-161514-0127eb7.md")

    def test_hashed_population_and_retained_evidence(self):
        st = self.build()["supplementary"][self.KEY]
        self.assertFalse(st["graded"])
        self.assertTrue(st["bound_free"])
        self.assertIsNone(st["verdict"])
        self.assertEqual(st["rows"], [])
        self.assertIn("no spec row", st["grading"])
        self.assertEqual(st["sha256"], hashlib.sha256((REPO / STEP_MD).read_bytes()).hexdigest())
        self.assertEqual(st["dut_check"], {"matches_selected_records": True, "matches_current_netlist": True})
        self.assertEqual(st["measurement_config"]["status"], "current")
        self.assertEqual((st["scope"]["cells"], len(st["scope"]["points"]), st["scope"]["full_pvt_cross_product"]),
                         (45, 45, True))
        self.assertEqual(st["population"], {"points": 45, "valid": 45, "invalid": 0, "monotonic": 33,
                                            "not_settled": {"1 %": 0, "0.1 %": 0}, "edges_per_point": ["rise", "fall"]})
        rv = st["retained_evidence"]
        self.assertEqual((rv["path"], rv["dat_files"]), (STEP_DIR, 45))
        names = [f"{m}_{t}c_{v}v.dat" for m in ("typical", "ff", "ss", "fs", "sf")
                 for t in ("-40", "27", "125") for v in ("2.97", "3.30", "3.63")]
        listing = "".join(f"{hashlib.sha256((REPO / STEP_DIR / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
        self.assertEqual(rv["sha256"], hashlib.sha256(listing.encode()).hexdigest())

    def test_figures_match_the_record_and_the_evidence(self):
        f = self.build()["supplementary"][self.KEY]["figures"]
        self.assertEqual(list(f), ["Overshoot (worse edge)", "Overshoot rising edge", "Overshoot falling edge",
                                   "Settling to 1 % (worse edge)", "Settling to 0.1 % (worse edge)",
                                   "Monotonicity", "Validity"])
        self.assertEqual((f["Overshoot (worse edge)"]["value"], f["Overshoot (worse edge)"]["point"]),
                         ("0.90", "fs / 125 C / 2.97 V"))
        self.assertEqual(f["Overshoot (worse edge)"]["range"], ["0.00", "0.90"])
        self.assertEqual((f["Overshoot rising edge"]["value"], f["Overshoot rising edge"]["point"]),
                         ("0.12", "ss / 125 C / 2.97 V"))
        self.assertEqual((f["Overshoot falling edge"]["value"], f["Overshoot falling edge"]["point"]),
                         ("0.90", "fs / 125 C / 2.97 V"))
        s1 = f["Settling to 1 % (worse edge)"]
        self.assertEqual((s1["value"], s1["point"]), ("47.1", "sf / -40 C / 2.97 V"))
        self.assertEqual(s1["not_settled"]["count"], 0)
        s2 = f["Settling to 0.1 % (worse edge)"]
        self.assertEqual((s2["value"], s2["point"]), ("72.2", "sf / -40 C / 2.97 V"))
        self.assertEqual(f["Monotonicity"], {"unit": "points", "non_monotonic": 12, "of_valid": 45,
                                             "points": ["typical / 125 C / 2.97 V", "typical / 125 C / 3.30 V",
                                                        "typical / 125 C / 3.63 V", "ff / -40 C / 2.97 V",
                                                        "ff / -40 C / 3.30 V", "ff / -40 C / 3.63 V"],
                                             "list_complete": False})
        self.assertEqual(f["Validity"], {"unit": "points", "invalid": 0, "of": 45})
        for k in ("bound_text", "verdict", "pass", "fail"):
            for fig in f.values():
                self.assertNotIn(k, fig)

    def test_no_verdict_and_graded_counts_and_bounds_unchanged(self):
        with_s, without = self.build(), self.build(self.without())
        self.assertEqual(with_s["summary"], without["summary"])
        for a, b in zip(with_s["rows"], without["rows"]):
            for k in ("status", "verdict", "points_pass", "points_total", "worst", "worst_corner", "coverage",
                      "spec_bound", "source", "figures"):
                self.assertEqual(a[k], b[k], (a["key"], k))
        self.assertEqual(with_s["rows"], without["rows"])
        md = cr.render_md(with_s)
        sec = md.split(f"### {self.KEY}", 1)[1].split("\n### ", 1)[0]
        self.assertIsNone(re.search(r"\b(PASS|FAIL)\b", sec.replace("PASS/FAIL verdict", "")), sec)
        self.assertIn("| Overshoot (worse edge) | 0.90 % of the realised step (range 0.00 .. 0.90) | fs / 125 C / 2.97 V |", sec)
        self.assertIn("| Settling to 0.1 % (worse edge) | 72.2 ns from the input edge | sf / -40 C / 2.97 V |", sec)
        self.assertIn("| Monotonicity | 12/45 valid points non-monotonic | - |", sec)
        st = with_s["supplementary"][self.KEY]["figures"]["Monotonicity"]
        self.assertIn("| Monotonicity -- non-monotonic points | first 6 of 12 listed | "
                      + "; ".join(st["points"]) + " |", sec)
        self.assertIn("no settling or overshoot row", sec)
        self.assertNotIn(STEP_REC, cr.render_md(without))
        self.assertTrue(any(l == f"no supplementary study selected for {self.KEY}: its ungraded observations "
                                "are not shown" for l in without["limitations"]))

    def test_evidence_sidecar_lists_the_study_ungraded(self):
        ev = json.loads(cr.render_evidence(cr.render_json(self.build())))
        s = ev["report_supplementary"][self.KEY]
        st = self.build()["supplementary"][self.KEY]
        self.assertEqual((s["graded"], s["bound_free"], s["verdict"], s["cells"], s["path"], s["rows"]),
                         (False, True, None, 45, STEP_MD, []))
        self.assertEqual(s["retained_evidence_sha256"], st["retained_evidence"]["sha256"])
        self.assertEqual(s["worst"], {"Overshoot (worse edge)": "0.90", "Overshoot rising edge": "0.12",
                                      "Overshoot falling edge": "0.90", "Settling to 1 % (worse edge)": "47.1",
                                      "Settling to 0.1 % (worse edge)": "72.2"})
        ev0 = json.loads(cr.render_evidence(cr.render_json(self.build(self.without()))))
        self.assertEqual(ev["report_summary"], ev0["report_summary"])
        self.assertNotIn(self.KEY, ev0["report_supplementary"])

    # ---- population: missing / duplicate / unexpected points ----
    def test_missing_point_rejected(self):
        self.edit(STEP_MD, "| `typical` | 27 | 3.30 | 0.00 / 0.00 | 0.00 | 44.6 / 41.9 | 67.2 / 64.0 | yes | "
                           "17.1 / 15.4 | 77.60 | 16.403 | |", "")
        self.rejects(r"'All 45 points' is missing 1 of 45 points \(first: typical / 27 C / 3\.30 V\)")

    def test_duplicate_point_rejected(self):
        self.edit(STEP_MD, "| `typical` | 27 | 3.63 | 0.00 / 0.00", "| `typical` | 27 | 3.30 | 0.00 / 0.00")
        self.rejects(r"duplicate point typical / 27 C / 3\.30 V under 'All 45 points'")

    def test_unexpected_point_rejected(self):
        self.edit(STEP_MD, "| `typical` | 27 | 3.30 | 0.00 / 0.00", "| `typical` | 27 | 2.50 | 0.00 / 0.00")
        self.rejects(r"unexpected point typical / 27 C / 2\.50 V under 'All 45 points'")

    # ---- header corner-matrix prose vs the fingerprinted population ----
    def test_header_corner_matrix_disagreeing_rejected(self):
        self.edit(STEP_MD, "  - Temperature: -40 C, 27 C, 125 C\n", "  - Temperature: -40 C, 27 C, 150 C\n")
        self.rejects(r"the header's corner-matrix line disagrees with the retained fingerprint population")

    def test_header_corner_matrix_vdd_disagreeing_rejected(self):
        self.edit(STEP_MD, "  - Supply VDD: 2.97 V, 3.30 V, 3.63 V (", "  - Supply VDD: 2.97 V, 3.30 V (")
        self.rejects(r"the header's corner-matrix line disagrees with the retained fingerprint population")

    def test_header_corner_matrix_line_missing_rejected(self):
        self.edit(STEP_MD, "  - Supply VDD: 2.97 V", "  - Supplies: 2.97 V")
        self.rejects(r"corner-matrix line disagrees .* \(a Process / Temperature / Supply VDD line is missing\)")

    # ---- rounded claims vs the retained evidence ----
    def test_summary_worst_value_disagreeing_rejected(self):
        self.edit(STEP_MD, "worst overshoot **0.90 %**", "worst overshoot **0.99 %**")
        self.rejects(r"the summary's worst overshoot states 0\.99 but the retained transients give 0\.90249")

    def test_summary_worst_settling_disagreeing_rejected(self):
        self.edit(STEP_MD, "worst 1 % settling **47.1 ns** at sf / -40 C / 2.97 V",
                  "worst 1 % settling **44.1 ns** at sf / -40 C / 2.97 V")
        self.rejects(r"the summary's worst settling to 1 % states 44\.1 but the retained transients give 47\.05")

    def test_summary_monotonic_count_disagreeing_rejected(self):
        self.edit(STEP_MD, "monotonic at 33/45 valid points", "monotonic at 40/45 valid points")
        self.rejects(r"Result summary states monotonic at 40/45 valid points but the retained transients give 33/45")

    def test_table_cell_disagreeing_with_evidence_rejected(self):
        self.edit(STEP_MD, "| `sf` | -40 | 2.97 | 0.00 / 0.00 | 0.00 | 47.1 / 45.1 | 72.2 / 70.5 | yes |",
                  "| `sf` | -40 | 2.97 | 0.00 / 0.00 | 0.00 | 48.1 / 45.1 | 72.2 / 70.5 | yes |")
        self.rejects(r"settling to 1\.0% of sf / -40 C / 2\.97 V states 48\.1 but the retained transients "
                     r"give 47\.05")

    def test_monotonic_flag_flip_rejected(self):
        self.edit(STEP_MD, "| `typical` | -40 | 2.97 | 0.00 / 0.00 | 0.00 | 45.8 / 43.4 | 70.2 / 68.3 | yes |",
                  "| `typical` | -40 | 2.97 | 0.00 / 0.00 | 0.00 | 45.8 / 43.4 | 70.2 / 68.3 | no |")
        self.rejects(r"monotonic flag of typical / -40 C / 2\.97 V states 'no' but the retained transients give 'yes'")

    def test_claiming_not_settled_when_the_evidence_settles_rejected(self):
        self.edit(STEP_MD, "| `typical` | -40 | 2.97 | 0.00 / 0.00 | 0.00 | 45.8 / 43.4 | 70.2 / 68.3 | yes |",
                  "| `typical` | -40 | 2.97 | 0.00 / 0.00 | 0.00 | 45.8 / 43.4 | not settled / 68.3 | yes |")
        self.rejects(r"settling to 0\.1% of typical / -40 C / 2\.97 V states 'not settled' but the retained "
                     r"transients give 70\.245")

    def test_missing_retained_transient_rejected(self):
        (self.root / f"{STEP_DIR}/fs_125c_2.97v.dat").unlink()
        self.rejects(r"retained per-point transient is missing: .*fs_125c_2\.97v\.dat")

    def test_tampered_retained_transient_rejected(self):
        p = self.root / f"{STEP_DIR}/fs_125c_2.97v.dat"
        lines = p.read_text().splitlines()
        i = next(i for i, l in enumerate(lines) if l and not l.startswith("#") and float(l.split()[0]) > 3.0e-7)
        c = lines[i].split()
        c[1] = f"{float(c[1]) + 5e-3:.10e}"
        lines[i] = " ".join(c)
        p.write_text("\n".join(lines) + "\n")
        self.rejects(r"rising overshoot of fs / 125 C / 2\.97 V states 0\.09 but the retained transients "
                     r"give 5\.00")

    # ---- a consistent not-settled record: explicit, never zero or omitted ----
    def test_not_settled_result_stays_explicit(self):
        self.not_settled_root()
        st = self.build()["supplementary"][self.KEY]
        self.assertEqual(st["population"]["monotonic"], 32)
        for band, ts in (("Settling to 1 % (worse edge)", "47.1"), ("Settling to 0.1 % (worse edge)", "72.2")):
            f = st["figures"][band]
            self.assertEqual((f["value"], f["point"]), ("not settled", "typical / 27 C / 3.30 V"))
            self.assertEqual((f["not_settled"]["count"], f["not_settled"]["points"]),
                             (1, ["typical / 27 C / 3.30 V"]))
            # the not-settled result is a named status, never zero or omitted
            self.assertIn("not settled", f["value"])
        md = cr.render_md(self.build())
        self.assertIn("| Settling to 1 % (worse edge) | not settled (1 point not settled) | typical / 27 C / 3.30 V |", md)
        self.assertIn("not settled within the edge window", md)
        ev = json.loads(cr.render_evidence(cr.render_json(self.build())))
        self.assertEqual(ev["report_supplementary"][self.KEY]["worst"]["Settling to 1 % (worse edge)"], "not settled")

    # ---- freshness: --check covers the step section and its sidecar entry ----
    def test_check_mode_covers_the_section_and_sidecar(self):
        mp = self.root / "m.json"
        mp.write_text(json.dumps(manifest()))
        args = ["--root", str(self.root), "--manifest", str(mp), "--out-dir", str(self.root / "o")]
        self.assertEqual(cr.main(args), 0)
        self.assertEqual(cr.main(args + ["--check"]), 0)
        out = self.root / "o"
        md, ev = out / f"{cr.OUT_NAME}.md", out / f"{cr.OUT_NAME}.evidence.json"
        self.assertIn(f"### {self.KEY}", md.read_text())
        self.assertIn(self.KEY, json.loads(ev.read_text())["report_supplementary"])
        good = {p: p.read_bytes() for p in (md, ev)}
        # a hand-edited step section or sidecar entry is stale
        md.write_text(md.read_text().replace("72.2 ns from the input edge", "70.0 ns from the input edge"))
        self.assertEqual(cr.main(args + ["--check"]), 1)
        md.write_bytes(good[md])
        ev.write_text(ev.read_text().replace('"72.2"', '"70.0"'))
        self.assertEqual(cr.main(args + ["--check"]), 1)
        ev.write_bytes(good[ev])
        self.assertEqual(cr.main(args + ["--check"]), 0)
        # consistently changed retained evidence (record + transient) makes the committed bundle stale
        self.not_settled_root()
        self.assertEqual(cr.main(args + ["--check"]), 1)
        self.assertEqual(cr.main(args), 0)
        self.assertEqual(cr.main(args + ["--check"]), 0)
        # deselecting the study makes the committed bundle stale too
        mp.write_text(json.dumps(self.without()))
        self.assertEqual(cr.main(args + ["--check"]), 1)

    # ---- invalid points: counted and excluded, never a value or a fast settle ----
    def truncate_fixture(self):
        """Cut the fixture point's retained transient short of TSTOP (the runner's INVALID rule)."""
        p = self.root / STEP_FIX_DAT
        p.write_text("\n".join(ln for ln in p.read_text().splitlines()
                               if ln.startswith("#") or not ln.strip() or float(ln.split()[0]) < 1.0e-6) + "\n")

    def test_invalid_point_counted_and_excluded(self):
        self.truncate_fixture()
        self.edit(STEP_MD,
                  "| `typical` | 27 | 3.30 | 0.00 / 0.00 | 0.00 | 44.6 / 41.9 | 67.2 / 64.0 | yes | 17.1 / 15.4 | 77.60 | 16.403 | |",
                  "| `typical` | 27 | 3.30 | n/a | n/a | n/a | n/a | n/a | n/a | 77.60 | 16.403 | INVALID: transient ends "
                  "early |")
        self.edit(STEP_MD, "45/45 points measured validly (0 invalid)", "44/45 points measured validly (1 invalid)")
        self.edit(STEP_MD, "monotonic at 33/45 valid points", "monotonic at 32/44 valid points")
        self.edit(STEP_MD, "| Non-monotonic points | 12/45 |", "| Non-monotonic points | 12/44 |")
        st = self.build()["supplementary"][self.KEY]
        self.assertEqual(st["population"], {"points": 45, "valid": 44, "invalid": 1, "monotonic": 32,
                                            "not_settled": {"1 %": 0, "0.1 %": 0}, "edges_per_point": ["rise", "fall"]})
        self.assertEqual(st["figures"]["Validity"], {"unit": "points", "invalid": 1, "of": 45})
        self.assertEqual(st["figures"]["Settling to 0.1 % (worse edge)"]["value"], "72.2")  # worst unchanged
        self.assertIn("| Validity | 1/45 points invalid | - |", cr.render_md(self.build()))

    def test_invalid_evidence_behind_a_valid_row_rejected(self):
        self.truncate_fixture()
        self.rejects(r"typical / 27 C / 3\.30 V is INVALID in the retained transients \(transient ends at .* before "
                     r"1\.1e-06 s\) but the table reports values")

    # ---- DUT identity and measurement-configuration freshness ----
    def test_pre_retune_selection_fails_dut_validation(self):
        rel = f"{STEP}/records/{STEP_PRE_RETUNE_REC}.md"
        self.rejects(r"supplementary step-response-follower:20261010-082214-a2cc91f: the side study measures DUT "
                     r"81fbd914f8254a49, the selected records 33b58dac62c747de",
                     {**manifest(), "supplementary": {self.KEY: rel}})
        self.rejects(r"stale DUT: supplementary step-response-follower",
                     {"experiments": {}, "supplementary": {self.KEY: rel}})

    def test_changed_step_configuration_fails_fingerprint_validation(self):
        self.edit(f"{STEP}/testbench/tb_step.spice", "CL vout 0 2p", "CL vout 0 3p")
        self.rejects(r"stale measurement configuration: supplementary step-response-follower:20261011-140956-cb03235 "
                     r"was measured with a different bench/analysis configuration than the current one "
                     r"\(changed bench\)")
        rep = self.build(archival=True)
        self.assertEqual(rep["supplementary"][self.KEY]["measurement_config"]["status"], "stale")
        self.assertTrue(any(l.startswith(f"supplementary {self.KEY}: measurement configuration differs")
                            for l in rep["limitations"]))
        self.assertEqual(rep["sources"]["gain-gbw-pm"]["measurement_config"]["status"], "current")  # primary unaffected

    def test_tampered_fingerprint_inputs_rejected(self):
        self.edit(STEP_MD, '"settle_bands":[0.01,0.001]', '"settle_bands":[0.01,0.002]')
        self.rejects(r"retained measurement-fingerprint inputs hash to .* \(record altered or corrupt\)")

    # ---- generation never touches the historical evidence ----
    def test_historical_artifacts_untouched_by_generation(self):
        paths = [self.root / STEP_MD, self.root / STEP_FIX_DAT] + list((self.root / STEP_DIR).glob("*.dat"))
        before = {p: p.read_bytes() for p in paths}
        cr.render_md(self.build())
        self.assertEqual(before, {p: p.read_bytes() for p in before})


class AtomicPublication(unittest.TestCase):
    """Issue #147: a failed candidate leaves the published bundle byte-identical."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._t.name))
        self.mp = self.root / "sel.json"
        self.mp.write_text(MANIFEST.read_text())
        self.out = self.root / "o"
        self.args = ["--root", str(self.root), "--manifest", str(self.mp), "--out-dir", str(self.out)]

    def tearDown(self):
        self._t.cleanup()

    def run_main(self, extra):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            return cr.main(self.args + extra), err.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*"))
                if p.is_file() and ("o" in p.relative_to(self.root).parts[:1] or p == self.mp)}

    def seed(self):
        self.assertEqual(self.run_main([])[0], 0)
        self.assertEqual(len(list(self.out.iterdir())), 3)

    def test_report_error_leaves_bundle_unchanged(self):
        self.seed()
        self.mp.write_text(json.dumps({"experiments": {}, "stale": True}))  # distinct pre-state
        before = self.snapshot()
        # a newer malformed record makes --latest select something that cannot build
        (self.root / "sim/gain-gbw-pm/records/99999999-000000-feedbee.md").write_text("not a record\n")
        rc, err = self.run_main(["--latest", "--update-manifest"])
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.snapshot(), before)

    def test_missing_output_case_stays_missing(self):
        # no committed bundle yet: a failed refresh must not create any output
        before = self.snapshot()
        (self.root / "sim/gain-gbw-pm/records/99999999-000000-feedbee.md").write_text("not a record\n")
        rc, _ = self.run_main(["--latest", "--update-manifest"])
        self.assertEqual(rc, 2)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.snapshot(), before)

    def test_render_failure_publishes_nothing(self):
        self.seed()
        before = self.snapshot()
        orig = cr.render_json
        cr.render_json = lambda rep: (_ for _ in ()).throw(RuntimeError("render boom"))
        try:
            with self.assertRaises(RuntimeError):
                self.run_main(["--latest", "--update-manifest"])
        finally:
            cr.render_json = orig
        self.assertEqual(self.snapshot(), before)

    def test_staging_failure_publishes_nothing(self):
        self.seed()
        before = self.snapshot()
        orig = os.replace
        os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("replace boom"))
        try:
            with self.assertRaises(OSError):
                self.run_main(["--latest", "--update-manifest"])
        finally:
            os.replace = orig
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p for p in self.root.rglob("*.tmp")], [])

    def test_success_publishes_selection_and_matching_bundle(self):
        old = json.loads(MANIFEST.read_text())
        old["experiments"] = {"cmrr": old["experiments"]["cmrr"]}  # stale partial selection, supplementary kept
        self.mp.write_text(json.dumps(old))
        rc, err = self.run_main(["--latest", "--update-manifest"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(self.mp.read_text()), json.loads(MANIFEST.read_text()))
        self.assertEqual(self.run_main(["--check"])[0], 0)  # bundle built from exactly the published selection
        self.assertEqual([p for p in self.root.rglob("*.tmp")], [])

    def test_check_with_update_manifest_writes_nothing(self):
        self.seed()
        self.mp.write_text(json.dumps({"experiments": {}}))
        before = self.snapshot()
        self.run_main(["--latest", "--update-manifest", "--check"])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
