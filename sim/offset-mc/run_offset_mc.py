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

Usage:
    python3 sim/offset-mc/run_offset_mc.py                 # full grid + record
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
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

from harness import Pdk, allocate_record_id, find_pdk, ngspice_version  # noqa: E402

TESTBENCH = HERE / "testbench" / "tb_offset_mc.spice"
DUT_EXPORT = REPO_ROOT / "design" / "netlist" / "opamp_two_stage.spice"

# --------------------------------------------------------------------------
# Conditions (issue #45 scope decision: nominal T and supply only)
# --------------------------------------------------------------------------

CORNERS = ["typical", "ff", "ss", "fs", "sf"]
PASSIVE_SECTIONS = ("res_typical", "mimcap_typical")
TEMP_C = 27.0
VDD_V = 3.30
VCM_V = 1.65
MODEL_LIB = "libs.tech/ngspice/sm141064.ngspice"

#: Monte Carlo request. The seed is recorded; klt derives every per-sample
#: seed from it deterministically, so re-running reproduces the draw.
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"
#: Sigma of a normal sample has relative standard error ~ 1/sqrt(2(N-1)).
MIN_N = 300

#: Control sizes (kept small).
CONTROL_N = 40
IMBALANCE_DEVICE = "xm1"
IMBALANCE_W_FROM, IMBALANCE_W_TO = "W=3.6u", "W=3.96u"  # +10 %
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

SIM_SOURCE = "Ibias"
SIM_ARGS = "Ibias 10u 11u 1u"
MEASUREMENTS = [
    {"name": "vout_v", "spice": ".meas dc vout_v FIND v(vout) AT=10u", "unit": "V"},
    {"name": "vinp_v", "spice": ".meas dc vinp_v FIND v(vinp) AT=10u", "unit": "V"},
    {"name": "vos_v", "spice": ".meas dc vos_v FIND par('v(vout)-v(vinp)') AT=10u", "unit": "V"},
]


class KltError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# DUT: the committed export, wrapper-normalised (shared helper)
# --------------------------------------------------------------------------


def load_dut_text() -> str:
    """The committed export as an includable subcircuit (shared with
    `design/check_dc_op.py`: uncomment `**.subckt`/`**.ends`, drop `.end`)."""
    from check_dc_op import subckt_from_export

    return subckt_from_export(DUT_EXPORT.read_text())


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

DUT_INCLUDE_NAME = "opamp_two_stage.dut.spice"
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
    tb = TESTBENCH.read_text()
    errs = guard_testbench(tb)
    if errs:
        raise RuntimeError("testbench guard failed:\n  " + "\n  ".join(errs))
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdk.design_include, work / "design.ngspice")
    dut = load_dut_text() if dut_text is None else dut_text
    derrs = guard_dut(dut, allow_changed=allow_changed)
    if derrs:
        raise RuntimeError("DUT guard failed:\n  " + "\n  ".join(derrs))
    (work / DUT_INCLUDE_NAME).write_text(dut)
    tb = tb.replace("'design.ngspice'", f"'{work / 'design.ngspice'}'")
    tb = tb.replace("'opamp_two_stage.dut.spice'", f"'{work / DUT_INCLUDE_NAME}'")
    tb, n = _SWITCH_RE.subn(f".param sw_stat_mismatch={int(mismatch)}", tb)
    if n != 1:
        raise RuntimeError("testbench `.param sw_stat_mismatch=<0|1>` line not found exactly once")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt request / run
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


def run_klt(request: dict, outdir: Path, backend: str | None, workdir: Path) -> dict:
    """Run `klt sim` on a request dict and return its JSON report. `outdir`
    must be absolute (klt launches ngspice with it as cwd)."""
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    req_path = workdir / f"{outdir.name}.request.json"
    req_path.write_text(json.dumps(request, indent=2))
    cmd = ["klt", "sim", str(req_path), "-o", str(outdir), "--format", "json"]
    if backend:
        cmd += ["--backend", backend]
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
    capacity or the shared concurrency cap (nothing ran). Never changes
    backend; any other error, or exhausted retries, propagates."""
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


def extract_samples(report: dict, corners: list[str], n_expected: int | None) -> Extraction:
    """Per-corner offset samples from a klt report, with extraction validation.

    Per sample: all three measurements finite; vinp == VCM (the follower tie
    vinn == vout is structural, enforced by `guard_testbench`); the independently measured `vos_v` agrees with
    vout - vinp; |offset| inside the valid follower window. Anything else is
    a problem string -- the sample is NOT silently dropped. With
    `n_expected`, each corner must contribute exactly that many samples with
    distinct sample indices.
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
        if abs(vinp - VCM_V) > TIE_TOL_V:
            ex.problems.append(f"{cid}: vinp = {vinp:.6g} V, expected {VCM_V} V")
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


def mismatch_activity(report: dict) -> list[dict]:
    return ((report.get("environment") or {}).get("monte_carlo") or {}).get("family_mismatch") or []


def mosfet_active(report: dict) -> bool | None:
    for f in mismatch_activity(report):
        if f.get("family") == "mosfet":
            return f.get("active")
    return None


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


def claim_record_paths(base: Path, record: str) -> dict[str, Path]:
    """Output paths for one record under `base`. Raises FileExistsError if ANY
    already exists: evidence is append-only; a re-run mints a new id."""
    paths = {
        "record": base / "records" / f"{record}.md",
        "snapshot": base / "netlist-snapshots" / f"{record}.spice",
        "corners": base / "corners" / record,
    }
    for p in paths.values():
        if p.exists():
            raise FileExistsError(f"{p} already exists; evidence is append-only")
    return paths


def klt_version() -> str:
    try:
        return subprocess.run(["klt", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def batch_block(args) -> dict:
    block: dict = {}
    if args.batch_runner_version_check:
        block["runner_version_check"] = args.batch_runner_version_check
    if args.batch_capacity_wait_s is not None:
        block["capacity_wait_s"] = args.batch_capacity_wait_s
    return block


def remote_of(report: dict) -> dict:
    return (report.get("environment") or {}).get("remote") or {}


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


def write_samples_csv(path: Path, stats_samples: dict[str, list[Sample]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["corner", "sample_index", "rndseed", "mismatch_seed", "process_seed", "vout_v", "offset_v"])
        for p in CORNERS:
            for s in sorted(stats_samples.get(p, []), key=lambda s: -1 if s.index is None else s.index):
                w.writerow([p, s.index, s.seed, s.mismatch_seed, s.process_seed, repr(s.vout_v), repr(s.offset_v)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one deterministic local unit, no record")
    ap.add_argument("--backend", help="klt execution backend for the Monte Carlo grid "
                    "(default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0)
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record)
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

        # Controls: two deterministic LOCAL units + one small process-only MC.
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
            return 2
        if abs(imb.offset_v - sw_off.offset_v) <= IMBALANCE_MIN_SHIFT_V:
            stats_problems.append("imbalance control did not shift the mean offset visibly")

        paths["corners"].mkdir(parents=True, exist_ok=False)
        write_samples_csv(paths["corners"] / "offset_samples.csv", ex.samples)
        (paths["corners"] / "klt-report.json").write_text(json.dumps(sanitise_report(report), separators=(",", ":")))
        cdir = paths["corners"] / "controls"
        cdir.mkdir()
        for r in (sw_off, imb):
            if r.report:
                (cdir / f"{r.name}.klt-report.json").write_text(json.dumps(sanitise_report(r.report), indent=1))
        if off_report:
            (cdir / "switch-off-mc.klt-report.json").write_text(json.dumps(sanitise_report(off_report), separators=(",", ":")))
        if proc_report:
            (cdir / "process-only.klt-report.json").write_text(json.dumps(sanitise_report(proc_report), separators=(",", ":")))

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
