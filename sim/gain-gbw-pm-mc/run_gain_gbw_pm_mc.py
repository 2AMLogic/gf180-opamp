#!/usr/bin/env python3
"""Mismatch Monte Carlo of open-loop gain, GBW and phase margin (issue #129).

Characterization machinery for the statistical spread of the three AC rows
the deterministic driver (`sim/gain-gbw-pm/run_gain_gbw_pm.py`) measures on
matched devices. It measures; it judges nothing new: the bounds are the gain
driver's ratified ones (gain >= 60 dB, GBW >= 10 MHz into 2 pF, PM >= 60 deg),
a sample exactly on a bound passes, and no bound is proposed or relaxed here.

Reuse, not a second metric definition
-------------------------------------
The testbench IS the gain bench (`sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice`,
materialised and guarded by the gain driver, DUT from the committed export),
with ONE line added after the `design.ngspice` include: `.param
sw_stat_mismatch=1`. The request is the gain driver's `ac_request` (same
corner bundles, `.ac` sweep, VCM = VDD/2, spice `.meas` cross-checks) plus a
`monte_carlo` block and a control-language tail that prints the complex
`v(vout)`, `v(vinp)`, `v(vinn)` vectors into each sample's ngspice log. Each
sample is then reduced by the gain driver's own `differential_gain` and
`extract_metrics` (actual differential phasor, plateau / polarity, unwrapped
phase, first descending 0 dB crossing, re-crossings invalid) and its `.meas`
cross-check. The sibling driver is loaded through `harness.load_sibling`.

Why the log, not the rawfile: the fleet runner retains per-sample logs for
Monte Carlo AC requests (`sim/cmrr-mc/` probes); a per-sample rawfile has not
been demonstrated. `--probe` is the bounded N=3 check that the backend returns
the vectors this runner needs.

Population (the campaign; see README "Campaign gate")
-----------------------------------------------------
Five MOS corners (typical, ff, ss, fs, sf) x 27 C x 3.30 V, VCM = VDD/2,
CL = 2 pF, ibias = 10 uA, typical passives, `monte_carlo = {n: 300, seed: 45,
vary: "mismatch"}`: ONE multi-corner `klt sim` request (1500 units). Which
backend executes it is klt's decision (`--backend`, `$KLT_SIM_BACKEND`); on
a dispatch worker that is the Spot batch fleet. This script never launches
ngspice itself, never loops over a grid, refuses `--backend local` for any
Monte Carlo request, and never falls back to a local grid when a submit
fails: it stops with the error and writes nothing.

Campaign gate (#42): the full submission requires `--revised-dut-sha256`
(the wrapper-normalised sha256 of the revised DUT) and `--deterministic-record`
(a committed `sim/gain-gbw-pm/records/<rid>.md` that cites that hash). The
pin must not be the pre-redesign DUT, must equal the committed export, and
the record must exist and cite it -- all checked before any record path is
claimed or anything is submitted. `--smoke` and `--probe` are diagnostic
only: they write nothing into the repository and are never evidence.

    python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py --smoke   # local single units, no files
    python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py --probe   # N=3 MC transport probe (batch), no files
    python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py \\
        --revised-dut-sha256 <sha256> --deterministic-record <rid>   # full campaign + record
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
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "sim"))

from harness import (  # noqa: E402
    KltError,
    Pdk,
    allocate_record_id,
    batch_block,
    claim_record_paths,
    find_pdk,
    klt_version,
    load_dut_text,
    load_sibling,
    ngspice_version,
    remote_of,
    run_klt,
    run_klt_retrying,
    sanitise_report,
)

# The deterministic gain driver owns the bench, its guards, the request shape,
# the extraction and the ratified bounds; one shared module copy.
G = load_sibling("gain_gbw_pm_driver", "sim/gain-gbw-pm/run_gain_gbw_pm.py")

Key = tuple  # (process, temperature_c, supply_v)

# --------------------------------------------------------------------------
# Population and campaign gate
# --------------------------------------------------------------------------

CORNERS = list(G.CORNERS)  # typical, ff, ss, fs, sf
TEMPS_C = [27.0]
SUPPLIES_V = [3.30]
MC_N = 300
MC_SEED = 45
MC_VARY = "mismatch"
PROBE_N = 3
PROBE_POINT: Key = ("typical", 27.0, 3.30)
#: Bias and load the population is defined at; the guard checks the bench
#: carries exactly these (the gain bench's own values).
IBIAS_LINE = "Ibias vdd ibias dc 10u"
CL_LINE = "CL vout 0 2p"

#: Wrapper-normalised sha256 of the committed export when this runner was
#: written -- the pre-#42 DUT whose PM shortfall #42 is redesigning. A full
#: campaign pinned to it is refused: the N=300 campaign waits for the revised
#: DUT (issue #129 "Campaign sequencing and scope").
PRE_REDESIGN_DUT_SHA256 = "81fbd914f8254a49af9eaaa819d41ca88dde49cd73128602e1bfb921dec07860"
GAIN_RECORDS = G.HERE / "records"

#: Rows: the gain driver's ratified bounds, never edited here.
ROWS = tuple((rid, label, attr, bound, unit) for rid, label, attr, bound, unit in G.ROWS)

#: Excitation: the gain bench drives vinp with `ac 1`; vinn is AC-isolated by
#: Lfb/Cfb, so the actual differential phasor must be 1 V at every frequency.
DIFF_EXC_TOL_V = 1e-6


class GateError(RuntimeError):
    """A full-campaign request refused before anything is claimed or submitted."""


def dut_sha256(dut_text: str) -> str:
    return hashlib.sha256(dut_text.encode()).hexdigest()


def campaign_gate(pin: str | None, det_record: str | None, *, current_sha: str,
                  pre_sha: str = PRE_REDESIGN_DUT_SHA256, records_dir: Path = GAIN_RECORDS) -> list[str]:
    """Reasons the full N=300 campaign may NOT be submitted (empty = go).

    Pure apart from reading the cited deterministic record; never contacts
    the forge. Checks, in order: a pin is given and is a sha256; it is not the
    pre-#42 DUT; it equals the committed export (a stale pin or an unrevised
    export is refused); the deterministic gain-gbw-pm record exists and cites
    the pinned DUT hash.
    """
    errs: list[str] = []
    if not pin:
        return ["full campaign requires --revised-dut-sha256 (the #42 revised-DUT pin); "
                "use --probe / --smoke for diagnostics"]
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        return [f"--revised-dut-sha256 {pin!r} is not a lowercase sha256 hex digest"]
    if pin == pre_sha:
        errs.append("the pin is the pre-redesign DUT; the campaign is deferred until #42 delivers the revised DUT")
    if pin != current_sha:
        errs.append(f"the committed export hashes to {current_sha[:16]}..., not the pin {pin[:16]}... "
                    "(stale pin, or the revised DUT is not on this checkout)")
    if not det_record:
        errs.append("full campaign requires --deterministic-record (the revised DUT's deterministic "
                    "sim/gain-gbw-pm record)")
    elif not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{7}", det_record):
        errs.append(f"--deterministic-record {det_record!r} is not a record id")
    else:
        p = records_dir / f"{det_record}.md"
        if not p.is_file():
            errs.append(f"deterministic record {p.name} does not exist in {records_dir}")
        elif f"normalised sha256 `{pin}`" not in p.read_text():
            errs.append(f"deterministic record {det_record} does not cite the pinned DUT sha256")
    return errs


# --------------------------------------------------------------------------
# Testbench: the gain bench + the mismatch switch
# --------------------------------------------------------------------------

_DESIGN_INC_RE = re.compile(r"^\.include\s+['\"]?[^'\"\n]*design\.ngspice['\"]?[ \t]*$", re.M | re.I)
_SWITCH_RE = re.compile(r"^\.param\s+sw_stat_mismatch\s*=\s*(\S+)[ \t]*$", re.I)


def inject_mismatch(tb_text: str, mismatch: int) -> str:
    """The bench with `.param sw_stat_mismatch=<0|1>` right after the
    `design.ngspice` include (after it, so it overrides the PDK default 0)."""
    if mismatch not in (0, 1):
        raise ValueError(f"mismatch switch must be 0 or 1, got {mismatch!r}")
    out, n = _DESIGN_INC_RE.subn(lambda m: m.group(0) + f"\n.param sw_stat_mismatch={mismatch}", tb_text, count=0)
    if n != 1:
        raise RuntimeError(f"expected exactly one design.ngspice include in the bench, found {n}")
    return out


def _code(text: str) -> list[str]:
    logical: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("+") and logical:
            logical[-1] += " " + raw[1:].strip()
        else:
            logical.append(raw.strip())
    return [" ".join(ln.split()) for ln in logical if ln and not ln.startswith("*")]


def guard_mc_testbench(text: str, gain_tb_text: str | None = None) -> list[str]:
    """Reasons `text` is no longer the gain bench plus the mismatch switch.

    The gain driver's source guard (DUT + PDK includes, no hand-declared
    device, one `opamp_two_stage` instance, circuit body), plus: exactly one
    `.param sw_stat_mismatch` line with value 0 or 1, AFTER the
    `design.ngspice` include; `sw_stat_global` untouched; every other code
    line identical, in order, to the committed gain bench; the population's
    bias and load lines present.
    """
    gain = G.TESTBENCH.read_text() if gain_tb_text is None else gain_tb_text
    errs = list(G.guard_testbench(text))
    code = _code(text)
    sw = [i for i, ln in enumerate(code) if re.match(r"\.param\s+sw_stat_mismatch\b", ln, re.I)]
    if len(sw) != 1:
        errs.append(f"expected exactly one `.param sw_stat_mismatch` line, found {len(sw)}")
    else:
        m = _SWITCH_RE.match(code[sw[0]])
        if not m or m.group(1) not in ("0", "1"):
            errs.append(f"`{code[sw[0]]}`: the switch must be exactly sw_stat_mismatch=0 or =1")
        inc = [i for i, ln in enumerate(code) if re.match(r"\.include\s+['\"]?[^'\"]*design\.ngspice", ln, re.I)]
        if not inc or sw[0] < inc[0]:
            errs.append("`.param sw_stat_mismatch` precedes the design.ngspice include (the PDK default 0 would win)")
    if any(re.search(r"\bsw_stat_global\b", ln, re.I) for ln in code):
        errs.append("testbench must not touch sw_stat_global (global spread is the corners' job)")
    rest = [ln for i, ln in enumerate(code) if i not in sw]
    if rest != _code(gain):
        errs.append("circuit differs from sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice "
                    "(only the sw_stat_mismatch line may be added)")
    for need in (IBIAS_LINE, CL_LINE):
        if need not in code:
            errs.append(f"bench lost `{need}` (the population's bias/load)")
    return errs


def materialise(work: Path, pdk: Pdk, *, mismatch: int = 1) -> Path:
    """Stage the gain bench via the gain driver (its bench + DUT guards run),
    add the mismatch switch, guard the result, write `tb.spice`."""
    committed = inject_mismatch(G.TESTBENCH.read_text(), mismatch)
    errs = guard_mc_testbench(committed)
    if errs:
        raise RuntimeError("Monte Carlo testbench guard failed:\n  " + "\n  ".join(errs))
    path = G.materialise(work, pdk)
    path.write_text(inject_mismatch(path.read_text(), mismatch))
    return path


# --------------------------------------------------------------------------
# Request: the gain driver's AC request + monte_carlo + the vector-print tail
# --------------------------------------------------------------------------

VEC_COLS = ("gmc_fr", "gmc_vo_re", "gmc_vo_im", "gmc_vp_re", "gmc_vp_im", "gmc_vn_re", "gmc_vn_im")
BEGIN, END = "GMC_VECTORS_BEGIN", "GMC_VECTORS_END"
N_FREQ = int(round(G.AC_PPD * math.log10(G.AC_FSTOP / G.AC_FSTART))) + 1


def analysis_tail() -> str:
    """Control commands klt places verbatim after its `ac <args>` line: print
    the real/imag parts of the three node phasors, 15 significant digits, one
    table without page breaks, between two markers."""
    return "\n".join([
        "set width=4000",
        "set nobreak",
        "set numdgt=15",
        "let gmc_fr = real(frequency)",
        "let gmc_vo_re = real(v(vout))",
        "let gmc_vo_im = imag(v(vout))",
        "let gmc_vp_re = real(v(vinp))",
        "let gmc_vp_im = imag(v(vinp))",
        "let gmc_vn_re = real(v(vinn))",
        "let gmc_vn_im = imag(v(vinn))",
        f"echo {BEGIN}",
        "print " + " ".join(VEC_COLS),
        f"echo {END}",
    ])


def mc_request(netlist: Path, pdk: Pdk, corners, temps, supplies, *, n: int | None,
               seed: int = MC_SEED, vary: str = MC_VARY, local: bool = False) -> dict:
    req = G.ac_request(netlist, pdk, corners, temps, supplies)
    req["analysis"]["args"] = req["analysis"]["args"] + "\n" + analysis_tail()
    # Logs carry the vectors; no per-sample rawfile (1500 of them) is needed.
    req["options"] = {"timeout_s": 300, "keep_artifacts": True, "waveforms": False}
    if n is not None:
        req["monte_carlo"] = {"n": int(n), "seed": int(seed), "vary": vary}
    if local:
        req["models"]["pdk_root"] = str(pdk.path.parent)
    return req


# --------------------------------------------------------------------------
# Log parsing (pure; unit-tested offline)
# --------------------------------------------------------------------------

_NUMTOK = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$|^[-+]?(?:nan|inf(?:inity)?)$", re.I)


def parse_log_vectors(log: str, n_freq: int = N_FREQ) -> dict[str, np.ndarray]:
    """{frequency, v(vout), v(vinp), v(vinn)} complex arrays from one log.

    Strict, like the gain driver's rawfile parser: exactly one marked block,
    the expected header, consecutive indices 0..n_freq-1, nine numeric fields
    per row, the scale column equal to `gmc_fr`, every value finite. Anything
    else raises ValueError (the sample is then invalid, never filtered).
    """
    nb, ne = log.count(BEGIN), log.count(END)
    if nb != 1 or ne != 1:
        raise ValueError(f"expected one vector block, found {nb} begin / {ne} end markers")
    body = log.split(BEGIN, 1)[1].split(END, 1)[0]
    header = None
    rows: list[list[str]] = []
    for ln in body.splitlines():
        toks = ln.split()
        if not toks or set(ln.strip()) <= {"-"}:
            continue
        if toks[0] == "Index":
            if header is not None:
                raise ValueError("vector table header repeated (page break)")
            header = toks
            continue
        if header is None:
            continue  # ngspice's title / analysis banner lines
        rows.append(toks)
    want = ["Index", "frequency", *VEC_COLS]
    if header != want:
        raise ValueError(f"vector table header {header} != {want}")
    if len(rows) != n_freq:
        raise ValueError(f"vector table has {len(rows)} rows, expected {n_freq}")
    data = np.empty((n_freq, len(VEC_COLS) + 1))
    for i, toks in enumerate(rows):
        if len(toks) != len(want):
            raise ValueError(f"row {i}: {len(toks)} fields, expected {len(want)}")
        if toks[0] != str(i):
            raise ValueError(f"row {i}: index {toks[0]!r} (rows missing or out of order)")
        for t in toks[1:]:
            if not _NUMTOK.match(t):
                raise ValueError(f"row {i}: non-numeric field {t!r}")
        data[i] = [float(t) for t in toks[1:]]
    if not np.all(np.isfinite(data)):
        raise ValueError("vector table contains non-finite values")
    if np.any(data[:, 0] != data[:, 1]):
        raise ValueError("scale column differs from gmc_fr")
    return {
        "frequency": data[:, 0].astype(complex),
        "v(vout)": data[:, 2] + 1j * data[:, 3],
        "v(vinp)": data[:, 4] + 1j * data[:, 5],
        "v(vinn)": data[:, 6] + 1j * data[:, 7],
    }


# --------------------------------------------------------------------------
# Samples, identity and validation (pure; unit-tested offline)
# --------------------------------------------------------------------------

SEED_FIELDS = ("seed", "mismatch_seed", "process_seed")


@dataclass
class Sample:
    key: Key
    index: int
    seeds: dict
    status: str
    log_ref: str | None
    reason: str = ""  # empty = valid
    metrics: object | None = None  # G.Metrics when the vectors were extracted

    @property
    def valid(self) -> bool:
        return not self.reason

    def value(self, attr: str) -> float:
        return getattr(self.metrics, attr) if self.metrics is not None else float("nan")


def evaluate_vectors(vec: dict[str, np.ndarray], klt_meas: dict) -> tuple[object | None, str]:
    """(metrics, reason) of one sample's vectors via the gain driver's
    extraction; reason empty when valid."""
    try:
        freq, h, vdiff = G.differential_gain(vec)
    except ValueError as exc:
        return None, f"malformed data: {exc}"
    err = float(np.max(np.abs(vdiff - 1.0)))
    if not err <= DIFF_EXC_TOL_V:
        return None, f"differential excitation off: max |vdiff - 1| = {err:.3g} V"
    m = G.extract_metrics(freq, h)
    if not m.valid:
        return m, m.reason
    bad = G.crosscheck(PROBE_POINT, klt_meas, vec["v(vout)"][0], m)
    if bad:
        return m, "ngspice .meas cross-check disagrees: " + "; ".join(b.split(": ", 1)[-1] for b in bad)
    return m, ""


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def extract(report: dict, want: list[Key], n_expected: int, *, read_log=None) -> tuple[dict[Key, list[Sample]], list[str]]:
    """Samples per expected point from a klt Monte Carlo report.

    Identity: every expected point must carry exactly the sample indices
    0..n_expected-1, each once, each with all three klt seeds. A report entry
    with an unexpected point, a malformed or out-of-range index, or a repeated
    index is a problem and contributes NO sample (it cannot inflate the
    denominator). A missing index becomes an explicit invalid row. Every
    identified sample with no log, unparseable vectors, a failed check or a
    simulator error is an explicit invalid row with its reason -- nothing is
    dropped silently. Returns (samples sorted by index, problems).
    """
    read_log = read_log or (lambda p: Path(p).read_text())
    problems: list[str] = []
    out: dict[Key, dict[int, Sample]] = {k: {} for k in want}
    for c in report.get("corners", []):
        cid = str(c.get("corner_id", "?"))
        try:
            k = G.point_key(c)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{cid}: unparseable corner identity")
            continue
        if k not in out:
            problems.append(f"{cid}: unexpected grid point {G.fmt_key(k)}")
            continue
        mc = c.get("monte_carlo")
        idx = mc.get("sample_index") if isinstance(mc, dict) else None
        if not _is_int(idx):
            problems.append(f"{cid}: malformed sample identity (sample_index {idx!r})")
            continue
        if not 0 <= idx < n_expected:
            problems.append(f"{cid}: sample index {idx} outside 0..{n_expected - 1}")
            continue
        if idx in out[k]:
            problems.append(f"{G.fmt_key(k)}: duplicate sample index {idx}")
            continue
        seeds = {f: mc.get(f) for f in SEED_FIELDS}
        status = str(c.get("status", "?"))
        logp = (c.get("artifacts") or {}).get("log")
        s = Sample(k, idx, seeds, status, Path(logp).name if logp else None)
        out[k][idx] = s
        missing_seeds = [f for f, v in seeds.items() if not _is_int(v)]
        if missing_seeds:
            s.reason = f"seeds not retained: {', '.join(missing_seeds)}"
            continue
        try:
            log = read_log(logp) if logp else None
        except OSError:
            log = None
        if not log:
            diag = "; ".join(str(d.get("message", ""))[:160] for d in c.get("diagnostics", []) or []
                             if d.get("severity") == "error")
            s.reason = f"simulation failed: no ngspice log retained (status {status}{'; ' + diag if diag else ''})"
            continue
        try:
            vec = parse_log_vectors(log)
        except ValueError as exc:
            diag = "; ".join(str(d.get("message", ""))[:160] for d in c.get("diagnostics", []) or []
                             if d.get("severity") == "error")
            s.reason = f"malformed data: {exc}" + (f" (simulator: {diag})" if diag else "")
            continue
        meas = {x.get("name"): x.get("value") for x in c.get("measurements", []) or []}
        s.metrics, s.reason = evaluate_vectors(vec, meas)
    final: dict[Key, list[Sample]] = {}
    for k in want:
        for i in range(n_expected):
            if i not in out[k]:
                out[k][i] = Sample(k, i, {f: None for f in SEED_FIELDS}, "absent", None,
                                   "sample missing from the report")
        final[k] = [out[k][i] for i in range(n_expected)]
        bad = [s for s in final[k] if not s.valid]
        if bad:
            problems.append(f"{G.fmt_key(k)}: {len(bad)}/{n_expected} samples invalid "
                            f"(first: #{bad[0].index} {bad[0].reason[:120]})")
    return final, problems


# --------------------------------------------------------------------------
# Statistics and failure accounting (pure; unit-tested offline)
# --------------------------------------------------------------------------


@dataclass
class RowStats:
    row: str
    unit: str
    bound: float
    n_expected: int  # the fixed denominator
    n_valid: int
    mean: float  # valid-only descriptive statistics
    sigma: float  # sample standard deviation, ddof = 1
    mean_3s: float  # mean - 3 sigma
    n_below: int  # valid samples numerically below the bound
    n_invalid: int  # invalid or missing samples: failures of every row

    @property
    def n_fail(self) -> int:
        return self.n_below + self.n_invalid

    @property
    def fail_fraction(self) -> float:
        return self.n_fail / self.n_expected

    @property
    def complete(self) -> bool:
        return self.n_valid == self.n_expected


def row_stats(samples: list[Sample], n_expected: int) -> dict[str, RowStats]:
    """Per-row statistics of one point. Denominator is `n_expected`, always;
    invalid and missing samples count as failures of all three rows."""
    if len(samples) != n_expected or sorted(s.index for s in samples) != list(range(n_expected)):
        raise ValueError(f"sample set is not exactly the indices 0..{n_expected - 1}")
    good = [s for s in samples if s.valid]
    out: dict[str, RowStats] = {}
    for rid, _label, attr, bound, unit in ROWS:
        xs = [s.value(attr) for s in good]
        n = len(xs)
        mean = statistics.fmean(xs) if n >= 1 else float("nan")
        sigma = statistics.stdev(xs) if n >= 2 else float("nan")
        below = sum(not G.point_passes(s.metrics, attr, bound) for s in good)
        n_invalid = n_expected - n
        out[rid] = RowStats(rid, unit, bound, n_expected, n, mean, sigma, mean - 3 * sigma, below, n_invalid)
    return out


def campaign_stats(samples: dict[Key, list[Sample]], n_expected: int) -> dict[Key, dict[str, RowStats]]:
    return {k: row_stats(v, n_expected) for k, v in samples.items()}


def campaign_complete(stats: dict[Key, dict[str, RowStats]], problems: list[str]) -> bool:
    """A successful statistical record: no identity/validity problem and every
    point has exactly `n_expected` valid samples."""
    return not problems and bool(stats) and all(r.complete for st in stats.values() for r in st.values())


def fmt_val(rid: str, x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x / 1e6:.3f}" if rid == "gbw" else f"{x:.2f}"


UNIT_LABEL = {"gain": "dB", "gbw": "MHz", "pm": "deg"}


def stats_table(stats: dict[Key, dict[str, RowStats]], *, diagnostic: bool) -> list[str]:
    """Markdown table; `diagnostic` labels valid-only figures of an
    incomplete set as such."""
    L = []
    if diagnostic:
        L.append("DIAGNOSTIC ONLY -- incomplete sample set; mean/sigma are over the valid samples "
                 "named in `valid`, NOT a campaign statistic.")
        L.append("")
    L.append("| point | row | valid / N | mean | sigma (ddof=1) | mean-3sigma | below bound | invalid | "
             "fail fraction (/N) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for k, st in stats.items():
        for rid, r in st.items():
            u = UNIT_LABEL[rid]
            L.append(f"| {G.fmt_key(k)} | {rid} (>= {fmt_val(rid, r.bound)} {u}) | {r.n_valid} / {r.n_expected} | "
                     f"{fmt_val(rid, r.mean)} {u} | {fmt_val(rid, r.sigma)} {u} | {fmt_val(rid, r.mean_3s)} {u} | "
                     f"{r.n_below} | {r.n_invalid} | {r.n_fail}/{r.n_expected} = {r.fail_fraction:.4f} |")
    return L


# --------------------------------------------------------------------------
# Running (one request; never a local grid)
# --------------------------------------------------------------------------


class BackendRefused(RuntimeError):
    pass


def check_backend(backend: str | None) -> None:
    if backend and backend.strip().lower() == "local":
        raise BackendRefused("--backend local is refused for a Monte Carlo request: a multi-unit grid never "
                             "runs on this host (use --smoke for local single units)")


def submit(tag: str, tb: Path, pdk: Pdk, corners, temps, supplies, n: int, args, work: Path) -> tuple[dict, dict, float]:
    """ONE `klt sim` request. A KltError propagates; nothing is retried on
    another backend."""
    check_backend(args.backend)
    req = mc_request(tb, pdk, corners, temps, supplies, n=n)
    req["batch"] = batch_block(args)
    units = len(corners) * len(temps) * len(supplies) * n
    print(f"  submitting `{tag}` ({units} units, one request)...", flush=True)
    t0 = time.time()
    rep = run_klt_retrying(req, work / tag / "out", args.backend, work / tag,
                           retries=args.batch_submit_retries, wait_s=args.batch_retry_wait_s)
    wall = time.time() - t0
    print(f"  `{tag}` done in {wall:.0f} s (job {remote_of(rep).get('job_id', 'local')})", flush=True)
    return rep, req, wall


def exec_line(rep: dict, wall: float) -> str:
    r = remote_of(rep)
    env = rep.get("environment") or {}
    eng = f"{env.get('engine', 'ngspice')} {env.get('engine_version', '')}".strip()
    if not r:
        return f"backend `local`; engine `{eng}`; wall {wall:.0f} s"
    return (f"backend `{r.get('provider')}`, job `{r.get('job_id')}`, {'Spot ' if r.get('spot') else ''}"
            f"{r.get('instance_type', '')}, state `{r.get('state')}`, runner klt `{r.get('runner_klt_version')}` "
            f"vs client `{r.get('client_klt_version')}`; engine `{eng}`; wall {wall:.0f} s")


# --------------------------------------------------------------------------
# Append-only evidence
# --------------------------------------------------------------------------

CSV_COLS = ["process", "temperature_c", "vdd_v", "sample_index", *SEED_FIELDS, "klt_status", "valid", "reason",
            "dc_gain_db", "gbw_hz", "pm_deg", "plateau_spread_db", "plateau_phase_deg", "n_crossings", "log"]


def write_samples_csv(path: Path, samples: dict[Key, list[Sample]]) -> None:
    with path.open("x", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for k, ss in samples.items():
            for s in ss:
                m = s.metrics
                vals = [repr(float(getattr(m, a))) if m is not None else ""
                        for a in ("dc_gain_db", "gbw_hz", "pm_deg", "plateau_spread_db", "plateau_phase_deg")]
                w.writerow([k[0], f"{k[1]:g}", f"{k[2]:.2f}", s.index, *(s.seeds.get(f) for f in SEED_FIELDS),
                            s.status, int(s.valid), s.reason, *vals, m.n_crossings if m is not None else "",
                            s.log_ref or ""])


def write_vectors(cdir: Path, samples: dict[Key, list[Sample]]) -> list[str]:
    """Per point, the extracted responses of every sample that produced
    vectors (`<point>.responses.npz`: freq, h dB, unwrapped phase)."""
    names = []
    for k, ss in samples.items():
        have = [s for s in ss if s.metrics is not None and getattr(s.metrics, "freq", np.array([])).size]
        if not have:
            continue
        p = cdir / f"{G.point_stem(k)}.responses.npz"
        if p.exists():
            raise FileExistsError(f"{p} exists; evidence is append-only")
        np.savez_compressed(p, freq=have[0].metrics.freq, sample_index=np.array([s.index for s in have]),
                            db=np.vstack([s.metrics.db for s in have]),
                            phase_deg=np.vstack([s.metrics.phase_deg for s in have]))
        names.append(p.name)
    return names


def write_snapshot(path: Path, record: str, req: dict, dut_text: str) -> None:
    r = json.loads(json.dumps(req))
    r["netlist"] = Path(r["netlist"]).name
    r.get("models", {}).pop("pdk_root", None)
    L = [f"* netlist snapshot for gain/GBW/PM Monte Carlo record {record} (issue #129)",
         "* ---- committed testbench (sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice) + mismatch switch ----"]
    L += [f"* | {ln}" for ln in inject_mismatch(G.TESTBENCH.read_text(), 1).splitlines()]
    L += [f"* ---- DUT include (wrapper-normalised export), sha256 {dut_sha256(dut_text)} ----"]
    L += [f"* | {ln}" for ln in dut_text.splitlines()]
    L += ["* ---- klt request ----"] + [f"* | {ln}" for ln in json.dumps(r, indent=2).splitlines()]
    with path.open("x") as fh:
        fh.write("\n".join(L) + "\n")


def build_record(*, record, stamp, pdk, ngspice, kver, rep, wall, stats, samples, dut_sha, det_record) -> str:
    L: list[str] = []
    add = L.append
    n_pts = len(stats)
    add(f"# Gain / GBW / PM mismatch Monte Carlo record `{record}`")
    add("")
    add(f"- **Date**: {stamp:%Y-%m-%d %H:%M} UTC; issue #129")
    add(f"- **DUT**: `design/netlist/opamp_two_stage.spice` (normalised sha256 `{dut_sha}`, the #42 revised-DUT "
        f"pin); deterministic evidence for the same DUT: `sim/gain-gbw-pm/records/{det_record}.md`; snapshot "
        f"`netlist-snapshots/{record}.spice`")
    add(f"- **PDK revision**: {pdk.variant}, open_pdks `{pdk.version}` (harness `find_pdk`, via {pdk.source})")
    add(f"- **Tools**: ngspice local `{ngspice}`, klt `{kver}`")
    add(f"- **Execution**: one `klt sim` Monte Carlo request: {exec_line(rep, wall)}")
    add(f"- **Population**: process {', '.join(CORNERS)}; 27 C; VDD 3.30 V (VCM = VDD/2); ibias = 10 uA; "
        f"CL = 2 pF; typical passives; `monte_carlo = {{n: {MC_N}, seed: {MC_SEED}, vary: \"{MC_VARY}\"}}` -> "
        f"{n_pts} x {MC_N} = {n_pts * MC_N} samples. MOS mismatch only (`sw_stat_mismatch=1`; `sw_stat_global` "
        "untouched). Temperature and supply are NOT sampled under mismatch.")
    add(f"- **environment.monte_carlo**: `{json.dumps((rep.get('environment') or {}).get('monte_carlo'), sort_keys=True)[:600]}`")
    add("")
    add("## Claim")
    add("")
    add("**Characterization evidence only.** Statistics against the ratified bounds of `spec/target-spec.md` "
        "(gain >= 60 dB, GBW >= 10 MHz, PM >= 60 deg; a sample on the bound passes). No bound is proposed or "
        "relaxed, and no yield claim is made: `manifests/` item 6 accepts only `yield` envelopes and stays unmet.")
    add("")
    add("## Statistics per corner")
    add("")
    L += stats_table(stats, diagnostic=False)
    add("")
    add("- mean-3sigma is a descriptive figure from N = 300 samples (sample sigma, ddof = 1); it is not a tail "
        "estimate and assumes nothing about normality. The failure fraction uses the fixed denominator N.")
    add("")
    add("## Method")
    add("")
    add("- Bench, guards, request shape and extraction are the deterministic gain driver's "
        "(`sim/gain-gbw-pm/run_gain_gbw_pm.py`): `differential_gain` (actual differential phasor) and "
        "`extract_metrics` (lowest-decade plateau and polarity, unwrapped phase, first descending 0 dB crossing "
        "interpolated in (log f, dB), any re-crossing invalid), plus its `.meas` cross-check.")
    add("- Per sample, the complex `v(vout)`, `v(vinp)`, `v(vinn)` vectors are printed into the ngspice log "
        "(15 significant digits) and parsed strictly; the differential excitation must be 1 V within "
        f"{DIFF_EXC_TOL_V:g} V at every frequency.")
    add(f"- Identity: exactly the indices 0..{MC_N - 1} per corner, each once, each with klt's seed, "
        "mismatch_seed and process_seed retained.")
    add("")
    add("## Reproduce / evidence")
    add("")
    add("```")
    add(f"python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py --revised-dut-sha256 {dut_sha} "
        f"--deterministic-record {det_record}")
    add("```")
    add("")
    add(f"- Per-sample table (seeds, status, reason, metrics, log name): `corners/{record}/samples.csv`; responses "
        f"`corners/{record}/*.responses.npz`; sanitised klt report `corners/{record}/klt-report.json`.")
    add(f"- Timestamp / author: {stamp:%Y-%m-%dT%H:%M:%SZ}, agent-builder (issue #129)")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--smoke", action="store_true",
                    help="diagnostic: local single units at the nominal point (mismatch off: parity with the gain "
                    "driver's rawfile path; mismatch on: one valid sample); writes nothing")
    ap.add_argument("--probe", action="store_true",
                    help=f"diagnostic: N={PROBE_N} Monte Carlo transport probe at typical/27C/3.30V on klt's "
                    "backend (batch on a dispatch worker); writes nothing into the repository")
    ap.add_argument("--revised-dut-sha256", default=None,
                    help="full campaign: the #42 revised DUT's wrapper-normalised sha256 (required)")
    ap.add_argument("--deterministic-record", default=None,
                    help="full campaign: the sim/gain-gbw-pm record id holding the revised DUT's deterministic "
                    "evidence (required; must cite the pinned sha256)")
    ap.add_argument("--backend", default=None, help="klt backend (default: klt's own, e.g. $KLT_SIM_BACKEND); "
                    "`local` is refused for Monte Carlo")
    ap.add_argument("--batch-runner-version-check", choices=["enforce", "warn"], default=None)
    ap.add_argument("--batch-capacity-wait-s", type=float, default=None)
    ap.add_argument("--batch-submit-retries", type=int, default=0,
                    help="re-submit the SAME request on a batch capacity refusal (never another backend)")
    ap.add_argument("--batch-retry-wait-s", type=float, default=120.0)
    ap.add_argument("--keep-work", type=Path, default=None,
                    help="probe: keep the sanitised report here (outside the repository; never commit it)")


def _as_mc(rep: dict) -> dict:
    """A non-MC single-unit report dressed as sample 0 (smoke only)."""
    for c in rep.get("corners", []):
        if not c.get("monte_carlo"):
            c["monte_carlo"] = {"sample_index": 0, "seed": 0, "mismatch_seed": 0, "process_seed": 0}
    return rep


#: Smoke parity: printed (15 digits) vs rawfile vectors through one extraction.
SMOKE_TOL = {"dc_gain_db": 1e-6, "gbw_hz": 1e-6, "pm_deg": 1e-4}


def smoke(pdk: Pdk) -> int:
    print(f"smoke test: {G.NOMINAL}, local single units, PDK={pdk.path}; writes nothing")
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="gainpm-mc-smoke-") as scratch:
        work = Path(scratch)
        got = {}
        for sw in (0, 1):
            tb = materialise(work / f"sw{sw}", pdk, mismatch=sw)
            req = mc_request(tb, pdk, [G.NOMINAL[0]], [G.NOMINAL[1]], [G.NOMINAL[2]], n=None, local=True)
            rep = run_klt(req, work / f"sw{sw}" / "out", "local", work / f"sw{sw}")
            ss, probs = extract(_as_mc(rep), [G.NOMINAL], 1)
            s = ss[G.NOMINAL][0]
            if probs:
                bad += [f"sw_stat_mismatch={sw}: {p}" for p in probs]
                continue
            got[sw] = s.metrics
            print(f"  sw_stat_mismatch={sw}: gain {s.metrics.dc_gain_db:.4f} dB, GBW {s.metrics.gbw_hz / 1e6:.4f} MHz, "
                  f"PM {s.metrics.pm_deg:.3f} deg")
        ref = G.run_single("smoke-ref", "gain driver rawfile path", pdk, work)
        if ref.error or ref.metrics is None or not ref.metrics.valid:
            bad.append(f"gain driver reference failed: {ref.error or ref.metrics.reason}")
        elif 0 in got:
            devs = []
            for attr, tol in SMOKE_TOL.items():
                a, b = getattr(got[0], attr), getattr(ref.metrics, attr)
                dev = abs(a / b - 1) if attr == "gbw_hz" else abs(a - b)
                devs.append(f"{attr} {dev:.2g} (tol {tol:g})")
                if not dev <= tol:
                    bad.append(f"parity {attr}: log path {a:.9g} vs rawfile path {b:.9g} (dev {dev:.3g} > {tol:g})")
            print("  parity with the gain driver's rawfile path (mismatch off): " + ", ".join(devs))
    if bad:
        print("SMOKE TEST FAILED:\n  " + "\n  ".join(bad))
        return 1
    print("smoke test OK (diagnostic; not evidence)")
    return 0


def probe(pdk: Pdk, args) -> int:
    """N=3 Monte Carlo transport probe on klt's backend: does each sample
    return a log with the vectors? Diagnostic; writes nothing into the repo."""
    check_backend(args.backend)
    want = [PROBE_POINT]
    print(f"probe: {PROBE_POINT} N={PROBE_N}, current committed export (diagnostic; not evidence)")
    with tempfile.TemporaryDirectory(prefix="gainpm-mc-probe-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "probe", pdk)
        try:
            rep, _req, wall = submit("probe", tb, pdk, [PROBE_POINT[0]], [PROBE_POINT[1]], [PROBE_POINT[2]],
                                     PROBE_N, args, work)
        except KltError as exc:
            print(f"ERROR: probe submission failed; no local fallback, nothing written.\n{exc}", file=sys.stderr)
            return 2
        samples, probs = extract(rep, want, PROBE_N)
        san = sanitise_report(rep)
    print(f"  execution: {exec_line(rep, wall)}")
    print(f"  environment.monte_carlo: {json.dumps((rep.get('environment') or {}).get('monte_carlo'), sort_keys=True)[:400]}")
    for s in samples[PROBE_POINT]:
        print(f"  sample {s.index}: seeds {s.seeds}, log {s.log_ref}, "
              + (f"gain {s.value('dc_gain_db'):.3f} dB, GBW {s.value('gbw_hz') / 1e6:.4f} MHz, "
                 f"PM {s.value('pm_deg'):.3f} deg" if s.valid else f"INVALID: {s.reason}"))
    distinct = len({round(s.value("pm_deg"), 6) for s in samples[PROBE_POINT] if s.valid})
    print(f"  distinct per-sample PM values: {distinct}/{PROBE_N}; problems: {'; '.join(probs) or 'none'}")
    if args.keep_work:
        args.keep_work.mkdir(parents=True, exist_ok=False)
        (args.keep_work / "probe-report.json").write_text(json.dumps(san, indent=1, sort_keys=True) + "\n")
        print(f"  sanitised report kept at {args.keep_work} (never commit it)")
    return 0 if not probs and distinct == PROBE_N else 1


def run_campaign(args, *, base: Path = HERE, records_dir: Path = GAIN_RECORDS) -> int:
    """The full campaign: gate -> claim record paths -> ONE request -> validate
    -> append-only record. Any refusal happens before anything is written or
    submitted; a failed submit or an incomplete sample set writes nothing."""
    dut = load_dut_text()
    errs = campaign_gate(args.revised_dut_sha256, args.deterministic_record, current_sha=dut_sha256(dut),
                         records_dir=records_dir)
    if errs:
        print("CAMPAIGN GATE: refused before submission:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 2
    try:
        check_backend(args.backend)
    except BackendRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    record, stamp = allocate_record_id(REPO_ROOT)
    try:
        paths = claim_record_paths(base, record, plots=False)
    except FileExistsError as exc:
        print(f"ERROR: {exc}; nothing submitted", file=sys.stderr)
        return 2
    pdk = find_pdk()
    want = G.expected_keys(CORNERS, TEMPS_C, SUPPLIES_V)
    ngspice, kver = ngspice_version(), klt_version()
    print(f"record {record}: {len(want)} points x N={MC_N}, PDK {pdk.path} (open_pdks {pdk.version}), klt {kver}")
    with tempfile.TemporaryDirectory(prefix="gainpm-mc-") as scratch:
        work = Path(scratch)
        tb = materialise(work / "grid", pdk)
        try:
            rep, req, wall = submit("grid", tb, pdk, CORNERS, TEMPS_C, SUPPLIES_V, MC_N, args, work)
        except KltError as exc:
            print(f"ERROR: the Monte Carlo request could not be run; NO RECORD WRITTEN, no local fallback.\n{exc}",
                  file=sys.stderr)
            return 2
        samples, problems = extract(rep, want, MC_N)
        stats = campaign_stats(samples, MC_N)
        if not campaign_complete(stats, problems):
            print("\n".join(stats_table(stats, diagnostic=True)))
            print("VALIDATION FAILED; NO RECORD WRITTEN:\n  " + "\n  ".join(problems[:60]), file=sys.stderr)
            return 2
        cdir = paths["corners"]
        cdir.mkdir(parents=True)
        write_samples_csv(cdir / "samples.csv", samples)
        write_vectors(cdir, samples)
        with (cdir / "klt-report.json").open("x") as fh:
            fh.write(json.dumps(sanitise_report(rep), indent=1, sort_keys=True) + "\n")
        paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
        write_snapshot(paths["snapshot"], record, req, dut)
        paths["record"].parent.mkdir(parents=True, exist_ok=True)
        with paths["record"].open("x") as fh:
            fh.write(build_record(record=record, stamp=stamp, pdk=pdk, ngspice=ngspice, kver=kver, rep=rep,
                                  wall=wall, stats=stats, samples=samples, dut_sha=dut_sha256(dut),
                                  det_record=args.deterministic_record))
    print(f"record written: {paths['record']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_args(ap)
    args = ap.parse_args(argv)
    if args.smoke and args.probe:
        ap.error("--smoke and --probe are separate diagnostic modes")
    if (args.smoke or args.probe) and (args.revised_dut_sha256 or args.deterministic_record):
        ap.error("--smoke/--probe are diagnostic and take no campaign pin; they never launch the campaign")
    if args.smoke:
        return smoke(find_pdk())
    if args.probe:
        try:
            return probe(find_pdk(), args)
        except BackendRefused as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    return run_campaign(args)


if __name__ == "__main__":
    sys.exit(main())
