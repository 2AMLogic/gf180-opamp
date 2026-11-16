#!/usr/bin/env python3
"""Shared library of the frequency-domain rejection benches (issue #337).

The record-writing helpers and table emitters the CMRR, PSRR and
input-common-mode benches emit their evidence with. They lived in
`sim/cmrr/run_cmrr.py` since issue #39, which forced the psrr and
input-common-mode drivers to load the CMRR driver as a library to reach
generic plumbing; they are bench-agnostic REJECTION-bench machinery (not
cross-experiment harness material), so they live here rather than in
`harness.py`.

Hoisted verbatim from `run_cmrr.py` (per-symbol diffs in the #337 PR). The
corner axes, the nominal point and the point-formatting helpers are sourced
exactly as the CMRR driver sources them -- the gain driver and its config,
the one source of the 45-point grid every rejection bench runs -- so the
emitted record text is unchanged. `load_sibling` itself is retained for
MEASUREMENT reuse (issue #68); what stopped is benches reaching into another
bench for plumbing.

Type annotations naming `Key`, `Rejection` and `LocalRun` refer to the
drivers' types (they are duck-typed at runtime and never evaluated, per
`from __future__ import annotations`).
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))

from harness import (  # noqa: E402
    Pdk,
    db,
    load_dut_text,
    load_sibling,
    remote_of,
    state_phrase,
)

# The grid axes, the nominal point and the point-formatting helpers come from
# the gain driver, as in every rejection bench (one source: the gain config).
G = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")
CORNERS = G.CORNERS
NOMINAL = G.NOMINAL
fmt_key = G.fmt_key
point_stem = G.point_stem

SPOT_HZ = (1e3, 1e4, 1e5, 1e6)

# --------------------------------------------------------------------------
# Reported figures and worst case
# --------------------------------------------------------------------------

FIGURES = (("dc", "DC plateau (0.1-1 Hz)"), *((f"{f:g}", f"{f / 1e3:g} kHz" if f < 1e6 else f"{f / 1e6:g} MHz") for f in SPOT_HZ),
           ("fu", "at f_u (differential unity gain)"))


def worst_case(values: dict[Key, Rejection], which: str) -> tuple[float, Key | None, int]:
    """(lowest value, its corner, number of points without the figure)."""
    items = [(r.figure(which), k) for k, r in values.items() if math.isfinite(r.figure(which))]
    missing = len(values) - len(items)
    if not items:
        return float("nan"), None, missing
    v, k = min(items)
    return v, k, missing


def fmt_db(v: float, lb: bool = False) -> str:
    if not math.isfinite(v):
        return "n/a"
    return (">= " if lb else "") + f"{v:.2f}"


def klt_pdk_version(report: dict) -> str:
    return ((report.get("provenance") or {}).get("pdk") or {}).get("version", "n/a")


def pdk_lines(pdk: Pdk, reports: dict[str, dict]) -> list[str]:
    """PDK provenance, stating exactly what each source does and does not say."""
    client = sorted({f"{klt_pdk_version(r)} ({((r.get('provenance') or {}).get('pdk') or {}).get('source', '?')})"
                     for r in reports.values()})
    return [
        f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (harness `find_pdk`, via {pdk.source}); the "
        "local single units are pinned to it (`models.pdk_root`).",
        f"  - klt's `provenance.pdk` and `environment.models_lib_sha256` in the grid reports are resolved on the "
        f"SUBMITTING CLIENT ({'; '.join(client)}), not on the fleet runner, which uses its own install and (klt "
        "0.5.0) does not report its revision. The fleet library is tied to the pinned revision indirectly: the "
        "local pinned units reproduce the grid's nominal point, and the grid's open-loop response reproduces the "
        "committed gain-bench record (see Cross-checks).",
    ]


def execution_lines(reports: dict[str, dict], walls: dict[str, float]) -> list[str]:
    L = []
    for mode, rep in reports.items():
        r = remote_of(rep)
        env = rep.get("environment") or {}
        if r:
            L.append(f"  - `{mode}`: backend `{r.get('provider')}`, job `{r.get('job_id')}`, "
                     f"{'Spot ' if r.get('spot') else ''}{r.get('instance_type', '')}, {state_phrase(r)}, "
                     f"runner klt `{r.get('runner_klt_version')}` vs client `{r.get('client_klt_version')}` "
                     f"(compatibility `{r.get('runner_compatibility')}`); engine `{env.get('engine')} "
                     f"{env.get('engine_version')}`; wall {walls.get(mode, 0):.0f} s")
        else:
            L.append(f"  - `{mode}`: `klt sim` local backend (no `environment.remote`); engine "
                     f"`{env.get('engine')} {env.get('engine_version')}`; wall {walls.get(mode, 0):.0f} s")
    return L


# --------------------------------------------------------------------------
# Evidence writing (shared)
# --------------------------------------------------------------------------


def save_mode_point(corners_dir: Path, k: Key, mode: str, a: dict, cols: tuple[str, ...]) -> None:
    stem = f"{point_stem(k)}.{mode}"
    shutil.copyfile(a["log"], corners_dir / f"{stem}.log")
    if a.get("deck"):
        shutil.copyfile(a["deck"], corners_dir / f"{stem}.cir")
    save_mode_point_vec(corners_dir / f"{stem}.dat", a["vec"], cols)


SAVED_VECTORS = ("v(vout)", "v(vinp)", "v(vinn)", "v(vdd)", "v(vss)")


def save_local_runs(cdir: Path, runs: list[LocalRun]) -> None:
    """Log + klt deck of every local unit, and its node phasors as `.dat`
    (the same layout as the grid points; the full rawfile is not kept)."""
    cdir.mkdir(parents=True, exist_ok=True)
    for r in runs:
        for kind in ("log", "deck"):
            src = r.files.get(kind)
            if src and Path(src).is_file():
                shutil.copyfile(src, cdir / f"{r.name}.{'cir' if kind == 'deck' else 'log'}")
        if r.vec is not None:
            cols = tuple(c for c in SAVED_VECTORS if c in r.vec)
            save_mode_point_vec(cdir / f"{r.name}.dat", r.vec, cols)


def save_mode_point_vec(path: Path, vec: dict, cols: tuple[str, ...]) -> None:
    data = [vec["frequency"].real]
    hdr = ["freq_hz"]
    for c in cols:
        data += [vec[c].real, vec[c].imag]
        hdr += [f"re{c}", f"im{c}"]
    np.savetxt(path, np.column_stack(data), fmt="%.12e", header=" ".join(hdr))


def request_comment(req: dict) -> list[str]:
    return ["* " + ln for ln in json.dumps({k: v for k, v in req.items() if k != "netlist"}, indent=1).splitlines()]


def write_snapshot(path: Path, record: str, issue: str, testbench: Path, reqs: dict[str, dict], deck0: str) -> None:
    dut_text = load_dut_text()
    lines = [f"* netlist snapshot for record {record} ({issue})",
             "* Reproduces the measured design: DUT contents, testbench, conditions."]
    for mode, req in reqs.items():
        lines += [f"* ---- conditions: klt sim request, excitation `{mode}` (grid) ----", *request_comment(req), ""]
    lines += [
        "* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised (file opamp_two_stage.dut.spice) ----",
        dut_text,
        f"* ---- testbench: {testbench.relative_to(REPO_ROOT)} (verbatim; the driver rewrites only its .param line) ----",
        testbench.read_text(),
        "* ---- klt-generated deck of the nominal point, first excitation (corner.cir) ----",
        *("* | " + ln for ln in deck0.splitlines()),
        "",
    ]
    path.write_text("\n".join(lines))


def build_plots_rejection(freq_by_key: dict[Key, np.ndarray], curves: dict[str, dict[Key, np.ndarray]],
                          ad_nom: np.ndarray | None, errs_nom: dict[str, np.ndarray], plot_dir: Path,
                          ylabel: str) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for name, cur in curves.items():
        fig, ax = plt.subplots(figsize=(8, 5))
        for k, c in cur.items():
            ax.semilogx(freq_by_key[k], c, lw=0.6, alpha=0.5, color="0.6")
        for corner in CORNERS:
            k = (corner, 27.0, 3.30)
            if k in cur:
                ax.semilogx(freq_by_key[k], cur[k], lw=1.3, label=f"{corner} 27C 3.30V")
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(f"{name} (dB)")
        ax.set_title(f"{name}: all 45 points (grey), 27 C / 3.30 V corners (colour)")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fn = f"{name.lower().replace('+', 'plus').replace('-', 'minus')}-all45.png"
        fig.savefig(plot_dir / fn, dpi=110)
        plt.close(fig)
        out.append(fn)
    if ad_nom is not None:
        fig, ax = plt.subplots(figsize=(8, 5))
        f = freq_by_key[NOMINAL]
        ax.semilogx(f, db(ad_nom), label="|Ad|")
        for label, e in errs_nom.items():
            ax.semilogx(f, e, label=label)
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel(ylabel)
        ax.set_title("Transfers at typical / 27 C / 3.30 V")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(plot_dir / "transfers-nominal.png", dpi=110)
        plt.close(fig)
        out.append("transfers-nominal.png")
    return out


# --------------------------------------------------------------------------
# Table emitters
# --------------------------------------------------------------------------

def figure_table(add, values: dict[Key, Rejection], title: str) -> None:
    add(f"| point | {title} DC | " + " | ".join(lbl for w, lbl in FIGURES[1:-1]) + " | at f_u | f_u (MHz) | note |")
    add("|---|---|" + "---|" * (len(FIGURES) - 2) + "---|---|---|")
    for k in sorted(values, key=lambda k: (CORNERS.index(k[0]), k[2], k[1])):
        r = values[k]
        note = []
        if not r.plateau_ok:
            note.append(f"no plateau (spread {r.plateau_spread_db:.3f} dB); 0.1 Hz value {r.low_db:.2f} dB")
        if r.lower_bound:
            note.append("lower bound (numerical floor)")
        add(f"| {fmt_key(k)} | {fmt_db(r.dc_db, r.lower_bound)} | "
            + " | ".join(fmt_db(r.spot_db[f], r.lower_bound) for f in SPOT_HZ)
            + f" | {fmt_db(r.at_fu_db, r.lower_bound)} | {r.fu_hz / 1e6:.3f} | {'; '.join(note)} |")


def worst_table(add, values: dict[Key, Rejection], name: str) -> None:
    add(f"| {name} figure | worst (lowest) value (dB) | worst-case corner (process / T / VDD) | best (dB) | points without the figure |")
    add("|---|---|---|---|---|")
    for which, lbl in FIGURES:
        v, k, miss = worst_case(values, which)
        hi = max((r.figure(which) for r in values.values() if math.isfinite(r.figure(which))), default=float("nan"))
        add(f"| {lbl} | **{fmt_db(v)}** | {fmt_key(k) if k else 'n/a'} | {fmt_db(hi)} | {miss} |")
