#!/usr/bin/env python3
"""Regenerate the first-stage sub-block layout and re-run DRC / extract / LVS.

Do not call directly -- use layout/first_stage/regen.sh, which pins the tool
environment and checks the tool/PDK identities before this script is allowed
to run.

What this builds (issue #328, the first slice of #241):

  1. read the XM1..XM5 instance cards from design/netlist/opamp_two_stage.spice
  2. cut the reference sub-netlist mechanically (the five cards, unchanged)
  3. `klt gen` three matched groups with netlist-derived sizes:
        in   XM1+XM2  diff_pair (nfet, common-centroid cross-quad, splits=nf)
        load XM3+XM4  diff_pair (pfet, mirror, splits=nf)
        tail XM5      mos_array (one unit, fingers=nf; the pilot's own mapping)
     Both diff_pairs widen their inter-unit column pitch by PITCH_MARGIN_UM
     over the generator's own legal minimum (`params.pitch_x_um`, klt#6242):
     at the default pitch two different nets' router fixtures share the
     inter-unit gap 0.19 um apart where the deck demands 0.28 (klt#5764).
  4. `klt gen-compose` places the three groups in one row (load, in, tail)
     and ROUTES the seven multi-terminal nets with explicit legs on per-net
     planes (gds/first_stage.gds), with a net-name pin label on each of the
     eight sub-block nets. The placed-only pass (routing omitted) is kept as
     the declare-only connectivity validation.
  5. `klt drc` on the routed cell (klt curated gf180mcu deck): must be clean
  6. `klt extract` + `klt lvs` against the cut sub-netlist, declaring the
     eight subckt ports as the pin set: must match with zero errors.
  7. a negative control re-compares the SAME GDS against a reference copy
     with XM1's w deliberately wrong (schematic value doubled): it must
     report a device.property error, proving the (W, L) compare sees the
     diff_pair's splits-folded width.

The GDS and results.json are deterministic (no wall-clock content; klt zeroes
the GDS timestamp records); only the extract envelope's wall-clock `timing`
block moves between runs. Everything is written under layout/first_stage/.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

FIRST = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.abspath(os.path.join(FIRST, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "layout", "pilot", "tools"))
import pilot  # noqa: E402  (reuse: num_um, schematic_cards, check_engine, control_tripped, layout_sha256_matches)

PDK = "gf180mcuD"
NETLIST = "design/netlist/opamp_two_stage.spice"
KLAYOUT_PIN = os.environ.get("PILOT_KLAYOUT_PIN")  # exported by regen.sh
DECK = "gf180mcu"
CELL = "first_stage"

#: instance card order as they appear in the schematic
INSTANCES = ("XM5", "XM1", "XM2", "XM3", "XM4")
SUBNET_PORTS = "vdd vss vinp vinn tail n1 n2 ibias"
#: the sub-block's interface, derived from the declaration above. Extraction
#: and LVS both receive it as the declared pin set so the layout side sees the
#: same port set as the reference .subckt.
INTERFACE_PINS = SUBNET_PORTS.split()

GROUPS = (
    # id, generator, instances, mos flavor -- every size below is derived
    # from the schematic cards in build_groups(); nothing here is a typed-in
    # device dimension
    ("in", "diff_pair", ("XM1", "XM2"), "nfet"),
    ("load", "diff_pair", ("XM3", "XM4"), "pfet"),
    ("tail", "mos_array", ("XM5",), "nfet"),
)

#: column-pitch margin (um) added to each diff_pair's generator-reported legal
#: minimum. The inter-unit gap holds two different nets' router fixtures on
#: every row -- the west unit's drain (stub + via pad facing east) and the east
#: unit's source (facing west) -- whose pad edges sit (margin - 0.16) um apart,
#: so the deck's 0.28 um same-layer spacing needs at least 0.44; 0.5 is the
#: value klt's own regression for klt#5764 uses, with the composed DRC as the
#: gate. A device dimension is never overridden: W, L and splits stay
#: netlist-derived, and the cross-quad interleave is unchanged by the pitch.
PITCH_MARGIN_UM = 0.5

#: the seven multi-terminal nets of the sub-block and the group ports their
#: terminals land on (derived from the cards in build_connectivity). ibias
#: (XM5's gate) is single-terminal in this cut and is labelled via pins[]
#: below, not connectivity.
NET_PINS = {
    "vdd": (("load", "M1_1_S"), ("load", "M1_2_S"), ("load", "M2_1_S"), ("load", "M2_2_S"),
            ("load", "TAP_S")),
    "vss": (("tail", "U0_S"), ("in", "TAP_S"), ("tail", "TAP_S")),
    "vinp": (("in", "Q2_1_G"), ("in", "Q2_2_G")),
    "vinn": (("in", "Q1_1_G"), ("in", "Q1_2_G")),
    "tail": (("in", "Q1_1_S"), ("in", "Q2_1_S"), ("in", "Q2_2_S"), ("in", "Q1_2_S"),
             ("tail", "U0_D")),
    "n1": (("in", "Q1_1_D"), ("in", "Q1_2_D"), ("load", "M1_1_D"), ("load", "M1_2_D"),
           ("load", "M1_1_G"), ("load", "M1_2_G"), ("load", "M2_1_G"), ("load", "M2_2_G")),
    "n2": (("in", "Q2_1_D"), ("in", "Q2_2_D"), ("load", "M2_1_D"), ("load", "M2_2_D")),
}

#: top-level pin label on every sub-block net. vdd and vss label the well /
#: substrate ring taps; the ring is tied into the net by routing, so the label
#: names the same extracted net. gen-compose deduplicates the routed nets'
#: entries against the router's own net-name route label, so those carry their
#: label on the route plane; ibias is labelled at its port.
PINS = (
    {"net": "vdd", "block": "load", "port": "TAP_S"},
    {"net": "vss", "block": "tail", "port": "TAP_S"},
    {"net": "vinp", "block": "in", "port": "Q2_1_G"},
    {"net": "vinn", "block": "in", "port": "Q1_1_G"},
    {"net": "tail", "block": "in", "port": "Q1_1_S"},
    {"net": "n1", "block": "in", "port": "Q1_1_D"},
    {"net": "n2", "block": "in", "port": "Q2_1_D"},
    {"net": "ibias", "block": "tail", "port": "U0_G"},
)

PLACEMENT = {"strategy": "row", "order": ["load", "in", "tail"], "spacing_um": 1.0}

#: routing plane per net (gf180mcuD 5LM: metal1=34 metal2=36 metal3=42
#: metal4=46 metal5/top_metal=81). Neighbouring lanes that cross each other
#: are kept one plane apart; the supply ties stay low.
NET_PLANE = {
    "vdd": "metal2",
    "vss": "metal3",
    "tail": "metal2",
    "n2": "metal3",
    "n1": "metal4",
    "vinp": "metal3",
    "vinn": "metal4",
}

#: negative control: XM1's width in the reference only (GDS untouched) -- the
#: pilot's MOS control perturbation (schematic W doubled), compared with
#: (W, L). XM1 is a diff_pair leg, so the control proves the composed LVS
#: compares the splits-folded width against the schematic's single nf=2 W.
NEG_CONTROL = {"instance": "XM1", "param": "w", "double": True}


def run(args, out_path=None, ok_rc=(0,)):
    r = subprocess.run(args, capture_output=True, text=True, cwd=FIRST)
    if out_path is not None:
        open(os.path.join(FIRST, out_path), "w").write(r.stdout)
    if r.returncode not in ok_rc and not r.stdout.strip():
        raise SystemExit("klt %s failed (rc %d): %s" % (args[1], r.returncode, r.stderr[-800:]))
    return json.loads(r.stdout), r.returncode


def klt(args, out_path, ok_rc=(0,)):
    return run(["klt"] + args + ["--format", "json"], out_path, ok_rc)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight_engine():
    """Check klt's klayout engine in scratch, before any output is replaced.

    A rejected run must leave the previous gds/ ref/ extracted/ evidence/ and
    results.json untouched, so the identity check cannot wait for the first
    real `klt gen` (which only runs after those directories are wiped).
    """
    with tempfile.TemporaryDirectory() as tmp:
        rep, rc = run(["klt", "gen", "mos_array", "--pdk", PDK,
                       "--params", json.dumps({"w_um": 1.0, "l_um": 1.0, "fingers": 1}),
                       "--cell-name", "preflight",
                       "-o", os.path.join(tmp, "preflight.gds"),
                       "--format", "json"])
        assert rc == 0, rep
        pilot.check_engine(rep)


def min_col_pitch(params):
    """The generator's own legal minimum inter-unit column pitch (um) for a
    diff_pair, read from a scratch probe carrying the real device params (the
    minimum follows the unit extent, hence L): a sub-minimum override is
    clamped up to it and reported as `drc_hints.pitch.min_col_pitch_um`."""
    with tempfile.TemporaryDirectory() as tmp:
        rep, rc = run(["klt", "gen", "diff_pair", "--pdk", PDK,
                       "--params", json.dumps(dict(params, pitch_x_um=0.01)),
                       "--cell-name", "pitch_probe",
                       "-o", os.path.join(tmp, "probe.gds"),
                       "--format", "json"])
        assert rc == 0, rep
        pitch = (rep.get("drc_hints") or {}).get("pitch") or {}
        assert pitch.get("pitch_x_clamped") is True, pitch
        return pitch["min_col_pitch_um"]


def cut_ref(cards):
    """Cut the five instance cards verbatim (continuation lines included)."""
    text = open(os.path.join(REPO, NETLIST)).read()
    lines = text.splitlines()
    out = []
    for inst in INSTANCES:
        for i, line in enumerate(lines):
            if line.split() and line.split()[0] == inst:
                out.append(line)
                j = i + 1
                while j < len(lines) and lines[j].startswith("+"):
                    out.append(lines[j])
                    j += 1
                break
        else:
            raise SystemExit("instance %s not found in %s" % (inst, NETLIST))
    header = ("* schematic-derived sub-netlist for %s: %s cut mechanically from\n"
              "* %s -- the five instance cards, unchanged.\n"
              "* (issue #328; ad/as/pd/ps and nrd/nrs are carried verbatim.)\n"
              % (CELL, "+".join(INSTANCES), NETLIST))
    body = "\n".join([".subckt %s %s" % (CELL, SUBNET_PORTS)] + out + [".ends"]) + "\n"
    return header + body


def build_groups(cards):
    """gen params per group, derived from the schematic cards."""
    groups = []
    for gid, gen, insts, flavor in GROUPS:
        first = cards[insts[0]]
        nf = int(first[2]["nf"])
        w_tot, l_um = pilot.num_um(first[2]["w"]), pilot.num_um(first[2]["l"])
        if gen == "diff_pair":
            # diff_pair draws `splits` parallel sub-instances per leg of
            # w_um each: W_total = w_um * splits, matching the pilot's
            # mos_array w_um = W/nf mapping that already LVS-matches.
            params = {
                "w_um": round(w_tot / nf, 6), "l_um": l_um, "splits": nf,
                "flavor": flavor, "gate_contact": True, "add_guard_ring": True,
            }
            if flavor == "pfet":
                params["mirror"] = True  # XM3/XM4 are the mirror load
            floor = min_col_pitch(params)
            params["pitch_x_um"] = round(floor + PITCH_MARGIN_UM, 6)
        else:
            params = {
                "w_um": round(w_tot / nf, 6), "l_um": l_um, "fingers": nf,
                "rows": 1, "cols": 1, "dummy": 0, "flavor": flavor,
                "gate_contact": True, "add_guard_ring": True,
            }
        groups.append({"id": gid, "generator": gen, "instances": list(insts),
                       "params": params})
    return groups


def build_routed_request(placed_req, placed, gen_reports):
    """Add the explicit-leg routing plan to the placed request.

    Per-net routing planes (see NET_PLANE), yard lanes south / north / west /
    east of the row, and mid-block dodge lanes, tuned to the deck's 0.28 um
    same-layer spacing against the router's 0.42 um via-drop pads. The
    generators' ports face fixed directions (S west, D east, G north, TAP_S
    south) and the router extends a one-width stub along that direction
    before/after the caller's waypoint chain, so each approach lane is
    pinned to the stub end (pin +/- 0.28 um), never the pin column itself.
    All coordinates derive from the placed response's port positions and
    bbox; the constants below are clearance constants only.

    The inter-unit gap of each diff_pair carries the west unit's drain (n1 /
    n2 at the east stub, facing east) and the east unit's source (tail /
    vdd at the west stub, facing west) on both rows; those two different nets
    are what the widened column pitch (PITCH_MARGIN_UM) separates.
    """
    offsets = {b["id"]: b["offset_um"] for b in placed["blocks"]}
    bbox = {b["id"]: b["bbox_um"] for b in placed["blocks"]}
    ports = {}
    for gid, rep in gen_reports.items():
        for p in rep["ports"]:
            ports[(gid, p["name"])] = (p["x_um"] + offsets[gid]["x"],
                                       p["y_um"] + offsets[gid]["y"])
    P = lambda b, n: ports[(b, n)]  # noqa: E731

    y0, y1 = placed["bbox_um"]["y0"], placed["bbox_um"]["y1"]
    x0 = placed["bbox_um"]["x0"]
    vdd_lane = y0 - 0.5        # south yard lane, below the load ring's TAP_S stub pad
    s_tail = y0 - 0.5          # south yard lane, below the input pair's S stub pads
    s_vss = y0 - 0.5           # south yard lane, below the nfet rings' TAP_S stub pads
    s_n2 = y0 - 1.1            # second south lane, one plane above tail / vss
    s_n1 = y0 - 1.7            # south lane, two planes above tail / vss
    n_n2 = y1 + 1.5            # north yard lane, above the load ring
    n_n1 = y1 + 2.1            # second north lane, one plane above n2
    w_n2 = x0 - 2.32           # west yard lane, outside the load ring (n2's loop around it)
    vinn_lane = P("in", "Q1_1_G")[1] + 0.70    # between the input pair's two gate rows:
    # a turn must sit >= 0.63 above the gate port -- the 0.42 via pad reaches
    # 0.21 and the 0.28 wire needs the deck's 0.28 beyond it (same-net notch)
    vinp_lane = P("in", "Q2_1_G")[1] + 0.84    # above vinn's lane, one plane below it
    vdd_mid1 = P("load", "M1_1_G")[1] - 0.66   # between the mirror load's two gate rows
    vdd_mid3 = P("load", "M1_1_G")[1] + 0.74   # above vdd's first mid lane, same plane
    # tail's dodge lane over the east column of the input pair: clear of the
    # Q1_2_D via pad below and the Q1_2_G via pad above
    tail_mid = P("tail", "U0_D")[1] + 1.17
    # the tail route leaves the input pair through the channel between the
    # `in` and `tail` rings and crosses the tail block at U0_D's own row
    ch_in_tail = (bbox["in"]["x1"] + bbox["tail"]["x0"]) / 2
    # n2's climb between the `load` and `in` rings, clear of the load column's
    # D via pads, and its descent east of the input pair's east column, clear
    # of the Q1_2_D via pad (n1's landing on the same column)
    ch_load_in = bbox["in"]["x0"] - 0.33
    x_east_n2 = P("in", "Q2_1_D")[0] + 0.95

    def pt(b, p):
        x, y = P(b, p)
        return [round(x, 6), round(y, 6)]

    def leg(net, a, b, wps):
        return {"from_pin": {"block": a[0], "port": a[1]},
                "to_pin": {"block": b[0], "port": b[1]},
                "waypoints_um": [[round(x, 6), round(y, 6)] for x, y in wps]}

    q11s, q21s, q22s, q12s = (pt("in", p) for p in ("Q1_1_S", "Q2_1_S", "Q2_2_S", "Q1_2_S"))
    q11d, q21d, q22d, q12d = (pt("in", p) for p in ("Q1_1_D", "Q2_1_D", "Q2_2_D", "Q1_2_D"))
    q11g, q21g, q22g, q12g = (pt("in", p) for p in ("Q1_1_G", "Q2_1_G", "Q2_2_G", "Q1_2_G"))
    m11s, m21s, m22s, m12s = (pt("load", p) for p in ("M1_1_S", "M2_1_S", "M2_2_S", "M1_2_S"))
    m11d, m21d, m22d, m12d = (pt("load", p) for p in ("M1_1_D", "M2_1_D", "M2_2_D", "M1_2_D"))
    m11g, m21g, m22g, m12g = (pt("load", p) for p in ("M1_1_G", "M2_1_G", "M2_2_G", "M1_2_G"))
    u0d, u0s = pt("tail", "U0_D"), pt("tail", "U0_S")
    lt_s, it_s, tt_s = pt("load", "TAP_S"), pt("in", "TAP_S"), pt("tail", "TAP_S")

    LEGS = {
        "vdd": [
            leg("vdd", ("load", "M1_1_S"), ("load", "M1_2_S"),
                [[m11s[0], vdd_mid1], [m12s[0], vdd_mid1]]),
            leg("vdd", ("load", "M2_1_S"), ("load", "M1_2_S"),
                [[m21s[0], vdd_mid1]]),
            leg("vdd", ("load", "M2_2_S"), ("load", "M1_2_S"),
                [[m22s[0], vdd_mid3], [m12s[0], vdd_mid3]]),
            leg("vdd", ("load", "M1_1_S"), ("load", "TAP_S"),
                [[m11s[0] - 0.28, vdd_lane], [lt_s[0], vdd_lane]]),
        ],
        "vss": [
            leg("vss", ("tail", "U0_S"), ("tail", "TAP_S"),
                [[u0s[0] - 0.28, s_vss], [tt_s[0], s_vss]]),
            leg("vss", ("tail", "TAP_S"), ("in", "TAP_S"),
                [[tt_s[0], s_vss], [it_s[0], s_vss]]),
        ],
        # the four input-pair sources: a west bus joining the west column's
        # two S ports, the south yard lane to the east column, a bus up the
        # east column's own S stub column (0.44 um clear of the Q2_1_G via
        # pad, where x + 0.28 is only 0.16 from it), then the dodge lane
        # through the inter-block channel to XM5's drain
        "tail": [
            leg("tail", ("in", "Q2_2_S"), ("in", "Q1_1_S"),
                [[q22s[0] - 0.28, q22s[1]], [q11s[0] - 0.28, q11s[1]]]),
            leg("tail", ("in", "Q1_1_S"), ("in", "Q2_1_S"),
                [[q11s[0], s_tail], [q21s[0] + 0.28, s_tail], [q21s[0] + 0.28, q21s[1]]]),
            leg("tail", ("in", "Q2_1_S"), ("in", "Q1_2_S"),
                [[q21s[0], q21s[1]], [q12s[0], q12s[1]]]),
            leg("tail", ("in", "Q1_2_S"), ("tail", "U0_D"),
                [[q12s[0], tail_mid], [ch_in_tail, tail_mid], [ch_in_tail, u0d[1]],
                 [u0d[0] - 0.28, u0d[1]]]),
        ],
        # n1 is XM1's drain, the diode-connected XM3's drain AND all four
        # pfet gates (XM4's gate is n1 too): a gate bus inside the mirror
        # load -- both row-1 gates joined along their own row, both columns
        # joined by the gate stub columns -- then north to the east input leg
        "n1": [
            leg("n1", ("in", "Q1_1_D"), ("load", "M1_1_D"),
                [[q11d[0], s_n1], [m11d[0], s_n1]]),
            leg("n1", ("load", "M1_1_D"), ("load", "M1_1_G"),
                [[m11d[0], m11g[1]]]),
            leg("n1", ("load", "M1_1_G"), ("load", "M2_1_G"),
                [[m21g[0], m21g[1]]]),
            leg("n1", ("load", "M1_1_G"), ("load", "M2_2_G"),
                [[m11g[0], m22g[1]]]),
            leg("n1", ("load", "M2_1_G"), ("load", "M1_2_G"),
                [[m21g[0], m12g[1]]]),
            leg("n1", ("load", "M1_2_G"), ("load", "M1_2_D"),
                [[m12d[0] + 0.28, m12g[1]]]),
            leg("n1", ("load", "M1_2_D"), ("in", "Q1_2_D"),
                [[m12d[0], n_n1], [q12d[0], n_n1]]),
        ],
        # n2 stays north of the input pair: a south exit from Q2_1_D would
        # cross the vss tie (same plane) on the south yard lane
        "n2": [
            leg("n2", ("load", "M2_1_D"), ("load", "M2_2_D"),
                [[m21d[0], s_n2], [w_n2, s_n2], [w_n2, n_n2], [m22d[0], n_n2]]),
            leg("n2", ("load", "M2_2_D"), ("in", "Q2_2_D"),
                [[m22d[0], n_n2], [q22d[0], n_n2]]),
            leg("n2", ("load", "M2_1_D"), ("in", "Q2_1_D"),
                [[ch_load_in, m21d[1]], [ch_load_in, n_n2], [x_east_n2, n_n2],
                 [x_east_n2, q21d[1]]]),
        ],
        "vinn": [
            leg("vinn", ("in", "Q1_1_G"), ("in", "Q1_2_G"),
                [[q11g[0], vinn_lane], [q12g[0], vinn_lane]]),
        ],
        "vinp": [
            leg("vinp", ("in", "Q2_1_G"), ("in", "Q2_2_G"),
                [[q21g[0], vinp_lane], [q22g[0], vinp_lane]]),
        ],
    }

    routed = json.loads(json.dumps(placed_req))
    routed["routing"] = {"layer_role": "metal2", "width_um": 0.28}
    out = []
    for net in ("vdd", "vss", "vinp", "vinn", "tail", "n1", "n2"):
        c = next(c for c in routed["connectivity"] if c["net"] == net)
        c["layer_role"] = NET_PLANE[net]
        c["legs"] = LEGS[net]
        out.append(c)
    routed["connectivity"] = out
    return routed


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

    preflight_engine()

    for d in ("gds", "ref", "evidence", "extracted"):
        shutil.rmtree(os.path.join(FIRST, d), ignore_errors=True)
        os.makedirs(os.path.join(FIRST, d))

    groups = build_groups(cards)
    results = {"schema": "gf180-opamp.layout-first-stage.results/1",
               "netlist": NETLIST, "pdk": PDK, "deck": DECK,
               "instances": list(INSTANCES), "groups": []}

    # --- 1. per-group generation ------------------------------------------------
    gen_reports = {}
    for g in groups:
        gds = "gds/%s.gds" % g["id"]
        rep, rc = klt(["gen", g["generator"], "--pdk", PDK,
                       "--params", json.dumps(g["params"]),
                       "--cell-name", "%s_%s" % (CELL, g["id"]),
                       "-o", gds], "evidence/%s.gen.json" % g["id"])
        assert rc == 0, rep
        pilot.check_engine(rep)
        rep["gds_path"] = gds  # the report's absolute path is not stable
        json.dump(rep, open(os.path.join(FIRST, "evidence/%s.gen.json" % g["id"]), "w"), indent=2)
        gen_reports[g["id"]] = rep
        entry = {"id": g["id"], "generator": g["generator"], "instances": g["instances"],
                 "params": g["params"], "bbox_um": rep["bbox_um"], "ports": len(rep["ports"])}
        pitch = (rep.get("drc_hints") or {}).get("pitch")
        if g["generator"] == "diff_pair":
            # the override must have been honoured, not clamped
            assert pitch and pitch.get("pitch_x_clamped") is False, pitch
            assert abs(pitch["col_pitch_um"] - g["params"]["pitch_x_um"]) < 1e-9, pitch
            assert abs(pitch["col_pitch_um"] - pitch["min_col_pitch_um"] - PITCH_MARGIN_UM) < 1e-9, pitch
            entry["pitch"] = pitch
        results["groups"].append(entry)
        print("gen %-5s %-9s bbox %s%s" % (g["id"], g["generator"],
              [round(rep["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")],
              "  col pitch %s (min %s)" % (pitch["col_pitch_um"], pitch["min_col_pitch_um"])
              if pitch else ""), flush=True)

    # --- 2. reference sub-netlist ------------------------------------------------
    open(os.path.join(FIRST, "ref/first_stage.klt.spice"), "w").write(cut_ref(cards))

    # --- 3. placed composition (connectivity validation; offsets feed routing) ---
    connectivity = [{"net": net, "pins": [{"block": b, "port": p} for b, p in pins]}
                    for net, pins in sorted(NET_PINS.items())]
    placed_req = {
        "schema": "klt.gen_compose.request/1",
        "pdk": {"variant": PDK},
        "blocks": [{"id": g["id"], "generator_report": "%s.gen.json" % g["id"]}
                   for g in groups],
        "placement": PLACEMENT,
        "connectivity": connectivity,
        "pins": [dict(p) for p in PINS],
        "options": {"cell_name": CELL, "output": "gds/%s.gds" % CELL},
    }
    json.dump(placed_req, open(os.path.join(FIRST, "evidence/first_stage.placed.request.json"), "w"), indent=2)
    placed, rc = klt(["gen-compose", "evidence/first_stage.placed.request.json"],
                     "evidence/first_stage.placed.json", ok_rc=(0, 3))
    assert rc == 3, "placed compose: expected exit 3 (placed, nets open), got %d" % rc
    assert placed["unrouted_nets"] == sorted(NET_PINS), placed["unrouted_nets"]
    for net in placed["nets"]:
        assert net["legs"][0].get("reason") == "routing not requested", net
    results["placed"] = {
        "note": "declare-only connectivity validation (routing omitted): every "
                "multi-terminal net is validated and reported open, as expected",
        "bbox_um": placed["bbox_um"],
        "unrouted_nets": placed["unrouted_nets"],
        "pins_labelled": [p["net"] for p in placed["pins"]],
    }
    print("compose placed  bbox %s  (%d nets validated, routing not requested)" %
          ([round(placed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")], len(placed["nets"])),
          flush=True)

    # --- 4. routed composition (the deliverable) ---------------------------------
    routed_req = build_routed_request(placed_req, placed, gen_reports)
    json.dump(routed_req, open(os.path.join(FIRST, "evidence/first_stage.compose.request.json"), "w"), indent=2)
    routed, rc = klt(["gen-compose", "evidence/first_stage.compose.request.json"],
                     "evidence/first_stage.compose.json", ok_rc=(0, 3))
    assert rc == 0, "routed compose rc %d: unrouted %s, error %s" % (
        rc, routed.get("unrouted_nets"), routed.get("error"))
    assert routed["unrouted_nets"] == [], routed["unrouted_nets"]
    for net in routed["nets"]:
        assert net["status"] == "routed", net
        for l in net["legs"]:
            assert l.get("routed") is True, l
    pins_labelled = [p["net"] for p in routed["pins"]]
    results["routed"] = {
        "cell": CELL,
        # gen-compose's bbox is the placement bounds only; the routed
        # footprint is measured from the GDS after the compose (below)
        "placement_bbox_um": routed["bbox_um"],
        "unrouted_nets": routed["unrouted_nets"],
        "pins_labelled": pins_labelled,
        "nets": {n["net"]: {"status": n["status"], "plane": NET_PLANE[n["net"]],
                            "route_length_um": round(sum(l["route_length_um"] for l in n["legs"]), 2),
                            "legs": len(n["legs"])} for n in routed["nets"]},
    }
    assert sorted(set(pins_labelled)) == sorted(INTERFACE_PINS), pins_labelled
    assert len(pins_labelled) == len(INTERFACE_PINS), pins_labelled
    rb = measure_bbox_um(os.path.join(FIRST, "gds/%s.gds" % CELL))
    results["routed"]["bbox_um"] = rb
    print("compose routed  placement bbox %s, routed GDS bbox %.2f x %.2f um  all %d nets routed, pins %s" %
          ([round(routed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")],
           rb["width"], rb["height"], len(routed["nets"]), pins_labelled), flush=True)

    # --- 5. DRC (the AC gate) ----------------------------------------------------
    deck, _ = klt(["deck", "rules", "--deck", DECK], "evidence/deck.rules.json")
    drc, rc = klt(["drc", "gds/%s.gds" % CELL, "--deck", DECK, "--pdk", PDK],
                  "evidence/first_stage.drc.klt.json", ok_rc=(0, 3))
    cov = drc.get("coverage", {})
    gds_sha = sha256_file(os.path.join(FIRST, "gds/%s.gds" % CELL))
    results["routed"]["drc"] = {
        "status": drc.get("status"), "violations": drc.get("violation_count"),
        "rules_checked": len(cov.get("rules_checked", [])),
        "rules_skipped": len(cov.get("rules_skipped", [])),
        "layers_in_stream_without_rules": cov.get("layers_in_stream_without_rules"),
        "deck_content_hash": deck.get("content_hash"),
    }
    results["routed"]["layout_sha256"] = gds_sha
    if not (drc.get("status") == "clean" and drc.get("violation_count") == 0):
        for v in (drc.get("violations") or [])[:12]:
            print("DRC %s: %s" % (v.get("rule"), json.dumps(v)[:400]), flush=True)
    assert drc.get("status") == "clean" and drc.get("violation_count") == 0, drc.get("status")
    assert (drc.get("provenance") or {}).get("input") == {
        "content_hash": "sha256:" + gds_sha, "role": "layout"}, drc.get("provenance")
    print("drc clean: %d rules checked, %d skipped (%s)" %
          (results["routed"]["drc"]["rules_checked"],
           results["routed"]["drc"]["rules_skipped"],
           deck.get("content_hash", "")[:22]), flush=True)

    # --- 6. extract + LVS against the cut sub-netlist ---------------------------
    ext, rc = klt(["extract", "gds/%s.gds" % CELL, "--deck", DECK, "--pdk", PDK,
                   "--pins", ",".join(INTERFACE_PINS),
                   "-o", "extracted/%s.klt.spice" % CELL],
                  "evidence/first_stage.extract.klt.json", ok_rc=(0, 3))
    assert ext.get("status") == "extracted", ext.get("status")
    # expected device counts derive from the cards: fingers sum per flavor
    want = {}
    for inst in INSTANCES:
        model = cards[inst][1]
        want[model.split("_")[0]] = want.get(model.split("_")[0], 0) + int(cards[inst][2]["nf"])
    assert ext.get("device_counts") == want, (ext.get("device_counts"), want)
    got_pins = extracted_pins(os.path.join(FIRST, "extracted/%s.klt.spice" % CELL))
    assert sorted(got_pins) == sorted(INTERFACE_PINS), (got_pins, INTERFACE_PINS)
    results["extract"] = {"status": ext.get("status"), "device_counts": ext.get("device_counts"),
                          "pins": got_pins}

    lvs_req = {
        "schema": "klt.lvs.request/1", "engine": "klayout",
        "layout": {"file": "../gds/%s.gds" % CELL, "deck": DECK, "top": CELL,
                   "declared_pins": list(INTERFACE_PINS)},
        "reference": {"netlist": "../ref/first_stage.klt.spice", "top": CELL,
                      "form": "subckt-call"},
        "options": {"combine_devices": True,
                    "compare_parameters": {"nfet": ["W", "L"], "pfet": ["W", "L"]}},
    }
    json.dump(lvs_req, open(os.path.join(FIRST, "evidence/first_stage.lvs.klt.request.json"), "w"), indent=2)
    lvs, rc = klt(["lvs", "evidence/first_stage.lvs.klt.request.json"],
                  "evidence/first_stage.lvs.klt.json", ok_rc=(0, 3))
    results["lvs"] = {
        "status": lvs.get("status"), "error_count": lvs.get("error_count"),
        "mismatch_count": lvs.get("mismatch_count"),
        "counts": lvs.get("counts"),
        "category_error_counts": lvs.get("category_error_counts"),
        "layout_sha256": (lvs.get("environment") or {}).get("layout_sha256"),
    }
    if not (lvs.get("status") == "match" and lvs.get("error_count") == 0):
        for m in (lvs.get("mismatches") or [])[:12]:
            print("LVS %s: %s" % (m.get("category"), json.dumps(m)[:400]), flush=True)
    assert lvs.get("status") == "match" and lvs.get("error_count") == 0, lvs.get("status")
    assert results["lvs"]["layout_sha256"] == gds_sha, "lvs envelope GDS sha mismatch"
    print("lvs %s (errors %d, counts %s)" % (lvs.get("status"), lvs.get("error_count"),
                                             lvs.get("counts")), flush=True)

    # --- 7. negative control: same GDS, XM1 w wrong in the reference -------------
    nc = NEG_CONTROL
    true_ref = open(os.path.join(FIRST, "ref/first_stage.klt.spice")).read()
    true_value = cards[nc["instance"]][2][nc["param"]]
    assert nc["double"], nc
    wrong = "%gu" % (pilot.num_um(true_value) * 2)  # '3.6u' -> '7.2u'
    # the cards' keys are upper case in the netlist (W=), lower in schematic_cards();
    # scope to the instance's own card line (XM2 shares XM1's W)
    tok = re.compile(r"(?im)^(%s\s[^\n]*?\b%s=)%s(?=\s)"
                     % (re.escape(nc["instance"]), re.escape(nc["param"]), re.escape(true_value)))
    assert len(tok.findall(true_ref)) == 1, tok.pattern
    stem = "%s.negctl-%s" % (CELL, nc["param"])
    open(os.path.join(FIRST, "ref/%s.klt.spice" % stem), "w").write(
        "* NEGATIVE CONTROL for %s -- %s %s deliberately %s (schematic %s).\n"
        "* klt lvs must report a mismatch with a device.property error against\n"
        "* the unchanged gds/%s.gds; a match would mean the (W, L) compare is\n"
        "* insensitive to the diff_pair's splits-folded width.\n%s"
        % (CELL, nc["instance"], nc["param"].upper(), wrong, true_value, CELL,
           tok.sub(r"\g<1>%s" % wrong, true_ref)))
    nc_req = dict(lvs_req, reference=dict(lvs_req["reference"],
                                          netlist="../ref/%s.klt.spice" % stem))
    json.dump(nc_req, open(os.path.join(FIRST, "evidence/%s.lvs.klt.request.json" % stem), "w"), indent=2)
    ncenv, rc = klt(["lvs", "evidence/%s.lvs.klt.request.json" % stem],
                    "evidence/%s.lvs.klt.json" % stem, ok_rc=(0, 3))
    cec = ncenv.get("category_error_counts") or {}
    ctl = {"altered_instance": nc["instance"], "altered_parameter": nc["param"],
           "schematic_value": true_value, "control_value": wrong,
           "parameters_compared": {"nfet": ["W", "L"], "pfet": ["W", "L"]},
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
                         % (nc["param"], wrong, ctl["status"],
                            ctl["device_property_errors"],
                            ctl["layout_sha256_matches_positive"]))
    print("negctl %s=%s: %s, device.property=%d, layout unchanged=%s"
          % (nc["param"], wrong, ctl["status"], ctl["device_property_errors"],
             ctl["layout_sha256_matches_positive"]), flush=True)

    json.dump(results, open(os.path.join(FIRST, "results.json"), "w"), indent=2)
    print("results.json written", flush=True)


if __name__ == "__main__":
    main()
