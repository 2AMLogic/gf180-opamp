#!/usr/bin/env python3
"""Tests for the layout-pilot negative-control gate predicate (issue #338).

No simulator, no klt, no PDK, no network: the gate's tripped predicate is a
pure function, exercised on inline dicts, plus one check that it reproduces
the verdicts recorded in the committed results.json.

    python3 layout/pilot/tools/test_pilot.py   # or: python3 -m unittest
"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pilot  # noqa: E402

RESULTS = HERE.parent / "results.json"


def ctl(status="mismatch", dperr=1, nc_sha="a" * 64, pos_sha="a" * 64):
    """An otherwise-tripping control carrying exactly the fields the gate reads."""
    return {"status": status, "device_property_errors": dperr,
            "layout_sha256_matches_positive": bool(nc_sha) and nc_sha == pos_sha}


class TestControlTripped(unittest.TestCase):
    def test_matching_hashes_trip(self):
        self.assertTrue(pilot.control_tripped(ctl()))

    def test_missing_hash_on_both_sides_does_not_trip(self):
        self.assertFalse(pilot.control_tripped(ctl(nc_sha=None, pos_sha=None)))

    def test_empty_hash_does_not_count_as_present(self):
        self.assertFalse(pilot.control_tripped(ctl(nc_sha="", pos_sha="a" * 64)))
        self.assertFalse(pilot.control_tripped(ctl(nc_sha="a" * 64, pos_sha="")))

    def test_differing_hashes_do_not_trip(self):
        self.assertFalse(pilot.control_tripped(ctl(nc_sha="a" * 64, pos_sha="b" * 64)))

    def test_missing_positive_hash_does_not_trip(self):
        self.assertFalse(pilot.control_tripped(ctl(nc_sha="a" * 64, pos_sha=None)))

    def test_status_match_does_not_trip(self):
        self.assertFalse(pilot.control_tripped(ctl(status="match")))

    def test_no_device_property_error_does_not_trip(self):
        self.assertFalse(pilot.control_tripped(ctl(dperr=0)))


class TestCommittedVerdictsPreserved(unittest.TestCase):
    def test_committed_results_still_trip_under_hardened_predicate(self):
        controls = json.loads(RESULTS.read_text())["negative_controls"]
        self.assertTrue(controls)
        for c in controls:
            self.assertTrue(c["layout_sha256"],
                            "%s: recorded layout_sha256 is not present" % c["cell"])
            self.assertTrue(c["layout_sha256_matches_positive"],
                            "%s: recorded hash match is not present-and-equal" % c["cell"])
            self.assertTrue(pilot.control_tripped(c),
                            "%s: hardened gate would flip the recorded verdict" % c["cell"])


if __name__ == "__main__":
    unittest.main()
