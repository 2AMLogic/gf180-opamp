#!/usr/bin/env python3
"""Regenerate the output-stage + compensation sub-block layout and re-run
DRC / extract / LVS.

Do not call directly -- use layout/output_stage/regen.sh, which pins the tool
environment and checks the tool/PDK identities before this script is allowed
to run.

What this builds (issue #424, the second slice of #241):

  1. read the XM6/XM7/XMB1/XCC/XRZ instance cards from
     design/netlist/opamp_two_stage.spice
  2. cut the reference sub-netlist mechanically (the five cards, unchanged,
     continuation lines included)
  3. `klt gen` five blocks with netlist-derived sizes (the pilot's mappings):
        m6   XM6   mos_array (pfet,  w_um = W/nf, fingers = nf)
        mb1  XMB1  mos_array (nfet)
        m7   XM7   mos_array (nfet)
        cc   XCC   cap_array (square plate c_width x c_length)
        rz   XRZ   res_array (flavor 1k from the model name)
  4. `klt gen-compose` places them in one row (m6, mb1, m7, cc, rz; XMB1 next
     to XM7 as a matched 1:6 pair, same orientation) and ROUTES the six nets
     with explicit legs on per-net planes (gds/output_stage.gds), with
     net-name pin labels on n2/vout/ibias/vdd/vss. Unlike the first stage
     (#328), this composition routes fully: unrouted_nets comes back empty
     and the routed GDS is DRC-clean and LVS-matched.
  5. `klt drc` on the routed cell (klt curated gf180mcu deck): must be clean
  6. `klt extract` (--deck-option poly_res=1k, as in the pilot) + `klt lvs`
     against the cut sub-netlist: must match with zero errors. The resistor
     substrate terminal is checked here: extraction must tie it to vss.
  7. a negative control re-compares the SAME GDS against a reference copy
     with XCC's c_width deliberately wrong (16.8u -> 20u, the pilot's MIM
     control value): it must report a device.property error, proving the
     parameter compare (C, A) is live.

Outputs are deterministic (no wall-clock content in the GDS; klt 0.8.0 zeros
the GDS timestamp records) and written under layout/output_stage/.
"""
import json
import os
import shutil
import sys
import tempfile

STAGE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.abspath(os.path.join(STAGE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "layout", "pilot", "tools"))
import pilot  # noqa: E402  (reuse: num_um, schematic_cards, check_engine, control_tripped, layout_sha256_matches, klt, run, sha256_file, cut_ref)

PDK = "gf180mcuD"
NETLIST = "design/netlist/opamp_two_stage.spice"
KLAYOUT_PIN = os.environ.get("PILOT_KLAYOUT_PIN")  # exported by regen.sh
DECK = "gf180mcu"
CELL = "output_stage"

#: instance card order as they appear in the schematic
INSTANCES = ("XM6", "XM7", "XMB1", "XCC", "XRZ")
SUBNET_PORTS = "n2 vout ibias vdd vss"
#: the sub-block's interface, derived from the declaration above. Extraction
#: and LVS both receive it as the declared pin set, so internal nets (nz)
#: stay internal even though they carry a route label.
INTERFACE_PINS = SUBNET_PORTS.split()

BLOCKS = (
    # id, generator, instances, mos flavor -- every size below is derived
    # from the schematic cards in build_blocks(); nothing is a typed-in
    # dimension
    ("m6", "mos_array", ("XM6",), "pfet"),
    ("mb1", "mos_array", ("XMB1",), "nfet"),
    ("m7", "mos_array", ("XM7",), "nfet"),
    ("cc", "cap_array", ("XCC",), None),
    ("rz", "res_array", ("XRZ",), None),
)

#: row order keeps XMB1 and XM7 adjacent with the same orientation (the
#: matched 1:6 bias-mirror pair of this slice; XM5, the third leg, is placed
#: in layout/first_stage/ and the full XMB1/XM5/XM7 group is #241's).
#: spacing 4 um leaves routing channels wide enough for the deck's 0.28 um
#: metal spacing and the MIM's 1.2 um foreign clearance.
PLACEMENT = {"strategy": "row", "order": ["m6", "mb1", "m7", "cc", "rz"],
             "spacing_um": 4.0}

#: the six nets of the sub-block, from the schematic cards (the issue's
#: connection table). vdd ties XM6's source to its own well (pfet) tap; vss
#: ties both nfets' sources to their substrate taps. XRZ's substrate has no
#: generator port and is verified tied to vss by the composed LVS instead.
NET_PINS = {
    "n2": (("m6", "U0_G"), ("cc", "C0_BOT")),
    "vout": (("m6", "U0_D"), ("m7", "U0_D"), ("rz", "R0_A")),
    "nz": (("cc", "C0_TOP"), ("rz", "R0_B")),
    "ibias": (("mb1", "U0_D"), ("mb1", "U0_G"), ("m7", "U0_G")),
    "vdd": (("m6", "U0_S"), ("m6", "TAP_W")),
    "vss": (("mb1", "U0_S"), ("mb1", "TAP_S"), ("m7", "U0_S"), ("m7", "TAP_S")),
}

#: top-level pin labels on the five interface nets. vdd/vss label the well /
#: substrate ring taps (the ring is tied into the net by routing, so the
#: label names the same extracted net); n2/vout/ibias label routing
#: terminals -- gen-compose deduplicates those against the router's own
#: net-name route label, so in the committed GDS those three nets carry
#: their label on the route plane instead of at the port.
PINS = (
    {"net": "n2", "block": "cc", "port": "C0_BOT"},
    {"net": "vout", "block": "rz", "port": "R0_A"},
    {"net": "ibias", "block": "mb1", "port": "U0_G"},
    {"net": "vdd", "block": "m6", "port": "TAP_N"},
    {"net": "vss", "block": "m7", "port": "TAP_W"},
)

#: routing plane per net (gf180mcuD 5LM: metal1=34 metal2=36 metal3=42
#: metal4=46 metal5/top_metal=81). n2 must land on the MIM bottom plate
#: (metal4) and nz on the top plate (top_metal), so their planes sit at
#: those levels; the MOS-local nets stay low.
NET_PLANE = {
    "vdd": "metal2",
    "vss": "metal2",
    "ibias": "metal3",
    "vout": "metal3",
    "n2": "metal4",
    "nz": "top_metal",
}

#: negative control: XCC's plate width in the reference only (GDS untouched)
#: -- the pilot's MIM control perturbation, compared with (C, A).
NEG_CONTROL = {"instance": "XCC", "param": "c_width", "wrong": "20u"}

#: provenance note cut_ref writes into the reference sub-netlist header
REF_NOTE = ("issue #424; ad/as/pd/ps and nrd/nrs are carried verbatim; nz is",
            "internal, not a sub-block port.")


def build_blocks(cards):
    """gen params per block, derived from the schematic cards (pilot mapping)."""
    blocks = []
    for bid, gen, insts, flavor in BLOCKS:
        nodes, model, prm = cards[insts[0]]
        if gen == "mos_array":
            nf = int(prm["nf"])
            w_tot, l_um = pilot.num_um(prm["w"]), pilot.num_um(prm["l"])
            params = {"w_um": round(w_tot / nf, 6), "l_um": l_um, "fingers": nf,
                      "rows": 1, "cols": 1, "dummy": 0, "flavor": flavor,
                      "gate_contact": True, "add_guard_ring": True}
        elif gen == "cap_array":
            w, l = pilot.num_um(prm["c_width"]), pilot.num_um(prm["c_length"])
            assert w == l, "cap_array generates square plates"
            params = {"plate_w_um": w, "plate_h_um": l, "num": 1}
        else:
            params = {"length_um": pilot.num_um(prm["r_length"]),
                      "width_um": pilot.num_um(prm["r_width"]),
                      "num": 1, "dummy": 0, "flavor": model.split("_")[-1]}
        blocks.append({"id": bid, "generator": gen, "instances": list(insts),
                       "params": params})
    return blocks


def build_routed_request(placed_req, placed, gen_reports):
    """Add the explicit-leg routing plan to the placed request.

    Per-net routing planes (see NET_PLANE), two yard lanes south of the row
    (vss on metal2, vout one level above on metal3), a north lane above the
    gates (ibias, metal3), a metal4 lane above the MOS row into the MIM
    bottom plate (n2), and a top_metal lane over the MIM top (nz). Lane
    offsets are clearance constants tuned to the deck's 0.28 um same-layer
    spacing and the MIM's 1.2 um foreign clearance; every coordinate itself
    derives from the placed response's port positions and bboxes.
    """
    offsets = {b["id"]: b["offset_um"] for b in placed["blocks"]}
    ports = {}
    for bid, rep in gen_reports.items():
        for p in rep["ports"]:
            ports[(bid, p["name"])] = (p["x_um"] + offsets[bid]["x"],
                                       p["y_um"] + offsets[bid]["y"])
    P = lambda b, n: ports[(b, n)]  # noqa: E731

    bx = {b["id"]: b["bbox_um"] for b in placed["blocks"]}
    y0 = placed["bbox_um"]["y0"]
    mos_top = max(bx[b]["y1"] for b in ("m6", "mb1", "m7"))
    vss_lane = y0 - 0.67        # south yard lane, below the TAP_S stub pads
    vout_lane = y0 - 1.37       # second south lane, one plane above vss
    ibias_lane = P("mb1", "U0_G")[1] + 0.75   # north lane above the gates
    n2_lane = mos_top + 1.81    # metal4 lane above the MOS row
    nz_lane = placed["bbox_um"]["y1"] + 1.6  # top_metal lane above the MIM
    ch = [(bx[a]["x1"] + bx[b]["x0"]) / 2
          for a, b in (("m6", "mb1"), ("mb1", "m7"), ("m7", "cc"), ("cc", "rz"))]
    ch_m6_mb1, ch_mb1_m7, ch_m7_cc, ch_cc_rz = ch
    x_east = placed["bbox_um"]["x1"] + 2.1    # east of rz, outside the stream

    def leg(net, a, b, wps):
        return {"from_pin": {"block": a[0], "port": a[1]},
                "to_pin": {"block": b[0], "port": b[1]},
                "waypoints_um": [[round(x, 4), round(y, 4)] for x, y in wps]}

    m6s, m6g, m6d, m6tw = (P("m6", p) for p in ("U0_S", "U0_G", "U0_D", "TAP_W"))
    mb1s, mb1g, mb1d, mb1ts = (P("mb1", p) for p in ("U0_S", "U0_G", "U0_D", "TAP_S"))
    m7s, m7g, m7d, m7ts = (P("m7", p) for p in ("U0_S", "U0_G", "U0_D", "TAP_S"))
    cb, ct = P("cc", "C0_BOT"), P("cc", "C0_TOP")
    ra, rb = P("rz", "R0_A"), P("rz", "R0_B")

    LEGS = {
        "vdd": [
            # XM6 source up to the pfet well ring's west tap (S and B are both
            # vdd on the card); the wire runs through the tap-port column so
            # it overlaps the router's via pad there.
            leg("vdd", ("m6", "U0_S"), ("m6", "TAP_W"),
                [[m6tw[0], m6s[1]], [m6tw[0], m6tw[1]]]),
        ],
        "vss": [
            leg("vss", ("mb1", "U0_S"), ("mb1", "TAP_S"),
                [[ch_m6_mb1 - 0.1, mb1s[1]], [ch_m6_mb1 - 0.1, vss_lane],
                 [mb1ts[0], vss_lane]]),
            leg("vss", ("mb1", "TAP_S"), ("m7", "TAP_S"),
                [[mb1ts[0], vss_lane], [m7ts[0], vss_lane]]),
            leg("vss", ("m7", "TAP_S"), ("m7", "U0_S"),
                [[m7ts[0], vss_lane], [ch_mb1_m7 - 0.2, vss_lane],
                 [ch_mb1_m7 - 0.2, m7s[1]]]),
        ],
        "ibias": [
            leg("ibias", ("mb1", "U0_D"), ("mb1", "U0_G"),
                [[ch_mb1_m7 - 0.6, mb1d[1]], [ch_mb1_m7 - 0.6, ibias_lane],
                 [mb1g[0], ibias_lane]]),
            leg("ibias", ("mb1", "U0_G"), ("m7", "U0_G"),
                [[mb1g[0], ibias_lane], [m7g[0], ibias_lane]]),
        ],
        "vout": [
            leg("vout", ("m6", "U0_D"), ("m7", "U0_D"),
                [[ch_m6_mb1 + 0.1, m6d[1]], [ch_m6_mb1 + 0.1, vout_lane],
                 [ch_m7_cc - 1.0, vout_lane], [ch_m7_cc - 1.0, m7d[1]]]),
            leg("vout", ("m7", "U0_D"), ("rz", "R0_A"),
                [[ch_m7_cc - 1.0, m7d[1]], [ch_m7_cc - 1.0, vout_lane],
                 [ch_cc_rz - 0.9, vout_lane], [ch_cc_rz - 0.9, ra[1]]]),
        ],
        "n2": [
            leg("n2", ("m6", "U0_G"), ("cc", "C0_BOT"),
                [[m6g[0], n2_lane], [cb[0] - 1.95, n2_lane],
                 [cb[0] - 1.95, cb[1]]]),
        ],
        "nz": [
            leg("nz", ("cc", "C0_TOP"), ("rz", "R0_B"),
                [[ct[0], nz_lane], [x_east, nz_lane], [x_east, rb[1]]]),
        ],
    }

    routed = json.loads(json.dumps(placed_req))
    routed["routing"] = {"layer_role": "metal2", "width_um": 0.28}
    out = []
    for c in routed["connectivity"]:
        c["layer_role"] = NET_PLANE[c["net"]]
        c["legs"] = LEGS[c["net"]]
        out.append(c)
    routed["connectivity"] = out
    return routed


def preflight_engine(block):
    """Check klt's klayout engine in scratch, before any output is replaced.

    A rejected run must leave the previous gds/ ref/ extracted/ evidence/ and
    results.json untouched, so the identity check cannot wait for the first
    real `klt gen` (which only runs after those directories are wiped).
    """
    with tempfile.TemporaryDirectory() as tmp:
        rep, rc = pilot.klt(["gen", block["generator"], "--pdk", PDK,
                             "--params", json.dumps(block["params"]),
                             "--cell-name", "preflight",
                             "-o", os.path.join(tmp, "preflight.gds")], STAGE)
        assert rc == 0, rep
        pilot.check_engine(rep)


def measure_bbox_um(gds_path):
    """Routed footprint: the committed GDS's own top-cell bounding box (um)."""
    import klayout.db as kdb
    ly = kdb.Layout()
    ly.read(gds_path)
    b = ly.top_cell().dbbox()
    return {"x0": b.left, "y0": b.bottom, "x1": b.right, "y1": b.top,
            "width": round(b.width(), 2), "height": round(b.height(), 2)}


def extracted_pins(path):
    """Terminal list of the extracted top .subckt (continuations joined)."""
    toks = []
    in_card = False
    for line in open(path).read().splitlines():
        if not in_card:
            t = line.split()
            if t and t[0].lower() == ".subckt":
                toks, in_card = t[2:], True
        elif line.startswith("+"):
            toks += line[1:].split()
        else:
            break
    return toks


def main():
    cards = pilot.schematic_cards()
    for inst in INSTANCES:
        if inst not in cards:
            raise SystemExit("instance %s not found in %s" % (inst, NETLIST))

    preflight_engine(build_blocks(cards)[0])

    for d in ("gds", "ref", "evidence", "extracted"):
        shutil.rmtree(os.path.join(STAGE, d), ignore_errors=True)
        os.makedirs(os.path.join(STAGE, d))

    blocks = build_blocks(cards)
    results = {"schema": "gf180-opamp.layout-output-stage.results/1",
               "netlist": NETLIST, "pdk": PDK, "deck": DECK,
               "instances": list(INSTANCES), "blocks": []}

    # --- 1. per-block generation ------------------------------------------------
    gen_reports = {}
    for g in blocks:
        gds = "gds/%s.gds" % g["id"]
        rep, rc = pilot.klt(["gen", g["generator"], "--pdk", PDK,
                             "--params", json.dumps(g["params"]),
                             "--cell-name", "%s_%s" % (CELL, g["id"]),
                             "-o", gds], STAGE, "evidence/%s.gen.json" % g["id"])
        assert rc == 0, rep
        pilot.check_engine(rep)
        rep["gds_path"] = gds  # the report's absolute path is not stable
        json.dump(rep, open(os.path.join(STAGE, "evidence/%s.gen.json" % g["id"]), "w"), indent=2)
        gen_reports[g["id"]] = rep
        results["blocks"].append({"id": g["id"], "generator": g["generator"],
                                  "instances": g["instances"], "params": g["params"],
                                  "bbox_um": rep["bbox_um"], "ports": len(rep["ports"])})
        print("gen %-4s %-10s bbox %s" % (g["id"], g["generator"],
              [round(rep["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")]), flush=True)

    # --- 2. reference sub-netlist ------------------------------------------------
    open(os.path.join(STAGE, "ref/%s.klt.spice" % CELL), "w").write(
        pilot.cut_ref(INSTANCES, CELL, SUBNET_PORTS, REF_NOTE))

    # --- 3. placed composition (connectivity validation; offsets feed routing) ---
    connectivity = [{"net": net, "pins": [{"block": b, "port": p} for b, p in pins]}
                    for net, pins in sorted(NET_PINS.items())]
    placed_req = {
        "schema": "klt.gen_compose.request/1",
        "pdk": {"variant": PDK},
        "blocks": [{"id": g["id"], "generator_report": "%s.gen.json" % g["id"]}
                   for g in blocks],
        "placement": PLACEMENT,
        "connectivity": connectivity,
        "pins": [dict(p) for p in PINS],
        "options": {"cell_name": CELL, "output": "gds/%s.gds" % CELL},
    }
    json.dump(placed_req, open(os.path.join(STAGE, "evidence/%s.placed.request.json" % CELL), "w"), indent=2)
    placed, rc = pilot.klt(["gen-compose", "evidence/%s.placed.request.json" % CELL],
                           STAGE, "evidence/%s.placed.json" % CELL, ok_rc=(0, 3))
    assert rc == 3, "placed compose: expected exit 3 (placed, nets open), got %d" % rc
    assert placed["unrouted_nets"] == sorted(NET_PINS), placed["unrouted_nets"]
    for net in placed["nets"]:
        assert net["legs"][0].get("reason") == "routing not requested", net
    print("compose placed  bbox %s  (routing not requested yet)" %
          [round(placed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")], flush=True)

    # --- 4. routed composition (the deliverable) ---------------------------------
    routed_req = build_routed_request(placed_req, placed, gen_reports)
    json.dump(routed_req, open(os.path.join(STAGE, "evidence/%s.compose.request.json" % CELL), "w"), indent=2)
    routed, rc = pilot.klt(["gen-compose", "evidence/%s.compose.request.json" % CELL],
                           STAGE, "evidence/%s.compose.json" % CELL, ok_rc=(0, 3))
    assert rc == 0, "routed compose rc %d: %s" % (rc, routed.get("error"))
    assert routed["unrouted_nets"] == [], routed["unrouted_nets"]
    for net in routed["nets"]:
        assert net["status"] == "routed", net
        for l in net["legs"]:
            assert l.get("routed") is True, l
    results["routed"] = {
        "cell": CELL,
        # gen-compose's bbox is the placement bounds only; the routed
        # footprint is measured from the GDS after the compose (below)
        "placement_bbox_um": routed["bbox_um"],
        "unrouted_nets": routed["unrouted_nets"],
        "pins_labelled": [p["net"] for p in routed["pins"]],
        "nets": {n["net"]: {"status": n["status"],
                            "route_length_um": round(sum(l["route_length_um"] for l in n["legs"]), 2),
                            "legs": len(n["legs"])} for n in routed["nets"]},
    }
    rb = measure_bbox_um(os.path.join(STAGE, "gds/%s.gds" % CELL))
    results["routed"]["bbox_um"] = rb
    print("compose routed  placement bbox %s, routed GDS bbox %.2f x %.2f um  all %d nets routed" %
          ([round(routed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")],
           rb["width"], rb["height"], len(routed["nets"])), flush=True)

    # --- 5. DRC (the AC gate) ----------------------------------------------------
    deck, _ = pilot.klt(["deck", "rules", "--deck", DECK], STAGE, "evidence/deck.rules.json")
    drc, rc = pilot.klt(["drc", "gds/%s.gds" % CELL, "--deck", DECK, "--pdk", PDK],
                        STAGE, "evidence/%s.drc.klt.json" % CELL, ok_rc=(0, 3))
    cov = drc.get("coverage", {})
    gds_sha = pilot.sha256_file(os.path.join(STAGE, "gds/%s.gds" % CELL))
    results["routed"]["drc"] = {
        "status": drc.get("status"), "violations": drc.get("violation_count"),
        "rules_checked": len(cov.get("rules_checked", [])),
        "rules_skipped": len(cov.get("rules_skipped", [])),
        "layers_in_stream_without_rules": cov.get("layers_in_stream_without_rules"),
        "deck_content_hash": deck.get("content_hash"),
    }
    results["routed"]["layout_sha256"] = gds_sha
    assert drc.get("status") == "clean" and drc.get("violation_count") == 0, drc.get("status")
    assert (drc.get("provenance") or {}).get("input") == {
        "content_hash": "sha256:" + gds_sha, "role": "layout"}, drc.get("provenance")
    print("drc clean: %d rules checked, %d skipped (%s)" %
          (results["routed"]["drc"]["rules_checked"],
           results["routed"]["drc"]["rules_skipped"],
           deck.get("content_hash", "")[:22]), flush=True)

    # --- 6. extract + LVS against the cut sub-netlist ---------------------------
    ext, rc = pilot.klt(["extract", "gds/%s.gds" % CELL, "--deck", DECK, "--pdk", PDK,
                         "--deck-option", "poly_res=1k",
                         "--pins", ",".join(INTERFACE_PINS),
                         "-o", "extracted/%s.klt.spice" % CELL],
                        STAGE, "evidence/%s.extract.klt.json" % CELL, ok_rc=(0, 3))
    assert ext.get("status") == "extracted", ext.get("status")
    # expected device counts derive from the cards: fingers sum per flavor,
    # one MIM, one resistor
    want = {"cap_mim_2f0_m4m5_noshield": 1, "ppolyf_u_1k": 1}
    for inst, (_, model, prm) in cards.items():
        if inst not in INSTANCES:
            continue
        if model.startswith(("nfet", "pfet")):
            key = model.split("_")[0]  # the extractor reports the bare flavor
            want[key] = want.get(key, 0) + int(prm["nf"])
    assert ext.get("device_counts") == want, (ext.get("device_counts"), want)
    got_pins = extracted_pins(os.path.join(STAGE, "extracted/%s.klt.spice" % CELL))
    assert sorted(got_pins) == sorted(INTERFACE_PINS), (got_pins, INTERFACE_PINS)
    results["extract"] = {"status": ext.get("status"), "device_counts": ext.get("device_counts"),
                          "pins": got_pins}

    lvs_req = {
        "schema": "klt.lvs.request/1", "engine": "klayout",
        "layout": {"file": "../gds/%s.gds" % CELL, "deck": DECK, "top": CELL,
                   "deck_options": {"poly_res": "1k"},
                   "declared_pins": list(INTERFACE_PINS)},
        "reference": {"netlist": "../ref/%s.klt.spice" % CELL, "top": CELL,
                      "form": "subckt-call"},
        "options": {"combine_devices": True,
                    "compare_parameters": {"nfet": ["W", "L"], "pfet": ["W", "L"],
                                           "cap_mim_2f0_m4m5_noshield": ["C", "A"],
                                           "ppolyf_u_1k": ["L", "W"]}},
    }
    json.dump(lvs_req, open(os.path.join(STAGE, "evidence/%s.lvs.klt.request.json" % CELL), "w"), indent=2)
    lvs, rc = pilot.klt(["lvs", "evidence/%s.lvs.klt.request.json" % CELL],
                        STAGE, "evidence/%s.lvs.klt.json" % CELL, ok_rc=(0, 3))
    results["lvs"] = {
        "status": lvs.get("status"), "error_count": lvs.get("error_count"),
        "mismatch_count": lvs.get("mismatch_count"),
        "counts": lvs.get("counts"),
        "category_error_counts": lvs.get("category_error_counts"),
        "layout_sha256": (lvs.get("environment") or {}).get("layout_sha256"),
    }
    assert lvs.get("status") == "match" and lvs.get("error_count") == 0, lvs.get("status")
    assert results["lvs"]["layout_sha256"] == gds_sha, "lvs envelope GDS sha mismatch"
    print("lvs %s (errors %d, counts %s)" % (lvs.get("status"), lvs.get("error_count"),
                                             lvs.get("counts")), flush=True)

    # --- 7. negative control: same GDS, XCC c_width wrong in the reference -------
    nc = NEG_CONTROL
    true_ref = open(os.path.join(STAGE, "ref/%s.klt.spice" % CELL)).read()
    true_value = cards[nc["instance"]][2][nc["param"]]
    tok = "%s=%s" % (nc["param"], true_value)
    assert tok in true_ref, tok
    stem = "%s.negctl-%s" % (CELL, nc["param"])
    open(os.path.join(STAGE, "ref/%s.klt.spice" % stem), "w").write(
        "* NEGATIVE CONTROL for %s -- XCC %s deliberately %s (schematic %s).\n"
        "* klt lvs must report a mismatch with a device.property error against\n"
        "* the unchanged gds/%s.gds; a match would mean the (C, A) compare is\n"
        "* insensitive to the plate size this slice claims it checks.\n%s"
        % (CELL, nc["param"], nc["wrong"], true_value, CELL,
           true_ref.replace(tok, "%s=%s" % (nc["param"], nc["wrong"]))))
    nc_req = dict(lvs_req, reference=dict(lvs_req["reference"],
                                          netlist="../ref/%s.klt.spice" % stem))
    json.dump(nc_req, open(os.path.join(STAGE, "evidence/%s.lvs.klt.request.json" % stem), "w"), indent=2)
    ncenv, rc = pilot.klt(["lvs", "evidence/%s.lvs.klt.request.json" % stem],
                          STAGE, "evidence/%s.lvs.klt.json" % stem, ok_rc=(0, 3))
    cec = ncenv.get("category_error_counts") or {}
    ctl = {"altered_parameter": nc["param"],
           "schematic_value": true_value, "control_value": nc["wrong"],
           "parameters_compared": {"cap_mim_2f0_m4m5_noshield": ["C", "A"]},
           "status": ncenv.get("status"), "error_count": ncenv.get("error_count"),
           "category_error_counts": cec,
           "device_property_errors": cec.get("device.property", 0),
           "klt_exit_code": rc,
           "layout_sha256": (ncenv.get("environment") or {}).get("layout_sha256"),
           "layout_sha256_matches_positive":
               pilot.layout_sha256_matches(ncenv.get("environment") or {},
                                           lvs.get("environment") or {}),
           "reference_netlist": "ref/%s.klt.spice" % stem,
           "request": "evidence/%s.lvs.klt.request.json" % stem,
           "evidence": "evidence/%s.lvs.klt.json" % stem}
    ctl["tripped"] = pilot.control_tripped(ctl)
    results["negative_control"] = ctl
    if not ctl["tripped"]:
        raise SystemExit("negative control did not trip: %s=%s gave status=%s, "
                         "device.property errors=%d, layout unchanged=%s"
                         % (nc["param"], nc["wrong"], ctl["status"],
                            ctl["device_property_errors"],
                            ctl["layout_sha256_matches_positive"]))
    print("negctl c_width=%s: %s, device.property=%d, layout unchanged=%s"
          % (nc["wrong"], ctl["status"], ctl["device_property_errors"],
             ctl["layout_sha256_matches_positive"]), flush=True)

    json.dump(results, open(os.path.join(STAGE, "results.json"), "w"), indent=2)
    print("results.json written", flush=True)


if __name__ == "__main__":
    main()
