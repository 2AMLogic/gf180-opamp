#!/usr/bin/env python3
"""Open-loop DC gain / GBW / phase margin of the committed sized schematic
across the full ratified PVT grid (issue #38; tracker #7 item 5).

The device under test is `design/netlist/opamp_two_stage.spice` -- the xschem
export, instantiated as `opamp_two_stage` -- not a hand-built netlist. The
testbench (`testbench/tb_gain_gbw_pm.spice`) declares no transistor.

What it runs
------------
The 45-point grid `process {typical, ff, ss, fs, sf} x temperature {-40, 27,
125 C} x supply {2.97, 3.30, 3.63 V}` is expressed as ONE `klt sim` request
(a `corners` block), one small-signal `.ac` sweep per point. Which backend
executes it is `klt`'s decision (`--backend`, the request's `backend`, or
`$KLT_SIM_BACKEND`); on a dispatch worker that is the Spot batch fleet. This
script never launches ngspice itself and never falls back to a local grid
when a batch submit fails -- it stops with the error and writes no record.

Per point it extracts, from the complex `.ac` rawfile klt retains:

  * open-loop DC gain -- the LOW-FREQUENCY PLATEAU of the response (the
    lowest decade of the sweep must be flat to within `PLATEAU_TOL_DB` and
    have ~0 deg phase). It is NOT the response's peak magnitude: a peak would
    silently accept a feedback-isolation artifact (a gain that rises with
    frequency), which the plateau check rejects.
  * GBW -- frequency of the FIRST DESCENDING 0 dB crossing, interpolated
    linearly in (log f, dB).
  * phase margin -- 180 deg + the UNWRAPPED phase at that crossing,
    interpolated the same way.

The gain is `v(vout) / (v(vinp) - v(vinn))`, the actual differential input
phasor, not an assumed 1 V.

Verdicts are checked per point against the ratified bounds in
`spec/target-spec.md` (gain >= 60 dB with a separately labelled >= 70 dB
stretch, GBW >= 10 MHz into 2 pF, PM >= 60 deg); each metric's binding corner
is the worst point. Missing crossings, non-finite or malformed data, a
non-flat plateau, a re-crossing (peaking) response and a wrong-polarity
response are INVALID and can never pass.

Evidence produced (append-only, a new record id every run):

    corners/<rid>/<process>_<T>c_<vdd>v.{log,dat,cir}   one triple per point
    corners/<rid>/klt-report.json                         sanitised klt report
    corners/<rid>/controls/...                            isolation + negative controls
    netlist-snapshots/<rid>.spice                         DUT + testbench + conditions
    records/<rid>.md, records/<rid>-plots/*.png

Usage:
    python3 sim/gain-gbw-pm/run_gain_gbw_pm.py                # full grid + record
    python3 sim/gain-gbw-pm/run_gain_gbw_pm.py --smoke        # one point, no record
    python3 sim/gain-gbw-pm/run_gain_gbw_pm.py --backend local  # force a backend

Exit status: 0 when the evidence is complete (45 valid simulations, negative
controls fail as they must, isolation sweep stable) -- INCLUDING when a spec
row misses, because a miss is a result; `--strict` makes a spec miss exit 1.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

from harness import Pdk, allocate_record_id, find_pdk, ngspice_version  # noqa: E402

TESTBENCH = HERE / "testbench" / "tb_gain_gbw_pm.spice"
DUT_EXPORT = REPO_ROOT / "design" / "netlist" / "opamp_two_stage.spice"

# --------------------------------------------------------------------------
# Grid and conditions (spec/target-spec.md Sec.1)
# --------------------------------------------------------------------------

#: MOS corners; each is bundled with the SAME typical resistor and MIM
#: capacitor sections. The grid varies MOS corner, temperature and supply;
#: it does NOT claim independent passive (RZ / CC) corner coverage -- that was
#: not run. See the record's "Passive-section policy".
CORNERS = ["typical", "ff", "ss", "fs", "sf"]
PASSIVE_SECTIONS = ("res_typical", "mimcap_typical")
TEMPS_C = [-40.0, 27.0, 125.0]
SUPPLIES_V = [2.97, 3.30, 3.63]
MODEL_LIB = "libs.tech/ngspice/sm141064.ngspice"

NOMINAL = ("typical", 27.0, 3.30)

#: `.ac dec` sweep. Starts at 0.1 Hz so the lowest decade is a plateau for
#: every corner (dominant pole is hundreds of Hz), ends well above any GBW.
AC_FSTART, AC_FSTOP, AC_PPD = 0.1, 1e9, 20

# Ratified bounds (spec/target-spec.md Sec.2). Never edited here.
GAIN_MIN_DB = 60.0
GAIN_STRETCH_DB = 70.0
GBW_MIN_HZ = 10e6
PM_MIN_DEG = 60.0

# Plateau / polarity validity.
PLATEAU_DECADE_HI = 10.0  # plateau band = [f0, 10*f0]
PLATEAU_TOL_DB = 0.10
POLARITY_TOL_DEG = 10.0
MIN_POINTS = 20

# Feedback-isolation study: Lfb = Cfb values (nominal is 1e9 in the testbench).
ISOLATION_VALUES = (1e8, 1e9, 1e10)
ISOLATION_INADEQUATE = 1e4  # deliberately too small: must be detected
ISOLATION_GAIN_TOL_DB = 0.10
ISOLATION_PM_TOL_DEG = 0.5
ISOLATION_GBW_REL_TOL = 0.005

# Operating-point flags (recorded, not a spec row).
OP_VOUT_TOL_V = 0.10
DEVICES = ("xm1", "xm2", "xm3", "xm4", "xm5", "xm6", "xm7", "xmb1")


# --------------------------------------------------------------------------
# DUT: the committed export, wrapper-normalised (shared helper)
# --------------------------------------------------------------------------


def load_dut_text() -> str:
    """The committed export as an includable subcircuit.

    Reuses `subckt_from_export()` from `design/check_dc_op.py` unchanged, so
    the DC operating-point check and this testbench consume the export through
    one conversion: uncomment xschem's `**.subckt`/`**.ends`, drop `.end`.
    """
    from check_dc_op import subckt_from_export

    return subckt_from_export(DUT_EXPORT.read_text())


def strip_instance(dut_text: str, inst: str) -> str:
    """Remove one device (and its `+` continuation lines) from a subckt body."""
    out: list[str] = []
    skipping = False
    removed = 0
    for line in dut_text.splitlines():
        first = line.split(None, 1)[0].lower() if line.strip() else ""
        if first == inst.lower():
            skipping = True
            removed += 1
            continue
        if skipping and line.startswith("+"):
            continue
        skipping = False
        out.append(line)
    if removed != 1:
        raise RuntimeError(f"expected exactly one {inst} in the DUT, found {removed}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Source guard: the bench must stay tied to the committed design
# --------------------------------------------------------------------------

DUT_INCLUDE_NAME = "opamp_two_stage.dut.spice"
_DEVICE_MODEL_RE = re.compile(
    r"\b(nfet|pfet|nmos|pmos|cap_mim|ppolyf|npolyf|nplus|pplus|diode|bjt)\w*", re.I
)


def guard_testbench(tb_text: str) -> list[str]:
    """Reasons the testbench no longer tests the committed design (empty = ok).

    The bench must (1) include the design-derived DUT file and the PDK design
    file, (2) declare no transistor or PDK device of its own, and (3)
    instantiate `opamp_two_stage` exactly once. A bench that stops including
    the DUT, or grows its own devices, can no longer drift silently from the
    schematic.
    """
    errs: list[str] = []
    logical: list[str] = []
    for raw in tb_text.splitlines():
        if raw.startswith("+") and logical:
            logical[-1] += " " + raw[1:].strip()
        else:
            logical.append(raw.strip())
    code = [ln for ln in logical if ln and not ln.startswith("*")]
    includes = [re.match(r"\.include\s+['\"]?([^'\"\s]+)", ln, re.I) for ln in code]
    targets = [m.group(1) for m in includes if m]
    if DUT_INCLUDE_NAME not in targets:
        errs.append(f"testbench does not `.include '{DUT_INCLUDE_NAME}'` (the design-derived DUT)")
    if "design.ngspice" not in targets:
        errs.append("testbench does not `.include 'design.ngspice'` (PDK global parameters)")
    for ln in code:
        first = ln.split(None, 1)[0]
        low = first.lower()
        if low.startswith("m"):
            errs.append(f"hand-declared MOSFET in the testbench: {ln[:60]}")
        elif low.startswith("x") and low != "xdut":
            errs.append(f"unexpected subcircuit/device instance in the testbench: {ln[:60]}")
        elif low == "xdut":
            if "opamp_two_stage" not in ln.split():
                errs.append("Xdut does not instantiate opamp_two_stage")
        elif _DEVICE_MODEL_RE.search(ln) and not ln.startswith("."):
            errs.append(f"PDK device declared in the testbench: {ln[:60]}")
    n_dut = sum(1 for ln in code if ln.split(None, 1)[0].lower() == "xdut")
    if n_dut != 1:
        errs.append(f"expected exactly one Xdut instance, found {n_dut}")
    for ln in code:
        if re.match(r"\.(lib|temp|control|end)\b", ln, re.I):
            errs.append(f"testbench must be a circuit body; found `{ln.split()[0]}`")
    return errs


def guard_dut(dut_text: str, *, allow_missing: tuple[str, ...] = ()) -> list[str]:
    """The materialised DUT must be the committed export, wrapper-normalised.

    Every non-wrapper line of the export must appear, unchanged and in order,
    in the DUT (a negative control may drop devices named in `allow_missing`);
    no device may appear twice; exactly one `.subckt opamp_two_stage`.
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
            if first in allow_missing:
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


# --------------------------------------------------------------------------
# Testbench materialisation
# --------------------------------------------------------------------------

_PARAM_RE = re.compile(r"^\.param\s+lfb=\S+\s+cfb=\S+\s*$", re.M)
_IBIAS_RE = re.compile(r"^Ibias\s+vdd\s+ibias\s+dc\s+\S+\s*$", re.M)


def materialise(
    work: Path,
    pdk: Pdk,
    *,
    dut_text: str | None = None,
    lfb: float | None = None,
    ibias_a: float | None = None,
    allow_missing: tuple[str, ...] = (),
) -> Path:
    """Write the per-run work directory and return the testbench path.

    The committed testbench is used verbatim except for (a) the two `.include`
    targets, rewritten to absolute paths in `work`, and (b) optional, explicit
    overrides used only by the isolation study (`lfb`) and a negative control
    (`ibias_a`). `dut_text` overrides the DUT only for a negative control.
    """
    errs = guard_testbench(TESTBENCH.read_text())
    if errs:
        raise RuntimeError("testbench guard failed:\n  " + "\n  ".join(errs))
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdk.design_include, work / "design.ngspice")
    dut = load_dut_text() if dut_text is None else dut_text
    derrs = guard_dut(dut, allow_missing=allow_missing)
    if derrs:
        raise RuntimeError("DUT guard failed:\n  " + "\n  ".join(derrs))
    (work / DUT_INCLUDE_NAME).write_text(dut)
    tb = TESTBENCH.read_text()
    tb = tb.replace("'design.ngspice'", f"'{work / 'design.ngspice'}'")
    tb = tb.replace(
        "'opamp_two_stage.dut.spice'", f"'{work / 'opamp_two_stage.dut.spice'}'"
    )
    if lfb is not None:
        tb, n = _PARAM_RE.subn(f".param lfb={lfb:g} cfb={lfb:g}", tb)
        if n != 1:
            raise RuntimeError("testbench .param lfb/cfb line not found exactly once")
    if ibias_a is not None:
        tb, n = _IBIAS_RE.subn(f"Ibias vdd ibias dc {ibias_a:g}", tb)
        if n != 1:
            raise RuntimeError("testbench Ibias line not found exactly once")
    path = work / "tb.spice"
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt request / run
# --------------------------------------------------------------------------


class KltError(RuntimeError):
    pass


def process_axis(corners: list[str]) -> list[dict]:
    return [{"name": c, "sections": [c, *PASSIVE_SECTIONS]} for c in corners]


def ac_request(netlist: Path, pdk: Pdk, corners, temps, supplies) -> dict:
    return {
        "netlist": str(netlist),
        "engine": "ngspice",
        "models": {"pdk": pdk.variant, "lib": MODEL_LIB},
        "corners": {
            "process": process_axis(list(corners)),
            # vdd and vcm sweep together by index: VCM tracks VDD/2.
            "supply_v": {"vdd": list(supplies), "vcm": [round(v / 2, 6) for v in supplies]},
            "temperature_c": list(temps),
        },
        "analysis": {"kind": "ac", "args": f"dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}"},
        # Cross-checks only, as native `.meas` cards (every klt/fleet-runner
        # version supports `spice` measurements; `expr` is not available on
        # the older fleet runner). The verdicts come from the rawfile
        # extraction; these must agree with it (ngspice measures |v(vout)|,
        # not the differential gain, and interpolates on the sweep grid).
        "measurements": [
            {"name": "a_first_db", "spice": f".meas ac a_first_db FIND vdb(vout) AT={AC_FSTART:g}", "unit": "dB"},
            {"name": "gbw_hz", "spice": ".meas ac gbw_hz WHEN vdb(vout)=0 FALL=1", "unit": "Hz"},
            {"name": "ph_gbw_rad", "spice": ".meas ac ph_gbw_rad FIND vp(vout) WHEN vdb(vout)=0 FALL=1", "unit": "rad"},
        ],
        "options": {"timeout_s": 300, "keep_artifacts": True, "waveforms": True},
    }


def op_request_grid(netlist: Path, pdk: Pdk, corners, temps, supplies) -> dict:
    """Operating point of every grid point as a one-step DC sweep of `Ibias`
    (`.meas dc ... AT=` works on any runner; there is no `.meas op`)."""
    req = ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = {"kind": "dc", "args": "Ibias 10u 11u 1u"}
    req["measurements"] = [
        {"name": "vout_v", "spice": ".meas dc vout_v FIND v(vout) AT=10u", "unit": "V"},
        {"name": "vinn_v", "spice": ".meas dc vinn_v FIND v(vinn) AT=10u", "unit": "V"},
        {"name": "ivdd_a", "spice": ".meas dc ivdd_a FIND i(vdd) AT=10u", "unit": "A"},
    ]
    req["options"] = {"timeout_s": 300}
    return req


def op_request_nominal(netlist: Path, pdk: Pdk, corners, temps, supplies) -> dict:
    """Device-level operating point (needs `expr`; local single unit only)."""
    req = ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"] = {"kind": "op", "args": ""}
    meas = [
        {"name": "vout_v", "expr": "v(vout)", "unit": "V"},
        {"name": "vinn_v", "expr": "v(vinn)", "unit": "V"},
        {"name": "itail_a", "expr": "abs(@m.xdut.xm5.m0[id])", "unit": "A"},
        {"name": "iout_a", "expr": "abs(@m.xdut.xm7.m0[id])", "unit": "A"},
    ]
    for d in DEVICES:
        meas.append(
            {
                "name": f"satm_{d}",
                "expr": f"abs(@m.xdut.{d}.m0[vds]) - abs(@m.xdut.{d}.m0[vdsat])",
                "unit": "V",
            }
        )
    req["measurements"] = meas
    req["options"] = {"timeout_s": 300}
    return req


def run_klt(request: dict, outdir: Path, backend: str | None, workdir: Path) -> dict:
    """Run `klt sim` on a request dict and return its JSON report.

    `outdir` MUST be absolute: klt launches ngspice with the corner's artifact
    directory as cwd, so a relative `-o` makes every corner fail before it
    runs (reported as a bare measurement error).
    """
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
    capacity or the shared concurrency cap (nothing ran, so nothing is lost).

    This only retries the same off-host submit; it never changes backend. Any
    other error, or exhausting the retries, propagates as `KltError`.
    """
    import time

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
# Rawfile parsing (ngspice ASCII, Flags: complex)
# --------------------------------------------------------------------------


def parse_ascii_complex_raw(text: str) -> dict[str, np.ndarray]:
    """Parse an ngspice ASCII `.ac` rawfile into {vector name: complex array}.

    Raises ValueError on anything malformed (no header, wrong value count,
    non-finite numbers): bad data must fail loudly, never extrapolate.
    """
    if "Flags: complex" not in text:
        raise ValueError("rawfile is not a complex (ac) rawfile")
    m = re.search(r"No\. Variables:\s*(\d+)", text)
    p = re.search(r"No\. Points:\s*(\d+)", text)
    if not m or not p or "Variables:" not in text or "Values:" not in text:
        raise ValueError("rawfile header incomplete")
    nvar, npts = int(m.group(1)), int(p.group(1))
    head, _, body = text.partition("Values:")
    names = re.findall(r"^\t\d+\t(\S+)\t", head.split("Variables:", 1)[1], re.M)
    if len(names) != nvar:
        raise ValueError(f"rawfile declares {nvar} variables, found {len(names)}")
    pairs = re.findall(r"([-+0-9.eE]+|nan|inf|-inf),([-+0-9.eE]+|nan|inf|-inf)", body)
    if len(pairs) != nvar * npts:
        raise ValueError(
            f"rawfile has {len(pairs)} values, expected {nvar} x {npts} = {nvar * npts}"
        )
    arr = np.array([complex(float(a), float(b)) for a, b in pairs]).reshape(npts, nvar)
    if not np.all(np.isfinite(arr)):
        raise ValueError("rawfile contains non-finite values")
    return {n.lower(): arr[:, i] for i, n in enumerate(names)}


def differential_gain(vec: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(freq, h, vdiff): h = v(vout) / (v(vinp) - v(vinn)) with the ACTUAL
    differential phasor."""
    for need in ("frequency", "v(vout)", "v(vinp)", "v(vinn)"):
        if need not in vec:
            raise ValueError(f"rawfile has no {need} vector")
    freq = vec["frequency"].real
    vdiff = vec["v(vinp)"] - vec["v(vinn)"]
    if np.any(np.abs(vdiff) < 1e-9):
        raise ValueError("differential input phasor is ~0: AC stimulus not reaching the DUT")
    return freq, vec["v(vout)"] / vdiff, vdiff


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class Metrics:
    valid: bool
    reason: str = ""
    dc_gain_db: float = float("nan")
    gbw_hz: float = float("nan")
    pm_deg: float = float("nan")
    phase_at_gbw_deg: float = float("nan")
    plateau_spread_db: float = float("nan")
    plateau_phase_deg: float = float("nan")
    n_crossings: int = 0
    freq: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    db: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)
    phase_deg: np.ndarray = field(default_factory=lambda: np.array([]), repr=False)


def _invalid(reason: str, **kw) -> Metrics:
    return Metrics(valid=False, reason=reason, **kw)


def extract_metrics(freq: np.ndarray, h: np.ndarray) -> Metrics:
    """Extract DC gain / GBW / PM from a complex open-loop response `h(freq)`.

    Method (also the record's "Extraction method"):

    1. Validate: >= MIN_POINTS finite points, positive strictly increasing
       frequency, non-zero magnitude.
    2. Plateau: the lowest decade [f0, 10 f0] must be flat to within
       PLATEAU_TOL_DB and have phase within POLARITY_TOL_DEG of 0 (vinp is the
       non-inverting input). DC gain = mean dB of that band. A gain that
       rises, or a plateau of the wrong sign, is invalid -- never "the peak".
    3. Phase is unwrapped with `numpy.unwrap` over the full sweep and
       referenced to the plateau phase, so a response that crosses +-180 deg
       is not folded back.
    4. GBW is the first DESCENDING 0 dB crossing (db[i-1] >= 0 > db[i]),
       linearly interpolated in (log10 f, dB); phase at that frequency is
       interpolated the same way. PM = 180 + phase_at_gbw (relative, unwrapped).
    5. Any later crossing of 0 dB (gain peaking back above 0 dB) makes the
       response invalid: the first crossing would not be a trustworthy GBW.
    """
    freq = np.asarray(freq, dtype=float)
    h = np.asarray(h, dtype=complex)
    if freq.ndim != 1 or h.shape != freq.shape or freq.size < MIN_POINTS:
        return _invalid(f"fewer than {MIN_POINTS} points or mismatched shapes")
    if not (np.all(np.isfinite(freq)) and np.all(np.isfinite(h))):
        return _invalid("non-finite data")
    if np.any(freq <= 0) or np.any(np.diff(freq) <= 0):
        return _invalid("frequency not positive and strictly increasing")
    mag = np.abs(h)
    if np.any(mag <= 0):
        return _invalid("zero magnitude in response")
    db = 20.0 * np.log10(mag)
    phase = np.degrees(np.unwrap(np.angle(h)))

    band = freq <= freq[0] * PLATEAU_DECADE_HI
    if band.sum() < 3:
        return _invalid("fewer than 3 points in the plateau band")
    spread = float(db[band].max() - db[band].min())
    plateau_phase = float(np.mean(phase[band]))
    common = dict(
        plateau_spread_db=spread,
        plateau_phase_deg=plateau_phase,
        freq=freq,
        db=db,
        phase_deg=phase - plateau_phase,
    )
    # Polarity first: a sign-flipped response is a wiring/polarity error.
    wrapped = (plateau_phase + 180.0) % 360.0 - 180.0
    if abs(wrapped) > POLARITY_TOL_DEG:
        return _invalid(
            f"plateau phase {wrapped:.1f} deg is not ~0 (wrong polarity or lagging plateau)",
            **common,
        )
    if spread > PLATEAU_TOL_DB:
        return _invalid(
            f"no low-frequency plateau: lowest decade varies by {spread:.3f} dB "
            f"(> {PLATEAU_TOL_DB} dB; feedback isolation or a pole below the sweep)",
            **common,
        )
    dc_gain = float(np.mean(db[band]))
    common["dc_gain_db"] = dc_gain

    # Crossings of 0 dB over the whole sweep.
    sign = db >= 0
    desc = np.where(sign[:-1] & ~sign[1:])[0]
    asc = np.where(~sign[:-1] & sign[1:])[0]
    n_cross = int(desc.size + asc.size)
    common["n_crossings"] = n_cross
    if not sign[0]:
        return _invalid("response is below 0 dB at the lowest frequency (no gain)", **common)
    if desc.size == 0:
        return _invalid(
            f"no 0 dB crossing: gain still {db[-1]:.1f} dB at {freq[-1]:.3g} Hz "
            "(sweep ends above unity gain)",
            **common,
        )
    if n_cross > 1:
        return _invalid(
            f"{n_cross} 0 dB crossings (gain peaks back above 0 dB); first descending "
            "crossing is not a trustworthy GBW",
            **common,
        )
    i = int(desc[0])
    lf0, lf1 = math.log10(freq[i]), math.log10(freq[i + 1])
    d0, d1 = db[i], db[i + 1]
    frac = (0.0 - d0) / (d1 - d0)
    gbw = 10 ** (lf0 + frac * (lf1 - lf0))
    rel = phase - plateau_phase
    ph = float(rel[i] + frac * (rel[i + 1] - rel[i]))
    pm = 180.0 + ph
    if not (math.isfinite(gbw) and math.isfinite(pm)):
        return _invalid("non-finite GBW or phase margin", **common)
    return Metrics(
        valid=True, gbw_hz=float(gbw), pm_deg=float(pm), phase_at_gbw_deg=ph, **common
    )


# --------------------------------------------------------------------------
# Verdicts and binding corners
# --------------------------------------------------------------------------

Key = tuple[str, float, float]  # (process, temperature_c, supply_v)

ROWS = (
    # (id, label, attribute, bound, unit)
    ("gain", "Open-loop DC gain", "dc_gain_db", GAIN_MIN_DB, "dB"),
    ("gbw", "GBW (CL = 2 pF)", "gbw_hz", GBW_MIN_HZ, "Hz"),
    ("pm", "Phase margin", "pm_deg", PM_MIN_DEG, "deg"),
)


def point_passes(m: Metrics, attr: str, bound: float) -> bool:
    if not m.valid:
        return False
    v = getattr(m, attr)
    return math.isfinite(v) and v >= bound


@dataclass
class RowVerdict:
    row: str
    label: str
    bound: float
    unit: str
    verdict: str  # "PASS" | "FAIL"
    n_pass: int
    n_total: int
    worst_value: float
    binding: Key | None
    n_invalid: int
    stretch_pass: int | None = None  # gain only


def fmt_key(k: Key) -> str:
    return f"{k[0]} / {k[1]:g} C / {k[2]:.2f} V"


def judge(results: dict[Key, Metrics]) -> list[RowVerdict]:
    out: list[RowVerdict] = []
    for rid, label, attr, bound, unit in ROWS:
        n_pass = sum(point_passes(m, attr, bound) for m in results.values())
        n_inv = sum(not m.valid for m in results.values())
        finite = [(getattr(m, attr), k) for k, m in results.items() if m.valid]
        if n_inv and len(finite) < len(results):
            # An invalid point is the worst possible outcome for a row.
            bad = next(k for k, m in results.items() if not m.valid)
            worst, binding = float("nan"), bad
        elif finite:
            worst, binding = min(finite, key=lambda t: t[0])
        else:
            worst, binding = float("nan"), None
        stretch = None
        if rid == "gain":
            stretch = sum(point_passes(m, attr, GAIN_STRETCH_DB) for m in results.values())
        out.append(
            RowVerdict(
                rid, label, bound, unit,
                "PASS" if results and n_pass == len(results) else "FAIL",
                n_pass, len(results), worst, binding, n_inv, stretch,
            )
        )
    return out


# --------------------------------------------------------------------------
# Report handling
# --------------------------------------------------------------------------


def point_key(corner: dict) -> Key:
    return (
        corner["process"],
        float(corner["temperature_c"]),
        float(corner["supply_v"]["vdd"]),
    )


def point_stem(k: Key) -> str:
    return f"{k[0]}_{k[1]:g}c_{k[2]:.2f}v"


def expected_keys(corners, temps, supplies) -> list[Key]:
    return [(c, float(t), float(v)) for c in corners for v in supplies for t in temps]


def analyse_ac_report(report: dict, want: list[Key]) -> tuple[dict[Key, Metrics], dict[Key, dict], list[str]]:
    """Per-point metrics + artifacts from a klt report; list of failure strings.

    Every expected point must be present exactly once with a finished
    simulation and a readable rawfile; anything else is a failed simulation.
    """
    problems: list[str] = []
    results: dict[Key, Metrics] = {}
    arts: dict[Key, dict] = {}
    seen: dict[Key, dict] = {}
    for c in report.get("corners", []):
        k = point_key(c)
        if k in seen:
            problems.append(f"duplicate result for {fmt_key(k)}")
        seen[k] = c
    for k in want:
        c = seen.get(k)
        if c is None:
            problems.append(f"missing result for {fmt_key(k)}")
            continue
        diag = "; ".join(d.get("message", "")[:200] for d in c.get("diagnostics", []) if d.get("severity") == "error")
        raw = (c.get("artifacts") or {}).get("raw")
        # klt grades a corner "error" if ANY measurement had no value (e.g. an
        # older batch runner dropping the `expr` cross-check); the point only
        # failed if the rawfile itself is missing.
        if not raw or not Path(raw).is_file():
            problems.append(f"simulation failed for {fmt_key(k)}: {diag or 'no rawfile retained'}")
            continue
        try:
            vec = parse_ascii_complex_raw(Path(raw).read_text())
            freq, h, vdiff = differential_gain(vec)
        except ValueError as exc:
            problems.append(f"malformed data for {fmt_key(k)}: {exc}")
            continue
        m = extract_metrics(freq, h)
        vals = {x["name"]: x.get("value") for x in c.get("measurements", [])}
        if m.valid:
            problems += crosscheck(k, vals, vec["v(vout)"][0], m)
        results[k] = m
        arts[k] = {
            "raw": raw,
            "log": (c.get("artifacts") or {}).get("log"),
            "deck": (c.get("artifacts") or {}).get("deck"),
            "freq": freq,
            "h": h,
            "vdiff": vdiff,
            "klt_meas": vals,
        }
    extra = set(seen) - set(want)
    if extra:
        problems.append(f"unexpected extra points: {sorted(extra)}")
    return results, arts, problems


XCHK_GAIN_DB = 0.05
XCHK_GBW_REL = 0.01
XCHK_PM_DEG = 1.0


def crosscheck(k: Key, vals: dict, vout0: complex, m: Metrics) -> list[str]:
    """Compare ngspice's own `.meas` results with the rawfile extraction.

    A `.meas` that produced no value is skipped (the extraction is the
    authority); a value that DISAGREES is a problem that blocks the record.
    """
    bad: list[str] = []
    first = vals.get("a_first_db")
    if first is not None:
        ours = 20 * math.log10(abs(vout0))
        if abs(first - ours) > XCHK_GAIN_DB:
            bad.append(f"{fmt_key(k)}: ngspice first-point |vout| {first:.3f} dB vs rawfile {ours:.3f} dB")
    g = vals.get("gbw_hz")
    if g is not None and abs(g / m.gbw_hz - 1) > XCHK_GBW_REL:
        bad.append(f"{fmt_key(k)}: ngspice GBW {g:.4g} Hz vs extraction {m.gbw_hz:.4g} Hz")
    ph = vals.get("ph_gbw_rad")
    if ph is not None:
        pm_meas = 180.0 + math.degrees(ph)
        if abs(((pm_meas - m.pm_deg) + 180) % 360 - 180) > XCHK_PM_DEG:
            bad.append(f"{fmt_key(k)}: ngspice PM {pm_meas:.2f} deg vs extraction {m.pm_deg:.2f} deg")
    return bad


def op_flags(report: dict, want: list[Key]) -> dict[Key, dict]:
    """Operating-point sanity per point from a klt report.

    `level` says what was actually checked: `devices` (vout offset + every
    device's saturation margin, from `expr` measurements -- the nominal local
    unit), `vout-only` (vout offset from `.meas` cards -- the fleet grid), or
    `none` (no operating point returned). A device check that was not done is
    never reported as passed.
    """
    out: dict[Key, dict] = {}
    for c in report.get("corners", []):
        k = point_key(c)
        vals = {x["name"]: x.get("value") for x in c.get("measurements", [])}
        vcm = c["supply_v"].get("vcm", k[2] / 2)
        flags: list[str] = []
        if vals.get("vout_v") is None:
            out[k] = {"vals": vals, "flags": ["operating point not returned"], "vcm": vcm, "level": "none"}
            continue
        if abs(vals["vout_v"] - vcm) > OP_VOUT_TOL_V:
            flags.append(f"vout {vals['vout_v']:.3f} V is {vals['vout_v'] - vcm:+.3f} V from VCM")
        level = "vout-only"
        if all(vals.get(f"satm_{d}") is not None for d in DEVICES):
            level = "devices"
            for d in DEVICES:
                if vals[f"satm_{d}"] < 0:
                    flags.append(f"{d.upper()[1:]} not saturated (|Vds|-|Vdsat| = {vals['satm_' + d] * 1e3:+.0f} mV)")
        out[k] = {"vals": vals, "flags": flags, "vcm": vcm, "level": level}
    for k in want:
        out.setdefault(
            k, {"vals": {}, "flags": ["operating point not returned"], "vcm": k[2] / 2, "level": "none"}
        )
    return out


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
# Single-unit studies: feedback isolation and negative controls (local)
# --------------------------------------------------------------------------


@dataclass
class StudyRun:
    name: str
    description: str
    metrics: Metrics | None
    verdicts: list[RowVerdict]
    error: str = ""
    files: dict[str, Path] = field(default_factory=dict)


def run_single(
    name: str, desc: str, pdk: Pdk, work: Path, *, dut_text=None, lfb=None, ibias_a=None,
    allow_missing: tuple[str, ...] = (),
) -> StudyRun:
    """One nominal-corner point, run as its own single-unit `klt sim`, LOCAL.

    Single-unit runs are exactly what the host rules allow locally; the 45-point
    grid never takes this path.
    """
    wd = work / name
    tb = materialise(wd, pdk, dut_text=dut_text, lfb=lfb, ibias_a=ibias_a, allow_missing=allow_missing)
    proc, temp, vdd = NOMINAL
    req = ac_request(tb, pdk, [proc], [temp], [vdd])
    try:
        rep = run_klt(req, wd / "out", "local", wd)
        res, arts, problems = analyse_ac_report(rep, [NOMINAL])
    except KltError as exc:
        return StudyRun(name, desc, None, [], error=str(exc))
    if problems or NOMINAL not in res:
        return StudyRun(name, desc, None, [], error="; ".join(problems))
    m = res[NOMINAL]
    a = arts[NOMINAL]
    return StudyRun(
        name, desc, m, judge({NOMINAL: m}),
        files={"raw": Path(a["raw"]), "log": Path(a["log"]) if a["log"] else None, "deck": Path(a["deck"]) if a["deck"] else None},
    )


def run_nominal_op(pdk: Pdk, work: Path) -> dict | None:
    """Device-level operating point of the nominal point: ONE local `op` unit.

    Gives the device saturation margins even when the grid's executing runner
    drops `expr` measurements. A single operating point is a single unit, so
    it may run locally.
    """
    wd = work / "nominal-op"
    tb = materialise(wd, pdk)
    proc, temp, vdd = NOMINAL
    try:
        rep = run_klt(op_request_nominal(tb, pdk, [proc], [temp], [vdd]), wd / "out", "local", wd)
    except KltError:
        return None
    res = op_flags(rep, [NOMINAL])
    return res.get(NOMINAL)


def run_studies(pdk: Pdk, work: Path) -> tuple[list[StudyRun], list[StudyRun]]:
    iso = [
        run_single(f"isolation-lfb-{v:g}", f"Lfb = Cfb = {v:g} (H, F)", pdk, work, lfb=v)
        for v in ISOLATION_VALUES
    ]
    iso.append(
        run_single(
            "isolation-inadequate",
            f"Lfb = Cfb = {ISOLATION_INADEQUATE:g} (deliberately inadequate isolation)",
            pdk, work, lfb=ISOLATION_INADEQUATE,
        )
    )
    controls = [
        run_single(
            "control-no-miller-cap",
            "Miller capacitor XCC removed from the DUT (all else nominal)",
            pdk, work, dut_text=strip_instance(load_dut_text(), "XCC"), allow_missing=("xcc",),
        ),
        run_single(
            "control-ibias-zero",
            "ibias driven at 0 A (no bias current; all else nominal)",
            pdk, work, ibias_a=0.0,
        ),
    ]
    return iso, controls


def study_failures(iso: list[StudyRun], controls: list[StudyRun], nominal: Metrics | None) -> list[str]:
    """Reasons the supporting studies do NOT behave as the method requires."""
    bad: list[str] = []
    stable = [r for r in iso if r.name != "isolation-inadequate"]
    for r in stable:
        if r.metrics is None or not r.metrics.valid:
            bad.append(f"{r.name}: expected a valid measurement ({r.error or r.metrics.reason})")
    ok = [r for r in stable if r.metrics and r.metrics.valid]
    if len(ok) >= 2:
        ref = ok[len(ok) // 2].metrics
        for r in ok:
            m = r.metrics
            if abs(m.dc_gain_db - ref.dc_gain_db) > ISOLATION_GAIN_TOL_DB:
                bad.append(f"{r.name}: DC gain moves {m.dc_gain_db - ref.dc_gain_db:+.3f} dB with isolation")
            if abs(m.pm_deg - ref.pm_deg) > ISOLATION_PM_TOL_DEG:
                bad.append(f"{r.name}: phase margin moves {m.pm_deg - ref.pm_deg:+.2f} deg with isolation")
            if abs(m.gbw_hz / ref.gbw_hz - 1) > ISOLATION_GBW_REL_TOL:
                bad.append(f"{r.name}: GBW moves {100 * (m.gbw_hz / ref.gbw_hz - 1):+.2f} % with isolation")
    inadequate = next((r for r in iso if r.name == "isolation-inadequate"), None)
    if inadequate and inadequate.metrics is not None and inadequate.metrics.valid:
        bad.append("isolation-inadequate: the plateau check failed to reject inadequate isolation")
    for r in controls:
        if r.error:
            bad.append(f"{r.name}: control did not simulate ({r.error[:200]})")
        elif all(v.verdict == "PASS" for v in r.verdicts):
            bad.append(f"{r.name}: negative control PASSED every row -- the checks cannot be trusted")
    return bad


# --------------------------------------------------------------------------
# Plot and record
# --------------------------------------------------------------------------


def build_plots(results: dict[Key, Metrics], plot_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for fname, keys, title in (
        ("bode_typical_27c_3p30v.png", [NOMINAL], "typical / 27 C / 3.30 V"),
        ("bode_all45.png", sorted(results), "all 45 PVT points"),
    ):
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(6.5, 7.5), sharex=True)
        for k in keys:
            m = results.get(k)
            if m is None or m.freq.size == 0:
                continue
            kw = {"linewidth": 1.2} if len(keys) == 1 else {"linewidth": 0.6, "alpha": 0.6}
            a1.semilogx(m.freq, m.db, **kw)
            a2.semilogx(m.freq, m.phase_deg, **kw)
            if len(keys) == 1 and m.valid:
                for ax in (a1, a2):
                    ax.axvline(m.gbw_hz, color="red", linestyle="--", linewidth=0.8)
        a1.axhline(0, color="gray", linewidth=0.8)
        a1.axhline(GAIN_MIN_DB, color="green", linewidth=0.6, linestyle=":")
        a2.axhline(-180 + PM_MIN_DEG, color="green", linewidth=0.6, linestyle=":")
        a1.set_ylabel("|Vout/Vdiff| (dB)")
        a2.set_ylabel("phase vs plateau (deg)")
        a2.set_xlabel("Frequency (Hz)")
        a1.set_title(f"Open-loop response, {title}")
        a1.grid(True, which="both", alpha=0.3)
        a2.grid(True, which="both", alpha=0.3)
        fig.tight_layout()
        fig.savefig(plot_dir / fname, dpi=110)
        plt.close(fig)
        names.append(fname)
    return names


def fmt_hz(v: float) -> str:
    return "n/a" if not math.isfinite(v) else f"{v / 1e6:.3f} MHz"


def build_record(
    *, record: str, stamp, pdk: Pdk, ngspice: str, klt_version: str, backend_desc: str,
    report: dict, results: dict[Key, Metrics], verdicts: list[RowVerdict],
    op: dict[Key, dict] | None, nominal_op: dict | None, op_report_remote: dict | None,
    iso: list[StudyRun], controls: list[StudyRun], study_bad: list[str], plots: list[str],
    dut_sha: str, xchk: tuple[int, int],
) -> str:
    L: list[str] = []
    add = L.append
    env = report.get("environment", {})
    remote = env.get("remote") or {}
    all_pass = all(v.verdict == "PASS" for v in verdicts)

    add(f"# Record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    add(
        "- **Claim**: open-loop DC gain, GBW (into CL = 2 pF) and phase margin of the "
        "**committed sized schematic** (`design/opamp_two_stage.sch`, export "
        "`design/netlist/opamp_two_stage.spice`, instantiated as `opamp_two_stage`) "
        "across the full ratified PVT grid -- 5 MOS corners x 3 temperatures x 3 supplies "
        "= 45 points -- each judged against its ratified bound in `spec/target-spec.md`. "
        "This is the first circuit-level spec-row evidence for the block; it supersedes the "
        "provisional, schematic-disconnected records (see Supersedes)."
    )
    add(
        "- **Overall**: "
        + ("every row passes at all 45 points." if all_pass
           else "**at least one ratified row MISSES** -- recorded as measured, the spec "
                "is untouched; the miss goes to a design follow-up issue that cites this record, "
                "not to a resize or a relaxed target here (see Verdicts).")
    )
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (via {pdk.source}); "
        f"klt reported `{(report.get('provenance') or {}).get('pdk', {}).get('version', 'n/a')}`")
    add(f"- **Tools**: ngspice (local: {ngspice}; engine as run by klt: "
        f"`{env.get('engine')} {env.get('engine_version')}`), klt `{klt_version}`")
    add(f"- **Execution**: {backend_desc}")
    if remote:
        add(f"  - `environment.remote`: provider `{remote.get('provider')}`, job id "
            f"`{remote.get('job_id')}`, instance type `{remote.get('instance_type')}`, "
            f"state `{remote.get('state')}`, runner klt `{remote.get('runner_klt_version')}` "
            f"vs client klt `{remote.get('client_klt_version')}` "
            f"(compatibility `{remote.get('runner_compatibility')}`)")
    if op_report_remote:
        add(f"  - operating-point job id `{op_report_remote.get('job_id')}`")
    if remote.get("runner_compatibility") == "mismatch":
        add(
            "  - The fleet runner's klt is older than the client's; the grid was submitted with "
            "`batch.runner_version_check: warn`. Options a 0.5.0 runner does not know may be "
            "silently ignored, so only request features that worked are relied on: corner "
            "bundles, `supply_v` alters, `.meas` cards, `keep_artifacts` + `waveforms` (rawfiles "
            "and per-point logs were returned for all 45 points). `expr` measurements are NOT "
            "used on the fleet (the 0.5.0 runner rejects them)."
        )
    nom_iso = next((r_ for r_ in iso if r_.name == "isolation-lfb-1e+09"), None)
    nm = results.get(NOMINAL)
    if nom_iso and nom_iso.metrics and nom_iso.metrics.valid and nm and nm.valid:
        add(
            f"  - Engine versions differ: the grid ran `{env.get('engine')} {env.get('engine_version')}` "
            f"on the fleet; the single-unit studies below ran locally under {ngspice.split(':')[0].strip()}. "
            "The same nominal point agrees: "
            f"grid {nm.dc_gain_db:.3f} dB / {nm.gbw_hz / 1e6:.4f} MHz / {nm.pm_deg:.3f} deg vs local "
            f"{nom_iso.metrics.dc_gain_db:.3f} dB / {nom_iso.metrics.gbw_hz / 1e6:.4f} MHz / "
            f"{nom_iso.metrics.pm_deg:.3f} deg."
        )
    add(f"  - ngspice-native `.meas` cross-checks (first-point |vout|, GBW, phase at GBW) returned for "
        f"{xchk[0]}/{xchk[1]} points and agreed with the rawfile extraction within "
        f"{XCHK_GAIN_DB} dB / {100 * XCHK_GBW_REL:g} % / {XCHK_PM_DEG:g} deg (a disagreement blocks the record).")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body "
        f"verbatim; normalised sha256 `{dut_sha}`), snapshotted in full in "
        f"`netlist-snapshots/{record}.spice`")
    add("- **Corner matrix run**:")
    add(f"  - Process (MOS): {', '.join(CORNERS)}")
    add("  - Temperature: " + ", ".join(f"{t:g} C" for t in TEMPS_C))
    add("  - Supply VDD: " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V) + " (VCM tracks VDD/2)")
    add(f"  - {len(results)} points; ibias = 10 uA into `ibias`; CL = 2 pF")
    add(
        "- **Passive-section policy**: every MOS corner is paired with the SAME "
        f"`{PASSIVE_SECTIONS[0]}` and `{PASSIVE_SECTIONS[1]}` sections (the sections the "
        "nominal DC check `design/check_dc_op.py` uses). The 45-point grid therefore varies "
        "MOS corner, temperature and supply; it does **not** cover independent corners of the "
        "nulling resistor RZ or the Miller capacitor CC, and no claim of passive-corner "
        "coverage is made. The committed RZ and CC are used as drawn; phase-margin "
        "implications are reported, nothing is retuned."
    )
    add("- **Statistical convention**: N/A -- deterministic process/temperature/supply corners; no mismatch or Monte Carlo.")
    add("")

    add("## Extraction method")
    add("")
    add(
        f"One `.ac dec {AC_PPD:g} {AC_FSTART:g} {AC_FSTOP:g}` sweep per point, driven on `vinp` "
        "(1 V AC). The loop is DC-closed and AC-open with a huge inductor `Lfb` from `vout` "
        "to `vinn` (2*pi*f*Lfb >= ~6e8 ohm even at 0.1 Hz) and a huge `Cfb` from `vinn` to "
        "ground; DC bias is therefore the unity-gain-buffer operating point. Gain is "
        "`v(vout)/(v(vinp)-v(vinn))` using the actual differential phasor."
    )
    add("")
    add(
        f"- **DC gain** = mean dB over the lowest decade ({AC_FSTART:g}-{AC_FSTART * PLATEAU_DECADE_HI:g} Hz), "
        f"valid only if that band is flat to {PLATEAU_TOL_DB} dB and its phase is within "
        f"{POLARITY_TOL_DEG:g} deg of 0 (vinp non-inverting). Not the peak magnitude."
    )
    add("- **Phase** is unwrapped (`numpy.unwrap`) over the full sweep and referenced to the plateau phase.")
    add("- **GBW** = first descending 0 dB crossing, interpolated linearly in (log10 f, dB); "
        "**PM** = 180 deg + unwrapped phase at that frequency (same interpolation).")
    add("- **Never passes**: missing crossing, any further 0 dB crossing (peaking), non-flat plateau, "
        "wrong polarity, malformed or non-finite data, failed simulation.")
    add("")

    add("## Verdicts (ratified rows, `spec/target-spec.md` Sec.2)")
    add("")
    add("| Row | Ratified bound | Verdict | Points passing | Worst value | Binding corner (process / T / VDD) |")
    add("|---|---|---|---|---|---|")
    for v in verdicts:
        if v.row == "gbw":
            bound, worst = f">= {v.bound / 1e6:g} MHz", fmt_hz(v.worst_value)
        else:
            bound = f">= {v.bound:g} {v.unit}"
            worst = "n/a" if not math.isfinite(v.worst_value) else f"{v.worst_value:.2f} {v.unit}"
        binding = fmt_key(v.binding) if v.binding else "n/a"
        add(f"| {v.label} | {bound} | **{v.verdict}** | {v.n_pass}/{v.n_total} | {worst} | {binding} |")
    g = next(v for v in verdicts if v.row == "gain")
    add("")
    add(f"- **Stretch (reported separately, not a mandatory row)**: open-loop DC gain >= {GAIN_STRETCH_DB:g} dB "
        f"holds at {g.stretch_pass}/{g.n_total} points.")
    n_inv = sum(not m.valid for m in results.values())
    add(f"- Invalid measurements (count as failing every row): {n_inv}/{len(results)}.")
    add("")

    add(f"## All {len(results)} points")
    add("")
    add("| Process | T (C) | VDD (V) | DC gain (dB) | GBW (MHz) | PM (deg) | gain 60 | gain 70 (stretch) | GBW 10 MHz | PM 60 | note |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    yn = lambda b: "ok" if b else "FAIL"  # noqa: E731
    for k in sorted(results, key=lambda k: (CORNERS.index(k[0]), k[2], k[1])):
        m = results[k]
        if m.valid:
            add(
                f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | {m.dc_gain_db:.2f} | {m.gbw_hz / 1e6:.3f} | "
                f"{m.pm_deg:.2f} | {yn(m.dc_gain_db >= GAIN_MIN_DB)} | "
                f"{'ok' if m.dc_gain_db >= GAIN_STRETCH_DB else 'no'} | "
                f"{yn(m.gbw_hz >= GBW_MIN_HZ)} | {yn(m.pm_deg >= PM_MIN_DEG)} | |"
            )
        else:
            add(f"| `{k[0]}` | {k[1]:g} | {k[2]:.2f} | n/a | n/a | n/a | FAIL | no | FAIL | FAIL | INVALID: {m.reason} |")
    add("")

    if op or nominal_op:
        add("## Operating-point and polarity verification")
        add("")
        add(
            "Polarity: every valid point has a plateau phase within "
            f"{POLARITY_TOL_DEG:g} deg of 0 (checked in the extraction; max |plateau phase| over "
            "valid points: "
            f"{max((abs((m.plateau_phase_deg + 180) % 360 - 180) for m in results.values() if m.valid), default=float('nan')):.2f} deg), "
            "i.e. `vinp` is the non-inverting input. Bias: a separate `op` analysis of the same "
            "testbench (loop DC-closed, unity-gain-buffer operating point) is checked for "
            f"|vout - VCM| <= {OP_VOUT_TOL_V * 1e3:g} mV and, where available, every DUT MOSFET "
            "saturated (|Vds| >= |Vdsat|). These are recorded sanity flags, not spec rows."
        )
        add("")
        if op:
            flagged = {k: v for k, v in op.items() if v["flags"]}
            levels = sorted({v["level"] for v in op.values()})
            add(f"- Grid `op` analysis (45 points) check level(s): {', '.join(levels)} "
                "(`devices` = vout offset + all 8 device saturation margins; `vout-only` = vout offset "
                "from `.meas dc` cards, which every runner version supports -- device saturation "
                "is NOT checked at the 45 points, only at the nominal local unit below; "
                "`none` = no operating point returned).")
            add(f"- Points with an operating-point flag: {len(flagged)}/{len(op)}")
            for k, v in sorted(flagged.items()):
                add(f"  - {fmt_key(k)}: " + "; ".join(v["flags"]))
        else:
            add("- The grid `op` request did not complete; no 45-point operating-point check is claimed.")
        if nominal_op and nominal_op["level"] == "devices":
            vv = nominal_op["vals"]
            add(f"- Nominal point (local single `op` unit): vout = {vv['vout_v']:.4f} V "
                f"(VCM {nominal_op['vcm']:.3f} V), tail current {vv['itail_a'] * 1e6:.2f} uA, "
                f"output-stage current {vv['iout_a'] * 1e6:.2f} uA; min saturation margin "
                f"{min(vv['satm_' + d] for d in DEVICES) * 1e3:.0f} mV; flags: "
                f"{'; '.join(nominal_op['flags']) or 'none'}.")
        add("")

    add("## Feedback-isolation study (nominal corner, local single-unit runs)")
    add("")
    add(
        "Demonstrates the measured plateau is a property of the amplifier, not of the DC "
        "feedback network: the same point is re-measured with the isolation inductor/capacitor "
        "(`Lfb = Cfb`) varied by two decades, plus a deliberately inadequate value that the "
        "plateau check must reject."
    )
    add("")
    add("| Run | Lfb = Cfb | valid | DC gain (dB) | GBW (MHz) | PM (deg) | plateau spread (dB) | note |")
    add("|---|---|---|---|---|---|---|---|")
    for r in iso:
        m = r.metrics
        if m is None:
            add(f"| `{r.name}` | {r.description} | n/a | | | | | error: {r.error[:120]} |")
        elif m.valid:
            add(f"| `{r.name}` | {r.description} | yes | {m.dc_gain_db:.3f} | {m.gbw_hz / 1e6:.4f} | {m.pm_deg:.3f} | {m.plateau_spread_db:.4f} | |")
        else:
            add(f"| `{r.name}` | {r.description} | **no** | | | | {m.plateau_spread_db:.4f} | rejected: {m.reason} |")
    add("")
    add(f"Stability tolerances: gain {ISOLATION_GAIN_TOL_DB} dB, PM {ISOLATION_PM_TOL_DEG} deg, GBW {100 * ISOLATION_GBW_REL_TOL:g} %.")
    add("")

    add("## Negative controls (recorded separately from the grid evidence)")
    add("")
    add("Single-unit local runs at the nominal point (typical / 27 C / 3.30 V). Each simulates "
        "successfully; the checks are required to FAIL, otherwise the driver exits non-zero.")
    add("")
    add("| Control | Condition | Simulated | DC gain (dB) | GBW (MHz) | PM (deg) | Gain | GBW | PM | Result |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for r in controls:
        if r.error or r.metrics is None:
            add(f"| `{r.name}` | {r.description} | no | | | | | | | ERROR: {r.error[:150]} |")
            continue
        m = r.metrics
        by = {v.row: v.verdict for v in r.verdicts}
        failing = [k for k, v in by.items() if v == "FAIL"]
        res = "checks FAIL as required (" + ", ".join(failing) + ")" if failing else "ALL PASSED -- CHECKS UNTRUSTWORTHY"
        dc = f"{m.dc_gain_db:.2f}" if math.isfinite(m.dc_gain_db) else "n/a"
        gb = f"{m.gbw_hz / 1e6:.3f}" if math.isfinite(m.gbw_hz) else "n/a"
        pm = f"{m.pm_deg:.2f}" if math.isfinite(m.pm_deg) else "n/a"
        note = "" if m.valid else f" [invalid: {m.reason}]"
        add(f"| `{r.name}` | {r.description} | yes | {dc} | {gb} | {pm} | {by['gain']} | {by['gbw']} | {by['pm']} | {res}{note} |")
    add("")
    if study_bad:
        add("**STUDY PROBLEMS** (driver exits non-zero):")
        for s in study_bad:
            add(f"- {s}")
        add("")

    add("## Plots")
    add("")
    for p in plots:
        add(f"- `sim/gain-gbw-pm/records/{record}-plots/{p}`")
    add("")
    add("## Links")
    add("")
    add("- Testbench: `sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice`")
    add("- Run script: `sim/gain-gbw-pm/run_gain_gbw_pm.py`; guard/extraction tests: `sim/gain-gbw-pm/test_gain_gbw_pm.py`")
    add(f"- Netlist snapshot (DUT contents + testbench + conditions): `sim/gain-gbw-pm/netlist-snapshots/{record}.spice`")
    add(f"- Per-point logs, decks and data (all 45), the sanitised klt report and the control runs: `sim/gain-gbw-pm/corners/{record}/`")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #38)")
    add("- **Supersedes**: the provisional records `20260915-221407-1bb9a74` and "
        "`20261003-030340-7bd8071` as evidence for these rows. They are retained unchanged "
        "(append-only); they measured a hand-built, schematic-disconnected netlist and made no pass/fail claim.")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def claim_record_paths(base: Path, record: str) -> dict[str, Path]:
    """Output paths for one record under `base` (this experiment's directory).

    Raises FileExistsError if ANY of them already exists: records, snapshots
    and corner data are append-only evidence and a re-run mints a new id.
    """
    paths = {
        "record": base / "records" / f"{record}.md",
        "plots": base / "records" / f"{record}-plots",
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
    """`batch.*` request fields from CLI flags (read only by the batch backend)."""
    block: dict = {}
    if args.batch_runner_version_check:
        block["runner_version_check"] = args.batch_runner_version_check
    if args.batch_capacity_wait_s is not None:
        block["capacity_wait_s"] = args.batch_capacity_wait_s
    return block


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="gainpm-smoke-") as scratch:
        run = run_single("smoke", "nominal", pdk, Path(scratch))
    if run.error or run.metrics is None:
        print(f"SMOKE TEST FAILED: {run.error}")
        return 1
    m = run.metrics
    if not m.valid:
        print(f"SMOKE TEST FAILED: invalid measurement: {m.reason}")
        return 1
    print(f"  gain={m.dc_gain_db:.2f} dB GBW={fmt_hz(m.gbw_hz)} PM={m.pm_deg:.2f} deg")
    print("smoke test OK (measurement valid; spec verdicts are the full run's job)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one nominal point, local, no record")
    ap.add_argument("--backend", help="klt execution backend for the 45-point grid "
                    "(default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a ratified row misses")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None,
                    help="forward batch.runner_version_check (only meaningful on the batch backend)")
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None,
                    help="forward batch.capacity_wait_s: wait this long for fleet capacity "
                    "instead of failing the submit immediately")
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit up to N times when the batch submit is refused for fleet "
                    "capacity / the shared concurrency cap (never changes backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    want = expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    claim_record_paths(HERE, record)  # fail early if this id was already used
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: {len(want)} points, PDK={pdk.path} (open_pdks {pdk.version}), klt {kver}")

    with tempfile.TemporaryDirectory(prefix="gainpm-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        req = ac_request(tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
        req["batch"] = batch_block(args)
        try:
            report = run_klt_retrying(
                req, work / "grid" / "out", args.backend, work / "grid",
                retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
            )
        except KltError as exc:
            print(f"ERROR: the grid request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        results, arts, problems = analyse_ac_report(report, want)
        if problems:
            print("ERROR: the 45-point grid did not complete cleanly; NO RECORD WRITTEN:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 2
        remote = (report.get("environment") or {}).get("remote") or {}
        backend_desc = (
            f"`klt sim` backend `{remote.get('provider', 'local')}`"
            + (f" ({'Spot' if remote.get('spot') else 'on-demand'} {remote.get('instance_type')})" if remote else "")
            + "; the 45 points are ONE `klt sim` corner-matrix request"
        )

        # Operating point + polarity (same grid, `op` analysis).
        op = None
        op_remote = None
        try:
            op_tb = materialise(work / "op", pdk)
            oreq = op_request_grid(op_tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V)
            oreq["batch"] = batch_block(args)
            oreport = run_klt_retrying(
                oreq, work / "op" / "out", args.backend, work / "op",
                retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
            )
            op = op_flags(oreport, want)
            op_remote = (oreport.get("environment") or {}).get("remote")
        except KltError as exc:
            print(f"WARNING: operating-point request failed: {exc}", file=sys.stderr)

        nominal_op = run_nominal_op(pdk, work)
        iso, controls = run_studies(pdk, work)
        verdicts = judge(results)
        study_bad = study_failures(iso, controls, results.get(NOMINAL))

        corners_dir = HERE / "corners" / record
        corners_dir.mkdir(parents=True, exist_ok=False)
        for k, a in arts.items():
            stem = point_stem(k)
            if a["log"]:
                shutil.copyfile(a["log"], corners_dir / f"{stem}.log")
            if a["deck"]:
                shutil.copyfile(a["deck"], corners_dir / f"{stem}.cir")
            np.savetxt(
                corners_dir / f"{stem}.dat",
                np.column_stack([a["freq"], a["h"].real, a["h"].imag, a["vdiff"].real, a["vdiff"].imag]),
                header="freq_hz re(vout/vdiff) im(vout/vdiff) re(vdiff) im(vdiff)",
            )
        (corners_dir / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))
        if op is not None:
            (corners_dir / "klt-op-report.json").write_text(json.dumps(sanitise_report(oreport), indent=1))
        cdir = corners_dir / "controls"
        cdir.mkdir()
        for r in iso + controls:
            for kind, src in r.files.items():
                if src and Path(src).is_file():
                    # `.raw` is gitignored repo-wide; keep the ASCII rawfile as evidence.
                    ext = {"raw": "ac.rawascii", "log": "log", "deck": "cir"}[kind]
                    shutil.copyfile(src, cdir / f"{r.name}.{ext}")

        dut_text = load_dut_text()
        import hashlib

        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        snap = HERE / "netlist-snapshots"
        snap.mkdir(parents=True, exist_ok=True)
        deck0 = ""
        first = arts.get(NOMINAL)
        if first and first["deck"]:
            deck0 = Path(first["deck"]).read_text()
        (snap / f"{record}.spice").write_text(
            "\n".join(
                [
                    f"* netlist snapshot for record {record} (issue #38)",
                    "* Reproduces the measured design: DUT contents, testbench, conditions.",
                    "* ---- conditions: klt sim request (grid) ----",
                    *("* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
                    "",
                    "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised "
                    "(file opamp_two_stage.dut.spice) ----",
                    dut_text,
                    "* ---- testbench: sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice (verbatim) ----",
                    TESTBENCH.read_text(),
                    "* ---- klt-generated deck of the nominal point (corner.cir) ----",
                    *("* | " + ln for ln in deck0.splitlines()),
                    "",
                ]
            )
        )

        plots = build_plots(results, HERE / "records" / f"{record}-plots")
        md = build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_version=kver,
            backend_desc=backend_desc, report=report, results=results, verdicts=verdicts,
            op=op, nominal_op=nominal_op, op_report_remote=op_remote, iso=iso, controls=controls,
            study_bad=study_bad, plots=plots, dut_sha=dut_sha,
            xchk=(
                sum(all(a["klt_meas"].get(n) is not None for n in ("a_first_db", "gbw_hz", "ph_gbw_rad")) for a in arts.values()),
                len(arts),
            ),
        )
        out = HERE / "records" / f"{record}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)

    print(f"wrote {out}")
    for v in verdicts:
        print(f"  {v.label}: {v.verdict} ({v.n_pass}/{v.n_total}), worst "
              f"{v.worst_value:.4g} {v.unit} at {fmt_key(v.binding) if v.binding else 'n/a'}")
    if study_bad:
        print("STUDY PROBLEMS:")
        for s in study_bad:
            print(f"  - {s}")
        return 1
    if args.strict and any(v.verdict == "FAIL" for v in verdicts):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
