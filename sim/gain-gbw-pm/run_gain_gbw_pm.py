#!/usr/bin/env python3
"""Run the gf180mcu two-stage op-amp open-loop gain/GBW/phase-margin sweep
(issue #19 -- the repo's first spec-row testbench under `sim/`).

Runs a single open-loop AC sweep (the "big resistor" DC-feedback trick --
see `testbench/tb_gain_gbw_pm.spice`'s header) against a *provisional,
smoke-level* two-stage Miller-compensated op-amp netlist across the
ratified PVT corner grid (`typical, ff, ss, fs, sf` x -40/27/125 C, DR-0001)
and extracts three numbers per point from the single sweep:

  - Open-loop DC gain (dB) -- the swept response's peak magnitude (see
    "Design notes / limitations" in this experiment's README for why a
    peak, not a literal f->0 sample, is used).
  - GBW (Hz) -- the 0 dB magnitude crossover frequency.
  - Phase margin (deg) -- 180 - |phase| at that same crossover frequency.

**No pass/fail claim.** No schematic exists yet for this block
(spec/decision-records/0001-topology-and-cl.md is proposed, not ratified;
design/ is still a placeholder) and `spec/target-spec.md`'s phase-margin
row is `[P]` (proposed, unratified) with every other performance row
`[TBD]`. This script records measured numbers from a provisional netlist
as evidence, never as a verdict against that unratified 60 deg figure --
see build_record()'s "Claim" field.

Usage:
    python3 sim/gain-gbw-pm/run_gain_gbw_pm.py

PDK resolution (first hit wins): $GF180_PDK_PATH (a gf180mcu variant dir
containing libs.tech/), else $PDK_ROOT (+ $PDK, default gf180mcuD), else the
default volare install path ~/.volare/gf180mcuD. Reuses
`sim/gm-id-characterization/run_gmid.py`'s PDK-discovery code (copied, not
imported, to keep each experiment self-contained per that study's own
convention).
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
# PDK discovery (identical to sim/gm-id-characterization/run_gmid.py)
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
# Corner / temperature grid (DR-0001-ratified: spec/target-spec.md Sec.1)
# --------------------------------------------------------------------------

CORNERS = ["typical", "ff", "ss", "fs", "sf"]
TEMPS_C = [-40.0, 27.0, 125.0]

# AC sweep range and resolution -- wide enough to bracket GBW comfortably
# (the provisional netlist's GBW lands in the several-hundred-kHz range;
# see the record) with enough points/decade for a good crossover/phase
# interpolation.
AC_FSTART, AC_FSTOP, AC_PPD = 1.0, 1e9, 40


# --------------------------------------------------------------------------
# Deck composition / execution
# --------------------------------------------------------------------------


def compose_deck(pdk: Pdk, corner: str, temp_c: float) -> str:
    fragment = (HERE / "testbench" / "tb_gain_gbw_pm.spice").read_text()
    lines = [
        f"* gain/GBW/PM open-loop AC sweep: corner={corner} temp={temp_c:g}C",
        f".include '{pdk.design_include}'",
        f".lib '{pdk.model_lib}' {corner}",
        f".temp {temp_c:g}",
        "",
        fragment,
        "",
        ".control",
        "op",
        f"ac dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}",
        "wrdata @@DATFILE@@ vout",
        ".endc",
        ".end",
    ]
    return "\n".join(lines)


def run_corner(pdk: Pdk, corner: str, temp_c: float, workdir: Path) -> tuple[str, np.ndarray]:
    """Run one (corner, temperature) point. `workdir` is scratch space for the
    composed deck + raw `wrdata` file -- neither is committed under that
    name; the caller copies the parsed result into `corners/<record>/*`.
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


# --------------------------------------------------------------------------
# Extraction: DC gain (peak), GBW (0 dB crossover), phase margin
# --------------------------------------------------------------------------


@dataclass
class AcResult:
    freq: np.ndarray
    db: np.ndarray
    phase_deg: np.ndarray  # unwrapped
    dc_gain_db: float
    gbw_hz: float
    phase_at_gbw_deg: float
    phase_margin_deg: float
    sane: bool
    note: str


def extract(data: np.ndarray) -> AcResult:
    freq = data[:, 0]
    re = data[:, 1]
    im = data[:, 2]
    mag = np.sqrt(re**2 + im**2)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(mag)
    phase = np.degrees(np.unwrap(np.angle(re + 1j * im)))

    nan_present = bool(np.isnan(db).any() or np.isnan(phase).any() or np.isinf(db).any())
    dc_gain_db = float(np.max(db)) if not nan_present else float("nan")

    below = np.where(db < 0)[0]
    if nan_present or len(below) == 0 or below[0] == 0:
        return AcResult(
            freq=freq,
            db=db,
            phase_deg=phase,
            dc_gain_db=dc_gain_db,
            gbw_hz=float("nan"),
            phase_at_gbw_deg=float("nan"),
            phase_margin_deg=float("nan"),
            sane=False,
            note="no 0 dB crossing found in swept range, or NaN/Inf present",
        )

    i1 = int(below[0])
    i0 = i1 - 1
    # Linear interpolation in log10(f) vs dB to find the exact 0 dB point,
    # then linear interpolation of phase at that same fractional point.
    logf0, logf1 = math.log10(freq[i0]), math.log10(freq[i1])
    db0, db1 = db[i0], db[i1]
    frac = (0.0 - db0) / (db1 - db0)
    logf_gbw = logf0 + frac * (logf1 - logf0)
    gbw_hz = 10**logf_gbw
    ph0, ph1 = phase[i0], phase[i1]
    phase_at_gbw = ph0 + frac * (ph1 - ph0)

    # Normalize phase_at_gbw into (-180, 0] before computing PM -- this
    # amplifier's DC phase is ~0 deg (non-inverting input driven directly,
    # see testbench header's polarity note), so phase should already be
    # decreasing through this range without needing a +/-360 wrap.
    phase_margin = 180.0 + phase_at_gbw if phase_at_gbw < 0 else 180.0 - phase_at_gbw
    sane = 0.0 < phase_margin < 180.0 and dc_gain_db > 0
    return AcResult(
        freq=freq,
        db=db,
        phase_deg=phase,
        dc_gain_db=dc_gain_db,
        gbw_hz=gbw_hz,
        phase_at_gbw_deg=phase_at_gbw,
        phase_margin_deg=phase_margin,
        sane=sane,
        note="ok" if sane else "phase margin or DC gain outside sane range",
    )


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


def build_plot(results: dict, plot_dir: Path) -> str:
    plot_dir.mkdir(parents=True, exist_ok=True)
    corner, temp = "typical", 27.0
    r = results[(corner, temp)]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(6, 7), sharex=True)
    ax1.semilogx(r.freq, r.db)
    ax1.axhline(0, color="gray", linewidth=0.8)
    ax1.set_ylabel("|Vout/Vip| (dB)")
    ax1.set_title(f"Open-loop AC response ({corner}, {temp:g}C)")
    ax1.grid(True, which="both", alpha=0.3)
    ax2.semilogx(r.freq, r.phase_deg)
    ax2.axhline(-180, color="gray", linewidth=0.8)
    ax2.set_ylabel("Phase (deg)")
    ax2.set_xlabel("Frequency (Hz)")
    ax2.grid(True, which="both", alpha=0.3)
    if not math.isnan(r.gbw_hz):
        ax1.axvline(r.gbw_hz, color="red", linestyle="--", linewidth=0.8)
        ax2.axvline(r.gbw_hz, color="red", linestyle="--", linewidth=0.8)
    fig.tight_layout()
    out = plot_dir / "bode_typical_27c.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out.relative_to(REPO_ROOT).as_posix()


def build_record(
    record: str,
    stamp: datetime,
    pdk: Pdk,
    ngspice: str,
    results: dict[tuple[str, float], AcResult],
    plot_path: str,
) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# Record {record}")
    add("")
    add(
        "- **Record ID**: "
        + record
    )
    add(
        "- **Claim**: open-loop DC gain / GBW / phase margin of a "
        "**provisional, smoke-level** two-stage Miller-compensated op-amp "
        "netlist (DR-0001 topology: NMOS input pair, PMOS common-source "
        "output stage, no cascode, CL = 2 pF), measured via the "
        "\"big resistor\" open-loop AC testbench. **No spec pass/fail "
        "claim**: no `design/` schematic exists yet for this block "
        "(DR-0001 is proposed, not ratified) and `spec/target-spec.md`'s "
        "phase-margin row is `[P]` (proposed, unratified) with every other "
        "performance row `[TBD]` -- every number below is recorded as "
        "measured evidence pending a real sizing pass and spec "
        "ratification, not a verdict against the unratified >=60 deg "
        "figure."
    )
    add(
        "- **Bias/netlist simplification (explicit)**: the tail current and "
        "second-stage bias current are IDEAL SPICE current sources, not a "
        "self-biased mirror/reference network -- this factors out "
        "bias-generator design (a future sizing issue's job) and keeps the "
        "netlist robust across all 15 PVT points. Device widths/lengths/"
        "currents are illustrative (chosen to converge to sane small-"
        "signal behaviour), not a gm/ID-driven sizing pass -- see "
        "`testbench/tb_gain_gbw_pm.spice`'s header and this experiment's "
        "README."
    )
    add(
        "- **Extraction method**: one `.ac dec` sweep per corner/"
        "temperature "
        f"({AC_FSTART:g} Hz - {AC_FSTOP:g} Hz, {AC_PPD:g} points/decade) "
        "of Vout with the non-inverting input AC-excited and the inverting "
        "input DC-fed-back from Vout through a 1e15 ohm resistor (DC-closes "
        "the loop, AC-open). **Open-loop DC gain is reported as the "
        "swept response's peak magnitude**, not a literal f->0 sample -- "
        "this netlist's inverting-input gate capacitance is small enough "
        "(short-channel input pair) that the feedback resistor's own "
        "low-frequency artifact (an R-C zero from Rfb loading that small "
        "gate cap) sits within a few hundred Hz of DC rather than far "
        "below it; the *peak* value converges (to within ~0.3 dB scanning "
        "Rfb from 1e13-1e20 ohm) and is unaffected by Rfb's exact value, "
        "unlike GBW/phase margin extracted from the roll-off region well "
        "above the peak, which are insensitive to Rfb entirely. See "
        "`testbench/tb_gain_gbw_pm.spice`'s header for the full derivation."
    )
    add(
        "- **Netlist provenance**: hand-built provisional testbench "
        "(`sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice`) -- no "
        "`design/` schematic exists yet for this block; PDK device models "
        "instantiated directly."
    )
    add("- **Corner matrix run**:")
    add(f"  - Process: {', '.join(CORNERS)} (DR-0001-ratified MOS corner grid)")
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add(f"  - Supply: fixed at the nominal 3.3 V (no +/-10% supply sweep in this pass)")
    add(f"  - {len(CORNERS) * len(TEMPS_C)} corner points ({len(CORNERS)} process x {len(TEMPS_C)} temperature)")
    add("- **Statistical convention**: N/A -- process/temperature corner matrix only, no mismatch/Monte Carlo.")
    add("- **Result**: measured evidence pending ratification (see Claim) -- no pass/fail verdict.")
    add("")

    add("### Open-loop DC gain (dB, peak of swept response), GBW (Hz), phase margin (deg)")
    add("")
    add("| Corner | Temp (C) | DC gain (dB) | GBW (Hz) | Phase margin (deg) | Sane? |")
    add("|---|---|---|---|---|---|")
    for corner in CORNERS:
        for temp in TEMPS_C:
            r = results[(corner, temp)]
            add(
                f"| `{corner}` | {temp:g} | {r.dc_gain_db:.2f} | {r.gbw_hz:.4g} | "
                f"{r.phase_margin_deg:.2f} | {'yes' if r.sane else 'NO -- ' + r.note} |"
            )
    add("")

    all_sane = all(r.sane for r in results.values())
    add(
        f"**All {len(results)} PVT points sane (non-error, non-NaN, positive gain, "
        f"0-180 deg phase margin): {'YES' if all_sane else 'NO -- see table above'}.**"
    )
    add("")
    dc_gains = [r.dc_gain_db for r in results.values()]
    gbws = [r.gbw_hz for r in results.values()]
    pms = [r.phase_margin_deg for r in results.values()]
    add(
        f"- DC gain range across all 15 points: {min(dc_gains):.2f}-"
        f"{max(dc_gains):.2f} dB"
    )
    add(f"- GBW range across all 15 points: {min(gbws):.4g}-{max(gbws):.4g} Hz")
    add(
        f"- Phase margin range across all 15 points: {min(pms):.2f}-{max(pms):.2f} deg "
        "(for context only, against the *unratified* [P] >=60 deg target in "
        "`spec/target-spec.md` -- not a pass/fail claim)."
    )
    add("")

    add("### Bode plot (`typical` corner, 27 C)")
    add("")
    add(f"- `{plot_path}`")
    add("")

    add("- **Links**:")
    add("  - Testbench: `sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice`")
    add("  - Run script: `sim/gain-gbw-pm/run_gain_gbw_pm.py`")
    add(f"  - Netlist snapshot: `sim/gain-gbw-pm/netlist-snapshots/{record}.spice`")
    add(f"  - Raw logs: `sim/gain-gbw-pm/corners/{record}/`")
    add(f"  - PDK: {pdk.variant} (open_pdks {pdk.version}), ngspice: {ngspice}")
    add(f"- **Timestamp / author**: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #19)")
    add("- **Supersedes**: (none -- first record for this claim)")
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Fast sanity check: run only the (typical, 27C) point, print "
            "the extracted gain/GBW/PM, and exit non-zero if not sane. "
            "Does not write to corners/, netlist-snapshots/ or records/ -- "
            "for sim/selftest.sh, not for evidence collection."
        ),
    )
    args = parser.parse_args(argv)

    pdk = find_pdk()
    ngspice = ngspice_version()

    if args.smoke:
        print(f"smoke test: (typical, 27C) only, PDK={pdk.path}")
        with tempfile.TemporaryDirectory(prefix="gainpm-smoke-") as scratch:
            _log, data = run_corner(pdk, "typical", 27.0, Path(scratch))
            result = extract(data)
        print(
            f"  typical_27c: gain={result.dc_gain_db:.2f}dB "
            f"GBW={result.gbw_hz:.4g}Hz PM={result.phase_margin_deg:.2f}deg "
            f"sane={result.sane}"
        )
        if not result.sane:
            print(f"SMOKE TEST FAILED: {result.note}")
            return 1
        print("smoke test OK")
        return 0

    record, stamp = allocate_record_id(REPO_ROOT)

    corners_dir = HERE / "corners" / record
    corners_dir.mkdir(parents=True, exist_ok=True)

    print(f"record {record}: {len(CORNERS) * len(TEMPS_C)} corner points, PDK={pdk.path}")

    results: dict[tuple[str, float], AcResult] = {}
    with tempfile.TemporaryDirectory(prefix="gainpm-scratch-") as scratch:
        scratch_dir = Path(scratch)
        for corner in CORNERS:
            for temp in TEMPS_C:
                log, data = run_corner(pdk, corner, temp, scratch_dir)
                result = extract(data)
                results[(corner, temp)] = result

                corner_id = f"{corner}_{temp:g}c".replace(".", "p")
                (corners_dir / f"{corner_id}.log").write_text(log)
                np.savetxt(corners_dir / f"{corner_id}.dat", data)
                status = "ok" if result.sane else f"SANITY WARNING: {result.note}"
                print(
                    f"  {corner_id}: {status} (gain={result.dc_gain_db:.2f}dB "
                    f"GBW={result.gbw_hz:.4g}Hz PM={result.phase_margin_deg:.2f}deg)"
                )

    snapshot_dir = HERE / "netlist-snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{record}.spice").write_text(
        (HERE / "testbench" / "tb_gain_gbw_pm.spice").read_text()
    )

    plot_dir = HERE / "records" / f"{record}-plots"
    plot_path = build_plot(results, plot_dir)

    records_dir = HERE / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    record_md = build_record(record, stamp, pdk, ngspice, results, plot_path)
    out_path = records_dir / f"{record}.md"
    out_path.write_text(record_md)
    print(f"wrote {out_path}")

    if not all(r.sane for r in results.values()):
        print("WARNING: at least one PVT point was not sane -- see the record for details.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
