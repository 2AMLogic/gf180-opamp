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
  4. `klt gen-compose` the three blocks into one labelled cell
       (gds/first_stage.gds): placement + guard rings + net-name pin labels.
       Routing is NOT drawn in the deliverable -- see the routed attempt below
       and README.md for why.
  5. `klt drc` on the composed cell (klt curated gf180mcu deck): must be clean
  6. `klt extract` + `klt lvs` against the cut sub-netlist: recorded as-is.
       The placed-only composition does not match (the inter-block and
       inter-unit nets are open); README.md documents this as the filed
       klayout-tools routing gap, not a relaxed criterion.
  7. a non-normative routed attempt (explicit legs, per-net planes) is also
       regenerated into evidence/: it shows every net is routable and records
       the exact DRC gaps the router's own spacing check rejects.

Outputs are deterministic (no wall-clock content) and written under
layout/first_stage/.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

FIRST = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.abspath(os.path.join(FIRST, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "layout", "pilot", "tools"))
import pilot  # noqa: E402  (reuse: num_um, schematic_cards, check_engine)

PDK = "gf180mcuD"
NETLIST = "design/netlist/opamp_two_stage.spice"
KLAYOUT_PIN = os.environ.get("PILOT_KLAYOUT_PIN")  # exported by regen.sh
DECK = "gf180mcu"
CELL = "first_stage"

#: instance card order as they appear in the schematic
INSTANCES = ("XM5", "XM1", "XM2", "XM3", "XM4")
SUBNET_PORTS = "vdd vss vinp vinn tail n1 n2 ibias"

GROUPS = (
    # id, generator, instances, params -- every size below is derived from the
    # schematic cards in build_groups(); nothing here is a typed-in dimension
    ("in", "diff_pair", ("XM1", "XM2"), "nfet"),
    ("load", "diff_pair", ("XM3", "XM4"), "pfet"),
    ("tail", "mos_array", ("XM5",), "nfet"),
)

#: placed-composition connectivity: every multi-terminal net of the sub-block
#: and the group ports its terminals land on (derived from the cards in
#: build_connectivity). The single-terminal nets (vss at XM5's source, ibias
#: at XM5's gate) are labelled via request pins[] below, not connectivity.
NET_PINS = {
    "vdd": (("load", "M1_1_S"), ("load", "M1_2_S"), ("load", "M2_1_S"), ("load", "M2_2_S")),
    "vinp": (("in", "Q2_1_G"), ("in", "Q2_2_G")),
    "vinn": (("in", "Q1_1_G"), ("in", "Q1_2_G")),
    "tail": (("in", "Q1_1_S"), ("in", "Q2_1_S"), ("in", "Q2_2_S"), ("in", "Q1_2_S"), ("tail", "U0_D")),
    "n1": (("in", "Q1_1_D"), ("in", "Q1_2_D"), ("load", "M1_1_D"), ("load", "M1_2_D"),
           ("load", "M1_1_G"), ("load", "M1_2_G")),
    "n2": (("in", "Q2_1_D"), ("in", "Q2_2_D"), ("load", "M2_1_D"), ("load", "M2_2_D")),
}

#: top-level pin labels: the single-terminal nets and the guard-ring ties
PINS = (
    {"net": "vss", "block": "tail", "port": "U0_S"},
    {"net": "ibias", "block": "tail", "port": "U0_G"},
    {"net": "vss", "block": "in", "port": "TAP_S"},
    {"net": "vss", "block": "tail", "port": "TAP_S"},
    {"net": "vdd", "block": "load", "port": "TAP_S"},
)

PLACEMENT = {"strategy": "row", "order": ["load", "in", "tail"], "spacing_um": 1.0}


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
            params = {
                # diff_pair draws `splits` parallel sub-instances per leg of
                # w_um each: W_total = w_um * splits, matching the pilot's
                # mos_array w_um = W/nf mapping that already LVS-matches.
                "w_um": round(w_tot / nf, 6), "l_um": l_um, "splits": nf,
                "flavor": flavor, "gate_contact": True, "add_guard_ring": True,
            }
            if flavor == "pfet":
                params["mirror"] = True  # XM3/XM4 are the mirror load
        else:
            params = {
                "w_um": round(w_tot / nf, 6), "l_um": l_um, "fingers": nf,
                "rows": 1, "cols": 1, "dummy": 0, "flavor": flavor,
                "gate_contact": True, "add_guard_ring": True,
            }
        groups.append({"id": gid, "generator": gen, "instances": list(insts),
                       "params": params})
    return groups


def main():
    cards = pilot.schematic_cards()
    for inst in INSTANCES:
        if inst not in cards:
            raise SystemExit("instance %s not found in %s" % (inst, NETLIST))

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
        gen_reports[g["id"]] = rep
        results["groups"].append({"id": g["id"], "generator": g["generator"],
                                  "instances": g["instances"], "params": g["params"],
                                  "bbox_um": rep["bbox_um"], "ports": len(rep["ports"])})
        print("gen %-5s %-9s bbox %s" % (g["id"], g["generator"],
              [round(rep["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")]), flush=True)

    # --- 2. reference sub-netlist ------------------------------------------------
    open(os.path.join(FIRST, "ref/first_stage.klt.spice"), "w").write(cut_ref(cards))

    # --- 3. placed composition (the deliverable) ---------------------------------
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
    json.dump(placed_req, open(os.path.join(FIRST, "evidence/first_stage.compose.request.json"), "w"), indent=2)
    placed, rc = klt(["gen-compose", "evidence/first_stage.compose.request.json"],
                     "evidence/first_stage.compose.json", ok_rc=(0, 3))
    assert rc == 3, "placed compose: expected exit 3 (placed, nets open), got %d" % rc
    assert placed["unrouted_nets"] == sorted(NET_PINS), placed["unrouted_nets"]
    for net in placed["nets"]:
        assert net["legs"][0].get("reason") == "routing not requested", net
    print("compose placed  bbox %s  nets open (routing not requested)" %
          [round(placed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")], flush=True)

    # --- 4. DRC (the AC 4 gate) ---------------------------------------------------
    deck, _ = klt(["deck", "rules", "--deck", DECK], "evidence/deck.rules.json")
    drc, rc = klt(["drc", "gds/%s.gds" % CELL, "--deck", DECK, "--pdk", PDK],
                  "evidence/first_stage.drc.klt.json", ok_rc=(0, 3))
    cov = drc.get("coverage", {})
    gds_sha = sha256_file(os.path.join(FIRST, "gds/%s.gds" % CELL))
    results["placed"] = {
        "cell": CELL, "bbox_um": placed["bbox_um"],
        "unrouted_nets": placed["unrouted_nets"],
        "pins_labelled": [p["net"] for p in placed["pins"]],
        "drc": {"status": drc.get("status"), "violations": drc.get("violation_count"),
                "rules_checked": len(cov.get("rules_checked", [])),
                "rules_skipped": len(cov.get("rules_skipped", [])),
                "deck_content_hash": deck.get("content_hash")},
        "layout_sha256": gds_sha,
    }
    assert drc.get("status") == "clean" and drc.get("violation_count") == 0, drc.get("status")
    assert (drc.get("provenance") or {}).get("input") == {
        "content_hash": "sha256:" + gds_sha, "role": "layout"}, drc.get("provenance")
    print("drc clean: %d rules checked, %d skipped (%s)" %
          (results["placed"]["drc"]["rules_checked"],
           results["placed"]["drc"]["rules_skipped"],
           deck.get("content_hash", "")[:22]), flush=True)

    # --- 5. extract + LVS (recorded as-is; see README for the routing gap) -------
    ext, rc = klt(["extract", "gds/%s.gds" % CELL, "--deck", DECK, "--pdk", PDK,
                   "-o", "extracted/%s.klt.spice" % CELL],
                  "evidence/first_stage.extract.klt.json", ok_rc=(0, 3))
    assert ext.get("status") == "extracted", ext.get("status")
    lvs_req = {
        "schema": "klt.lvs.request/1", "engine": "klayout",
        "layout": {"file": "../gds/%s.gds" % CELL, "deck": DECK, "top": CELL},
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
        "expected": "mismatch -- the placed-only composition leaves the "
                    "inter-block and inter-unit nets open (README: filed gap)",
    }
    assert lvs.get("status") == "mismatch", "placed-only LVS unexpectedly matched"
    assert results["lvs"]["layout_sha256"] == gds_sha, "lvs envelope GDS sha mismatch"
    print("lvs %s (placed-only: nets open by design; devices extracted: %s)" %
          (lvs.get("status"), ext.get("device_counts")), flush=True)
    results["extract"] = {"status": ext.get("status"), "device_counts": ext.get("device_counts")}
    assert ext.get("device_counts") == {"nfet": 6, "pfet": 4}, ext.get("device_counts")

    # --- 6. routed attempt (non-normative evidence; see README) ------------------
    routed_req = build_routed_attempt(placed_req, placed, gen_reports)
    json.dump(routed_req, open(os.path.join(FIRST, "evidence/first_stage.routed-attempt.request.json"), "w"), indent=2)
    routed, rc = klt(["gen-compose", "evidence/first_stage.routed-attempt.request.json"],
                     "evidence/first_stage.routed-attempt.json", ok_rc=(0, 3))
    routed_summary = {"unrouted_nets": routed.get("unrouted_nets"),
                      "nets": {n["net"]: n["status"] for n in routed["nets"]}}
    drc_r, rc = klt(["drc", "gds/%s_routed.gds" % CELL, "--deck", DECK, "--pdk", PDK],
                    "evidence/first_stage.routed-attempt.drc.klt.json", ok_rc=(0, 3))
    routed_summary["drc"] = {"status": drc_r.get("status"),
                             "violations": drc_r.get("violation_count"),
                             "rules": sorted({v.get("rule") for v in (drc_r.get("violations") or [])})}
    routed_summary["note"] = ("non-normative: explicit-leg routing attempt over the "
                              "same placement; the router's own spacing check refuses "
                              "the final S-pad approaches (0.19 um stub-vs-ladder-pad, "
                              "deck space 0.28 um) -- README: filed gap")
    results["routed_attempt"] = routed_summary
    os.remove(os.path.join(FIRST, "gds/%s_routed.gds" % CELL))
    print("routed attempt: nets %s  drc %s (%d violations)" %
          (routed_summary["nets"], drc_r.get("status"), drc_r.get("violation_count")), flush=True)

    json.dump(results, open(os.path.join(FIRST, "results.json"), "w"), indent=2)
    print("results.json written", flush=True)


def build_routed_attempt(placed_req, placed, gen_reports):
    """The explicit-leg routing plan for the non-normative routed attempt.

    Per-net routing planes (metal2 TAIL, metal3 N2/VINP, metal4 N1/VINN,
    top_metal VDD), yard lanes south/north/west/east of the row, and every
    leg written stub-aware: the generator's ports face fixed directions
    (S west, D east, G north) and the router extends a one-width stub along
    that direction before/after the caller's waypoint chain, so each
    approach lane is pinned to the stub end (pin +/- 0.28 um), never the
    pin column itself. All coordinates derive from the placed response's
    port positions and bbox; the offsets below are the clearance constants.
    """
    offsets = {b["id"]: b["offset_um"] for b in placed["blocks"]}
    ports = {}
    for gid, rep in gen_reports.items():
        for p in rep["ports"]:
            ports[(gid, p["name"])] = (p["x_um"] + offsets[gid]["x"],
                                       p["y_um"] + offsets[gid]["y"])
    P = lambda b, n: ports[(b, n)]  # noqa: E731

    y0, y1 = placed["bbox_um"]["y0"], placed["bbox_um"]["y1"]
    x0, x1 = placed["bbox_um"]["x0"], placed["bbox_um"]["x1"]
    s_tail, s_n1, s_n2 = y0 - 0.5, y0 - 1.1, y0 - 1.7
    n_n2, n_n1 = y1 + 1.5, y1 + 2.1
    w_n2, w_n1 = x0 - 2.32, x0 - 1.92
    e_tail = x1 + 1.12
    vinn_lane = P("in", "Q1_1_G")[1] + 0.54
    vinp_lane = P("in", "Q2_1_G")[1] + 0.84
    vdd_mid1 = P("load", "M1_1_G")[1] - 0.66
    vdd_mid3 = P("load", "M1_1_G")[1] + 0.74

    def pt(b, p):
        x, y = P(b, p)
        return [round(x, 6), round(y, 6)]

    def leg(net, a, b, wps):
        return {"from_pin": {"block": a[0], "port": a[1]},
                "to_pin": {"block": b[0], "port": b[1]},
                "waypoints_um": [list(w) for w in wps]}

    q11s, q21s, q22s, q12s = (pt("in", p) for p in ("Q1_1_S", "Q2_1_S", "Q2_2_S", "Q1_2_S"))
    q11d, q21d, q22d, q12d = (pt("in", p) for p in ("Q1_1_D", "Q2_1_D", "Q2_2_D", "Q1_2_D"))
    q11g, q21g, q22g, q12g = (pt("in", p) for p in ("Q1_1_G", "Q2_1_G", "Q2_2_G", "Q1_2_G"))
    m11s, m21s, m22s, m12s = (pt("load", p) for p in ("M1_1_S", "M2_1_S", "M2_2_S", "M1_2_S"))
    m11d, m21d, m22d, m12d = (pt("load", p) for p in ("M1_1_D", "M2_1_D", "M2_2_D", "M1_2_D"))
    m11g, m12g = pt("load", "M1_1_G"), pt("load", "M1_2_G")
    u0d = pt("tail", "U0_D")

    # lane constants (see README "Routed attempt"): yard lanes south/north/
    # west/east of the row and mid-block dodge lanes, tuned to the deck's
    # 0.28 um same-layer spacing against the 0.42 um ladder pads.
    routed = json.loads(json.dumps(placed_req))
    routed["options"] = {"cell_name": CELL + "_routed",
                         "output": "gds/%s_routed.gds" % CELL}
    routed["routing"] = {"layer_role": "metal2", "width_um": 0.28}
    planes = {"tail": "metal2", "n1": "metal4", "n2": "metal3",
              "vinn": "metal4", "vinp": "metal3", "vdd": "metal2"}
    legs = {
        "tail": [
            leg("tail", ("in", "Q1_1_S"), ("in", "Q2_1_S"),
                [[q11s[0], s_tail], [q21s[0] + 0.28, s_tail], [q21s[0] + 0.28, q21s[1]]]),
            leg("tail", ("in", "Q2_1_S"), ("in", "Q2_2_S"),
                [[q21s[0] + 0.28, q21s[1]], [q21s[0] + 0.28, s_tail],
                 [q12s[0], s_tail], [q22s[0], s_tail]]),
            leg("tail", ("in", "Q2_2_S"), ("in", "Q1_2_S"),
                [[q22s[0], s_tail], [q12s[0] + 0.28, s_tail], [q12s[0] + 0.28, q12s[1]]]),
            leg("tail", ("in", "Q1_2_S"), ("tail", "U0_D"),
                [[q12s[0] + 0.28, q12s[1]], [q12s[0] + 0.28, s_tail],
                 [e_tail, s_tail], [e_tail, u0d[1]]]),
        ],
        "n1": [
            leg("n1", ("in", "Q1_1_D"), ("load", "M1_1_D"),
                [[q11d[0], s_n1], [m11d[0], s_n1]]),
            leg("n1", ("load", "M1_1_D"), ("load", "M1_1_G"),
                [[m11d[0], s_n1], [m11g[0], s_n1]]),
            leg("n1", ("load", "M1_1_G"), ("load", "M1_2_G"),
                [[m11g[0], s_n1], [w_n1, s_n1], [w_n1, n_n1], [m12g[0], n_n1]]),
            leg("n1", ("load", "M1_2_G"), ("load", "M1_2_D"),
                [[m12g[0], n_n1], [m12d[0], n_n1]]),
            leg("n1", ("load", "M1_2_D"), ("in", "Q1_2_D"),
                [[m12d[0], n_n1], [q12d[0], n_n1]]),
        ],
        "n2": [
            leg("n2", ("in", "Q2_1_D"), ("load", "M2_1_D"),
                [[q21d[0], s_n2], [m21d[0], s_n2]]),
            leg("n2", ("load", "M2_1_D"), ("load", "M2_2_D"),
                [[m21d[0], s_n2], [w_n2, s_n2], [w_n2, n_n2], [m22d[0], n_n2]]),
            leg("n2", ("load", "M2_2_D"), ("in", "Q2_2_D"),
                [[m22d[0], n_n2], [q22d[0], n_n2]]),
        ],
        "vinn": [
            leg("vinn", ("in", "Q1_1_G"), ("in", "Q1_2_G"),
                [[q11g[0], vinn_lane], [q12g[0], vinn_lane]]),
        ],
        "vinp": [
            leg("vinp", ("in", "Q2_1_G"), ("in", "Q2_2_G"),
                [[q21g[0], vinp_lane], [q22g[0], vinp_lane]]),
        ],
        "vdd": [
            leg("vdd", ("load", "M1_1_S"), ("load", "M1_2_S"),
                [[m11s[0], vdd_mid1], [m12s[0], vdd_mid1]]),
            leg("vdd", ("load", "M2_1_S"), ("load", "M1_2_S"),
                [[m21s[0], vdd_mid1]]),
            leg("vdd", ("load", "M2_2_S"), ("load", "M1_2_S"),
                [[m22s[0], vdd_mid3], [m12s[0], vdd_mid3]]),
        ],
    }
    by_net = {c["net"]: c for c in routed["connectivity"]}
    order = ["tail", "n1", "n2", "vinn", "vinp", "vdd"]
    out = []
    for net in order:
        c = by_net[net]
        c["layer_role"] = planes[net]
        c["legs"] = legs[net]
        out.append(c)
    routed["connectivity"] = out
    return routed


if __name__ == "__main__":
    main()
