#!/usr/bin/env python3
"""Offline consistency check tying layout/pilot to the schematic (issue #330).

`regen.sh` needs klt, KLayout and the PDK, so CI cannot run it. This script is
the gate that can: with only the Python standard library it re-derives, from
the committed tree,

  schematic card -> gen_params, terminal mapping, labels, bbox in results.json
  schematic card -> ref/<cell>.klt.spice byte-for-byte (positive and negative controls)
  results.json   -> the DRC / extract / LVS envelopes in evidence/
  gds + ref bytes-> the sha256 bindings recorded in the LVS envelopes

so a re-sized schematic, a regenerated cell left behind, or an edited
results.json cannot pass unnoticed. Read-only: no file is written.

    python3 layout/pilot/check_consistency.py              # root from __file__
    python3 layout/pilot/check_consistency.py --root DIR   # scratch copy (tests)

The re-derivations mirror tools/pilot.py at the version that produced the
committed evidence (num_um, schematic_cards, the reference-card writers and
NEG_CONTROLS); edit the two together. Stdlib only -- no klt, KLayout, PDK or
simulator, so it also refuses to import the producer, which pulls in
klayout.db. Exit: 0 consistent, 1 any inconsistency (all failures printed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PILOT_REL = Path("layout/pilot")
NETLIST_REL = Path("design/netlist/opamp_two_stage.spice")
RESULTS_REL = PILOT_REL / "results.json"
CELLS_REL = PILOT_REL / "cells.json"

#: results.json schema versions this checker understands (producer:
#: tools/pilot.py, which writes /2). An unknown version is refused loudly.
SCHEMA_1 = "gf180-opamp.layout-pilot.results/1"
SCHEMA_2 = "gf180-opamp.layout-pilot.results/2"
KNOWN_SCHEMAS = (SCHEMA_1, SCHEMA_2)

#: Cell-pin orders and label-name -> cell-pin maps, mirrored from tools/pilot.py.
KIND_PINS = {"mos": ("D", "G", "S", "B"), "mim": ("BOT", "TOP"), "res": ("A", "B", "VSUB")}
KIND_LABELS = {
    "mos": {"U0_S": "S", "U0_D": "D", "U0_G": "G", "TAP_*": "B"},
    "mim": {"C0_BOT": "BOT", "C0_TOP": "TOP"},
    "res": {"R0_A": "A", "R0_B": "B"},
}
#: Schematic-card parameter keys that must be identical across the instances
#: of one cell (tools/pilot.py asserts the same before generating).
KIND_DIM_KEYS = {"mos": ("l", "w", "nf"), "mim": ("c_width", "c_length"), "res": ("r_width", "r_length")}

#: One negative control per device kind, mirrored from tools/pilot.py
#: NEG_CONTROLS: cell -> (altered card parameter, the deliberately wrong value
#: written into the control's reference netlist only).
NEG_CONTROLS = {
    "pilot_nfet_mb1_m5": ("w", "12u"),
    "pilot_mim_cc": ("c_width", "20u"),
    "pilot_rpoly_rz": ("r_length", "30u"),
}

#: tools/pilot.py hardcodes the MIM compare key instead of using the card model.
MIM_COMPARE_MODEL = "cap_mim_2f0_m4m5_noshield"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_sha(value) -> str:
    """'sha256:<hex>' or '<hex>' -> '<hex>'; the envelopes use both spellings."""
    if not isinstance(value, str):
        return ""
    return value[len("sha256:"):] if value.startswith("sha256:") else value


def num_um(tok):
    """'3.6u' -> 3.6 (micrometres). Mirrors tools/pilot.py num_um."""
    m = re.fullmatch(r"([0-9.]+)u", tok)
    if not m:
        raise ValueError("unsupported value " + str(tok))
    return float(m.group(1))


def schematic_cards(text):
    """instance name -> (nodes, model, params); mirrors tools/pilot.py."""
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


def ref_card_of(kind, model, prm):
    """The reference device card; mirrors tools/pilot.py's ref_card_of per kind."""
    if kind == "mos":
        return "XM1 D G S B %s L=%s W=%s nf=%d m=1" % (model, prm["l"], prm["w"], int(prm["nf"]))
    if kind == "mim":
        return "XCC BOT TOP %s c_width=%s c_length=%s m=1" % (model, prm["c_width"], prm["c_length"])
    return "XRZ A B VSUB %s r_width=%s r_length=%s m=1" % (model, prm["r_width"], prm["r_length"])


def ref_netlist_text(cell, insts, kind, model, prm):
    """Byte-exact re-derivation of ref/<cell>.klt.spice (tools/pilot.py)."""
    pins = " ".join(KIND_PINS[kind])
    return ("* schematic-derived reference for %s (dimensions from %s; ad/as/pd/ps omitted)\n"
            ".subckt %s %s\n%s\n.ends\n"
            % (cell, "+".join(insts), cell, pins, ref_card_of(kind, model, prm)))


def negctl_ref_text(cell, kind, model, prm, param, wrong):
    """Byte-exact re-derivation of ref/<cell>.negctl-<param>.klt.spice."""
    pins = " ".join(KIND_PINS[kind])
    return ("* NEGATIVE CONTROL for %s -- %s is deliberately %s (schematic value %s).\n"
            "* klt lvs must report a mismatch with a device.property error against the\n"
            "* unchanged gds/%s.gds; a match here would mean the device-parameter\n"
            "* compare is insensitive to the dimensions this pilot claims it checks,\n"
            "* and tools/pilot.py exits non-zero.\n"
            ".subckt %s %s\n%s\n.ends\n"
            % (cell, param, wrong, prm[param], cell, cell, pins,
               ref_card_of(kind, model, dict(prm, **{param: wrong}))))


def count_labels(port_names, mapping):
    """Ports a label would be added to; mirrors tools/pilot.py add_labels
    (an exact key match, or a prefix match for a key ending in '*')."""
    n = 0
    for name in port_names:
        for k in mapping:
            if name == k or (k.endswith("*") and str(name).startswith(k[:-1])):
                n += 1
                break
    return n


def expected_compare(kind, gp_flavor, model):
    """The compare_parameters dict tools/pilot.py derives per kind."""
    if kind == "mos":
        return {gp_flavor: ["W", "L"]}
    if kind == "mim":
        return {MIM_COMPARE_MODEL: ["C", "A"]}
    return {model: ["L", "W"]}


class Checker:
    def __init__(self, root: Path):
        self.root = root
        self.errors: list[str] = []

    def err(self, msg):
        self.errors.append(msg)

    def pilot_path(self, *parts) -> Path:
        return self.root.joinpath(PILOT_REL, *parts)

    def load_json(self, rel: Path, what: str):
        p = self.root / rel
        if not p.is_file():
            self.err(f"missing {what}: {rel}")
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError) as e:
            self.err(f"{rel}: unreadable {what} ({e})")
            return None

    # ------------------------------------------------------------ results ---
    def check(self) -> list[str]:
        netlist = self.root / NETLIST_REL
        if not netlist.is_file():
            self.err(f"missing schematic netlist: {NETLIST_REL}")
            return self.errors
        cards = schematic_cards(netlist.read_text())

        results = self.load_json(RESULTS_REL, "results.json")
        if results is None:
            return self.errors
        schema = results.get("schema")
        if schema not in KNOWN_SCHEMAS:
            self.err(
                "results.json: unknown schema %r -- this checker understands %s; refusing to "
                "interpret an unknown record version rather than passing vacuously"
                % (schema, " / ".join(KNOWN_SCHEMAS)))
            return self.errors

        cells_json = self.load_json(CELLS_REL, "cells.json")
        if cells_json is None:
            return self.errors
        cj_cells = self.indexed_cells(cells_json, "cells.json")
        rj_cells = self.indexed_cells(results, "results.json")
        if cj_cells is None or rj_cells is None:
            return self.errors

        self.check_cell_tables(cj_cells, rj_cells)
        self.check_coverage(cards, rj_cells)

        for name, rj in rj_cells.items():
            cj = cj_cells.get(name)
            if cj is None:
                continue  # already reported by check_cell_tables
            try:
                self.check_cell(name, cj, rj, cards)
            except (KeyError, IndexError, ValueError, TypeError) as e:
                self.err(f"{name}: malformed record or schematic card ({type(e).__name__}: {e})")

        if schema == SCHEMA_2:
            self.check_negative_controls(results, rj_cells, cards)
        elif "negative_controls" in results:
            self.err("results.json: schema %s carries a negative_controls list, which the "
                     "producer writes only from %s" % (SCHEMA_1, SCHEMA_2))
        return self.errors

    def indexed_cells(self, doc, what):
        cells = doc.get("cells")
        if not isinstance(cells, list) or not cells:
            self.err(f"{what}: no cells[] list")
            return None
        out = {}
        for c in cells:
            name = c.get("cell") if isinstance(c, dict) else None
            if not name:
                self.err(f"{what}: a cells[] entry has no cell name")
                continue
            if name in out:
                self.err(f"{what}: duplicate cell {name}")
            out[name] = c
        return out

    # --------------------------------------------------- cells.json vs results ---
    def check_cell_tables(self, cj_cells, rj_cells):
        for name in sorted(set(cj_cells) | set(rj_cells)):
            if name not in cj_cells:
                self.err(f"results.json cell {name} is missing from cells.json (the declared input)")
            elif name not in rj_cells:
                self.err(f"cells.json cell {name} is missing from results.json (the recorded output)")
            else:
                cj, rj = cj_cells[name], rj_cells[name]
                if cj.get("kind") != rj.get("kind"):
                    self.err(f"{name}: kind disagrees -- cells.json {cj.get('kind')!r} vs "
                             f"results.json {rj.get('kind')!r}")
                if cj.get("schematic_instances") != rj.get("schematic_instances"):
                    self.err(f"{name}: schematic_instances disagree -- cells.json "
                             f"{cj.get('schematic_instances')} vs results.json "
                             f"{rj.get('schematic_instances')}")

    def check_coverage(self, cards, rj_cells):
        covered = {}
        for name, rj in rj_cells.items():
            for inst in rj.get("schematic_instances") or []:
                if inst in covered:
                    self.err(f"schematic instance {inst} is claimed by both {covered[inst]} and {name}")
                covered[inst] = name
                if inst not in cards:
                    self.err(f"{name}: instance {inst} named in results.json is not in the netlist")
        for inst in sorted(cards):
            if inst not in covered:
                self.err(f"uncovered schematic instance {inst}: covered by no pilot cell")

    # ------------------------------------------------------------ per cell ---
    def check_cell(self, name, cj, rj, cards):
        kind = rj.get("kind")
        if kind not in KIND_PINS:
            self.err(f"{name}: unknown kind {kind!r}")
            return
        insts = rj.get("schematic_instances") or []
        missing = [i for i in insts if i not in cards]
        if missing:
            self.err(f"{name}: instances {missing} not in the netlist; skipping its card relations")
            return
        nodes, model, prm = cards[insts[0]]

        # every instance of the cell must be interchangeable for the generator
        # (mirrors tools/pilot.py's sharing assertion, with model equality added)
        dims = KIND_DIM_KEYS[kind]
        for other in insts[1:]:
            onodes, omodel, oprm = cards[other]
            same_dims = all(oprm.get(k) == prm.get(k) for k in dims)
            if not ((omodel == model and onodes == nodes) or same_dims):
                self.err(f"{name}: instances {insts[0]} and {other} do not share dimensions "
                         f"({dims}) or card, so one generator cell cannot correspond to both")
            if omodel != model:
                self.err(f"{name}: instance {other} has model {omodel!r}, recorded model is {model!r}")

        self.check_gen_params(name, kind, cj, rj, model, prm, nodes)
        self.check_gen_evidence(name, kind, rj)
        self.check_envelopes(name, kind, rj)
        self.check_hash_bindings(name, insts, kind, rj, model, prm)

    def check_gen_params(self, name, kind, cj, rj, model, prm, nodes):
        if rj.get("model") != model:
            self.err(f"{name}: model {rj.get('model')!r} != netlist card model {model!r}")
        gp = rj.get("gen_params") or {}

        if kind == "mos":
            nf = int(prm["nf"])
            want = {"w_um": round(num_um(prm["w"]) / nf, 6), "l_um": num_um(prm["l"]), "fingers": nf}
            for k, v in want.items():
                if gp.get(k) != v:
                    self.err(f"{name}: gen_params.{k} {gp.get(k)!r} != schematic-derived {v!r} "
                             f"(w={prm['w']}, l={prm['l']}, nf={prm['nf']})")
            if gp.get("flavor") != cj.get("flavor"):
                self.err(f"{name}: gen_params.flavor {gp.get('flavor')!r} != cells.json flavor "
                         f"{cj.get('flavor')!r}")
        elif kind == "mim":
            for k, card_k in (("plate_w_um", "c_width"), ("plate_h_um", "c_length")):
                if gp.get(k) != num_um(prm[card_k]):
                    self.err(f"{name}: gen_params.{k} {gp.get(k)!r} != card {card_k}={prm[card_k]}")
        else:
            for k, card_k in (("length_um", "r_length"), ("width_um", "r_width")):
                if gp.get(k) != num_um(prm[card_k]):
                    self.err(f"{name}: gen_params.{k} {gp.get(k)!r} != card {card_k}={prm[card_k]}")
            if gp.get("flavor") != model.split("_")[-1]:
                self.err(f"{name}: gen_params.flavor {gp.get('flavor')!r} != "
                         f"model suffix {model.split('_')[-1]!r}")

        mapping = dict(zip(KIND_PINS[kind], nodes))
        if rj.get("terminal_mapping_schematic_net_to_cell_pin") != mapping:
            self.err(f"{name}: terminal_mapping {rj.get('terminal_mapping_schematic_net_to_cell_pin')} "
                     f"!= card nodes {nodes} mapped to {list(KIND_PINS[kind])}")

    def check_gen_evidence(self, name, kind, rj):
        gen = self.load_json(PILOT_REL / "evidence" / f"{name}.gen.json", f"{name} gen report")
        if gen is None:
            return
        ports = [p.get("name") for p in gen.get("ports") or []]
        nlab = count_labels(ports, KIND_LABELS[kind])
        if rj.get("labels_added") != nlab:
            self.err(f"{name}: labels_added {rj.get('labels_added')} != {nlab} matching ports in "
                     f"the gen report")
        b = gen.get("bbox_um") or {}
        try:
            want_bbox = [round(b["x1"] - b["x0"], 3), round(b["y1"] - b["y0"], 3)]
        except (KeyError, TypeError):
            self.err(f"{name}: gen report has no usable bbox_um")
            return
        # round to 3 decimals: the raw extent subtraction is float-noisy
        # (6.8100000000000005 - (-1.23) == 8.040000000000001 vs recorded 8.04)
        if rj.get("bbox_um") != want_bbox:
            self.err(f"{name}: bbox_um {rj.get('bbox_um')} != gen report extent {want_bbox}")

    def check_envelopes(self, name, kind, rj):
        drc = self.load_json(PILOT_REL / "evidence" / f"{name}.drc.klt.json", f"{name} DRC envelope")
        ext = self.load_json(PILOT_REL / "evidence" / f"{name}.extract.klt.json", f"{name} extract envelope")
        lvs = self.load_json(PILOT_REL / "evidence" / f"{name}.lvs.klt.json", f"{name} LVS envelope")

        if drc is not None:
            d = rj.get("drc_klt_curated") or {}
            cov = drc.get("coverage") or {}
            if d.get("status") != drc.get("status"):
                self.err(f"{name}: drc_klt_curated.status {d.get('status')!r} != envelope "
                         f"{drc.get('status')!r}")
            if d.get("violations") != drc.get("violation_count"):
                self.err(f"{name}: drc_klt_curated.violations {d.get('violations')} != envelope "
                         f"violation_count {drc.get('violation_count')}")
            # these are list lengths in the envelope, not scalars
            for k in ("rules_checked", "rules_skipped"):
                want = len(cov.get(k) or [])
                if d.get(k) != want:
                    self.err(f"{name}: drc_klt_curated.{k} {d.get(k)} != len(envelope coverage.{k}) "
                             f"= {want}")

        if ext is not None:
            e = rj.get("extract_klt") or {}
            for k in ("status", "device_counts", "warnings"):
                if e.get(k) != ext.get(k):
                    self.err(f"{name}: extract_klt.{k} {e.get(k)!r} != extract envelope "
                             f"{ext.get(k)!r}")

        if lvs is not None:
            l = rj.get("lvs_klt") or {}
            for k, ek in (("status", "status"), ("mismatch_count", "mismatch_count"),
                          ("errors", "error_count"), ("counts", "counts")):
                if l.get(k) != lvs.get(ek):
                    self.err(f"{name}: lvs_klt.{k} {l.get(k)!r} != envelope {ek} {lvs.get(ek)!r}")
            env_opts = lvs.get("options") or {}
            if l.get("parameters_compared") != env_opts.get("compare_parameters"):
                self.err(f"{name}: lvs_klt.parameters_compared {l.get('parameters_compared')} != "
                         f"envelope options.compare_parameters {env_opts.get('compare_parameters')}")
            gp = (rj.get("gen_params") or {})
            want_cmp = expected_compare(kind, gp.get("flavor"), rj.get("model"))
            if l.get("parameters_compared") != want_cmp:
                self.err(f"{name}: lvs_klt.parameters_compared {l.get('parameters_compared')} != "
                         f"what the producer derives for a {kind} cell: {want_cmp}")

    def check_hash_bindings(self, name, insts, kind, rj, model, prm):
        gds = self.pilot_path("gds", f"{name}.gds")
        if not gds.is_file():
            self.err(f"missing gds/{name}.gds")
            return
        gds_sha = sha256_hex(gds.read_bytes())

        lvs = self.load_json(PILOT_REL / "evidence" / f"{name}.lvs.klt.json", f"{name} LVS envelope")
        drc = self.load_json(PILOT_REL / "evidence" / f"{name}.drc.klt.json", f"{name} DRC envelope")
        if lvs is not None:
            env = lvs.get("environment") or {}
            prov_in = (lvs.get("provenance") or {}).get("input") or {}
            for where, got in (("environment.layout_sha256", env.get("layout_sha256")),
                               ("provenance.input.content_hash", prov_in.get("content_hash"))):
                if norm_sha(got) != gds_sha:
                    self.err(f"{name}: sha256(gds/{name}.gds) {gds_sha[:12]}... != LVS envelope "
                             f"{where} {got!r}")
            ref = self.pilot_path("ref", f"{name}.klt.spice")
            if not ref.is_file():
                self.err(f"missing ref/{name}.klt.spice")
            else:
                ref_bytes = ref.read_bytes()
                ref_sha = sha256_hex(ref_bytes)
                if norm_sha(env.get("reference_sha256")) != ref_sha:
                    self.err(f"{name}: sha256(ref/{name}.klt.spice) {ref_sha[:12]}... != LVS envelope "
                             f"environment.reference_sha256 {env.get('reference_sha256')!r}")
                want = ref_netlist_text(name, insts, kind, model, prm)
                if ref_bytes.decode("utf-8", "replace") != want:
                    self.err(f"{name}: ref/{name}.klt.spice does not re-derive byte-for-byte from "
                             f"the schematic card ({'+'.join(insts)})")
        if drc is not None:
            drc_in = (drc.get("provenance") or {}).get("input") or {}
            if norm_sha(drc_in.get("content_hash")) != gds_sha:
                self.err(f"{name}: sha256(gds/{name}.gds) {gds_sha[:12]}... != DRC envelope "
                         f"provenance.input.content_hash {drc_in.get('content_hash')!r}")

    # -------------------------------------------------- negative controls ---
    def check_negative_controls(self, results, rj_cells, cards):
        controls = results.get("negative_controls")
        if not isinstance(controls, list):
            self.err("results.json: schema /2 without a negative_controls list")
            return

        seen = set()
        for i, ctl in enumerate(controls):
            tag = f"negative_controls[{i}]"
            if not isinstance(ctl, dict):
                self.err(f"{tag}: not an object")
                continue
            name = ctl.get("cell")
            param = ctl.get("altered_parameter")
            key = (name, param)
            if key in seen:
                self.err(f"{tag}: duplicate control for {name} {param}")
            seen.add(key)
            try:
                self.check_one_control(tag, ctl, name, param, rj_cells, cards)
            except (KeyError, IndexError, ValueError, TypeError) as e:
                self.err(f"{tag} ({name} {param}): malformed record or card "
                         f"({type(e).__name__}: {e})")

        for cell, (param, _wrong) in sorted(NEG_CONTROLS.items()):
            if (cell, param) not in seen:
                self.err(f"results.json: the {cell} {param} negative control is missing "
                         f"(tools/pilot.py runs one control per device kind)")

    def check_one_control(self, tag, ctl, name, param, rj_cells, cards):
        if name not in rj_cells:
            self.err(f"{tag}: control cell {name!r} is not a pilot cell in results.json")
            return
        expected = NEG_CONTROLS.get(name)
        if expected is None:
            self.err(f"{tag}: no negative control is defined for {name} in tools/pilot.py")
            return
        exp_param, exp_wrong = expected
        if param != exp_param:
            self.err(f"{tag}: altered_parameter {param!r} != producer's {exp_param!r}")
            return

        rj = rj_cells[name]
        kind = rj.get("kind")
        insts = rj.get("schematic_instances") or []
        if not insts or insts[0] not in cards:
            self.err(f"{tag}: cannot resolve {name}'s schematic card")
            return
        _nodes, model, prm = cards[insts[0]]

        # the recorded values must be the card's true value and the producer's wrong value
        if ctl.get("schematic_value") != prm.get(param):
            self.err(f"{tag}: schematic_value {ctl.get('schematic_value')!r} != card value "
                     f"{prm.get(param)!r}")
        if ctl.get("control_value") != exp_wrong:
            self.err(f"{tag}: control_value {ctl.get('control_value')!r} != producer's "
                     f"{exp_wrong!r}")

        # envelope the record's own evidence field names
        evidence = ctl.get("evidence")
        if not isinstance(evidence, str) or not evidence:
            self.err(f"{tag}: no evidence path")
            return
        env = self.load_json(PILOT_REL / evidence, f"{tag} envelope")
        if env is None:
            return
        for k, ek in (("status", "status"), ("error_count", "error_count"),
                      ("mismatch_count", "mismatch_count"),
                      ("category_error_counts", "category_error_counts")):
            if ctl.get(k) != env.get(ek):
                self.err(f"{tag}: {k} {ctl.get(k)!r} != envelope {ek} {env.get(ek)!r}")
        dpe = (ctl.get("category_error_counts") or {}).get("device.property", 0)
        if ctl.get("device_property_errors") != dpe:
            self.err(f"{tag}: device_property_errors {ctl.get('device_property_errors')} != "
                     f"category_error_counts['device.property'] {dpe}")
        if ctl.get("parameters_compared") != (env.get("options") or {}).get("compare_parameters"):
            self.err(f"{tag}: parameters_compared != envelope options.compare_parameters")

        # a control exists to fail: status must be a mismatch carrying a
        # device.property error (tools/pilot.py exits non-zero otherwise)
        if ctl.get("status") != "mismatch" or env.get("status") != "mismatch":
            self.err(f"{tag}: the negative control did not trip -- record status "
                     f"{ctl.get('status')!r}, envelope status {env.get('status')!r} "
                     f"(expected mismatch)")
        if not isinstance(dpe, int) or dpe < 1:
            self.err(f"{tag}: expected >=1 device.property error, got {dpe!r}")

        # hash bindings: control layout == unchanged GDS == the positive run
        gds = self.pilot_path("gds", f"{name}.gds")
        if not gds.is_file():
            self.err(f"missing gds/{name}.gds")
            return
        gds_sha = sha256_hex(gds.read_bytes())
        env_layout = norm_sha((env.get("environment") or {}).get("layout_sha256"))
        if ctl.get("layout_sha256") != gds_sha:
            self.err(f"{tag}: layout_sha256 {ctl.get('layout_sha256')!r} != sha256 of the "
                     f"unchanged gds/{name}.gds")
        if env_layout and env_layout != gds_sha:
            self.err(f"{tag}: envelope environment.layout_sha256 != sha256(gds/{name}.gds); "
                     f"the control did not run against the committed layout")
        pos_env = self.load_json(PILOT_REL / "evidence" / f"{name}.lvs.klt.json",
                                 f"{name} positive LVS envelope") or {}
        pos_layout = norm_sha(((pos_env.get("environment")) or {}).get("layout_sha256"))
        matches = bool(env_layout and env_layout == pos_layout)
        if ctl.get("layout_sha256_matches_positive") != matches:
            self.err(f"{tag}: layout_sha256_matches_positive "
                     f"{ctl.get('layout_sha256_matches_positive')!r} != re-derived {matches!r}")

        ref_sha = sha256_hex(self.pilot_path("ref", f"{name}.negctl-{param}.klt.spice").read_bytes()) \
            if self.pilot_path("ref", f"{name}.negctl-{param}.klt.spice").is_file() else None
        if ref_sha is None:
            self.err(f"missing ref/{name}.negctl-{param}.klt.spice")
        else:
            if ctl.get("reference_sha256") != ref_sha:
                self.err(f"{tag}: reference_sha256 != sha256 of ref/{name}.negctl-{param}.klt.spice")
            if norm_sha((env.get("environment") or {}).get("reference_sha256")) != ref_sha:
                self.err(f"{tag}: envelope environment.reference_sha256 != sha256 of the "
                         f"control's reference netlist")
            pos_ref = self.pilot_path("ref", f"{name}.klt.spice")
            if not pos_ref.is_file():
                self.err(f"missing ref/{name}.klt.spice")
            else:
                pos_sha = sha256_hex(pos_ref.read_bytes())
                if ctl.get("positive_reference_sha256") != pos_sha:
                    self.err(f"{tag}: positive_reference_sha256 != sha256 of ref/{name}.klt.spice")

            # the control's reference netlist re-derives byte-for-byte too
            want = negctl_ref_text(name, kind, model, prm, param, exp_wrong)
            got = self.pilot_path("ref", f"{name}.negctl-{param}.klt.spice").read_text()
            if got != want:
                self.err(f"{tag}: ref/{name}.negctl-{param}.klt.spice does not re-derive "
                         f"byte-for-byte from the schematic card with {param}={exp_wrong}")

        # tripped is a derived field; re-derive it
        tripped = bool(ctl.get("status") != "match" and isinstance(dpe, int) and dpe >= 1
                       and ctl.get("layout_sha256_matches_positive"))
        if ctl.get("tripped") != tripped:
            self.err(f"{tag}: tripped {ctl.get('tripped')!r} != re-derived {tripped!r}")

        # the recorded paths must point at the producer's file layout
        want_ref = f"ref/{name}.negctl-{param}.klt.spice"
        want_req = f"evidence/{name}.negctl-{param}.lvs.klt.request.json"
        if ctl.get("reference_netlist") != want_ref:
            self.err(f"{tag}: reference_netlist {ctl.get('reference_netlist')!r} != {want_ref!r}")
        if ctl.get("request") != want_req:
            self.err(f"{tag}: request {ctl.get('request')!r} != {want_req!r}")
        if not self.pilot_path(want_req).is_file():
            self.err(f"missing {want_req}")


def main(argv=None) -> int:
    default_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(
        description="Offline layout/pilot consistency check (stdlib only; read-only).")
    ap.add_argument("--root", default=str(default_root),
                    help="repo root to check (default: the tree this file lives in)")
    args = ap.parse_args(argv)

    errors = Checker(Path(args.root).resolve()).check()
    for e in errors:
        print("INCONSISTENT: " + e)
    if errors:
        print(f"layout/pilot consistency: {len(errors)} failure(s)")
        return 1
    print("layout/pilot consistency: OK -- schematic, results.json, evidence envelopes, "
          "GDS hashes and reference netlists agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
