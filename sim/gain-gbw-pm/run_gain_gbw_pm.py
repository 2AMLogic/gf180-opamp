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
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

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
    run_klt,
    run_klt_retrying,
    sanitise_report,
    stage_workdir,
)

from harness import load_config_module  # noqa: E402

# Loaded by path, NOT `import measurement_config`: sibling drivers load this
# driver through `harness.load_sibling`, which does not put this directory on
# sys.path, and every experiment has its own `measurement_config.py`, so a bare
# top-level name would either fail to resolve or collide in sys.modules.
# `load_config_module` keys the module by its path, so the other experiments'
# configuration modules (which take the shared grid axes from this one) get the
# very same module object the gain driver uses (issue #89).
mc = load_config_module(HERE / "measurement_config.py")
# Single source for the fingerprinted constants.
AC_FSTART = mc.AC_FSTART
AC_FSTOP = mc.AC_FSTOP
AC_PPD = mc.AC_PPD
CORNERS = mc.CORNERS
MODEL_LIB = mc.MODEL_LIB
PASSIVE_SECTIONS = mc.PASSIVE_SECTIONS
SUPPLIES_V = mc.SUPPLIES_V
TEMPS_C = mc.TEMPS_C

TESTBENCH = HERE / "testbench" / "tb_gain_gbw_pm.spice"

# --------------------------------------------------------------------------
# Grid and conditions (spec/target-spec.md Sec.1)
# --------------------------------------------------------------------------

#: MOS corners; each is bundled with the SAME typical resistor and MIM
#: capacitor sections. The grid varies MOS corner, temperature and supply;
#: it does NOT claim independent passive (RZ / CC) corner coverage -- that was
#: not run. See the record's "Passive-section policy".

#: Opt-in passive-corner axis (`--passive-corners`, issue #70). Names map to the
#: PDK's own sections in `sm141064.ngspice` (verified against the installed
#: PDK: `.LIB res_typical|res_ss|res_ff`, `.LIB mimcap_typical|mimcap_ss|mimcap_ff`).
#: "worst" = the PDK `_ss` section, "best" = `_ff`. Which is worse for a given
#: row is a RESULT, not an assumption: the record reports every combination.
#: res_ss: ppolyf_u_1k = 1000+200 ohm; res_ff = 1000-200. mimcap_ss:
#: mim_corner_2p0fF = 1.1; mimcap_ff = 0.9 (cap_mim_2f0_m4m5_noshield scales
#: with mim_corner_2p0fF).
# Shared with every other passive-corner side study (issue #97): one copy.
from passive_corners import (  # noqa: E402,F401
    MIM_FACTOR,
    PASSIVE_LEVELS,
    PASSIVE_POINTS,
    RES_FACTOR,
    apply_passive_matrix,
    passive_combos,
    passive_expected_keys,
    passive_name,
    passive_process_axis,
    passive_sections,
    split_passive_key,
)


NOMINAL = ("typical", 27.0, 3.30)

#: `.ac dec` sweep. Starts at 0.1 Hz so the lowest decade is a plateau for
#: every corner (dominant pole is hundreds of Hz), ends well above any GBW.

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
# DUT: the committed export, wrapper-normalised (`harness.load_dut_text`)
# --------------------------------------------------------------------------


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
    tb = stage_workdir(
        work, pdk, TESTBENCH.read_text(), guard_tb=guard_testbench,
        guard_dut=lambda d: guard_dut(d, allow_missing=allow_missing), dut_text=dut_text,
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
# klt requests (run via `harness.run_klt` / `harness.run_klt_retrying`)
# --------------------------------------------------------------------------


def process_axis(corners: list[str]) -> list[dict]:
    return [{"name": c, "sections": [c, *PASSIVE_SECTIONS]} for c in corners]


class VcmRequestError(ValueError):
    """An invalid fixed-common-mode request; raised BEFORE any submission."""


def validate_vcm_fixed(vcm_v, supplies=None) -> float | None:
    """Validate an explicit fixed common-mode request (issue #125).

    `None` is the default VDD/2-tracking policy. Otherwise the value must be a
    finite number, strictly positive and strictly below every supply point (a
    common mode at or above VDD is not a bias point of this amplifier).
    """
    if vcm_v is None:
        return None
    if isinstance(vcm_v, bool) or not isinstance(vcm_v, (int, float)) or not math.isfinite(vcm_v):
        raise VcmRequestError(f"fixed VCM must be a finite number of volts, got {vcm_v!r}")
    if vcm_v <= 0:
        raise VcmRequestError(f"fixed VCM must be > 0 V, got {vcm_v:g}")
    lo = min(SUPPLIES_V if supplies is None else supplies)
    if vcm_v >= lo:
        raise VcmRequestError(f"fixed VCM {vcm_v:g} V must be below every supply point (lowest {lo:g} V)")
    return float(vcm_v)


def vcm_axis(supplies, vcm_fixed: float | None = None) -> list[float]:
    """The `vcm` values swept by index alongside `vdd`: VDD/2 by default, or
    the one fixed value at every supply point."""
    vcm_fixed = validate_vcm_fixed(vcm_fixed, supplies)
    if vcm_fixed is None:
        return [round(v / 2, 6) for v in supplies]
    return [vcm_fixed for _ in supplies]


def ac_request(netlist: Path, pdk: Pdk, corners, temps, supplies, vcm_fixed: float | None = None) -> dict:
    return {
        "netlist": str(netlist),
        "engine": "ngspice",
        "models": {"pdk": pdk.variant, "lib": MODEL_LIB},
        "corners": {
            "process": process_axis(list(corners)),
            # vdd and vcm sweep together by index: VCM tracks VDD/2 (default),
            # or stays at one explicit value (opt-in `--vcm-fixed`, issue #125).
            "supply_v": {"vdd": list(supplies), "vcm": vcm_axis(supplies, vcm_fixed)},
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


def op_request_grid(netlist: Path, pdk: Pdk, corners, temps, supplies, vcm_fixed: float | None = None) -> dict:
    """Operating point of every grid point as a one-step DC sweep of `Ibias`
    (`.meas dc ... AT=` works on any runner; there is no `.meas op`)."""
    req = ac_request(netlist, pdk, corners, temps, supplies, vcm_fixed)
    req["analysis"] = {"kind": "dc", "args": "Ibias 10u 11u 1u"}
    req["measurements"] = [
        {"name": "vout_v", "spice": ".meas dc vout_v FIND v(vout) AT=10u", "unit": "V"},
        {"name": "vinn_v", "spice": ".meas dc vinn_v FIND v(vinn) AT=10u", "unit": "V"},
        {"name": "ivdd_a", "spice": ".meas dc ivdd_a FIND i(vdd) AT=10u", "unit": "A"},
    ]
    req["options"] = {"timeout_s": 300}
    return req


def op_request_nominal(netlist: Path, pdk: Pdk, corners, temps, supplies, vcm_fixed: float | None = None) -> dict:
    """Device-level operating point (needs `expr`; local single unit only)."""
    req = ac_request(netlist, pdk, corners, temps, supplies, vcm_fixed)
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


def latest_gain_dir() -> Path | None:
    """Newest gain-bench corner directory holding a full 45-point dataset.

    Counts only per-point data files (`<process>_<T>c_<vdd>v.dat`), so an
    auxiliary `.dat` in a record directory cannot masquerade as a point
    (issue #68: one copy, shared by the noise/CMRR/PSRR cross-checks).
    """
    base = HERE / "corners"
    if not base.is_dir():
        return None
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        if len(list(d.glob("*_*c_*v.dat"))) >= 45:
            return d
    return None


def load_gain_bench(gdir: Path, k: Key) -> np.ndarray | None:
    """The committed (freq, re, im, ...) table for point `k`, or None if absent."""
    p = gdir / f"{point_stem(k)}.dat"
    if not p.is_file():
        return None
    return np.loadtxt(p)


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
    allow_missing: tuple[str, ...] = (), vcm_fixed: float | None = None,
) -> StudyRun:
    """One nominal-corner point, run as its own single-unit `klt sim`, LOCAL.

    Single-unit runs are exactly what the host rules allow locally; the 45-point
    grid never takes this path.
    """
    wd = work / name
    tb = materialise(wd, pdk, dut_text=dut_text, lfb=lfb, ibias_a=ibias_a, allow_missing=allow_missing)
    proc, temp, vdd = NOMINAL
    req = ac_request(tb, pdk, [proc], [temp], [vdd], vcm_fixed)
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


def run_nominal_op(pdk: Pdk, work: Path, vcm_fixed: float | None = None) -> dict | None:
    """Device-level operating point of the nominal point: ONE local `op` unit.

    Gives the device saturation margins even when the grid's executing runner
    drops `expr` measurements. A single operating point is a single unit, so
    it may run locally.
    """
    wd = work / "nominal-op"
    tb = materialise(wd, pdk)
    proc, temp, vdd = NOMINAL
    try:
        rep = run_klt(op_request_nominal(tb, pdk, [proc], [temp], [vdd], vcm_fixed), wd / "out", "local", wd)
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
    for ln in mc.fingerprint_lines(TESTBENCH.read_text()):
        add(ln)
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

    L.extend(mc.inputs_section(TESTBENCH.read_text()))

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


# --------------------------------------------------------------------------
# Opt-in passive-corner study (issue #70; DR-3 section (d) obligation)
# --------------------------------------------------------------------------


def passive_ac_request(netlist: Path, pdk: Pdk, points=PASSIVE_POINTS) -> dict:
    """ONE corner-matrix request: MOS x passive-combo x T x VDD, with the
    cross-product cells that are not study points removed via klt's `exclude`."""
    temps = sorted({p[1] for p in points})
    vdds = sorted({p[2] for p in points})
    return apply_passive_matrix(ac_request(netlist, pdk, [], temps, vdds), points)


def passive_summary(results: dict[Key, Metrics], points=PASSIVE_POINTS) -> dict:
    """Per study point: metrics by (res, mim) level, deltas vs all-typical
    passives, and the relative slew factor (Itail / CC ~ 1 / MIM factor)."""
    out: dict = {}
    for (m, t, v) in points:
        rows = {}
        base = results.get((passive_name(m, "typical", "typical"), t, v))
        for r, c in passive_combos():
            mt = results.get((passive_name(m, r, c), t, v))
            if mt is None:
                continue
            ok = base is not None and mt.valid and base.valid
            rows[(r, c)] = {
                "metrics": mt,
                "d_pm": mt.pm_deg - base.pm_deg if ok else float("nan"),
                "d_gbw_pct": 100 * (mt.gbw_hz / base.gbw_hz - 1) if ok else float("nan"),
                "slew_rel": 1.0 / MIM_FACTOR[c],
            }
        out[(m, t, v)] = rows
    return out


def _passive_lab(k: Key) -> str:
    mos, r, c = split_passive_key(k)
    return f"{mos} / {k[1]:g} C / {k[2]:.2f} V, RZ {r}, CC {c}"


def passive_verdict_lines(results: dict[Key, Metrics]) -> list[str]:
    """The record's PM / GBW verdict bullets.

    The verdict word covers the whole study (PASS only if every cell meets
    the bound); the counts are spelled out as "passes at n/N (fails at N-n)"
    so a pass count is never printed next to the word FAIL (issue #70
    review: the first passive record's header read "FAIL at 7/27" where 7
    was the number of PASSING cells). An INVALID cell counts as failing.
    """
    out: list[str] = []
    allm = [(k, m) for k, m in results.items() if m.valid]
    if not allm:
        return out
    total = len(results)
    worst_pm = min(allm, key=lambda t: t[1].pm_deg)
    best_pm = max(allm, key=lambda t: t[1].pm_deg)
    worst_gbw = min(allm, key=lambda t: t[1].gbw_hz)
    n_pm = sum(m.valid and m.pm_deg >= PM_MIN_DEG for m in results.values())
    n_gbw = sum(m.valid and m.gbw_hz >= GBW_MIN_HZ for m in results.values())

    def counts(n: int) -> str:
        return f"**{'PASS' if n == total else 'FAIL'}** -- passes at {n}/{total} study cells (fails at {total - n}/{total})"

    out.append(f"  - Phase margin >= {PM_MIN_DEG:g} deg: {counts(n_pm)}; "
               f"worst {worst_pm[1].pm_deg:.2f} deg at {_passive_lab(worst_pm[0])}; "
               f"best {best_pm[1].pm_deg:.2f} deg at {_passive_lab(best_pm[0])}.")
    out.append(f"  - GBW >= {GBW_MIN_HZ / 1e6:g} MHz: {counts(n_gbw)}; "
               f"worst {fmt_hz(worst_gbw[1].gbw_hz)} at {_passive_lab(worst_gbw[0])}.")
    return out


def build_passive_record(*, record, stamp, pdk, ngspice, klt_version, backend_desc, report,
                         results, dut_sha, base_record, supersedes: str | None = None,
                         supersede_note: str = "", findings: str = "") -> str:
    """Markdown for a passive-corner record.

    `supersedes` (with `supersede_note`) marks a record regenerated from an
    earlier record's committed data (`--recompute-passive`): the corner data
    and netlist snapshot stay under the superseded id and are referenced, not
    copied. `findings` is appended verbatim before the Artifacts section.
    """
    L: list[str] = []
    add = L.append
    remote = (report.get("environment") or {}).get("remote") or {}
    summ = passive_summary(results)
    pm_ok = all(m.valid and m.pm_deg >= PM_MIN_DEG for m in results.values())
    gbw_ok = all(m.valid and m.gbw_hz >= GBW_MIN_HZ for m in results.values())
    data_id = supersedes or record

    add(f"# gain/GBW/PM passive-corner study (RZ x CC) -- record {record}")
    add("")
    add(f"- **Date (UTC)**: {stamp:%Y-%m-%d %H:%M:%S}")
    if supersedes:
        add(f"- **Supersedes**: `{supersedes}` -- {supersede_note}")
    add("- **Issue**: #70 (DR-3 section (d) RZ PVT-tracking quantification; tracker #7 item 5)")
    add(f"- **Measured against**: the committed sized schematic; the default 45-point grid record is `{base_record}` (unchanged, typical passives)")
    add("- **Verdict (spec bounds NOT edited here)**:")
    L.extend(passive_verdict_lines(results))
    add("  - Slew = Itail/CC: Itail is set by the 10 uA bias mirror (passive-independent to first order), "
        "so slew scales as 1/CC: CC worst (x1.10) -> slew x0.909; CC best (x0.90) -> slew x1.111 "
        "(column `slew rel.` below; an analytic scaling, not a transient measurement -- the slew "
        "bench is `sim/slew-swing-power/`).")
    if not (pm_ok and gbw_ok):
        add("  - **Reachability**: a ratified row misses at one or more passive corners (see tables). "
            "No bound is relaxed here; if the miss survives the design repair for the nominal PM failure, "
            "the remedy is a superseding decision record (DR-3 section (d)).")
    add("")
    add("## Conditions")
    add("")
    add(f"- **PDK**: {pdk.path} (open_pdks {pdk.version}); ngspice {ngspice}; klt {klt_version}")
    add(f"- **Execution**: {backend_desc}")
    if remote:
        add(f"  - environment.remote: `{json.dumps(remote, sort_keys=True)}`")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (normalised sha256 `{dut_sha}`); snapshot `netlist-snapshots/{data_id}.spice`")
    add("- **Points**: " + "; ".join(f"{m} / {t:g} C / {v:.2f} V" for m, t, v in PASSIVE_POINTS)
        + f", each x 3 RZ levels x 3 CC levels = {len(results)} cells; ibias = 10 uA, CL = 2 pF, same `.ac` and extraction as the 45-point grid.")
    add("- **Passive-section policy (swept)**: RZ (`ppolyf_u_1k`) uses `res_typical` / `res_ff` (best, 1000-200 ohm = 0.8x) / "
        "`res_ss` (worst, 1000+200 ohm = 1.2x); CC (`cap_mim_2f0_m4m5_noshield`) uses `mimcap_typical` / `mimcap_ff` "
        "(best, mim_corner_2p0fF = 0.9) / `mimcap_ss` (worst, 1.1). All nine RZ x CC combinations are run independently "
        "at each point. Section names verified in `libs.tech/ngspice/sm141064.ngspice`. \"best\"/\"worst\" are the PDK "
        "ff/ss labels, not a claim about which is worse for PM; the tables decide.")
    add("")
    add("## Results")
    for (m, t, v), rows in summ.items():
        add("")
        add(f"### {m} / {t:g} C / {v:.2f} V")
        add("")
        add("| RZ | CC | PM (deg) | dPM vs typ. (deg) | GBW (MHz) | dGBW (%) | slew rel. | PM >= 60 | GBW >= 10 MHz |")
        add("|---|---|---|---|---|---|---|---|---|")
        for (r, c), d in rows.items():
            mt = d["metrics"]
            if not mt.valid:
                add(f"| {r} | {c} | INVALID: {mt.reason} | | | | | FAIL | FAIL |")
                continue
            add(f"| {r} | {c} | {mt.pm_deg:.2f} | {d['d_pm']:+.2f} | {mt.gbw_hz / 1e6:.3f} | {d['d_gbw_pct']:+.1f} | "
                f"{d['slew_rel']:.3f} | {'PASS' if mt.pm_deg >= PM_MIN_DEG else 'FAIL'} | "
                f"{'PASS' if mt.gbw_hz >= GBW_MIN_HZ else 'FAIL'} |")
    add("")
    add("## Sensitivity (one passive moved, the other typical; deltas vs both typical)")
    add("")
    add("| Point | dPM: RZ best / worst (deg) | dPM: CC best / worst (deg) | dGBW: CC best / worst (%) |")
    add("|---|---|---|---|")
    for (m, t, v), rows in summ.items():
        def g(r, c, f):
            d = rows.get((r, c))
            return f"{d[f]:+.2f}" if d and math.isfinite(d[f]) else "n/a"
        add(f"| {m} / {t:g} C / {v:.2f} V | {g('best','typical','d_pm')} / {g('worst','typical','d_pm')} | "
            f"{g('typical','best','d_pm')} / {g('typical','worst','d_pm')} | "
            f"{g('typical','best','d_gbw_pct')} / {g('typical','worst','d_gbw_pct')} |")
    add("")
    if findings:
        add(findings.rstrip("\n"))
        add("")
    add("## Artifacts")
    add("")
    add("- Runner: `sim/gain-gbw-pm/run_gain_gbw_pm.py --passive-corners`; tests: `sim/gain-gbw-pm/test_gain_gbw_pm.py`")
    if supersedes:
        add(f"- Regenerated without a simulator: `sim/gain-gbw-pm/run_gain_gbw_pm.py --recompute-passive {supersedes}`")
    add(f"- Per-cell logs, decks, data and the sanitised klt report: `sim/gain-gbw-pm/corners/{data_id}/`")
    add("")
    return "\n".join(L)


def run_passive(pdk: Pdk, args) -> int:
    want = passive_expected_keys()
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: passive-corner study, {len(want)} cells, PDK={pdk.path}, klt {kver}")
    with tempfile.TemporaryDirectory(prefix="gainpm-pas-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        req = passive_ac_request(tb, pdk)
        req["batch"] = batch_block(args)
        try:
            report = run_klt_retrying(
                req, work / "grid" / "out", args.backend, work / "grid",
                retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
            )
        except KltError as exc:
            print(f"ERROR: the passive-corner request could not be run; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
            return 2
        results, arts, problems = analyse_ac_report(report, want)
        if problems:
            print("ERROR: passive-corner study did not complete cleanly; NO RECORD WRITTEN:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 2
        remote = (report.get("environment") or {}).get("remote") or {}
        backend_desc = (
            f"`klt sim` backend `{remote.get('provider', 'local')}`"
            + (f" ({'Spot' if remote.get('spot') else 'on-demand'} {remote.get('instance_type')})" if remote else "")
            + f"; the {len(want)} cells are ONE `klt sim` corner-matrix request"
        )
        cdir = paths["corners"]
        cdir.mkdir(parents=True, exist_ok=False)
        for k, a in arts.items():
            stem = point_stem(k)
            if a["log"]:
                shutil.copyfile(a["log"], cdir / f"{stem}.log")
            if a["deck"]:
                shutil.copyfile(a["deck"], cdir / f"{stem}.cir")
            np.savetxt(
                cdir / f"{stem}.dat",
                np.column_stack([a["freq"], a["h"].real, a["h"].imag, a["vdiff"].real, a["vdiff"].imag]),
                header="freq_hz re(vout/vdiff) im(vout/vdiff) re(vdiff) im(vdiff)",
            )
        (cdir / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))
        import hashlib

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join([
            f"* netlist snapshot for record {record} (issue #70, passive-corner study)",
            "* ---- conditions: klt sim request ----",
            *("* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
            "", "* ---- DUT (wrapper-normalised) ----", dut_text,
            "* ---- testbench (verbatim) ----", TESTBENCH.read_text(), "",
        ]))
        base = sorted(p.name for p in (HERE / "records").glob("*.md"))[-1]
        md = build_passive_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_version=kver,
            backend_desc=backend_desc, report=report, results=results, dut_sha=dut_sha,
            base_record=base.removesuffix(".md"),
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)
    print(f"wrote {paths['record']}")
    return 0


def _record_field(md: str, pattern: str, what: str) -> re.Match:
    m = re.search(pattern, md, re.M)
    if not m:
        raise ValueError(f"superseded record has no {what} line")
    return m


def recompute_passive(source: str, *, root: Path = HERE, now=None) -> tuple[str, str]:
    """Rebuild a passive-corner record from a committed record's evidence.

    No simulator and no network: per-cell metrics are re-extracted from the
    committed `corners/<source>/*.dat` (the same `extract_metrics` the live
    run uses), cross-checked against ngspice's `.meas` values in the
    committed `klt-report.json`, and the conditions (PDK, tool versions,
    execution, DUT hash, base record) plus the analyst `## Findings` section
    are carried over verbatim from the superseded record. Returns
    (new record id, markdown); the caller writes it.
    """
    old_md = (root / "records" / f"{source}.md").read_text()
    cdir = root / "corners" / source
    report = json.loads((cdir / "klt-report.json").read_text())
    want = passive_expected_keys()
    seen = {point_key(c): c for c in report.get("corners", [])}
    if set(seen) != set(want):
        raise ValueError(f"{source}: klt-report.json cells do not match the passive study grid")
    results: dict[Key, Metrics] = {}
    problems: list[str] = []
    for k in want:
        a = np.loadtxt(cdir / f"{point_stem(k)}.dat")
        freq, h, vdiff = a[:, 0], a[:, 1] + 1j * a[:, 2], a[:, 3] + 1j * a[:, 4]
        m = extract_metrics(freq, h)
        vals = {x["name"]: x.get("value") for x in seen[k].get("measurements", [])}
        if m.valid:
            problems += crosscheck(k, vals, h[0] * vdiff[0], m)
        results[k] = m
    if problems:
        raise ValueError("cross-check against the committed .meas values failed: " + "; ".join(problems))

    cond = _record_field(old_md, r"^- \*\*PDK\*\*: (.+) \(open_pdks (\S+)\); ngspice (.+); klt (.+)$", "PDK")
    pdk = argparse.Namespace(path=cond.group(1), version=cond.group(2))
    backend_desc = _record_field(old_md, r"^- \*\*Execution\*\*: (.+)$", "Execution").group(1)
    dut_sha = _record_field(old_md, r"normalised sha256 `([0-9a-f]{64})`", "DUT hash").group(1)
    closure = {e.get("sha256") for e in (report.get("environment") or {}).get("netlist_closure", [])}
    if dut_sha not in closure:
        raise ValueError(f"{source}: DUT sha256 in the record is not in klt-report.json's netlist closure")
    base_record = _record_field(old_md, r"default 45-point grid record is `([^`]+)`", "base record").group(1)
    fm = re.search(r"^## Findings.*?(?=^## )", old_md, re.M | re.S)
    findings = fm.group(0) if fm else ""

    if now is None:
        record, stamp = allocate_record_id(REPO_ROOT)
    else:  # tests: deterministic id, no git call
        stamp = now
        record = f"{stamp:%Y%m%d-%H%M%S}-test"
    note = (f"regenerated with NO new simulation from `{source}`'s committed `corners/{source}/` data "
            f"(`.dat` per cell + `klt-report.json`, fleet job "
            f"`{((report.get('environment') or {}).get('remote') or {}).get('job_id', 'n/a')}`, "
            f"measured {old_md.split('**Date (UTC)**: ', 1)[1].split(chr(10), 1)[0]} UTC). "
            f"Only the Verdict header changes: `{source}` printed the PASSING-cell count after the word "
            f"FAIL (\"FAIL at 7/27\" / \"FAIL at 24/27\"); every table, sensitivity and Findings number "
            f"is unchanged. Corner data and the netlist snapshot stay under `{source}`.")
    md = build_passive_record(
        record=record, stamp=stamp, pdk=pdk, ngspice=cond.group(3), klt_version=cond.group(4),
        backend_desc=backend_desc, report=report, results=results, dut_sha=dut_sha,
        base_record=base_record, supersedes=source, supersede_note=note, findings=findings,
    )
    return record, md


def run_recompute_passive(source: str) -> int:
    try:
        record, md = recompute_passive(source)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot regenerate from {source}; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
        return 2
    out = HERE / "records" / f"{record}.md"
    if out.exists():
        print(f"ERROR: {out} already exists; evidence is append-only", file=sys.stderr)
        return 2
    out.write_text(md)
    print(f"wrote {out}")
    return 0


# --------------------------------------------------------------------------
# Opt-in fixed-common-mode grid (issue #125): VCM = 1.20 V at every supply
# --------------------------------------------------------------------------

FIXED_VCM_DIR = HERE / "fixed-vcm"  # kept out of corners/ and records/: those are
# globbed by the shared 45-point cross-checks and the report generator, which
# must keep selecting the VDD/2 default records.


def load_baseline(gdir: Path | None, want: list[Key]) -> dict[Key, Metrics]:
    """Re-extract the committed VDD/2 baseline from its stored per-point data."""
    out: dict[Key, Metrics] = {}
    if gdir is None:
        return out
    for k in want:
        tab = load_gain_bench(gdir, k)
        if tab is None:
            continue
        out[k] = extract_metrics(tab[:, 0], tab[:, 1] + 1j * tab[:, 2])
    return out


def vcm_mismatches(report: dict, vcm: float) -> list[str]:
    """Every reported grid point must carry exactly the requested VCM."""
    bad = []
    for c in report.get("corners", []):
        got = (c.get("supply_v") or {}).get("vcm")
        if got is None or abs(float(got) - vcm) > 1e-9:
            bad.append(f"{fmt_key(point_key(c))}: reported vcm {got!r} != {vcm:g}")
    return bad


def _cell(m: Metrics | None, attr: str, fmt: str) -> str:
    if m is None:
        return "n/a"
    if not m.valid:
        return "INVALID"
    return fmt.format(getattr(m, attr))


def _delta(a: Metrics | None, b: Metrics | None, attr: str, fmt: str) -> str:
    if a is None or b is None or not (a.valid and b.valid):
        return "n/a"
    return fmt.format(getattr(a, attr) - getattr(b, attr))


def build_fixed_vcm_record(
    *, record, stamp, pdk, ngspice, klt_version, backend_desc, report, op_report_remote, op_error,
    vcm, want, results, baseline, baseline_id, op, nominal_op, problems, smoke_line, dut_sha, req,
    testbench_text: str | None = None, supersedes: str | None = None, supersede_note: str = "",
    nominal_op_lines: list[str] | None = None,
) -> str:
    """Fixed-VCM diagnostic record.

    `testbench_text` is the bench the grid actually ran (default: the committed
    file); the fingerprint header AND the retained canonical-inputs block are
    both derived from it, so the JSON in the record rehashes to the header.
    `supersedes` marks a record regenerated without a simulator from a
    committed record's data (`--recompute-fixed-vcm`): per-point data and the
    netlist snapshot stay under that id and are referenced, not copied, and
    `nominal_op_lines` carries the superseded record's local operating-point
    lines verbatim (that single unit's report is not retained as data).
    """
    tb_text = TESTBENCH.read_text() if testbench_text is None else testbench_text
    data_id = supersedes or record
    L: list[str] = []
    add = L.append
    remote = (report.get("environment") or {}).get("remote") or {}
    env = report.get("environment") or {}
    failed = [k for k in want if k not in results]
    invalid = [k for k in want if k in results and not results[k].valid]
    ok = [k for k in want if k in results and results[k].valid]

    def n_pass(attr, bound):
        return sum(1 for k in want if k in results and point_passes(results[k], attr, bound))

    add(f"# gain/GBW/PM at fixed VCM = {vcm:g} V (diagnostic grid) -- record {record}")
    add("")
    add(f"- **Record ID**: {record}")
    add(f"- **Date (UTC)**: {stamp:%Y-%m-%d %H:%M:%S}")
    if supersedes:
        add(f"- **Supersedes**: `{supersedes}` -- {supersede_note}")
    add("- **Issue**: #125 (evidence for #42, the phase-margin repair; Tier 1 integration evidence)")
    add(
        f"- **Claim (diagnostic only)**: open-loop DC gain, GBW and phase margin of the committed sized "
        f"schematic with the amplifier inputs held at a FIXED common mode of {vcm:g} V (the documented nominal "
        "LDO input, `spec/decision-records/0005-input-common-mode-range-row.md`) over the same 45-point "
        "MOS x temperature x supply grid as the default bench, ideal 10 uA bias, CL = 2 pF, typical passives. "
        "No consumer contract is claimed: the consumer's common-mode tolerance is unspecified and none is "
        "invented. This record does not complete T1, does not resize the DUT and relaxes no bound."
    )
    add(f"- **Common-mode policy**: `{mc.vcm_rule(vcm)}` -- VCM = {vcm:g} V at every supply point "
        "(the default bench tracks VDD/2 and is unchanged; its newest 45-point record is the baseline below).")
    add(f"- **Baseline (VDD/2)**: gain/GBW/PM data re-extracted from `sim/gain-gbw-pm/corners/{baseline_id}/` "
        f"({len(baseline)}/{len(want)} points available)")
    add(f"- **Coverage**: {len(ok)}/{len(want)} points measured and valid; "
        f"{len(failed)} failed/missing, {len(invalid)} invalid (listed under 'Failed, missing and invalid points').")
    add(f"- **Smoke point before submission**: {smoke_line}")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (via {pdk.source})")
    add(f"- **Tools**: ngspice (local: {ngspice}; engine as run by klt: `{env.get('engine')} {env.get('engine_version')}`), "
        f"klt `{klt_version}`")
    add(f"- **Execution**: {backend_desc}")
    if remote:
        add(f"  - `environment.remote`: provider `{remote.get('provider')}`, job id `{remote.get('job_id')}`, "
            f"instance type `{remote.get('instance_type')}`, state `{remote.get('state')}`, runner klt "
            f"`{remote.get('runner_klt_version')}` vs client klt `{remote.get('client_klt_version')}` "
            f"(compatibility `{remote.get('runner_compatibility')}`)")
    if op_report_remote:
        add(f"  - operating-point job id `{op_report_remote.get('job_id')}`")
    if op_error:
        add(f"  - operating-point request FAILED (no local fallback): {op_error}")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body verbatim; normalised "
        f"sha256 `{dut_sha}`), snapshotted in `netlist-snapshots/{data_id}.spice`")
    for ln in mc.fingerprint_lines(tb_text, vcm):
        add(ln)
    add("- **Source guards**: the committed testbench and DUT passed `guard_testbench` / `guard_dut` before every "
        "simulation (no transistor declared in the bench; every export device present once); the committed "
        "testbench is used verbatim -- `Vcm` is set per point by the `supply_v.vcm` axis of the klt request.")
    add("- **Corner matrix**: process " + ", ".join(CORNERS) + "; temperature "
        + ", ".join(f"{t:g} C" for t in TEMPS_C) + "; VDD " + ", ".join(f"{v:.2f} V" for v in SUPPLIES_V)
        + f"; VCM {vcm:g} V (all points); passive sections `{PASSIVE_SECTIONS[0]}`, `{PASSIVE_SECTIONS[1]}` "
        "(no independent RZ/CC corners).")
    add("")
    add("## Summary against the existing bounds (not a new contract)")
    add("")
    add("Bounds are the ratified `spec/target-spec.md` rows, unchanged; missing/invalid points count as not passing.")
    add("")
    base_ok = {k: baseline[k] for k in want if k in baseline}

    def bn(attr, bound):
        return sum(1 for m in base_ok.values() if point_passes(m, attr, bound))

    add("| Row | Bound | Fixed VCM: points passing | VDD/2 baseline: points passing |")
    add("|---|---|---|---|")
    for rid, label, attr, bound, unit in ROWS:
        add(f"| {label} | >= {bound:g} {unit} | {n_pass(attr, bound)}/{len(want)} | {bn(attr, bound)}/{len(base_ok)} |")
    add(f"| DC gain stretch | >= {GAIN_STRETCH_DB:g} dB | {n_pass('dc_gain_db', GAIN_STRETCH_DB)}/{len(want)} | "
        f"{bn('dc_gain_db', GAIN_STRETCH_DB)}/{len(base_ok)} |")
    add("")
    for rid, label, attr, bound, unit in ROWS:
        pts = [(getattr(results[k], attr), k) for k in ok]
        if pts:
            w, kk = min(pts, key=lambda t: t[0])
            add(f"- Worst {label}: {w:.4g} {unit} at {fmt_key(kk)}"
                + (f" (baseline worst {min(getattr(m, attr) for m in base_ok.values() if m.valid):.4g} {unit})"
                   if any(m.valid for m in base_ok.values()) else ""))
    add("")
    add("## Per-point results and differences from the VDD/2 baseline")
    add("")
    add("`dX` = fixed-VCM minus baseline (positive = higher at fixed VCM). The baseline VCM at 2.97 / 3.30 / 3.63 V is "
        "1.485 / 1.650 / 1.815 V. `|vout-VCM|` is the closed-loop DC follower error from the operating-point request "
        "(flag threshold " + f"{OP_VOUT_TOL_V * 1e3:g} mV; vout-only level on the grid -- device saturation was "
        "not checked per point on the fleet).")
    add("")
    add("| Point | Gain (dB) | dGain | GBW (MHz) | dGBW (%) | PM (deg) | dPM | gain>=60 | GBW>=10M | PM>=60 | vout-VCM (mV) | op flags |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k in want:
        m = results.get(k)
        b = baseline.get(k)
        if m is None:
            add(f"| {fmt_key(k)} | MISSING/FAILED | | | | | | FAIL | FAIL | FAIL | | |")
            continue
        if not m.valid:
            add(f"| {fmt_key(k)} | INVALID: {m.reason} | | | | | | FAIL | FAIL | FAIL | | |")
            continue
        gp = "PASS" if m.dc_gain_db >= GAIN_MIN_DB else "FAIL"
        bp = "PASS" if m.gbw_hz >= GBW_MIN_HZ else "FAIL"
        pp = "PASS" if m.pm_deg >= PM_MIN_DEG else "FAIL"
        dg = ("n/a" if not (b and b.valid) else f"{100 * (m.gbw_hz / b.gbw_hz - 1):+.1f}")
        o = (op or {}).get(k)
        if o and o["vals"].get("vout_v") is not None:
            ov = f"{(o['vals']['vout_v'] - o['vcm']) * 1e3:+.1f}"
            of = "; ".join(o["flags"]) or "none"
        else:
            ov, of = "n/a", "operating point not returned"
        add(f"| {fmt_key(k)} | {m.dc_gain_db:.2f} | {_delta(m, b, 'dc_gain_db', '{:+.2f}')} | "
            f"{m.gbw_hz / 1e6:.3f} | {dg} | {m.pm_deg:.2f} | {_delta(m, b, 'pm_deg', '{:+.2f}')} | "
            f"{gp} | {bp} | {pp} | {ov} | {of} |")
    add("")
    add("## Failed, missing and invalid points")
    add("")
    if not (failed or invalid or problems):
        add("None: all 45 points were returned by the executing runner, extracted valid, and agreed with the "
            "ngspice-native `.meas` cross-checks.")
    else:
        for k in failed:
            add(f"- MISSING/FAILED: {fmt_key(k)}")
        for k in invalid:
            add(f"- INVALID: {fmt_key(k)} -- {results[k].reason}")
        for pr in problems:
            add(f"- problem reported: {pr}")
    add("")
    add("## Nominal device-level operating point (local single unit)")
    add("")
    if nominal_op_lines is not None:
        L.extend(nominal_op_lines)
    elif nominal_op and nominal_op["vals"].get("vout_v") is not None:
        v = nominal_op["vals"]
        add(f"- typical / 27 C / 3.30 V, VCM {nominal_op['vcm']:g} V: vout {v['vout_v']:.4f} V, vinn {v.get('vinn_v', float('nan')):.4f} V, "
            f"tail {v.get('itail_a', float('nan')) * 1e6:.2f} uA, stage-2 {v.get('iout_a', float('nan')) * 1e6:.2f} uA; level `{nominal_op['level']}`")
        add("- flags: " + ("; ".join(nominal_op["flags"]) or "none (all DUT MOSFETs saturated, |vout-VCM| within tolerance)"))
    else:
        add("- not returned (the single local op unit failed); no device-level claim is made.")
    add("")
    add("## Interpretation limits")
    add("")
    add("- The measurement is the unchanged DC-closed / AC-open bench with only the VCM axis changed; extraction, "
        "validity checks and bounds are identical to the default grid.")
    add("- Shortfalls against the existing bounds are disclosed above and not resolved here: no DUT resize, no bound "
        "relaxed, no consumer tolerance assumed. #42 owns the phase-margin repair; this grid is input to it.")
    add("- An ICMR endpoint check (`sim/input-common-mode/`) does not substitute for this dynamic evidence, and a "
        "single VCM point is not a VCM sweep.")
    add("")
    L.extend(mc.inputs_section(tb_text, vcm))
    add("## Artifacts")
    add("")
    add(f"- Runner: `sim/gain-gbw-pm/run_gain_gbw_pm.py --vcm-fixed {vcm:g}`; tests: `sim/gain-gbw-pm/test_gain_gbw_pm.py`")
    if supersedes:
        add(f"- Regenerated without a simulator: `sim/gain-gbw-pm/run_gain_gbw_pm.py --recompute-fixed-vcm {supersedes}`")
    add(f"- Per-point logs, decks, data and the sanitised klt report(s): `sim/gain-gbw-pm/fixed-vcm/corners/{data_id}/`")
    add(f"- Netlist snapshot (request, DUT, testbench): `sim/gain-gbw-pm/fixed-vcm/netlist-snapshots/{data_id}.spice`")
    add("")
    return "\n".join(L)


def fixed_vcm_misses(results: dict[Key, Metrics], want: list[Key]) -> list[str]:
    """Ratified rows (spec/target-spec.md bounds, unchanged) that miss at any
    grid point; a missing or invalid point counts as a miss, as in `judge`."""
    out = []
    for _rid, label, attr, bound, unit in ROWS:
        n = sum(1 for k in want if k in results and point_passes(results[k], attr, bound))
        if n < len(want):
            out.append(f"{label} >= {bound:g} {unit}: passes at {n}/{len(want)} points")
    return out


SNAPSHOT_TB_MARKER = "* ---- testbench (verbatim) ----\n"


def snapshot_testbench(snapshot_text: str) -> str:
    """The testbench a fixed-VCM run retained verbatim at the end of its snapshot."""
    if snapshot_text.count(SNAPSHOT_TB_MARKER) != 1:
        raise ValueError("netlist snapshot has no single verbatim-testbench section")
    tail = snapshot_text.split(SNAPSHOT_TB_MARKER, 1)[1]
    if not tail.endswith("\n"):
        raise ValueError("netlist snapshot testbench section is truncated")
    return tail[:-1]  # the snapshot writer appends exactly one "\n" after the bench


def recompute_fixed_vcm(source: str, *, root: Path = FIXED_VCM_DIR, now=None) -> tuple[str, str]:
    """Rebuild a fixed-VCM record from a committed record's evidence, no simulator.

    Per-point metrics are re-extracted from the committed `corners/<source>/*.dat`
    and cross-checked against ngspice's `.meas` values in the committed
    `klt-report.json`; the grid operating-point flags come from the committed
    `klt-op-report.json`; the baseline is re-extracted from the default record
    the superseded one names. The canonical fingerprint inputs are rebuilt from
    the testbench retained verbatim in the committed netlist snapshot and must
    rehash to the fingerprint the superseded record printed. Conditions (PDK,
    tools, execution, DUT hash, smoke point, local nominal operating point) are
    carried over verbatim. Returns (new record id, markdown); the caller writes it.
    """
    old_md = (root / "records" / f"{source}.md").read_text()
    cdir = root / "corners" / source
    report = json.loads((cdir / "klt-report.json").read_text())
    vcm = float(_record_field(old_md, r"^- \*\*Common-mode policy\*\*: `fixed:([0-9.]+)`", "Common-mode policy").group(1))
    vcm = validate_vcm_fixed(vcm)
    tb_text = snapshot_testbench((root / "netlist-snapshots" / f"{source}.spice").read_text())
    old_fp = _record_field(old_md, r"^- \*\*Measurement fingerprint\*\*: version \S+, sha256 `([0-9a-f]{64})`",
                           "Measurement fingerprint").group(1)
    if mc.fingerprint(tb_text, vcm) != old_fp:
        raise ValueError(f"{source}: the snapshot testbench at VCM {vcm:g} does not rehash to the recorded "
                         f"fingerprint {old_fp}")

    want = expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    seen = {point_key(c): c for c in report.get("corners", [])}
    if set(seen) != set(want):
        raise ValueError(f"{source}: klt-report.json points do not match the 45-point grid")
    bad = vcm_mismatches(report, vcm)
    if bad:
        raise ValueError(f"{source}: klt-report.json does not carry VCM {vcm:g}: " + "; ".join(bad))
    results: dict[Key, Metrics] = {}
    problems: list[str] = []
    for k in want:
        a = np.loadtxt(cdir / f"{point_stem(k)}.dat")
        freq, h, vdiff = a[:, 0], a[:, 1] + 1j * a[:, 2], a[:, 3] + 1j * a[:, 4]
        m = extract_metrics(freq, h)
        vals = {x["name"]: x.get("value") for x in seen[k].get("measurements", [])}
        if m.valid:
            problems += crosscheck(k, vals, h[0] * vdiff[0], m)
        results[k] = m
    if problems:
        raise ValueError("cross-check against the committed .meas values failed: " + "; ".join(problems))
    op = op_remote = None
    op_path = cdir / "klt-op-report.json"
    if op_path.is_file():
        oreport = json.loads(op_path.read_text())
        op = op_flags(oreport, want)
        op_remote = (oreport.get("environment") or {}).get("remote")

    pdk_m = _record_field(old_md, r"^- \*\*PDK revision\*\*: (\S+), open_pdks `([^`]+)` \(via (.+)\)$", "PDK revision")
    pdk = argparse.Namespace(variant=pdk_m.group(1), version=pdk_m.group(2), source=pdk_m.group(3))
    tools = _record_field(old_md, r"^- \*\*Tools\*\*: ngspice \(local: (.+); engine as run by klt: `[^`]*`\), klt `([^`]+)`$",
                          "Tools")
    backend_desc = _record_field(old_md, r"^- \*\*Execution\*\*: (.+)$", "Execution").group(1)
    dut_sha = _record_field(old_md, r"normalised sha256 `([0-9a-f]{64})`", "DUT hash").group(1)
    closure = {e.get("sha256") for e in (report.get("environment") or {}).get("netlist_closure", [])}
    if dut_sha not in closure:
        raise ValueError(f"{source}: DUT sha256 in the record is not in klt-report.json's netlist closure")
    smoke_line = _record_field(old_md, r"^- \*\*Smoke point before submission\*\*: (.+)$", "Smoke point").group(1)
    baseline_id = _record_field(old_md, r"re-extracted from `sim/gain-gbw-pm/corners/([^/`]+)/`", "Baseline").group(1)
    op_error_m = re.search(r"^  - operating-point request FAILED \(no local fallback\): (.+)$", old_md, re.M)
    nm = re.search(r"^## Nominal device-level operating point \(local single unit\)\n\n(.*?)\n\n## ", old_md, re.M | re.S)
    if not nm:
        raise ValueError("superseded record has no nominal operating-point section")
    baseline = load_baseline(HERE / "corners" / baseline_id, want)

    if now is None:
        record, stamp = allocate_record_id(REPO_ROOT)
    else:  # tests: deterministic id, no git call
        stamp = now
        record = f"{stamp:%Y%m%d-%H%M%S}-test"
    measured = old_md.split("**Date (UTC)**: ", 1)[1].split(chr(10), 1)[0]
    note = (f"regenerated with NO new simulation from `{source}`'s committed `fixed-vcm/corners/{source}/` data "
            f"(`.dat` per point + `klt-report.json` / `klt-op-report.json`, fleet job "
            f"`{((report.get('environment') or {}).get('remote') or {}).get('job_id', 'n/a')}`, measured {measured} UTC). "
            f"Correction: `{source}` printed the measurement-fingerprint header but omitted the "
            "'Measurement fingerprint inputs' section it refers to; that section is added below, rebuilt from the "
            f"testbench retained verbatim in `fixed-vcm/netlist-snapshots/{source}.spice`, and rehashes to the same "
            f"fingerprint `{old_fp}`. Every table and count is re-derived from the committed data and unchanged; "
            f"corner data and the netlist snapshot stay under `{source}`.")
    md = build_fixed_vcm_record(
        record=record, stamp=stamp, pdk=pdk, ngspice=tools.group(1), klt_version=tools.group(2),
        backend_desc=backend_desc, report=report, op_report_remote=op_remote,
        op_error=op_error_m.group(1) if op_error_m else None, vcm=vcm, want=want, results=results,
        baseline=baseline, baseline_id=baseline_id, op=op, nominal_op=None, problems=[],
        smoke_line=smoke_line, dut_sha=dut_sha, req=None, testbench_text=tb_text,
        supersedes=source, supersede_note=note, nominal_op_lines=nm.group(1).split("\n"),
    )
    return record, md


def run_recompute_fixed_vcm(source: str) -> int:
    try:
        record, md = recompute_fixed_vcm(source)
    except (OSError, ValueError, VcmRequestError) as exc:
        print(f"ERROR: cannot regenerate from {source}; NO RECORD WRITTEN.\n{exc}", file=sys.stderr)
        return 2
    out = FIXED_VCM_DIR / "records" / f"{record}.md"
    if out.exists():
        print(f"ERROR: {out} already exists; evidence is append-only", file=sys.stderr)
        return 2
    out.write_text(md)
    print(f"wrote {out}")
    return 0


def run_fixed_vcm(pdk: Pdk, args, vcm: float) -> int:
    import hashlib

    want = expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(FIXED_VCM_DIR, record, plots=False)
    ngspice = ngspice_version()
    kver = klt_version()
    print(f"record {record}: fixed-VCM {vcm:g} V grid, {len(want)} points, PDK={pdk.path}, klt {kver}")
    with tempfile.TemporaryDirectory(prefix="gainpm-vcm-") as scratch:
        work = Path(scratch)
        # Nominal smoke point FIRST (one local single unit); a bad bench never reaches the fleet.
        sm = run_single("smoke-fixed-vcm", "nominal, fixed VCM", pdk, work, vcm_fixed=vcm)
        if sm.error or sm.metrics is None or not sm.metrics.valid:
            why = sm.error or (sm.metrics.reason if sm.metrics else "no metrics")
            print(f"ERROR: nominal smoke point failed ({why}); grid NOT submitted, NO RECORD WRITTEN.", file=sys.stderr)
            return 2
        m0 = sm.metrics
        smoke_line = (f"typical / 27 C / 3.30 V at VCM {vcm:g} V, local single unit: gain {m0.dc_gain_db:.2f} dB, "
                      f"GBW {fmt_hz(m0.gbw_hz)}, PM {m0.pm_deg:.2f} deg (valid)")
        print("  smoke: " + smoke_line)

        tb = materialise(work / "grid", pdk)
        req = ac_request(tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V, vcm)
        req["batch"] = batch_block(args)
        try:
            report = run_klt_retrying(
                req, work / "grid" / "out", args.backend, work / "grid",
                retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
            )
        except KltError as exc:
            print(f"ERROR: the grid request could not be run; NO RECORD WRITTEN, NO LOCAL FALLBACK.\n{exc}", file=sys.stderr)
            return 2
        bad = vcm_mismatches(report, vcm)
        if bad:
            print("ERROR: the report does not carry the requested VCM; NO RECORD WRITTEN:", file=sys.stderr)
            for b in bad:
                print(f"  - {b}", file=sys.stderr)
            return 2
        results, arts, problems = analyse_ac_report(report, want)
        remote = (report.get("environment") or {}).get("remote") or {}
        backend_desc = (
            f"`klt sim` backend `{remote.get('provider', 'local')}`"
            + (f" ({'Spot' if remote.get('spot') else 'on-demand'} {remote.get('instance_type')})" if remote else "")
            + "; the 45 points are ONE `klt sim` corner-matrix request"
        )

        op = op_remote = op_error = oreport = None
        try:
            op_tb = materialise(work / "op", pdk)
            oreq = op_request_grid(op_tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V, vcm)
            oreq["batch"] = batch_block(args)
            oreport = run_klt_retrying(
                oreq, work / "op" / "out", args.backend, work / "op",
                retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s,
            )
            op = op_flags(oreport, want)
            op_remote = (oreport.get("environment") or {}).get("remote")
        except KltError as exc:
            op_error = str(exc).splitlines()[0][:300] if str(exc) else "unknown"
            print(f"WARNING: operating-point request failed: {exc}", file=sys.stderr)
        nominal_op = run_nominal_op(pdk, work, vcm)

        cdir = paths["corners"]
        cdir.mkdir(parents=True, exist_ok=False)
        for k, a in arts.items():
            stem = point_stem(k)
            if a["log"]:
                shutil.copyfile(a["log"], cdir / f"{stem}.log")
            if a["deck"]:
                shutil.copyfile(a["deck"], cdir / f"{stem}.cir")
            np.savetxt(
                cdir / f"{stem}.dat",
                np.column_stack([a["freq"], a["h"].real, a["h"].imag, a["vdiff"].real, a["vdiff"].imag]),
                header="freq_hz re(vout/vdiff) im(vout/vdiff) re(vdiff) im(vdiff)",
            )
        (cdir / "klt-report.json").write_text(json.dumps(sanitise_report(report), indent=1))
        if oreport is not None:
            (cdir / "klt-op-report.json").write_text(json.dumps(sanitise_report(oreport), indent=1))

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join([
            f"* netlist snapshot for record {record} (issue #125, fixed VCM {vcm:g} V)",
            "* ---- conditions: klt sim request (grid) ----",
            *("* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()),
            "", "* ---- DUT (wrapper-normalised) ----", dut_text,
            "* ---- testbench (verbatim) ----", TESTBENCH.read_text(), "",
        ]))
        gdir = latest_gain_dir()
        baseline = load_baseline(gdir, want)
        md = build_fixed_vcm_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_version=kver, backend_desc=backend_desc,
            report=report, op_report_remote=op_remote, op_error=op_error, vcm=vcm, want=want, results=results,
            baseline=baseline, baseline_id=gdir.name if gdir else "none", op=op, nominal_op=nominal_op,
            problems=problems, smoke_line=smoke_line, dut_sha=dut_sha, req=req,
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)
    print(f"wrote {paths['record']}")
    if problems or any(k not in results or not results[k].valid for k in want):
        print("INCOMPLETE: failed/missing/invalid points are listed in the record", file=sys.stderr)
        return 1
    misses = fixed_vcm_misses(results, want)
    for ms in misses:
        print(f"  ratified bound missed (diagnostic grid): {ms}")
    if getattr(args, "strict", False) and misses:
        print("STRICT: a ratified row misses at fixed VCM; evidence retained in the record, exit 1", file=sys.stderr)
        return 1
    return 0


def smoke(pdk: Pdk, vcm_fixed: float | None = None) -> int:
    print(f"smoke test: {NOMINAL} only, local, PDK={pdk.path}, VCM {'VDD/2' if vcm_fixed is None else f'{vcm_fixed:g} V'}")
    with tempfile.TemporaryDirectory(prefix="gainpm-smoke-") as scratch:
        run = run_single("smoke", "nominal", pdk, Path(scratch), vcm_fixed=vcm_fixed)
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
    ap.add_argument("--passive-corners", action="store_true",
                    help="opt-in: RZ x CC passive-corner study (3x3 independent poly-resistor / "
                    "MIM-cap sections at fs/125C/2.97V, ss/125C/2.97V and nominal) as ONE klt sim "
                    "request; the default 45-point grid is unchanged")
    ap.add_argument("--recompute-passive", metavar="RECORD",
                    help="no simulator: write a new record superseding passive-corner RECORD, "
                    "regenerated from its committed corners/RECORD/ data")
    ap.add_argument("--recompute-fixed-vcm", metavar="RECORD",
                    help="no simulator: write a new record superseding fixed-VCM RECORD, regenerated "
                    "from its committed fixed-vcm/corners/RECORD/ data and netlist snapshot")
    ap.add_argument("--vcm-fixed", type=float, metavar="VOLTS", default=None,
                    help="opt-in (issue #125): hold the input common mode at VOLTS at every supply "
                    "point instead of tracking VDD/2 (the LDO consumer input is 1.20). Writes a "
                    "separate record under fixed-vcm/; default records are untouched. With --smoke it "
                    "runs the one local nominal point at that VCM.")
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

    # Validate BEFORE any tool is touched or anything is submitted (issue #125).
    if args.vcm_fixed is not None:
        if args.passive_corners or args.recompute_passive or args.recompute_fixed_vcm:
            ap.error("--vcm-fixed cannot be combined with --passive-corners / --recompute-passive / "
                     "--recompute-fixed-vcm")
        try:
            args.vcm_fixed = validate_vcm_fixed(args.vcm_fixed)
        except VcmRequestError as exc:
            ap.error(str(exc))

    if args.recompute_passive:
        return run_recompute_passive(args.recompute_passive)
    if args.recompute_fixed_vcm:
        return run_recompute_fixed_vcm(args.recompute_fixed_vcm)
    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk, args.vcm_fixed)
    if args.vcm_fixed is not None:
        return run_fixed_vcm(pdk, args, args.vcm_fixed)
    if args.passive_corners:
        return run_passive(pdk, args)

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
