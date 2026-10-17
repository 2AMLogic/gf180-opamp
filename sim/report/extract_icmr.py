"""Split out of characterization_report.py (issue #219); moved verbatim, no logic changes."""
from __future__ import annotations

import re
from pathlib import Path
from report_common import (
    ReportError, common_limitations, corner_sort_key, coverage_of, fmt_point, header_of, parse_provenance,
    section, sha256_file, split_cells)


# --------------------------------------------------------------------------
# input common-mode range (issue #90): measured, never graded while proposed
# --------------------------------------------------------------------------
ICMR_EXP = "input-common-mode"
ICMR_ROW_KEY = "input-common-mode-range"
ICMR_TARGET_MV = 1200  # the explicit VCM sample the record grades at every PVT point
_PT = r"(\w+) / (-?\d+) C / ([\d.]+) V"
_IV_RX = re.compile(r"\[(\d+\.\d+), (\d+\.\d+)\] V")


def _mv(s: str) -> int:
    return round(float(s) * 1000)


def _v(mv: int) -> str:
    return f"{mv / 1000:.3f}"


def fmt_ivs(ivs: list) -> str:
    return ", ".join(f"[{_v(i['lo'])}, {_v(i['hi'])}] V" for i in ivs) if ivs else "empty"


def _parse_ivs(cell: str, label: str, what: str) -> list:
    """'[a, b] V, [c, d] V' (disjoint components kept apart) or 'none'/'empty' -> [(lo_mv, hi_mv)]."""
    cell = cell.strip().rstrip(".")
    if cell in ("none", "empty"):
        return []
    found = [(_mv(a), _mv(b)) for a, b in _IV_RX.findall(cell)]
    if not found or _IV_RX.sub("", cell).replace(",", "").strip():
        raise ReportError(f"{label}: malformed interval list in {what}: '{cell}'")
    return found


def _check_disjoint(ivs: list, label: str, what: str) -> None:
    for lo, hi in ivs:
        if lo > hi:
            raise ReportError(f"{label}: inconsistent interval data in {what}: [{_v(lo)}, {_v(hi)}] V has low > high")
    for (_, a_hi), (b_lo, _) in zip(ivs, ivs[1:]):
        if b_lo <= a_hi:
            raise ReportError(f"{label}: inconsistent interval data in {what}: components overlap or are "
                              "out of order (disjoint passing intervals must be listed separately, low to high)")


def _unc(cell: str, label: str) -> int | None:
    cell = cell.strip()
    if cell == "scan edge":
        return None
    m = re.fullmatch(r"(\d+) mV", cell)
    if not m or int(m.group(1)) <= 0:
        raise ReportError(f"{label}: malformed transition bracket '{cell}' in the per-point interval table")
    return int(m.group(1))


def icmr_intersection(points: list, per_point: dict) -> list:
    """Intersection of every point's union of passing intervals, components kept disjoint.

    Mirrors the driver's rule: each endpoint keeps the bracket, neighbour and PVT
    point of the interval that set it (on a tie, the earlier point in grid order).
    A point without any interval empties the result.
    """
    cur = None
    for pt in points:
        tagged = [dict(i, lo_src=fmt_point(*pt), hi_src=fmt_point(*pt)) for i in per_point[pt]]
        if cur is None:
            cur = tagged
            continue
        nxt = []
        for a in cur:
            for b in tagged:
                lo_s = b if b["lo"] > a["lo"] else a
                hi_s = b if b["hi"] < a["hi"] else a
                lo, hi = max(a["lo"], b["lo"]), min(a["hi"], b["hi"])
                if lo <= hi:
                    nxt.append({"lo": lo, "hi": hi, "lo_unc": lo_s["lo_unc"], "hi_unc": hi_s["hi_unc"],
                                "lo_nb": lo_s["lo_nb"], "hi_nb": hi_s["hi_nb"],
                                "lo_src": lo_s["lo_src"], "hi_src": hi_s["hi_src"]})
        cur = sorted(nxt, key=lambda i: i["lo"])
        if not cur:
            return []
    return cur or []


def _grid_order(pts) -> list:
    """Grid order of the driver (process, VDD, T), so endpoint ties resolve the same way."""
    return sorted(pts, key=lambda p: (corner_sort_key(p[0]), float(p[2]), int(p[1])))


def _margin_claim(m, label: str, what: str) -> dict:
    if not m:
        raise ReportError(f"{label}: no '{what}' headline line")
    return m


def extract_icmr(text: str, label: str) -> dict:
    """Follower-biased ICMR record: per-point passing intervals, the conservative common
    interval, its edge-binding corners and brackets, the explicit 1.20 V sample and the
    smallest saturation margins, each re-derived from the record's own per-point tables.

    The retained per-sample evidence (samples.csv) is cross-checked separately
    (`icmr_crosscheck`), since it lives beside the record rather than in it.
    """
    prov = parse_provenance(text, label)
    head = header_of(text)
    ax = re.search(r"VCM scanned 0\.\.VDD at <= (\d+) mV .*?transitions refined to <= (\d+) mV", head)
    smp = re.search(r"^- \*\*Samples\*\*: (\d+) \(PVT point, VCM\) samples x \d+ excitations: "
                    r"(\d+) pass, (\d+) fail, (\d+) invalid\.", head, re.M)
    if not (ax and smp):
        raise ReportError(f"{label}: no VCM scan/refinement axes line or samples line in the record header")
    n_samples, n_pass, n_fail, n_inv = (int(x) for x in smp.groups())
    if n_pass + n_fail + n_inv != n_samples:
        raise ReportError(f"{label}: inconsistent sample counts ({n_pass} + {n_fail} + {n_inv} != {n_samples})")

    # ---- per-point passing intervals (every contiguous component, never bridged) ----
    sec = section(text, "Passing intervals at every PVT point")
    lines = [ln for ln in sec.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no per-point table under '## Passing intervals at every PVT point'")
    per_point, strict, st120, order = {}, {}, {}, []
    for ln in lines[2:]:
        c = split_cells(ln)
        m = re.fullmatch(_PT, c[0]) if c else None
        if not m or len(c) != 9:
            raise ReportError(f"{label}: malformed per-point interval row: {ln.strip()}")
        pt = m.groups()
        what = f"the per-point table at {c[0]}"
        if pt not in per_point:
            order.append(pt)
            per_point[pt] = []
            strict[pt] = _parse_ivs(c[8], label, f"{what} (strict column)")
            _check_disjoint(strict[pt], label, f"{what} (strict column)")
            st120[pt] = c[7]
        elif st120[pt] != c[7] or strict[pt] != _parse_ivs(c[8], label, what):
            raise ReportError(f"{label}: inconsistent interval data in {what}: its rows disagree on the 1.20 V "
                              "status or the strict intervals")
        if c[1] == "none":
            if per_point[pt] or c[2] != "0":
                raise ReportError(f"{label}: inconsistent interval data in {what}: 'none' beside other intervals")
            continue
        ivs = _parse_ivs(c[1], label, what)
        if len(ivs) != 1 or not c[2].isdigit() or int(c[2]) < 1:
            raise ReportError(f"{label}: malformed interval row in {what}: one interval and a sample count expected")
        (lo, hi), = ivs
        per_point[pt].append({"lo": lo, "hi": hi, "n": int(c[2]), "lo_unc": _unc(c[3], label), "lo_nb": c[4],
                              "hi_unc": _unc(c[5], label), "hi_nb": c[6]})
        _check_disjoint([(i["lo"], i["hi"]) for i in per_point[pt]], label, what)
    for pt in order:
        if st120[pt] not in ("pass", "fail", "invalid", "n/a"):
            raise ReportError(f"{label}: unknown 1.20 V status '{st120[pt]}' at {fmt_point(*pt)}")
        inside = any(i["lo"] <= ICMR_TARGET_MV <= i["hi"] for i in per_point[pt])
        if inside != (st120[pt] == "pass"):
            raise ReportError(f"{label}: inconsistent interval data at {fmt_point(*pt)}: 1.20 V status "
                              f"'{st120[pt]}' but 1.20 V is {'inside' if inside else 'outside'} its passing intervals")
    points = _grid_order(order)

    # ---- conservative common interval, re-derived and compared with the headline ----
    inter = icmr_intersection(points, per_point)
    inter_s = icmr_intersection(points, {p: [{"lo": lo, "hi": hi, "lo_unc": None, "hi_unc": None, "lo_nb": "",
                                              "hi_nb": ""} for lo, hi in strict[p]] for p in points})
    hl = section(text, "Headline")
    m = re.search(r"^- \*\*Intersection over the (\d+) PVT points\*\* \(every contiguous component\): (.*)$", hl, re.M)
    if m:
        claimed = _parse_ivs(m.group(2), label, "the headline intersection")
    elif re.search(r"^- \*\*Intersection of the passing ranges over the \d+ PVT points: EMPTY\.\*\*", hl, re.M):
        claimed = []
    else:
        raise ReportError(f"{label}: no headline intersection line")
    if m and int(m.group(1)) != len(points):
        raise ReportError(f"{label}: headline intersection claims {m.group(1)} PVT points; "
                          f"the per-point table has {len(points)}")
    if claimed != [(i["lo"], i["hi"]) for i in inter]:
        raise ReportError(f"{label}: headline intersection {fmt_ivs([{'lo': a, 'hi': b} for a, b in claimed])} "
                          f"disagrees with its own per-point table ({fmt_ivs(inter)})")
    tol = re.search(r"^- Intersection with the 1 mV tolerance: (.*?); strict \(0 mV\): (.*)$", hl, re.M)
    if not tol or _parse_ivs(tol.group(1), label, "the tolerance line") != claimed:
        raise ReportError(f"{label}: tolerance-sensitivity intersection line missing or disagrees with the headline")
    if _parse_ivs(tol.group(2), label, "the strict intersection") != [(i["lo"], i["hi"]) for i in inter_s]:
        raise ReportError(f"{label}: strict (0 mV) intersection disagrees with its own per-point strict intervals "
                          f"({fmt_ivs(inter_s)})")
    comp = next((i for i in inter if i["lo"] <= ICMR_TARGET_MV <= i["hi"]), None)
    cm = re.search(r"^- \*\*Component containing 1\.20 V\*\*: \[([\d.]+), ([\d.]+)\] V; low endpoint ([\d.]+) V set by "
                   + _PT + r" \(bracket (scan edge|\d+ mV) to a `([^`]+)` sample\), high endpoint ([\d.]+) V set by "
                   + _PT + r" \(bracket (scan edge|\d+ mV) to a `([^`]+)` sample\)\.$", hl, re.M)
    if comp is None:
        if cm or not re.search(r"^- \*\*1\.20 V is NOT inside the intersection\*\*", hl, re.M):
            raise ReportError(f"{label}: headline 1.20 V component disagrees with its own per-point table "
                              "(1.20 V is outside every common component)")
    else:
        if not cm:
            raise ReportError(f"{label}: no headline 'Component containing 1.20 V' line")
        g = cm.groups()
        got = {"lo": _mv(g[0]), "hi": _mv(g[1]), "lo_src": fmt_point(*g[3:6]), "lo_unc": _unc(g[6], label),
               "lo_nb": g[7], "hi_src": fmt_point(*g[9:12]), "hi_unc": _unc(g[12], label), "hi_nb": g[13]}
        if _mv(g[2]) != got["lo"] or _mv(g[8]) != got["hi"] or any(got[k] != comp[k] for k in got):
            raise ReportError(f"{label}: headline 1.20 V component (edges / binding corners / brackets) disagrees "
                              f"with its own per-point table ([{_v(comp['lo'])}, {_v(comp['hi'])}] V, low set by "
                              f"{comp['lo_src']}, high set by {comp['hi_src']})")

    # ---- explicit 1.20 V sample at every point ----
    sec = section(text, "Explicit 1.20 V samples")
    t120 = {}
    for ln in [ln for ln in sec.splitlines() if ln.startswith("|")][2:]:
        c = split_cells(ln)
        m = re.fullmatch(_PT, c[0]) if c else None
        if not m or len(c) != 7 or m.groups() in t120:
            raise ReportError(f"{label}: malformed or duplicate explicit 1.20 V row: {ln.strip()}")
        row = {"status": c[1]}
        if c[1] in ("pass", "fail"):
            try:
                row.update(gain_db=float(c[2]), device=c[3], margin_mv=float(c[4]))
            except ValueError:
                raise ReportError(f"{label}: malformed explicit 1.20 V row: {ln.strip()}") from None
        elif c[1] != "invalid":
            raise ReportError(f"{label}: unknown explicit 1.20 V status '{c[1]}' at {c[0]}")
        t120[m.groups()] = row
    if set(t120) != set(points):
        raise ReportError(f"{label}: explicit 1.20 V table covers {len(t120)} points, the interval table "
                          f"{len(points)} (or a different set)")
    for pt in points:
        want = st120[pt] if st120[pt] != "n/a" else "invalid"
        if t120[pt]["status"] != want:
            raise ReportError(f"{label}: inconsistent 1.20 V status at {fmt_point(*pt)}: explicit table "
                              f"'{t120[pt]['status']}', interval table '{st120[pt]}'")
    cnt = {s: sum(1 for r in t120.values() if r["status"] == s) for s in ("pass", "fail", "invalid")}
    verdict = "fails" if cnt["fail"] else ("unknown" if cnt["invalid"] else "meets")
    v = re.search(r"^- \*\*1\.20 V explicit sample at all (\d+) combinations\*\*: \*\*(\w+)\*\* "
                  r"\((\d+) pass, (\d+) fail, (\d+) invalid/missing\)\.", hl, re.M)
    if not v or (int(v.group(1)), v.group(2).lower(), int(v.group(3)), int(v.group(4)), int(v.group(5))) != \
            (len(points), verdict, cnt["pass"], cnt["fail"], cnt["invalid"]):
        raise ReportError(f"{label}: headline 1.20 V sample line disagrees with its explicit 1.20 V table "
                          f"({verdict}: {cnt['pass']} pass, {cnt['fail']} fail, {cnt['invalid']} invalid/missing)")
    valid120 = {p: r for p, r in t120.items() if "margin_mv" in r}
    sm = _margin_claim(re.search(r"^- Smallest device margin at 1\.20 V: (\w+) ([+-][\d.]+) mV \(plateau gain ([\d.]+) dB\) "
                                 r"at " + _PT + r" / VCM 1\.200 V\.$", hl, re.M), label, "Smallest device margin at 1.20 V")
    wg = _margin_claim(re.search(r"^- Worst plateau gain at 1\.20 V: ([\d.]+) dB, limiting (\w+) ([+-][\d.]+) mV at "
                                 + _PT + r" / VCM 1\.200 V\.$", hl, re.M), label, "Worst plateau gain at 1.20 V")
    min_m = min(r["margin_mv"] for r in valid120.values())
    min_g = min(r["gain_db"] for r in valid120.values())
    sm_pt, wg_pt = sm.groups()[3:6], wg.groups()[3:6]
    if (abs(float(sm.group(2)) - min_m) > 0.051 or sm_pt not in valid120
            or abs(valid120[sm_pt]["margin_mv"] - min_m) > 0.051 or valid120[sm_pt]["device"] != sm.group(1)):
        raise ReportError(f"{label}: headline smallest 1.20 V margin ({sm.group(1)} {sm.group(2)} mV at "
                          f"{fmt_point(*sm_pt)}) disagrees with its explicit 1.20 V table (min {min_m:+.1f} mV)")
    if abs(float(wg.group(1)) - min_g) > 0.0051 or wg_pt not in valid120 or abs(valid120[wg_pt]["gain_db"] - min_g) > 0.0051:
        raise ReportError(f"{label}: headline worst 1.20 V plateau gain ({wg.group(1)} dB at {fmt_point(*wg_pt)}) "
                          f"disagrees with its explicit 1.20 V table (min {min_g:.2f} dB)")

    # ---- inside the common component: needs the retained samples (checked by icmr_crosscheck) ----
    in_comp = {}
    if comp is not None:
        g2 = _margin_claim(re.search(r"^- Worst plateau gain inside the 1\.20 V component[^:]*: ([\d.]+) dB, limiting (\w+) "
                                     r"([+-][\d.]+) mV at " + _PT + r" / VCM ([\d.]+) V\.$", hl, re.M),
                           label, "Worst plateau gain inside the 1.20 V component")
        m2 = _margin_claim(re.search(r"^- Smallest device margin inside that component: (\w+) ([+-][\d.]+) mV "
                                     r"\(plateau gain ([\d.]+) dB\) at " + _PT + r" / VCM ([\d.]+) V\.$", hl, re.M),
                           label, "Smallest device margin inside that component")
        in_comp = {"gain": {"gain_db": float(g2.group(1)), "device": g2.group(2), "margin_mv": float(g2.group(3)),
                            "point": g2.groups()[3:6], "vcm_mv": _mv(g2.group(7))},
                   "margin": {"device": m2.group(1), "margin_mv": float(m2.group(2)), "gain_db": float(m2.group(3)),
                              "point": m2.groups()[3:6], "vcm_mv": _mv(m2.group(7))}}
        for k_, c_ in in_comp.items():
            if not comp["lo"] <= c_["vcm_mv"] <= comp["hi"]:
                raise ReportError(f"{label}: headline worst {k_} inside the 1.20 V component sits at VCM "
                                  f"{_v(c_['vcm_mv'])} V, outside the component")

    brackets = [u for p in points for i in per_point[p] for u in (i["lo_unc"], i["hi_unc"]) if u is not None]
    res = int(ax.group(2))
    if brackets and max(brackets) > res:
        raise ReportError(f"{label}: a transition bracket of {max(brackets)} mV exceeds the record's stated "
                          f"refinement (<= {res} mV)")
    n_end = sum(2 * len(per_point[p]) for p in points)
    ends = re.search(r"^- Interval endpoints \(of (\d+)\)", hl, re.M)
    if ends and int(ends.group(1)) != n_end:
        raise ReportError(f"{label}: record counts {ends.group(1)} interval endpoints; its per-point table has {n_end}")
    flagged = re.search(r"^- Passing samples whose saturation margin is negative but inside the 1 mV tolerance: (\d+)\.",
                        hl, re.M)
    cov = coverage_of(set(points))
    lim = common_limitations(text, prov, cov)
    lim += [
        "follower-biased bench (unity-gain-follower DC operating point): the measured range is not an "
        "arbitrary-output-voltage input range and not an offset-accuracy claim",
        "systematic only: perfectly matched schematic, mismatch not covered",
        "sampled coverage: each edge is the nearest verified passing sample; the true transition lies within the "
        f"stated bracket on the non-passing side (initial scan <= {ax.group(1)} mV, transitions refined to <= {res} mV)",
        "the bench-validity tolerances (1 mV saturation tolerance, 0.10 V output tolerance) are not ratified bounds"
        + (f"; {flagged.group(1)} passing samples have a negative saturation margin inside the 1 mV tolerance"
           if flagged else ""),
    ]
    if n_inv:
        lim.append(f"{n_inv} of {n_samples} samples are invalid (never passing, never bridged); see the record's "
                   "validity-failure section")
    return {"prov": prov, "coverage": cov, "limitations": lim, "points": points, "per_point": per_point,
            "strict": strict, "st120": st120, "t120": t120, "intersection": inter, "intersection_strict": inter_s,
            "component": comp, "in_component": in_comp, "verdict_120": verdict, "count_120": cnt,
            "min_margin_120": {"device": sm.group(1), "margin_mv": float(sm.group(2)), "gain_db": float(sm.group(3)),
                               "point": sm_pt},
            "min_gain_120": {"gain_db": float(wg.group(1)), "device": wg.group(2), "margin_mv": float(wg.group(3)),
                             "point": wg_pt},
            "samples": {"total": n_samples, "pass": n_pass, "fail": n_fail, "invalid": n_inv},
            "scan_step_mv": int(ax.group(1)), "refine_step_mv": res, "max_bracket_mv": max(brackets) if brackets else None,
            "n_endpoints": n_end,
            "tolerance_flagged": int(flagged.group(1)) if flagged else None}


def _icmr_runs(samples: list, max_gap_mv: int, key: str) -> list:
    """Contiguous passing runs of one point's samples (sorted by VCM), as the driver forms them."""
    out, i = [], 0
    while i < len(samples):
        if samples[i][key] != "pass":
            i += 1
            continue
        j = i
        while j + 1 < len(samples) and samples[j + 1][key] == "pass" and samples[j + 1]["vcm"] - samples[j]["vcm"] <= max_gap_mv:
            j += 1
        lo_nb = samples[i - 1] if i > 0 else None
        hi_nb = samples[j + 1] if j + 1 < len(samples) else None
        out.append({"lo": samples[i]["vcm"], "hi": samples[j]["vcm"], "n": j - i + 1,
                    "lo_unc": None if lo_nb is None else samples[i]["vcm"] - lo_nb["vcm"],
                    "hi_unc": None if hi_nb is None else hi_nb["vcm"] - samples[j]["vcm"],
                    "lo_nb": "scan edge" if lo_nb is None else lo_nb[key],
                    "hi_nb": "scan edge" if hi_nb is None else hi_nb[key]})
        i = j + 1
    return out


def icmr_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive the record's per-point intervals, 1.20 V statuses and margins from its
    retained per-sample evidence (corners/<rid>/samples.csv). Offline; no simulator."""
    import csv
    rel = f"sim/{ICMR_EXP}/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-point evidence is missing: {rel} (the record's intervals cannot "
                          "be cross-checked)")
    need = ("process", "temp_c", "vdd_v", "vcm_v", "status", "status_strict", "gain_db", "min_margin_mv",
            "limiting_device")
    by_pt: dict = {}
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns {list(need)}")
        seen = set()
        for r in rd:
            try:
                pt = (r["process"], str(int(float(r["temp_c"]))), f"{float(r['vdd_v']):.2f}")
                vcm = _mv(r["vcm_v"])
                s = {"vcm": vcm, "status": r["status"], "status_strict": r["status_strict"],
                     "gain_db": float(r["gain_db"]) if r["gain_db"] else None,
                     "margin_mv": float(r["min_margin_mv"]) if r["min_margin_mv"] else None,
                     "device": r["limiting_device"].upper(), "tol_flag": bool((r.get("tolerance_flags") or "").strip())}
            except (ValueError, KeyError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if (pt, vcm) in seen or s["status"] not in ("pass", "fail", "invalid"):
                raise ReportError(f"{label}: {rel}: duplicate sample or unknown status at {fmt_point(*pt)} / VCM {_v(vcm)} V")
            if s["status"] != "invalid" and (s["gain_db"] is None or s["margin_mv"] is None):
                raise ReportError(f"{label}: {rel}: valid sample without gain/margin at {fmt_point(*pt)} / VCM {_v(vcm)} V")
            seen.add((pt, vcm))
            by_pt.setdefault(pt, []).append(s)
    if set(by_pt) != set(ex["points"]):
        raise ReportError(f"{label}: {rel} covers {len(by_pt)} PVT points; the record's per-point table "
                          f"{len(ex['points'])} (or a different set)")
    total = sum(len(v) for v in by_pt.values())
    counts = {k: sum(1 for v in by_pt.values() for s in v if s["status"] == k) for k in ("pass", "fail", "invalid")}
    if {"total": total, **counts} != ex["samples"]:
        raise ReportError(f"{label}: {rel} holds {total} samples ({counts}); the record header states {ex['samples']}")
    n_flag = sum(1 for v in by_pt.values() for s in v if s["status"] == "pass" and s["tol_flag"])
    if ex["tolerance_flagged"] is not None and n_flag != ex["tolerance_flagged"]:
        raise ReportError(f"{label}: {rel} has {n_flag} passing samples inside the saturation tolerance band; "
                          f"the record states {ex['tolerance_flagged']}")
    for pt, ss in by_pt.items():
        ss.sort(key=lambda s: s["vcm"])
        got = _icmr_runs(ss, ex["scan_step_mv"], "status")
        if got != ex["per_point"][pt]:
            raise ReportError(f"{label}: per-point intervals at {fmt_point(*pt)} disagree with the retained evidence "
                              f"{rel} ({fmt_ivs(got)} with its brackets/neighbours, record {fmt_ivs(ex['per_point'][pt])})")
        got_s = [(i["lo"], i["hi"]) for i in _icmr_runs(ss, ex["scan_step_mv"], "status_strict")]
        if got_s != ex["strict"][pt]:
            raise ReportError(f"{label}: strict (0 mV) intervals at {fmt_point(*pt)} disagree with the retained evidence {rel}")
        s120 = next((s for s in ss if s["vcm"] == ICMR_TARGET_MV), None)
        t = ex["t120"][pt]
        if (s120 or {"status": "invalid"})["status"] != t["status"] or (
                "margin_mv" in t and (abs(s120["margin_mv"] - t["margin_mv"]) > 0.051
                                      or abs(s120["gain_db"] - t["gain_db"]) > 0.0051 or s120["device"] != t["device"])):
            raise ReportError(f"{label}: explicit 1.20 V sample at {fmt_point(*pt)} disagrees with the retained evidence {rel}")
    comp = ex["component"]
    if comp is not None:
        inside = [(pt, s) for pt, ss in by_pt.items() for s in ss
                  if comp["lo"] <= s["vcm"] <= comp["hi"] and s["status"] != "invalid"]
        for k_, field in (("margin", "margin_mv"), ("gain", "gain_db")):
            c_ = ex["in_component"][k_]
            lo_val = min(s[field] for _, s in inside)
            at = next((s for pt, s in inside if pt == c_["point"] and s["vcm"] == c_["vcm_mv"]), None)
            tol_ = 0.051 if field == "margin_mv" else 0.0051
            if (at is None or abs(c_[field] - lo_val) > tol_ or abs(at[field] - lo_val) > tol_
                    or at["device"] != c_["device"]):
                raise ReportError(f"{label}: headline worst {k_} inside the 1.20 V component disagrees with the "
                                  f"retained evidence {rel} (min {lo_val:+.2f})")
    return {"path": rel, "sha256": sha256_file(p), "samples": total, "points": len(by_pt)}


def _icmr_addendum(root: Path, rid: str) -> dict | None:
    rel = f"sim/{ICMR_EXP}/records/{rid}-addendum/ADDENDUM.md"
    p = root / rel
    return {"path": rel, "sha256": sha256_file(p)} if p.is_file() else None


_INFO = " (information only, not graded)"


def _edge(c: dict, side: str) -> str:
    unc = c[f"{side}_unc"]
    return (f"{_v(c[side])} V, bracket {'scan edge' if unc is None else f'{unc} mV'} to a "
            f"{c[f'{side}_nb']} sample")


def icmr_figures(ex: dict) -> list:
    """The ICMR row's measured figures, each labelled information only (the bound is proposed)."""
    F = []

    def add(label, value, corner=None):
        F.append({"label": label + _INFO, "value": value, "corner": corner})

    n = ex["coverage"]["points"]
    add(f"conservative common interval over all {n} PVT points (intersection; disjoint components listed "
        "separately, never bridged)", fmt_ivs(ex["intersection"]))
    comp = ex["component"]
    if comp is None:
        add("component of the common interval containing 1.20 V", "none: 1.20 V is outside every common component")
    else:
        add("component of the common interval containing 1.20 V", fmt_ivs([comp]))
        add("low edge, edge-binding corner", _edge(comp, "lo"), comp["lo_src"])
        add("high edge, edge-binding corner", _edge(comp, "hi"), comp["hi_src"])
    add("transition resolution (largest edge bracket over every per-point interval endpoint)",
        (f"{ex['max_bracket_mv']} mV over {ex['n_endpoints']} endpoints" if ex["max_bracket_mv"] is not None
         else f"no bracketed endpoint ({ex['n_endpoints']} endpoints)")
        + f" (scan <= {ex['scan_step_mv']} mV, refinement <= {ex['refine_step_mv']} mV)")
    c = ex["count_120"]
    add("explicit 1.20 V sample coverage", f"{c['pass']}/{n} pass, {c['fail']} fail, {c['invalid']} invalid/missing "
        f"(record verdict: {ex['verdict_120']})")
    m = ex["min_margin_120"]
    add("smallest saturation margin at 1.20 V", f"{m['device']} {m['margin_mv']:+.1f} mV (plateau gain "
        f"{m['gain_db']:.2f} dB)", fmt_point(*m["point"]))
    g = ex["min_gain_120"]
    add("lowest plateau gain at 1.20 V", f"{g['gain_db']:.2f} dB (limiting {g['device']} {g['margin_mv']:+.1f} mV)",
        fmt_point(*g["point"]))
    if comp is not None:
        m = ex["in_component"]["margin"]
        add("smallest saturation margin inside the 1.20 V component", f"{m['device']} {m['margin_mv']:+.1f} mV at "
            f"VCM {_v(m['vcm_mv'])} V (plateau gain {m['gain_db']:.2f} dB)", fmt_point(*m["point"]))
        g = ex["in_component"]["gain"]
        add("lowest plateau gain inside the 1.20 V component", f"{g['gain_db']:.2f} dB at VCM {_v(g['vcm_mv'])} V "
            f"(limiting {g['device']} {g['margin_mv']:+.1f} mV)", fmt_point(*g["point"]))
    add("common interval with a strict 0 mV saturation tolerance", fmt_ivs(ex["intersection_strict"]))
    dis = [p for p in ex["points"] if len(ex["per_point"][p]) > 1]
    add("PVT points with disjoint passing intervals", f"{len(dis)} of {n}" + (": " + "; ".join(
        f"{fmt_point(*p)} {fmt_ivs(ex['per_point'][p])}" for p in dis) if dis else ""))
    s = ex["samples"]
    add("(PVT point, VCM) samples", f"{s['total']} ({s['pass']} pass, {s['fail']} fail, {s['invalid']} invalid); "
        f"re-derived from the retained {ex['retained']['path']}")
    return F


def icmr_evidence(ex: dict, source: dict) -> dict:
    """Structured, source-pinned ICMR measurement attached to the (ungraded) proposed row."""
    def comp_d(c):
        return {"low_mv": c["lo"], "high_mv": c["hi"], "low_binding_point": c["lo_src"],
                "high_binding_point": c["hi_src"], "low_bracket_mv": c["lo_unc"], "high_bracket_mv": c["hi_unc"],
                "low_neighbour": c["lo_nb"], "high_neighbour": c["hi_nb"]}

    def pt_d(d):
        return {k: (fmt_point(*v) if k == "point" else v) for k, v in d.items()}

    prov_keys = ("dut_sha256", "pdk_open_pdks", "pdk_client_resolved", "ngspice_local", "ngspice_engine",
                 "klt_client", "backend", "fleet_runner_mismatch", "measurement_config")
    return {
        "graded": False,
        "common_interval_components": [comp_d(c) for c in ex["intersection"]],
        "component_containing_1v20": comp_d(ex["component"]) if ex["component"] else None,
        "common_interval_strict_mv": [[c["lo"], c["hi"]] for c in ex["intersection_strict"]],
        "transition_resolution": {"scan_step_mv": ex["scan_step_mv"], "refine_step_mv": ex["refine_step_mv"],
                                  "max_endpoint_bracket_mv": ex["max_bracket_mv"], "endpoints": ex["n_endpoints"]},
        "explicit_1v20": {"verdict_in_record": ex["verdict_120"], "points": ex["coverage"]["points"], **ex["count_120"]},
        "smallest_saturation_margin": {"at_1v20": pt_d(ex["min_margin_120"]),
                                       "inside_component": pt_d(ex["in_component"]["margin"]) if ex["in_component"] else None},
        "lowest_plateau_gain": {"at_1v20": pt_d(ex["min_gain_120"]),
                                "inside_component": pt_d(ex["in_component"]["gain"]) if ex["in_component"] else None},
        "per_point_intervals": [{"point": fmt_point(*p), "intervals_mv": [[i["lo"], i["hi"]] for i in ex["per_point"][p]],
                                 "status_1v20": ex["st120"][p]} for p in ex["points"]],
        "samples": ex["samples"],
        "tolerance_flagged_samples": ex["tolerance_flagged"],
        "provenance": {"record_id": source["record_id"], "record_sha256": source["sha256"],
                       **{k: source[k] for k in prov_keys}},
        "retained_evidence": ex["retained"],
        "addendum": ex["addendum"],
    }
