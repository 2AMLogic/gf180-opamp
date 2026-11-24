#!/usr/bin/env python3
"""The shared seven-step composed-layout stage pipeline (issue #445).

The stage tools (first_stage #328, output_stage #424) each carried a
~210-line copy of the same regeneration orchestration. It lives here once
now, so the full-core slice (#241) gets a tested runner instead of a third
copy:

  0. validate the instance cards, preflight klt's klayout engine, wipe and
     recreate gds/ ref/ evidence/ extracted/
  1. per-block `klt gen` (+ check_engine, gds_path rewrite, evidence dump)
  2. the cut reference sub-netlist (pilot.cut_ref)
  3. placed `klt gen-compose` (rc 3, every net open: the declare-only
     connectivity validation)
  4. routed `klt gen-compose` (rc 0, every leg routed, pins labelled exactly
     the declared interface, routed-GDS bbox measured from the file)
  5. `klt deck rules` + `klt drc` + the provenance hash binding
  6. `klt extract` (device counts, extracted pins) + `klt lvs` (match, zero
     errors, envelope GDS sha equal)
  7. negative-control `klt lvs` (must trip) + results.json

A stage tool supplies its declarative topology constants, three hooks
(build_blocks, build_routed_request, build_neg_control) and the optional
on_block_gen per-block generation check, plus the small per-stage knobs
(blocks_key, preflight_first_block, record_placed, net_plane,
extract_deck_options, extra_device_counts, lvs_compare_parameters). The
pipeline preserves each stage's results.json schema and reference SPICE
bytes byte-for-byte: a future regeneration must reproduce the committed
evidence unchanged.

Offline tests: layout/pilot/tools/test_pilot.py (stubbed klt, stubbed
measure_bbox_um). Do not call directly -- a stage tool's regen.sh pins the
tool environment and checks the tool/PDK identities before the tool is
allowed to run.
"""
import json
import os
import shutil

import pilot

DECK = "gf180mcu"


def run_stage_pipeline(stage_dir, cell, results_schema, instances, subnet_ports,
                       ref_note, placement, net_pins, pins, blocks_key,
                       build_blocks, build_routed_request, build_neg_control,
                       preflight_first_block=False, on_block_gen=None,
                       record_placed=False, net_plane=None,
                       extract_deck_options=(), extra_device_counts=None,
                       lvs_compare_parameters=None):
    """Regenerate one composed-layout stage under `stage_dir` and return the
    results dict written to its results.json (see the module docstring for
    the seven steps and the hook contracts)."""
    interface_pins = subnet_ports.split()

    cards = pilot.schematic_cards()
    for inst in instances:
        if inst not in cards:
            raise SystemExit("instance %s not found in %s" % (inst, pilot.NETLIST))

    blocks = build_blocks(cards)
    if preflight_first_block:
        pilot.preflight_engine(stage_dir, blocks[0]["generator"],
                               blocks[0]["params"])
    else:
        pilot.preflight_engine(stage_dir)

    for d in ("gds", "ref", "evidence", "extracted"):
        shutil.rmtree(os.path.join(stage_dir, d), ignore_errors=True)
        os.makedirs(os.path.join(stage_dir, d))

    results = {"schema": results_schema, "netlist": pilot.NETLIST,
               "pdk": pilot.PDK, "deck": DECK, "instances": list(instances),
               blocks_key: []}

    # --- 1. per-block generation ---------------------------------------------
    gen_reports = {}
    for g in blocks:
        gds = "gds/%s.gds" % g["id"]
        rep, rc = pilot.klt(["gen", g["generator"], "--pdk", pilot.PDK,
                             "--params", json.dumps(g["params"]),
                             "--cell-name", "%s_%s" % (cell, g["id"]),
                             "-o", gds], stage_dir, "evidence/%s.gen.json" % g["id"])
        assert rc == 0, rep
        pilot.check_engine(rep)
        rep["gds_path"] = gds  # the report's absolute path is not stable
        with open(os.path.join(stage_dir, "evidence/%s.gen.json" % g["id"]), "w") as f:
            json.dump(rep, f, indent=2)
        gen_reports[g["id"]] = rep
        entry = {"id": g["id"], "generator": g["generator"], "instances": g["instances"],
                 "params": g["params"], "bbox_um": rep["bbox_um"], "ports": len(rep["ports"])}
        suffix = on_block_gen(g, rep, entry) if on_block_gen else ""
        results[blocks_key].append(entry)
        print("gen %-5s %-10s bbox %s%s" % (g["id"], g["generator"],
              [round(rep["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")],
              suffix or ""), flush=True)

    # --- 2. reference sub-netlist ---------------------------------------------
    true_ref = pilot.cut_ref(instances, cell, subnet_ports, ref_note)
    with open(os.path.join(stage_dir, "ref/%s.klt.spice" % cell), "w") as f:
        f.write(true_ref)

    # --- 3. placed composition (connectivity validation; offsets feed routing) -
    connectivity = [{"net": net, "pins": [{"block": b, "port": p} for b, p in npins]}
                    for net, npins in sorted(net_pins.items())]
    placed_req = {
        "schema": "klt.gen_compose.request/1",
        "pdk": {"variant": pilot.PDK},
        "blocks": [{"id": g["id"], "generator_report": "%s.gen.json" % g["id"]}
                   for g in blocks],
        "placement": placement,
        "connectivity": connectivity,
        "pins": [dict(p) for p in pins],
        "options": {"cell_name": cell, "output": "gds/%s.gds" % cell},
    }
    with open(os.path.join(stage_dir, "evidence/%s.placed.request.json" % cell), "w") as f:
        json.dump(placed_req, f, indent=2)
    placed, rc = pilot.klt(["gen-compose", "evidence/%s.placed.request.json" % cell],
                           stage_dir, "evidence/%s.placed.json" % cell, ok_rc=(0, 3))
    assert rc == 3, "placed compose: expected exit 3 (placed, nets open), got %d" % rc
    assert placed["unrouted_nets"] == sorted(net_pins), placed["unrouted_nets"]
    for net in placed["nets"]:
        assert net["legs"][0].get("reason") == "routing not requested", net
    if record_placed:
        results["placed"] = {
            "note": "declare-only connectivity validation (routing omitted): every "
                    "multi-terminal net is validated and reported open, as expected",
            "bbox_um": placed["bbox_um"],
            "unrouted_nets": placed["unrouted_nets"],
            "pins_labelled": [p["net"] for p in placed["pins"]],
        }
    print("compose placed  bbox %s  (%d nets validated, routing not requested)" %
          ([round(placed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")],
           len(placed["nets"])), flush=True)

    # --- 4. routed composition (the deliverable) -------------------------------
    routed_req = build_routed_request(placed_req, placed, gen_reports)
    with open(os.path.join(stage_dir, "evidence/%s.compose.request.json" % cell), "w") as f:
        json.dump(routed_req, f, indent=2)
    routed, rc = pilot.klt(["gen-compose", "evidence/%s.compose.request.json" % cell],
                           stage_dir, "evidence/%s.compose.json" % cell, ok_rc=(0, 3))
    assert rc == 0, "routed compose rc %d: unrouted %s, error %s" % (
        rc, routed.get("unrouted_nets"), routed.get("error"))
    assert routed["unrouted_nets"] == [], routed["unrouted_nets"]
    for net in routed["nets"]:
        assert net["status"] == "routed", net
        for l in net["legs"]:
            assert l.get("routed") is True, l
    pins_labelled = [p["net"] for p in routed["pins"]]
    nets_record = {}
    for n in routed["nets"]:
        rec = {"status": n["status"]}
        if net_plane is not None:
            rec["plane"] = net_plane[n["net"]]
        rec["route_length_um"] = round(sum(l["route_length_um"] for l in n["legs"]), 2)
        rec["legs"] = len(n["legs"])
        nets_record[n["net"]] = rec
    results["routed"] = {
        "cell": cell,
        # gen-compose's bbox is the placement bounds only; the routed
        # footprint is measured from the GDS after the compose (below)
        "placement_bbox_um": routed["bbox_um"],
        "unrouted_nets": routed["unrouted_nets"],
        "pins_labelled": pins_labelled,
        "nets": nets_record,
    }
    # the routed interface must carry exactly the declared pin set (the
    # check output_stage omitted when it cloned first_stage; #445 unifies it)
    assert sorted(set(pins_labelled)) == sorted(interface_pins), pins_labelled
    assert len(pins_labelled) == len(interface_pins), pins_labelled
    rb = pilot.measure_bbox_um(os.path.join(stage_dir, "gds/%s.gds" % cell))
    results["routed"]["bbox_um"] = rb
    print("compose routed  placement bbox %s, routed GDS bbox %.2f x %.2f um  all %d nets routed, pins %s" %
          ([round(routed["bbox_um"][k], 2) for k in ("x0", "y0", "x1", "y1")],
           rb["width"], rb["height"], len(routed["nets"]), pins_labelled), flush=True)

    # --- 5. DRC (the AC gate) --------------------------------------------------
    deck, _ = pilot.klt(["deck", "rules", "--deck", DECK], stage_dir, "evidence/deck.rules.json")
    drc, rc = pilot.klt(["drc", "gds/%s.gds" % cell, "--deck", DECK, "--pdk", pilot.PDK],
                        stage_dir, "evidence/%s.drc.klt.json" % cell, ok_rc=(0, 3))
    cov = drc.get("coverage", {})
    gds_sha = pilot.sha256_file(os.path.join(stage_dir, "gds/%s.gds" % cell))
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

    # --- 6. extract + LVS against the cut sub-netlist --------------------------
    extract_args = ["extract", "gds/%s.gds" % cell, "--deck", DECK, "--pdk", pilot.PDK]
    for opt in extract_deck_options:
        extract_args += ["--deck-option", opt]
    extract_args += ["--pins", ",".join(interface_pins),
                     "-o", "extracted/%s.klt.spice" % cell]
    ext, rc = pilot.klt(extract_args, stage_dir, "evidence/%s.extract.klt.json" % cell,
                        ok_rc=(0, 3))
    assert ext.get("status") == "extracted", ext.get("status")
    # expected device counts derive from the cards: fingers sum per bare MOS
    # flavor, plus the stage's declared non-MOS devices
    want = dict(extra_device_counts or {})
    for inst in instances:
        model = cards[inst][1]
        if model.startswith(("nfet", "pfet")):
            key = model.split("_")[0]  # the extractor reports the bare flavor
            want[key] = want.get(key, 0) + int(cards[inst][2]["nf"])
    assert ext.get("device_counts") == want, (ext.get("device_counts"), want)
    got_pins = pilot.extracted_pins(os.path.join(stage_dir, "extracted/%s.klt.spice" % cell))
    assert sorted(got_pins) == sorted(interface_pins), (got_pins, interface_pins)
    results["extract"] = {"status": ext.get("status"), "device_counts": ext.get("device_counts"),
                          "pins": got_pins}

    layout = {"file": "../gds/%s.gds" % cell, "deck": DECK, "top": cell}
    if extract_deck_options:
        layout["deck_options"] = dict(o.split("=", 1) for o in extract_deck_options)
    layout["declared_pins"] = list(interface_pins)
    lvs_req = {
        "schema": "klt.lvs.request/1", "engine": "klayout",
        "layout": layout,
        "reference": {"netlist": "../ref/%s.klt.spice" % cell, "top": cell,
                      "form": "subckt-call"},
        "options": {"combine_devices": True,
                    "compare_parameters": lvs_compare_parameters},
    }
    with open(os.path.join(stage_dir, "evidence/%s.lvs.klt.request.json" % cell), "w") as f:
        json.dump(lvs_req, f, indent=2)
    lvs, rc = pilot.klt(["lvs", "evidence/%s.lvs.klt.request.json" % cell],
                        stage_dir, "evidence/%s.lvs.klt.json" % cell, ok_rc=(0, 3))
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

    # --- 7. negative control: same GDS, one reference parameter wrong ----------
    nc_text, ctl_fields = build_neg_control(cards, true_ref)
    stem = "%s.negctl-%s" % (cell, ctl_fields["altered_parameter"])
    with open(os.path.join(stage_dir, "ref/%s.klt.spice" % stem), "w") as f:
        f.write(nc_text)
    nc_req = dict(lvs_req, reference=dict(lvs_req["reference"],
                                          netlist="../ref/%s.klt.spice" % stem))
    with open(os.path.join(stage_dir, "evidence/%s.lvs.klt.request.json" % stem), "w") as f:
        json.dump(nc_req, f, indent=2)
    ncenv, rc = pilot.klt(["lvs", "evidence/%s.lvs.klt.request.json" % stem],
                          stage_dir, "evidence/%s.lvs.klt.json" % stem, ok_rc=(0, 3))
    cec = ncenv.get("category_error_counts") or {}
    ctl = dict(ctl_fields)
    ctl.update({
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
        "evidence": "evidence/%s.lvs.klt.json" % stem})
    ctl["tripped"] = pilot.control_tripped(ctl)
    results["negative_control"] = ctl
    if not ctl["tripped"]:
        raise SystemExit("negative control did not trip: %s=%s gave status=%s, "
                         "device.property errors=%d, layout unchanged=%s"
                         % (ctl["altered_parameter"], ctl["control_value"], ctl["status"],
                            ctl["device_property_errors"],
                            ctl["layout_sha256_matches_positive"]))
    print("negctl %s=%s: %s, device.property=%d, layout unchanged=%s"
          % (ctl["altered_parameter"], ctl["control_value"], ctl["status"],
             ctl["device_property_errors"], ctl["layout_sha256_matches_positive"]), flush=True)

    with open(os.path.join(stage_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print("results.json written", flush=True)
    return results
