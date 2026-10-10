#!/usr/bin/env python3
"""Sensitivity of GBW, phase margin, slew and quiescent power to the external
bias current `ibias` and the load capacitance CL (issue #114; evidence for #42).

DATA ONLY. No row is graded here: the +-20 % `ibias` range and the CL list are
exploratory (`spec/target-spec.md` records no amplifier bias tolerance and no
CL other than DR-1's 2 pF), and no spec row, bound or selection is changed.

What it runs (all reusing the committed benches of the sibling experiments;
the only change is the one `Ibias` or `CL` line of the materialised copy, never
the committed file):

  * GBW and PM (and DC gain) vs `ibias` in {8, 9, 10, 11, 12} uA at CL = 2 pF,
    and vs CL in {1, 2, 4, 10} pF at 10 uA, at three corner points: nominal,
    the PM-binding point and the GBW-binding point.  `gain-gbw-pm` bench and
    extractor.
  * Quiescent power (`slew-swing-power` power bench) and slew rate (slew bench,
    CL = 2 pF) vs `ibias`, at nominal, the power-binding point and the
    slew-binding point.

The sweeps are separate (no Cartesian product). Each (figure, sweep value) is
ONE small `klt sim` `corners` request restricted, with klt's `exclude`, to the
named points. Which backend executes it is `klt`'s decision (`--backend`, the
request's `backend`, or `$KLT_SIM_BACKEND`; on a dispatch worker the Spot batch
fleet). This script never launches a simulator itself and never falls back to a
local run when a submit fails: it stops with the error and writes NO record.
The 10 uA / 2 pF point is simulated once and doubles as the control: it must
reproduce the committed `gain-gbw-pm` and `slew-swing-power` records.

Evidence (append-only, a new record id every run):

    corners/<rid>/<unit>/<process>_<T>c_<vdd>v.{log,cir}     per point
    corners/<rid>/<unit>/klt-report.json                      sanitised klt report
    corners/<rid>/results.json                                every extracted value
    netlist-snapshots/<rid>.spice                             DUT + benches + request conditions
    records/<rid>.md

Usage:
    python3 sim/ibias-cl-sensitivity/run_ibias_cl_sensitivity.py            # full sweep + record
    python3 sim/ibias-cl-sensitivity/run_ibias_cl_sensitivity.py --smoke    # one local nominal point, no record

Exit status: 0 when the record is complete and the controls reproduce the
committed records; 1 when a control disagrees; 2 when a request could not be
run (no record written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))
sys.path.insert(0, str(REPO_ROOT / "design"))

from harness import (  # noqa: E402
    KltError,
    Pdk,
    allocate_record_id,
    batch_block,
    claim_record_paths,
    find_pdk,
    klt_version,
    load_config_module,
    load_dut_text,
    load_sibling,
    ngspice_version,
    run_klt,
    run_klt_retrying,
    sanitise_report,
)

g = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")
s = load_sibling("slew_swing_power_driver", "sim/slew-swing-power/run_slew_swing_power.py")
mc = load_config_module(HERE / "measurement_config.py")

IBIAS_SWEEP_A = mc.IBIAS_SWEEP_A
CL_SWEEP_F = mc.CL_SWEEP_F
IBIAS_NOM_A = mc.IBIAS_NOM_A
CL_NOM_F = mc.CL_NOM_F
POINTS = mc.POINTS
NOMINAL = mc.NOMINAL

BENCH = {
    "ac": g.TESTBENCH,
    "power": s.TESTBENCH["power"],
    "slew": s.TESTBENCH["slew"],
}

# Control tolerances against the committed records' printed values (rounded to
# 3 decimals GBW MHz / 2 decimals the rest).
CTRL_GBW_REL = 2e-3
CTRL_PM_DEG = 0.02
CTRL_GAIN_DB = 0.02
CTRL_POWER_REL = 1e-3
CTRL_SLEW_VUS = 0.02

_IBIAS_RE = re.compile(r"^Ibias\s+vdd\s+ibias\s+dc\s+\S+\s*$", re.M)
_CL_RE = re.compile(r"^CL\s+vout\s+0\s+\S+\s*$", re.M)

Key = tuple


# --------------------------------------------------------------------------
# Bench substitution (pure; the committed bench files are never edited)
# --------------------------------------------------------------------------


def set_ibias(tb: str, ibias_a: float) -> str:
    out, n = _IBIAS_RE.subn(f"Ibias vdd ibias dc {ibias_a:g}", tb)
    if n != 1:
        raise RuntimeError(f"testbench Ibias line matched {n} times, expected exactly once")
    return out


def set_cl(tb: str, cl_f: float) -> str:
    out, n = _CL_RE.subn(f"CL vout 0 {cl_f:g}", tb)
    if n != 1:
        raise RuntimeError(f"testbench CL line matched {n} times, expected exactly once")
    return out


def materialise(fig: str, work: Path, pdk: Pdk, ibias_a: float, cl_f: float | None = None) -> Path:
    """Stage `work` with the sibling bench's guards and includes, then set Ibias (and CL)."""
    if fig == "ac":
        path = g.materialise(work, pdk)
    else:
        path = s.materialise(fig, work, pdk)
    tb = set_ibias(path.read_text(), ibias_a)
    if cl_f is not None:
        tb = set_cl(tb, cl_f)
    path.write_text(tb)
    return path


# --------------------------------------------------------------------------
# klt requests
# --------------------------------------------------------------------------


def restrict_to_points(req: dict, points: list[Key]) -> dict:
    """Restrict a T x VDD x process cross-product request to exactly `points`
    with klt's `exclude`. Mutates and returns `req`."""
    procs = list(dict.fromkeys(p[0] for p in points))
    temps = sorted({p[1] for p in points})
    vdds = sorted({p[2] for p in points})
    req["corners"]["process"] = g.process_axis(procs)
    req["corners"]["supply_v"] = {"vdd": list(vdds), "vcm": [round(v / 2, 6) for v in vdds]}
    req["corners"]["temperature_c"] = list(temps)
    want = {(p[0], float(p[1]), float(p[2])) for p in points}
    req["exclude"] = [
        {"process": pr, "temperature_c": t, "supply_v": {"vdd": v}}
        for pr in procs for t in temps for v in vdds if (pr, t, v) not in want
    ]
    return req


def make_request(fig: str, netlist: Path, pdk: Pdk, points: list[Key], ibias_a: float) -> dict:
    procs = list(dict.fromkeys(p[0] for p in points))
    temps = sorted({p[1] for p in points})
    vdds = sorted({p[2] for p in points})
    if fig == "ac":
        req = g.ac_request(netlist, pdk, procs, temps, vdds)
    else:
        req = s.make_request(fig, netlist, pdk, procs, temps, vdds, ibias_a=ibias_a)
    return restrict_to_points(req, points)


def units() -> list[dict]:
    """Every distinct simulation: the 10 uA / 2 pF point is run once and shared."""
    out = []
    for ib in IBIAS_SWEEP_A:
        out.append({"fig": "ac", "ibias_a": ib, "cl_f": CL_NOM_F})
        out.append({"fig": "power", "ibias_a": ib, "cl_f": None})
        out.append({"fig": "slew", "ibias_a": ib, "cl_f": CL_NOM_F})
    for cl in CL_SWEEP_F:
        if cl != CL_NOM_F:
            out.append({"fig": "ac", "ibias_a": IBIAS_NOM_A, "cl_f": cl})
    for u in out:
        u["name"] = unit_name(u)
    return out


def unit_name(u: dict) -> str:
    cl = "" if u["cl_f"] is None else f"_cl{u['cl_f'] * 1e12:g}p"
    return f"{u['fig']}_ibias{u['ibias_a'] * 1e6:g}u{cl}"


# --------------------------------------------------------------------------
# Extraction (reuses the sibling analysers; this layer only flattens them)
# --------------------------------------------------------------------------


def flatten(fig: str, m) -> dict:
    """The reported numbers of one point (None for an invalid measurement)."""
    if not m.valid:
        return {"valid": False, "reason": m.reason}
    if fig == "ac":
        return {"valid": True, "gain_db": m.dc_gain_db, "gbw_mhz": m.gbw_hz / 1e6, "pm_deg": m.pm_deg}
    if fig == "power":
        return {"valid": True, "power_uw": m.power_uw, "idd_ua": m.idd_ua}
    return {"valid": True, "slew_vus": m.slew_vus, "slew_rise_vus": m.slew_rise_vus, "slew_fall_vus": m.slew_fall_vus}


def analyse(fig: str, report: dict, points: list[Key], ibias_a: float):
    """(per-point flattened values, artifacts, problems) of one unit's klt report."""
    want = [(p[0], float(p[1]), float(p[2])) for p in points]
    if fig == "ac":
        res, arts, problems = g.analyse_ac_report(report, want)
    else:
        res, arts, problems = s.analyse_report(fig, report, want, ibias_a)
    vals = {k: flatten(fig, m) for k, m in res.items()}
    for k, v in vals.items():
        if not v["valid"]:
            problems.append(f"{fig} {g.fmt_key(k)}: invalid measurement: {v['reason']}")
    return vals, arts, problems


# --------------------------------------------------------------------------
# Control: the 10 uA / 2 pF points against the committed records
# --------------------------------------------------------------------------


def _row(md: str, key: Key, ncells: int) -> list[str] | None:
    proc, t, v = key
    pat = re.compile(rf"^\|\s*`{re.escape(proc)}`\s*\|\s*{t:g}\s*\|\s*{v:.2f}\s*\|(.*)\|\s*$", re.M)
    for m in pat.finditer(md):
        cells = [c.strip() for c in m.group(1).split("|")]
        if len(cells) >= ncells:
            return cells
    return None


def _num(cell: str) -> float:
    return float(cell.split()[0])


def reference_gain(md: str, key: Key):
    """(gain dB, GBW MHz, PM deg) of `key` from a gain-gbw-pm record's per-point table."""
    c = _row(md, key, 3)
    return None if c is None else (_num(c[0]), _num(c[1]), _num(c[2]))


def reference_ssp(md: str, key: Key):
    """(power uW, slew V/us) of `key` from a slew-swing-power record's per-point table."""
    c = _row(md, key, 5)
    return None if c is None else (_num(c[0]), _num(c[4]))


def selected_records(root: Path = REPO_ROOT) -> dict[str, str]:
    sel = json.loads((root / "sim" / "report" / "selection.json").read_text())["experiments"]
    ssp = sel["slew-swing-power"]
    return {
        "gain-gbw-pm": sel["gain-gbw-pm"],
        "power": ssp["power"] if isinstance(ssp, dict) else ssp,
        "slew": ssp["slew"] if isinstance(ssp, dict) else ssp,
    }


def control_failures(nominal_vals: dict[str, dict[Key, dict]], refs: dict[str, str]) -> list[str]:
    """Compare the 10 uA / 2 pF values with the committed records' values.

    `nominal_vals[fig][key]` are flatten() dicts; `refs` maps `gain-gbw-pm` /
    `power` / `slew` to the record markdown text. Returns problem strings.
    """
    bad: list[str] = []
    for key in POINTS["ac"]:
        v, ref = nominal_vals["ac"].get(key), reference_gain(refs["gain-gbw-pm"], key)
        lab = g.fmt_key(key)
        if v is None or not v["valid"] or ref is None:
            bad.append(f"control gain-gbw-pm {lab}: value or committed reference missing")
            continue
        if abs(v["gain_db"] - ref[0]) > CTRL_GAIN_DB:
            bad.append(f"control {lab}: gain {v['gain_db']:.3f} dB vs record {ref[0]:.3f} dB")
        if abs(v["gbw_mhz"] / ref[1] - 1) > CTRL_GBW_REL:
            bad.append(f"control {lab}: GBW {v['gbw_mhz']:.4f} MHz vs record {ref[1]:.4f} MHz")
        if abs(v["pm_deg"] - ref[2]) > CTRL_PM_DEG:
            bad.append(f"control {lab}: PM {v['pm_deg']:.3f} deg vs record {ref[2]:.3f} deg")
    for fig, field, i, tol_kind, tol in (
        ("power", "power_uw", 0, "rel", CTRL_POWER_REL), ("slew", "slew_vus", 1, "abs", CTRL_SLEW_VUS)
    ):
        for key in POINTS[fig]:
            v, ref = nominal_vals[fig].get(key), reference_ssp(refs[fig], key)
            lab = g.fmt_key(key)
            if v is None or not v["valid"] or ref is None:
                bad.append(f"control {fig} {lab}: value or committed reference missing")
                continue
            d = abs(v[field] / ref[i] - 1) if tol_kind == "rel" else abs(v[field] - ref[i])
            if d > tol:
                bad.append(f"control {fig} {lab}: {v[field]:.4g} vs record {ref[i]:.4g}")
    return bad


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


def _pct(x: float, ref: float) -> str:
    return f"{(x / ref - 1) * 100:+.1f} %" if ref else "n/a"


def sweep_table(title: str, axis_label: str, axis_vals: list, fmt_axis, lookup, cols: list[tuple[str, str, str]],
                points: list[Key]) -> list[str]:
    """Markdown table: one row per (point, axis value). `cols`: (header, field, fmt).
    `lookup(axis_val, point)` -> flatten dict or None. A delta-vs-nominal column
    is added for each field, relative to the nominal axis value of the same point."""
    head = ["point", "corner", axis_label] + [h for h, _, _ in cols] + [f"d{h.split(' ')[0]} vs nominal" for h, _, _ in cols]
    out = [f"### {title}", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for p in points:
        ref = None
        for av, nominal_flag in axis_vals:
            if nominal_flag:
                ref = lookup(av, p)
        for av, nominal_flag in axis_vals:
            v = lookup(av, p)
            lab = mc.POINT_NAMES.get(p, "")
            cell = [lab, g.fmt_key(p), fmt_axis(av) + (" (control)" if nominal_flag else "")]
            if v is None or not v.get("valid"):
                cell += ["INVALID"] * len(cols) + [""] * len(cols)
            else:
                cell += [format(v[f], fm) for _, f, fm in cols]
                cell += [
                    _pct(v[f], ref[f]) if ref and ref.get("valid") else "n/a" for _, f, _ in cols
                ]
            out.append("| " + " | ".join(cell) + " |")
    return out + [""]


def build_record(*, record, stamp, pdk, ngspice, klt_ver, vals, ctrl_bad, refs, backend_lines, dut_sha, texts) -> str:
    """The record markdown. `vals[unit name][key]` are flatten() dicts."""
    def get(fig, ib, cl):
        d = vals.get(unit_name({"fig": fig, "ibias_a": ib, "cl_f": cl}), {})
        return lambda p: d.get((p[0], float(p[1]), float(p[2])))

    L = [f"# Record {record}", ""]
    add = L.append
    add(f"- **Record ID**: {record}")
    add("- **Claim**: sensitivity DATA of GBW, phase margin, slew and quiescent power of the committed sized schematic "
        "to the external bias current `ibias` and the load capacitance CL, at named PVT corner points. "
        "**No verdict is given and no spec row is graded or changed** (issue #114; evidence for #42).")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M:%S} UTC")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}`")
    add(f"- **Tools**: ngspice (local: {ngspice}), klt `{klt_ver}`")
    L += backend_lines
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (wrapper-normalised, device body verbatim; normalised sha256 `{dut_sha}`), "
        f"snapshotted in full in `netlist-snapshots/{record}.spice`")
    L += mc.fingerprint_lines(texts)
    add("- **Statistical convention**: N/A -- deterministic corner points; no mismatch or Monte Carlo.")
    add("- **Passive-section policy**: every MOS corner uses the `res_typical` / `mimcap_typical` sections, as the sibling experiments.")
    add("- **Ranges are exploratory**: `ibias` +-20 % of 10 uA and CL in {1, 2, 4, 10} pF are NOT consumer requirements. "
        "`spec/target-spec.md` records no amplifier bias tolerance and no CL other than DR-1's 2 pF, so there is nothing to grade against.")
    add("- **Corner points** (named from `gain-gbw-pm` record "
        f"{Path(refs['gain-gbw-pm_id']).stem} and `slew-swing-power` record {Path(refs['slew_id']).stem}): "
        + "; ".join(f"{n} = {g.fmt_key(p)}" for p, n in mc.POINT_NAMES.items()))
    add("")
    if ctrl_bad:
        add("## CONTROL MISMATCH")
        add("")
        add("The 10 uA / 2 pF points do NOT reproduce the committed records; treat the sweep values below as unverified.")
        L += [f"- {b}" for b in ctrl_bad]
        add("")
    else:
        add("## Control")
        add("")
        add("The 10 uA / 2 pF points (marked `(control)`) reproduce the committed records' per-point GBW, PM, DC gain, "
            f"power and slew within the record rounding (GBW {CTRL_GBW_REL:.0e} rel, PM {CTRL_PM_DEG} deg, gain {CTRL_GAIN_DB} dB, "
            f"power {CTRL_POWER_REL:.0e} rel, slew {CTRL_SLEW_VUS} V/us).")
        add("")
    add("## Results")
    add("")
    ib_axis = [(ib, math.isclose(ib, IBIAS_NOM_A)) for ib in IBIAS_SWEEP_A]
    cl_axis = [(cl, math.isclose(cl, CL_NOM_F)) for cl in CL_SWEEP_F]
    acols = [("GBW (MHz)", "gbw_mhz", ".3f"), ("PM (deg)", "pm_deg", ".2f"), ("gain (dB)", "gain_db", ".2f")]
    L += sweep_table("GBW and PM vs ibias (CL = 2 pF)", "ibias (uA)", ib_axis, lambda x: f"{x * 1e6:g}",
                     lambda ib, p: get("ac", ib, CL_NOM_F)(p), acols, POINTS["ac"])
    L += sweep_table("GBW and PM vs CL (ibias = 10 uA)", "CL (pF)", cl_axis, lambda x: f"{x * 1e12:g}",
                     lambda cl, p: get("ac", IBIAS_NOM_A, cl)(p), acols, POINTS["ac"])
    L += sweep_table("Quiescent power vs ibias (no load)", "ibias (uA)", ib_axis, lambda x: f"{x * 1e6:g}",
                     lambda ib, p: get("power", ib, None)(p),
                     [("power (uW)", "power_uw", ".2f"), ("supply current (uA)", "idd_ua", ".2f")], POINTS["power"])
    L += sweep_table("Slew rate vs ibias (CL = 2 pF; slower of rise/fall)", "ibias (uA)", ib_axis, lambda x: f"{x * 1e6:g}",
                     lambda ib, p: get("slew", ib, CL_NOM_F)(p), [("slew (V/us)", "slew_vus", ".2f")], POINTS["slew"])
    add("Deltas are relative to the 10 uA (resp. 2 pF) control of the same corner point. Only the sweep variable differs between rows; "
        "the committed benches are otherwise used verbatim.")
    add("")
    add("## Reproduction")
    add("")
    add("```")
    add("python3 sim/ibias-cl-sensitivity/run_ibias_cl_sensitivity.py")
    add("```")
    add("")
    L += mc.inputs_section(texts)
    return "\n".join(L)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def _backend_desc(report: dict) -> str:
    r = (report.get("environment") or {}).get("remote") or {}
    if not r:
        return "no remote block in the klt report"
    return f"{r.get('provider')} job `{r.get('job_id')}` ({'Spot ' if r.get('spot') else 'on-demand '}{r.get('instance_type')})"


def smoke(pdk: Pdk) -> int:
    print(f"smoke: {NOMINAL}, ac bench, local single unit, PDK={pdk.path}")
    with tempfile.TemporaryDirectory(prefix="icl-smoke-") as scratch:
        work = Path(scratch)
        tb = materialise("ac", work, pdk, IBIAS_NOM_A, CL_NOM_F)
        req = make_request("ac", tb, pdk, [NOMINAL], IBIAS_NOM_A)
        try:
            rep = run_klt(req, work / "out", "local", work)
        except KltError as exc:
            print(f"SMOKE FAILED: {exc}")
            return 1
        vals, _, problems = analyse("ac", rep, [NOMINAL], IBIAS_NOM_A)
    v = vals.get((NOMINAL[0], NOMINAL[1], NOMINAL[2]))
    if problems or not v or not v["valid"]:
        print(f"SMOKE FAILED: {problems}")
        return 1
    print(f"  GBW {v['gbw_mhz']:.3f} MHz, PM {v['pm_deg']:.2f} deg")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="one nominal point, local, no record")
    ap.add_argument("--backend", help="klt execution backend (default: klt's own resolution, e.g. $KLT_SIM_BACKEND)")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0)
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    args = ap.parse_args(argv)

    pdk = find_pdk()
    if args.smoke:
        return smoke(pdk)

    record, stamp = allocate_record_id(REPO_ROOT)
    paths = claim_record_paths(HERE, record, plots=False)
    recs = selected_records()
    refs = {
        "gain-gbw-pm": (REPO_ROOT / recs["gain-gbw-pm"]).read_text(),
        "power": (REPO_ROOT / recs["power"]).read_text(),
        "slew": (REPO_ROOT / recs["slew"]).read_text(),
    }
    ulist = units()
    ngspice, kver = ngspice_version(), klt_version()
    print(f"record {record}: {len(ulist)} klt requests, PDK={pdk.path}, klt {kver}")
    vals: dict[str, dict] = {}
    arts: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    reqs: dict[str, dict] = {}
    descs: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="icl-") as scratch:
        work = Path(scratch)
        for u in ulist:
            wd = work / u["name"]
            tb = materialise(u["fig"], wd, pdk, u["ibias_a"], u["cl_f"])
            req = make_request(u["fig"], tb, pdk, POINTS[u["fig"]], u["ibias_a"])
            req["batch"] = batch_block(args)
            reqs[u["name"]] = req
            try:
                report = run_klt_retrying(req, wd / "out", args.backend, wd,
                                          retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
            except KltError as exc:
                # never fall back to a local run, never write a partial record
                print(f"ERROR: {u['name']}: the klt request could not be run: {str(exc)[-600:]}", file=sys.stderr)
                print("NO RECORD WRITTEN.", file=sys.stderr)
                return 2
            v, a, problems = analyse(u["fig"], report, POINTS[u["fig"]], u["ibias_a"])
            if problems:
                print(f"ERROR: {u['name']}: " + "; ".join(problems[:6]), file=sys.stderr)
                print("NO RECORD WRITTEN.", file=sys.stderr)
                return 2
            vals[u["name"]], arts[u["name"]], reports[u["name"]] = v, a, report
            descs[u["name"]] = _backend_desc(report)
            print(f"  {u['name']}: {len(v)} points ok ({descs[u['name']]})")

        nominal_vals = {
            fig: vals[unit_name({"fig": fig, "ibias_a": IBIAS_NOM_A, "cl_f": CL_NOM_F if fig != "power" else None})]
            for fig in ("ac", "power", "slew")
        }
        ctrl_bad = control_failures(nominal_vals, refs)

        cdir = paths["corners"]
        cdir.mkdir(parents=True, exist_ok=False)
        for name, a in arts.items():
            d = cdir / name
            d.mkdir()
            for k, art in a.items():
                stem = g.point_stem(k)
                if art.get("log"):
                    shutil.copyfile(art["log"], d / f"{stem}.log")
                if art.get("deck"):
                    shutil.copyfile(art["deck"], d / f"{stem}.cir")
            (d / "klt-report.json").write_text(json.dumps(sanitise_report(reports[name]), indent=1))
        (cdir / "results.json").write_text(json.dumps(
            {n: {g.fmt_key(k): v for k, v in d.items()} for n, d in vals.items()}, indent=1, sort_keys=True))

        dut_text = load_dut_text()
        dut_sha = hashlib.sha256(dut_text.encode()).hexdigest()
        lines = [f"* netlist snapshot for record {record} (issue #114, ibias / CL sensitivity)"]
        for name, req in reqs.items():
            lines.append(f"* ---- conditions: klt sim request ({name}) ----")
            lines += ["* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()]
        lines += ["", "* ---- DUT (wrapper-normalised) ----", dut_text]
        for fig, p in BENCH.items():
            lines += [f"* ---- testbench: {mc.BENCHES_REL[fig]} (verbatim; Ibias / CL lines substituted per request) ----", p.read_text(), ""]
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        paths["snapshot"].write_text("\n".join(lines))

        texts = {fig: p.read_text() for fig, p in BENCH.items()}
        jobs = sorted(set(descs.values()))
        backend_lines = [
            "- **Execution**: one small `klt sim` corner request per (figure, sweep value); "
            f"{len(ulist)} requests; backend(s): " + "; ".join(jobs),
        ] + [f"  - `{n}`: {d}" for n, d in descs.items()]
        md = build_record(
            record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, klt_ver=kver, vals=vals, ctrl_bad=ctrl_bad,
            refs={"gain-gbw-pm_id": recs["gain-gbw-pm"], "slew_id": recs["slew"]},
            backend_lines=backend_lines, dut_sha=dut_sha, texts=texts,
        )
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        paths["record"].write_text(md)
    print(f"wrote {paths['record']}")
    if ctrl_bad:
        print("CONTROL PROBLEMS:")
        for b in ctrl_bad:
            print(f"  - {b}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
