#!/usr/bin/env python3
"""Run the gf180mcu 3.3 V MOS gm/ID characterization sweep (issue #10).

Sweeps `nfet_03v3` / `pfet_03v3` at W = 10 um across five channel lengths
(0.28, 0.5, 1, 2, 4 um) and the PDK's `typical, ff, ss, fs, sf` MOS corner
grid at -40/27/125 C, extracting gm, gds, id, cgg and vth directly from the
BSIM4 operating point at every DC sweep step (no finite-difference
derivative, no separate AC run -- see `testbench/tb_gmid.spice`'s header).

From those, it derives and records gm/ID, gm/gds (intrinsic gain) and
fT = gm / (2*pi*cgg) vs Vov = Vgs - Vth(op) for every (device, length,
corner, temperature) point, per `spec/porting-plan.md` Sec.1's "gm/ID
first" mandate.

This experiment is a fresh, self-contained implementation (not vendored
from `gf180-bandgap/sim/harness`, per issue #10's own guidance) -- it
depends only on `ngspice`, `numpy`, `pandas` and `matplotlib`, all already
used elsewhere by this environment. It follows `sim/README.md`'s
append-only record convention: `<record-id> = <YYYYMMDD-HHMMSS>-<git-sha>`,
one record per run, corner logs and a netlist snapshot alongside it, never
edited in place.

Usage:
    python3 sim/gm-id-characterization/run_gmid.py

PDK resolution (first hit wins): $GF180_PDK_PATH (a gf180mcu variant dir
containing libs.tech/), else $PDK_ROOT (+ $PDK, default gf180mcuD), else the
default volare install path ~/.volare/gf180mcuD.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

# --------------------------------------------------------------------------
# PDK discovery
# --------------------------------------------------------------------------

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

    # volare default install layout: ~/.volare/gf180mcuD -> a symlink into
    # ~/.volare/volare/gf180mcu/versions/<hash>/gf180mcuD
    volare_default = Path(os.path.expanduser(f"~/.volare/{variant}"))
    if _valid(volare_default):
        return Pdk(path=volare_default, variant=variant, source="volare-default")

    raise PdkNotFound(
        "gf180mcu PDK not found. Install with volare:\n"
        "    pip install volare\n"
        "    volare enable --pdk gf180mcu <version-hash>\n"
        "or point at an existing install with GF180_PDK_PATH=/path/to/gf180mcuD"
    )


# --------------------------------------------------------------------------
# Corner / temperature / device grid
# --------------------------------------------------------------------------

# The PDK's own top-level `.LIB` corner bundles in sm141064.ngspice --
# each pulls in the matching nfet_03v3_* and pfet_03v3_* bins together.
# `fs` = fast NMOS / slow PMOS, `sf` = the reverse (verified against the
# model file's own `.LIB fs` / `.LIB sf` sections, which include
# nfet_03v3_fs+pfet_03v3_fs and nfet_03v3_sf+pfet_03v3_sf respectively).
# This is the grid `spec/target-spec.md` Sec.1 names as gf180-bandgap's
# likely template for this block's corner-grid row.
CORNERS = ["typical", "ff", "ss", "fs", "sf"]
TEMPS_C = [-40.0, 27.0, 125.0]

# Representative Vds for a two-stage amplifier's gain/input-pair devices --
# half the nominal 3.3 V supply, per this study's scope ("a representative
# Vds and the block's nominal 3.3 V supply").
VDS = 1.65
NOMINAL_VDD = 3.3

# name -> (nmos gm vector, pmos gm vector, L in um)
LENGTHS_UM = [0.28, 0.5, 1.0, 2.0, 4.0]
DEVICES = {
    "nfet_03v3": {L: f"xn{int(round(L * 100)):03d}.m0" for L in LENGTHS_UM},
    "pfet_03v3": {L: f"xp{int(round(L * 100)):03d}.m0" for L in LENGTHS_UM},
}

SWEEP_START, SWEEP_STOP, SWEEP_STEP = 0.01, 3.3, 0.01
PARAMS = ["gm", "gds", "id", "cgg", "vth"]


def save_targets() -> list[str]:
    targets = []
    for _dev, insts in DEVICES.items():
        for inst in insts.values():
            for p in PARAMS:
                targets.append(f"@m.{inst}[{p}]")
    return targets


# --------------------------------------------------------------------------
# Deck composition / execution
# --------------------------------------------------------------------------


def compose_deck(pdk: Pdk, corner: str, temp_c: float) -> str:
    fragment = (HERE / "testbench" / "tb_gmid.spice").read_text()
    targets = save_targets()
    save_line = "save all " + " ".join(targets)
    wrdata_line = "wrdata @@DATFILE@@ " + " ".join(targets)
    lines = [
        f"* gm-id characterization: corner={corner} temp={temp_c:g}C",
        f".include '{pdk.design_include}'",
        f".lib '{pdk.model_lib}' {corner}",
        f".temp {temp_c:g}",
        "",
        fragment,
        "",
        ".control",
        save_line,
        f"dc Vsweep {SWEEP_START} {SWEEP_STOP} {SWEEP_STEP}",
        wrdata_line,
        ".endc",
        ".end",
    ]
    return "\n".join(lines)


def run_corner(pdk: Pdk, corner: str, temp_c: float, workdir: Path) -> tuple[str, np.ndarray]:
    """Run one (corner, temperature) point. `workdir` is scratch space for the
    composed deck + raw `wrdata` file -- neither is committed under that name;
    the caller copies the parsed result into `corners/<record>/<corner-id>.*`.
    """
    datfile = workdir / f"{corner}_{temp_c:g}c.dat"
    deck = compose_deck(pdk, corner, temp_c).replace("@@DATFILE@@", str(datfile))
    deckfile = workdir / f"{corner}_{temp_c:g}c.spice"
    deckfile.write_text(deck)
    result = subprocess.run(
        ["ngspice", "-b", str(deckfile)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    log = result.stdout + result.stderr
    if not datfile.is_file():
        raise RuntimeError(f"ngspice produced no data for {corner}@{temp_c}C:\n{log}")
    data = np.loadtxt(datfile)
    return log, data


def parse_columns(data: np.ndarray) -> dict:
    """`wrdata` writes (x, y) pairs per requested vector, in request order."""
    targets = save_targets()
    out: dict[str, np.ndarray] = {}
    vsweep = data[:, 0]
    out["vsweep"] = vsweep
    for i, target in enumerate(targets):
        out[target] = data[:, 2 * i + 1]
    return out


# --------------------------------------------------------------------------
# Derived quantities
# --------------------------------------------------------------------------


@dataclass
class DeviceCurve:
    vsweep: np.ndarray
    vov: np.ndarray
    gm: np.ndarray
    gds: np.ndarray
    id: np.ndarray
    cgg: np.ndarray
    vth: np.ndarray
    gmid: np.ndarray
    gm_gds: np.ndarray
    ft: np.ndarray


def extract(cols: dict, dev: str, length_um: float) -> DeviceCurve:
    inst = DEVICES[dev][length_um]
    gm = cols[f"@m.{inst}[gm]"]
    gds = cols[f"@m.{inst}[gds]"]
    id_ = cols[f"@m.{inst}[id]"]
    cgg = cols[f"@m.{inst}[cgg]"]
    vth = cols[f"@m.{inst}[vth]"]
    vsweep = cols["vsweep"]
    vov = vsweep - vth
    with np.errstate(divide="ignore", invalid="ignore"):
        gmid = np.where(id_ > 0, gm / id_, np.nan)
        gm_gds = np.where(gds > 0, gm / gds, np.nan)
        ft = np.where(cgg > 0, gm / (2 * math.pi * cgg), np.nan)
    return DeviceCurve(vsweep, vov, gm, gds, id_, cgg, vth, gmid, gm_gds, ft)


def interp_at_vov(curve: DeviceCurve, field: str, vov_target: float) -> float:
    y = getattr(curve, field)
    mask = np.isfinite(y)
    x = curve.vov[mask]
    y = y[mask]
    order = np.argsort(x)
    x, y = x[order], y[order]
    if vov_target < x.min() or vov_target > x.max():
        return float("nan")
    return float(np.interp(vov_target, x, y))


# --------------------------------------------------------------------------
# Record / provenance helpers
# --------------------------------------------------------------------------


def git_short_sha(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "0000000"


def allocate_record_id(root: Path) -> tuple[str, datetime]:
    stamp = datetime.now(timezone.utc)
    sha = git_short_sha(root)
    return f"{stamp:%Y%m%d-%H%M%S}-{sha}", stamp


def ngspice_version() -> str:
    try:
        out = subprocess.run(["ngspice", "-v"], capture_output=True, text=True, check=True)
        first = out.stdout.splitlines()[1] if len(out.stdout.splitlines()) > 1 else out.stdout.splitlines()[0]
        return first.strip().lstrip("*").strip()
    except Exception:
        return "unknown"


REPRESENTATIVE_VOVS = [0.05, 0.10, 0.20, 0.30]


def build_plots(record: str, results: dict, plot_dir: Path) -> list[str]:
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    corner, temp = "typical", 27.0
    for dev in DEVICES:
        for field, ylabel, fname, logy in [
            ("gmid", "gm/ID (1/V)", "gmid", False),
            ("gm_gds", "gm/gds (intrinsic gain, V/V)", "gmgds", True),
            ("ft", "fT (Hz)", "ft", True),
        ]:
            fig, ax = plt.subplots(figsize=(5, 4))
            for length_um in LENGTHS_UM:
                curve = results[(corner, temp)][(dev, length_um)]
                mask = (curve.vov >= 0) & (curve.vov <= 1.0) & np.isfinite(getattr(curve, field))
                ax.plot(
                    curve.vov[mask],
                    getattr(curve, field)[mask],
                    label=f"L={length_um:g}um",
                )
            ax.set_xlabel("Vov = Vgs - Vth(op) (V)")
            ax.set_ylabel(ylabel)
            if logy:
                ax.set_yscale("log")
            ax.set_title(f"{dev} {ylabel} vs Vov ({corner}, {temp:g}C, Vds={VDS}V)")
            ax.legend(fontsize=7)
            ax.grid(True, which="both", alpha=0.3)
            fig.tight_layout()
            out = plot_dir / f"{dev}_{fname}_vs_vov.png"
            fig.savefig(out, dpi=120)
            plt.close(fig)
            paths.append(out.relative_to(REPO_ROOT).as_posix())
    return paths


def build_record(record: str, stamp: datetime, pdk: Pdk, ngspice: str, results: dict, plot_paths: list[str]) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# Record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    add(
        "- **Claim**: gf180mcu 3.3 V MOS gm/ID characterization (`nfet_03v3`, "
        "`pfet_03v3`) feeding `spec/target-spec.md` Sec.1's `[TBD]` corner-grid "
        "row and the future topology/sizing decision named in "
        "`spec/porting-plan.md` Sec.4. **This record makes no spec pass/fail "
        "claim**: `target-spec.md` is DRAFT and every performance row is "
        "`[TBD]` -- every number below is a measured device number, not a "
        "verdict."
    )
    add(
        "- **Extraction method**: single DC gate-voltage sweep per corner/"
        "temperature (`Vsweep` = 0.01..3.3 V, 0.01 V step) at a fixed, "
        "representative Vds = 1.65 V (half of the nominal 3.3 V supply) and "
        "Vsb = 0, reading BSIM4's own operating-point parameters "
        "(`gm`, `gds`, `id`, `cgg`, `vth`) directly off each `@m.<inst>.m0[...]` "
        "accessor at every sweep step -- no finite-difference derivative and "
        "no separate AC run. `Vov = Vgs - Vth(op)` uses the per-point `vth` "
        "BSIM4 itself reports (already DIBL/body-effect corrected), not a "
        "fixed constant-current threshold."
    )
    add(
        "- **Netlist provenance**: schematic-level device testbench "
        "(`sim/gm-id-characterization/testbench/tb_gmid.spice`) -- PDK "
        "device models instantiated directly; no `design/` schematic, no "
        "extracted layout."
    )
    add("- **Corner matrix run**:")
    add(
        f"  - Process: {', '.join(CORNERS)} (the top-level `.LIB` MOS corner "
        "bundles in `sm141064.ngspice`; `fs` = fast NMOS / slow PMOS, `sf` = "
        "the reverse, verified against the model file's own `.LIB fs`/`.LIB "
        "sf` sections)"
    )
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add(
        "  - Supply: **not applicable** -- each DUT is a two-terminal-style "
        "device sweep referred to its own source node and driven by an "
        "ideal gate source at a fixed representative Vds, per `sim/README.md`'s "
        "`nosupply` device-testbench convention (mirrors this repo's own "
        "`sim/README.md` framing and `gf180-bandgap/sim/device-mos-vth`'s "
        "identical justification). The +/-10% supply axis is a circuit-level "
        "property; it applies to future PSRR/line-regulation-style benches on "
        "an actual amplifier stage, not to a bare device characterization."
    )
    add(
        f"  - {len(CORNERS) * len(TEMPS_C)} corner points "
        f"({len(CORNERS)} process x {len(TEMPS_C)} temperature), each "
        f"covering both polarities x {len(LENGTHS_UM)} lengths "
        f"({', '.join(f'{l:g}um' for l in LENGTHS_UM)}) at W = 10 um in one "
        "ngspice run"
    )
    add(
        "- **Statistical convention**: N/A -- this record is the process/"
        "temperature corner matrix, not a mismatch/Monte Carlo distribution "
        "claim."
    )
    add("- **Result**: measured device data (no spec comparison -- see Claim).")
    add("")

    add("### gm/ID (1/V) at representative Vov, `typical` corner, 27 C")
    add("")
    add(
        "| Device | L (um) | " + " | ".join(f"Vov={v*1e3:g}mV" for v in REPRESENTATIVE_VOVS) + " |"
    )
    add("|---|---|" + "---|" * len(REPRESENTATIVE_VOVS))
    for dev in DEVICES:
        for length_um in LENGTHS_UM:
            curve = results[("typical", 27.0)][(dev, length_um)]
            cells = [
                f"{interp_at_vov(curve, 'gmid', v):.2f}" for v in REPRESENTATIVE_VOVS
            ]
            add(f"| `{dev}` | {length_um:g} | " + " | ".join(cells) + " |")
    add("")

    add("### gm/gds -- intrinsic gain (V/V) at representative Vov, `typical` corner, 27 C")
    add("")
    add(
        "| Device | L (um) | " + " | ".join(f"Vov={v*1e3:g}mV" for v in REPRESENTATIVE_VOVS) + " |"
    )
    add("|---|---|" + "---|" * len(REPRESENTATIVE_VOVS))
    for dev in DEVICES:
        for length_um in LENGTHS_UM:
            curve = results[("typical", 27.0)][(dev, length_um)]
            cells = [
                f"{interp_at_vov(curve, 'gm_gds', v):.1f}" for v in REPRESENTATIVE_VOVS
            ]
            add(f"| `{dev}` | {length_um:g} | " + " | ".join(cells) + " |")
    add("")

    add("### fT (GHz) at representative Vov, `typical` corner, 27 C")
    add("")
    add(
        "| Device | L (um) | " + " | ".join(f"Vov={v*1e3:g}mV" for v in REPRESENTATIVE_VOVS) + " |"
    )
    add("|---|---|" + "---|" * len(REPRESENTATIVE_VOVS))
    for dev in DEVICES:
        for length_um in LENGTHS_UM:
            curve = results[("typical", 27.0)][(dev, length_um)]
            cells = [
                f"{interp_at_vov(curve, 'ft', v) / 1e9:.2f}" for v in REPRESENTATIVE_VOVS
            ]
            add(f"| `{dev}` | {length_um:g} | " + " | ".join(cells) + " |")
    add("")

    add("### gm/ID process/temperature spread at Vov = 200 mV, L = 1 um")
    add("")
    add("| Device | " + " | ".join(f"{c} @ {t:g}C" for c in CORNERS for t in TEMPS_C) + " |")
    add("|---|" + "---|" * (len(CORNERS) * len(TEMPS_C)))
    for dev in DEVICES:
        cells = []
        for c in CORNERS:
            for t in TEMPS_C:
                curve = results[(c, t)][(dev, 1.0)]
                cells.append(f"{interp_at_vov(curve, 'gmid', 0.20):.2f}")
        add(f"| `{dev}` | " + " | ".join(cells) + " |")
    add("")

    add("### Plots (`typical` corner, 27 C, all swept lengths)")
    add("")
    for p in plot_paths:
        add(f"- `{p}`")
    add("")

    add("- **Links**:")
    add("  - Testbench: `sim/gm-id-characterization/testbench/tb_gmid.spice`")
    add("  - Run script: `sim/gm-id-characterization/run_gmid.py`")
    add(f"  - Netlist snapshot: `sim/gm-id-characterization/netlist-snapshots/{record}.spice`")
    add(f"  - Raw logs: `sim/gm-id-characterization/corners/{record}/`")
    add(f"  - PDK: {pdk.variant} (open_pdks {pdk.version}), ngspice: {ngspice}")
    add(f"- **Timestamp / author**: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #10)")
    add("- **Supersedes**: (none -- first record for this claim)")
    add("")
    return "\n".join(lines)


def main() -> int:
    import tempfile

    pdk = find_pdk()
    ngspice = ngspice_version()
    record, stamp = allocate_record_id(REPO_ROOT)

    corners_dir = HERE / "corners" / record
    corners_dir.mkdir(parents=True, exist_ok=True)

    print(f"record {record}: {len(CORNERS) * len(TEMPS_C)} corner points, PDK={pdk.path}")

    results: dict[tuple[str, float], dict] = {}
    with tempfile.TemporaryDirectory(prefix="gmid-scratch-") as scratch:
        scratch_dir = Path(scratch)
        for corner in CORNERS:
            for temp in TEMPS_C:
                log, data = run_corner(pdk, corner, temp, scratch_dir)
                cols = parse_columns(data)
                per_device = {}
                for dev in DEVICES:
                    for length_um in LENGTHS_UM:
                        per_device[(dev, length_um)] = extract(cols, dev, length_um)
                results[(corner, temp)] = per_device

                # Only the committed, corner-id-named log + data land under
                # corners/<record>/ -- the scratch deck/dat above are discarded.
                corner_id = f"{corner}_{temp:g}c_nosupply".replace(".", "p")
                (corners_dir / f"{corner_id}.log").write_text(log)
                np.savetxt(corners_dir / f"{corner_id}.dat", data)
                print(f"  {corner_id}: ok ({data.shape[0]} points)")

    # Netlist snapshot: the shared DUT fragment (corner/.temp selection is a
    # driver detail composed fresh per run -- see compose_deck()).
    snapshot_dir = HERE / "netlist-snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{record}.spice").write_text(
        (HERE / "testbench" / "tb_gmid.spice").read_text()
    )

    plot_dir = HERE / "records" / f"{record}-plots"
    plot_paths = build_plots(record, results, plot_dir)

    records_dir = HERE / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    record_md = build_record(record, stamp, pdk, ngspice, results, plot_paths)
    out_path = records_dir / f"{record}.md"
    out_path.write_text(record_md)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
