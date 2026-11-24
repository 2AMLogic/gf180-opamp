#!/usr/bin/env python3
"""Tests for the layout-pilot negative-control gate predicate (issue #338)
and the shared 7-step composed-layout stage pipeline (issue #445).

No simulator, no klt, no PDK, no network: the gate's tripped predicate is a
pure function, exercised on inline dicts, plus one check that it reproduces
the verdicts recorded in the committed results.json. The stage-pipeline
tests run run_stage_pipeline() end-to-end in a scratch stage directory with
a stubbed pilot.klt / pilot.measure_bbox_um, against the committed
schematic netlist.

    python3 layout/pilot/tools/test_pilot.py   # or: python3 -m unittest
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pilot  # noqa: E402
import stage  # noqa: E402

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


# ------------------------------------------------- shared stage pipeline ---

#: the GDS bytes the fake klt leaves behind: fixed, so the sha256 bindings
#: the pipeline asserts (DRC provenance, LVS envelope) are precomputable
DUMMY_GDS = b"dummy routed gds bytes for the shared stage pipeline tests\n"
DUMMY_GDS_SHA = hashlib.sha256(DUMMY_GDS).hexdigest()
FAKE_BBOX = {"x0": 0.0, "y0": 0.0, "x1": 24.0, "y1": 12.0,
             "width": 24.0, "height": 12.0}
#: a diff_pair generation report's widened pitch (col = min + 0.5 margin)
DIFF_PAIR_PITCH = {"pitch_x_clamped": False, "col_pitch_um": 1.5,
                   "min_col_pitch_um": 1.0}
PITCH_MARGIN_UM = 0.5

CARDS = pilot.schematic_cards()


def card_device_counts(instances, extra=None):
    """Fingers sum per bare MOS flavor plus the stage's extra devices -- the
    derivation both stage tools document for their extractor check."""
    want = dict(extra or {})
    for inst in instances:
        model = CARDS[inst][1]
        if model.startswith(("nfet", "pfet")):
            key = model.split("_")[0]
            want[key] = want.get(key, 0) + int(CARDS[inst][2]["nf"])
    return want


def ports_of(net_pins):
    """One generator port per distinct (block, port) the nets terminate on."""
    ports = {}
    for pins in net_pins.values():
        for b, p in pins:
            names = [q["name"] for q in ports.setdefault(b, [])]
            if p not in names:
                ports[b].append({"name": p, "x_um": 1.0, "y_um": 2.0})
    return ports


class FakeKlt:
    """pilot.klt stand-in: canned evidence envelopes per verb, leaving the
    files a real klt would (the per-block and routed GDS, the extracted
    netlist) so the pipeline's sha256 bindings and extracted-pin parse run
    against real files. A test installs a failure path by overriding one of
    the envelope attributes (drc_env, lvs_env, nc_env, routed_pins) before
    the run."""

    def __init__(self, cell, interface_pins, net_pins, ports, device_counts,
                 diff_pair_pitch=None):
        self.cell = cell
        self.interface_pins = list(interface_pins)
        self.ports = ports
        self.diff_pair_pitch = diff_pair_pitch
        self.deck_env = {"content_hash": "f" * 64}
        self.drc_env = self.clean_drc()
        self.lvs_env = self.matching_lvs()
        self.nc_env = self.tripped_control()
        self.routed_pins = list(interface_pins)
        self.extract_env = {"status": "extracted", "device_counts": device_counts}
        nets = sorted(net_pins)
        self.placed_env = {
            "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 24.0, "y1": 12.0},
            "unrouted_nets": nets,
            "nets": [{"net": n, "legs": [{"reason": "routing not requested"}]}
                     for n in nets],
            "blocks": [{"id": b, "offset_um": {"x": 1.0, "y": 0.0},
                        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 4.0, "y1": 8.0}}
                       for b in ports],
            "pins": [{"net": n} for n in self.interface_pins],
        }
        self.calls = []

    def clean_drc(self):
        return {"status": "clean", "violation_count": 0,
                "coverage": {"rules_checked": ["a", "b"], "rules_skipped": []},
                "provenance": {"input": {"content_hash": "sha256:" + DUMMY_GDS_SHA,
                                         "role": "layout"}}}

    def matching_lvs(self):
        return {"status": "match", "error_count": 0, "mismatch_count": 0,
                "counts": {"devices": 7}, "category_error_counts": {},
                "environment": {"layout_sha256": DUMMY_GDS_SHA}}

    def tripped_control(self):
        return {"status": "mismatch", "error_count": 2, "mismatch_count": 1,
                "category_error_counts": {"device.property": 2},
                "environment": {"layout_sha256": DUMMY_GDS_SHA}}

    def routed_env(self):
        return {"bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 26.0, "y1": 14.0},
                "unrouted_nets": [],
                "nets": [{"net": n, "status": "routed",
                          "legs": [{"routed": True, "route_length_um": 4.25}]}
                         for n in self.placed_env["unrouted_nets"]],
                "pins": [{"net": n} for n in self.routed_pins]}

    def __call__(self, args, cwd, out_path=None, ok_rc=(0,)):
        verb = args[0]
        self.calls.append(list(args))
        if verb == "gen":
            return self._gen(args, cwd)
        if verb == "gen-compose":
            return self._compose(args, cwd)
        if verb == "deck":
            return dict(self.deck_env), 0
        if verb == "drc":
            return dict(self.drc_env), 0
        if verb == "extract":
            return self._extract(args, cwd)
        assert verb == "lvs", args
        if "negctl" in args[1]:
            return dict(self.nc_env), 3
        return dict(self.lvs_env), 0

    def _block_id(self, args):
        name = args[args.index("--cell-name") + 1]
        return name[len(self.cell) + 1:] if name.startswith(self.cell + "_") else name

    def _gen(self, args, cwd):
        out = args[args.index("-o") + 1]
        with open(os.path.join(cwd, out), "wb") as f:
            f.write(DUMMY_GDS)
        rep = {"bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 4.0, "y1": 8.0},
               "ports": list(self.ports.get(self._block_id(args), [])),
               "provenance": {"klayout_version": "0.30.12"}}
        if args[1] == "diff_pair" and self.diff_pair_pitch:
            rep["drc_hints"] = {"pitch": dict(self.diff_pair_pitch)}
        return rep, 0

    def _compose(self, args, cwd):
        with open(os.path.join(cwd, args[1])) as f:
            req = json.load(f)
        if "placed" in os.path.basename(args[1]):
            return dict(self.placed_env), 3
        with open(os.path.join(cwd, req["options"]["output"]), "wb") as f:
            f.write(DUMMY_GDS)
        return self.routed_env(), 0

    def _extract(self, args, cwd):
        out = args[args.index("-o") + 1]
        with open(os.path.join(cwd, out), "w") as f:
            f.write(".subckt %s %s\nX1 %s dev\n.ends\n"
                    % (self.cell, " ".join(self.interface_pins),
                       " ".join(self.interface_pins)))
        return dict(self.extract_env), 0


#: a first_stage-shaped topology: a diff_pair input group (pitch widened over
#: the generator minimum) plus a mos_array tail, three interface ports, one
#: internal-free net set. Sizes derive from the committed XM1/XM2/XM5 cards.
FS_NET_PINS = {
    "vinp": (("ing", "Q2_1_G"), ("ing", "Q2_2_G")),
    "vinn": (("ing", "Q1_1_G"), ("ing", "Q1_2_G")),
    "tail": (("ing", "Q1_1_S"), ("tailb", "U0_D")),
}
FS_PINS = (
    {"net": "vinp", "block": "ing", "port": "Q2_1_G"},
    {"net": "vinn", "block": "ing", "port": "Q1_1_G"},
    {"net": "tail", "block": "tailb", "port": "U0_D"},
)

#: an output_stage-shaped topology: mos + cap + res blocks, an internal net
#: (nz) that is routed and labelled but not a subckt port, the poly_res deck
#: option, and non-MOS expected device counts.
OS_NET_PINS = {
    "n2": (("m6b", "U0_G"), ("ccb", "C0_BOT")),
    "nz": (("ccb", "C0_TOP"), ("rzb", "R0_B")),
    "vout": (("m6b", "U0_D"), ("rzb", "R0_A")),
    "vss": (("m7b", "U0_S"), ("m7b", "TAP_S")),
}
OS_PINS = (
    {"net": "n2", "block": "ccb", "port": "C0_BOT"},
    {"net": "vout", "block": "rzb", "port": "R0_A"},
    {"net": "vss", "block": "m7b", "port": "TAP_S"},
)


def stub_routing_plan(placed_req, placed, gen_reports):
    """A minimal stand-in for a stage's build_routed_request: the pipeline
    only ships the request to gen-compose, never inspects the legs."""
    routed = json.loads(json.dumps(placed_req))
    routed["routing"] = {"layer_role": "metal2", "width_um": 0.28}
    for c in routed["connectivity"]:
        c["layer_role"] = "metal2"
        c["legs"] = []
    return routed


def fs_build_blocks(cards):
    blocks = []
    for bid, gen, insts, flavor in (("ing", "diff_pair", ("XM1", "XM2"), "nfet"),
                                    ("tailb", "mos_array", ("XM5",), "nfet")):
        prm = cards[insts[0]][2]
        nf = int(prm["nf"])
        params = {"w_um": round(pilot.num_um(prm["w"]) / nf, 6),
                  "l_um": pilot.num_um(prm["l"]), "flavor": flavor}
        if gen == "diff_pair":
            params.update({"splits": nf,
                           "pitch_x_um": DIFF_PAIR_PITCH["col_pitch_um"]})
        else:
            params.update({"fingers": nf, "rows": 1, "cols": 1})
        blocks.append({"id": bid, "generator": gen, "instances": list(insts),
                       "params": params})
    return blocks


def fs_on_block_gen(group, rep, entry):
    """first_stage's per-block generation check: the diff_pair pitch checks."""
    pitch = (rep.get("drc_hints") or {}).get("pitch")
    if group["generator"] != "diff_pair":
        return ""
    assert pitch and pitch.get("pitch_x_clamped") is False, pitch
    assert abs(pitch["col_pitch_um"] - group["params"]["pitch_x_um"]) < 1e-9, pitch
    assert abs(pitch["col_pitch_um"] - pitch["min_col_pitch_um"] - PITCH_MARGIN_UM) < 1e-9, pitch
    entry["pitch"] = pitch
    return "  col pitch %s (min %s)" % (pitch["col_pitch_um"], pitch["min_col_pitch_um"])


def fs_neg_control(cards, true_ref):
    """first_stage's control: XM1's width doubled in the reference only."""
    true_value = cards["XM1"][2]["w"]
    wrong = "%gu" % (pilot.num_um(true_value) * 2)
    tok = re.compile(r"(?im)^(XM1\s[^\n]*?\bw=)%s(?=\s)" % re.escape(true_value))
    assert len(tok.findall(true_ref)) == 1, tok.pattern
    text = ("* NEGATIVE CONTROL (offline test) -- XM1 w deliberately %s.\n%s"
            % (wrong, tok.sub(r"\g<1>%s" % wrong, true_ref)))
    return text, {"altered_instance": "XM1", "altered_parameter": "w",
                  "schematic_value": true_value, "control_value": wrong,
                  "parameters_compared": {"nfet": ["W", "L"], "pfet": ["W", "L"]}}


def os_build_blocks(cards):
    blocks = []
    for bid, gen, insts, flavor in (("m6b", "mos_array", ("XM6",), "pfet"),
                                    ("m7b", "mos_array", ("XM7",), "nfet"),
                                    ("ccb", "cap_array", ("XCC",), None),
                                    ("rzb", "res_array", ("XRZ",), None)):
        _, model, prm = cards[insts[0]]
        if gen == "mos_array":
            nf = int(prm["nf"])
            params = {"w_um": round(pilot.num_um(prm["w"]) / nf, 6),
                      "l_um": pilot.num_um(prm["l"]), "fingers": nf,
                      "rows": 1, "cols": 1, "flavor": flavor}
        elif gen == "cap_array":
            params = {"plate_w_um": pilot.num_um(prm["c_width"]),
                      "plate_h_um": pilot.num_um(prm["c_length"]), "num": 1}
        else:
            params = {"length_um": pilot.num_um(prm["r_length"]),
                      "width_um": pilot.num_um(prm["r_width"]), "num": 1}
        blocks.append({"id": bid, "generator": gen, "instances": list(insts),
                       "params": params})
    return blocks


def os_neg_control(cards, true_ref):
    """output_stage's control: XCC's plate width wrong in the reference only."""
    true_value = cards["XCC"][2]["c_width"]
    tok = "c_width=%s" % true_value
    assert tok in true_ref, tok
    text = ("* NEGATIVE CONTROL (offline test) -- XCC c_width deliberately 20u.\n%s"
            % true_ref.replace(tok, "c_width=20u"))
    return text, {"altered_parameter": "c_width",
                  "schematic_value": true_value, "control_value": "20u",
                  "parameters_compared": {"cap_mim_2f0_m4m5_noshield": ["C", "A"]}}


class TestStagePipeline(unittest.TestCase):
    """The hoisted 7-step pipeline (issue #445): happy paths for a
    first_stage-shaped and an output_stage-shaped configuration, and the
    failure paths (DRC violation, LVS mismatch, wrong routed pins, negative
    control that does not trip). Everything runs in a scratch stage dir."""

    def _stage_dir(self):
        d = tempfile.mkdtemp(prefix="stage-pipeline-")
        self.addCleanup(shutil.rmtree, d, True)
        return Path(d)

    def _run(self, kwargs, fake, stage_dir):
        with mock.patch.object(pilot, "klt", fake), \
                mock.patch.object(pilot, "measure_bbox_um",
                                  return_value=dict(FAKE_BBOX)), \
                mock.patch.object(pilot, "KLAYOUT_PIN", None):
            return stage.run_stage_pipeline(stage_dir=str(stage_dir), **kwargs)

    def _fs_kwargs(self):
        return dict(
            cell="tstage",
            results_schema="gf180-opamp.layout-first-stage.results/1",
            instances=("XM5", "XM1", "XM2"), subnet_ports="vinp vinn tail",
            ref_note=("offline pipeline test;",),
            placement={"strategy": "row", "order": ["ing", "tailb"],
                       "spacing_um": 1.0},
            net_pins=FS_NET_PINS, pins=FS_PINS, blocks_key="groups",
            build_blocks=fs_build_blocks, build_routed_request=stub_routing_plan,
            build_neg_control=fs_neg_control, on_block_gen=fs_on_block_gen,
            record_placed=True,
            net_plane={"vinp": "metal3", "vinn": "metal4", "tail": "metal2"},
            lvs_compare_parameters={"nfet": ["W", "L"], "pfet": ["W", "L"]})

    def _fs_fake(self):
        return FakeKlt("tstage", "vinp vinn tail".split(), FS_NET_PINS,
                       ports_of(FS_NET_PINS),
                       card_device_counts(("XM5", "XM1", "XM2")),
                       diff_pair_pitch=DIFF_PAIR_PITCH)

    def _os_kwargs(self):
        return dict(
            cell="ostage",
            results_schema="gf180-opamp.layout-output-stage.results/1",
            instances=("XM6", "XM7", "XMB1", "XCC", "XRZ"),
            subnet_ports="n2 vout vss",
            ref_note=("offline pipeline test; nz is internal.",),
            placement={"strategy": "row", "order": ["m6b", "m7b", "ccb", "rzb"],
                       "spacing_um": 4.0},
            net_pins=OS_NET_PINS, pins=OS_PINS, blocks_key="blocks",
            build_blocks=os_build_blocks, build_routed_request=stub_routing_plan,
            build_neg_control=os_neg_control, preflight_first_block=True,
            extract_deck_options=("poly_res=1k",),
            extra_device_counts={"cap_mim_2f0_m4m5_noshield": 1,
                                 "ppolyf_u_1k": 1},
            lvs_compare_parameters={"nfet": ["W", "L"], "pfet": ["W", "L"],
                                    "cap_mim_2f0_m4m5_noshield": ["C", "A"],
                                    "ppolyf_u_1k": ["L", "W"]})

    def _os_fake(self):
        return FakeKlt("ostage", "n2 vout vss".split(), OS_NET_PINS,
                       ports_of(OS_NET_PINS),
                       card_device_counts(("XM6", "XM7", "XMB1", "XCC", "XRZ"),
                                          {"cap_mim_2f0_m4m5_noshield": 1,
                                           "ppolyf_u_1k": 1}))

    # ------------------------------------------------------ happy paths ---
    def test_first_stage_shape_happy_path(self):
        d = self._stage_dir()
        fake = self._fs_fake()
        results = self._run(self._fs_kwargs(), fake, d)
        self.assertEqual(results["schema"],
                         "gf180-opamp.layout-first-stage.results/1")
        self.assertEqual(results["instances"], ["XM5", "XM1", "XM2"])
        self.assertEqual([g["id"] for g in results["groups"]], ["ing", "tailb"])
        self.assertEqual(results["groups"][0]["pitch"]["col_pitch_um"], 1.5)
        self.assertNotIn("pitch", results["groups"][1])
        self.assertIn("placed", results)  # first_stage records the placed pass
        self.assertEqual(results["routed"]["nets"]["tail"]["plane"], "metal2")
        self.assertEqual(results["routed"]["pins_labelled"],
                         ["vinp", "vinn", "tail"])
        self.assertEqual(results["routed"]["bbox_um"], FAKE_BBOX)
        self.assertEqual(results["routed"]["layout_sha256"], DUMMY_GDS_SHA)
        self.assertEqual(results["extract"]["device_counts"],
                         card_device_counts(("XM5", "XM1", "XM2")))
        self.assertEqual(results["extract"]["pins"], ["vinp", "vinn", "tail"])
        self.assertEqual(results["lvs"]["status"], "match")
        nc = results["negative_control"]
        self.assertTrue(nc["tripped"])
        self.assertEqual(nc["altered_instance"], "XM1")
        self.assertEqual((nc["schematic_value"], nc["control_value"]),
                         ("3.6u", "7.2u"))
        # results.json is what was returned, the cut reference counts the
        # instances (#445: only five is spelled out), the control's mutated
        # copy is separate, and the placed request carries the topology
        self.assertEqual(json.loads((d / "results.json").read_text()), results)
        ref = (d / "ref" / "tstage.klt.spice").read_text()
        self.assertIn("the 3 instance cards", ref)
        negref = (d / "ref" / "tstage.negctl-w.klt.spice").read_text()
        self.assertIn("W=7.2u", negref)
        self.assertNotIn("W=7.2u", ref)
        placed_req = json.loads(
            (d / "evidence" / "tstage.placed.request.json").read_text())
        self.assertEqual(placed_req["placement"]["order"], ["ing", "tailb"])
        self.assertEqual(placed_req["pins"], [dict(p) for p in FS_PINS])
        self.assertEqual(sorted(c["net"] for c in placed_req["connectivity"]),
                         ["tail", "vinn", "vinp"])
        # no lvs deck_options for a stage without extract deck options
        lvs_req = json.loads(
            (d / "evidence" / "tstage.lvs.klt.request.json").read_text())
        self.assertNotIn("deck_options", lvs_req["layout"])

    def test_output_stage_shape_happy_path(self):
        d = self._stage_dir()
        fake = self._os_fake()
        results = self._run(self._os_kwargs(), fake, d)
        self.assertEqual(results["schema"],
                         "gf180-opamp.layout-output-stage.results/1")
        self.assertEqual([b["id"] for b in results["blocks"]],
                         ["m6b", "m7b", "ccb", "rzb"])
        self.assertNotIn("placed", results)  # output_stage does not record it
        self.assertNotIn("plane", results["routed"]["nets"]["vout"])
        self.assertEqual(sorted(results["routed"]["nets"]),
                         ["n2", "nz", "vout", "vss"])  # nz routed, not a port
        self.assertEqual(results["routed"]["pins_labelled"],
                         ["n2", "vout", "vss"])
        nc = results["negative_control"]
        self.assertTrue(nc["tripped"])
        self.assertNotIn("altered_instance", nc)  # output_stage schema
        self.assertEqual((nc["schematic_value"], nc["control_value"]),
                         ("16.8u", "20u"))
        self.assertEqual(results["extract"]["device_counts"]["nfet"], 14)
        self.assertEqual(results["extract"]["device_counts"]["pfet"], 24)
        # five instance cards keep the written-out count (#445)
        self.assertIn("the five instance cards",
                      (d / "ref" / "ostage.klt.spice").read_text())
        # the deck option reaches extract and the LVS request's deck_options
        extract_calls = [c for c in fake.calls if c[0] == "extract"]
        self.assertEqual(len(extract_calls), 1)
        self.assertEqual(
            extract_calls[0][extract_calls[0].index("--deck-option") + 1],
            "poly_res=1k")
        lvs_req = json.loads(
            (d / "evidence" / "ostage.lvs.klt.request.json").read_text())
        self.assertEqual(lvs_req["layout"]["deck_options"], {"poly_res": "1k"})
        self.assertEqual(lvs_req["layout"]["declared_pins"],
                         ["n2", "vout", "vss"])

    # ---------------------------------------------------- failure paths ---
    def test_drc_violation_fails_the_pipeline(self):
        d = self._stage_dir()
        fake = self._fs_fake()
        fake.drc_env = {"status": "violations", "violation_count": 1,
                        "coverage": {"rules_checked": [], "rules_skipped": []},
                        "violations": [{"rule": "M2.SP.1", "details": "x"}],
                        "provenance": {"input": {
                            "content_hash": "sha256:" + DUMMY_GDS_SHA,
                            "role": "layout"}}}
        with self.assertRaises(AssertionError):
            self._run(self._fs_kwargs(), fake, d)
        self.assertFalse((d / "results.json").exists())

    def test_lvs_mismatch_fails_the_pipeline(self):
        d = self._stage_dir()
        fake = self._fs_fake()
        fake.lvs_env = {"status": "mismatch", "error_count": 1,
                        "mismatch_count": 1, "counts": {},
                        "mismatches": [{"category": "net"}],
                        "environment": {"layout_sha256": DUMMY_GDS_SHA}}
        with self.assertRaises(AssertionError):
            self._run(self._fs_kwargs(), fake, d)
        self.assertFalse((d / "results.json").exists())

    def test_routed_pins_mismatch_fails_the_pipeline(self):
        # the check output_stage omitted when it cloned first_stage: the
        # routed interface must carry exactly the declared pin set
        d = self._stage_dir()
        fake = self._fs_fake()
        fake.routed_pins = ["vinp", "vinn"]  # tail's label went missing
        with self.assertRaises(AssertionError):
            self._run(self._fs_kwargs(), fake, d)
        self.assertFalse((d / "results.json").exists())

    def test_untripped_negative_control_fails_the_pipeline(self):
        d = self._stage_dir()
        fake = self._fs_fake()
        fake.nc_env = fake.matching_lvs()  # the control reports a match
        with self.assertRaises(SystemExit):
            self._run(self._fs_kwargs(), fake, d)
        self.assertFalse((d / "results.json").exists())


class TestStageReferenceBytes(unittest.TestCase):
    """The hoisted pipeline must preserve each stage's reference SPICE bytes
    (#445): cut_ref's header (now with a len(instances)-based card count)
    and both stages' negative-control mutators re-derive the committed
    ref/ files byte-for-byte from the schematic. Loads the stage tools as
    modules (importing them runs no regeneration)."""

    def _stage_tool(self, name):
        spec = importlib.util.spec_from_file_location(
            name, HERE.parent.parent / name / "tools" / ("%s.py" % name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _stage_dir(self, name):
        return HERE.parent.parent / name

    def test_first_stage_reference_and_control_bytes(self):
        fs = self._stage_tool("first_stage")
        committed = (self._stage_dir("first_stage") / "ref" /
                     "first_stage.klt.spice").read_text()
        self.assertEqual(
            pilot.cut_ref(fs.INSTANCES, fs.CELL, fs.SUBNET_PORTS, fs.REF_NOTE),
            committed)
        ctl_text, fields = fs.build_neg_control(CARDS, committed)
        self.assertEqual(
            (self._stage_dir("first_stage") / "ref" /
             "first_stage.negctl-w.klt.spice").read_text(), ctl_text)
        self.assertEqual(fields["altered_instance"], "XM1")

    def test_output_stage_reference_and_control_bytes(self):
        os_ = self._stage_tool("output_stage")
        committed = (self._stage_dir("output_stage") / "ref" /
                     "output_stage.klt.spice").read_text()
        self.assertEqual(
            pilot.cut_ref(os_.INSTANCES, os_.CELL, os_.SUBNET_PORTS,
                          os_.REF_NOTE), committed)
        ctl_text, fields = os_.build_neg_control(CARDS, committed)
        self.assertEqual(
            (self._stage_dir("output_stage") / "ref" /
             "output_stage.negctl-c_width.klt.spice").read_text(), ctl_text)
        self.assertNotIn("altered_instance", fields)

    def test_cut_ref_counts_instances_that_are_not_five(self):
        text = pilot.cut_ref(("XM1",), "solo", "a b", ("note",))
        self.assertIn("the 1 instance cards", text)
        self.assertIn("the five instance cards",
                      pilot.cut_ref(("XM1", "XM2", "XM3", "XM4", "XM5"),
                                    "penta", "a b", ("note",)))


if __name__ == "__main__":
    unittest.main()
