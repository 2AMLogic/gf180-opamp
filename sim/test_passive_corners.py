#!/usr/bin/env python3
"""Offline tests of the shared passive-corner matrix, limited and full scope (issue #150).

Stdlib only: no simulator.

    python3 sim/test_passive_corners.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import passive_corners as pc  # noqa: E402


def _config(experiment: str):
    spec = importlib.util.spec_from_file_location(f"mc_{experiment}", HERE / experiment / "measurement_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emulate_klt(req: dict) -> set:
    """Cross product of the request's axes minus its `exclude` entries."""
    sup = req["corners"]["supply_v"]
    cells = set()
    for a in req["corners"]["process"]:
        for t in req["corners"]["temperature_c"]:
            for v in sup["vdd"]:
                e = {"process": a["name"], "temperature_c": t, "supply_v": {"vdd": v}}
                if e not in req.get("exclude", []):
                    cells.add((a["name"], float(t), float(v)))
    return cells


class ScopeTests(unittest.TestCase):
    def test_limited_is_unchanged(self):
        self.assertEqual(pc.PASSIVE_POINTS, [("fs", 125.0, 2.97), ("ss", 125.0, 2.97), ("typical", 27.0, 3.30)])
        self.assertEqual(pc.scope_points("limited"), pc.PASSIVE_POINTS)
        self.assertEqual(len(pc.passive_expected_keys()), 27)
        self.assertEqual(pc.passive_expected_keys(), pc.passive_expected_keys(pc.scope_points("limited")))

    def test_full_axes_match_the_45_point_grid(self):
        for exp in ("gain-gbw-pm", "slew-swing-power"):
            mc = _config(exp)
            self.assertEqual(list(pc.FULL_MOS), list(mc.CORNERS), exp)
            self.assertEqual(list(pc.FULL_TEMPS), list(mc.TEMPS_C), exp)
            self.assertEqual(list(pc.FULL_SUPPLIES), list(mc.SUPPLIES_V), exp)

    def test_full_is_405_cells_covering_every_combination(self):
        pts = pc.scope_points("full")
        self.assertEqual(len(pts), 45)
        keys = pc.passive_expected_keys(pts)
        self.assertEqual(len(keys), 405)
        self.assertEqual(len(set(keys)), 405)
        self.assertEqual(pc.SCOPE_CELLS, {"limited": 27, "full": 405})
        self.assertEqual({pc.split_passive_key(k)[0] for k in keys}, {"typical", "ff", "ss", "fs", "sf"})
        self.assertEqual({k[1] for k in keys}, {-40.0, 27.0, 125.0})
        self.assertEqual({k[2] for k in keys}, {2.97, 3.30, 3.63})
        self.assertEqual({pc.split_passive_key(k)[1:] for k in keys}, set(pc.passive_combos()))
        for mos in pc.FULL_MOS:
            for t in pc.FULL_TEMPS:
                for v in pc.FULL_SUPPLIES:
                    n = sum(1 for k in keys if pc.split_passive_key(k)[0] == mos and k[1] == t and k[2] == v)
                    self.assertEqual(n, 9, (mos, t, v))

    def test_full_request_is_the_whole_cross_product(self):
        req = pc.apply_passive_matrix({"corners": {}}, pc.scope_points("full"))
        self.assertEqual(len(req["corners"]["process"]), 45)
        self.assertNotIn("exclude", req)
        self.assertEqual(emulate_klt(req), set(pc.passive_expected_keys(pc.scope_points("full"))))

    def test_limited_request_is_unchanged(self):
        req = pc.apply_passive_matrix({"corners": {}})
        self.assertEqual(len(req["corners"]["process"]), 27)
        self.assertEqual(emulate_klt(req), set(pc.passive_expected_keys()))

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            pc.scope_points("everything")

    def test_scope_description_names_scope(self):
        self.assertIn("FULL", pc.scope_description("full"))
        self.assertIn("405", pc.scope_description("full"))
        self.assertIn("LIMITED", pc.scope_description("limited"))
        self.assertIn("27", pc.scope_description("limited"))


class CompletenessTests(unittest.TestCase):
    WANT = pc.passive_expected_keys(pc.scope_points("full"))

    def test_exact_set_passes(self):
        self.assertEqual(pc.key_set_problems(self.WANT, self.WANT), [])
        self.assertEqual(pc.key_set_problems(reversed(self.WANT), self.WANT), [])

    def test_omitted_cell_fails(self):
        p = pc.key_set_problems(self.WANT[1:], self.WANT)
        self.assertEqual(len(p), 1)
        self.assertIn("missing", p[0])

    def test_duplicate_cell_fails(self):
        p = pc.key_set_problems(self.WANT + [self.WANT[7]], self.WANT)
        self.assertEqual(len(p), 1)
        self.assertIn("duplicate", p[0])

    def test_unexpected_cell_fails(self):
        extra = (pc.passive_name("typical", "typical", "typical"), 85.0, 3.30)
        p = pc.key_set_problems(self.WANT + [extra], self.WANT)
        self.assertEqual(len(p), 1)
        self.assertIn("unexpected", p[0])

    def test_limited_results_do_not_satisfy_full(self):
        p = pc.key_set_problems(pc.passive_expected_keys(), self.WANT)
        self.assertEqual(len(p), 405 - 27)


class DeckSectionTests(unittest.TestCase):
    LIB = "/opt/pdk/libs.tech/ngspice/sm141064.ngspice"

    def deck(self, mos, r, c):
        return "\n".join(f".lib {self.LIB} {s}" for s in [mos, *pc.passive_sections(r, c)]) + "\n.lib /x/d.ngspice typical\n"

    def test_every_full_cell_deck_verifies_and_mismatches_fail(self):
        for k in pc.passive_expected_keys(pc.scope_points("full")):
            mos, r, c = pc.split_passive_key(k)
            self.assertEqual(pc.deck_section_problems(k, self.deck(mos, r, c)), [], k)
        k = (pc.passive_name("sf", "best", "worst"), -40.0, 3.63)
        self.assertTrue(pc.deck_section_problems(k, self.deck("sf", "worst", "worst")))
        self.assertTrue(pc.deck_section_problems(k, self.deck("ff", "best", "worst")))
        self.assertTrue(pc.deck_section_problems(k, self.deck("sf", "best", "best")))


if __name__ == "__main__":
    unittest.main()
