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

Work-dir staging (issue #94): `stage_workdir` / `DUT_INCLUDE_NAME` -- the
guard / copy `design.ngspice` / write DUT / rewrite-includes skeleton that
every runner's `materialise()` previously repeated (and on which the copies
had begun to drift).

Generic source guards (issue #105, narrowing the #94 boundary by operator
ruling): `logical_lines` (the one `+`-join / comment-drop line splitter),
`guard_testbench_core` (DUT + `design.ngspice` includes, no hand-declared
MOSFET / PDK device, exactly one `Xdut`, circuit body) and `guard_dut_core`
(the DUT is the committed export). The bench-specific checks -- Xdut port
wiring, `sw_stat_mismatch` ordering, servo / load / bias lines, and the
`.param` / `Ibias` substitution -- stay per-experiment.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import statistics
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
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    # The loaded driver's own directory is on sys.path while it executes, as it
    # would be when run as a script (issue #85: a driver importing a helper that
    # sits next to it must not depend on which directory the importer ran from).
    here = str(path.resolve().parent)
    sys.path.insert(0, here)
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    finally:
        try:
            sys.path.remove(here)
        except ValueError:
            pass
    return mod

#: Measurement-configuration fingerprint (issue #85). Version of the
#: normalisation rules below; bump it when they change so old fingerprints are
#: reported as "different rules" rather than silently compared.
FINGERPRINT_VERSION = 1


def logical_lines(text: str) -> list[str]:
    """SPICE logical lines of `text`: `+` continuations joined to their parent
    (leading whitespace ignored), every line stripped, blank lines and
    full-line `*` comments dropped. The one place a bench is split into
    lines; every source guard and `canonical_bench_lines` build on it."""
    logical: list[str] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("+") and logical:
            logical[-1] += " " + ln[1:].strip()
        else:
            logical.append(ln)
    return [ln for ln in logical if ln and not ln.startswith("*")]


def canonical_bench_lines(text: str) -> list[str]:
    """Canonical form of a SPICE bench body for freshness comparison.

    Rules (version 1): continuation lines (`+`) are joined to their parent;
    full-line `*` comments, `;`/`$` trailing comments and blank lines are
    dropped; whitespace runs collapse to one space; text is lower-cased
    (SPICE is case-insensitive); `.include`/`.inc` targets are reduced to
    their basename (the driver rewrites them to per-run absolute paths, which
    must not change the fingerprint). Component values are NOT numerically
    re-parsed: `2p` and `2e-12` fingerprint differently, deliberately.
    """
    out: list[str] = []
    for ln in logical_lines(text):
        ln = ln.split(";", 1)[0]
        ln = ln.split(" $", 1)[0]
        ln = " ".join(ln.split()).lower()
        m = re.match(r"^(\.(?:include|inc))\s+['\"]?([^'\"\s]+)['\"]?$", ln)
        if m:
            ln = f"{m.group(1)} '{m.group(2).replace(chr(92), '/').rsplit('/', 1)[-1]}'"
        if ln:
            out.append(ln)
    return out


def fingerprint_json(inputs: dict) -> str:
    """Deterministic JSON of a fingerprint-input dict (sorted keys, no spaces)."""
    return json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def measurement_fingerprint(inputs: dict) -> str:
    """sha256 over the canonical JSON of the effective measurement inputs."""
    import hashlib

    return hashlib.sha256(fingerprint_json(inputs).encode()).hexdigest()


def load_config_module(path: Path, name: str | None = None):
    """Load a stdlib `measurement_config.py` by file path (issues #85, #89).

    Cached per resolved path (not per name), so a scratch checkout used by the
    offline report never shares a module with the real repository. Compiled
    from source (no bytecode cache) so an edited module is never shadowed by a
    stale `.pyc`. `purge_config_modules()` drops the cache.
    """
    import types

    path = Path(path).resolve()
    if name is None:
        try:
            name = "_mcfg_" + re.sub(r"\W", "_", path.relative_to(REPO_ROOT).as_posix())
        except ValueError:  # a scratch checkout: key by its full path
            name = "_mcfg_" + re.sub(r"\W", "_", path.as_posix())
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    sys.modules[name] = mod
    try:
        exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def purge_config_modules() -> None:
    """Forget every config module loaded by `load_config_module`."""
    for k in [k for k in sys.modules if k.startswith("_mcfg_")]:
        del sys.modules[k]


def fingerprint_header_lines(inputs: dict, covers: str) -> list[str]:
    """The `**Measurement fingerprint**` header line a record embeds."""
    return [
        f"- **Measurement fingerprint**: version {inputs['fingerprint_version']}, sha256 "
        f"`{measurement_fingerprint(inputs)}` over the canonical inputs retained in "
        f"the 'Measurement fingerprint inputs' section ({covers}; excludes record IDs, "
        "paths and backend scheduling)",
    ]


def fingerprint_inputs_section(inputs: dict, module_rel: str) -> list[str]:
    """The `## Measurement fingerprint inputs` block a record embeds."""
    return [
        "## Measurement fingerprint inputs",
        "",
        "Canonical JSON (sorted keys, compact separators); its sha256 is the fingerprint in the header. "
        f"Recompute with `{module_rel}`.",
        "",
        "```json",
        fingerprint_json(inputs),
        "```",
        "",
    ]


DEFAULT_VARIANT = "gf180mcuD"
# Pinned open_pdks revision of the gf180mcu PDK (full hash). CI's
# GF180_PDK_REV must equal this; sim/ci_prereqs.py enforces it on the PDK
# find_pdk() selects.
PINNED_PDK_REV = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"


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

    # ciel default install layout (the pinned tree): ~/.ciel/gf180mcuD -> a
    # symlink into ~/.ciel/ciel/gf180mcu/versions/<hash>/gf180mcuD. Searched
    # before ~/.volare so a stale volare tree never shadows the pin.
    ciel_default = Path(os.path.expanduser(f"~/.ciel/{variant}"))
    if _valid(ciel_default):
        return Pdk(path=ciel_default, variant=variant, source="ciel-default")

    # Legacy volare layout (~/.volare/gf180mcuD): only reached when no ciel
    # tree exists; ci_prereqs.py then fails it unless its revision is the pin.
    volare_default = Path(os.path.expanduser(f"~/.volare/{variant}"))
    if _valid(volare_default):
        return Pdk(path=volare_default, variant=variant, source="volare-default")

    raise PdkNotFound(
        "gf180mcu PDK not found. Install with ciel 2.6.1:\n"
        "    pip install ciel==2.6.1\n"
        f"    ciel enable --pdk-family gf180mcu -l gf180mcu_fd_pr {PINNED_PDK_REV}\n"
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


def normalize_dut_text(export_text: str) -> str:
    """Normalise an xschem export text into the includable subcircuit.

    Reuses `subckt_from_export()` from `design/check_dc_op.py` unchanged, so
    the DC operating-point check, every testbench and the report's stale-DUT
    gate (issue #75) consume the export through one conversion: uncomment
    xschem's `**.subckt`/`**.ends`, drop `.end`.
    """
    design = str(REPO_ROOT / "design")
    if design not in sys.path:
        sys.path.insert(0, design)
    from check_dc_op import subckt_from_export

    return subckt_from_export(export_text)


def load_dut_text() -> str:
    """The committed export as an includable subcircuit (see normalize_dut_text)."""
    return normalize_dut_text(DUT_EXPORT.read_text())


# --------------------------------------------------------------------------
# Per-run work-directory staging
# --------------------------------------------------------------------------

#: Name every committed testbench `.include`s the DUT under; `stage_workdir`
#: writes the DUT to this file and rewrites the include to its absolute path.
DUT_INCLUDE_NAME = "opamp_two_stage.dut.spice"


# --------------------------------------------------------------------------
# Generic source guards (issue #105): the bench must stay tied to the
# committed design. Experiment-specific checks (Xdut port wiring, the
# `sw_stat_mismatch` ordering, `.param` / `Ibias` substitution) stay in each
# experiment; only the checks every bench shares live here.
# --------------------------------------------------------------------------

DEVICE_MODEL_RE = re.compile(
    r"\b(nfet|pfet|nmos|pmos|cap_mim|ppolyf|npolyf|nplus|pplus|diode|bjt)\w*", re.I
)


def guard_testbench_core(tb_text: str) -> list[str]:
    """Reasons the testbench no longer tests the committed design (empty = ok).

    The bench must (1) include the design-derived DUT file and the PDK design
    file, (2) declare no transistor or PDK device of its own, (3) instantiate
    `opamp_two_stage` as `Xdut` exactly once, and (4) be a circuit body (no
    `.lib` / `.temp` / `.control` / `.end`).
    """
    errs: list[str] = []
    code = logical_lines(tb_text)
    targets = []
    for ln in code:
        m = re.match(r"\.include\s+['\"]?([^'\"\s]+)", ln, re.I)
        if m:
            targets.append(m.group(1))
    if DUT_INCLUDE_NAME not in targets:
        errs.append(f"testbench does not `.include '{DUT_INCLUDE_NAME}'` (the design-derived DUT)")
    if "design.ngspice" not in targets:
        errs.append("testbench does not `.include 'design.ngspice'` (PDK global parameters)")
    for ln in code:
        low = ln.split(None, 1)[0].lower()
        if low.startswith("m"):
            errs.append(f"hand-declared MOSFET in the testbench: {ln[:60]}")
        elif low.startswith("x") and low != "xdut":
            errs.append(f"unexpected subcircuit/device instance in the testbench: {ln[:60]}")
        elif low == "xdut":
            if "opamp_two_stage" not in ln.split():
                errs.append("Xdut does not instantiate opamp_two_stage")
        elif DEVICE_MODEL_RE.search(ln) and not ln.startswith("."):
            errs.append(f"PDK device declared in the testbench: {ln[:60]}")
    n_dut = sum(1 for ln in code if ln.split(None, 1)[0].lower() == "xdut")
    if n_dut != 1:
        errs.append(f"expected exactly one Xdut instance, found {n_dut}")
    for ln in code:
        if re.match(r"\.(lib|temp|control|end)\b", ln, re.I):
            errs.append(f"testbench must be a circuit body; found `{ln.split()[0]}`")
    return errs


def guard_dut_core(dut_text: str, *, allow: tuple[str, ...] = ()) -> list[str]:
    """The materialised DUT must be the committed export, wrapper-normalised.

    Every non-wrapper line of the export must appear, unchanged and in order,
    in the DUT (a negative control may alter or drop devices named in
    `allow`); no device may appear twice; exactly one `.subckt
    opamp_two_stage`.
    """
    errs: list[str] = []
    export = [ln.rstrip() for ln in DUT_EXPORT.read_text().splitlines()]
    body = [
        ln for ln in export
        if not re.match(r"^\*\*\.(subckt|ends)\b", ln.strip(), re.I)
        and not re.match(r"^\.end\s*$", ln.strip(), re.I)
    ]
    got = [ln.rstrip() for ln in dut_text.splitlines()]
    pos = 0
    for ln in body:
        first = ln.split(None, 1)[0].lower() if ln.strip() else ""
        try:
            pos = got.index(ln, pos) + 1
        except ValueError:
            if first in allow:
                continue
            errs.append(f"export line missing or altered in the DUT: {ln[:70]}")
    names = [ln.split(None, 1)[0].lower() for ln in got if ln and ln[0] in "xX"]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        errs.append(f"duplicate DUT devices: {', '.join(dups)}")
    n_sub = len(re.findall(r"^\.subckt\s+opamp_two_stage\b", dut_text, re.I | re.M))
    if n_sub != 1:
        errs.append(f"expected exactly one `.subckt opamp_two_stage`, found {n_sub}")
    return errs


def stage_workdir(
    work: Path,
    pdk: Pdk,
    tb_text: str,
    *,
    guard_tb,
    guard_dut,
    dut_text: str | None = None,
    tb_label: str = "testbench",
) -> str:
    """Stage one per-run work directory; return the include-rewritten bench text.

    The skeleton every runner's `materialise()` shares (issue #94): guard the
    committed bench text with `guard_tb`, create `work`, copy the PDK's
    `design.ngspice` into it, guard the DUT (the committed export via
    `load_dut_text`, or `dut_text` for a control) with `guard_dut`, write it
    as `DUT_INCLUDE_NAME`, and rewrite the bench's two `.include` targets to
    the absolute paths in `work`. Guards are per-bench callables returning a
    list of error strings; either guard failing raises RuntimeError before
    anything further is written. The bench-specific `.param` / `Ibias`
    substitution and the `tb.spice` write stay with the caller.
    """
    errs = guard_tb(tb_text)
    if errs:
        raise RuntimeError(f"{tb_label} guard failed:\n  " + "\n  ".join(errs))
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdk.design_include, work / "design.ngspice")
    dut = load_dut_text() if dut_text is None else dut_text
    derrs = guard_dut(dut)
    if derrs:
        raise RuntimeError("DUT guard failed:\n  " + "\n  ".join(derrs))
    (work / DUT_INCLUDE_NAME).write_text(dut)
    tb = tb_text.replace("'design.ngspice'", f"'{work / 'design.ngspice'}'")
    return tb.replace(f"'{DUT_INCLUDE_NAME}'", f"'{work / DUT_INCLUDE_NAME}'")


_IBIAS_RE = re.compile(r"^Ibias\s+vdd\s+ibias\s+dc\s+\S+\s*$", re.M)
_CL_RE = re.compile(r"^CL\s+vout\s+0\s+\S+\s*$", re.M)


def set_ibias(tb: str, ibias_a: float) -> str:
    """Return `tb` with its `Ibias` source set to `ibias_a`; the line must match exactly once."""
    out, n = _IBIAS_RE.subn(f"Ibias vdd ibias dc {ibias_a:g}", tb)
    if n != 1:
        raise RuntimeError(f"testbench Ibias line matched {n} times, expected exactly once")
    return out


def set_cl(tb: str, cl_f: float) -> str:
    """Return `tb` with its `CL` load set to `cl_f`; the line must match exactly once."""
    out, n = _CL_RE.subn(f"CL vout 0 {cl_f:g}", tb)
    if n != 1:
        raise RuntimeError(f"testbench CL line matched {n} times, expected exactly once")
    return out


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


def submit_request(tag: str, req: dict, args, work: Path, *, units: int, note: str = "") -> tuple[dict, dict, float]:
    """Submit one already-built `klt sim` request: attach `batch.*`, print
    progress, run with the capacity retry. Returns `(report, request, wall_s)`."""
    req["batch"] = batch_block(args)
    print(f"  submitting `{tag}` ({units} units{note})...", flush=True)
    t0 = time.time()
    rep = run_klt_retrying(req, work / tag / "out", args.backend, work / tag,
                           retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
    wall = time.time() - t0
    print(f"  `{tag}` done in {wall:.0f} s (job {remote_of(rep).get('job_id', 'local')})", flush=True)
    return rep, req, wall


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_prefixed_scalars(log: str, prefix: str) -> dict[str, float]:
    """`<prefix>* = value` lines of an ngspice log (last occurrence wins, so the
    explicit `print` after a `meas` line is the value read)."""
    out: dict[str, float] = {}
    for name, val in re.findall(rf"^\s*({re.escape(prefix)}\w+)\s*=\s*({_NUM})\s*$", log, re.M | re.I):
        out[name.lower()] = float(val)
    return out


def describe_backend(report: dict) -> str:
    """One-line markdown description of the `klt sim` backend that produced `report`."""
    remote = remote_of(report)
    return (
        f"`klt sim` backend `{remote.get('provider', 'local')}`"
        + (f", job id `{remote.get('job_id')}`, {('Spot ' if remote.get('spot') else 'on-demand ')}{remote.get('instance_type')}, "
           f"runner klt `{remote.get('runner_klt_version')}` vs client `{remote.get('client_klt_version')}` "
           f"(compatibility `{remote.get('runner_compatibility')}`)" if remote else "")
    )


def fmt_finite(v: float, spec: str = ".2f") -> str:
    """`format(v, spec)`, or "n/a" for a non-float or non-finite value."""
    return "n/a" if not (isinstance(v, float) and math.isfinite(v)) else format(v, spec)


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


# --------------------------------------------------------------------------
# Monte Carlo statistics / extraction shared by the *-mc drivers (issue #153)
# --------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default 'linear' method)."""
    xs = sorted(values)
    if not xs:
        raise ValueError("no values")
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def skew(xs: list[float]) -> float:
    """Population skewness (0.0 for a degenerate, zero-variance sample)."""
    n = len(xs)
    m = statistics.fmean(xs)
    m2 = sum((x - m) ** 2 for x in xs) / n
    return 0.0 if m2 <= 0 else sum((x - m) ** 3 for x in xs) / n / m2**1.5


def extract_mc_samples(
    report: dict,
    want: list[tuple],
    n_expected: int,
    *,
    point_key,
    fmt_key,
    sample_cls,
    parse_scalars,
    validate_sample,
    clamp_floor,
    read_log=None,
) -> tuple[dict[tuple, list], list[str]]:
    """Samples per grid point from a klt Monte Carlo report.

    Blocking: every expected point contributes exactly `n_expected` samples
    with distinct sample indices 0..n-1, each with a retained log, every
    scalar present and every per-sample check passing. Nothing is dropped
    silently: an invalid sample is a problem string.

    The per-driver pieces are parameters: `point_key(corner) -> tuple`,
    `fmt_key(key) -> str`, `sample_cls(key, idx, seeds, vals, problems[, lb])`
    (with a `.valid` property), `parse_scalars(log) -> dict`,
    `validate_sample(vals, vcm) -> (problems, lower_bound)` and
    `clamp_floor(sample)`.
    """
    read_log = read_log or (lambda p: Path(p).read_text())
    problems: list[str] = []
    out: dict[tuple, list] = {k: [] for k in want}
    seen: dict[tuple, set] = {k: set() for k in want}
    for c in report.get("corners", []):
        try:
            k = point_key(c)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{c.get('corner_id', '?')}: unparseable corner")
            continue
        cid = str(c.get("corner_id", fmt_key(k)))
        if k not in out:
            problems.append(f"{cid}: unexpected grid point")
            continue
        mcd = c.get("monte_carlo") or {}
        idx = mcd.get("sample_index")
        if idx is None or idx in seen[k]:
            problems.append(f"{cid}: missing or duplicate sample index {idx!r}")
            continue
        seen[k].add(idx)
        logp = (c.get("artifacts") or {}).get("log")
        try:
            log = read_log(logp) if logp else None
        except OSError:
            log = None
        seeds = {s: mcd.get(s) for s in ("seed", "mismatch_seed", "process_seed")}
        if not log:
            diag = "; ".join(d.get("message", "")[:160] for d in c.get("diagnostics", []) if d.get("severity") == "error")
            s = sample_cls(k, idx, seeds, {}, [f"no ngspice log retained ({diag or c.get('status')})"])
        else:
            vals = parse_scalars(log)
            vcm = float((c.get("supply_v") or {}).get("vcm", k[2] / 2))
            probs, lb = validate_sample(vals, vcm)
            s = sample_cls(k, idx, seeds, vals, probs, lb)
            if s.valid:
                clamp_floor(s)
        if not s.valid:
            problems.append(f"{cid}: " + "; ".join(s.problems))
        out[k].append(s)
    for k in want:
        got = sorted(seen[k])
        if got != list(range(n_expected)):
            problems.append(f"{fmt_key(k)}: sample indices {got[:5]}{'...' if len(got) > 5 else ''} "
                            f"({len(got)}), expected 0..{n_expected - 1}")
    return out, problems


# --------------------------------------------------------------------------
# Single-unit bench helpers shared by the transient benches (step-response,
# overdrive-recovery): the runner binds its own bench, guards and report
# analyser with `functools.partial`; the bodies were byte-identical copies.
# --------------------------------------------------------------------------


def materialise_bench(work: Path, pdk: Pdk, *, testbench: Path, guard_tb, guard_dut,
                      ibias_a: float | None = None, cl_f: float | None = None,
                      dut_text: str | None = None) -> Path:
    """Write the per-run work directory; return the testbench path.

    The committed testbench is used verbatim except for the two `.include`
    targets (rewritten to absolute paths in `work`) and, for a control only,
    an explicit `Ibias` or `CL` override. `dut_text` replaces the committed DUT
    only to prove the stale-DUT guard (it must then be rejected).
    """
    tb = stage_workdir(work, pdk, testbench.read_text(), guard_tb=guard_tb,
                       guard_dut=guard_dut, dut_text=dut_text, tb_label=testbench.name)
    if ibias_a is not None:
        tb = set_ibias(tb, ibias_a)
    if cl_f is not None:
        tb = set_cl(tb, cl_f)
    path = work / "tb.spice"
    path.write_text(tb)
    return path


def tran_request(netlist: Path, pdk: Pdk, corners, temps, supplies, *, ac_request, mc) -> dict:
    """One 45-point (or single-point) `corners` request; `mc` supplies the analysis and `.meas` cards."""
    req = ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = mc.analysis()
    req["measurements"] = mc.measurements()
    req["options"] = {"timeout_s": 600, "keep_artifacts": True, "waveforms": True}
    return req


def save_point_data(m, path: Path, header: str) -> None:
    """Every rawfile sample of the point: `header` columns (enough for the
    runner's `rederive()` to reproduce the extraction from the committed file alone)."""
    import numpy as np  # deferred: the rest of this module is numpy-free at import time

    if m.x.size == 0:
        path.write_text(f"# INVALID: {m.reason}\n")
        return
    head = ([] if m.valid else [f"INVALID: {m.reason}"]) + [header]
    np.savetxt(path, np.column_stack([m.x, m.y, m.vin]), header="\n".join(head), fmt=("%.9e", "%.10e", "%.10e"))


def _g():
    """The gain bench driver (point key / stem / label helpers); loaded lazily
    because it imports this module."""
    return load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")


def index_corners(report: dict, want: list, problems: list[str]) -> dict:
    """klt corners keyed by point; appends duplicate / unexpected-extra-point problems."""
    g = _g()
    seen: dict = {}
    for c in report.get("corners", []):
        k = g.point_key(c)
        if k in seen:
            problems.append(f"duplicate result for {g.fmt_key(k)}")
        seen[k] = c
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"unexpected extra points: {sorted(extra)}")
    return seen


def read_raw(c: dict, k, problems: list[str]):
    """Parsed rawfile vectors of corner `c`, or None (with a problem appended)
    when the simulation failed or the rawfile is malformed."""
    g = _g()
    ssp = load_sibling("slew_swing_power_driver", "sim/slew-swing-power/run_slew_swing_power.py")
    diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
    raw = (c.get("artifacts") or {}).get("raw")
    if not raw or not Path(raw).is_file():
        problems.append(f"simulation failed for {g.fmt_key(k)}: {diag or 'no rawfile retained'}")
        return None
    try:
        return ssp.parse_ascii_real_raw(Path(raw).read_text())
    except ValueError as exc:
        problems.append(f"malformed data for {g.fmt_key(k)}: {exc}")
        return None


def persist_points(sub: Path, results: dict, arts: dict, report: dict, *, save, suffix: str) -> None:
    """One case's committed evidence: per-point logs, decks and `<stem><suffix>`
    traces (written by `save(metrics, path)`), plus the sanitised klt report."""
    g = _g()
    for k, a in arts.items():
        stem = g.point_stem(k)
        if a["log"]:
            shutil.copyfile(a["log"], sub / f"{stem}.log")
        if a["deck"]:
            shutil.copyfile(a["deck"], sub / f"{stem}.cir")
        save(results[k], sub / f"{stem}{suffix}")
    (sub / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))


def check_rederive(sub: Path, results: dict, *, rederive, same, suffix: str, label: str,
                   rbad: list[str]) -> tuple[int, int]:
    """Re-extract every persisted point of `sub` from its committed trace
    (`rederive(path, key)`) and compare with the in-memory metrics via
    `same(a, b)`; mismatch / unreadable points are appended to `rbad`
    (prefixed `label` when non-empty). Returns (n_ok, n_all)."""
    g = _g()
    pre = f"{label} " if label else ""
    n_ok = n_all = 0
    for k, m in results.items():
        n_all += 1
        try:
            m2 = rederive(sub / f"{g.point_stem(k)}{suffix}", k)
        except (ValueError, OSError) as exc:
            rbad.append(f"{pre}{g.fmt_key(k)}: {exc}")
            continue
        if same(m, m2):
            n_ok += 1
        else:
            rbad.append(f"{pre}{g.fmt_key(k)}")
    return n_ok, n_all


def copy_controls(ccdir: Path, ctrls: list, *, name=lambda c: c.name) -> None:
    """Copy each control run's log / deck into `ccdir` as `<name>.log|.cir`."""
    for c in ctrls:
        for kind, src in c.files.items():
            if src and Path(src).is_file():
                shutil.copyfile(src, ccdir / f"{name(c)}.{'log' if kind == 'log' else 'cir'}")


def run_unit(name: str, pdk: Pdk, work: Path, overrides: dict, *, nominal, materialise, make_request, analyse_report):
    """The nominal point as its own single-unit `klt sim`, LOCAL (allowed by
    the host rules; the 45-point grid never takes this path)."""
    wd = work / name
    tb = materialise(wd, pdk, **overrides)
    proc, temp, vdd = nominal
    req = make_request(tb, pdk, [proc], [temp], [vdd])
    rep = run_klt(req, wd / "out", "local", wd)
    res, arts, problems = analyse_report(rep, [nominal])
    if nominal not in res:
        raise KltError("; ".join(problems))
    return res[nominal], arts[nominal], problems


def run_controls(pdk: Pdk, work: Path, *, controls, control_cls, run_unit) -> list:
    """Run every `(name, description, overrides)` control as a single unit."""
    out = []
    for name, desc, ov in controls:
        cr = control_cls(name, desc)
        try:
            m, a, _ = run_unit(name, pdk, work, ov)
            cr.metrics, cr.files = m, {"log": a["log"], "deck": a["deck"]}
        except (KltError, KeyError, RuntimeError) as exc:
            cr.error = str(exc)[:300]
        out.append(cr)
    return out


# --------------------------------------------------------------------------
# Monte Carlo runner helpers shared by the psrr-mc / cmrr-mc / offset-mc runners
# --------------------------------------------------------------------------

#: A shard whose launch the fleet refused (shared concurrency cap or Spot
#: capacity) comes back as `lost_shard` units, not a submit error, and
#: `batch.capacity_wait_s` does not wait out the concurrency cap
#: (2AMLogic/klayout-tools#2917). Such a run is re-submitted whole (still
#: through `klt sim`, never locally), up to `--batch-submit-retries` times.
#: Substrings of a lost-shard diagnostic that mark a transient capacity refusal.
LOST_SHARD_TRANSIENT = ("already running", "BATCH_MAX_CONCURRENT_INSTANCES", "batch_no_capacity", "no capacity")


def lost_shard_refusals(report: dict) -> int:
    """Units lost because a shard's fleet launch was refused for capacity or
    the shared concurrency cap (nothing ran for them; re-submittable)."""
    n = 0
    for c in report.get("corners", []):
        for d in c.get("diagnostics", []) or []:
            msg = str(d.get("message", ""))
            if (d.get("code") == "lost_shard" or "shard lost" in msg) and any(t in msg for t in LOST_SHARD_TRANSIENT):
                n += 1
                break
    return n


def remote_jobs(report: dict) -> list[dict]:
    """The fleet job block(s) of a report: one `environment.remote` block, or
    its `fleet[]` entries for a sharded run (`None` for a lost shard)."""
    r = remote_of(report)
    if "fleet" in r:
        return [e or {} for e in r.get("fleet") or []]
    return [r] if r else []


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def request_for_snapshot(req: dict) -> dict:
    """Copy of a klt request as recorded in a netlist snapshot: basename `netlist`, no host `models.pdk_root`."""
    r = json.loads(json.dumps(req))
    r["netlist"] = Path(r["netlist"]).name
    r.get("models", {}).pop("pdk_root", None)
    return r


def write_mc_snapshot(path: Path, *, title: str, tb_label: str, tb_text: str, dut_text: str, ctl_dut: str,
                      control_mirror: tuple, reqs: dict[str, dict]) -> None:
    """Write the netlist snapshot of a CMRR/PSRR Monte Carlo record: testbench, DUT, control-line, requests."""
    L = [f"* netlist snapshot for {title}",
         f"* ---- committed testbench ({tb_label}) ----"]
    L += [f"* | {ln}" for ln in tb_text.splitlines()]
    L += [f"* ---- DUT include (wrapper-normalised export), sha256 {sha256_text(dut_text)[:16]} ----"]
    L += [f"* | {ln}" for ln in dut_text.splitlines()]
    L += [(f"* ---- negative-control DUT copy ({control_mirror[0]} {control_mirror[1]} changed), sha256 "
           f"{sha256_text(ctl_dut)[:16]}: the changed line ----")]
    L += [f"* | {ln}" for ln in ctl_dut.splitlines() if ln.lower().startswith(control_mirror[0].lower() + " ")]
    for tag, req in reqs.items():
        L += [f"* ---- klt request `{tag}` ----"] + [
            f"* | {ln}" for ln in json.dumps(request_for_snapshot(req), indent=2).splitlines()]
    path.write_text("\n".join(L) + "\n")


def exec_line(tag: str, rep: dict, wall: float) -> str:
    r = remote_of(rep)
    env = rep.get("environment") or {}
    eng_s = f"{env.get('engine', 'ngspice')} {env.get('engine_version', '')}".strip()
    if not r:
        return f"  - `{tag}`: backend `local`; engine `{eng_s}`; wall {wall:.0f} s"
    return (f"  - `{tag}`: backend `{r.get('provider')}`, job `{r.get('job_id')}`, "
            f"{'Spot ' if r.get('spot') else ''}{r.get('instance_type', '')}, state `{r.get('state')}`, "
            f"runner klt `{r.get('runner_klt_version')}` vs client `{r.get('client_klt_version')}` "
            f"(compatibility `{r.get('runner_compatibility')}`); engine `{eng_s}`; wall {wall:.0f} s")


def as_mc(rep: dict) -> dict:
    """A non-MC single-unit report, dressed as sample 0 (smoke test only)."""
    for c in rep.get("corners", []):
        if not c.get("monte_carlo"):
            c["monte_carlo"] = {"sample_index": 0}
    return rep


def fmt_digits(x: float, nd: int = 2) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:.{nd}f}"
