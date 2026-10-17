"""Split out of characterization_report.py (issue #219); moved verbatim, no logic changes."""
from __future__ import annotations

import hashlib
import json
import os
from report_common import (
    OUT_DIR_REL, OUT_NAME, coverage_text)
from studies import (
    SUPPLEMENTARY, study_observation)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def link_from_reports(path: str) -> str:
    return os.path.relpath(path, OUT_DIR_REL).replace(os.sep, "/")


def mc_cell(m: dict) -> str:
    if m["status"] == "unknown":
        return "unknown"
    return f"{m['status']} (`{m['fingerprint'][:16]}`)"


def sc_link(src: dict) -> str:
    return f"[`{src['record_id']}`]({link_from_reports(src['path'])})"


def render_psrr_study(a, st: dict) -> None:
    """Markdown body of the bound-free PSRR study (issue #173): measured, ungraded."""
    rv, pop = st["retained_evidence"], st["population"]
    pa = rv["partitions"]
    a(f"- **Grading**: {st['grading']}")
    a(f"- **Retained evidence (figures re-derived from it)**: [`{rv['path']}`]({link_from_reports(rv['path'])}): "
      f"{rv['psrr_dat_files']} `<cell>.psrr.dat` curves, aggregate sha256 `{rv['sha256']}` ({rv['sha256_method']}); "
      f"[`{pa['path']}`]({link_from_reports(pa['path'])}) sha256 `{pa['sha256']}`: "
      + ", ".join(f"`{m}` {n} cells" for m, n in pa["excitations"].items())
      + f" (each the exact {pop['cells']}-cell union, no overlap)")
    a(f"- **Population**: {pop['cells']} cells ({pop['typical_passive_cells']} typical-passive, "
      f"{pop['non_typical_cells']} non-typical), each paired from the {', '.join(pop['excitations'])} excitations")
    a("")
    a(f"| figure (information only, no verdict) | worst (dB) | binding cell (MOS / T / VDD / RZ / CC) | best (dB) | "
      f"best cell | worst over the {pop['typical_passive_cells']} typical-passive cells (dB) | passive shift of the "
      f"worst case (dB) | most negative delta vs typical (dB) | at cell | most positive delta vs typical (dB) | "
      f"at cell | max abs delta (dB) |")
    a("|---|---|---|---|---|---|---|---|---|---|---|---|")
    cell = lambda c: f"{c['point']} / RZ {c['rz']} / CC {c['cc']}"
    for f in st["figures"].values():
        d, t = f["delta_vs_typical"], f["typical_passive_worst"]
        a(f"| {f['label']} | {f['worst']['value']} | {cell(f['worst'])} | {f['best']['value']} | {cell(f['best'])} | "
          f"{t['value']} | {f['passive_shift_of_worst']} | {d['most_negative']['value']} | {cell(d['most_negative'])} | "
          f"{d['most_positive']['value']} | {cell(d['most_positive'])} | {d['max_abs']} |")
    a("")
    prim = [f for f in st["figures"].values() if "primary_record_worst" in f["typical_passive_worst"]]
    if prim:
        a("- Typical-passive worst values agree with the selected 45-point PSRR record: "
          + "; ".join(f"{f['label']} {f['typical_passive_worst']['value']} dB vs "
                      f"{f['typical_passive_worst']['primary_record_worst']} dB" for f in prim))


def render_step_study(a, st: dict) -> None:
    """Markdown body of the ungraded follower step-response study (issue #198): measured."""
    rv, pop = st["retained_evidence"], st["population"]
    a(f"- **Grading**: {st['grading']}")
    a(f"- **Retained evidence (figures re-derived from it)**: [`{rv['path']}`]({link_from_reports(rv['path'])}): "
      f"{rv['dat_files']} per-point `.dat` transients (rising and falling edge each), aggregate sha256 "
      f"`{rv['sha256']}` ({rv['sha256_method']})")
    a(f"- **Population**: {pop['points']} PVT points, {pop['valid']} measured validly, {pop['invalid']} invalid; "
      f"monotonic at {pop['monotonic']}/{pop['valid']} valid points; edges per point: {', '.join(pop['edges_per_point'])}")
    a("")
    a("| figure (information only, no verdict) | worst | binding point (MOS / T / VDD) |")
    a("|---|---|---|")
    for name, f in st["figures"].items():
        unit = f["unit"]
        if "not_settled" in f:  # settling: the worst cell names not-settled explicitly when it applies
            ns = f["not_settled"]
            val = (f"not settled ({ns['count']} point{'s' if ns['count'] != 1 else ''} not settled)"
                   if f["value"] == "not settled" else f"{f['value']} {unit}")
            a(f"| {name} | {val} | {f['point'] or '-'} |")
            if ns["count"]:
                a(f"| {name} -- not settled (status, not a value) | {ns['status']} | "
                  + "; ".join(ns["points"]) + " |")
        elif name == "Overshoot (worse edge)":
            a(f"| {name} | {f['value']} {unit} (range {f['range'][0]} .. {f['range'][1]}) | {f['point']} |")
        elif name == "Monotonicity":
            a(f"| {name} | {f['non_monotonic']}/{f['of_valid']} valid points non-monotonic | - |")
            if f["non_monotonic"]:  # name the points, as the not-settled row does (list may be truncated)
                shown = ("all listed" if f["list_complete"]
                         else f"first {len(f['points'])} of {f['non_monotonic']} listed")
                a(f"| {name} -- non-monotonic points | {shown} | " + "; ".join(f["points"]) + " |")
        elif name == "Validity":
            a(f"| {name} | {f['invalid']}/{f['of']} points invalid | - |")
        else:
            a(f"| {name} | {f['value']} {unit} | {f['point']} |")
    a("")


SUPPLEMENTARY["psrr-passive-corners-full"]["render"] = render_psrr_study  # issue #173: bound-free body
SUPPLEMENTARY["step-response-follower"]["render"] = render_step_study  # issue #198: ungraded body


def render_offset_drift(a, od: dict) -> None:
    """Issue #184: derived offset temperature drift and systematic / statistical split."""
    a("")
    a("## Offset temperature drift and systematic-vs-statistical split (derived; not graded)")
    a("")
    if not od["available"]:
        a(f"Not available: {od['reason']}.")
        return
    a(f"Derived offline from the retained per-sample offsets of the selected offset Monte Carlo record "
      f"([`{od['record']}`]({link_from_reports(od['record'])}), sha256 `{od['record_sha256']}`; samples "
      f"[`{od['input']}`]({link_from_reports(od['input'])}), sha256 `{od['input_sha256']}`). No simulation was run, "
      f"no verdict is given and no bound is proposed. Offsets in uV, drifts in uV/C; N={od['n_per_point']} per point; "
      f"every mean carries its standard error sigma/sqrt(N). A drift is *resolved* only if |drift| > "
      f"{od['resolution_threshold_se']:g} standard errors, otherwise *unresolved* (noise, not a measured drift).")
    temps = od["temps_c"]
    a("")
    a("### Mean offset and drift per process corner and supply")
    a("")
    a("| Process | VDD (V) | " + " | ".join(f"mean at {t:g} C (uV)" for t in temps)
      + " | End-to-end drift (uV/C) | Best-fit drift (uV/C) | Segment drifts (uV/C) |")
    a("|---|---|" + "---|" * len(temps) + "---|---|---|")

    def dcell(d):
        if d is None:
            return "n/a"
        return f"{d['uv_per_c']:+.3f} +/- {d['se_uv_per_c']:.3f} ({'resolved' if d['resolved'] else 'unresolved'})"
    for g in od["groups"]:
        a(f"| {g['process']} | {g['vdd_v']} | "
          + " | ".join(f"{p['mean_uv']:+.1f} +/- {p['se_mean_uv']:.1f}" for p in g["points"])
          + f" | {dcell(g['drift_end_to_end'])} | {dcell(g['drift_fit'])} | "
          + ("; ".join(f"{s_['from_c']:g}..{s_['to_c']:g} C: {dcell(s_)}" for s_ in g["segments"]) or "n/a") + " |")
    a("")
    a("### Sigma versus temperature")
    a("")
    a("| Process | VDD (V) | " + " | ".join(f"sigma at {t:g} C (uV)" for t in temps) + " | max/min |")
    a("|---|---|" + "---|" * len(temps) + "---|")
    for g in od["groups"]:
        r = g["sigma_vs_t"]["max_over_min"]
        a(f"| {g['process']} | {g['vdd_v']} | " + " | ".join(f"{p['sigma_uv']:.1f}" for p in g["points"])
          + f" | {'n/a' if r is None else f'{r:.3f}'} |")
    a("")
    a("### Worst |mean| + 3 sigma per process corner and supply, split")
    a("")
    a("| Process | VDD (V) | Temperature (C) | \\|mean\\| systematic (uV) | 3 sigma statistical (uV) | \\|mean\\| + 3 sigma (uV) | Systematic share |")
    a("|---|---|---|---|---|---|---|")
    for g in od["groups"]:
        w = g["worst_point"]
        sh = w["systematic_fraction"]
        a(f"| {g['process']} | {g['vdd_v']} | {w['temp_c']:g} | {w['abs_mean_uv']:.1f} | {w['three_sigma_uv']:.1f} | "
          f"{w['abs3_uv']:.1f} | {'n/a' if sh is None else f'{sh * 100:.1f} %'} |")
    w = od["worst_overall"]
    a("")
    a(f"Worst point of the grid: {w['process']} / {w['temp_c']:g} C / {w['vdd_v']} V, |mean| + 3 sigma = "
      f"{w['abs3_uv']:.1f} uV = {w['abs_mean_uv']:.1f} uV systematic (|mean|) + {w['three_sigma_uv']:.1f} uV statistical "
      f"(3 sigma); mismatch-seed independence across temperatures: {od['seed_independence']}.")
    a("")
    for l in od["limitations"]:
        a(f"- Limitation: {l}")


def render_md(rep: dict) -> str:
    L = []
    a = L.append
    a(f"# {rep['title']}")
    a("")
    a("Generated by `sim/report/characterization_report.py` from the committed records listed under Sources "
      "(selection: `sim/report/selection.json`). Do not edit by hand; regenerate and compare with "
      "`python3 sim/report/characterization_report.py --check`. The records, not this file, are the evidence; "
      "no simulator is run to produce it.")
    a("")
    d = rep["dut"]
    a(f"- **DUT**: `{d['netlist']}` (normalised sha256 prefix `{d['normalised_sha256_prefix']}`"
      + (f", full `{d['normalised_sha256']}`" if d["normalised_sha256"] else "") + ")")
    a(f"- **Spec**: `{rep['spec']['path']}` (sha256 `{rep['spec']['sha256']}`)")
    s = rep["summary"]
    a(f"- **Ratified rows judged**: {s['ratified_rows_judged']} ({s['pass']} PASS, {s['fail']} FAIL); "
      f"{s['measured_no_bound']} measured with the bound open (no verdict); {s['not_measured']} not measured"
      + (f"; {s['proposed_not_graded']} proposed, not ratified (not graded)" if s["proposed_not_graded"] else ""))
    a("")
    a("## Spec rows (spec/target-spec.md Sec. 2)")
    a("")
    a("| Row | Bound | Status | Verdict | Points | Worst value | Binding / worst corner | Source |")
    a("|---|---|---|---|---|---|---|---|")
    for r in rep["rows"]:
        pts = (f"{r['points_pass']}/{r['points_total']}" if r["points_pass"] is not None
               else (str(r["points_total"]) if r["points_total"] else "-"))
        verdict = f"**{r['verdict']}**" if r["verdict"] else "none"
        bound = "open" if r["bound_open"] and r["status"] != "not-measured" else r["spec_bound"]
        if r["bound_open"]:
            bound = "open (no ratified bound)" if r["key"] != "post-layout" else r["spec_bound"]
        sc = f"[`{r['source']['record_id']}`]({link_from_reports(r['source']['path'])})" if r["source"] else "no record"
        if r["status"] == "proposed-not-graded":
            bound = r["spec_bound"]  # carries the "proposed, not ratified: " qualifier
            sc = "not graded (see spec status)"
            if r.get("icmr"):  # source-pinned measurement, still not graded
                sc = f"not graded; measured in {sc_link(r['source'])}"
        cell = lambda s: (s or "-").replace("|", "\\|")  # e.g. the offset row's "|mean| + 3 sigma"
        a(f"| {r['row']} | {bound} | {r['status']} | {verdict} | {pts} | {cell(r['worst'])} | "
          f"{cell(r['worst_corner'])} | {sc} |")
    a("")
    a("## Row details")
    for r in rep["rows"]:
        a("")
        a(f"### {r['row']}")
        a("")
        a(f"- **Status**: {r['status']}" + (f"; verdict **{r['verdict']}**" if r["verdict"] else "; no verdict issued"))
        if r.get("spec_status"):
            a(f"- **Spec status column (verbatim text, not a measurement)**: {r['spec_status']}")
        if r["coverage"]:
            a(f"- **Coverage**: {coverage_text(r['coverage'])}")
        if r["source"]:
            a(f"- **Source**: [`{r['source']['path']}`]({link_from_reports(r['source']['path'])}), "
              f"sha256 `{r['source']['sha256']}`")
        if r.get("icmr"):
            pv = r["icmr"]["provenance"]
            a(f"- **Measured evidence (not graded)**: DUT normalised sha256 `{pv['dut_sha256'][:16]}`; PDK open_pdks "
              f"`{(pv['pdk_open_pdks'] or 'n/a')[:12]}`; ngspice local {pv['ngspice_local'] or 'n/a'} / klt engine "
              f"{pv['ngspice_engine'] or 'n/a'}; klt client {pv['klt_client'] or 'n/a'}; backend "
              f"{pv['backend'] or 'n/a'}; measurement-configuration freshness {mc_cell(pv['measurement_config'])}")
            rv = r["icmr"]["retained_evidence"]
            a(f"- **Retained per-sample evidence (cross-checked)**: [`{rv['path']}`]({link_from_reports(rv['path'])}), "
              f"sha256 `{rv['sha256']}` ({rv['samples']} samples at {rv['points']} PVT points)")
            if r["icmr"]["addendum"]:
                ad = r["icmr"]["addendum"]
                a(f"- **Record addendum**: [`{ad['path']}`]({link_from_reports(ad['path'])}), sha256 `{ad['sha256']}`")
        for f in r["figures"]:
            a(f"- {f['label']}: {f['value']}" + (f" ({f['corner']})" if f["corner"] else ""))
        for l in r["limitations"]:
            for st in rep.get("supplementary", {}).values():
                l = l.replace(f"`{st['path']}`", f"[`{st['path']}`]({link_from_reports(st['path'])})")
            a(f"- Limitation: {l}")
    if rep.get("offset_drift"):
        render_offset_drift(a, rep["offset_drift"])
    if rep.get("supplementary"):
        a("")
        a("## Supplementary side studies (measured side evidence; not graded)")
        a("")
        a("Selected explicitly under `supplementary` in `sim/report/selection.json`, separately from the records "
          "that grade the spec rows. Every figure below is re-derived from the study's own RZ x CC tables and "
          "checked against its verdict header (a bound-free study: re-derived from its retained per-cell curves or "
          "per-point transients and checked against its rounded tables, with no verdict at all); none is counted in "
          "a row's points or verdict or in the ratified-row summary above.")
        for k, st in rep["supplementary"].items():
            a("")
            a(f"### {k}")
            a("")
            a(f"- **Source**: [`{st['path']}`]({link_from_reports(st['path'])}), sha256 `{st['sha256']}`")
            a(f"- **Scope**: {st['scope']['text']}")
            dc = st["dut_check"]
            a(f"- **Provenance**: DUT normalised sha256 `{st['dut_sha256'][:16]}` ("
              + ("matches the selected records" if dc["matches_selected_records"] else "no selected record to compare")
              + "; " + ("matches the current netlist" if dc["matches_current_netlist"]
                        else "current netlist not checked (archival)")
              + f"); PDK open_pdks `{(st['pdk_open_pdks'] or 'n/a')[:12]}`; ngspice local {st['ngspice_local'] or 'n/a'} / "
              f"klt engine {st['ngspice_engine'] or 'n/a'}; klt client {st['klt_client'] or 'n/a'}; backend "
              f"{st['backend'] or 'n/a'}; measurement-configuration freshness {mc_cell(st['measurement_config'])}")
            a("- **Rows it informs (not graded)**: " + (", ".join(st["rows"]) if st["rows"] else
              "none -- settling and overshoot have no spec row (a bound would need a spec/ decision record); "
              "the observations stand alone as evidence"))
            render = SUPPLEMENTARY[k].get("render")
            if render:
                render(a, st)
            for fk, f in ([] if st.get("bound_free") else st["figures"].items()):
                a(f"- {study_observation(f)} (record verdict {f['verdict_in_record']}; information only, not graded)")
            for fk, why in sorted(st["not_measured"].items()):
                a(f"- {fk}: {why}")
            for l in st["limitations"]:
                a(f"- Limitation: {l}")
    a("")
    a("## Sources")
    a("")
    a("| Experiment | Record | sha256 | DUT prefix | PDK (open_pdks) | ngspice local / klt engine | klt client | Backend | Measurement config |")
    a("|---|---|---|---|---|---|---|---|---|")
    for e, sr in rep["sources"].items():
        pdk = (sr["pdk_open_pdks"] or "n/a")[:12]
        if sr["pdk_client_resolved"] and sr["pdk_client_resolved"] != sr["pdk_open_pdks"]:
            pdk += f" (client-resolved {sr['pdk_client_resolved'][:12]})"
        a(f"| {e} | [`{sr['record_id']}`]({link_from_reports(sr['path'])}) | `{sr['sha256']}` | "
          f"`{sr['dut_sha256'][:16]}` | `{pdk}` | {sr['ngspice_local'] or 'n/a'} / {sr['ngspice_engine'] or 'n/a'} | "
          f"{sr['klt_client'] or 'n/a'} | {sr['backend'] or 'n/a'} | {mc_cell(sr['measurement_config'])} |")
    a("")
    a("## Report-level limitations")
    a("")
    for l in rep["limitations"] or ["none"]:
        a(f"- {l}")
    for w in rep["warnings"]:
        a(f"- WARNING: {w}")
    a("")
    return "\n".join(L)


def render_json(rep: dict) -> str:
    return json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def report_hash(report_json: str) -> str:
    """sha256 of the exact UTF-8 bytes of the report JSON (final newline included)."""
    return "sha256:" + hashlib.sha256(report_json.encode("utf-8")).hexdigest()


def render_evidence(report_json: str) -> str:
    """Generic evidence wrapper (klt signoff item 8) for the report JSON.

    ``status: pass`` asserts only that the aggregated report was produced and
    verified against its selected records; it does NOT assert that every spec
    row passes.  Per-row verdicts and limitations live in the report itself.
    Only called after build() succeeded, so a generator error never yields a
    passing wrapper.
    """
    rep = json.loads(report_json)
    ev = {
        "kind": "generic",
        "status": "pass",
        "schema_version": 1,
        "subject": "characterization-report",
        "claim": "The aggregated characterization report was generated from its selected committed "
                 "records and verified; this is not a claim that every spec row passes.",
        "report": {"path": f"{OUT_DIR_REL}/{OUT_NAME}.json", "markdown": f"{OUT_DIR_REL}/{OUT_NAME}.md"},
        "report_summary": rep["summary"],
        # issue #173/#198: the hashed side evidence the report attaches to its rows; never graded,
        # never part of report_summary (a bound-free study carries no verdict at all)
        "report_supplementary": {
            k: {"graded": False, "bound_free": bool(st.get("bound_free")), "rows": st["rows"], "path": st["path"],
                "sha256": st["sha256"], "cells": st["scope"]["cells"],
                **({"verdict": None, "retained_evidence_sha256": st["retained_evidence"]["sha256"],
                    **({"worst_db": {f["label"]: f["worst"]["value"] for f in st["figures"].values()}}
                       if all("worst" in f for f in st["figures"].values()) else
                       {"worst": {name: f["value"] for name, f in st["figures"].items() if "value" in f}})}
                   if st.get("bound_free") else {})}
            for k, st in rep.get("supplementary", {}).items()},
        # issue #184: derived (not simulated) view of committed evidence; never graded
        **({"report_derived": {"offset_temperature_drift": {
            "graded": False, "verdict": None, "available": rep["offset_drift"]["available"],
            **({"record_sha256": rep["offset_drift"]["record_sha256"], "input": rep["offset_drift"]["input"],
                "input_sha256": rep["offset_drift"]["input_sha256"]} if rep["offset_drift"]["available"] else {})}}}
           if rep.get("offset_drift") else {}),
        "provenance": {"input": {"path": f"{OUT_DIR_REL}/{OUT_NAME}.json",
                                 "content_hash": report_hash(report_json)}},
    }
    return json.dumps(ev, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
