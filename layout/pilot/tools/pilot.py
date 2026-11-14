#!/usr/bin/env python3
"""Regenerate the device-pilot layout cells and re-run DRC / LVS on each.

Do not call directly -- use layout/pilot/regen.sh, which pins the tool
environment (klayout python for cell labelling) and checks the
tool/PDK identities before this script is allowed to run.

Per cell:
  1. read the schematic instance card(s) from design/netlist/opamp_two_stage.spice
  2. `klt gen` the device (mos_array / cap_array / res_array) with the
     schematic's dimensions, then add net-name labels at the generator's ports
  3. write the schematic-derived reference netlist (klt form)
  4. DRC: klt's curated gf180mcu deck
  5. LVS: klt extract + klt lvs (with device-parameter compare)
  6. negative control (one cell per device kind): the same unchanged GDS
     re-compared against a reference card with a single dimension
     deliberately wrong.  It must report a `device.property` error; if a
     control reports `match` the regeneration exits non-zero, because then a
     passing LVS would no longer prove the compared dimensions agree.
Outputs are deterministic (no timestamps) and written under layout/pilot/.
"""
import json
import os
import re
import shutil
import subprocess

import klayout.db as db

PILOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.abspath(os.path.join(PILOT, "..", ".."))
PDK = "gf180mcuD"
KLAYOUT_PIN = os.environ.get("PILOT_KLAYOUT_PIN")  # exported by regen.sh

MOS_LABELS = {"U0_S": "S", "U0_D": "D", "U0_G": "G", "TAP_*": "B"}
MIM_LABELS = {"C0_BOT": "BOT", "C0_TOP": "TOP"}
RES_LABELS = {"R0_A": "A", "R0_B": "B"}

# One negative control per device kind, run on the small cell of that kind so
# the control set stays three `klt lvs` calls (~0.4 s each).  `param` is a key
# of the schematic instance card; `wrong` is the value written into the
# control's *reference* netlist only -- the generated GDS is never touched.
NEG_CONTROLS = {
    "pilot_nfet_mb1_m5": {"param": "w", "wrong": "12u"},         # MOS: wrong W
    "pilot_mim_cc": {"param": "c_width", "wrong": "20u"},        # MIM: wrong plate width
    "pilot_rpoly_rz": {"param": "r_length", "wrong": "30u"},     # res: wrong length
}


def num_um(tok):
    """'3.6u' -> 3.6 (micrometres)."""
    m = re.fullmatch(r"([0-9.]+)u", tok)
    if not m:
        raise ValueError("unsupported value " + tok)
    return float(m.group(1))


def schematic_cards():
    """instance name -> (nodes, model, params) from the sizing netlist."""
    text = open(os.path.join(REPO, "design/netlist/opamp_two_stage.spice")).read()
    text = re.sub(r"\n\+", " ", text)
    cards = {}
    for line in text.splitlines():
        if not line.startswith("X"):
            continue
        toks = re.findall(r"[^\s=]+=(?:'[^']*'|\S+)|\S+", line)
        name = toks[0]
        params = {}
        pos = []
        for t in toks[1:]:
            if "=" in t:
                k, v = t.split("=", 1)
                params[k.lower()] = v
            else:
                pos.append(t)
        cards[name] = (pos[:-1], pos[-1], params)
    return cards


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def klt_json(args, out_path, ok_rc=(0,)):
    """Run a klt verb with --format json, store stdout verbatim, return parsed."""
    r = run(["klt"] + args + ["--format", "json"], cwd=PILOT)
    open(os.path.join(PILOT, out_path), "w").write(r.stdout)
    if r.returncode not in ok_rc and not r.stdout.strip():
        raise SystemExit("klt %s failed: %s" % (args[0], r.stderr))
    return json.loads(r.stdout), r.returncode


def check_engine(rep):
    """Refuse the run unless klt's own klayout engine is the pinned one.

    regen.sh pins the klayout it imports for cell labelling, but `klt` is a
    separate process that resolves its own klayout dependency -- installing
    klayout-tools alone picks up whatever klayout is newest.  A different engine
    rewrites the GDS timestamp records and therefore every layout content hash
    in evidence/, which makes a regeneration unreproducible for no visible
    reason (klt itself only flags it, as `klayout_version_mismatch`).  Catch it
    on the first generated cell rather than after a full run.
    """
    got = (rep.get("provenance") or {}).get("klayout_version")
    if KLAYOUT_PIN and got != KLAYOUT_PIN:
        raise SystemExit(
            "klt's klayout engine is %s, want %s: install klayout==%s into the same "
            "environment as the klt on PATH (regen.sh pins it as KLAYOUT_PIN)."
            % (got, KLAYOUT_PIN, KLAYOUT_PIN))


def lvs_request(cell, gds, ref_path, opts):
    """A klt lvs request comparing `gds` against the reference netlist `ref_path`."""
    return {"schema": "klt.lvs.request/1", "engine": "klayout",
            "layout": {"file": "../" + gds, "deck": "gf180mcu", "top": cell,
                       "deck_options": {"poly_res": "1k"}},
            "reference": {"netlist": "../" + ref_path, "top": cell, "form": "subckt-call"},
            "options": opts}


def add_labels(gds_in, report, gds_out, top, mapping):
    ly = db.Layout()
    ly.read(gds_in)
    c = ly.cell(top)
    n = 0
    for p in report["ports"]:
        name = None
        for k, v in mapping.items():
            if p["name"] == k or (k.endswith("*") and p["name"].startswith(k[:-1])):
                name = v
        if name is None:
            continue
        li = ly.layer(p["layer"]["layer"], 10)  # label purpose of the port's layer
        c.shapes(li).insert(db.Text(name, db.Trans(int(round(p["x_um"] / ly.dbu)),
                                                   int(round(p["y_um"] / ly.dbu)))))
        n += 1
    ly.write(gds_out)
    return n


def main():
    cells = json.load(open(os.path.join(PILOT, "cells.json")))
    cards = schematic_cards()
    for d in ("gds", "ref", "extracted", "evidence"):
        shutil.rmtree(os.path.join(PILOT, d), ignore_errors=True)
        os.makedirs(os.path.join(PILOT, d))
    results = []
    controls = []

    for spec in cells["cells"]:
        cell, kind = spec["cell"], spec["kind"]
        insts = spec["schematic_instances"]
        first = cards[insts[0]]
        for other in insts[1:]:
            assert cards[other][1:] == first[1:] or all(
                cards[other][2][k] == first[2][k] for k in ("l", "w", "nf")), \
                "instances of one cell must share dimensions"
        nodes, model, prm = first
        gds0 = "gds/%s.raw.gds" % cell
        gds = "gds/%s.gds" % cell
        gen_rep = "evidence/%s.gen.json" % cell

        if kind == "mos":
            nf = int(prm["nf"])
            w_tot, l_um = num_um(prm["w"]), num_um(prm["l"])
            params = {"w_um": round(w_tot / nf, 6), "l_um": l_um, "fingers": nf, "rows": 1,
                      "cols": 1, "dummy": 0, "flavor": spec["flavor"], "gate_contact": True,
                      "add_guard_ring": True}
            gen = "mos_array"
            labels = MOS_LABELS
            ref_pins = "D G S B"
            def ref_card_of(p, model=model, nf=nf):
                return "XM1 D G S B %s L=%s W=%s nf=%d m=1" % (model, p["l"], p["w"], nf)
            sch_to_pins = dict(zip(["D", "G", "S", "B"], nodes))
            compare = {spec["flavor"]: ["W", "L"]}
        elif kind == "mim":
            w, l = num_um(prm["c_width"]), num_um(prm["c_length"])
            assert w == l, "cap_array generates square plates"
            params = {"plate_w_um": w, "plate_h_um": l, "num": 1}
            gen = "cap_array"
            labels = MIM_LABELS
            ref_pins = "BOT TOP"
            def ref_card_of(p, model=model):
                return "XCC BOT TOP %s c_width=%s c_length=%s m=1" % (model, p["c_width"], p["c_length"])
            sch_to_pins = dict(zip(["BOT", "TOP"], nodes))
            compare = {"cap_mim_2f0_m4m5_noshield": ["C", "A"]}
        else:
            params = {"length_um": num_um(prm["r_length"]), "width_um": num_um(prm["r_width"]),
                      "num": 1, "dummy": 0, "flavor": model.split("_")[-1]}
            gen = "res_array"
            labels = RES_LABELS
            ref_pins = "A B VSUB"
            def ref_card_of(p, model=model):
                return "XRZ A B VSUB %s r_width=%s r_length=%s m=1" % (model, p["r_width"], p["r_length"])
            sch_to_pins = dict(zip(["A", "B", "VSUB"], nodes))
            compare = {model: ["L", "W"]}

        rep, rc = klt_json(["gen", gen, "--pdk", PDK, "--params", json.dumps(params),
                            "--cell-name", cell, "-o", gds0], gen_rep)
        assert rc == 0, rep
        check_engine(rep)
        nlab = add_labels(os.path.join(PILOT, gds0), rep, os.path.join(PILOT, gds), cell, labels)
        os.remove(os.path.join(PILOT, gds0))
        # the generator report's absolute output path is not stable; keep a relative one
        rep["gds_path"] = gds
        json.dump(rep, open(os.path.join(PILOT, gen_rep), "w"), indent=2)
        bbox = rep["bbox_um"]

        ref_k = "ref/%s.klt.spice" % cell
        open(os.path.join(PILOT, ref_k), "w").write(
            "* schematic-derived reference for %s (dimensions from %s; ad/as/pd/ps omitted)\n"
            ".subckt %s %s\n%s\n.ends\n" % (cell, "+".join(insts), cell, ref_pins, ref_card_of(prm)))

        ev = {"cell": cell, "kind": kind, "schematic_instances": insts, "model": model,
              "gen_params": params, "labels_added": nlab,
              "terminal_mapping_schematic_net_to_cell_pin": sch_to_pins,
              "bbox_um": [round(bbox["x1"] - bbox["x0"], 3), round(bbox["y1"] - bbox["y0"], 3)]}

        # --- DRC: klt curated deck -----------------------------------------------
        d, rc = klt_json(["drc", gds, "--deck", "gf180mcu", "--pdk", PDK], "evidence/%s.drc.klt.json" % cell,
                         ok_rc=(0, 3))
        ev["drc_klt_curated"] = {"status": d.get("status"), "violations": d.get("violation_count"),
                                 "rules_checked": len(d.get("coverage", {}).get("rules_checked", [])),
                                 "rules_skipped": len(d.get("coverage", {}).get("rules_skipped", []))}

        # --- extraction + LVS: klt -------------------------------------------------
        d, rc = klt_json(["extract", gds, "--deck", "gf180mcu", "--pdk", PDK,
                          "--deck-option", "poly_res=1k", "-o", "extracted/%s.klt.spice" % cell],
                         "evidence/%s.extract.klt.json" % cell)
        ev["extract_klt"] = {"status": d.get("status"), "device_counts": d.get("device_counts"),
                             "warnings": d.get("warnings", [])}
        opts = {"combine_devices": True}
        if compare:
            opts["compare_parameters"] = compare
        json.dump(lvs_request(cell, gds, ref_k, opts),
                  open(os.path.join(PILOT, "evidence/%s.lvs.klt.request.json" % cell), "w"), indent=2)
        d, rc = klt_json(["lvs", "evidence/%s.lvs.klt.request.json" % cell],
                         "evidence/%s.lvs.klt.json" % cell, ok_rc=(0, 3))
        ev["lvs_klt"] = {"status": d.get("status"), "mismatch_count": d.get("mismatch_count"),
                         "errors": d.get("error_count"), "counts": d.get("counts"),
                         "parameters_compared": compare}
        pos_env = d.get("environment") or {}

        # --- negative control: same GDS, one reference dimension wrong on purpose --
        nc = NEG_CONTROLS.get(cell)
        if nc:
            param, wrong = nc["param"], nc["wrong"]
            true_value = prm[param]
            stem = "%s.negctl-%s" % (cell, param)
            nc_ref = "ref/%s.klt.spice" % stem
            open(os.path.join(PILOT, nc_ref), "w").write(
                "* NEGATIVE CONTROL for %s -- %s is deliberately %s (schematic value %s).\n"
                "* klt lvs must report a mismatch with a device.property error against the\n"
                "* unchanged gds/%s.gds; a match here would mean the device-parameter\n"
                "* compare is insensitive to the dimensions this pilot claims it checks,\n"
                "* and tools/pilot.py exits non-zero.\n"
                ".subckt %s %s\n%s\n.ends\n"
                % (cell, param, wrong, true_value, cell, cell, ref_pins,
                   ref_card_of(dict(prm, **{param: wrong}))))
            nc_req = "evidence/%s.lvs.klt.request.json" % stem
            nc_out = "evidence/%s.lvs.klt.json" % stem
            json.dump(lvs_request(cell, gds, nc_ref, opts),
                      open(os.path.join(PILOT, nc_req), "w"), indent=2)
            d, rc = klt_json(["lvs", nc_req], nc_out, ok_rc=(0, 3))
            nc_env = d.get("environment") or {}
            cec = d.get("category_error_counts") or {}
            ctl = {"cell": cell, "kind": kind, "altered_parameter": param,
                   "schematic_value": true_value, "control_value": wrong,
                   "parameters_compared": compare,
                   "status": d.get("status"), "error_count": d.get("error_count"),
                   "mismatch_count": d.get("mismatch_count"),
                   "category_error_counts": cec,
                   "device_property_errors": cec.get("device.property", 0),
                   "klt_exit_code": rc,
                   "reference_netlist": nc_ref, "request": nc_req, "evidence": nc_out,
                   "layout_sha256": nc_env.get("layout_sha256"),
                   "layout_sha256_matches_positive":
                       nc_env.get("layout_sha256") == pos_env.get("layout_sha256"),
                   "reference_sha256": nc_env.get("reference_sha256"),
                   "positive_reference_sha256": pos_env.get("reference_sha256")}
            ctl["tripped"] = bool(ctl["status"] != "match" and ctl["device_property_errors"] >= 1
                                  and ctl["layout_sha256_matches_positive"])
            controls.append(ctl)

        results.append(ev)
        line = [cell, ev["drc_klt_curated"]["status"], ev["lvs_klt"]["status"]]
        if nc:
            line.append("negctl(%s=%s) %s device.property=%d"
                        % (param, wrong, ctl["status"], ctl["device_property_errors"]))
        print(*line, flush=True)

    json.dump({"schema": "gf180-opamp.layout-pilot.results/2", "cells": results,
               "negative_controls": controls},
              open(os.path.join(PILOT, "results.json"), "w"), indent=2)

    # The gate: a passing LVS only means something if a wrong dimension fails.
    # klt_json tolerates rc 0, so a control that wrongly matched would otherwise
    # pass unnoticed -- and "some mismatch appeared" is too weak, because the
    # perturbation also raises device.unmatched / net.unmatched entries.  Require
    # a device.property error specifically, against a byte-identical layout.
    not_run = [c for c in sorted(NEG_CONTROLS) if c not in {x["cell"] for x in controls}]
    failed = [c for c in controls if not c["tripped"]]
    for c in failed:
        print("NEGATIVE CONTROL DID NOT TRIP: %s with %s=%s (schematic %s): status=%s, "
              "device.property errors=%d, layout unchanged=%s -- see %s"
              % (c["cell"], c["altered_parameter"], c["control_value"], c["schematic_value"],
                 c["status"], c["device_property_errors"], c["layout_sha256_matches_positive"],
                 c["evidence"]), flush=True)
    for cell in not_run:
        print("NEGATIVE CONTROL DID NOT RUN: %s (not in cells.json?)" % cell, flush=True)
    if failed or not_run:
        raise SystemExit(
            "negative controls failed (%d of %d did not trip, %d did not run): the klt lvs "
            "device-parameter compare no longer proves the pilot cells match the schematic "
            "dimensions, so the 'match' verdicts above cannot be relied on."
            % (len(failed), len(controls), len(not_run)))


if __name__ == "__main__":
    main()
