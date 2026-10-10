#!/usr/bin/env python3
"""Offline consistency check of manifests/integrator.json (issue #84).

Checks that the consumer-facing integrator view agrees with the artifacts it
mirrors: the netlist (top cell + ordered ports), the xschem symbol (port
directions), and the committed signoff record (rung). Stdlib only: no
simulator, PDK, network, or writes.

This checks INTERFACE CONSISTENCY ONLY. It does not validate that a populated
`gds`/`area` matches real geometry, nor that the signoff tier itself is
correct; those are measured-layout / signoff questions owned by klt signoff.

    python3 manifests/check_integrator.py [--root DIR]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

VIEW = "manifests/integrator.json"
SYMBOL = "design/opamp_two_stage.sym"
KEYS = ("schema_version", "block", "top_cell", "ports", "netlist", "gds",
        "area", "rung", "rung_source")
DIRECTIONS = ("in", "out", "inout")
TIERS = ("T1", "T2", "T3", "T4")


def _load_json(root: Path, rel: str, errs: list[str]):
    p = root / rel
    if not p.is_file():
        errs.append(f"{rel}: file not found")
        return None
    try:
        return json.loads(p.read_text())
    except ValueError as e:
        errs.append(f"{rel}: invalid JSON ({e})")
        return None


def netlist_decl(text: str) -> tuple[str, list[str]] | None:
    """First `.subckt` (or xschem's commented `**.subckt`) declaration,
    joining `+` continuation lines. Returns (name, ordered ports)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(?:\*\*)?\.subckt\s+(.*)$", ln, re.I)
        if not m:
            continue
        body = m.group(1)
        for nxt in lines[i + 1:]:
            c = re.match(r"^\s*(?:\*\*)?\+\s*(.*)$", nxt)
            if not c:
                break
            body += " " + c.group(1)
        toks = [t for t in body.split() if "=" not in t]
        if toks:
            return toks[0], toks[1:]
    return None


def symbol_pins(text: str) -> list[tuple[str, str]]:
    """Ordered (name, dir) from xschem `B 5 ... {name=X dir=D pinnumber=N}`,
    sorted by pinnumber (falls back to file order)."""
    pins = []
    for idx, ln in enumerate(text.splitlines()):
        if not ln.startswith("B 5 "):
            continue
        name = re.search(r"\bname=(\S+?)[\s}]", ln)
        d = re.search(r"\bdir=(\S+?)[\s}]", ln)
        num = re.search(r"\bpinnumber=(\d+)", ln)
        if name:
            pins.append((int(num.group(1)) if num else idx, name.group(1),
                         d.group(1) if d else ""))
    pins.sort(key=lambda t: t[0])
    return [(n, d) for _, n, d in pins]


def check(root: Path) -> list[str]:
    errs: list[str] = []
    view = _load_json(root, VIEW, errs)
    if not isinstance(view, dict):
        if view is not None:
            errs.append(f"{VIEW}: must be a JSON object")
        return errs
    for k in KEYS:
        if k not in view:
            errs.append(f"{VIEW}: missing required key '{k}' (use explicit null for unavailable data)")
    if errs:
        return errs

    # --- netlist, top cell, ports
    ports = view["ports"]
    nl_rel = view["netlist"]
    decl = None
    if not isinstance(nl_rel, str) or not (root / nl_rel).is_file():
        errs.append(f"netlist: '{nl_rel}' does not exist; fix the path or restore the netlist")
    else:
        decl = netlist_decl((root / nl_rel).read_text())
        if decl is None:
            errs.append(f"netlist: no .subckt declaration found in {nl_rel}")
    if not isinstance(ports, list) or not all(
            isinstance(p, dict) and isinstance(p.get("name"), str) for p in ports):
        errs.append("ports: must be a list of {name, direction} objects")
        ports = []
    for p in ports:
        if p.get("direction") not in DIRECTIONS:
            errs.append(f"ports: '{p['name']}' direction {p.get('direction')!r} not in {DIRECTIONS}")
    names = [p["name"] for p in ports]
    if decl:
        cell, nl_ports = decl
        if view["top_cell"] != cell:
            errs.append(f"top_cell: view says '{view['top_cell']}' but {nl_rel} declares subckt '{cell}'")
        if names != nl_ports:
            errs.append(f"ports: view order {names} != netlist declaration order {nl_ports}; "
                        "regenerate the view from the netlist (names and order must match)")

    # --- directions against the xschem symbol
    sym_p = root / SYMBOL
    if not sym_p.is_file():
        errs.append(f"{SYMBOL}: file not found (needed to check port directions)")
    else:
        pins = symbol_pins(sym_p.read_text())
        sym_dir = dict(pins)
        if [n for n, _ in pins] != names:
            errs.append(f"ports: view {names} != symbol pin order {[n for n, _ in pins]} in {SYMBOL}")
        for p in ports:
            sd = sym_dir.get(p["name"])
            if sd is not None and sd != p.get("direction"):
                errs.append(f"ports: '{p['name']}' direction '{p.get('direction')}' but {SYMBOL} says '{sd}'")

    # --- rung vs signoff record
    src = view["rung_source"]
    rec = _load_json(root, src, errs) if isinstance(src, str) else None
    if not isinstance(src, str):
        errs.append("rung_source: must be a path string")
    if isinstance(rec, dict):
        tier = rec.get("tier")
        if tier is not None and tier not in TIERS:
            errs.append(f"{src}: tier {tier!r} is not null or one of {TIERS}")
        else:
            want = "below-T1" if tier is None else tier
            if view["rung"] != want:
                errs.append(f"rung: view says '{view['rung']}' but {src} tier is {tier!r} "
                            f"(expected '{want}'); update rung with the tier change")
    elif rec is not None:
        errs.append(f"{src}: must be a JSON object")

    # --- gds / area
    gds, area = view["gds"], view["area"]
    if gds is not None:
        if not isinstance(gds, str) or Path(gds).is_absolute() or ".." in Path(gds).parts \
                or not (root / gds).is_file():
            errs.append(f"gds: {gds!r} is not an existing repository-relative file; "
                        "use null until a full-block GDS is committed")
    if area is not None:
        if isinstance(area, bool) or not isinstance(area, (int, float)) \
                or not math.isfinite(area) or area <= 0:
            errs.append(f"area: {area!r} must be a finite positive number (mm^2) or null")
        elif gds is None:
            errs.append("area: populated while gds is null; both stay null until layout lands")
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args(argv)
    errs = check(args.root)
    if errs:
        for e in errs:
            print(f"integrator view: {e}", file=sys.stderr)
        print("See manifests/README.md -> 'The integrator view' for the update procedure.", file=sys.stderr)
        return 1
    print("integrator view consistent with netlist, symbol, and signoff record "
          "(interface consistency only; not layout/signoff correctness).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
