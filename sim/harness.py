"""Shared sim-harness helpers for gf180-opamp (issue #30).

One master copy of the helpers every runner needs: gf180mcu PDK discovery
(`find_pdk`), ngspice version provenance (`ngspice_version`), append-only
record-id allocation (`allocate_record_id` / `git_short_sha`), and the
per-corner ngspice deck executor (`run_corner`). Imported by
`design/check_dc_op.py`, `sim/gm-id-characterization/run_gmid.py` and
every `sim/<experiment>/run_*.py`; deck composition, extraction and
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

`klt sim` wrapper (issue #58): `KltError`, `run_klt` (optional `env`),
`run_klt_retrying` / `_TRANSIENT_SUBMIT`, `remote_of`, `klt_version`,
`batch_block`, `sanitise_report`, plus the record bookkeeping
`claim_record_paths` and the committed-DUT loader `load_dut_text`. These
were previously copied into, or loaded by file path from, the individual
experiment drivers; one copy here means the batch-submit retry policy
(retry the same off-host submit on a capacity refusal, never fall back to
another backend) is fixed in one place.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The committed xschem export of the DUT (every bench measures this).
DUT_EXPORT = REPO_ROOT / "design" / "netlist" / "opamp_two_stage.spice"


def load_sibling(module_name: str, relpath: str):
    """Import a sibling driver by repo-relative path (issue #68).

    Experiment directories contain hyphens, so the drivers cannot be imported
    by name. Guarded by `sys.modules`: a driver already loaded under
    `module_name` (by a test, or by another driver) is reused, so every
    importer shares ONE copy of the module.
    """
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

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


# --------------------------------------------------------------------------
# Committed DUT
# --------------------------------------------------------------------------


def load_dut_text() -> str:
    """The committed export as an includable subcircuit.

    Reuses `subckt_from_export()` from `design/check_dc_op.py` unchanged, so
    the DC operating-point check and every testbench consume the export
    through one conversion: uncomment xschem's `**.subckt`/`**.ends`, drop
    `.end`.
    """
    design = str(REPO_ROOT / "design")
    if design not in sys.path:
        sys.path.insert(0, design)
    from check_dc_op import subckt_from_export

    return subckt_from_export(DUT_EXPORT.read_text())


# --------------------------------------------------------------------------
# `klt sim` wrapper
# --------------------------------------------------------------------------


class KltError(RuntimeError):
    pass


def run_klt(request: dict, outdir: Path, backend: str | None, workdir: Path, env: dict | None = None) -> dict:
    """Run `klt sim` on a request dict and return its JSON report.

    `outdir` MUST be absolute: klt launches ngspice with the corner's artifact
    directory as cwd, so a relative `-o` makes every corner fail before it
    runs (reported as a bare measurement error). `env` (default: inherit)
    replaces the subprocess environment, e.g. a scratch HOME for local units.
    """
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    req_path = workdir / f"{outdir.name}.request.json"
    req_path.write_text(json.dumps(request, indent=2))
    cmd = ["klt", "sim", str(req_path), "-o", str(outdir), "--format", "json"]
    if backend:
        cmd += ["--backend", backend]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise KltError(
            f"klt sim produced no JSON report (exit {proc.returncode}):\n"
            f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
        ) from exc
    if "error" in report and "corners" not in report:
        raise KltError(f"klt sim error: {report['error']}")
    report["_exit_code"] = proc.returncode
    return report


_TRANSIENT_SUBMIT = ("BATCH_MAX_CONCURRENT_INSTANCES", "batch_no_capacity", "no capacity")


def run_klt_retrying(request, outdir, backend, workdir, *, retries: int, wait_s: float) -> dict:
    """`run_klt`, re-submitting when the BATCH submit was refused for fleet
    capacity or the shared concurrency cap (nothing ran, so nothing is lost).

    This only retries the same off-host submit; it never changes backend. Any
    other error, or exhausting the retries, propagates as `KltError`.
    """
    for attempt in range(retries + 1):
        try:
            return run_klt(request, outdir, backend, workdir)
        except KltError as exc:
            msg = str(exc)
            if attempt < retries and any(t in msg for t in _TRANSIENT_SUBMIT):
                print(f"  batch submit refused ({attempt + 1}/{retries + 1}); retrying in {wait_s:g}s: {msg[-160:]}", flush=True)
                time.sleep(wait_s)
                continue
            raise
    raise AssertionError("unreachable")


def remote_of(report: dict) -> dict:
    """The `environment.remote` block of a klt report (empty when local)."""
    return (report.get("environment") or {}).get("remote") or {}


def klt_version() -> str:
    try:
        return subprocess.run(["klt", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def batch_block(args) -> dict:
    """`batch.*` request fields from CLI flags (read only by the batch backend)."""
    block: dict = {}
    if args.batch_runner_version_check:
        block["runner_version_check"] = args.batch_runner_version_check
    if args.batch_capacity_wait_s is not None:
        block["capacity_wait_s"] = args.batch_capacity_wait_s
    return block


def sanitise_report(report: dict) -> dict:
    """Drop absolute host paths from a klt report before committing it."""
    rep = json.loads(json.dumps({k: v for k, v in report.items() if not k.startswith("_")}))
    for c in rep.get("corners", []):
        arts = c.get("artifacts") or {}
        for name, path in list(arts.items()):
            if path:
                arts[name] = Path(path).name
    return rep


# --------------------------------------------------------------------------
# Append-only record bookkeeping
# --------------------------------------------------------------------------


def claim_record_paths(base: Path, record: str, *, plots: bool = True) -> dict[str, Path]:
    """Output paths for one record under `base` (an experiment's directory).

    Raises FileExistsError if ANY of them already exists: records, snapshots
    and corner data are append-only evidence and a re-run mints a new id.
    `plots=False` omits the `records/<record>-plots` directory for an
    experiment that writes no plots.
    """
    paths = {"record": base / "records" / f"{record}.md"}
    if plots:
        paths["plots"] = base / "records" / f"{record}-plots"
    paths["snapshot"] = base / "netlist-snapshots" / f"{record}.spice"
    paths["corners"] = base / "corners" / record
    for p in paths.values():
        if p.exists():
            raise FileExistsError(f"{p} already exists; evidence is append-only")
    return paths
