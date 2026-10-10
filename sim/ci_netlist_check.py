#!/usr/bin/env python3
"""Check the committed netlist still represents the xschem schematic (issue #76).

Exports design/opamp_two_stage.sch with xschem into a fresh scratch directory
(nothing is written inside the checkout), validates that the export is
complete, and compares it with design/netlist/opamp_two_stage.spice.

Documented normalization (the ONLY differences ignored):
  * the single first-line comment `** sch_path: <absolute path>` -- xschem
    stamps the checkout-specific path of the schematic there. It is dropped
    from both sides; every other line (device names, nets, models, W/L/nf/m,
    passive parameters, pin order, comments) must match byte for byte, apart
    from trailing whitespace.

An export is rejected (not silently accepted) when: xschem is missing, the
run times out or exits with anything but 0/10 (`xschem -q` exits 10 on a
successful netlist run), no non-empty .spice appears in the scratch dir,
xschem reports a missing symbol ("IS MISSING" in the netlist or "Symbol not
found" on stderr), the netlist lacks .subckt/.ends/.end, or any subcircuit
has no device lines.

    python3 sim/ci_netlist_check.py                  # check the committed pair
    python3 sim/ci_netlist_check.py --xschem PATH --sch S --netlist N

Exit status: 0 match, 1 mismatch / bad export, 2 tool or usage problem.
Regenerate with the command in design/README.md if it reports a mismatch.
"""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCH = REPO / "design" / "opamp_two_stage.sch"
NETLIST = REPO / "design" / "netlist" / "opamp_two_stage.spice"
RCFILE = REPO / "design" / "xschemrc"
OK_EXIT = (0, 10)
SCH_PATH_PREFIX = "** sch_path:"


class CheckError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def normalize(text: str) -> list[str]:
    """Drop only the `** sch_path:` comment; strip trailing whitespace."""
    return [
        ln.rstrip() for ln in text.splitlines() if not ln.startswith(SCH_PATH_PREFIX)
    ]


def validate_export(text: str, stderr: str = "") -> None:
    """Raise CheckError unless `text` is a complete, non-empty export."""
    if not text.strip():
        raise CheckError("export is empty")
    if "Symbol not found" in stderr:
        raise CheckError("xschem could not resolve a symbol:\n" + stderr.strip())
    for ln in text.splitlines():
        if "IS MISSING" in ln:
            raise CheckError(f"export has a missing model symbol: {ln.strip()}")
    lines = [ln.strip() for ln in text.splitlines()]
    # xschem comments the top-level .subckt/.ends (`**.subckt`); accept both.
    stripped = [ln.lstrip("*") if ln.startswith("**") else ln for ln in lines]
    if not any(ln.lower().startswith(".subckt") for ln in stripped):
        raise CheckError("export has no .subckt line (incomplete)")
    if not any(ln.lower().startswith(".ends") for ln in stripped):
        raise CheckError("export has no .ends line (incomplete)")
    if not any(ln.lower() == ".end" for ln in stripped):
        raise CheckError("export has no .end line (truncated)")
    # Every subcircuit (hierarchical children too) must contain a device.
    name, devices = None, 0
    for ln in stripped:
        low = ln.lower()
        if low.startswith(".subckt"):
            name, devices = ln.split()[1] if len(ln.split()) > 1 else "?", 0
        elif low.startswith(".ends"):
            if name is not None and devices == 0:
                raise CheckError(f"subcircuit {name} is empty (no devices)")
            name = None
        elif name is not None and ln and ln[0] in "XMRCLDQVIBEFGHJK":
            devices += 1


def export_schematic(xschem: str, sch: Path, outdir: Path) -> tuple[str, str]:
    exe = shutil.which(xschem) if os.sep not in xschem else (
        xschem if os.access(xschem, os.X_OK) else None
    )
    if not exe:
        raise CheckError(f"xschem not found (looked for {xschem!r})", 2)
    if not sch.is_file():
        raise CheckError(f"schematic not found: {sch}", 2)
    cmd = [exe, "-n", "-x", "-q", "--rcfile", str(RCFILE), "-o", str(outdir), str(sch)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=outdir
        )
    except subprocess.TimeoutExpired:
        raise CheckError("xschem export timed out after 120 s")
    if proc.returncode not in OK_EXIT:
        raise CheckError(
            f"xschem exited {proc.returncode} (expected 0 or 10):\n{proc.stderr.strip()}"
        )
    out = outdir / (sch.stem + ".spice")
    if not out.is_file() or out.stat().st_size == 0:
        raise CheckError(
            f"xschem exited {proc.returncode} but wrote no non-empty {out.name}:\n"
            f"{proc.stderr.strip()}"
        )
    return out.read_text(), proc.stderr


def compare(fresh: str, committed: str) -> list[str]:
    a, b = normalize(committed), normalize(fresh)
    return list(
        difflib.unified_diff(a, b, "committed netlist", "fresh xschem export", lineterm="")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--xschem", default="xschem")
    ap.add_argument("--sch", type=Path, default=SCH)
    ap.add_argument("--netlist", type=Path, default=NETLIST)
    args = ap.parse_args(argv)
    try:
        if not args.netlist.is_file():
            raise CheckError(f"committed netlist not found: {args.netlist}", 2)
        with tempfile.TemporaryDirectory(prefix="netlist-check-") as tmp:
            fresh, err = export_schematic(args.xschem, args.sch, Path(tmp))
        validate_export(fresh, err)
        validate_export(args.netlist.read_text())
        diff = compare(fresh, args.netlist.read_text())
    except CheckError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return e.code
    if diff:
        print("\n".join(diff), file=sys.stderr)
        print(
            "FAIL: committed netlist does not match the schematic export.\n"
            "Regenerate: xschem -n -x -q --rcfile design/xschemrc "
            "-o design/netlist design/opamp_two_stage.sch  (exit 10 is success)",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {args.netlist.name} matches a fresh export of {args.sch.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
