"""Split out of characterization_report.py (issue #219); moved verbatim, no logic changes."""
from __future__ import annotations

import math
import re
from pathlib import Path
from report_common import (
    ReportError, common_limitations, corner_sort_key, coverage_of, fmt_point, grid_points, header_of,
    parse_fingerprint, parse_provenance, section, sha256_file, split_cells)
from extract_icmr import (
    _PT)


OFFSET_GRID_TITLE = "# Offset Monte Carlo PVT grid record"
OFFSET_NOMINAL_TITLE = "# Offset Monte Carlo record"
#: Ratified statistical basis of the offset row (spec Sec. 2, [DR-2]): mismatch MC N >= 300.
OFFSET_MIN_N = 300
#: Display precision of the grid record: mV values at 3 decimals, skew / excess kurtosis at 2.
_MV_TOL = 0.0005 + 1e-6
_MOM_TOL = 0.005 + 1e-6


def extract_offset(text: str, label: str) -> dict:
    """Offset Monte Carlo record of either committed format.

    The issue #45 nominal record (five MOS corners at 27 C / 3.30 V) and the
    issue #106 full PVT grid record (`run_offset_mc.py --grid full`) differ in
    population and layout; the title line decides which parser reads it, and a
    record that matches neither is rejected rather than guessed at.
    """
    head = text.lstrip()
    if head.startswith(OFFSET_GRID_TITLE):
        return extract_offset_grid(text, label)
    if head.startswith(OFFSET_NOMINAL_TITLE):
        return extract_offset_nominal(text, label)
    raise ReportError(f"{label}: unknown offset record format (title is neither '{OFFSET_NOMINAL_TITLE}' "
                      f"nor '{OFFSET_GRID_TITLE}')")


def _okey(proc: str, t, v) -> tuple:
    """Typed offset grid point key: (process, T in C as '%g', VDD in V as '%.2f')."""
    return (str(proc), f"{float(t):g}", f"{float(v):.2f}")


def offset_stats(values: list) -> dict:
    """Per-point offset statistics in mV, the driver's definitions
    (`sim/offset-mc/run_offset_mc.py` stats_of: sample sigma (n-1), population
    skew / excess kurtosis); a parity test pins the two together."""
    import math
    import statistics
    n = len(values)
    if n < 2 or not all(math.isfinite(x) for x in values):
        raise ValueError("need at least two finite samples")
    mean = statistics.fmean(values)
    sigma = statistics.stdev(values)
    m2 = sum((x - mean) ** 2 for x in values) / n
    skew = sum((x - mean) ** 3 for x in values) / n / m2 ** 1.5 if m2 > 0 else 0.0
    kurt = sum((x - mean) ** 4 for x in values) / n / m2 ** 2 - 3.0 if m2 > 0 else 0.0
    return {"n": n, "mean": mean * 1e3, "sigma": sigma * 1e3, "three_sigma": 3 * sigma * 1e3,
            "abs3": (abs(mean) + 3 * sigma) * 1e3, "min": min(values) * 1e3, "max": max(values) * 1e3,
            "skew": skew, "ex_kurt": kurt}


_OG_COLS = ("mean", "sigma", "three_sigma", "abs3", "min", "max", "skew", "ex_kurt")


def _offset_worst(stats: dict, order: list) -> dict:
    """Worst point by sigma and by |mean| + 3 sigma (ties: first in grid order, as the driver)."""
    return {"sigma": max(order, key=lambda k: stats[k]["sigma"]),
            "abs3": max(order, key=lambda k: stats[k]["abs3"])}


def extract_offset_grid(text: str, label: str) -> dict:
    """Full PVT grid offset record (issue #106): every (process, T, VDD) point's statistics,
    the worst sigma / worst |mean| + 3 sigma points and the T x VDD worst-corner table,
    each re-derived from the record's own per-point table. The point set and N are those of
    the record's retained measurement-fingerprint inputs (hash-verified), so a missing,
    duplicate or unexpected point, or a point with too few samples, is rejected.

    The retained per-sample evidence (offset_samples.csv) is cross-checked separately
    (`offset_grid_crosscheck`), since it lives beside the record rather than in it.
    """
    prov = parse_provenance(text, label)
    fp = parse_fingerprint(text, label)
    if fp is None:
        raise ReportError(f"{label}: offset grid record carries no measurement fingerprint, so its PVT point set "
                          "and sample count cannot be established")
    inp = fp["inputs"]
    try:
        if inp.get("grid") != "full":
            raise KeyError("grid")
        ax = inp["corners"]
        procs, temps, sups, vcms = ax["process"], ax["temperature_c"], ax["supply_v"], ax["vcm_v"]
        n_want = int(inp["monte_carlo"]["n"])
    except (KeyError, TypeError, ValueError):
        raise ReportError(f"{label}: retained measurement-fingerprint inputs do not describe a full offset grid "
                          "(need \"grid\": \"full\", corners.process/temperature_c/supply_v/vcm_v and monte_carlo.n)") from None
    if len(vcms) != len(sups):
        raise ReportError(f"{label}: retained inputs pair {len(vcms)} VCM values with {len(sups)} supplies")
    order = [_okey(p, t, v) for p in procs for t in temps for v in sups]  # driver's grid order
    if len(set(order)) != len(order):
        raise ReportError(f"{label}: retained inputs repeat a grid axis value")
    if n_want < OFFSET_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the offset row's ratified statistical basis needs N >= {OFFSET_MIN_N}")
    req = re.search(r"^- \*\*Request\*\*:.*?= (\d+) points, `monte_carlo = \{n: (\d+),", header_of(text), re.M)
    if not req or (int(req.group(1)), int(req.group(2))) != (len(order), n_want):
        raise ReportError(f"{label}: request line missing or disagrees with the retained inputs "
                          f"({len(order)} points, N={n_want} per point)")

    # ---- per-point statistics table ----
    sec = section(text, "Offset statistics per grid point")
    lines = [ln for ln in sec.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no per-point table under '## Offset statistics per grid point'")
    stats: dict = {}
    for ln in lines[2:]:
        c = split_cells(ln.replace("\\|", "/"))
        if len(c) != 12:
            raise ReportError(f"{label}: malformed per-point offset row: {ln.strip()}")
        try:
            k = _okey(c[0], c[1], c[2])
            n = int(c[3])
        except ValueError:
            raise ReportError(f"{label}: malformed per-point offset row: {ln.strip()}") from None
        if k in stats:
            raise ReportError(f"{label}: duplicate grid point {fmt_point(*k)} in the per-point table")
        if k not in order:
            raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} (not in the retained grid axes)")
        if n != n_want:
            raise ReportError(f"{label}: insufficient samples at {fmt_point(*k)}: N={n}, the request is N={n_want} "
                              "per point")
        try:
            stats[k] = {"n": n, **{col: float(v.replace("+", "")) for col, v in zip(_OG_COLS, c[4:])}}
        except ValueError:
            raise ReportError(f"{label}: malformed per-point offset row: {ln.strip()}") from None
    missing = [k for k in order if k not in stats]
    if missing:
        raise ReportError(f"{label}: per-point table is missing {len(missing)} of {len(order)} grid points "
                          f"(first: {fmt_point(*missing[0])})")
    for k, s in stats.items():
        if (abs(s["three_sigma"] - 3 * s["sigma"]) > 3 * _MV_TOL + _MV_TOL
                or abs(s["abs3"] - (abs(s["mean"]) + s["three_sigma"])) > 2 * _MV_TOL + _MV_TOL
                or not s["min"] <= s["mean"] <= s["max"] or s["sigma"] <= 0):
            raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)}: its 3 sigma, |mean| + 3 sigma, "
                              "min/max or sigma columns disagree with each other")

    # ---- headline worst points ----
    hl = section(text, "Worst point")
    we = re.search(r"^- \*\*Worst linear 3-sigma offset over the (\d+)-point grid\*\*: \*\*([\d.]+) mV\*\* at "
                   r"`" + _PT + r"` \(mean ([+-][\d.]+), sigma ([\d.]+), 3 sigma ([\d.]+)\)$", hl, re.M)
    ws = re.search(r"^- \*\*Worst sigma over the grid\*\*: ([\d.]+) mV \(3 sigma ([\d.]+)\) at `" + _PT + r"`$", hl, re.M)
    if not (we and ws):
        raise ReportError(f"{label}: no worst-point headline lines (worst linear 3-sigma offset / worst sigma)")
    w = _offset_worst(stats, order)
    ka, ks = _okey(*we.groups()[2:5]), _okey(*ws.groups()[2:5])
    sa, ss_ = stats[w["abs3"]], stats[w["sigma"]]
    if (int(we.group(1)) != len(order) or ka != w["abs3"]
            or any(abs(float(x) - sa[col]) > _MV_TOL for x, col in
                   ((we.group(2), "abs3"), (we.group(6), "mean"), (we.group(7), "sigma"), (we.group(8), "three_sigma")))):
        raise ReportError(f"{label}: headline worst |mean| + 3 sigma ({we.group(2)} mV at {fmt_point(*ka)}) disagrees "
                          f"with its own per-point table ({sa['abs3']:.3f} mV at {fmt_point(*w['abs3'])})")
    if (ks != w["sigma"] or abs(float(ws.group(1)) - ss_["sigma"]) > _MV_TOL
            or abs(float(ws.group(2)) - ss_["three_sigma"]) > _MV_TOL):
        raise ReportError(f"{label}: headline worst sigma ({ws.group(1)} mV at {fmt_point(*ks)}) disagrees with its "
                          f"own per-point table ({ss_['sigma']:.3f} mV at {fmt_point(*w['sigma'])})")

    # ---- T x VDD worst-corner table ----
    tv = section(text, "Linear 3-sigma offset by temperature and supply")
    tv_rows = [split_cells(ln) for ln in tv.splitlines() if ln.startswith("|")][2:]
    by_tv = {}
    for c in tv_rows:
        m = re.fullmatch(r"(-?\d+) C", c[0]) if c else None
        if not m or len(c) != len(sups) + 1:
            raise ReportError(f"{label}: malformed temperature x supply row: {' | '.join(c)}")
        for v, cell in zip(sups, c[1:]):
            mc_ = re.fullmatch(r"([\d.]+) \((\w+)\)", cell)
            if not mc_:
                raise ReportError(f"{label}: malformed temperature x supply cell '{cell}'")
            by_tv[(f"{float(m.group(1)):g}", f"{float(v):.2f}")] = (float(mc_.group(1)), mc_.group(2))
    worst_tv = []
    for t in temps:
        for v in sups:
            tk = (f"{float(t):g}", f"{float(v):.2f}")
            pts = [k for k in order if k[1:] == tk]
            kk = max(pts, key=lambda k: stats[k]["abs3"])
            got = by_tv.get(tk)
            if got is None or got[1] != kk[0] or abs(got[0] - stats[kk]["abs3"]) > _MV_TOL:
                raise ReportError(f"{label}: temperature x supply table at {tk[0]} C / {tk[1]} V "
                                  f"({'missing' if got is None else f'{got[0]:.3f} ({got[1]})'}) disagrees with its own "
                                  f"per-point table ({stats[kk]['abs3']:.3f} ({kk[0]}))")
            worst_tv.append(kk)
    if len(by_tv) != len(temps) * len(sups):
        raise ReportError(f"{label}: temperature x supply table has {len(by_tv)} cells, the grid "
                          f"{len(temps) * len(sups)}")

    n_total = len(order) * n_want
    valid = re.search(r"^Result: all (\d+) samples valid;", section(text, "Extraction validation"), re.M)
    if not valid or int(valid.group(1)) != n_total:
        raise ReportError(f"{label}: extraction validation does not state all {n_total} samples valid "
                          "(the record lists problems, or its count disagrees with the grid)")
    pts = set(order)
    cov = coverage_of(pts)
    cov["mc_samples_per_point"] = n_want
    se = re.search(r"sigma relative standard error ~ ([\d.]+) %", hl)
    lim = common_limitations(text, prov, cov)
    lim.append("mismatch Monte Carlo of the offset only; no numeric bound proposed or judged "
               "(DR-3 residual e2; the bound is spec issue #62's decision)")
    if se:
        lim.append(f"sigma relative standard error ~ {se.group(1)} % at the stated N per point; the worst of "
                   f"{len(order)} points is a maximum over noisy estimates, so it is biased high by a few standard errors")
    return {"prov": prov, "coverage": cov, "grid": True, "order": order, "stats": stats, "n_per_point": n_want,
            "vcm_of": {f"{float(v):.2f}": float(vc) for v, vc in zip(sups, vcms)},
            "worst": w, "worst_tv": worst_tv, "n_total": n_total, "limitations": lim}


def offset_grid_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every point's displayed statistics, and the worst points, from the record's
    retained per-sample evidence (corners/<rid>/offset_samples.csv), within the record's display
    precision. Offline; no simulator."""
    import csv
    import math
    rel = f"sim/offset-mc/corners/{rid}/offset_samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    need = ("corner", "temperature_c", "vdd_v", "vcm_v", "sample_index", "offset_v")
    by: dict = {}
    seen = set()
    seeds: dict = {}  # (process, VDD, sample index) -> {T: mismatch_seed}, for the drift view's independence note
    seeds_known = True
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns {list(need)}")
        seeds_known = "mismatch_seed" in rd.fieldnames
        for r in rd:
            try:
                k = _okey(r["corner"], r["temperature_c"], r["vdd_v"])
                idx, vcm, off = int(r["sample_index"]), float(r["vcm_v"]), float(r["offset_v"])
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["stats"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if seeds_known:
                seeds.setdefault((k[0], k[2], idx), {})[k[1]] = r["mismatch_seed"]
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if not math.isfinite(off) or abs(vcm - ex["vcm_of"][k[2]]) > 1e-6:
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)} (non-finite offset or VCM "
                                  f"{vcm:g} V, not the commanded {ex['vcm_of'][k[2]]:g} V)")
            seen.add((k, idx))
            by.setdefault(k, []).append(off)
    stats = {}
    for k in ex["order"]:
        vals = by.get(k, [])
        if len(vals) != ex["n_per_point"]:
            raise ReportError(f"{label}: insufficient samples in {rel} at {fmt_point(*k)}: {len(vals)}, the record "
                              f"states N={ex['n_per_point']}")
        s = stats[k] = offset_stats(vals)
        rec = ex["stats"][k]
        bad = [col for col in _OG_COLS
               if abs(rec[col] - s[col]) > (_MOM_TOL if col in ("skew", "ex_kurt") else _MV_TOL)]
        if bad:
            raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)}: the record's {', '.join(bad)} "
                              f"disagree(s) with the retained evidence {rel} (e.g. {bad[0]} "
                              f"{rec[bad[0]]:.3f} vs {s[bad[0]]:.3f})")
    w = _offset_worst(stats, ex["order"])
    if w != ex["worst"]:
        raise ReportError(f"{label}: the record's worst points ({fmt_point(*ex['worst']['abs3'])} by |mean| + 3 sigma, "
                          f"{fmt_point(*ex['worst']['sigma'])} by sigma) disagree with the retained evidence {rel}")
    # derived-view inputs (issue #184): the unrounded per-point statistics re-derived above, and whether any
    # sample index reuses its mismatch seed across temperatures (then the per-temperature means are paired)
    ex["sample_stats"] = stats
    ex["seed_independence"] = ("unknown" if not seeds_known else
                               "shared" if any(len(set(d.values())) < len(d) for d in seeds.values())
                               else "independent")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(stats)}


# --------------------------------------------------------------------------
# derived offset temperature drift and systematic-vs-statistical split (issue #184)
# --------------------------------------------------------------------------
#: A drift is called resolved only if |drift| exceeds this many of its own standard errors.
DRIFT_RESOLVE_K = 2.0


def _fin(x: float) -> float:
    """JSON-safe, byte-stable number: finite, rounded, no negative zero."""
    import math
    if not math.isfinite(x):
        raise ReportError("derived offset drift: non-finite value")
    r = round(x, 6)
    return 0.0 if r == 0 else r


def _slope(ts: list, ms: list, ses: list) -> tuple:
    """Least-squares slope (uV/C) of mean offset vs T and its standard error propagated from the
    per-mean standard errors (independent points): slope = sum(w_i m_i), w_i = (T_i - Tbar) / Sxx,
    variance = sum(w_i^2 se_i^2)."""
    import math
    tb = sum(ts) / len(ts)
    sxx = sum((t - tb) ** 2 for t in ts)
    w = [(t - tb) / sxx for t in ts]
    return (sum(wi * m for wi, m in zip(w, ms)), math.sqrt(sum((wi * se) ** 2 for wi, se in zip(w, ses))))


def _drift_entry(d: float, se: float) -> dict:
    return {"uv_per_c": _fin(d), "se_uv_per_c": _fin(se), "resolved": bool(abs(d) > DRIFT_RESOLVE_K * se)}


def offset_drift_view(ex: dict, record: dict) -> dict:
    """Offset temperature drift and systematic / statistical split, derived offline from the
    per-sample statistics already cross-checked against the record (no simulation, no verdict, no
    bound). `record` is the selected record's source entry (path, sha256)."""
    import math
    if not ex.get("grid") or "sample_stats" not in ex:
        return {"available": False,
                "reason": "the selected offset record is not a full PVT grid (single temperature): "
                          "no temperature drift can be derived"}
    st, n = ex["sample_stats"], ex["n_per_point"]
    temps = sorted({float(k[1]) for k in ex["order"]})
    procs = []
    for k in ex["order"]:
        if k[0] not in procs:
            procs.append(k[0])
    keys = sorted({(k[0], k[2]) for k in ex["order"]}, key=lambda q: (procs.index(q[0]), float(q[1])))
    groups = []
    for proc, vdd in keys:
        raw = []
        for t in temps:
            s = st[(proc, f"{t:g}", vdd)]
            mean, sig = s["mean"] * 1e3, s["sigma"] * 1e3  # mV -> uV
            raw.append((t, mean, sig, sig / math.sqrt(s["n"]), s["n"]))
        pts = []
        for t, mean, sig, se, nn in raw:
            a3 = abs(mean) + 3 * sig
            pts.append({"temp_c": _fin(t), "n": nn, "mean_uv": _fin(mean), "sigma_uv": _fin(sig),
                        "se_mean_uv": _fin(se), "abs_mean_uv": _fin(abs(mean)), "three_sigma_uv": _fin(3 * sig),
                        "abs3_uv": _fin(a3), "systematic_fraction": _fin(abs(mean) / a3) if a3 > 0 else None})
        g = {"process": proc, "vdd_v": vdd, "points": pts}
        if len(raw) >= 2:
            ts, ms, ses = [r[0] for r in raw], [r[1] for r in raw], [r[3] for r in raw]
            span = ts[-1] - ts[0]
            g["drift_end_to_end"] = {"from_c": _fin(ts[0]), "to_c": _fin(ts[-1]),
                                     **_drift_entry((ms[-1] - ms[0]) / span, math.hypot(ses[0], ses[-1]) / span)}
            g["drift_fit"] = {"points": len(ts), **_drift_entry(*_slope(ts, ms, ses))}
            g["segments"] = [{"from_c": _fin(ts[i]), "to_c": _fin(ts[i + 1]),
                              **_drift_entry((ms[i + 1] - ms[i]) / (ts[i + 1] - ts[i]),
                                             math.hypot(ses[i], ses[i + 1]) / (ts[i + 1] - ts[i]))}
                             for i in range(len(ts) - 1)]
        else:
            g["drift_end_to_end"] = g["drift_fit"] = None
            g["segments"] = []
        sg = [p_["sigma_uv"] for p_ in pts]
        g["sigma_vs_t"] = {"min_uv": min(sg), "max_uv": max(sg),
                           "max_over_min": _fin(max(sg) / min(sg)) if min(sg) > 0 else None}
        g["worst_point"] = dict(max(pts, key=lambda p_: p_["abs3_uv"]))
        groups.append(g)
    allw = max(groups, key=lambda g: g["worst_point"]["abs3_uv"])
    lim = [f"{len(temps)} temperatures ({', '.join(f'{t:g}' for t in temps)} C) only: the drift is a "
           f"{'two-segment' if len(temps) == 3 else 'piecewise'} description of the measured points, not a fitted "
           "law; nothing is extrapolated below the lowest or above the highest temperature, and curvature inside "
           "a segment is not resolved",
           f"each mean has standard error sigma/sqrt(N) with N={n}; a drift is called resolved only when "
           f"|drift| > {DRIFT_RESOLVE_K:g} of its own standard errors (points treated as independent), "
           "otherwise it is unresolved and its value is a noisy estimate, not a measurement",
           f"{len(groups)} corner/supply groups are examined at the {DRIFT_RESOLVE_K:g}-standard-error threshold, so a "
           "few chance 'resolved' flags are expected even for zero true drift; one flag is not by itself a trend",
           "the systematic part |mean| of a point still contains its own sampling error (standard error "
           "sigma/sqrt(N)); it is not a pure process-systematic offset",
           "evidence only: no verdict, no bound proposed; the offset bound is the spec issue #62 decision"]
    if ex.get("seed_independence") != "independent":
        lim.append(f"mismatch-seed independence across temperatures is {ex.get('seed_independence')}: if samples are "
                   "paired across temperatures the drift standard errors are conservative (too large)")
    return {"available": True, "record": record["path"], "record_sha256": record["sha256"],
            "input": ex["retained"]["path"], "input_sha256": ex["retained"]["sha256"],
            "units": "uV for offsets, uV/C for drifts", "temps_c": [_fin(t) for t in temps], "n_per_point": n,
            "resolution_threshold_se": DRIFT_RESOLVE_K, "seed_independence": ex.get("seed_independence"),
            "groups": groups,
            "worst_overall": {"process": allw["process"], "vdd_v": allw["vdd_v"], **allw["worst_point"]},
            "limitations": lim}


def extract_offset_nominal(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    sec = section(text, "Offset statistics per corner")
    rows = []
    for m in re.finditer(r"^\| (\w+) \| (\d+) \| ([+-][\d.]+) \| ([\d.]+) \| ([\d.]+) \|", sec, re.M):
        rows.append({"corner": m.group(1), "n": int(m.group(2)), "mean_mv": m.group(3),
                     "sigma_mv": m.group(4), "three_sigma_mv": m.group(5)})
    ws = re.search(r"\*\*Worst corner by sigma\*\*: `(\w+)` \(([\d.]+) mV; 3 sigma ([\d.]+) mV\)", sec)
    wm = re.search(r"\*\*Worst corner by \|mean\| \+ 3 sigma\*\*: `(\w+)` \(([\d.]+) mV\)", sec)
    cond = re.search(r"at (-?\d+) C, VDD ([\d.]+) V", header_of(text))
    if not (rows and ws and wm and cond):
        raise ReportError(f"{label}: offset statistics table / worst-corner lines / conditions not found")
    ns = {r["n"] for r in rows}
    cov = {"points": len(rows), "corners": sorted([r["corner"] for r in rows], key=corner_sort_key),
           "temps_c": [int(cond.group(1))], "vdd_v": [cond.group(2)],
           "mc_samples_per_corner": min(ns) if len(ns) == 1 else sorted(ns)}
    se = re.search(r"sigma relative standard error ~ ([\d.]+) %", sec)
    lim = common_limitations(text, prov, cov)
    lim.append(f"nominal T/VDD only ({cond.group(1)} C, {cond.group(2)} V): no temperature or supply axis")
    lim.append("mismatch Monte Carlo of the offset only; no numeric bound proposed (DR-3 residual e2)")
    if se:
        lim.append(f"sigma relative standard error ~ {se.group(1)} % at the stated N")
    return {"prov": prov, "coverage": cov, "stats": rows,
            "worst_sigma": ws.groups(), "worst_abs": wm.groups(), "limitations": lim}


def _worst_table(text: str, heading: str, label: str) -> list:
    sec = section(text, heading)
    out = []
    rx = re.compile(r"^\| ([^|]+?) \| \*\*(?:>= )?([\d.]+)\*\* \| ([^|]+?) \| ([\d.]+) \| (\d+) \|$", re.M)
    for m in rx.finditer(sec):
        out.append({"figure": m.group(1), "worst_db": m.group(2), "worst_corner": m.group(3),
                    "best_db": m.group(4), "missing_points": int(m.group(5))})
    if not out:
        raise ReportError(f"{label}: no worst-case table under '## {heading}'")
    return out


def extract_cmrr(text: str, label: str) -> dict:
    prov = parse_provenance(text, label)
    cov = coverage_of(grid_points(text, "slash"))
    lim = common_limitations(text, prov, cov)
    if "**Systematic only.**" in text:
        lim.append("systematic figure of a perfectly matched schematic; mismatch-limited CMRR (Monte Carlo) is not covered by this record")
    lim.append("no ratified bound (DR-3 residual e3)")
    return {"prov": prov, "coverage": cov, "figures": {"CMRR": _worst_table(text, "Worst case over", label)},
            "limitations": lim}


# --------------------------------------------------------------------------
# CMRR mismatch Monte Carlo (issue #124): measured, never graded while proposed
# --------------------------------------------------------------------------
CMRR_MC_EXP = "cmrr-mc"
CMRR_MC_MIN_N = 300
#: (record heading, key, samples.csv column suffix), in the record's order
CMRR_MC_FIGS = (("DC plateau (0.1-1 Hz)", "dc", "dc"), ("1 kHz", "1k", "1k"), ("10 kHz", "10k", "10k"),
                ("100 kHz", "100k", "100k"), ("1 MHz", "1m", "1m"),
                ("at f_u (differential unity gain)", "fu", "fu"))
_CM_TOL = 0.011      # dB: two-decimal display of a dB mean / quantile, plus rounding of the inputs
_CM_DERIVE_TOL = 0.03  # dB: a statistic derived from other displayed (rounded) columns
_CM_REL = 0.006      # relative: |Acm| columns are displayed to 3-4 significant digits
_CM_COLS = ("mean", "sigma", "skew", "min", "p1", "p5", "lin3", "db3", "acm_mean", "acm_sigma", "ad_mean")


def _cm_lin3(ad_mean: float, acm_mean: float, acm_sigma: float) -> float:
    import math
    return ad_mean - 20 * math.log10(acm_mean + 3 * acm_sigma)


def _cm_quantile(sorted_vals: list, q: float) -> float:
    k = (len(sorted_vals) - 1) * q
    f = int(k)
    return sorted_vals[f] + (sorted_vals[min(f + 1, len(sorted_vals) - 1)] - sorted_vals[f]) * (k - f)


def cmrr_mc_stats(cmrr: list, acm: list, ad: list) -> dict:
    """The record's per-point statistics from per-sample CMRR (dB), Acm (V/V) and Ad (dB):
    sample sigma (n-1), skewness with the population sigma, linear-interpolated percentiles,
    mean |Acm| and its sample sigma (the driver's definitions)."""
    import statistics
    n = len(cmrr)
    mean, sigma = statistics.fmean(cmrr), statistics.stdev(cmrr)
    sp = statistics.pstdev(cmrr)
    skew = sum((x - mean) ** 3 for x in cmrr) / n / sp ** 3 if sp > 0 else 0.0
    a = [abs(x) for x in acm]
    am, asg = statistics.fmean(a), statistics.stdev(a)
    srt = sorted(cmrr)
    return {"n": n, "mean": mean, "sigma": sigma, "skew": skew, "min": srt[0],
            "p1": _cm_quantile(srt, 0.01), "p5": _cm_quantile(srt, 0.05),
            "lin3": _cm_lin3(statistics.fmean(ad), am, asg), "db3": mean - 3 * sigma,
            "acm_mean": am, "acm_sigma": asg, "ad_mean": statistics.fmean(ad)}


def _cm_close(col: str, rec: float, got: float) -> bool:
    if col in ("acm_mean", "acm_sigma"):
        return abs(rec - got) <= _CM_REL * abs(got) + 1e-12
    return abs(rec - got) <= _CM_TOL


def _cm_worst(stats: dict, order: list) -> dict:
    """Worst point per statistic (ties: first in grid order, as the driver)."""
    return {"lin3": min(order, key=lambda k: stats[k]["lin3"]), "db3": min(order, key=lambda k: stats[k]["db3"]),
            "min": min(order, key=lambda k: stats[k]["min"]), "p5": min(order, key=lambda k: stats[k]["p5"])}


def extract_cmrr_mc(text: str, label: str) -> dict:
    """CMRR mismatch Monte Carlo record (issue #61): every figure's per-point statistics, the
    worst point per figure and the control results, with each linear 3-sigma and dB 3-sigma
    re-derived from the record's own columns. The retained per-sample evidence (samples.csv)
    is cross-checked separately (`cmrr_mc_crosscheck`)."""
    prov = parse_provenance(text, label)
    hd = header_of(text)
    pop = re.search(r"^- \*\*Population\*\*: process ([\w, ]+); T (-?\d+) C; VDD ([\d.]+) V .*?"
                    r"`monte_carlo = \{n: (\d+), seed: (\d+), vary: \"(\w+)\"\}` per grid point -> (\d+) x (\d+) = (\d+) samples",
                    hd, re.M)
    if not pop:
        raise ReportError(f"{label}: no Population line (processes, T, VDD, monte_carlo n / seed / vary)")
    procs = [p.strip() for p in pop.group(1).split(",")]
    t_c, vdd = pop.group(2), pop.group(3)
    n_want, seed, vary = int(pop.group(4)), int(pop.group(5)), pop.group(6)
    n_pts, n_each, n_total = int(pop.group(7)), int(pop.group(8)), int(pop.group(9))
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if n_want < CMRR_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the CMRR mismatch statistic needs N >= {CMRR_MC_MIN_N}")
    if (n_pts, n_each, n_total) != (len(procs), n_want, len(procs) * n_want) or len(set(procs)) != len(procs):
        raise ReportError(f"{label}: Population line is inconsistent ({n_pts} x {n_each} = {n_total} samples for "
                          f"{len(procs)} processes at N={n_want})")
    order = [_okey(p, t_c, vdd) for p in procs]
    sec = section(text, "Statistics per grid point")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    tables = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    if sorted(tables) != sorted(h for h, _, _ in CMRR_MC_FIGS):
        raise ReportError(f"{label}: per-figure tables {sorted(tables)} are not the expected "
                          f"{[h for h, _, _ in CMRR_MC_FIGS]}")
    stats: dict = {}
    for head, key, _ in CMRR_MC_FIGS:
        lines = [ln for ln in tables[head].splitlines() if ln.startswith("|")]
        if len(lines) < 3:
            raise ReportError(f"{label}: no per-point table under '### {head}'")
        st: dict = {}
        for ln in lines[2:]:
            c = split_cells(ln)
            m = re.fullmatch(_PT, c[0]) if c else None
            if not m or len(c) != 13:
                raise ReportError(f"{label}: malformed CMRR row under '### {head}': {ln.strip()}")
            k = _okey(*m.groups())
            try:
                n = int(c[1])
                vals = [float(x.strip("*").replace("+", "")) for x in c[2:]]
            except ValueError:
                raise ReportError(f"{label}: malformed CMRR row under '### {head}': {ln.strip()}") from None
            if k in st:
                raise ReportError(f"{label}: duplicate grid point {fmt_point(*k)} under '### {head}'")
            if k not in order:
                raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} under '### {head}' "
                                  "(not in the Population line)")
            if n != n_want:
                raise ReportError(f"{label}: insufficient samples at {fmt_point(*k)} under '### {head}': N={n}, "
                                  f"the request is N={n_want} per point")
            # columns: mean sigma skew min p1 p5 lin3 db3 acm_mean acm_sigma ad_mean
            st[k] = {"n": n, **dict(zip(_CM_COLS, vals))}
        missing = [k for k in order if k not in st]
        if missing:
            raise ReportError(f"{label}: '### {head}' is missing {len(missing)} of {len(order)} grid points "
                              f"(first: {fmt_point(*missing[0])})")
        for k, s in st.items():
            if (abs(s["lin3"] - _cm_lin3(s["ad_mean"], s["acm_mean"], s["acm_sigma"])) > _CM_DERIVE_TOL
                    or abs(s["db3"] - (s["mean"] - 3 * s["sigma"])) > _CM_DERIVE_TOL
                    or s["sigma"] <= 0 or s["min"] > s["p1"] + _CM_TOL or s["p1"] > s["p5"] + _CM_TOL):
                raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} under '### {head}': its linear "
                                  "3-sigma, dB 3-sigma or percentile columns disagree with its own columns")
        stats[key] = st
    # ---- worst point per figure ----
    wsec = section(text, "Worst point per figure")
    wl = [split_cells(ln) for ln in wsec.splitlines() if ln.startswith("|")][2:]
    worst: dict = {}
    for c in wl:
        if len(c) != 8:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}")
        fig = next((f for f in CMRR_MC_FIGS if f[0] == c[0]), None)
        if fig is None or fig[1] in worst:
            raise ReportError(f"{label}: worst-point table row '{c[0]}' is unexpected or duplicated")
        try:
            vals = {"lin3": float(c[1].strip("*")), "db3": float(c[3]), "min": float(c[5]), "p5": float(c[7])}
        except ValueError:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}") from None
        ats = {"lin3": c[2], "db3": c[4], "min": c[6]}
        st = stats[fig[1]]
        w = _cm_worst(st, order)
        for col, v in vals.items():
            if abs(v - st[w[col]][col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({fig[0]}, {col}: {v}) disagrees with its own per-point "
                                  f"table ({st[w[col]][col]:.2f} at {fmt_point(*w[col])})")
        for col, at in ats.items():
            mm = re.fullmatch(_PT, at)
            if not mm or _okey(*mm.groups()) not in order or abs(st[_okey(*mm.groups())][col] - vals[col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({fig[0]}, {col}) names '{at}', which is not a point "
                                  "holding that worst value in the per-point table")
        worst[fig[1]] = {col: {"value": vals[col], "at": w[col]} for col in ("lin3", "db3", "min", "p5")}
    if sorted(worst) != sorted(k for _, k, _ in CMRR_MC_FIGS):
        raise ReportError(f"{label}: worst-point table does not cover every figure")
    # ---- controls ----
    ctl = section(text, "Controls")
    so = re.search(r"\*\*Switch-off\*\*.*?(?=\*\*Process-only\*\*)", ctl, re.S)
    po = re.search(r"\*\*Process-only\*\*.*?CMRR DC sigma ([\d.]+) dB, offset sigma ([\d.]+) mV", ctl, re.S)
    ib = re.search(r"\*\*Negative control: mirror imbalance\*\*.*?Result: \*\*PASS\*\*", ctl, re.S)
    sh = re.search(r"shift of mean \|Acm\|: ([+-][\d.]+) V/V; standard error of the difference ([\d.]*\d)", ctl)
    if not (so and "Result: **PASS**" in so.group(0) and po and ib and sh):
        raise ReportError(f"{label}: the controls section lacks a passing switch-off control, the process-only result "
                          "or a passing mirror-imbalance negative control")
    devs = [split_cells(ln) for ln in so.group(0).splitlines() if ln.startswith("| ") and "/ 27 C" in ln]
    try:
        dev_dc = max(float(c[-2]) for c in devs)
        dev_fu = max(float(c[-1]) for c in devs)
    except (ValueError, IndexError):
        raise ReportError(f"{label}: malformed switch-off control table") from None
    if not devs:
        raise ReportError(f"{label}: malformed switch-off control table")
    shift, se = float(sh.group(1)), float(sh.group(2))
    if se <= 0 or shift / se <= 3:
        raise ReportError(f"{label}: the mirror-imbalance control does not exceed 3 standard errors "
                          f"({shift} V/V, SE {se})")
    if not re.search(r"^- All blocking checks passed: yes\.", section(text, "Validation summary"), re.M):
        raise ReportError(f"{label}: validation summary does not state 'All blocking checks passed: yes'")
    cov = coverage_of(set(order))
    cov["mc_samples_per_corner"] = n_want
    lim = common_limitations(text, prov, cov)
    lim += [f"temperature and supply are NOT sampled under mismatch: {t_c} C / {vdd} V only (the systematic "
            "cmrr record covers T and VDD for matched devices only)",
            "MOS mismatch only; passives (RZ, CC) at typical with no passive mismatch, resistor and capacitor "
            "mismatch not modelled",
            f"N={n_want} cannot resolve a 0.135 % tail: neither the linear nor the dB 3-sigma figure is a sample "
            "statistic; the empirical minimum and percentiles are the normality cross-check",
            "mismatch Monte Carlo of the CMRR; no numeric bound proposed or judged here (DR-6 is proposed, not ratified)"]
    return {"prov": prov, "coverage": cov, "order": order, "stats": stats, "worst": worst, "n_per_point": n_want,
            "n_total": n_total, "seed": seed, "vary": vary, "t_c": t_c, "vdd": vdd,
            "controls": {"switch_off_max_dev_db_dc_to_1m": dev_dc, "switch_off_max_dev_db_at_fu": dev_fu,
                         "process_only_cmrr_dc_sigma_db": float(po.group(1)),
                         "process_only_offset_sigma_mv": float(po.group(2)),
                         "imbalance_shift_acm_vv": shift, "imbalance_shift_se": se},
            "limitations": lim}


def cmrr_mc_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every figure's per-point statistics, and the worst points, from the record's
    retained per-sample evidence (corners/<rid>/samples.csv), within display precision. Offline."""
    import csv
    import math
    rel = f"sim/cmrr-mc/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    need = ["process", "temperature_c", "vdd_v", "sample_index", "valid"] + \
           [f"cmc_{q}_{s}" for _, _, s in CMRR_MC_FIGS for q in ("cmrr", "ad", "acm")]
    by: dict = {}
    seen = set()
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns it needs")
        for r in rd:
            try:
                k = _okey(r["process"], r["temperature_c"], r["vdd_v"])
                idx, valid = int(r["sample_index"]), int(r["valid"])
                vals = {(q, s): float(r[f"cmc_{q}_{s}"]) for _, _, s in CMRR_MC_FIGS for q in ("cmrr", "ad", "acm")}
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["order"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if valid != 1 or not all(math.isfinite(v) for v in vals.values()):
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)}")
            seen.add((k, idx))
            by.setdefault(k, []).append(vals)
    for k in ex["order"]:
        if len(by.get(k, [])) != ex["n_per_point"]:
            raise ReportError(f"{label}: insufficient samples in {rel} at {fmt_point(*k)}: {len(by.get(k, []))}, the "
                              f"record states N={ex['n_per_point']}")
    for head, key, s in CMRR_MC_FIGS:
        got = {k: cmrr_mc_stats([v[("cmrr", s)] for v in by[k]], [v[("acm", s)] for v in by[k]],
                                [v[("ad", s)] for v in by[k]]) for k in ex["order"]}
        for k in ex["order"]:
            rec = ex["stats"][key][k]
            bad = [c for c in _CM_COLS if not _cm_close(c, rec[c], got[k][c])]
            if bad:
                raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({head}): the record's "
                                  f"{', '.join(bad)} disagree(s) with the retained evidence {rel} (e.g. {bad[0]} "
                                  f"{rec[bad[0]]:g} vs {got[k][bad[0]]:g})")
        w = _cm_worst(got, ex["order"])
        for col in ("lin3", "db3", "min", "p5"):
            if abs(ex["worst"][key][col]["value"] - got[w[col]][col]) > _CM_TOL:
                raise ReportError(f"{label}: the record's worst {col} for {head} disagrees with the retained "
                                  f"evidence {rel}")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(by)}


# --------------------------------------------------------------------------
# PSRR mismatch Monte Carlo (issue #182): measured, never graded while proposed
# --------------------------------------------------------------------------
PSRR_MC_EXP = "psrr-mc"
PSRR_MC_MIN_N = 300
#: (record side label, samples.csv rail suffix, supply-gain symbol), in the record's order
PSRR_MC_SIDES = (("PSRR+", "vdd", "Avdd"), ("PSRR-", "vss", "Avss"))
PSRR_MC_FIGS = CMRR_MC_FIGS  # the same six frequency figures, same headings
PSRR_MC_COLS = _CM_COLS      # mean sigma skew min p1 p5 lin3 db3 |Asupply| mean / sigma, mean Ad (dB)


def _pm_key(side: str, key: str) -> str:
    return f"{side} {key}"


def extract_psrr_mc(text: str, label: str) -> dict:
    """PSRR mismatch Monte Carlo record of either committed format.

    The issue #99 nominal record (five MOS corners at 27 C / 3.30 V) and the
    issue #190 full PVT grid record (`run_psrr_mc.py --grid full`) differ in
    population; the record's retained measurement-fingerprint inputs decide
    which parser reads it (`"grid": "full"`), and a record that matches
    neither is rejected rather than guessed at.
    """
    fp = parse_fingerprint(text, label)
    if fp is not None and fp["inputs"].get("grid") == "full":
        return extract_psrr_mc_grid(text, label, fp)
    return extract_psrr_mc_nominal(text, label)


def _psrr_mc_tables(text: str, label: str, order: list, n_want: int) -> tuple[dict, dict]:
    """Per-side and per-figure per-point statistics of a PSRR mismatch record,
    each row's linear 3-sigma and dB 3-sigma re-derived from the row's own
    columns (shared by the nominal and full-grid parsers)."""
    sec = section(text, "Statistics per grid point")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    tables = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    want = [f"{s} {h}" for s, _, _ in PSRR_MC_SIDES for h, _, _ in PSRR_MC_FIGS]
    if sorted(tables) != sorted(want):
        raise ReportError(f"{label}: per-figure tables {sorted(tables)} are not the expected {want}")
    stats: dict = {}
    knee_dcs: dict = {}
    for sname, _, _ in PSRR_MC_SIDES:
        for head, key, _ in PSRR_MC_FIGS:
            fig_head = f"{sname} {head}"
            lines = [ln for ln in tables[fig_head].splitlines() if ln.startswith("|")]
            if len(lines) < 3:
                raise ReportError(f"{label}: no per-point table under '### {fig_head}'")
            is_dc = key == "dc"
            st: dict = {}
            for ln in lines[2:]:
                c = split_cells(ln)
                m = re.fullmatch(_PT, c[0]) if c else None
                if not m or len(c) != (15 if is_dc else 14):
                    raise ReportError(f"{label}: malformed PSRR row under '### {fig_head}': {ln.strip()}")
                k = _okey(*m.groups())
                try:
                    n = int(c[1])
                    vals = [float(x.strip("*").replace("+", "")) for x in c[2:13]]
                    knee = int(c[14]) if is_dc else 0
                except ValueError:
                    raise ReportError(f"{label}: malformed PSRR row under '### {fig_head}': {ln.strip()}") from None
                if k in st:
                    raise ReportError(f"{label}: duplicate grid point {fmt_point(*k)} under '### {fig_head}'")
                if k not in order:
                    raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} under '### {fig_head}' "
                                      "(not in the record's population)")
                if n != n_want:
                    raise ReportError(f"{label}: insufficient samples at {fmt_point(*k)} under '### {fig_head}': "
                                      f"N={n}, the request is N={n_want} per point")
                st[k] = {"n": n, **dict(zip(PSRR_MC_COLS, vals))}
                if is_dc:
                    knee_dcs[(sname, k)] = knee
            missing = [k for k in order if k not in st]
            if missing:
                raise ReportError(f"{label}: '### {fig_head}' is missing {len(missing)} of {len(order)} grid "
                                  f"points (first: {fmt_point(*missing[0])})")
            for k, s in st.items():
                if (abs(s["lin3"] - _cm_lin3(s["ad_mean"], s["acm_mean"], s["acm_sigma"])) > _CM_DERIVE_TOL
                        or abs(s["db3"] - (s["mean"] - 3 * s["sigma"])) > _CM_DERIVE_TOL
                        or s["sigma"] <= 0 or s["min"] > s["p1"] + _CM_TOL or s["p1"] > s["p5"] + _CM_TOL):
                    raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} under '### {fig_head}': its "
                                      "linear 3-sigma, dB 3-sigma or percentile columns disagree with its own columns")
            stats[_pm_key(sname, key)] = st
    return stats, knee_dcs


def _psrr_mc_worst(text: str, label: str, stats: dict, order: list) -> dict:
    """The worst-point-per-figure table of a PSRR mismatch record, checked
    against its own per-point tables."""
    wsec = section(text, "Worst point per figure")
    wl = [split_cells(ln) for ln in wsec.splitlines() if ln.startswith("|")][2:]
    heads = {f"{s} {h}": _pm_key(s, k) for s, _, _ in PSRR_MC_SIDES for h, k, _ in PSRR_MC_FIGS}
    worst: dict = {}
    for c in wl:
        if len(c) != 8:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}")
        wk = heads.get(c[0])
        if wk is None or wk in worst:
            raise ReportError(f"{label}: worst-point table row '{c[0]}' is unexpected or duplicated")
        try:
            vals = {"lin3": float(c[1].strip("*")), "db3": float(c[3]), "min": float(c[5]), "p5": float(c[7])}
        except ValueError:
            raise ReportError(f"{label}: malformed worst-point row: {' | '.join(c)}") from None
        ats = {"lin3": c[2], "db3": c[4], "min": c[6]}
        st = stats[wk]
        w = _cm_worst(st, order)
        for col, v in vals.items():
            if abs(v - st[w[col]][col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({c[0]}, {col}: {v}) disagrees with its own per-point "
                                  f"table ({st[w[col]][col]:.2f} at {fmt_point(*w[col])})")
        for col, at in ats.items():
            mm = re.fullmatch(_PT, at)
            if not mm or _okey(*mm.groups()) not in order or abs(st[_okey(*mm.groups())][col] - vals[col]) > _CM_TOL:
                raise ReportError(f"{label}: worst-point table ({c[0]}, {col}) names '{at}', which is not a point "
                                  "holding that worst value in the per-point table")
        worst[wk] = {col: {"value": vals[col], "at": w[col]} for col in ("lin3", "db3", "min", "p5")}
    if sorted(worst) != sorted(heads.values()):
        raise ReportError(f"{label}: worst-point table does not cover every side and figure")
    return worst


def _psrr_mc_floor_knees(text: str, label: str, knee_dcs: dict) -> tuple[dict, int]:
    """The numerical-floor counts per side and the near-cancellation knee
    total of a PSRR mismatch record, the latter checked against the knee DC
    counts of its per-point tables."""
    nf = section(text, "Numerical floor and near-cancellation")
    floors = {}
    for sname, _, sym in PSRR_MC_SIDES:
        fm = re.search(rf"^- {re.escape(sname)}: samples with \|{sym}\| below the numerical floor .*?: \*\*(\d+)\*\*;", nf, re.M)
        if not fm:
            raise ReportError(f"{label}: no numerical-floor line for {sname}")
        floors[sname] = int(fm.group(1))
    km = re.search(r"^### Near-cancellation knees \((\d+) sample rails\)", nf, re.M)
    if not km:
        raise ReportError(f"{label}: no near-cancellation knee accounting")
    knees = int(km.group(1))
    if sum(v for (s, _), v in knee_dcs.items() if s == "PSRR+") + sum(v for (s, _), v in knee_dcs.items() if s == "PSRR-") != knees:
        raise ReportError(f"{label}: the knee DC counts of the per-point tables do not sum to the {knees} knee rails")
    return floors, knees


def _psrr_mc_controls(text: str, label: str) -> tuple[float, float, re.Match, dict]:
    """The controls section of a PSRR mismatch record: the passing switch-off
    control (with its deviation table), the process-only result and the
    passing mirror-imbalance negative control."""
    ctl = section(text, "Controls")
    so = re.search(r"\*\*Switch-off\*\*.*?(?=\*\*Process-only\*\*)", ctl, re.S)
    po = re.search(r"\*\*Process-only\*\*.*?PSRR\+ DC sigma ([\d.]+) dB, PSRR- DC sigma ([\d.]+) dB, offset sigma ([\d.]+) mV",
                   ctl, re.S)
    ib = re.search(r"\*\*Negative control: mirror imbalance\*\*.*", ctl, re.S)
    if not (so and "Result: **PASS**" in so.group(0) and po and ib and "Result: **PASS**" in ib.group(0)):
        raise ReportError(f"{label}: the controls section lacks a passing switch-off control, the process-only result "
                          "or a passing mirror-imbalance negative control")
    devs = [split_cells(ln) for ln in so.group(0).splitlines() if ln.startswith("| ") and "/ 27 C" in ln]
    try:
        dev_dc = max(float(c[-3]) for c in devs)
        dev_fu = max(float(c[-2]) for c in devs)
    except (ValueError, IndexError):
        raise ReportError(f"{label}: malformed switch-off control table") from None
    if not devs:
        raise ReportError(f"{label}: malformed switch-off control table")
    imb: dict = {}
    for sname, _, _ in PSRR_MC_SIDES:
        rm = re.search(rf"^\| {re.escape(sname)} DC \| XM3 W [\d.]+u \|.*\| ([+-][\d.]+) / ([\d.]*\d) \|\s*$", ib.group(0), re.M)
        if not rm:
            raise ReportError(f"{label}: no mirror-imbalance row for {sname}")
        shift, se = float(rm.group(1)), float(rm.group(2))
        if se <= 0 or shift / se <= 3:
            raise ReportError(f"{label}: the mirror-imbalance control does not exceed 3 standard errors for {sname} "
                              f"({shift} V/V, SE {se})")
        imb[sname] = {"shift_asup_vv": shift, "shift_se": se}
    return dev_dc, dev_fu, po, imb


def extract_psrr_mc_nominal(text: str, label: str) -> dict:
    """PSRR mismatch Monte Carlo nominal record (issue #99): per side (PSRR+ / PSRR-) and figure, the
    per-point statistics, the worst point per figure, the near-cancellation knee accounting and
    the control results, with each linear 3-sigma and dB 3-sigma re-derived from the record's own
    columns. The retained per-sample evidence (samples.csv) is cross-checked separately
    (`psrr_mc_crosscheck`)."""
    prov = parse_provenance(text, label)
    hd = header_of(text)
    pop = re.search(r"^- \*\*Population\*\*: process ([\w, ]+); T (-?\d+) C; VDD ([\d.]+) V .*?"
                    r"`monte_carlo = \{n: (\d+), seed: (\d+), vary: \"(\w+)\"\}` per grid point -> (\d+) x (\d+) = (\d+) samples",
                    hd, re.M)
    if not pop:
        raise ReportError(f"{label}: no Population line (processes, T, VDD, monte_carlo n / seed / vary)")
    procs = [p.strip() for p in pop.group(1).split(",")]
    t_c, vdd = pop.group(2), pop.group(3)
    n_want, seed, vary = int(pop.group(4)), int(pop.group(5)), pop.group(6)
    n_pts, n_each, n_total = int(pop.group(7)), int(pop.group(8)), int(pop.group(9))
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if n_want < PSRR_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the PSRR mismatch statistic needs N >= {PSRR_MC_MIN_N}")
    if (n_pts, n_each, n_total) != (len(procs), n_want, len(procs) * n_want) or len(set(procs)) != len(procs):
        raise ReportError(f"{label}: Population line is inconsistent ({n_pts} x {n_each} = {n_total} samples for "
                          f"{len(procs)} processes at N={n_want})")
    order = [_okey(p, t_c, vdd) for p in procs]
    stats, knee_dcs = _psrr_mc_tables(text, label, order, n_want)
    # ---- worst point per figure ----
    worst = _psrr_mc_worst(text, label, stats, order)
    # ---- numerical floor and near-cancellation knees ----
    floors, knees = _psrr_mc_floor_knees(text, label, knee_dcs)
    # ---- controls ----
    dev_dc, dev_fu, po, imb = _psrr_mc_controls(text, label)
    if not re.search(r"^- All blocking checks passed: yes\.", section(text, "Validation summary"), re.M):
        raise ReportError(f"{label}: validation summary does not state 'All blocking checks passed: yes'")
    cov = coverage_of(set(order))
    cov["mc_samples_per_corner"] = n_want
    lim = common_limitations(text, prov, cov)
    lim += [f"temperature and supply are NOT sampled under mismatch: {t_c} C / {vdd} V only (the systematic "
            "psrr record covers T and VDD for matched devices only; its four Avss sign-flip points, all at 125 C, "
            "remain systematic-only)",
            "MOS mismatch only; passives (RZ, CC) at typical with no passive mismatch, resistor and capacitor "
            "mismatch not modelled",
            f"N={n_want} cannot resolve a 0.135 % tail: neither the linear nor the dB 3-sigma figure is a sample "
            "statistic; the empirical minimum and percentiles are the normality cross-check",
            f"{knees} near-cancellation knee rail(s) (PSRR+ DC band non-flat but monotonically rising) are accepted "
            "with the 0.1 Hz asymptote as their DC figure",
            "supply rejection of the bias generator is excluded (the ideal 10 uA bias source carries no small-signal "
            "current)",
            "mismatch Monte Carlo of the PSRR; no numeric bound proposed or judged here (DR-6 is proposed, not ratified)"]
    if any(floors.values()):
        lim.append("samples below the numerical floor are clamped, so their PSRR figure is a lower bound: "
                   + ", ".join(f"{s} {n}" for s, n in floors.items()))
    return {"prov": prov, "coverage": cov, "order": order, "stats": stats, "worst": worst, "n_per_point": n_want,
            "n_total": n_total, "seed": seed, "vary": vary, "t_c": t_c, "vdd": vdd, "knee_rails": knees,
            "knee_dcs": {f"{s}|{fmt_point(*k)}": v for (s, k), v in knee_dcs.items()}, "floor_samples": floors,
            "controls": {"switch_off_max_dev_db_dc_to_1m": dev_dc, "switch_off_max_dev_db_at_fu": dev_fu,
                         "process_only_psrr_plus_dc_sigma_db": float(po.group(1)),
                         "process_only_psrr_minus_dc_sigma_db": float(po.group(2)),
                         "process_only_offset_sigma_mv": float(po.group(3)),
                         "imbalance": imb},
            "limitations": lim}


def extract_psrr_mc_grid(text: str, label: str, fp: dict) -> dict:
    """Full PVT grid PSRR mismatch record (issue #190): every (process, T, VDD) point's statistics,
    the worst point per figure and the T x VDD summary tables, each re-derived from the record's
    own columns. The point set and N are those of the record's retained measurement-fingerprint
    inputs (hash-verified), so a missing, duplicate or unexpected point, or a point with too few
    samples, is rejected. The retained per-sample evidence (samples.csv) is cross-checked
    separately (`psrr_mc_crosscheck`)."""
    prov = parse_provenance(text, label)
    inp = fp["inputs"]
    try:
        ax = inp["corners"]
        procs, temps, sups, vcms = ax["process"], ax["temperature_c"], ax["supply_v"], ax["vcm_v"]
        n_want = int(inp["monte_carlo"]["n"])
        seed, vary = int(inp["monte_carlo"]["seed"]), inp["monte_carlo"]["vary"]
    except (KeyError, TypeError, ValueError):
        raise ReportError(f"{label}: retained measurement-fingerprint inputs do not describe a full PSRR grid "
                          "(need \"grid\": \"full\", corners.process/temperature_c/supply_v/vcm_v and "
                          "monte_carlo.n/seed/vary)") from None
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if len(vcms) != len(sups) or [round(float(v) / 2, 6) for v in sups] != [float(x) for x in vcms]:
        raise ReportError(f"{label}: retained inputs' VCM axis is not VDD/2 of the supplies")
    order = [_okey(p, t, v) for p in procs for t in temps for v in sups]  # driver's grid order
    if len(set(order)) != len(order):
        raise ReportError(f"{label}: retained inputs repeat a grid axis value")
    if n_want < PSRR_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per point; "
                          f"the PSRR mismatch statistic needs N >= {PSRR_MC_MIN_N}")
    gl = re.search(r"^- \*\*Grid\*\*: full PVT -- process ([\w, ]+); T ([\d.,\- ]+) C; VDD ([\d., ]+) V "
                   r"\(VCM = VDD/2: ([\d., ]+) V\) = (\d+) points, `monte_carlo = \{n: (\d+), seed: (\d+), "
                   r"vary: \"(\w+)\"\}` per point -> (\d+) x (\d+) = (\d+) samples$", header_of(text), re.M)
    if not gl:
        raise ReportError(f"{label}: no '- **Grid**' line (processes, T and VDD axes, monte_carlo n / seed / vary)")
    temps_g = [f"{float(x):g}" for x in gl.group(2).replace(" ", "").split(",")]
    sups_g = [f"{float(x):.2f}" for x in gl.group(3).replace(" ", "").split(",")]
    vcms_g = [f"{float(x):g}" for x in gl.group(4).replace(" ", "").split(",")]
    if ([p.strip() for p in gl.group(1).split(",")], temps_g, sups_g, vcms_g) != \
            ([str(p) for p in procs], [f"{float(t):g}" for t in temps], [f"{float(v):.2f}" for v in sups],
             [f"{float(x):g}" for x in vcms]) \
            or (int(gl.group(5)), int(gl.group(6)), int(gl.group(7)), gl.group(8)) != \
               (len(order), n_want, seed, vary) \
            or (int(gl.group(9)), int(gl.group(10)), int(gl.group(11))) != (len(order), n_want, len(order) * n_want):
        raise ReportError(f"{label}: Grid line disagrees with the retained inputs "
                          f"({len(order)} points, N={n_want}, seed {seed}, vary \"{vary}\")")
    stats, knee_dcs = _psrr_mc_tables(text, label, order, n_want)
    worst = _psrr_mc_worst(text, label, stats, order)
    floors, knees = _psrr_mc_floor_knees(text, label, knee_dcs)
    dev_dc, dev_fu, po, imb = _psrr_mc_controls(text, label)
    _psrr_mc_grid_tv(text, label, stats, order, temps, sups)
    valid = re.search(r"^Result: all (\d+) samples valid;", section(text, "Extraction validation"), re.M)
    if not valid or int(valid.group(1)) != len(order) * n_want:
        raise ReportError(f"{label}: extraction validation does not state all {len(order) * n_want} samples valid "
                          "(the record lists problems, or its count disagrees with the grid)")
    if not re.search(r"^- All blocking checks passed: yes\.", section(text, "Validation summary"), re.M):
        raise ReportError(f"{label}: validation summary does not state 'All blocking checks passed: yes'")
    cov = coverage_of(set(order))
    cov["mc_samples_per_point"] = n_want
    lim = common_limitations(text, prov, cov)
    lim += ["full PVT grid under mismatch: temperature and supply are sampled (VCM = VDD/2 at every supply), "
            "but the RZ/CC passives stay typical at every point and no passive mismatch is modelled",
            f"N={n_want} cannot resolve a 0.135 % tail: neither the linear nor the dB 3-sigma figure is a sample "
            "statistic; the empirical minimum and percentiles are the normality cross-check",
            f"{knees} near-cancellation knee rail(s) (PSRR+ DC band non-flat but monotonically rising) are accepted "
            "with the 0.1 Hz asymptote as their DC figure",
            "supply rejection of the bias generator is excluded (the ideal 10 uA bias source carries no small-signal "
            "current)",
            "mismatch Monte Carlo of the PSRR over the T/VDD grid; no numeric bound proposed, ratified or judged "
            "at any point (DR-6 is proposed, not ratified, and covers mismatch only at 27 C / 3.30 V)"]
    if any(floors.values()):
        lim.append("samples below the numerical floor are clamped, so their PSRR figure is a lower bound: "
                   + ", ".join(f"{s} {n}" for s, n in floors.items()))
    return {"prov": prov, "coverage": cov, "grid": True, "order": order, "stats": stats, "worst": worst,
            "n_per_point": n_want, "n_total": len(order) * n_want, "seed": seed, "vary": vary,
            "t_c": None, "vdd": None,
            "temps_c": [f"{float(t):g}" for t in temps], "supplies_v": [f"{float(v):.2f}" for v in sups],
            "vcm_of": {f"{float(v):.2f}": float(x) for v, x in zip(sups, vcms)},
            "knee_rails": knees, "knee_dcs": {f"{s}|{fmt_point(*k)}": v for (s, k), v in knee_dcs.items()},
            "floor_samples": floors,
            "controls": {"switch_off_max_dev_db_dc_to_1m": dev_dc, "switch_off_max_dev_db_at_fu": dev_fu,
                         "process_only_psrr_plus_dc_sigma_db": float(po.group(1)),
                         "process_only_psrr_minus_dc_sigma_db": float(po.group(2)),
                         "process_only_offset_sigma_mv": float(po.group(3)),
                         "imbalance": imb},
            "limitations": lim}


def _psrr_mc_grid_tv(text: str, label: str, stats: dict, order: list, temps, sups) -> None:
    """The T x VDD worst-corner tables of a full-grid PSRR record (linear
    3-sigma per DR-6 figure), each cell checked against the per-point table."""
    sec = section(text, "Linear 3-sigma by temperature and supply")
    parts = re.split(r"^### (.+)$", sec, flags=re.M)
    tables = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    heads = {(s, k): f"{s} {h}" for s, _, _ in PSRR_MC_SIDES for h, k, _ in PSRR_MC_FIGS if k in ("dc", "1k", "10k")}
    if sorted(tables) != sorted(heads.values()):
        raise ReportError(f"{label}: T x VDD tables {sorted(tables)} are not the expected {sorted(heads.values())}")
    for (sname, key), head in heads.items():
        rail = next(r for s, r, _ in PSRR_MC_SIDES if s == sname)
        lines = [ln for ln in tables[head].splitlines() if ln.startswith("|")][2:]
        if len(lines) != len(temps):
            raise ReportError(f"{label}: T x VDD table '{head}' has {len(lines)} rows, the grid has {len(temps)} "
                              "temperatures")
        got: dict = {}
        for ln in lines:
            c = split_cells(ln)
            m = re.fullmatch(r"(-?[\d.]+) C", c[0]) if c else None
            if not m or len(c) != len(sups) + 1:
                raise ReportError(f"{label}: malformed T x VDD row under '{head}': {ln.strip()}")
            for v, cell in zip(sups, c[1:]):
                mc_ = re.fullmatch(r"([\d.]+) \((\w+)\)", cell)
                if not mc_:
                    raise ReportError(f"{label}: malformed T x VDD cell '{cell}' under '{head}'")
                got[(f"{float(m.group(1)):g}", f"{float(v):.2f}")] = (float(mc_.group(1)), mc_.group(2))
        if {t for t, _ in got} != {f"{float(t):g}" for t in temps}:
            raise ReportError(f"{label}: T x VDD table '{head}' does not carry exactly the grid's temperatures")
        for t in temps:
            for v in sups:
                tk = (f"{float(t):g}", f"{float(v):.2f}")
                pts = [k for k in order if k[1] == tk[0] and k[2] == tk[1]]
                kk = min(pts, key=lambda k: stats[_pm_key(sname, key)][k]["lin3"])
                cell = got.get(tk)
                if cell is None or cell[1] != kk[0] or abs(cell[0] - stats[_pm_key(sname, key)][kk]["lin3"]) > _CM_TOL:
                    raise ReportError(f"{label}: T x VDD table '{head}' at {tk[0]} C / {tk[1]} V "
                                      f"({'missing' if cell is None else f'{cell[0]:.2f} ({cell[1]})'}) disagrees "
                                      f"with its own per-point table "
                                      f"({stats[_pm_key(sname, key)][kk]['lin3']:.2f} ({kk[0]}))")


def psrr_mc_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every side's and figure's per-point statistics, the worst points and the knee
    accounting from the record's retained per-sample evidence (corners/<rid>/samples.csv),
    within display precision. Offline."""
    import csv
    import math
    rel = f"sim/psrr-mc/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    qs = [(r, q, s) for _, r, _ in PSRR_MC_SIDES for _, _, s in PSRR_MC_FIGS for q in ("psrr", "asup")]
    need = ["process", "temperature_c", "vdd_v", "sample_index", "valid", "knee_rails"] + \
           [f"pmc_{q}_{r}_{s}" for r, q, s in qs] + [f"pmc_ad_{s}" for _, _, s in PSRR_MC_FIGS] + \
           ["pmc_ad_b0"] + [f"pmc_{r}_aband0" for _, r, _ in PSRR_MC_SIDES]
    if ex.get("grid"):
        need += ["vcm_v"]  # issue #190: the grid record's CSV carries the commanded VCM per point
    by: dict = {}
    seen = set()
    knee_csv = 0
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns it needs")
        for r in rd:
            try:
                k = _okey(r["process"], r["temperature_c"], r["vdd_v"])
                idx, valid = int(r["sample_index"]), int(r["valid"])
                vals = {(q, rr, s): float(r[f"pmc_{q}_{rr}_{s}"]) for rr, q, s in qs}
                vals.update({("ad", None, s): float(r[f"pmc_ad_{s}"]) for _, _, s in PSRR_MC_FIGS})
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["order"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if ex.get("grid") and abs(float(r["vcm_v"]) - ex["vcm_of"][k[2]]) > 1e-6:
                raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} carries VCM {float(r['vcm_v']):g} V, "
                                  f"not the commanded {ex['vcm_of'][k[2]]:g} V (VCM = VDD/2)")
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if valid != 1 or not all(math.isfinite(v) for v in vals.values()):
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)}")
            seen.add((k, idx))
            knees = [x for x in r["knee_rails"].replace(",", " ").split() if x]
            knee_csv += len(knees)
            try:
                for kr in knees:
                    # a knee sample's DC figure is its 0.1 Hz asymptote, not the band mean (the record's definition)
                    a0 = float(r[f"pmc_{kr}_aband0"])
                    vals[("psrr", kr, "dc")] = float(r["pmc_ad_b0"]) - 20 * math.log10(a0)
                    vals[("asup", kr, "dc")] = a0
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed knee sample {idx} at {fmt_point(*k)}") from None
            by.setdefault(k, []).append(vals)
    for k in ex["order"]:
        if len(by.get(k, [])) != ex["n_per_point"]:
            raise ReportError(f"{label}: insufficient samples in {rel} at {fmt_point(*k)}: {len(by.get(k, []))}, the "
                              f"record states N={ex['n_per_point']}")
    if knee_csv != ex["knee_rails"]:
        raise ReportError(f"{label}: the record states {ex['knee_rails']} near-cancellation knee rail(s) but {rel} "
                          f"flags {knee_csv}")
    for sname, rail, _ in PSRR_MC_SIDES:
        for head, key, s in PSRR_MC_FIGS:
            got = {k: cmrr_mc_stats([v[("psrr", rail, s)] for v in by[k]], [v[("asup", rail, s)] for v in by[k]],
                                    [v[("ad", None, s)] for v in by[k]]) for k in ex["order"]}
            sk = _pm_key(sname, key)
            for k in ex["order"]:
                rec = ex["stats"][sk][k]
                bad = [c for c in PSRR_MC_COLS if not _cm_close(c, rec[c], got[k][c])]
                if bad:
                    raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({sname} {head}): the "
                                      f"record's {', '.join(bad)} disagree(s) with the retained evidence {rel} (e.g. "
                                      f"{bad[0]} {rec[bad[0]]:g} vs {got[k][bad[0]]:g})")
            w = _cm_worst(got, ex["order"])
            for col in ("lin3", "db3", "min", "p5"):
                if abs(ex["worst"][sk][col]["value"] - got[w[col]][col]) > _CM_TOL:
                    raise ReportError(f"{label}: the record's worst {col} for {sname} {head} disagrees with the "
                                      f"retained evidence {rel}")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(by)}


# --------------------------------------------------------------------------
# gain/GBW/PM mismatch Monte Carlo (issue #172): measured, never graded here
# --------------------------------------------------------------------------
GAIN_MC_EXP = "gain-gbw-pm-mc"
GAIN_MC_TITLE = "# Gain / GBW / PM mismatch Monte Carlo record"
GAIN_MC_MIN_N = 300
#: The three AC rows a mismatch sample is compared with, in the record's order:
#: (row key, samples.csv metric column, scale from the column to the published unit, unit)
GAIN_MC_ROWS = (("gain", "dc_gain_db", 1.0, "dB"), ("gbw", "gbw_hz", 1e-6, "MHz"), ("pm", "pm_deg", 1.0, "deg"))
#: Display precision of the record's statistics table (dB / deg at 2 decimals, MHz at 3)
_GAIN_MC_TOL = {"dB": 0.005 + 1e-6, "MHz": 0.0005 + 1e-6, "deg": 0.005 + 1e-6}
_GAIN_MC_FRAC_TOL = 0.00005 + 1e-9  # the failure fraction is displayed to 4 decimals
_GAIN_MC_DERIVE_TOL = 0.03  # mean-3sigma re-derived from displayed (rounded) mean and sigma
_GAIN_MC_ROW_RX = re.compile(r"(gain|gbw|pm) \(>= ([\d.]+) (dB|MHz|deg)\)")


def extract_gain_gbw_pm_mc(text: str, label: str) -> dict:
    """Gain/GBW/PM mismatch Monte Carlo record (issue #129): per-corner valid / mean / sigma /
    mean-3sigma / below-bound / invalid / failure-fraction for each of the three AC rows, each
    re-derived from the record's own columns and stated against the ratified bounds. The retained
    per-sample evidence (samples.csv) is cross-checked separately (`gain_gbw_pm_mc_crosscheck`).
    The record grades nothing: the deterministic gain/GBW/PM record stays responsible for the
    graded rows."""
    if not text.lstrip().startswith(GAIN_MC_TITLE):
        raise ReportError(f"{label}: not a '{GAIN_MC_TITLE.lstrip('# ')}' record")
    prov = parse_provenance(text, label)
    pop = re.search(r"^- \*\*Population\*\*: process ([\w, ]+); (-?\d+) C; VDD ([\d.]+) V .*?"
                    r"`monte_carlo = \{n: (\d+), seed: (\d+), vary: \"(\w+)\"\}` -> (\d+) x (\d+) = (\d+) samples",
                    header_of(text), re.M)
    if not pop:
        raise ReportError(f"{label}: no Population line (processes, T, VDD, monte_carlo n / seed / vary)")
    procs = [p.strip() for p in pop.group(1).split(",")]
    t_c, vdd = pop.group(2), pop.group(3)
    n_want, seed, vary = int(pop.group(4)), int(pop.group(5)), pop.group(6)
    n_pts, n_each, n_total = int(pop.group(7)), int(pop.group(8)), int(pop.group(9))
    if vary != "mismatch":
        raise ReportError(f"{label}: Monte Carlo varies '{vary}', not the mismatch population this report ingests")
    if n_want < GAIN_MC_MIN_N:
        raise ReportError(f"{label}: insufficient samples: the record's Monte Carlo request is N={n_want} per corner; "
                          f"the mismatch statistic needs N >= {GAIN_MC_MIN_N}")
    if (n_pts, n_each, n_total) != (len(procs), n_want, len(procs) * n_want) or len(set(procs)) != len(procs):
        raise ReportError(f"{label}: Population line is inconsistent ({n_pts} x {n_each} = {n_total} samples for "
                          f"{len(procs)} processes at N={n_want})")
    order = [_okey(p, t_c, vdd) for p in procs]
    sec = section(text, "Statistics per corner")
    lines = [ln for ln in sec.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        raise ReportError(f"{label}: no statistics table under '## Statistics per corner'")
    stats: dict = {}
    bounds: dict = {}
    units: dict = {}
    for ln in lines[2:]:
        c = split_cells(ln)
        m = re.fullmatch(_PT, c[0]) if c else None
        if not m or len(c) != 9:
            raise ReportError(f"{label}: malformed statistics row: {ln.strip()}")
        k = _okey(*m.groups())
        if k not in order:
            raise ReportError(f"{label}: unexpected grid point {fmt_point(*k)} in the statistics table "
                              "(not in the Population line)")
        rm = re.fullmatch(_GAIN_MC_ROW_RX, c[1])
        if not rm:
            raise ReportError(f"{label}: malformed statistics row label '{c[1]}' "
                              "(expected '<row> (>= <bound> <unit>)')")
        rk, bval, bunit = rm.group(1), float(rm.group(2)), rm.group(3)
        if rk in stats.setdefault(k, {}):
            raise ReportError(f"{label}: duplicate {rk} row at {fmt_point(*k)}")
        try:
            valid, n = (int(x) for x in c[2].split("/"))
            mean_s, sigma_s, m3_s = c[3], c[4], c[5]
            mean, sigma, m3 = (float(x.split()[0]) for x in (mean_s, sigma_s, m3_s))
            unit = mean_s.split()[1]
            below, invalid = int(c[6]), int(c[7])
            fm = re.fullmatch(r"(\d+)/(\d+) = ([\d.]+)", c[8])
            fbelow, fden, frac = (int(fm.group(1)), int(fm.group(2)), float(fm.group(3))) if fm else (None, None, None)
        except (ValueError, IndexError):
            raise ReportError(f"{label}: malformed statistics row: {ln.strip()}") from None
        if unit != bunit or units.setdefault(rk, unit) != unit:
            raise ReportError(f"{label}: {rk} row at {fmt_point(*k)} carries unit '{unit}', but its bound is "
                              f"'{bunit}'" if unit != bunit else
                              f"{label}: {rk} rows disagree on their unit ({units[rk]} vs {unit})")
        if n != n_want:
            raise ReportError(f"{label}: fixed denominator violated at {fmt_point(*k)} ({rk}): the table states "
                              f"N={n}, the request is N={n_want} per corner")
        if valid + invalid != n or not (0 <= below <= valid) or invalid < 0:
            raise ReportError(f"{label}: inconsistent population at {fmt_point(*k)} ({rk}): valid {valid} + "
                              f"invalid {invalid} != N {n}, or below-bound {below} exceeds the valid count")
        for fname, fval in (("mean", mean), ("sigma", sigma), ("mean-3sigma", m3)):
            if not math.isfinite(fval):
                raise ReportError(f"{label}: non-finite {fname} at {fmt_point(*k)} ({rk}): the published "
                                  "statistics must be finite")
        if sigma <= 0:
            raise ReportError(f"{label}: non-positive sigma at {fmt_point(*k)} ({rk})")
        if abs(m3 - (mean - 3 * sigma)) > _GAIN_MC_DERIVE_TOL:
            raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({rk}): its mean-3sigma column "
                              "disagrees with its own mean and sigma")
        if (fbelow, fden) != (below + invalid, n) or abs(frac - (below + invalid) / n) > _GAIN_MC_FRAC_TOL:
            raise ReportError(f"{label}: inconsistent failure fraction at {fmt_point(*k)} ({rk}): it does not state "
                              f"({below} + {invalid})/{n} to the displayed precision")
        stats[k][rk] = {"n": n, "valid": valid, "mean": mean, "sigma": sigma, "mean3": m3,
                        "mean_text": mean_s, "sigma_text": sigma_s, "mean3_text": m3_s,
                        "below": below, "invalid": invalid, "fraction": frac, "fraction_text": c[8],
                        "bound": f">= {rm.group(2)} {bunit}", "bound_value": bval}
    missing = [(k, rk) for k in order for rk, _, _, _ in GAIN_MC_ROWS if rk not in stats.get(k, {})]
    if missing:
        k, rk = missing[0]
        raise ReportError(f"{label}: statistics table is missing {len(missing)} of {len(order) * 3} rows "
                          f"(first: {rk} at {fmt_point(*k)})")
    for k in order:  # one shared population per corner: the three rows must agree on it
        vs = {stats[k][rk]["valid"] for rk, _, _, _ in GAIN_MC_ROWS}
        iv = {stats[k][rk]["invalid"] for rk, _, _, _ in GAIN_MC_ROWS}
        if len(vs) > 1 or len(iv) > 1:
            raise ReportError(f"{label}: the three rows disagree on the shared population at {fmt_point(*k)} "
                              f"(valid {sorted(vs)}, invalid {sorted(iv)})")
    bounds = {}
    for rk, _, _, _ in GAIN_MC_ROWS:
        ref = stats[order[0]][rk]
        for k in order:
            if stats[k][rk]["bound"] != ref["bound"]:
                raise ReportError(f"{label}: {rk} bound at {fmt_point(*k)} is '{stats[k][rk]['bound']}', but the "
                                  f"shared bound at {fmt_point(*order[0])} is '{ref['bound']}': the ratified "
                                  "bound must be identical at every corner")
        bounds[rk] = {"text": ref["bound"], "value": ref["bound_value"], "unit": units[rk]}
    cov = coverage_of(set(order))
    cov["mc_samples_per_corner"] = n_want
    n_invalid = sum(next(iter({stats[k][rk]["invalid"] for rk, _, _, _ in GAIN_MC_ROWS})) for k in order)
    lim = common_limitations(text, prov, cov)
    lim += [f"temperature and supply are NOT sampled under mismatch: {t_c} C / {vdd} V only; the deterministic "
            "gain/GBW/PM record covers the full T x VDD grid (matched devices)"]
    if "typical passives" in text:
        lim.append("typical passives only (res_typical, mimcap_typical): no passive-corner axis and no passive "
                   "mismatch")
    lim += ["MOS mismatch only (sw_stat_mismatch = 1); resistor and capacitor mismatch not modelled",
            f"mean - 3 sigma is a descriptive figure from N = {n_want} per corner (sample sigma, ddof = 1): "
            "not a tail estimate and not a yield claim",
            f"N = {n_want} with zero observed failures does not establish yield: T1 signoff item 6 accepts only "
            "yield envelopes and stays unmet",
            "mismatch Monte Carlo of the three AC rows; the ratified bounds are graded by the deterministic "
            "record, and no bound is proposed or relaxed here"]
    if n_invalid:
        lim.append(f"the record discloses an invalid population ({n_invalid} samples across the corners): invalid "
                   "samples are excluded from the statistics and count as failures of all three rows, never "
                   "silently dropped")
    return {"prov": prov, "coverage": cov, "order": order, "stats": stats, "bounds": bounds,
            "n_per_point": n_want, "n_total": n_total, "seed": seed, "vary": vary, "t_c": t_c, "vdd": vdd,
            "n_invalid": n_invalid, "limitations": lim}


def gain_gbw_pm_mc_crosscheck(root: Path, rid: str, ex: dict, label: str) -> dict:
    """Re-derive every corner's statistics from the record's retained per-sample evidence
    (corners/<rid>/samples.csv) within the record's display precision, and re-count the below-bound
    samples against the record's stated bounds. Validates the identity (exactly the indices
    0..N-1 per corner, each once), the klt status and the finite valid metrics. Offline."""
    import csv
    import math
    import statistics
    rel = f"sim/{GAIN_MC_EXP}/corners/{rid}/samples.csv"
    p = root / rel
    if not p.is_file():
        raise ReportError(f"{label}: retained per-sample evidence is missing: {rel} (the record's statistics cannot "
                          "be cross-checked)")
    need = ["process", "temperature_c", "vdd_v", "sample_index", "seed", "mismatch_seed", "process_seed",
            "klt_status", "valid", "reason"] + [col for _, col, _, _ in GAIN_MC_ROWS]
    by: dict = {}
    idx_by: dict = {}
    seen = set()
    with p.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or any(f not in rd.fieldnames for f in need):
            raise ReportError(f"{label}: {rel} lacks the columns it needs")
        for r in rd:
            try:
                k = _okey(r["process"], r["temperature_c"], r["vdd_v"])
                idx = int(r["sample_index"])
                int(r["seed"]), int(r["mismatch_seed"]), int(r["process_seed"])
                valid = int(r["valid"])
                vals = {rk: float(r[col]) * scale for rk, col, scale, _ in GAIN_MC_ROWS}
            except (ValueError, KeyError, TypeError):
                raise ReportError(f"{label}: {rel}: malformed sample row {dict(r)}") from None
            if k not in ex["order"]:
                raise ReportError(f"{label}: {rel}: sample at unexpected grid point {fmt_point(*k)}")
            if (k, idx) in seen:
                raise ReportError(f"{label}: {rel}: duplicate sample index {idx} at {fmt_point(*k)}")
            if not 0 <= idx < ex["n_per_point"]:
                raise ReportError(f"{label}: {rel}: sample index {idx} at {fmt_point(*k)} is outside the declared "
                                  f"identity 0..{ex['n_per_point'] - 1}")
            if valid not in (0, 1):
                raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} has invalid flag {valid}")
            if valid == 1:
                if r["klt_status"] != "pass":
                    raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} is marked valid but its klt "
                                      f"status is '{r['klt_status']}'")
                if not all(math.isfinite(v) for v in vals.values()):
                    raise ReportError(f"{label}: {rel}: sample {idx} at {fmt_point(*k)} carries a non-finite metric")
            elif not r["reason"].strip():
                raise ReportError(f"{label}: {rel}: invalid sample {idx} at {fmt_point(*k)} discloses no reason")
            seen.add((k, idx))
            idx_by.setdefault(k, set()).add(idx)
            if valid == 1:
                by.setdefault(k, []).append(vals)
    for k in ex["order"]:
        missing = sorted(set(range(ex["n_per_point"])) - idx_by.get(k, set()))
        if missing:
            raise ReportError(f"{label}: {rel}: {len(missing)} sample indices missing at {fmt_point(*k)} "
                              f"(first: {missing[0]}; the identity is exactly 0..{ex['n_per_point'] - 1}, each once)")
        valids = by.get(k, [])
        n_invalid = len(idx_by[k]) - len(valids)
        if len(valids) < 2:
            raise ReportError(f"{label}: {rel}: only {len(valids)} valid samples at {fmt_point(*k)}: the corner's "
                              "statistics cannot be re-derived")
        for rk, _, _, unit in GAIN_MC_ROWS:
            rec = ex["stats"][k][rk]
            col = [v[rk] for v in valids]
            below = sum(1 for x in col if x < rec["bound_value"])
            mean, sigma = statistics.fmean(col), statistics.stdev(col)
            tol = _GAIN_MC_TOL[unit]
            for f, fname in (("mean", "mean"), ("sigma", "sigma"), ("mean3", "mean-3sigma")):
                if not math.isfinite(rec[f]):
                    raise ReportError(f"{label}: non-finite {fname} at {fmt_point(*k)} ({rk}): the published "
                                      "statistics must be finite")
            bad = []
            if len(valids) != rec["valid"]:
                bad.append("valid count")
            if n_invalid != rec["invalid"]:
                bad.append("invalid count")
            if abs(rec["mean"] - mean) > tol:
                bad.append("mean")
            if abs(rec["sigma"] - sigma) > tol:
                bad.append("sigma")
            if abs(rec["mean3"] - (mean - 3 * sigma)) > tol:
                bad.append("mean-3sigma")
            if below != rec["below"]:
                bad.append("below-bound count")
            if abs(rec["fraction"] - (below + n_invalid) / ex["n_per_point"]) > _GAIN_MC_FRAC_TOL:
                bad.append("failure fraction")
            if bad:
                raise ReportError(f"{label}: inconsistent summary at {fmt_point(*k)} ({rk}): the record's "
                                  f"{', '.join(bad)} disagree(s) with the retained evidence {rel}")
    return {"path": rel, "sha256": sha256_file(p), "samples": len(seen), "points": len(idx_by)}
