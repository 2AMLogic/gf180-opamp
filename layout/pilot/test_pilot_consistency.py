#!/usr/bin/env python3
"""Offline tests for layout/pilot/check_consistency.py (issue #330).

Each case mutates one file in a scratch copy of the tree and asserts the
checker reports it (exit 1); the unmutated tree must pass (exit 0). Stdlib
only -- unittest, no klt, KLayout, PDK or simulator.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "layout" / "pilot"))
import check_consistency  # noqa: E402


def scratch() -> Path:
    """A minimal repo copy: only what the checker reads (tracked files)."""
    tmp = Path(tempfile.mkdtemp())
    files = subprocess.check_output(
        ["git", "-C", str(REPO), "ls-files",
         "layout/pilot", "design/netlist/opamp_two_stage.spice"],
        text=True).splitlines()
    for rel in files:
        if (REPO / rel).is_file():
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dst)
    return tmp


def edit_json(path: Path, fn):
    doc = json.loads(path.read_text())
    fn(doc)
    path.write_text(json.dumps(doc, indent=2))


class PilotConsistency(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)

    def check(self) -> int:
        return check_consistency.main(["--root", str(self.root)])

    def results_path(self) -> Path:
        return self.root / "layout/pilot/results.json"

    def cell(self, name):
        return next(c for c in json.loads(self.results_path().read_text())["cells"]
                    if c["cell"] == name)

    # ------------------------------------------------------------ baseline --
    def test_clean_tree_passes(self):
        self.assertEqual(self.check(), 0)

    # ------------------------------------------------- schematic relations --
    def test_netlist_dimension_edit_fails(self):
        n = self.root / "design/netlist/opamp_two_stage.spice"
        text = n.read_text().replace(
            "XMB1 ibias ibias vss vss nfet_03v3 L=2u W=6u",
            "XMB1 ibias ibias vss vss nfet_03v3 L=2u W=12u")
        self.assertNotEqual(text, n.read_text())
        n.write_text(text)
        self.assertEqual(self.check(), 1)

    def test_second_instance_dimension_drift_fails(self):
        # give XM5 XMB1's node list AND a drifting W: the sharing guard's
        # first disjunct is model+params equality (tools/pilot.py's
        # cards[other][1:] == first[1:]), so equal node lists must not wave
        # a dimension drift on the second instance through
        n = self.root / "design/netlist/opamp_two_stage.spice"
        text = n.read_text().replace(
            "XM5 tail ibias vss vss nfet_03v3 L=2u W=6u",
            "XM5 ibias ibias vss vss nfet_03v3 L=2u W=9u")
        self.assertNotEqual(text, n.read_text())
        n.write_text(text)
        self.assertEqual(self.check(), 1)

    def test_ref_netlist_edit_fails(self):
        f = self.root / "layout/pilot/ref/pilot_mim_cc.klt.spice"
        f.write_text(f.read_text() + "* edited\n")
        self.assertEqual(self.check(), 1)

    def test_gds_byte_edit_fails(self):
        g = self.root / "layout/pilot/gds/pilot_nfet_m7.gds"
        g.write_bytes(g.read_bytes() + b"\0")
        self.assertEqual(self.check(), 1)

    def test_missing_gds_fails_cleanly(self):
        (self.root / "layout/pilot/gds/pilot_nfet_m7.gds").unlink()
        self.assertEqual(self.check(), 1)

    # ----------------------------------------------------- results.json ---
    def test_gen_params_edit_fails(self):
        def bump(doc):
            next(c for c in doc["cells"] if c["cell"] == "pilot_nfet_m7")["gen_params"]["fingers"] += 1
        edit_json(self.results_path(), bump)
        self.assertEqual(self.check(), 1)

    def test_drc_scalar_edit_fails(self):
        def bump(doc):
            next(c for c in doc["cells"] if c["cell"] == "pilot_mim_cc")["drc_klt_curated"]["violations"] += 1
        edit_json(self.results_path(), bump)
        self.assertEqual(self.check(), 1)

    def test_lvs_scalar_edit_fails(self):
        def bump(doc):
            next(c for c in doc["cells"] if c["cell"] == "pilot_rpoly_rz")["lvs_klt"]["mismatch_count"] += 1
        edit_json(self.results_path(), bump)
        self.assertEqual(self.check(), 1)

    def test_extract_record_edit_fails(self):
        e = self.root / "layout/pilot/evidence/pilot_nfet_m7.extract.klt.json"
        edit_json(e, lambda d: d["device_counts"].__setitem__("nfet", d["device_counts"]["nfet"] + 1))
        self.assertEqual(self.check(), 1)

    def test_dropped_results_cell_fails(self):
        # results.json loses a cell: its schematic instance is left uncovered
        def drop(doc):
            doc["cells"] = [c for c in doc["cells"] if c["cell"] != "pilot_nfet_m7"]
        edit_json(self.results_path(), drop)
        self.assertEqual(self.check(), 1)

    def test_dropped_cells_json_entry_fails(self):
        # cells.json loses a cell: XRZ is covered by no pilot cell any more
        c = self.root / "layout/pilot/cells.json"

        def drop(doc):
            doc["cells"] = [x for x in doc["cells"] if x["cell"] != "pilot_rpoly_rz"]
        edit_json(c, drop)
        self.assertEqual(self.check(), 1)

    def test_kind_disagreement_fails(self):
        c = self.root / "layout/pilot/cells.json"
        edit_json(c, lambda d: next(x for x in d["cells"]
                                    if x["cell"] == "pilot_mim_cc").__setitem__("kind", "res"))
        self.assertEqual(self.check(), 1)

    def test_unknown_schema_fails_loudly(self):
        edit_json(self.results_path(),
                  lambda d: d.__setitem__("schema", "gf180-opamp.layout-pilot.results/9"))
        rc = self.check()
        self.assertEqual(rc, 1)
        errors = check_consistency.Checker(self.root).check()
        self.assertTrue(
            any("gf180-opamp.layout-pilot.results/9" in e for e in errors),
            f"unknown-schema failure does not name the version: {errors}")

    # ---------------------------------------------------- negative controls --
    def test_negctl_pins_nf_from_unmutated_card(self):
        # a future NEG_CONTROLS entry altering nf must still re-derive the
        # producer's card: tools/pilot.py captures nf as a default argument
        # from the unmutated card, so the emitted count must stay at 2
        prm = {"l": "2u", "w": "6u", "nf": "2"}
        text = check_consistency.negctl_ref_text(
            "pilot_nfet_mb1_m5", "mos", "nfet_03v3", prm, "nf", "4")
        self.assertIn("nf=2", text)
        self.assertNotIn("nf=4", text)

    def test_negative_control_envelope_reporting_match_fails(self):
        e = self.root / "layout/pilot/evidence/pilot_nfet_mb1_m5.negctl-w.lvs.klt.json"
        edit_json(e, lambda d: d.__setitem__("status", "match"))
        self.assertEqual(self.check(), 1)

    def test_negative_control_record_edit_fails(self):
        def bump(doc):
            doc["negative_controls"][0]["error_count"] += 1
        edit_json(self.results_path(), bump)
        self.assertEqual(self.check(), 1)

    def test_missing_negative_control_fails(self):
        def drop(doc):
            doc["negative_controls"] = doc["negative_controls"][1:]
        edit_json(self.results_path(), drop)
        self.assertEqual(self.check(), 1)


if __name__ == "__main__":
    unittest.main()
