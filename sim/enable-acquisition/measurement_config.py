"""Effective measurement configuration of the supply / external-bias enable
acquisition experiment (issue #191).

Stdlib only. `run_enable_acquisition.py` imports its analysis, schedules,
extraction thresholds and control settings FROM here, so the measurement
fingerprint cannot drift from what is run. The grid axes come from the gain
configuration module (one source for the 45-point grid).

Fingerprinted: the canonical bench content, the analysis cards, the scenario
and ramp schedules (every .param value substituted into the bench), the
acquisition band / dwell / drift rules, the initial-state and final-level
validity limits, the controls, the corner axes and the model library/sections.
NOT fingerprinted: record IDs, workspace paths, backend/retry options,
cross-check tolerances and the extraction code. The DUT has its own hash (#75).

All ramp durations and windows below are DIAGNOSTIC EXPERIMENT CONDITIONS.
They are not consumer supply/bias slew requirements and no startup bound is
graded against them.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sim"))
import harness  # noqa: E402

_GAIN = harness.load_config_module(HERE.parent / "gain-gbw-pm" / "measurement_config.py")

EXPERIMENT = "enable-acquisition"
FINGERPRINT_VERSION = harness.FINGERPRINT_VERSION
TESTBENCH_REL = "sim/enable-acquisition/testbench/tb_enable.spice"
MODULE_REL = "sim/enable-acquisition/measurement_config.py"

CORNERS = _GAIN.CORNERS
PASSIVE_SECTIONS = _GAIN.PASSIVE_SECTIONS
TEMPS_C = _GAIN.TEMPS_C
SUPPLIES_V = _GAIN.SUPPLIES_V
MODEL_LIB = _GAIN.MODEL_LIB

IBIAS_A = 10e-6
CL_F = 2e-12  # [DR-1]

# ---- diagnostic enable schedule -------------------------------------------
LEAD_T = 1e-6  # everything off (rail 0 V, bias 0 A) for this long before the first ramp
GAP_T = 2e-6  # delay between the rail ramp completing and the delayed bias enable
OFF_HOLD_T = 2e-6  # bias held off between the disable ramp completing and the re-enable
WINDOW_T = 4e-6  # observation window after each enable completes (>= 250 tau at GBW_MIN)
LONG_WINDOW_FACTOR = 10  # follow-up window multiplier at any unresolved point
NEVER_T = 1.0  # "this event never happens" (beyond every run stop)

#: Diagnostic ramp pair: (name, rail ramp duration, bias ramp duration, tstep).
#: A fast / slow PAIR so a result can be seen to depend on (or be independent
#: of) the enable rate. Not consumer guarantees.
RAMPS = (
    ("fast", 100e-9, 100e-9, 5e-9),
    ("slow", 5e-6, 5e-6, 10e-9),
)

#: Scenario names, in record order.
SCENARIOS = ("simultaneous", "supply-first", "bias-cycle")

SCENARIO_DESC = {
    "simultaneous": "rail and external bias enabled together (both ramps start at the same instant)",
    "supply-first": "rail ramps first; the bias is enabled GAP_T after the rail ramp completes",
    "bias-cycle": "rail ramps first, bias enabled GAP_T later (enable 1), then at fixed supply the bias is "
                  "ramped back to 0 A, held off OFF_HOLD_T and re-enabled (enable 2 = the re-enable)",
}

# ---- extraction -----------------------------------------------------------
REF_WINDOW = 200e-9  # the acquired level is judged over the last REF_WINDOW of each window
BAND_FRAC = 0.01  # diagnostic acquisition band: +-BAND_FRAC of |v_ref| (the overdrive-study convention)
DRIFT_FRAC = 0.1  # slow-tail rule: drift across the final reference window <= DRIFT_FRAC * band
MIN_DWELL_FRAC = 0.1  # in-band tail must also last >= this fraction of the window (not one crossing)
SAMPLE_BEFORE = 10e-9
# Initial-state requirement (the run must NOT start powered / biased).
INIT_MAX_V = 0.05  # |v(vdd)|, |v(vinp)|, |v(vout)|, |v(ibias)| at t=0 each below this
INIT_MAX_RAIL_FRAC = 0.02  # and v(rs), v(rb) envelopes at 0
# Final-level validity: the rail reached its final value; the steady reference follows VCM.
FINAL_RAIL_TOL_V = 0.02
REF_LEVEL_TOL_V = 0.02  # |v_ref - VCM| (separate powered-steady DC solution vs the ideal final input)

GBW_MIN_HZ = 10e6  # sizes windows against tau only; never judged here

#: Single-unit controls (name, description, bench overrides). Overrides: `ibias_a`, `cl_f`.
CONTROLS = (
    ("nominal", "unmodified nominal point (reference for the controls)", {}),
    ("cl-x5", "CL = 10 pF instead of 2 pF (heavier load; must acquire SLOWER than nominal)", {"cl_f": 10e-12}),
    ("ibias-zero", "external bias held at 0 A (dead amplifier; must NEVER yield an acquired output)", {"ibias_a": 0.0}),
)
#: The scenario/ramp the controls run in.
CONTROL_CASE = ("simultaneous", "fast")


def schedule(scenario: str, ramp: str, window_t: float | None = None) -> dict:
    """The full enable schedule of one (scenario, ramp) run: the bench `.param`
    values, the run stop, the sampling interval and the acquisition events.

    Each event is (name, t_start, t_complete, t_window_end): the instant its
    first ramp starts, the instant the last of its ramps has completed and the
    end of its observation window.
    """
    rname, tr_s, tr_b, tstep = next(r for r in RAMPS if r[0] == ramp)
    w = WINDOW_T if window_t is None else window_t
    t_vdd = LEAD_T
    if scenario == "simultaneous":
        t_ib = LEAD_T
        t_off, t_on2 = NEVER_T, 2 * NEVER_T
        done = LEAD_T + max(tr_s, tr_b)
        events = [("enable", LEAD_T, done, done + w)]
        tstop = done + w
    elif scenario in ("supply-first", "bias-cycle"):
        t_ib = LEAD_T + tr_s + GAP_T
        done = t_ib + tr_b
        if scenario == "supply-first":
            t_off, t_on2 = NEVER_T, 2 * NEVER_T
            events = [("enable", LEAD_T, done, done + w)]
            tstop = done + w
        else:
            t_off = done + w
            t_on2 = t_off + tr_b + OFF_HOLD_T
            done2 = t_on2 + tr_b
            events = [("enable", LEAD_T, done, t_off), ("re-enable", t_on2, done2, done2 + w)]
            tstop = done2 + w
    else:
        raise ValueError(f"unknown scenario {scenario!r}")
    params = {"t_vdd": t_vdd, "tr_s": tr_s, "t_ib": t_ib, "tr_b": tr_b, "t_off": t_off, "t_on2": t_on2}
    # pwl corner times must be strictly increasing
    assert t_ib + tr_b < t_off and t_off + tr_b < t_on2, (scenario, ramp)
    q = lambda x: round(x, 12)  # noqa: E731  (exact picosecond grid: no float noise in the deck)
    params = {k: q(v) for k, v in params.items()}
    events = [(n, q(a), q(b), q(c)) for n, a, b, c in events]
    return {"scenario": scenario, "ramp": rname, "params": params, "tstop": q(tstop), "tstep": tstep,
            "events": events, "t_off": params["t_off"], "t_on2": params["t_on2"], "t_ib": params["t_ib"],
            "t_vdd": params["t_vdd"], "tr_s": tr_s, "tr_b": tr_b}


def cases() -> list[tuple[str, str]]:
    return [(s, r[0]) for s in SCENARIOS for r in RAMPS]


def analysis(sched: dict) -> dict:
    """The klt `analysis` block for one schedule."""
    return {"kind": "tran", "args": f"{sched['tstep']:g} {sched['tstop']:g}"}


def measurements(sched: dict) -> list[dict]:
    """ngspice-native `.meas` cards, used only to cross-check the rawfile extraction."""
    t_end = sched["tstop"] - SAMPLE_BEFORE
    return [
        {"name": "vfinal_v", "spice": f".meas tran vfinal_v FIND v(vout) AT={t_end:g}", "unit": "V"},
        {"name": "vdd_final_v", "spice": f".meas tran vdd_final_v FIND v(vdd) AT={t_end:g}", "unit": "V"},
        {"name": "vout_max_v", "spice": ".meas tran vout_max_v MAX v(vout)", "unit": "V"},
    ]


def params() -> dict:
    return {
        "lead_t": LEAD_T, "gap_t": GAP_T, "off_hold_t": OFF_HOLD_T, "window_t": WINDOW_T,
        "long_window_factor": LONG_WINDOW_FACTOR, "never_t": NEVER_T,
        "ref_window": REF_WINDOW, "band_frac": BAND_FRAC, "drift_frac": DRIFT_FRAC,
        "min_dwell_frac": MIN_DWELL_FRAC, "sample_before": SAMPLE_BEFORE,
        "init_max_v": INIT_MAX_V, "init_max_rail_frac": INIT_MAX_RAIL_FRAC,
        "final_rail_tol_v": FINAL_RAIL_TOL_V, "ref_level_tol_v": REF_LEVEL_TOL_V,
        "ibias_a": IBIAS_A, "cl_f": CL_F,
    }


def schedules_dump() -> dict:
    out = {}
    for s, r in cases():
        sc = schedule(s, r)
        out[f"{s}/{r}"] = {"params": sc["params"], "tstop": sc["tstop"], "tstep": sc["tstep"],
                           "events": [list(e) for e in sc["events"]]}
    return out


def inputs(bench_text: str) -> dict:
    """The effective measurement inputs, as a JSON-serialisable dict."""
    return {
        "experiment": EXPERIMENT,
        "fingerprint_version": FINGERPRINT_VERSION,
        "bench": harness.canonical_bench_lines(bench_text),
        "analysis": {"kind": "tran", "per_schedule": schedules_dump(),
                     "measurements": {f"{s}/{r}": measurements(schedule(s, r)) for s, r in cases()}},
        "params": params(),
        "controls": {name: dict(ov) for name, _, ov in CONTROLS},
        "control_case": list(CONTROL_CASE),
        "corners": {
            "process": list(CORNERS),
            "passive_sections": list(PASSIVE_SECTIONS),
            "temperature_c": [float(t) for t in TEMPS_C],
            "supply_v": [float(v) for v in SUPPLIES_V],
            "vcm_rule": "vdd/2",
        },
        "models": {"pdk_variant": harness.DEFAULT_VARIANT, "lib": MODEL_LIB},
    }


def fingerprint(bench_text: str) -> str:
    return harness.measurement_fingerprint(inputs(bench_text))


def fingerprint_lines(bench_text: str) -> list[str]:
    return harness.fingerprint_header_lines(
        inputs(bench_text),
        "bench content, transient analysis and `.meas` cards, every scenario/ramp schedule, acquisition band, "
        "dwell and drift rules, initial-state and final-level validity limits, controls, corner axes "
        "and model library/sections")


def inputs_section(bench_text: str) -> list[str]:
    return harness.fingerprint_inputs_section(inputs(bench_text), MODULE_REL)
