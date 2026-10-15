#!/usr/bin/env python3
"""Tests for manifests/check_integrator.py (issue #84). Scratch copies only.

    python3 manifests/test_check_integrator.py
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
import check_integrator as ci  # noqa: E402

REPO = HERE.parent
FILES = ["manifests/integrator.json", "manifests/gf180-opamp.signoff.json",
         "design/netlist/opamp_two_stage.spice", "design/opamp_two_stage.sym"]


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for f in FILES:
            (self.root / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / f, self.root / f)

    def tearDown(self):
        self.tmp.cleanup()

    def view(self, **over):
        p = self.root / "manifests/integrator.json"
        v = json.loads(p.read_text())
        v.update(over)
        p.write_text(json.dumps(v))

    def sub(self, rel, old, new):
        p = self.root / rel
        t = p.read_text()
        assert old in t
        p.write_text(t.replace(old, new, 1))

    def fails(self, needle):
        errs = ci.check(self.root)
        self.assertTrue(any(needle in e for e in errs), errs)

    def test_committed_passes(self):
        self.assertEqual(ci.check(REPO), [])

    def test_clean_copy_passes(self):
        self.assertEqual(ci.check(self.root), [])

    def test_unknown_top_cell(self):
        self.view(top_cell="nope")
        self.fails("top_cell")

    def test_port_renamed(self):
        self.sub("design/netlist/opamp_two_stage.spice", "vout ibias", "vo ibias")
        self.fails("netlist declaration order")

    def test_port_reordered(self):
        self.sub("design/netlist/opamp_two_stage.spice", "vinp vinn", "vinn vinp")
        self.fails("netlist declaration order")

    def test_view_reordered(self):
        v = json.loads((self.root / "manifests/integrator.json").read_text())
        v["ports"][2], v["ports"][3] = v["ports"][3], v["ports"][2]
        self.view(ports=v["ports"])
        self.fails("netlist declaration order")

    def test_wrong_direction(self):
        v = json.loads((self.root / "manifests/integrator.json").read_text())
        v["ports"][4]["direction"] = "in"
        self.view(ports=v["ports"])
        self.fails("'vout' direction 'in' but")

    def test_symbol_direction_changed(self):
        self.sub("design/opamp_two_stage.sym", "name=vout dir=out", "name=vout dir=inout")
        self.fails("'vout'")

    def test_missing_netlist(self):
        (self.root / "design/netlist/opamp_two_stage.spice").unlink()
        self.fails("does not exist")

    def test_multiline_declaration(self):
        self.sub("design/netlist/opamp_two_stage.spice",
                 "vinn vout ibias", "vinn\n**+ vout ibias")
        self.assertEqual(ci.check(self.root), [])

    def test_real_subckt_form(self):
        self.sub("design/netlist/opamp_two_stage.spice", "**.subckt", ".subckt")
        self.assertEqual(ci.check(self.root), [])

    def test_rung_disagrees_with_tier(self):
        self.view(rung="T1")
        self.fails("rung")

    def test_tier_flip_with_stale_view(self):
        p = self.root / "manifests/gf180-opamp.signoff.json"
        r = json.loads(p.read_text())
        r["tier"] = "T1"
        p.write_text(json.dumps(r))
        self.fails("expected 'T1'")

    def test_tier_flip_with_updated_view(self):
        p = self.root / "manifests/gf180-opamp.signoff.json"
        r = json.loads(p.read_text())
        r["tier"] = "T1"
        p.write_text(json.dumps(r))
        self.view(rung="T1")
        self.assertEqual(ci.check(self.root), [])

    def test_invalid_area(self):
        (self.root / "layout").mkdir()
        (self.root / "layout/x.gds").write_bytes(b"")
        for bad in (0, -1.0, "1", float("nan"), True):
            self.view(gds="layout/x.gds", area=bad)
            self.fails("area")

    def test_area_without_gds(self):
        self.view(area=0.01)
        self.fails("area")

    def test_missing_populated_gds(self):
        self.view(gds="layout/missing.gds", area=0.01)
        self.fails("gds")

    def test_populated_valid(self):
        (self.root / "layout").mkdir()
        (self.root / "layout/x.gds").write_bytes(b"")
        self.view(gds="layout/x.gds", area=0.0123)
        self.assertEqual(ci.check(self.root), [])

    def test_missing_key(self):
        p = self.root / "manifests/integrator.json"
        v = json.loads(p.read_text())
        del v["area"]
        p.write_text(json.dumps(v))
        self.fails("missing required key 'area'")


if __name__ == "__main__":
    unittest.main()
