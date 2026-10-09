"""Shared sim-harness helpers for gf180-opamp (issue #30).

One master copy of the helpers every runner needs: gf180mcu PDK discovery
(`find_pdk`), ngspice version provenance (`ngspice_version`), append-only
record-id allocation (`allocate_record_id` / `git_short_sha`), and the
per-corner ngspice deck executor (`run_corner`). Imported by
`design/check_dc_op.py`, `sim/gm-id-characterization/run_gmid.py` and
`sim/gain-gbw-pm/run_gain_gbw_pm.py`; deck composition, extraction and
plotting stay per-experiment.

Convention history (recorded per the 2026-10-02 operator ruling on #30):
these helpers were previously copied into each runner -- "copied, not
imported", per the wording this module retired -- and the copies drifted
(`find_pdk`'s failure guidance lost the pinned PDK revision in two of the
three copies; `ngspice_version` diverged into a looser body). The ruling
reversed that convention in favor of one shared module under `sim/`, this
one. Per REUSE.md (rule 9) no fleet-level harness master exists to take by
pinned reference -- `gf180-bandgap/sim/harness` is a single-consumer
in-tree harness and no `reuse.lock.json` in the fleet pins it -- so this
repo's module is the master. A fact-check for stamped copies in the twin
repos found none to stamp: neither twin carries these helpers
(`sky130-opamp` already extracted its own stdlib-only harness,
`sim/lib/spice_harness.py`, from its two runners; `sg13g2-opamp` drives
its sweeps from shell and has no Python PDK-discovery layer), and the
PDK-discovery core here is gf180mcu/volare-specific -- per REUSE.md's
two-PDKs rule, per-PDK harness material is per-repo by design, not
duplication.

The canonical variants kept here resolve the recorded drift: the pinned
`find_pdk` failure message (carrying the cold-start `volare enable`
revision) and the tighter `ngspice_version` body.

PDK resolution order (first hit wins): $GF180_PDK_PATH (a gf180mcu
variant dir containing libs.tech/), else $PDK_ROOT (+ $PDK, default
gf180mcuD), else the default volare install path ~/.volare/gf180mcuD.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VARIANT = "gf180mcuD"
# Pinned open_pdks revision of the gf180mcu PDK (full hash). CI's
# GF180_PDK_REV must equal this; sim/ci_prereqs.py enforces it on the PDK
# find_pdk() selects.
PINNED_PDK_REV = "c6d73a35f524070e85faff4a6a9eef49553ebc2b"


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
        f"    volare enable --pdk gf180mcu {PINNED_PDK_REV}\n"
        "or point at an existing install with GF180_PDK_PATH=/path/to/gf180mcuD"
    )


def require_prereqs() -> bool:
    """True when the CI-strict switch (SIM_REQUIRE_PREREQS=1) is on.

    Local developers keep the skip-when-unavailable behavior; CI sets this so
    a missing klt / ngspice / PDK (or a missing committed dataset) fails the
    test instead of silently skipping it.
    """
    return os.environ.get("SIM_REQUIRE_PREREQS", "") not in ("", "0")


def skip_or_fail(case, reason: str) -> None:
    """Skip `case` (a unittest.TestCase or class) locally; fail under CI-strict."""
    import unittest

    if require_prereqs():
        raise AssertionError(f"required prerequisite missing (SIM_REQUIRE_PREREQS=1): {reason}")
    raise unittest.SkipTest(reason)


def ngspice_version() -> str:
    try:
        out = subprocess.run(["ngspice", "-v"], capture_output=True, text=True, check=True)
        lines = out.stdout.splitlines()
        return (lines[1] if len(lines) > 1 else lines[0]).strip().lstrip("*").strip()
    except (OSError, IndexError, subprocess.SubprocessError):
        return "unknown"


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


def run_corner(deck: str, corner: str, temp_c: float, workdir: Path) -> "tuple[str, np.ndarray]":
    """Run one (corner, temperature) point against an already-composed deck.

    `workdir` is scratch space for the composed deck + raw `wrdata` file --
    neither is committed under that name; the caller copies the parsed
    result into its `corners/<record>/` directory. The deck's
    `@@DATFILE@@` placeholder is substituted here with the scratch path.
    """
    import numpy as np  # lazy: only the ngspice-deck executor needs it

    datfile = workdir / f"{corner}_{temp_c:g}c.dat"
    deckfile = workdir / f"{corner}_{temp_c:g}c.spice"
    deckfile.write_text(deck.replace("@@DATFILE@@", str(datfile)))
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
