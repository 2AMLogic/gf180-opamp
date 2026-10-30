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

MOS_LABELS = {"U0_S": "S", "U0_D": "D", "U0_G": "G", "TAP_*": "B"}
MIM_LABELS = {"C0_BOT": "BOT", "C0_TOP": "TOP"}
RES_LABELS = {"R0_A": "A", "R0_B": "B"}


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
            ref_card = "XM1 D G S B %s L=%s W=%s nf=%d m=1" % (model, prm["l"], prm["w"], nf)
            sch_to_pins = dict(zip(["D", "G", "S", "B"], nodes))
            compare = {spec["flavor"]: ["W", "L"]}
        elif kind == "mim":
            w, l = num_um(prm["c_width"]), num_um(prm["c_length"])
            assert w == l, "cap_array generates square plates"
            params = {"plate_w_um": w, "plate_h_um": l, "num": 1}
            gen = "cap_array"
            labels = MIM_LABELS
            ref_pins = "BOT TOP"
            ref_card = "XCC BOT TOP %s c_width=%s c_length=%s m=1" % (model, prm["c_width"], prm["c_length"])
            sch_to_pins = dict(zip(["BOT", "TOP"], nodes))
            compare = {"cap_mim_2f0_m4m5_noshield": ["C", "A"]}
        else:
            params = {"length_um": num_um(prm["r_length"]), "width_um": num_um(prm["r_width"]),
                      "num": 1, "dummy": 0, "flavor": model.split("_")[-1]}
            gen = "res_array"
            labels = RES_LABELS
            ref_pins = "A B VSUB"
            ref_card = "XRZ A B VSUB %s r_width=%s r_length=%s m=1" % (model, prm["r_width"], prm["r_length"])
            sch_to_pins = dict(zip(["A", "B", "VSUB"], nodes))
            compare = {model: ["L", "W"]}

        rep, rc = klt_json(["gen", gen, "--pdk", PDK, "--params", json.dumps(params),
                            "--cell-name", cell, "-o", gds0], gen_rep)
        assert rc == 0, rep
        nlab = add_labels(os.path.join(PILOT, gds0), rep, os.path.join(PILOT, gds), cell, labels)
        os.remove(os.path.join(PILOT, gds0))
        # the generator report's absolute output path is not stable; keep a relative one
        rep["gds_path"] = gds
        json.dump(rep, open(os.path.join(PILOT, gen_rep), "w"), indent=2)
        bbox = rep["bbox_um"]

        ref_k = "ref/%s.klt.spice" % cell
        open(os.path.join(PILOT, ref_k), "w").write(
            "* schematic-derived reference for %s (dimensions from %s; ad/as/pd/ps omitted)\n"
            ".subckt %s %s\n%s\n.ends\n" % (cell, "+".join(insts), cell, ref_pins, ref_card))

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
        req = {"schema": "klt.lvs.request/1", "engine": "klayout",
               "layout": {"file": "../" + gds, "deck": "gf180mcu", "top": cell,
                          "deck_options": {"poly_res": "1k"}},
               "reference": {"netlist": "../" + ref_k, "top": cell, "form": "subckt-call"},
               "options": opts}
        json.dump(req, open(os.path.join(PILOT, "evidence/%s.lvs.klt.request.json" % cell), "w"), indent=2)
        d, rc = klt_json(["lvs", "evidence/%s.lvs.klt.request.json" % cell],
                         "evidence/%s.lvs.klt.json" % cell, ok_rc=(0, 3))
        ev["lvs_klt"] = {"status": d.get("status"), "mismatch_count": d.get("mismatch_count"),
                         "errors": d.get("error_count"), "counts": d.get("counts"),
                         "parameters_compared": compare}

        results.append(ev)
        print(cell, ev["drc_klt_curated"]["status"], ev["lvs_klt"]["status"], flush=True)

    json.dump({"schema": "gf180-opamp.layout-pilot.results/1", "cells": results},
              open(os.path.join(PILOT, "results.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
