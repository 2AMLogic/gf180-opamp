#!/usr/bin/env python3
"""Nominal DC operating-point smoke check for `design/opamp_two_stage.sch`.

    python3 design/check_dc_op.py            # PASS/FAIL, exit 0/1
    python3 design/check_dc_op.py --deck /tmp/op.spice   # also keep the deck

This is a *smoke-level* check at one corner (`typical` MOS + `res_typical` +
`mimcap_typical`, 27 C, VDD = 3.3 V), not characterization: it answers "does
the schematic as drawn bias up sensibly and converge", which is issue #17's
acceptance criterion. Full PVT-cornered characterization against
`spec/target-spec.md`'s performance rows is tracker #7 item 5 and needs a
real `sim/<experiment>/` testbench with an append-only record -- explicitly
out of scope here, which is also why this check lives in `design/` and
writes no record.

What it does:

1. Resolves the gf180mcu PDK by the same rules, in the same order, as
   `sim/gm-id-characterization/run_gmid.py` (GF180_PDK_PATH, then
   PDK_ROOT+PDK, then ~/.volare/<variant>).
2. Turns `design/netlist/opamp_two_stage.spice` -- the xschem export,
   committed exactly as xschem writes it -- into an includable subcircuit.
   xschem exports a *top-level* schematic with its own port list commented
   out (`**.subckt` / `**.ends`) and a deck-terminating `.end`; uncommenting
   those two lines and dropping `.end` is the only edit made, and it is made
   here rather than in the committed file so that file stays a pure
   regenerated artifact.
3. Wraps it in a unity-gain-buffer testbench (vinn tied to vout, vinp at
   VDD/2, CL = 2 pF per DR-0001, 10 uA into `ibias`). Closing the loop is
   what makes the DC operating point well defined: open loop, a ~97 dB
   two-stage amplifier's output node is pinned by device mismatch, not by
   design.
4. Runs `ngspice -b` and asserts: no simulator error, every MOSFET in
   saturation with margin, the bias mirror delivering its design currents,
   and the closed-loop output sitting at the input common mode.

Device sizes and the gm/ID derivation behind them: `design/opamp_sizing.md`.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
NETLIST = HERE / "netlist" / "opamp_two_stage.spice"

# ---------------------------------------------------------------------------
# Nominal operating conditions (spec/target-spec.md Sec.1; CL per DR-0001)
# ---------------------------------------------------------------------------

VDD = 3.3
VCM = VDD / 2.0
IBIAS = 10e-6
CL = 2e-12
TEMP_C = 27.0
CORNER_SECTIONS = ("typical", "res_typical", "mimcap_typical")

#: Saturation margin demanded of every MOSFET: |Vds| - |Vdsat|.
VDS_MARGIN_V = 0.05

# ---------------------------------------------------------------------------
# PDK discovery -- mirrors sim/gm-id-characterization/run_gmid.py::find_pdk
# ---------------------------------------------------------------------------

DEFAULT_VARIANT = "gf180mcuD"


class PdkNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class Pdk:
    path: Path
    variant: str
    source: str

    @property
    def design_include(self) -> Path:
        return self.path / "libs.tech" / "ngspice" / "design.ngspice"

    @property
    def model_lib(self) -> Path:
        return self.path / "libs.tech" / "ngspice" / "sm141064.ngspice"

    @property
    def version(self) -> str:
        sources = self.path / "SOURCES"
        if sources.is_file():
            for line in sources.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "open_pdks":
                    return parts[1]
        return "unknown"


def _valid(path: Path) -> bool:
    return (path / "libs.tech" / "ngspice" / "sm141064.ngspice").is_file()


def find_pdk() -> Pdk:
    direct = os.environ.get("GF180_PDK_PATH")
    if direct:
        path = Path(os.path.expanduser(direct))
        if _valid(path):
            return Pdk(path=path, variant=path.name, source="GF180_PDK_PATH")
        raise PdkNotFound(f"GF180_PDK_PATH={direct} is not a valid gf180mcu variant dir")

    variant = os.environ.get("PDK", DEFAULT_VARIANT)
    pdk_root = os.environ.get("PDK_ROOT")
    if pdk_root:
        path = Path(os.path.expanduser(pdk_root)) / variant
        if _valid(path):
            return Pdk(path=path, variant=variant, source="PDK_ROOT")

    volare_default = Path(os.path.expanduser(f"~/.volare/{variant}"))
    if _valid(volare_default):
        return Pdk(path=volare_default, variant=variant, source="volare-default")

    raise PdkNotFound(
        "gf180mcu PDK not found. Install with volare:\n"
        "    pip install volare\n"
        "    volare enable --pdk gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b\n"
        "or point at an existing install with GF180_PDK_PATH=/path/to/gf180mcuD"
    )


# ---------------------------------------------------------------------------
# The amplifier's devices, as instantiated inside the subcircuit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dev:
    inst: str  # instance name inside the subckt (lower-cased by ngspice)
    role: str
    wl: str  # W/L in um, as drawn


DEVICES = (
    Dev("xm1", "input pair (vinn)", "3.6/1"),
    Dev("xm2", "input pair (vinp)", "3.6/1"),
    Dev("xm3", "mirror load, diode", "6/1"),
    Dev("xm4", "mirror load, output", "6/1"),
    Dev("xm5", "tail current source", "6/2"),
    Dev("xm6", "stage-2 CS gain dev", "72/1"),
    Dev("xm7", "stage-2 current sink", "36/2"),
    Dev("xmb1", "bias mirror diode", "6/2"),
)

PARAMS = ("id", "vgs", "vds", "vth", "vdsat", "gm", "gds")
#: (display name, ngspice vector). `n1`/`n2`/`tail`/`nz` are internal to the
#: subcircuit, so they are reached through the `xdut.` instance path.
NODES = (
    ("vout", "v(vout)"),
    ("ibias", "v(ibias)"),
    ("n1", "v(xdut.n1)"),
    ("n2", "v(xdut.n2)"),
    ("tail", "v(xdut.tail)"),
    ("nz", "v(xdut.nz)"),
)


def subckt_from_export(text: str) -> str:
    """Uncomment xschem's top-cell wrapper and drop the deck-owning `.end`."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if re.match(r"^\*\*\.(subckt|ends)\b", stripped, re.IGNORECASE):
            out.append(stripped[2:])
            continue
        if re.match(r"^\.end\s*$", stripped, re.IGNORECASE):
            continue
        out.append(line)
    body = "\n".join(out)
    if not re.search(r"^\.subckt\s+opamp_two_stage\b", body, re.IGNORECASE | re.MULTILINE):
        raise SystemExit(
            f"{NETLIST}: no `.subckt opamp_two_stage` found after uncommenting.\n"
            "Regenerate it:\n"
            "  xschem -n -x -q --rcfile design/xschemrc -o design/netlist "
            "design/opamp_two_stage.sch"
        )
    return body


def compose_deck(pdk: Pdk) -> str:
    dut = subckt_from_export(NETLIST.read_text())
    accessors = [f"@m.xdut.{d.inst}.m0[{p}]" for d in DEVICES for p in PARAMS]
    lines = [
        "* gf180-opamp: nominal DC operating-point smoke check (design/check_dc_op.py)",
        f".include '{pdk.design_include}'",
    ]
    lines += [f".lib '{pdk.model_lib}' {section}" for section in CORNER_SECTIONS]
    lines += [
        f".temp {TEMP_C:g}",
        "",
        dut,
        "",
        "* ---- unity-gain buffer testbench (vinn tied to vout) ----",
        f"Vdd vdd 0 dc {VDD:g}",
        f"Vcm vinp 0 dc {VCM:g}",
        f"Ibias vdd ibias dc {IBIAS:g}",
        "Xdut vdd 0 vinp vout vout ibias opamp_two_stage",
        f"CL vout 0 {CL:g}",
        "",
        ".control",
        "op",
        "print " + " ".join(vec for _, vec in NODES),
        "print i(Vdd)",
    ]
    lines += [f"print {a}" for a in accessors]
    lines += [".endc", ".end", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run + parse
# ---------------------------------------------------------------------------

VALUE_RE = re.compile(r"^\s*(\S+)\s*=\s*([-+0-9.eE]+)\s*$")
ERROR_RE = re.compile(
    r"(^|\W)(error|fatal|singular matrix|no convergence|doAnalyses: TRAN|aborted)",
    re.IGNORECASE,
)


def run(deck: str, keep: Path | None) -> tuple[str, dict[str, float]]:
    with tempfile.TemporaryDirectory(prefix="opamp-dcop-") as scratch:
        deckfile = Path(scratch) / "dc_op.spice"
        deckfile.write_text(deck)
        if keep is not None:
            keep.write_text(deck)
        proc = subprocess.run(
            ["ngspice", "-b", str(deckfile)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    log = proc.stdout + proc.stderr
    values: dict[str, float] = {}
    for line in log.splitlines():
        m = VALUE_RE.match(line)
        if m:
            try:
                values[m.group(1).lower()] = float(m.group(2))
            except ValueError:
                pass
    return log, values


def ngspice_version() -> str:
    try:
        out = subprocess.run(["ngspice", "-v"], capture_output=True, text=True, check=True)
        lines = out.stdout.splitlines()
        return (lines[1] if len(lines) > 1 else lines[0]).strip().lstrip("*").strip()
    except (OSError, IndexError, subprocess.SubprocessError):
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--deck", type=Path, help="also write the composed deck here")
    args = ap.parse_args()

    if not NETLIST.is_file():
        raise SystemExit(
            f"{NETLIST} not found. Regenerate it:\n"
            "  xschem -n -x -q --rcfile design/xschemrc -o design/netlist "
            "design/opamp_two_stage.sch"
        )

    pdk = find_pdk()
    deck = compose_deck(pdk)
    log, v = run(deck, args.deck)

    print("gf180-opamp -- nominal DC operating point (design/opamp_two_stage.sch)")
    print(f"  PDK       : {pdk.variant} (open_pdks {pdk.version}, via {pdk.source})")
    print(f"  ngspice   : {ngspice_version()}")
    print(f"  corner    : {' + '.join(CORNER_SECTIONS)}, T = {TEMP_C:g} C")
    print(
        f"  bias      : VDD = {VDD:g} V, VCM = {VCM:g} V, IBIAS = {IBIAS * 1e6:g} uA, "
        f"CL = {CL * 1e12:g} pF"
    )
    print("  config    : unity-gain buffer (vinn tied to vout)")
    print()

    failures: list[str] = []
    notes: list[str] = []

    sim_errors = [ln for ln in log.splitlines() if ERROR_RE.search(ln)]
    if sim_errors:
        failures.append("ngspice reported error/convergence lines: " + "; ".join(sim_errors[:3]))

    missing = [name for name, vec in NODES if vec not in v]
    if missing:
        failures.append(f"no operating point returned for node(s): {', '.join(missing)}")
        print(log[-3000:])
        print("\nFAIL")
        return 1

    print("node voltages")
    for name, vec in NODES:
        print(f"  v({name:<6s}) = {v[vec]:8.4f} V")
    print()

    print("device operating points")
    header = (
        f"  {'dev':<5s} {'role':<22s} {'W/L':>8s} {'ID(uA)':>8s} {'|Vgs|':>7s} "
        f"{'|Vth|':>7s} {'|Vov|':>7s} {'|Vds|':>7s} {'|Vdsat|':>8s} {'gm(uS)':>8s} "
        f"{'gm/gds':>7s}  region"
    )
    print(header)
    for d in DEVICES:
        def p(name: str, inst: str = d.inst) -> float:
            return v.get(f"@m.xdut.{inst}.m0[{name}]", float("nan"))

        idv, vgs, vds = abs(p("id")), abs(p("vgs")), abs(p("vds"))
        vth, vdsat, gm, gds = abs(p("vth")), abs(p("vdsat")), abs(p("gm")), abs(p("gds"))
        vov = vgs - vth
        sat = vds >= vdsat + VDS_MARGIN_V
        on = vgs > vth
        region = "sat" if sat and on else ("triode" if on else "off")
        print(
            f"  {d.inst.upper()[1:]:<5s} {d.role:<22s} {d.wl:>8s} {idv * 1e6:8.3f} "
            f"{vgs:7.4f} {vth:7.4f} {vov:7.4f} {vds:7.4f} {vdsat:8.4f} "
            f"{gm * 1e6:8.2f} {gm / gds if gds else float('nan'):7.1f}  {region}"
        )
        if not on:
            failures.append(f"{d.inst.upper()[1:]} is off (|Vgs| {vgs:.3f} <= |Vth| {vth:.3f})")
        elif not sat:
            failures.append(
                f"{d.inst.upper()[1:]} is not saturated with margin "
                f"(|Vds| {vds:.3f} < |Vdsat| {vdsat:.3f} + {VDS_MARGIN_V:g})"
            )
    print()

    # Predicted small-signal figures from the operating point (no AC run --
    # these are the same gm/gds products design/opamp_sizing.md predicts from
    # the gm/ID record, recomputed here from the actual bias for comparison).
    def gp(inst: str, name: str) -> float:
        return abs(v.get(f"@m.xdut.{inst}.m0[{name}]", float("nan")))

    a1 = gp("xm2", "gm") / (gp("xm2", "gds") + gp("xm4", "gds"))
    a2 = gp("xm6", "gm") / (gp("xm6", "gds") + gp("xm7", "gds"))

    print("derived from this operating point (not an AC measurement)")
    print(f"  stage-1 gain gm2/(gds2+gds4) = {a1:8.1f} ({20 * math.log10(a1):.1f} dB)")
    print(f"  stage-2 gain gm6/(gds6+gds7) = {a2:8.1f} ({20 * math.log10(a2):.1f} dB)")
    print(f"  two-stage product            = {a1 * a2:8.0f} ({20 * math.log10(a1 * a2):.1f} dB)")
    print(f"  gm6/gm1 (RHP-zero ratio)     = {gp('xm6', 'gm') / gp('xm2', 'gm'):8.2f}")
    print()

    # Bias / closed-loop sanity.
    itail = abs(v.get("@m.xdut.xm5.m0[id]", float("nan")))
    iout = abs(v.get("@m.xdut.xm7.m0[id]", float("nan")))
    ivdd = abs(v.get("i(vdd)", float("nan")))
    for label, got, want, tol in (
        ("tail current", itail, 10e-6, 0.20),
        ("output-stage current", iout, 60e-6, 0.20),
    ):
        if not abs(got - want) <= tol * want:
            failures.append(
                f"{label} {got * 1e6:.2f} uA is more than {tol * 100:g}% off "
                f"its {want * 1e6:g} uA design target"
            )
    vout_err = v["v(vout)"] - VCM
    if abs(vout_err) > 0.05:
        failures.append(
            f"closed-loop output {v['v(vout)']:.4f} V is {vout_err * 1e3:+.1f} mV from "
            f"VCM {VCM:g} V (> 50 mV systematic offset)"
        )
    notes.append(f"total supply current {ivdd * 1e6:.2f} uA -> {ivdd * VDD * 1e6:.1f} uW")
    notes.append(f"closed-loop output offset {vout_err * 1e3:+.2f} mV")

    print("checks")
    print(f"  [{'ok' if not sim_errors else 'FAIL'}] ngspice reported no error/convergence lines")
    print(
        f"  [{'ok' if all('saturated' not in f and 'is off' not in f for f in failures) else 'FAIL'}]"
        " every MOSFET on and saturated with "
        f"|Vds| >= |Vdsat| + {VDS_MARGIN_V * 1e3:g} mV"
    )
    print(
        f"  [{'ok' if all('current' not in f for f in failures) else 'FAIL'}]"
        " bias mirror within 20% of its 10 uA / 60 uA design currents"
    )
    print(
        f"  [{'ok' if all('systematic offset' not in f for f in failures) else 'FAIL'}]"
        " closed-loop output within 50 mV of the input common mode"
    )
    for n in notes:
        print(f"  [--] {n}")
    print()

    if failures:
        for f in failures:
            print(f"FAILURE: {f}")
        print("\nFAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
