#!/usr/bin/env python3
"""Tests for the layout-pilot negative-control gate predicate (issue #338).

No simulator, no klt, no PDK, no network: the gate's tripped predicate is a
pure function, exercised on inline dicts, plus one check that it reproduces
the verdicts recorded in the committed results.json.

    python3 layout/pilot/tools/test_pilot.py   # or: python3 -m unittest
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pilot  # noqa: E402

RESULTS = HERE.parent / "results.json"


def ctl(status="mismatch", dperr=1, nc_sha="a" * 64, pos_sha="a" * 64):
    """An otherwise-tripping control carrying exactly the fields the gate reads.

    The hash conjunct is computed by the production predicate from two
    evidence-shaped environment dicts -- the same shape `main()` receives
    from the klt evidence files -- never re-implemented here, so a regression
    in `layout_sha256_matches` cannot pass this suite (issue #338 review)."""
    nc_env = {} if nc_sha is None else {"layout_sha256": nc_sha}
    pos_env = {} if pos_sha is None else {"layout_sha256": pos_sha}
    return {"status": status, "device_property_errors": dperr,
            "layout_sha256_matches_positive": pilot.layout_sha256_matches(nc_env, pos_env)}


class TestLayoutSha256Matches(unittest.TestCase):
    """The presence-and-equality predicate itself, on evidence-shaped dicts
    (including the `{}` / missing-key case a klt release dropping the field
    would actually produce)."""

    def test_equal_present_hashes_match(self):
        self.assertTrue(pilot.layout_sha256_matches({"layout_sha256": "a" * 64},
                                                    {"layout_sha256": "a" * 64}))

    def test_missing_key_on_both_sides_does_not_match(self):
        self.assertFalse(pilot.layout_sha256_matches({}, {}))

    def test_missing_key_on_one_side_does_not_match(self):
        self.assertFalse(pilot.layout_sha256_matches({}, {"layout_sha256": "a" * 64}))
        self.assertFalse(pilot.layout_sha256_matches({"layout_sha256": "a" * 64}, {}))

    def test_none_value_does_not_match(self):
        self.assertFalse(pilot.layout_sha256_matches({"layout_sha256": None},
                                                     {"layout_sha256": "a" * 64}))
        self.assertFalse(pilot.layout_sha256_matches({"layout_sha256": None},
                                                     {"layout_sha256": None}))

    def test_empty_hash_does_not_count_as_present(self):
        self.assertFalse(pilot.layout_sha256_matches({"layout_sha256": ""},
                                                     {"layout_sha256": "a" * 64}))

    def test_differing_hashes_do_not_match(self):
        self.assertFalse(pilot.layout_sha256_matches({"layout_sha256": "a" * 64},
                                                     {"layout_sha256": "b" * 64}))


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


class TestExtractedPins(unittest.TestCase):
    """The shared extracted-netlist terminal parser (issue #439, hoisted from
    the stage tools): the top .subckt's terminal list with '+ ' continuation
    lines joined, stopping at the card's end."""

    def _write(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "extracted.klt.spice"
        path.write_text(text)
        return str(path)

    def test_joins_continuation_lines_and_stops_at_card_end(self):
        path = self._write(
            "* extracted by klt\n"
            ".subckt first_stage vdd vss vinp vinn\n"
            "+ tail n1 n2 ibias\n"
            "XM5 tail vdd vss vdd nfet_lvt w=7.2u l=1u nf=4\n"
            ".ends\n"
            "* a later card must not be picked up\n"
            ".subckt other x y\n")
        self.assertEqual(pilot.extracted_pins(path),
                         ["vdd", "vss", "vinp", "vinn",
                          "tail", "n1", "n2", "ibias"])

    def test_subckt_keyword_is_case_insensitive(self):
        path = self._write(".SUBCKT output_stage n2 vout\n+ ibias\n.ends\n")
        self.assertEqual(pilot.extracted_pins(path),
                         ["n2", "vout", "ibias"])

    def test_card_without_continuations(self):
        path = self._write(".subckt top a b\nX1 a b dev\n.ends\n")
        self.assertEqual(pilot.extracted_pins(path), ["a", "b"])

    def test_lines_before_the_card_are_skipped(self):
        path = self._write("* header comment\n+ not a continuation\n.subckt top a\n+ b\n.ends\n")
        self.assertEqual(pilot.extracted_pins(path), ["a", "b"])


class TestPreflightEngine(unittest.TestCase):
    """The shared scratch-dir engine probe (issue #439, hoisted from the stage
    tools), with klt stubbed: no klt, no PDK, no klayout, no network."""

    OK_REPORT = {"provenance": {"klayout_version": "0.30.12"}}

    def test_default_probe_is_the_unit_mos_array_in_cwd(self):
        with mock.patch.object(pilot, "klt",
                               return_value=(self.OK_REPORT, 0)) as klt_spy, \
                mock.patch.object(pilot, "check_engine") as check:
            pilot.preflight_engine("/tmp/some-cwd")
        cmd, cwd = klt_spy.call_args[0][0], klt_spy.call_args[0][1]
        self.assertEqual(cwd, "/tmp/some-cwd")
        self.assertEqual(cmd[:2], ["gen", "mos_array"])
        self.assertEqual(cmd[cmd.index("--pdk") + 1], pilot.PDK)
        self.assertEqual(json.loads(cmd[cmd.index("--params") + 1]),
                         {"w_um": 1.0, "l_um": 1.0, "fingers": 1})
        self.assertEqual(cmd[cmd.index("--cell-name") + 1], "preflight")
        self.assertTrue(cmd[cmd.index("-o") + 1].endswith("preflight.gds"))
        check.assert_called_once_with(self.OK_REPORT)

    def test_explicit_generator_and_params_are_probed_verbatim(self):
        params = {"plate_w_um": 16.8, "plate_h_um": 16.8, "num": 1}
        with mock.patch.object(pilot, "klt",
                               return_value=(self.OK_REPORT, 0)) as klt_spy, \
                mock.patch.object(pilot, "check_engine"):
            pilot.preflight_engine("/tmp/some-cwd", "cap_array", params)
        cmd = klt_spy.call_args[0][0]
        self.assertEqual(cmd[:2], ["gen", "cap_array"])
        self.assertEqual(json.loads(cmd[cmd.index("--params") + 1]), params)

    def test_engine_mismatch_refuses_the_run(self):
        old_pin = pilot.KLAYOUT_PIN
        pilot.KLAYOUT_PIN = "0.30.12"
        try:
            rep = {"provenance": {"klayout_version": "9.9.9"}}
            with mock.patch.object(pilot, "klt", return_value=(rep, 0)):
                with self.assertRaises(SystemExit):
                    pilot.preflight_engine("/tmp/some-cwd")
        finally:
            pilot.KLAYOUT_PIN = old_pin

    def test_failed_probe_raises(self):
        with mock.patch.object(pilot, "klt", return_value=({}, 3)):
            with self.assertRaises(AssertionError):
                pilot.preflight_engine("/tmp/some-cwd")


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
