#!/usr/bin/env python3
"""Monte Carlo input-offset (mismatch) of the committed sized schematic across
the five MOS process corners (issue #45; tracker #7; DR-3 residual (e2)).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_offset_mc.spice`) declares no transistor.

What it runs
------------
A unity-gain follower (vinn tied to vout) with vinp at VCM = 1.65 V; the
static offset is `vout - vinp`. The mismatch Monte Carlo is ONE `klt sim`
request: `corners.process` = {typical, ff, ss, fs, sf} at 27 C / 3.30 V and
`monte_carlo = {n: 300, seed: SEED, vary: "mismatch"}` -- 5 x 300 = 1500
units. Which backend executes it is `klt`'s decision (`--backend`, the
request's `backend`, or `$KLT_SIM_BACKEND`); on a dispatch worker that is the
Spot batch fleet. This script never launches ngspice itself, never loops
ngspice over the grid, and never falls back to a local grid when a batch
submit fails -- it stops with the error and writes no record.

Per corner it reports the sample mean, sigma (n-1), 3 sigma and the
mean +/- 3 sigma extremes; the worst corner is named for sigma and for
|mean| + 3 sigma. The MEAN contains the systematic offset (sizing Sec.4
balance condition); the SPREAD is the mismatch distribution. They are kept
apart in the record: the systematic term is also measured directly with
mismatch switched off (a deterministic single unit).

Controls (small, single-purpose, in the same record):
  * switch-off: `sw_stat_mismatch=0` -- (a) one deterministic local unit, the
    systematic offset and the reference for the imbalance control; (b) a
    small `monte_carlo` request (vary "mismatch", N=CONTROL_N) with the
    switch off, which MUST give sigma = 0: the deterministic negative control
    for the mismatch path (nothing but the model switch injects variation).
  * process-only: `vary: "process"` at the typical corner -- recorded as
    measured. klt feeds ngspice ONE combined `.options seed` that changes
    with the process seed, so with this deck (RNG-drawn mismatch) it is NOT a
    sigma = 0 control; the finding is documented in the README.
  * imbalance: M1 W changed in a snapshot COPY of the DUT (the committed
    netlist is untouched), mismatch off, one deterministic local unit -- the
    mean offset must shift visibly.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/offset_samples.csv      one row per sample incl. seeds
    corners/<rid>/klt-report.json         sanitised klt report (grid)
    corners/<rid>/controls/...            control reports
    netlist-snapshots/<rid>.spice         DUT + testbench + conditions
    records/<rid>.md

PVT grid (issue #106): `--grid full` submits ONE `klt sim` request over the
45-point MOS x T x VDD grid (the gain bench's axes, VCM = VDD/2) with the
same `monte_carlo = {n: 300, seed: 45, vary: "mismatch"}` per point --
45 x 300 = 13500 units -- and writes a separate append-only record with mean,
sigma, 3 sigma and the linear 3-sigma offset |mean| + 3 sigma per grid point,
the worst point, and the worst figure next to the committed 27 C / 3.30 V
record's. The same controls run at typical / 27 C / 3.30 V. The default
(`--grid nominal`) is the issue #45 run, unchanged.

Usage:
    python3 sim/offset-mc/run_offset_mc.py                 # nominal 5-corner MC + record
    python3 sim/offset-mc/run_offset_mc.py --grid full     # 45-point PVT MC + record
    python3 sim/offset-mc/run_offset_mc.py --smoke         # one local unit, no record
    python3 sim/offset-mc/run_offset_mc.py --backend local # force a backend

No numeric offset bound is proposed or judged here: bound ratification is a
separate decision. Exit status 0 when the evidence is complete.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

from harness import (  # noqa: E402
    DUT_EXPORT,
    DUT_INCLUDE_NAME,
    KltError,
    Pdk,
    allocate_record_id,
    batch_block,
    claim_record_paths,
    find_pdk,
    klt_version,
    load_dut_text,
    ngspice_version,
    remote_of,
    run_klt,
    run_klt_retrying,
    sanitise_report,
    load_sibling,
    stage_workdir,
)

# One source for everything the fingerprint covers (issue #89).
mc = load_sibling("offset_mc_measurement_config", "sim/offset-mc/measurement_config.py")

TESTBENCH = HERE / "testbench" / "tb_offset_mc.spice"

# --------------------------------------------------------------------------
# Conditions (issue #45 scope decision: nominal T and supply only)
# --------------------------------------------------------------------------

CORNERS = mc.CORNERS
PASSIVE_SECTIONS = mc.PASSIVE_SECTIONS
TEMP_C = mc.TEMP_C
VDD_V = mc.VDD_V
VCM_V = mc.VCM_V
MODEL_LIB = mc.MODEL_LIB

#: Monte Carlo request (values live in measurement_config.py).
MC_N = mc.MC_N
MC_SEED = mc.MC_SEED
MC_VARY = mc.MC_VARY
#: Sigma of a normal sample has relative standard error ~ 1/sqrt(2(N-1)).
MIN_N = 300

#: Control sizes (kept small; values live in measurement_config.py).
CONTROL_N = mc.CONTROL_N
IMBALANCE_DEVICE = mc.IMBALANCE_DEVICE
IMBALANCE_W_FROM, IMBALANCE_W_TO = mc.IMBALANCE_W_FROM, mc.IMBALANCE_W_TO
#: The imbalance must move the mean offset by more than this to count as
#: "visible" (volts). Chosen well above numerical noise (~1e-9 V) and far
#: below the expected shift; it is a control threshold, not a spec bound.
IMBALANCE_MIN_SHIFT_V = 1e-3

#: A follower sample whose |vout - vinp| exceeds this has left the linear
#: region (output clipped / not a valid offset reading) and is INVALID.
OFFSET_VALID_ABS_V = 0.25
#: vinp must equal the commanded VCM (the vinn=vout tie is structural).
TIE_TOL_V = 1e-6
#: The offset is `vos_v`, ngspice's own `v(vout)-v(vinp)` (full precision);
#: it must agree with the difference of the two single-node measurements,
#: which ngspice prints to only 6 significant digits of ~1.65 V (+/- 5e-6 V
#: each; a 1e-5 V tolerance covers the rounding and nothing physical).
XCHK_TOL_V = 1e-5

SIM_SOURCE = mc.SIM_SOURCE
SIM_ARGS = mc.SIM_ARGS
MEASUREMENTS = mc.MEASUREMENTS

#: Grids (issue #106): `nominal` is the issue #45 population; `full` is the
#: 45-point MOS x T x VDD grid (axes from the gain bench, VCM = VDD/2).
GRIDS = ("nominal", "full")
#: The committed 27 C / 3.30 V record the full-grid record is set next to.
NOMINAL_RECORD = "20261009-072205-96bf3cc"
NOMINAL_CSV = HERE / "corners" / NOMINAL_RECORD / "offset_samples.csv"
#: Batch shards (`request.remote.hosts`, one fleet job each) for the full
#: grid. The fleet caps ONE job at 3600 s and a timed-out job returns no
#: per-unit results (2AMLogic/klayout-tools#2833): the unsharded 13500-unit
#: request ran 3688 s and was lost. The nominal 1500 units took 446 s on one
#: job (~0.30 s/unit), so 3 shards of 4500 units (~1350 s each) stay well
#: inside the cap while asking the shared fleet (capped at a few concurrent
#: instances) for few instances. klt derives every seed in the client from
#: (seed, corner index, sample index) over the WHOLE request, so sharding
#: changes no sample's value.
GRID_HOSTS = 3
#: A shard whose launch the fleet refused (shared concurrency cap or Spot
#: capacity) comes back as `lost_shard` units, not a submit error, and
#: `batch.capacity_wait_s` does not wait out the concurrency cap
#: (2AMLogic/klayout-tools#2917). Such a run is re-submitted whole (still
#: through `klt sim`, never locally), up to `--batch-submit-retries` times.
_LOST_SHARD_TRANSIENT = ("already running", "BATCH_MAX_CONCURRENT_INSTANCES", "batch_no_capacity", "no capacity")


# --------------------------------------------------------------------------
# DUT: the committed export, wrapper-normalised (`harness.load_dut_text`)
# --------------------------------------------------------------------------


def imbalance_dut(dut_text: str) -> str:
    """A COPY of the DUT text with M1's W changed (negative control). The
    committed export is never written."""
    out: list[str] = []
    hit = 0
    for line in dut_text.splitlines():
        if line.lower().startswith(IMBALANCE_DEVICE + " ") and IMBALANCE_W_FROM in line:
            line = line.replace(IMBALANCE_W_FROM, IMBALANCE_W_TO, 1)
            hit += 1
        out.append(line)
    if hit != 1:
        raise RuntimeError(f"expected exactly one {IMBALANCE_DEVICE} {IMBALANCE_W_FROM}, found {hit}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Source guards: the bench must stay tied to the committed design
# --------------------------------------------------------------------------

_DEVICE_MODEL_RE = re.compile(
    r"\b(nfet|pfet|nmos|pmos|cap_mim|ppolyf|npolyf|nplus|pplus|diode|bjt)\w*", re.I
)
_SWITCH_RE = re.compile(r"^\.param\s+sw_stat_mismatch\s*=\s*([01])[ \t]*$", re.M)


def _logical_lines(text: str) -> list[str]:
    logical: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("+") and logical:
            logical[-1] += " " + raw[1:].strip()
        else:
            logical.append(raw.strip())
    return [ln for ln in logical if ln and not ln.startswith("*")]


def guard_testbench(tb_text: str) -> list[str]:
    """Reasons the testbench no longer tests the committed design (empty = ok).

    It must (1) include the design-derived DUT and the PDK design file,
    (2) declare no transistor/PDK device, (3) instantiate `opamp_two_stage`
    exactly once as a unity follower (vinn and vout on the same net),
    (4) carry exactly one `.param sw_stat_mismatch` line, AFTER the
    design.ngspice include (otherwise the PDK default 0 would win), and
    (5) be a circuit body.
    """
    errs: list[str] = []
    code = _logical_lines(tb_text)
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
            toks = ln.split()
            if toks[-1] != "opamp_two_stage":
                errs.append("Xdut does not instantiate opamp_two_stage")
            elif len(toks) != 8:
                errs.append(f"Xdut must have exactly the six opamp ports: {ln[:70]}")
            else:
                # Xdut vdd vss vinp vinn vout ibias opamp_two_stage
                _, vdd, vss, vinp, vinn, vout, ibias, _sub = toks
                if vinn != vout:
                    errs.append("Xdut is not a unity follower (vinn is not tied to vout)")
                if vinp != "vinp" or vss != "0" or vdd != "vdd" or ibias != "ibias":
                    errs.append(f"Xdut port wiring changed: {ln[:70]}")
        elif _DEVICE_MODEL_RE.search(ln) and not ln.startswith("."):
            errs.append(f"PDK device declared in the testbench: {ln[:60]}")
    if sum(1 for ln in code if ln.split(None, 1)[0].lower() == "xdut") != 1:
        errs.append("expected exactly one Xdut instance")
    for ln in code:
        if re.match(r"\.(lib|temp|control|end)\b", ln, re.I):
            errs.append(f"testbench must be a circuit body; found `{ln.split()[0]}`")
    sw = [i for i, ln in enumerate(code) if re.match(r"\.param\s+sw_stat_mismatch\b", ln, re.I)]
    inc = [i for i, ln in enumerate(code) if re.match(r"\.include\s+['\"]?design\.ngspice", ln, re.I)]
    if len(sw) != 1:
        errs.append(f"expected exactly one `.param sw_stat_mismatch` line, found {len(sw)}")
    elif inc and sw[0] < inc[0]:
        errs.append("`.param sw_stat_mismatch` precedes the design.ngspice include (the PDK default 0 would win)")
    if any(re.match(r"\.param\s+sw_stat_global\b", ln, re.I) for ln in code):
        errs.append("testbench must not touch sw_stat_global (global spread is the corners' job)")
    return errs


def guard_dut(dut_text: str, *, allow_changed: tuple[str, ...] = ()) -> list[str]:
    """The materialised DUT must be the committed export, wrapper-normalised.

    Every non-wrapper line of the export must appear unchanged and in order,
    except lines of devices named in `allow_changed` (a negative control that
    deliberately perturbs a snapshot copy).
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
            if first in allow_changed:
                continue
            errs.append(f"export line missing or altered in the DUT: {ln[:70]}")
    names = [ln.split(None, 1)[0].lower() for ln in got if ln and ln[0] in "xX"]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        errs.append(f"duplicate DUT devices: {', '.join(dups)}")
    if len(re.findall(r"^\.subckt\s+opamp_two_stage\b", dut_text, re.I | re.M)) != 1:
        errs.append("expected exactly one `.subckt opamp_two_stage`")
    return errs


def materialise(
    work: Path, pdk: Pdk, *, dut_text: str | None = None, mismatch: int = 1,
    allow_changed: tuple[str, ...] = (),
) -> Path:
    """Write the per-run work directory and return the testbench path.

    The committed testbench is used verbatim except for the two `.include`
    targets (absolute paths in `work`) and, for the switch-off control, the
    single `sw_stat_mismatch` value.
    """
    tb = stage_workdir(
        work, pdk, TESTBENCH.read_text(), guard_tb=guard_testbench,
        guard_dut=lambda d: guard_dut(d, allow_changed=allow_changed), dut_text=dut_text,
    )
    tb, n = _SWITCH_RE.subn(f".param sw_stat_mismatch={int(mismatch)}", tb)
    if n != 1:
        raise RuntimeError("testbench `.param sw_stat_mismatch=<0|1>` line not found exactly once")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt requests (run via `harness.run_klt` / `harness.run_klt_retrying`)
# --------------------------------------------------------------------------


def process_axis(corners: list[str]) -> list[dict]:
    return [{"name": c, "sections": [c, *PASSIVE_SECTIONS]} for c in corners]


def base_request(netlist: Path, pdk: Pdk, corners: list[str]) -> dict:
    return {
        "netlist": str(netlist),
        "engine": "ngspice",
        "models": {"pdk": pdk.variant, "lib": MODEL_LIB},
        "corners": {
            "process": process_axis(list(corners)),
            "supply_v": {"vdd": [VDD_V], "vcm": [VCM_V]},
            "temperature_c": [TEMP_C],
        },
        "analysis": {"kind": "dc", "args": SIM_ARGS},
        "measurements": [dict(m) for m in MEASUREMENTS],
        "options": {"timeout_s": 300, "keep_artifacts": True},
    }


def mc_request(netlist: Path, pdk: Pdk, corners: list[str], n: int, seed: int, vary: str) -> dict:
    req = base_request(netlist, pdk, corners)
    req["monte_carlo"] = {"n": n, "seed": seed, "vary": vary}
    return req


def grid_request(netlist: Path, pdk: Pdk, corners: list[str], grid: str, n: int, seed: int, vary: str,
                 *, hosts: int | None = None) -> dict:
    """`mc_request` over a grid's T and VDD axes; vdd and vcm sweep together
    by index (VCM = VDD/2), as in the gain bench's request. `hosts` > 1
    shards the expanded unit list into that many fleet jobs (klt merges the
    shard reports back into one, in unsharded unit order)."""
    temps, supplies, vcms = mc.grid_axes(grid)
    req = mc_request(netlist, pdk, corners, n, seed, vary)
    req["corners"]["supply_v"] = {"vdd": supplies, "vcm": vcms}
    req["corners"]["temperature_c"] = temps
    if hosts and hosts > 1:
        req["remote"] = {"hosts": int(hosts)}
    return req


def lost_shard_refusals(report: dict) -> int:
    """Units lost because a shard's fleet launch was refused for capacity or
    the shared concurrency cap (nothing ran for them; re-submittable)."""
    n = 0
    for c in report.get("corners", []):
        for d in c.get("diagnostics", []) or []:
            msg = str(d.get("message", ""))
            if (d.get("code") == "lost_shard" or "shard lost" in msg) and any(t in msg for t in _LOST_SHARD_TRANSIENT):
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


# --------------------------------------------------------------------------
# Extraction + statistics (pure functions; unit-tested offline)
# --------------------------------------------------------------------------


@dataclass
class Sample:
    corner: str  # base corner (process) name
    index: int | None
    vout_v: float
    offset_v: float
    seed: int | None = None
    mismatch_seed: int | None = None
    process_seed: int | None = None


@dataclass
class Extraction:
    samples: dict[str, list[Sample]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _meas(c: dict) -> dict[str, float | None]:
    return {m["name"]: m.get("value") for m in c.get("measurements", [])}


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def extract_samples(report: dict, corners: list[str], n_expected: int | None, *, vcm_v: float = VCM_V) -> Extraction:
    """Per-corner offset samples from a klt report, with extraction validation.

    Per sample: all three measurements finite; vinp == VCM (the follower tie
    vinn == vout is structural, enforced by `guard_testbench`); the independently measured `vos_v` agrees with
    vout - vinp; |offset| inside the valid follower window. Anything else is
    a problem string -- the sample is NOT silently dropped. With
    `n_expected`, each corner must contribute exactly that many samples with
    distinct sample indices. `vcm_v` is the commanded VCM (a grid point's
    VDD/2; default the nominal 1.65 V).
    """
    ex = Extraction()
    seen: dict[str, set[int]] = {}
    for c in report.get("corners", []):
        mc = c.get("monte_carlo")
        proc = c.get("process")
        if isinstance(proc, dict):
            proc = proc.get("name")
        cid = str(c.get("corner_id", "?"))
        if proc not in corners:
            ex.problems.append(f"{cid}: unexpected process corner {proc!r}")
            continue
        idx = mc.get("sample_index") if mc else None
        v = _meas(c)
        names = ("vout_v", "vinp_v", "vos_v")
        if c.get("status") in ("error",) or not all(_finite(v.get(k)) for k in names):
            diag = "; ".join(d.get("message", "")[:120] for d in c.get("diagnostics", []) if d.get("severity") == "error")
            ex.problems.append(f"{cid}: no valid measurement ({diag or 'missing/non-finite value'})")
            continue
        vout, vinp, vos = (float(v[k]) for k in names)
        off = vos
        if abs(vinp - vcm_v) > TIE_TOL_V:
            ex.problems.append(f"{cid}: vinp = {vinp:.6g} V, expected {vcm_v} V")
            continue
        if abs(vos - (vout - vinp)) > XCHK_TOL_V:
            ex.problems.append(f"{cid}: vos_v cross-check mismatch ({vos:.9g} vs {vout - vinp:.9g})")
            continue
        if abs(off) > OFFSET_VALID_ABS_V:
            ex.problems.append(f"{cid}: |offset| = {abs(off):.3g} V outside the valid follower window")
            continue
        if n_expected is not None:
            if idx is None or idx in seen.setdefault(proc, set()):
                ex.problems.append(f"{cid}: missing or duplicate sample index")
                continue
            seen[proc].add(idx)
        ex.samples.setdefault(proc, []).append(
            Sample(
                proc, idx, vout, off,
                seed=(mc or {}).get("seed"),
                mismatch_seed=(mc or {}).get("mismatch_seed"),
                process_seed=(mc or {}).get("process_seed"),
            )
        )
    if n_expected is not None:
        for p in corners:
            got = len(ex.samples.get(p, []))
            if got != n_expected:
                ex.problems.append(f"corner {p}: {got} valid samples, expected {n_expected}")
    return ex


@dataclass
class Stats:
    n: int
    mean: float
    sigma: float
    three_sigma: float
    lo: float  # mean - 3 sigma
    hi: float  # mean + 3 sigma
    vmin: float
    vmax: float
    skew: float
    ex_kurt: float

    @property
    def worst_extreme(self) -> float:
        return abs(self.mean) + self.three_sigma


def stats_of(values: list[float]) -> Stats:
    """Mean, sample sigma (n-1), 3 sigma, mean +/- 3 sigma, min/max, skew and
    excess kurtosis (population moments) of one corner's offsets."""
    n = len(values)
    if n < 2:
        raise ValueError("need at least two samples for a sigma")
    if not all(_finite(x) for x in values):
        raise ValueError("non-finite offset value")
    mean = statistics.fmean(values)
    sigma = statistics.stdev(values)
    m2 = sum((x - mean) ** 2 for x in values) / n
    if m2 > 0:
        skew = sum((x - mean) ** 3 for x in values) / n / m2**1.5
        kurt = sum((x - mean) ** 4 for x in values) / n / m2**2 - 3.0
    else:
        skew = kurt = 0.0
    return Stats(n, mean, sigma, 3 * sigma, mean - 3 * sigma, mean + 3 * sigma, min(values), max(values), skew, kurt)


def per_corner_stats(samples: dict[str, list[Sample]], corners: list[str]) -> dict[str, Stats]:
    return {p: stats_of([s.offset_v for s in samples[p]]) for p in corners if p in samples}


def worst_corners(stats: dict[str, Stats]) -> dict[str, str]:
    """Worst corner by sigma and by |mean| + 3 sigma (ties: first in order)."""
    if not stats:
        raise ValueError("no statistics")
    by_sigma = max(stats, key=lambda p: stats[p].sigma)
    by_ext = max(stats, key=lambda p: stats[p].worst_extreme)
    return {"sigma": by_sigma, "extreme": by_ext}


def klt_rollup(report: dict) -> dict[str, dict]:
    """klt's own per-corner Monte Carlo rollup of `vos_v`, for cross-checking
    the driver's statistics (empty when the report carries none)."""
    out: dict[str, dict] = {}
    for m in report.get("measurements", []) or []:
        if m.get("name") != "vos_v":
            continue
        mc = m.get("monte_carlo") or {}
        for b in mc.get("by_corner", []) or []:
            out[str(b.get("corner_id", ""))] = b
    return out


def rollup_crosscheck(stats: dict[str, Stats], rollup: dict[str, dict]) -> list[str]:
    """Problems where klt's rollup disagrees with ours (empty = agrees or no rollup)."""
    problems: list[str] = []
    for p, s in stats.items():
        match = [b for cid, b in rollup.items() if cid.split("/")[0] == p or p in cid]
        if not match:
            continue
        b = match[0]
        mean, sig = b.get("mean"), b.get("sigma", b.get("stddev"))
        if _finite(mean) and abs(mean - s.mean) > 1e-9:
            problems.append(f"{p}: klt rollup mean {mean:.9g} != {s.mean:.9g}")
        if _finite(sig) and abs(sig - s.sigma) > max(1e-9, 1e-6 * s.sigma):
            problems.append(f"{p}: klt rollup sigma {sig:.9g} != {s.sigma:.9g}")
    return problems


# ---- PVT grid (issue #106) ------------------------------------------------

Key = tuple  # (process, temperature_c, vdd_v)


def grid_keys(grid: str) -> list[Key]:
    """Every (process, T, VDD) point of a grid, in record order."""
    temps, supplies, _ = mc.grid_axes(grid)
    return [(p, t, v) for p in CORNERS for t in temps for v in supplies]


def fmt_key(k: Key) -> str:
    return f"{k[0]} / {k[1]:g} C / {k[2]:.2f} V"


def _point_of(c: dict) -> Key:
    proc = c["process"]
    if isinstance(proc, dict):
        proc = proc["name"]
    return (proc, float(c["temperature_c"]), float(c["supply_v"]["vdd"]))


def extract_grid(report: dict, grid: str, n_expected: int) -> tuple[dict[Key, list[Sample]], list[str]]:
    """Per-point samples of a grid report, every point validated by
    `extract_samples` with that point's own commanded VCM (= VDD/2 from the
    grid axes, NOT the value the report claims). Blocking problems: an
    unparseable or unexpected point, and every per-sample / per-point
    problem `extract_samples` raises (exactly `n_expected` distinct samples)."""
    temps, supplies, vcms = mc.grid_axes(grid)
    vcm_of = dict(zip(supplies, vcms))
    want = grid_keys(grid)
    groups: dict[Key, list[dict]] = {k: [] for k in want}
    problems: list[str] = []
    for c in report.get("corners", []):
        cid = str(c.get("corner_id", "?"))
        try:
            k = _point_of(c)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{cid}: unparseable grid point")
            continue
        if k not in groups:
            problems.append(f"{cid}: unexpected grid point {k!r}")
            continue
        groups[k].append(c)
    samples: dict[Key, list[Sample]] = {}
    for k in want:
        ex = extract_samples({"corners": groups[k]}, [k[0]], n_expected, vcm_v=vcm_of[k[2]])
        problems += [f"[{fmt_key(k)}] {p}" for p in ex.problems]
        samples[k] = ex.samples.get(k[0], [])
    return samples, problems


def grid_stats(samples: dict[Key, list[Sample]]) -> dict[Key, Stats]:
    return {k: stats_of([s.offset_v for s in ss]) for k, ss in samples.items() if len(ss) >= 2}


def worst_points(stats: dict[Key, Stats]) -> dict[str, Key]:
    """Worst point by sigma and by the linear 3-sigma offset |mean| + 3 sigma."""
    if not stats:
        raise ValueError("no statistics")
    return {"sigma": max(stats, key=lambda k: stats[k].sigma),
            "extreme": max(stats, key=lambda k: stats[k].worst_extreme)}


def grid_rollup_crosscheck(stats: dict[Key, Stats], report: dict) -> list[str]:
    """klt's per-point `vos_v` rollup vs ours. The rollup's `corner_id` is a
    sample's `corner_id` without its `/mc<i>` suffix, so the point each
    rollup entry belongs to is taken from the report's own samples."""
    base: dict[str, Key] = {}
    for c in report.get("corners", []):
        cid = str(c.get("corner_id", ""))
        try:
            base[re.sub(r"/mc\d+$", "", cid)] = _point_of(c)
        except (KeyError, TypeError, ValueError):
            continue
    problems: list[str] = []
    for cid, b in klt_rollup(report).items():
        k = base.get(cid)
        s = stats.get(k) if k else None
        if s is None:
            continue
        mean, sig = b.get("mean"), b.get("sigma", b.get("stddev"))
        if _finite(mean) and abs(mean - s.mean) > 1e-9:
            problems.append(f"{fmt_key(k)}: klt rollup mean {mean:.9g} != {s.mean:.9g}")
        if _finite(sig) and abs(sig - s.sigma) > max(1e-9, 1e-6 * s.sigma):
            problems.append(f"{fmt_key(k)}: klt rollup sigma {sig:.9g} != {s.sigma:.9g}")
    return problems


def nominal_reference(path: Path = NOMINAL_CSV) -> dict[str, Stats]:
    """Per-corner statistics of the committed 27 C / 3.30 V record, recomputed
    from its committed per-sample CSV (empty when the CSV is absent)."""
    if not path.is_file():
        return {}
    by: dict[str, list[float]] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            by.setdefault(row["corner"], []).append(float(row["offset_v"]))
    return {p: stats_of(v) for p, v in by.items() if len(v) >= 2}


def mismatch_activity(report: dict) -> list[dict]:
    return ((report.get("environment") or {}).get("monte_carlo") or {}).get("family_mismatch") or []


def mosfet_active(report: dict) -> bool | None:
    for f in mismatch_activity(report):
        if f.get("family") == "mosfet":
            return f.get("active")
    return None


# --------------------------------------------------------------------------
# Single-unit controls (local: a single deterministic unit is host-rule safe)
# --------------------------------------------------------------------------


@dataclass
class DetRun:
    name: str
    description: str
    offset_v: float | None
    vout_v: float | None = None
    error: str = ""
    report: dict | None = None


def run_deterministic(name: str, desc: str, pdk: Pdk, work: Path, *, mismatch: int, dut_text=None, allow_changed=()) -> DetRun:
    """One typical-corner deterministic unit (no Monte Carlo), run LOCALLY."""
    wd = work / name
    tb = materialise(wd, pdk, dut_text=dut_text, mismatch=mismatch, allow_changed=allow_changed)
    try:
        rep = run_klt(base_request(tb, pdk, ["typical"]), wd / "out", "local", wd)
    except KltError as exc:
        return DetRun(name, desc, None, error=str(exc))
    ex = extract_samples(rep, ["typical"], None)
    if ex.problems or len(ex.samples.get("typical", [])) != 1:
        return DetRun(name, desc, None, error="; ".join(ex.problems) or "no sample", report=rep)
    s = ex.samples["typical"][0]
    return DetRun(name, desc, s.offset_v, s.vout_v, report=rep)


def run_mc_control(name: str, vary: str, mismatch: int, pdk: Pdk, work: Path, args) -> tuple[Stats | None, str, dict | None]:
    """A small typical-corner `monte_carlo` request through `klt sim` (batch
    when so configured). Returns (stats, note, report); a failed submit is
    reported as NOT RUN, never re-run locally."""
    try:
        tb = materialise(work / name, pdk, mismatch=mismatch)
        req = mc_request(tb, pdk, ["typical"], CONTROL_N, MC_SEED, vary)
        req["batch"] = batch_block(args)
        rep = run_klt_retrying(req, work / name / "out", args.backend, work / name,
                               retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
    except KltError as exc:
        return None, f"NOT RUN (submit failed: {str(exc)[:200]})", None
    ex = extract_samples(rep, ["typical"], CONTROL_N)
    if ex.problems:
        return None, "not usable: " + "; ".join(ex.problems[:3]), rep
    return stats_of([s.offset_v for s in ex.samples["typical"]]), "", rep


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def mv(x: float) -> str:
    return f"{x * 1e3:+.3f}"


def control_lines(*, sw_off: DetRun, imb: DetRun, off_stats: Stats | None, off_note: str,
                  proc_stats: Stats | None, proc_note: str) -> list[str]:
    """The record's `## Controls` section (shared by the nominal and grid records)."""
    L: list[str] = []
    add = L.append
    add("## Controls")
    add("")
    add(f"- **Switch-off** (`sw_stat_mismatch = 0`, typical, deterministic): offset {mv(sw_off.offset_v) if sw_off.offset_v is not None else 'n/a'} mV. "
        "The mismatch path contributes nothing when the model switch is off.")
    if imb.offset_v is not None and sw_off.offset_v is not None:
        shift = imb.offset_v - sw_off.offset_v
        add(f"- **Imbalance** (M1 `{IMBALANCE_W_FROM}` -> `{IMBALANCE_W_TO}` in a snapshot copy of the DUT, mismatch off, typical): "
            f"offset {mv(imb.offset_v)} mV, shift {mv(shift)} mV vs the unperturbed deterministic unit "
            f"({'visible, > ' + format(IMBALANCE_MIN_SHIFT_V*1e3, 'g') + ' mV' if abs(shift) > IMBALANCE_MIN_SHIFT_V else 'NOT visible: CONTROL FAILED'}).")
    else:
        add(f"- **Imbalance**: control did not produce a value: {imb.error}")
    if off_stats is not None:
        add(f"- **Switch-off Monte Carlo** (`sw_stat_mismatch = 0`, `vary: \"mismatch\"`, typical, N={CONTROL_N}, same seed): "
            f"sigma = {off_stats.sigma*1e3:.6f} mV, min {mv(off_stats.vmin)}, max {mv(off_stats.vmax)} mV "
            f"({'sigma = 0 as required: the sampler injects no variation by itself' if off_stats.sigma <= 1e-9 else 'NONZERO: CONTROL FAILED'}).")
    else:
        add(f"- **Switch-off Monte Carlo**: {off_note}")
    add(f"- **Process-only** (`vary: \"process\"`, typical, N={CONTROL_N}, same seed): {proc_note}")
    if proc_stats is not None:
        add(f"  mean {mv(proc_stats.mean)} mV, sigma {proc_stats.sigma*1e3:.4f} mV, min {mv(proc_stats.vmin)}, max {mv(proc_stats.vmax)} mV")
    add("")
    return L


def build_record(*, record, stamp, pdk, ngspice, kver, report, stats, worst, stats_problems,
                 sw_off: DetRun, imb: DetRun, proc_stats: Stats | None, proc_note: str,
                 proc_report: dict | None, off_stats: Stats | None, off_note: str, wall_s: float, dut_sha: str, n_units: int,
                 seed_range: tuple[int, int] | None) -> str:
    L: list[str] = []
    add = L.append
    remote = remote_of(report)
    add(f"# Offset Monte Carlo record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; commit `{record.rsplit('-', 1)[-1]}`; issue #45")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include `{dut_sha[:16]}`), unchanged")
    for ln in mc.fingerprint_lines({"bench": TESTBENCH.read_text()}):
        add(ln)
    add(f"- **PDK**: {pdk.variant} (open_pdks `{pdk.version}`); tools: ngspice local `{ngspice}`, klt `{kver}`")
    if remote:
        add(f"- **Execution**: `klt sim` backend `{remote.get('provider')}`, job `{remote.get('job_id', remote.get('job'))}`, "
            f"{'Spot ' if remote.get('spot') else ''}{remote.get('instance_type', '')}; "
            f"runner klt `{remote.get('runner_klt_version')}` vs client `{remote.get('client_klt_version')}`")
    else:
        add("- **Execution**: `klt sim` local backend (no `environment.remote` in the report)")
    add(f"- **Request**: 5 MOS corners x N={MC_N} at {TEMP_C:g} C, VDD {VDD_V:.2f} V, VCM {VCM_V} V, "
        f"`monte_carlo = {{n: {MC_N}, seed: {MC_SEED}, vary: \"{MC_VARY}\"}}` = {n_units} units, "
        f"wall time {wall_s:.0f} s (client-side, submit to report)")
    if seed_range:
        add(f"- **Seeds**: base seed {MC_SEED}; klt derives every per-sample seed (rndseed / mismatch_seed / process_seed) "
            f"from it; all of them are in `corners/{record}/offset_samples.csv`")
    add(f"- **Passive sections**: every corner uses `res_typical` and `mimcap_typical` (RZ/CC passive spread is NOT sampled; "
        f"the resistor family has no mismatch in this PDK, see README)")
    act = mosfet_active(report)
    add(f"- **klt mismatch-activity report (mosfet family)**: `active = {act}`"
        + (" (klt detects device families in the top-level netlist only; the transistors live in the included DUT, "
           "so it reports `other`/unverified here -- the response is established by the measured nonzero sigma instead)"
           if act is None else ""))
    add("")
    add("## Offset statistics per corner (unity follower, `vout - vinp`)")
    add("")
    add("The mean contains the systematic offset; sigma is the mismatch spread. Values in mV.")
    add("")
    add("| corner | N | mean | sigma | 3 sigma | mean-3s | mean+3s | min | max | skew | ex.kurt |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for p in CORNERS:
        s = stats.get(p)
        if s is None:
            add(f"| {p} | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        add(f"| {p} | {s.n} | {mv(s.mean)} | {s.sigma*1e3:.3f} | {s.three_sigma*1e3:.3f} | {mv(s.lo)} | {mv(s.hi)} | "
            f"{mv(s.vmin)} | {mv(s.vmax)} | {s.skew:+.2f} | {s.ex_kurt:+.2f} |")
    add("")
    if stats:
        ws, we = worst["sigma"], worst["extreme"]
        add(f"- **Worst corner by sigma**: `{ws}` ({stats[ws].sigma*1e3:.3f} mV; 3 sigma {stats[ws].three_sigma*1e3:.3f} mV)")
        add(f"- **Worst corner by |mean| + 3 sigma**: `{we}` ({stats[we].worst_extreme*1e3:.3f} mV)")
        n = min(s.n for s in stats.values())
        add(f"- Statistical precision: sigma relative standard error ~ {100/math.sqrt(2*(n-1)):.1f} % at N={n} (normal approximation).")
    add("- No numeric offset bound is proposed or judged here; bound ratification is separate (DR-3 residual (e2)).")
    add("")
    add("## Systematic vs mismatch")
    add("")
    if sw_off.offset_v is not None and "typical" in stats:
        st = stats["typical"]
        add(f"- Systematic offset (typical, `sw_stat_mismatch = 0`, one deterministic unit): **{mv(sw_off.offset_v)} mV**.")
        add(f"- Typical-corner Monte Carlo mean: {mv(st.mean)} mV; difference from the systematic term "
            f"{mv(st.mean - sw_off.offset_v)} mV = {abs(st.mean - sw_off.offset_v)/(st.sigma/math.sqrt(st.n)):.1f} standard errors of the mean "
            f"(mismatch is not exactly linear, so a small residual is expected).")
    else:
        add(f"- Switch-off control did not produce a value: {sw_off.error}")
    add("")
    L.extend(control_lines(sw_off=sw_off, imb=imb, off_stats=off_stats, off_note=off_note,
                           proc_stats=proc_stats, proc_note=proc_note))
    add("## Extraction validation")
    add("")
    add(f"Every sample was required to have finite `vout_v`/`vinp_v`/`vos_v`; `vinp = {VCM_V}` V; the independently measured `vos_v = v(vout)-v(vinp)` to agree with "
        f"`vout_v - vinp_v` within {XCHK_TOL_V:g} V; and |offset| < {OFFSET_VALID_ABS_V} V. "
        f"Each corner had to contribute exactly N={MC_N} samples with distinct indices.")
    if stats_problems:
        add("")
        add("**Problems:**")
        for pr in stats_problems:
            add(f"- {pr}")
    else:
        add("")
        add(f"Result: all {n_units} samples valid; driver statistics agree with klt's own per-corner rollup where it reported one.")
    add("")
    add("## Files")
    add("")
    add(f"- `sim/offset-mc/corners/{record}/offset_samples.csv`, `klt-report.json`, `controls/`")
    add(f"- `sim/offset-mc/netlist-snapshots/{record}.spice`")
    add("")
    L.extend(mc.inputs_section({"bench": TESTBENCH.read_text()}))
    return "\n".join(L)


def build_grid_record(*, record, stamp, pdk, ngspice, kver, report, stats: dict, worst: dict, stats_problems,
                      sw_off: DetRun, imb: DetRun, proc_stats: Stats | None, proc_note: str,
                      off_stats: Stats | None, off_note: str, wall_s: float, dut_sha: str, n_units: int,
                      nominal_ref: dict[str, Stats]) -> str:
    """The issue #106 record: per-point statistics over the 45-point grid, the
    worst point, and the worst linear 3-sigma offset next to the committed
    27 C / 3.30 V record's figure. Proposes and judges no bound."""
    L: list[str] = []
    add = L.append
    retained = {"grid": "full"}
    temps, supplies, vcms = mc.grid_axes("full")
    jobs = remote_jobs(report)
    keys = grid_keys("full")
    add(f"# Offset Monte Carlo PVT grid record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; commit `{record.rsplit('-', 1)[-1]}`; issue #106 "
        f"(extends the issue #45 record `{NOMINAL_RECORD}` to the T/VDD grid; that record is unchanged)")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (sha256 of the wrapper-normalised include `{dut_sha[:16]}`), unchanged")
    for ln in mc.fingerprint_lines({"bench": TESTBENCH.read_text()}, retained):
        add(ln)
    add(f"- **PDK**: {pdk.variant} (open_pdks `{pdk.version}`); tools: ngspice local `{ngspice}`, klt `{kver}`")
    if jobs:
        j0 = jobs[0]
        add(f"- **Execution**: `klt sim` backend `{j0.get('provider')}`, {len(jobs)} fleet job(s) "
            f"(`remote.hosts = {len(jobs)}`, contiguous shards of the unit list merged by klt): "
            + ", ".join(f"`{j.get('job_id', j.get('job'))}`" for j in jobs)
            + f"; {'Spot ' if j0.get('spot') else ''}{j0.get('instance_type', '')}; "
            f"runner klt `{j0.get('runner_klt_version')}` vs client `{j0.get('client_klt_version')}` "
            "(`environment.remote` of the grid report)")
    else:
        add("- **Execution**: `klt sim` local backend (no `environment.remote` in the report)")
    add(f"- **Request**: ONE `klt sim` request: {len(CORNERS)} MOS corners x T {', '.join(f'{t:g} C' for t in temps)} x "
        f"VDD {', '.join(f'{v:.2f} V' for v in supplies)} (VCM = VDD/2: {', '.join(f'{v:g} V' for v in vcms)}) = "
        f"{len(keys)} points, `monte_carlo = {{n: {MC_N}, seed: {MC_SEED}, vary: \"{MC_VARY}\"}}` per point = "
        f"{n_units} units, wall time {wall_s:.0f} s (client-side, submit to report)")
    add(f"- **Seeds**: base seed {MC_SEED}; klt derives every per-sample seed (rndseed / mismatch_seed / process_seed) "
        f"from it; all of them are in `corners/{record}/offset_samples.csv`")
    add("- **Passive sections**: every point uses `res_typical` and `mimcap_typical` (RZ/CC passive spread is NOT sampled; "
        "the resistor family has no mismatch in this PDK, see README)")
    act = mosfet_active(report)
    add(f"- **klt mismatch-activity report (mosfet family)**: `active = {act}`"
        + (" (klt scans the top-level netlist only; the response is established by the measured nonzero sigma at every point)"
           if act is None else ""))
    add("")
    add("## Worst point and the 27 C / 3.30 V figure")
    add("")
    add("The linear 3-sigma offset is |mean| + 3 sigma (systematic plus mismatch, added linearly). Values in mV.")
    add("")
    if stats:
        we, ws = worst["extreme"], worst["sigma"]
        add(f"- **Worst linear 3-sigma offset over the {len(keys)}-point grid**: **{stats[we].worst_extreme*1e3:.3f} mV** at "
            f"`{fmt_key(we)}` (mean {mv(stats[we].mean)}, sigma {stats[we].sigma*1e3:.3f}, 3 sigma {stats[we].three_sigma*1e3:.3f})")
        add(f"- **Worst sigma over the grid**: {stats[ws].sigma*1e3:.3f} mV (3 sigma {stats[ws].three_sigma*1e3:.3f}) at `{fmt_key(ws)}`")
        if nominal_ref:
            nw = max(nominal_ref, key=lambda p: nominal_ref[p].worst_extreme)
            add(f"- **27 C / 3.30 V figure** (record `{NOMINAL_RECORD}`, recomputed from its committed samples): "
                f"{nominal_ref[nw].worst_extreme*1e3:.3f} mV at `{nw}`; the grid's worst is "
                f"{(stats[we].worst_extreme - nominal_ref[nw].worst_extreme)*1e3:+.3f} mV from it "
                f"(x{stats[we].worst_extreme / nominal_ref[nw].worst_extreme:.3f})")
        else:
            add(f"- **27 C / 3.30 V figure**: the committed samples of record `{NOMINAL_RECORD}` were not found")
        nom = {k: st for k, st in stats.items() if k[1] == 27.0 and abs(k[2] - 3.30) < 1e-9}
        if nom:
            kn = max(nom, key=lambda k: nom[k].worst_extreme)
            add(f"- This grid's own 27 C / 3.30 V slice: worst {nom[kn].worst_extreme*1e3:.3f} mV at `{kn[0]}`")
        n = min(st.n for st in stats.values())
        add(f"- Statistical precision: sigma relative standard error ~ {100/math.sqrt(2*(n-1)):.1f} % at N={n} per point "
            "(normal approximation); the worst of 45 points is a maximum over noisy estimates, so it is biased high by "
            "a few standard errors.")
    add("- **No numeric offset bound is proposed, ratified or judged here.** The bound is spec issue #62's decision "
        "(DR-3 residual (e2)); this record only supplies the T/VDD-grid statistic it asked for.")
    add("")
    add("## Linear 3-sigma offset by temperature and supply (worst MOS corner)")
    add("")
    add("| T \\ VDD | " + " | ".join(f"{v:.2f} V" for v in supplies) + " |")
    add("|---|" + "---|" * len(supplies))
    for t in temps:
        cells = []
        for v in supplies:
            pts = {k: stats[k] for k in keys if k[1] == t and k[2] == v and k in stats}
            if pts:
                kk = max(pts, key=lambda k: pts[k].worst_extreme)
                cells.append(f"{pts[kk].worst_extreme*1e3:.3f} ({kk[0]})")
            else:
                cells.append("-")
        add(f"| {t:g} C | " + " | ".join(cells) + " |")
    add("")
    add("## Offset statistics per grid point (unity follower, `vout - vinp`)")
    add("")
    add("The mean contains the systematic offset; sigma is the mismatch spread. Values in mV.")
    add("")
    add("| corner | T (C) | VDD (V) | N | mean | sigma | 3 sigma | \\|mean\\|+3s | min | max | skew | ex.kurt |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        st = stats.get(k)
        if st is None:
            add(f"| {k[0]} | {k[1]:g} | {k[2]:.2f} | 0 | - | - | - | - | - | - | - | - |")
            continue
        add(f"| {k[0]} | {k[1]:g} | {k[2]:.2f} | {st.n} | {mv(st.mean)} | {st.sigma*1e3:.3f} | {st.three_sigma*1e3:.3f} | "
            f"{st.worst_extreme*1e3:.3f} | {mv(st.vmin)} | {mv(st.vmax)} | {st.skew:+.2f} | {st.ex_kurt:+.2f} |")
    add("")
    add("## Systematic vs mismatch")
    add("")
    kt = ("typical", 27.0, 3.30)
    if sw_off.offset_v is not None and kt in stats:
        st = stats[kt]
        add(f"- Systematic offset (typical / 27 C / 3.30 V, `sw_stat_mismatch = 0`, one deterministic unit): **{mv(sw_off.offset_v)} mV**.")
        add(f"- Grid typical / 27 C / 3.30 V Monte Carlo mean: {mv(st.mean)} mV; difference from the systematic term "
            f"{mv(st.mean - sw_off.offset_v)} mV = {abs(st.mean - sw_off.offset_v)/(st.sigma/math.sqrt(st.n)):.1f} standard errors of the mean.")
    else:
        add(f"- Switch-off control did not produce a value: {sw_off.error}")
    add("")
    L.extend(control_lines(sw_off=sw_off, imb=imb, off_stats=off_stats, off_note=off_note,
                           proc_stats=proc_stats, proc_note=proc_note))
    add("The controls run at typical / 27 C / 3.30 V (the nominal point); the grid itself is the 45-point request above.")
    add("")
    add("## Extraction validation")
    add("")
    add(f"Every sample was required to have finite `vout_v`/`vinp_v`/`vos_v`; `vinp` = the point's commanded VCM (VDD/2 "
        f"from the grid axes, within {TIE_TOL_V:g} V -- this also proves klt applied the supply/VCM alters); "
        f"`vos_v = v(vout)-v(vinp)` to agree with `vout_v - vinp_v` within {XCHK_TOL_V:g} V; and |offset| < "
        f"{OFFSET_VALID_ABS_V} V. Every one of the {len(keys)} points had to contribute exactly N={MC_N} samples with "
        "distinct indices, and a nonzero sigma.")
    add("")
    if stats_problems:
        add("**Problems:**")
        for pr in stats_problems:
            add(f"- {pr}")
    else:
        add(f"Result: all {n_units} samples valid; driver statistics agree with klt's own per-point rollup where it reported one.")
    add("")
    add("## Reproduce")
    add("")
    add("```")
    add("python3 sim/offset-mc/run_offset_mc.py --grid full --batch-runner-version-check warn --batch-submit-retries 10 "
        "--batch-capacity-wait-s 1800")
    add("```")
    add("")
    add("## Files")
    add("")
    add(f"- `sim/offset-mc/corners/{record}/offset_samples.csv`, `klt-report.json`, `controls/`")
    add(f"- `sim/offset-mc/netlist-snapshots/{record}.spice`")
    add("")
    L.extend(mc.inputs_section({"bench": TESTBENCH.read_text()}, retained))
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: typical, {TEMP_C:g} C, {VDD_V:.2f} V, one deterministic LOCAL unit, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="offmc-smoke-") as scratch:
        run = run_deterministic("smoke", "systematic offset", pdk, Path(scratch), mismatch=0)
    if run.offset_v is None:
        print(f"SMOKE TEST FAILED: {run.error}")
        return 1
    print(f"  systematic offset = {run.offset_v*1e3:+.3f} mV (vout = {run.vout_v:.4f} V)")
    print("smoke test OK (extraction valid; statistics are the full run's job)")
    return 0


def run_controls(pdk: Pdk, work: Path, args, stats_problems: list[str]):
    """The controls, at typical / 27 C / 3.30 V: two deterministic LOCAL units
    (switch-off, imbalance) and two small `monte_carlo` requests through
    `klt sim` (process-only, switch-off MC). Control failures are appended to
    `stats_problems`; returns None when a deterministic control produced no
    value (the caller writes no record)."""
    # Controls: two deterministic LOCAL units + two small Monte Carlo requests.
    sw_off = run_deterministic("switch-off", "sw_stat_mismatch=0", pdk, work, mismatch=0)
    imb = run_deterministic("imbalance", "M1 W +10 % (snapshot copy)", pdk, work, mismatch=0,
                            dut_text=imbalance_dut(load_dut_text()), allow_changed=(IMBALANCE_DEVICE,))
    proc_stats, proc_note, proc_report = run_mc_control("process-only", "process", 1, pdk, work, args)
    if proc_stats is not None:
        proc_note = (f"sigma = {proc_stats.sigma*1e3:.4f} mV "
                     f"({'~0: mismatch draws held fixed' if proc_stats.sigma < 1e-6 else 'NONZERO: the mismatch draws still change with the process seed'}).")
    off_stats, off_note, off_report = run_mc_control("switch-off-mc", MC_VARY, 0, pdk, work, args)
    if off_stats is None:
        stats_problems.append(f"switch-off Monte Carlo control: {off_note}")
    elif off_stats.sigma > 1e-9:
        stats_problems.append(f"switch-off Monte Carlo control has sigma = {off_stats.sigma:.3g} V (must be 0)")

    if sw_off.offset_v is None or imb.offset_v is None:
        print(f"ERROR: a deterministic control failed: {sw_off.error} {imb.error}", file=sys.stderr)
        return None
    if abs(imb.offset_v - sw_off.offset_v) <= IMBALANCE_MIN_SHIFT_V:
        stats_problems.append("imbalance control did not shift the mean offset visibly")
    return sw_off, imb, proc_stats, proc_note, proc_report, off_stats, off_note, off_report


def write_controls(cdir: Path, sw_off: DetRun, imb: DetRun, off_report: dict | None, proc_report: dict | None) -> None:
    cdir.mkdir()
    for r in (sw_off, imb):
        if r.report:
            (cdir / f"{r.name}.klt-report.json").write_text(json.dumps(sanitise_report(r.report), indent=1))
    if off_report:
        (cdir / "switch-off-mc.klt-report.json").write_text(json.dumps(sanitise_report(off_report), separators=(",", ":")))
    if proc_report:
        (cdir / "process-only.klt-report.json").write_text(json.dumps(sanitise_report(proc_report), separators=(",", ":")))


def write_samples_csv(path: Path, stats_samples: dict[str, list[Sample]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["corner", "sample_index", "rndseed", "mismatch_seed", "process_seed", "vout_v", "offset_v"])
        for p in CORNERS:
            for s in sorted(stats_samples.get(p, []), key=lambda s: -1 if s.index is None else s.index):
                w.writerow([p, s.index, s.seed, s.mismatch_seed, s.process_seed, repr(s.vout_v), repr(s.offset_v)])


def write_grid_csv(path: Path, samples: dict) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["corner", "temperature_c", "vdd_v", "vcm_v", "sample_index", "rndseed", "mismatch_seed",
                    "process_seed", "vout_v", "offset_v"])
        vcm_of = dict(zip(*mc.grid_axes("full")[1:]))
        for k in grid_keys("full"):
            for s in sorted(samples.get(k, []), key=lambda s: -1 if s.index is None else s.index):
                w.writerow([k[0], k[1], k[2], vcm_of[k[2]], s.index, s.seed, s.mismatch_seed, s.process_seed,
                            repr(s.vout_v), repr(s.offset_v)])


def run_grid(pdk: Pdk, args) -> int:
    """`--grid full` (issue #106): one 45-point `monte_carlo` request through
    `klt sim`, the shared controls, and a new append-only record. A failed
    submit is reported and NOTHING is written; there is no local fallback."""
    keys = grid_keys("full")
    n_units = len(keys) * MC_N
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    ngspice, kver = ngspice_version(), klt_version()
    print(f"record {record}: {len(keys)} points x N={MC_N} = {n_units} units, seed {MC_SEED}, PDK={pdk.path}, klt {kver}",
          flush=True)
    with tempfile.TemporaryDirectory(prefix="offmc-grid-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        req = grid_request(tb, pdk, CORNERS, "full", MC_N, MC_SEED, MC_VARY,
                           hosts=GRID_HOSTS if args.hosts is None else args.hosts)
        req["batch"] = batch_block(args)
        t0 = time.monotonic()
        for attempt in range(args.batch_submit_retries + 1):
            try:
                report = run_klt_retrying(req, work / "grid" / f"out{attempt}", args.backend, work / "grid",
                                          retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
            except KltError as exc:
                print(f"ERROR: the grid Monte Carlo request could not be run; NO RECORD WRITTEN (result NOT RUN).\n{exc}",
                      file=sys.stderr)
                return 2
            lost = lost_shard_refusals(report)
            if not lost or attempt == args.batch_submit_retries:
                break
            print(f"  {lost} units lost to a refused shard launch (attempt {attempt + 1}/{args.batch_submit_retries + 1}); "
                  f"re-submitting the request in {args.batch_retry_wait_s:g}s", flush=True)
            time.sleep(args.batch_retry_wait_s)
        wall_s = time.monotonic() - t0
        keep = Path(tempfile.gettempdir()) / f"offset-mc-{record}-pvt-grid-report.json"
        keep.write_text(json.dumps(sanitise_report(report), separators=(",", ":")))
        jobs = ", ".join(str(j.get("job_id", "?")) for j in remote_jobs(report)) or "local"
        print(f"  grid report kept at {keep} (job(s) {jobs}, {wall_s:.0f} s)", flush=True)
        samples, problems = extract_grid(report, "full", MC_N)
        if problems:
            print("ERROR: the grid Monte Carlo did not complete cleanly; NO RECORD WRITTEN:", file=sys.stderr)
            for pr in problems[:40]:
                print(f"  - {pr}", file=sys.stderr)
            if len(problems) > 40:
                print(f"  ... {len(problems) - 40} more", file=sys.stderr)
            return 2
        stats = grid_stats(samples)
        worst = worst_points(stats)
        stats_problems = grid_rollup_crosscheck(stats, report)
        if mosfet_active(report) is False:
            stats_problems.append("klt reports the mosfet family mismatch as structurally inactive")
        stats_problems += [f"{fmt_key(k)}: sigma = 0, the mismatch path did not respond" for k, st in stats.items()
                           if st.sigma <= 0]

        ctl = run_controls(pdk, work, args, stats_problems)
        if ctl is None:
            return 2
        sw_off, imb, proc_stats, proc_note, proc_report, off_stats, off_note, off_report = ctl

        paths["corners"].mkdir(parents=True, exist_ok=False)
        write_grid_csv(paths["corners"] / "offset_samples.csv", samples)
        (paths["corners"] / "klt-report.json").write_text(json.dumps(sanitise_report(report), separators=(",", ":")))
        write_controls(paths["corners"] / "controls", sw_off, imb, off_report, proc_report)

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join([
            f"* netlist snapshot for record {record} (issue #106, 45-point PVT grid)",
            "* Reproduces the measured design: DUT contents, testbench, conditions.",
            "* ---- conditions: klt sim request (grid) ----",
            *("* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
            "",
            "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
            dut_text,
            "* ---- testbench: sim/offset-mc/testbench/tb_offset_mc.spice (verbatim) ----",
            TESTBENCH.read_text(),
            "* ---- imbalance control: the ONLY DUT line that differs (snapshot copy; committed netlist untouched) ----",
            *("* " + ln for ln in imbalance_dut(dut_text).splitlines() if ln.lower().startswith(IMBALANCE_DEVICE + " ")),
            "",
        ]))
        md = build_grid_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, report=report, stats=stats, worst=worst,
            stats_problems=stats_problems, sw_off=sw_off, imb=imb, proc_stats=proc_stats, proc_note=proc_note,
            off_stats=off_stats, off_note=off_note, wall_s=wall_s, dut_sha=dut_sha, n_units=n_units,
            nominal_ref=nominal_reference(),
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    we = worst["extreme"]
    print(f"  worst |mean|+3s {stats[we].worst_extreme*1e3:.3f} mV at {fmt_key(we)}; "
          f"worst sigma {stats[worst['sigma']].sigma*1e3:.3f} mV at {fmt_key(worst['sigma'])}")
    if stats_problems:
        print("STUDY PROBLEMS:")
        for s in stats_problems:
            print(f"  - {s}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one deterministic local unit, no record")
    ap.add_argument("--grid", choices=GRIDS, default="nominal",
                    help="nominal: 5 corners at 27 C / 3.30 V (issue #45); full: the 45-point T/VDD grid (issue #106)")
    ap.add_argument("--backend", help="klt execution backend for the Monte Carlo grid "
                    "(default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0)
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--hosts", type=int, default=None,
                    help=f"--grid full only: fleet jobs to shard the units over (default {GRID_HOSTS})")
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)
    if args.grid == "full":
        return run_grid(pdk, args)

    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    ngspice, kver = ngspice_version(), klt_version()
    n_units = len(CORNERS) * MC_N
    print(f"record {record}: {len(CORNERS)} corners x N={MC_N} = {n_units} units, seed {MC_SEED}, PDK={pdk.path}, klt {kver}")

    with tempfile.TemporaryDirectory(prefix="offmc-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        req = mc_request(tb, pdk, CORNERS, MC_N, MC_SEED, MC_VARY)
        req["batch"] = batch_block(args)
        t0 = time.monotonic()
        try:
            report = run_klt_retrying(req, work / "grid" / "out", args.backend, work / "grid",
                                      retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
        except KltError as exc:
            print(f"ERROR: the Monte Carlo request could not be run; NO RECORD WRITTEN (result NOT RUN).\n{exc}", file=sys.stderr)
            return 2
        wall_s = time.monotonic() - t0
        # Keep the fleet result outside the repo so a post-processing failure
        # never throws away a completed grid (the repo record is append-only
        # and only written for a clean run).
        keep = Path(tempfile.gettempdir()) / f"offset-mc-{record}-grid-report.json"
        keep.write_text(json.dumps(sanitise_report(report), separators=(",", ":")))
        ex = extract_samples(report, CORNERS, MC_N)
        if ex.problems:
            print(f"(raw grid report kept at {keep})", file=sys.stderr)
            print("ERROR: the Monte Carlo grid did not complete cleanly; NO RECORD WRITTEN:", file=sys.stderr)
            for p in ex.problems[:30]:
                print(f"  - {p}", file=sys.stderr)
            return 2
        stats = per_corner_stats(ex.samples, CORNERS)
        worst = worst_corners(stats)
        stats_problems = rollup_crosscheck(stats, klt_rollup(report))
        if mosfet_active(report) is False:
            stats_problems.append("klt reports the mosfet family mismatch as structurally inactive")
        if any(s.sigma <= 0 for s in stats.values()):
            stats_problems.append("a corner has sigma = 0: the mismatch path did not respond")

        ctl = run_controls(pdk, work, args, stats_problems)
        if ctl is None:
            return 2
        sw_off, imb, proc_stats, proc_note, proc_report, off_stats, off_note, off_report = ctl

        paths["corners"].mkdir(parents=True, exist_ok=False)
        write_samples_csv(paths["corners"] / "offset_samples.csv", ex.samples)
        (paths["corners"] / "klt-report.json").write_text(json.dumps(sanitise_report(report), separators=(",", ":")))
        write_controls(paths["corners"] / "controls", sw_off, imb, off_report, proc_report)

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join([
            f"* netlist snapshot for record {record} (issue #45)",
            "* Reproduces the measured design: DUT contents, testbench, conditions.",
            "* ---- conditions: klt sim request (grid) ----",
            *("* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
            "",
            "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
            dut_text,
            "* ---- testbench: sim/offset-mc/testbench/tb_offset_mc.spice (verbatim) ----",
            TESTBENCH.read_text(),
            "* ---- imbalance control: the ONLY DUT line that differs (snapshot copy; committed netlist untouched) ----",
            *("* " + ln for ln in imbalance_dut(dut_text).splitlines() if ln.lower().startswith(IMBALANCE_DEVICE + " ")),
            "",
        ]))

        seeds = [s.seed for ss in ex.samples.values() for s in ss if s.seed is not None]
        md = build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, report=report, stats=stats,
            worst=worst, stats_problems=stats_problems, sw_off=sw_off, imb=imb, proc_stats=proc_stats,
            proc_note=proc_note, proc_report=proc_report, off_stats=off_stats, off_note=off_note, wall_s=wall_s, dut_sha=dut_sha, n_units=n_units,
            seed_range=(min(seeds), max(seeds)) if seeds else None,
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)

    print(f"wrote {paths['record']}")
    for p in CORNERS:
        s = stats[p]
        print(f"  {p}: mean {s.mean*1e3:+.3f} mV  sigma {s.sigma*1e3:.3f} mV  3s {s.three_sigma*1e3:.3f} mV")
    print(f"  worst: sigma -> {worst['sigma']}, |mean|+3s -> {worst['extreme']}")
    if stats_problems:
        print("STUDY PROBLEMS:")
        for s in stats_problems:
            print(f"  - {s}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
