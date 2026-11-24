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

Outputs are deterministic (no wall-clock content in the GDS; klt zeros
the GDS timestamp records) and written under layout/output_stage/.
"""
import json
import os
import sys

STAGE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.abspath(os.path.join(STAGE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "layout", "pilot", "tools"))
import pilot  # noqa: E402  (reuse: num_um)
import stage  # noqa: E402  (the shared 7-step stage pipeline, issue #445)

CELL = "output_stage"

#: instance card order as they appear in the schematic
INSTANCES = ("XM6", "XM7", "XMB1", "XCC", "XRZ")
#: the sub-block's interface ports. The shared pipeline splits this into the
#: declared pin set extraction and LVS receive (so internal nets (nz) stay
#: internal even though they carry a route label) and into the routed
#: pins_labelled assertion.
SUBNET_PORTS = "n2 vout ibias vdd vss"

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


#: the device-parameter compare per model: the MOS (W, L), the MIM's
#: (C, A), the resistor's (L, W)
COMPARE_PARAMETERS = {"nfet": ["W", "L"], "pfet": ["W", "L"],
                      "cap_mim_2f0_m4m5_noshield": ["C", "A"],
                      "ppolyf_u_1k": ["L", "W"]}

#: the non-MOS devices the extractor must report alongside the MOS fingers
#: (fingers sum per bare flavor, derived from the cards by the pipeline)
EXTRA_DEVICE_COUNTS = {"cap_mim_2f0_m4m5_noshield": 1, "ppolyf_u_1k": 1}

#: extraction deck option (the pilot's poly_res=1k); the pipeline also
#: forwards it to the LVS request's layout deck_options
DECK_OPTIONS = ("poly_res=1k",)


def build_neg_control(cards, true_ref):
    """build_neg_control hook for the shared pipeline: XCC's plate width
    wrong in the reference copy only (see NEG_CONTROL); the GDS is never
    touched."""
    nc = NEG_CONTROL
    true_value = cards[nc["instance"]][2][nc["param"]]
    tok = "%s=%s" % (nc["param"], true_value)
    assert tok in true_ref, tok
    text = ("* NEGATIVE CONTROL for %s -- %s %s deliberately %s (schematic %s).\n"
            "* klt lvs must report a mismatch with a device.property error against\n"
            "* the unchanged gds/%s.gds; a match would mean the (C, A) compare is\n"
            "* insensitive to the plate size this slice claims it checks.\n%s"
            % (CELL, nc["instance"], nc["param"], nc["wrong"], true_value, CELL,
               true_ref.replace(tok, "%s=%s" % (nc["param"], nc["wrong"]))))
    return text, {"altered_parameter": nc["param"],
                  "schematic_value": true_value, "control_value": nc["wrong"],
                  "parameters_compared": {"cap_mim_2f0_m4m5_noshield": ["C", "A"]}}


def main():
    stage.run_stage_pipeline(
        stage_dir=STAGE, cell=CELL,
        results_schema="gf180-opamp.layout-output-stage.results/1",
        instances=INSTANCES, subnet_ports=SUBNET_PORTS, ref_note=REF_NOTE,
        placement=PLACEMENT, net_pins=NET_PINS, pins=PINS, blocks_key="blocks",
        build_blocks=build_blocks, build_routed_request=build_routed_request,
        build_neg_control=build_neg_control, preflight_first_block=True,
        extract_deck_options=DECK_OPTIONS,
        extra_device_counts=EXTRA_DEVICE_COUNTS,
        lvs_compare_parameters=COMPARE_PARAMETERS)


if __name__ == "__main__":
    main()
