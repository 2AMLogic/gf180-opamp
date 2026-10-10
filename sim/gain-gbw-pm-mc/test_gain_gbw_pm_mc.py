#!/usr/bin/env python3
"""Offline tests for the gain / GBW / PM mismatch Monte Carlo runner (issue #129).

No simulator, no PDK, no forge: the log-vector transport, the shared
extraction (parity with the gain driver), sample identity and failure
accounting, the statistics, the testbench guard, the request, the #42
campaign gate and the submission path (with `klt` mocked) are checked on
synthetic data and on mutated copies of the committed bench. Everything is
written to temporary directories; nothing lands under `records/`.

    python3 sim/gain-gbw-pm-mc/test_gain_gbw_pm_mc.py
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import math
import statistics
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_gain_gbw_pm_mc as m  # noqa: E402
from harness import KltError, Pdk  # noqa: E402

G = m.G
FREQ = np.logspace(math.log10(G.AC_FSTART), math.log10(G.AC_FSTOP), m.N_FREQ)
NOM = ("typical", 27.0, 3.30)
WANT5 = G.expected_keys(m.CORNERS, m.TEMPS_C, m.SUPPLIES_V)


def poles(a0=6e4, p=(200.0, 25e6), z=(), freq=FREQ) -> np.ndarray:
    s = 1j * freq
    h = a0 * np.ones_like(s)
    for x in p:
        h = h / (1 + s / x)
    for x in z:
        h = h * (1 + s / x)
    return h


def ngspice_log(vout, vinp=None, vinn=None, freq=FREQ, *, header=None, rows=None, extra="") -> str:
    """A log in the shape ngspice-46 prints the tail's table (see the smoke)."""
    vinp = np.ones_like(vout) if vinp is None else vinp
    vinn = np.zeros_like(vout) if vinn is None else vinn
    cols = header or ["Index", "frequency", *m.VEC_COLS]
    L = ["Doing analysis at TEMP = 27.000000", "No. of Data Rows : 201", m.BEGIN,
         " " * 40 + "* klt sim -- generated corner deck, do not edit",
         " " * 40 + "AC Analysis  Sat Oct 10 18:01:23  2026", "-" * 120,
         "\t".join(cols), "-" * 120]
    for i, f in enumerate(freq):
        vals = [f, f, vout[i].real, vout[i].imag, vinp[i].real, vinp[i].imag, vinn[i].real, vinn[i].imag]
        L.append("\t".join([str(i)] + [f"{v:.15e}" for v in vals]) + "\t")
    if rows is not None:
        L = L[: 8 + rows]
    L += [m.END, extra, "ngspice-46 done"]
    return "\n".join(L) + "\n"


def corner(k, idx, log_path, *, seeds=True, status="pass", diags=(), meas=None):
    mc = {"sample_index": idx}
    if seeds:
        mc.update(seed=1000 + (idx if isinstance(idx, int) else 0), mismatch_seed=7, process_seed=9)
    return {"corner_id": f"{k[0]}/{k[2]:g}V/{k[1]:g}C/mc{idx}", "process": k[0], "temperature_c": k[1],
            "supply_v": {"vdd": k[2], "vcm": round(k[2] / 2, 6)}, "status": status, "monte_carlo": mc,
            "diagnostics": list(diags), "artifacts": {"log": log_path} if log_path else {},
            "measurements": meas or []}


def mc_report(keys, n, log_text, *, skip=(), dup=(), extra=()):
    """A synthetic klt MC report: every (key, idx) shares one log text."""
    corners, logs = [], {}
    for k in keys:
        for i in range(n):
            if (k, i) in skip:
                continue
            p = f"/x/{G.point_stem(k)}/mc{i}/ngspice.log"
            logs[p] = log_text
            corners.append(corner(k, i, p))
            if (k, i) in dup:
                corners.append(corner(k, i, p))
    corners += list(extra)
    return {"corners": corners}, logs


def fake_metrics(gain, gbw, pm, valid=True):
    return G.Metrics(valid=valid, reason="" if valid else "x", dc_gain_db=gain, gbw_hz=gbw, pm_deg=pm)


def samples_of(values, n=None):
    """Valid samples with given (gain, gbw, pm) tuples, indices 0.."""
    out = [m.Sample(NOM, i, {"seed": i, "mismatch_seed": 1, "process_seed": 2}, "pass", "l",
                    "", fake_metrics(*v)) for i, v in enumerate(values)]
    return out


GOOD_LOG = ngspice_log(poles())


class TransportTests(unittest.TestCase):
    def test_tail_prints_the_three_node_phasors_between_markers(self):
        t = m.analysis_tail()
        self.assertIn(f"echo {m.BEGIN}", t)
        self.assertIn(f"echo {m.END}", t)
        for v in ("v(vout)", "v(vinp)", "v(vinn)"):
            self.assertIn(f"real({v})", t)
            self.assertIn(f"imag({v})", t)
        self.assertIn("set numdgt=15", t)
        self.assertIn("set nobreak", t)

    def test_round_trip_is_exact_to_print_precision(self):
        h = poles()
        vec = m.parse_log_vectors(ngspice_log(h))
        self.assertEqual(vec["frequency"].size, m.N_FREQ)
        np.testing.assert_allclose(vec["v(vout)"], h, rtol=1e-14)
        np.testing.assert_allclose(vec["frequency"].real, FREQ, rtol=1e-14)

    def test_sweep_has_201_points(self):
        self.assertEqual(m.N_FREQ, 201)

    def test_malformed_logs_fail_loudly(self):
        h = poles()
        cases = {
            "no block": "ngspice-46 done\n",
            "two blocks": GOOD_LOG + GOOD_LOG,
            "missing end": GOOD_LOG.replace(m.END, ""),
            "row missing": ngspice_log(h, rows=200),
            "wrong header": ngspice_log(h, header=["Index", "frequency", "v(vout)"]),
            "page break header": GOOD_LOG.replace("\n0\t", "\nIndex\tfrequency\t" + "\t".join(m.VEC_COLS) + "\n0\t", 1),
            "nan": ngspice_log(np.where(np.arange(h.size) == 50, np.nan, h)),
            "inf": ngspice_log(np.where(np.arange(h.size) == 50, np.inf, h)),
            "text field": GOOD_LOG.replace("\n3\t", "\n3\tabc", 1),
            "index out of order": GOOD_LOG.replace("\n3\t", "\n4\t", 1),
            "scale mismatch": ngspice_log(h).replace(f"\n0\t{FREQ[0]:.15e}\t", f"\n0\t{FREQ[0] * 2:.15e}\t", 1),
        }
        for name, log in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                m.parse_log_vectors(log)


class ExtractionParityTests(unittest.TestCase):
    """Every sample goes through the gain driver's extraction, unchanged."""

    def metrics_via_log(self, vout, vinp=None, vinn=None):
        vec = m.parse_log_vectors(ngspice_log(vout, vinp, vinn))
        return m.evaluate_vectors(vec, {})

    def test_parity_with_gain_driver_extraction(self):
        for p in ((200.0, 25e6), (150.0, 12e6, 80e6), (300.0, 40e6)):
            h = poles(p=p)
            got, why = self.metrics_via_log(h)
            ref = G.extract_metrics(FREQ, h)
            self.assertEqual(why, "")
            for a in ("dc_gain_db", "gbw_hz", "pm_deg", "plateau_spread_db"):
                self.assertAlmostEqual(getattr(got, a), getattr(ref, a), places=9, msg=a)

    def test_units_and_interpolation_against_the_analytic_two_pole(self):
        a0, p1, p2 = 6e4, 200.0, 25e6
        got, _ = self.metrics_via_log(poles(a0, (p1, p2)))
        self.assertAlmostEqual(got.dc_gain_db, 20 * math.log10(a0), places=3)  # dB
        lo, hi = 1.0, 1e10
        for _ in range(200):
            mid = math.sqrt(lo * hi)
            mag = a0 / abs(1 + 1j * mid / p1) / abs(1 + 1j * mid / p2)
            lo, hi = (mid, hi) if mag > 1 else (lo, mid)
        f = math.sqrt(lo * hi)
        pm = 180 - math.degrees(math.atan(f / p1)) - math.degrees(math.atan(f / p2))
        self.assertLess(abs(got.gbw_hz / f - 1), 2e-3)  # Hz, interpolated on 20 pts/dec
        self.assertLess(abs(got.pm_deg - pm), 0.2)  # degrees

    def test_phase_is_unwrapped_through_minus_180(self):
        h = poles(6e4, (200.0, 2e6, 3e6, 4e6))  # phase passes -180 before unity gain
        got, why = self.metrics_via_log(h)
        self.assertEqual(why, "")
        self.assertLess(got.pm_deg, 0)  # not folded back to a positive margin
        self.assertAlmostEqual(got.pm_deg, G.extract_metrics(FREQ, h).pm_deg, places=9)

    def test_missing_crossing_is_invalid(self):
        _, why = self.metrics_via_log(poles(6e4, (1e6,)))
        self.assertIn("no 0 dB crossing", why)

    def test_recrossing_is_invalid(self):
        wn = 2 * math.pi * 5e7
        s = 2j * math.pi * FREQ
        h = poles() * wn**2 / (s**2 + 2 * 0.01 * wn * s + wn**2)  # 34 dB resonance above unity gain
        ref = G.extract_metrics(FREQ, h)
        self.assertFalse(ref.valid)
        _, why = self.metrics_via_log(h)
        self.assertIn("crossings", why)

    def test_non_flat_plateau_is_invalid(self):
        _, why = self.metrics_via_log(poles(6e4, (0.2, 25e6)))
        self.assertIn("plateau", why)

    def test_reversed_polarity_is_invalid(self):
        _, why = self.metrics_via_log(-poles())
        self.assertIn("polarity", why)

    def test_differential_excitation_is_checked(self):
        h = poles()
        _, why = self.metrics_via_log(h * 0.5, vinp=np.full(h.size, 0.5 + 0j))
        self.assertIn("excitation", why)
        _, why = self.metrics_via_log(h, vinp=np.zeros(h.size, complex))
        self.assertIn("malformed", why)

    def test_meas_crosscheck_disagreement_invalidates(self):
        vec = m.parse_log_vectors(GOOD_LOG)
        _, why = m.evaluate_vectors(vec, {"gbw_hz": 1.0})
        self.assertIn("cross-check", why)


class IdentityAndAccountingTests(unittest.TestCase):
    def run_extract(self, rep_logs, keys=WANT5, n=300):
        rep, logs = rep_logs
        return m.extract(rep, keys, n, read_log=logs.__getitem__)

    def test_complete_five_corner_campaign(self):
        samples, probs = self.run_extract(mc_report(WANT5, 300, GOOD_LOG))
        self.assertEqual(probs, [])
        self.assertEqual(sorted(samples), sorted(WANT5))
        self.assertTrue(all(len(v) == 300 and all(s.valid for s in v) for v in samples.values()))
        st = m.campaign_stats(samples, 300)
        self.assertTrue(m.campaign_complete(st, probs))
        self.assertEqual(st[NOM]["pm"].n_valid, 300)

    def test_missing_index_is_an_explicit_invalid_row(self):
        samples, probs = self.run_extract(mc_report([NOM], 5, GOOD_LOG, skip={(NOM, 3)}), [NOM], 5)
        ss = samples[NOM]
        self.assertEqual([s.index for s in ss], [0, 1, 2, 3, 4])
        self.assertFalse(ss[3].valid)
        self.assertIn("missing", ss[3].reason)
        self.assertTrue(probs)
        st = m.campaign_stats(samples, 5)
        self.assertFalse(m.campaign_complete(st, probs))
        for r in st[NOM].values():
            self.assertEqual((r.n_valid, r.n_invalid, r.n_fail, r.n_expected), (4, 1, 1, 5))

    def test_duplicate_index_cannot_inflate_the_denominator(self):
        samples, probs = self.run_extract(mc_report([NOM], 5, GOOD_LOG, dup={(NOM, 2)}), [NOM], 5)
        self.assertEqual(len(samples[NOM]), 5)
        self.assertTrue(any("duplicate" in p for p in probs))
        self.assertFalse(m.campaign_complete(m.campaign_stats(samples, 5), probs))

    def test_out_of_range_and_malformed_identities(self):
        bad = [corner(NOM, 5, "/x/l"), corner(NOM, -1, "/x/l"), corner(NOM, "2", "/x/l"),
               corner(NOM, True, "/x/l"), corner(NOM, 2.0, "/x/l"),
               {"corner_id": "broken", "monte_carlo": {"sample_index": 0}}]
        rep, logs = mc_report([NOM], 5, GOOD_LOG, extra=bad)
        logs["/x/l"] = GOOD_LOG
        samples, probs = m.extract(rep, [NOM], 5, read_log=logs.__getitem__)
        self.assertEqual(len(samples[NOM]), 5)
        self.assertTrue(all(s.valid for s in samples[NOM]))
        self.assertEqual(sum("outside" in p for p in probs), 2)
        self.assertEqual(sum("malformed sample identity" in p for p in probs), 3)
        self.assertEqual(sum("unparseable" in p for p in probs), 1)
        self.assertFalse(m.campaign_complete(m.campaign_stats(samples, 5), probs))

    def test_unexpected_corner_is_a_problem(self):
        rep, logs = mc_report([NOM], 3, GOOD_LOG, extra=[corner(("ss", 27.0, 3.30), 0, "/x/l")])
        logs["/x/l"] = GOOD_LOG
        samples, probs = m.extract(rep, [NOM], 3, read_log=logs.__getitem__)
        self.assertTrue(any("unexpected grid point" in p for p in probs))
        self.assertEqual(list(samples), [NOM])

    def test_absent_seeds_and_logs_and_simulator_errors_are_invalid_rows(self):
        rep, logs = mc_report([NOM], 4, GOOD_LOG)
        rep["corners"][0]["monte_carlo"].pop("mismatch_seed")
        rep["corners"][1]["artifacts"] = {}
        rep["corners"][2].update(status="error", diagnostics=[{"severity": "error", "message": "timestep too small"}])
        logs[rep["corners"][2]["artifacts"]["log"] + ".dead"] = ""
        rep["corners"][2]["artifacts"]["log"] += ".dead"
        samples, probs = m.extract(rep, [NOM], 4, read_log=logs.__getitem__)
        ss = samples[NOM]
        self.assertIn("seeds not retained: mismatch_seed", ss[0].reason)
        self.assertIn("no ngspice log", ss[1].reason)
        self.assertIn("simulation failed", ss[2].reason)
        self.assertIn("timestep too small", ss[2].reason)
        self.assertTrue(ss[3].valid)
        st = m.campaign_stats(samples, 4)
        self.assertEqual(st[NOM]["gain"].n_invalid, 3)
        self.assertEqual(st[NOM]["pm"].n_fail, 3)

    def test_malformed_vectors_keep_an_explicit_reason(self):
        rep, logs = mc_report([NOM], 2, GOOD_LOG)
        p = rep["corners"][1]["artifacts"]["log"] + ".nan"
        logs[p] = ngspice_log(np.where(np.arange(m.N_FREQ) == 9, np.nan, poles()))
        rep["corners"][1]["artifacts"]["log"] = p
        samples, _ = m.extract(rep, [NOM], 2, read_log=logs.__getitem__)
        self.assertIn("non-finite", samples[NOM][1].reason)

    def test_unreadable_log_is_invalid_not_skipped(self):
        rep, _ = mc_report([NOM], 2, GOOD_LOG)

        def read(p):
            raise OSError("gone")
        samples, probs = m.extract(rep, [NOM], 2, read_log=read)
        self.assertEqual(len(samples[NOM]), 2)
        self.assertFalse(any(s.valid for s in samples[NOM]))
        self.assertTrue(probs)


class StatisticsTests(unittest.TestCase):
    def test_known_distribution(self):
        pms = [58.0, 60.0, 62.0, 64.0, 66.0]
        ss = samples_of([(70.0 + i, 11e6 + 1e5 * i, pm) for i, pm in enumerate(pms)])
        st = m.row_stats(ss, 5)
        pm = st["pm"]
        self.assertAlmostEqual(pm.mean, statistics.fmean(pms))
        self.assertAlmostEqual(pm.sigma, statistics.stdev(pms))  # ddof = 1
        self.assertAlmostEqual(pm.sigma, float(np.std(pms, ddof=1)))
        self.assertAlmostEqual(pm.mean_3s, pm.mean - 3 * pm.sigma)
        self.assertEqual(pm.n_below, 1)  # 58 < 60; 60 is exactly on the bound and passes
        self.assertEqual(pm.n_invalid, 0)
        self.assertAlmostEqual(pm.fail_fraction, 1 / 5)
        self.assertEqual(st["gain"].n_below, 0)
        self.assertTrue(all(r.complete for r in st.values()))

    def test_exact_bound_samples_pass_every_row(self):
        st = m.row_stats(samples_of([(60.0, 10e6, 60.0)] * 3), 3)
        self.assertEqual([st[r].n_fail for r in ("gain", "gbw", "pm")], [0, 0, 0])

    def test_bounds_are_the_ratified_ones(self):
        self.assertEqual({r[0]: r[3] for r in m.ROWS}, {"gain": 60.0, "gbw": 10e6, "pm": 60.0})

    def test_invalid_samples_fail_all_three_rows_and_are_distinguished(self):
        ss = samples_of([(70.0, 12e6, 59.0), (70.0, 12e6, 65.0), (70.0, 12e6, 65.0), (70.0, 12e6, 65.0)])
        ss[3].reason = "no 0 dB crossing"
        st = m.row_stats(ss, 4)
        self.assertEqual((st["pm"].n_below, st["pm"].n_invalid, st["pm"].n_fail), (1, 1, 2))
        self.assertEqual((st["gain"].n_below, st["gain"].n_invalid, st["gain"].n_fail), (0, 1, 1))
        self.assertEqual(st["gbw"].fail_fraction, 1 / 4)
        self.assertEqual(st["pm"].n_valid, 3)
        self.assertFalse(st["pm"].complete)
        self.assertAlmostEqual(st["pm"].mean, statistics.fmean([59.0, 65.0, 65.0]))

    def test_denominator_is_fixed_and_sets_must_be_exact(self):
        with self.assertRaises(ValueError):
            m.row_stats(samples_of([(70, 12e6, 65)] * 3), 4)  # a missing row cannot be dropped
        ss = samples_of([(70, 12e6, 65)] * 3)
        ss[2].index = 1
        with self.assertRaises(ValueError):
            m.row_stats(ss, 3)  # duplicate index

    def test_insufficient_valid_samples(self):
        ss = samples_of([(70.0, 12e6, 65.0), (70.0, 12e6, 65.0), (70.0, 12e6, 65.0)])
        ss[1].reason = ss[2].reason = "x"
        st = m.row_stats(ss, 3)
        self.assertEqual(st["pm"].n_valid, 1)
        self.assertTrue(math.isnan(st["pm"].sigma))
        self.assertEqual(st["pm"].n_fail, 2)
        table = "\n".join(m.stats_table({NOM: st}, diagnostic=True))
        self.assertIn("DIAGNOSTIC ONLY", table)
        self.assertIn("1 / 3", table)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.gain = G.TESTBENCH.read_text()
        self.mc = m.inject_mismatch(self.gain, 1)

    def test_committed_bench_plus_switch_passes(self):
        self.assertEqual(m.guard_mc_testbench(self.mc), [])
        self.assertEqual(m.guard_mc_testbench(m.inject_mismatch(self.gain, 0)), [])
        self.assertEqual(self.mc.count("sw_stat_mismatch"), 1)

    def test_switch_follows_the_design_include(self):
        lines = self.mc.splitlines()
        i = next(j for j, ln in enumerate(lines) if "design.ngspice" in ln and ln.startswith(".include"))
        self.assertEqual(lines[i + 1], ".param sw_stat_mismatch=1")

    def test_mutations_are_rejected(self):
        cases = {
            "no switch": self.gain,
            "two switches": self.mc.replace(".param sw_stat_mismatch=1", ".param sw_stat_mismatch=1\n.param sw_stat_mismatch=1"),
            "switch before include": self.mc.replace(".param sw_stat_mismatch=1\n", "").replace(
                ".include 'design.ngspice'", ".param sw_stat_mismatch=1\n.include 'design.ngspice'"),
            "switch value": self.mc.replace("sw_stat_mismatch=1", "sw_stat_mismatch=2"),
            "global spread": self.mc + "\n.param sw_stat_global=1\n",
            "hand MOSFET": self.mc + "\nM1 a b c d nfet_03v3 w=1u l=1u\n",
            "load changed": self.mc.replace("CL vout 0 2p", "CL vout 0 1p"),
            "bias changed": self.mc.replace("Ibias vdd ibias dc 10u", "Ibias vdd ibias dc 12u"),
            "dut include dropped": self.mc.replace(".include 'opamp_two_stage.dut.spice'", ""),
            "extra element": self.mc + "\nRx vout 0 1meg\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertTrue(m.guard_mc_testbench(text), name)

    def test_inject_requires_exactly_one_design_include(self):
        with self.assertRaises(RuntimeError):
            m.inject_mismatch(self.gain.replace(".include 'design.ngspice'", ""), 1)
        with self.assertRaises(ValueError):
            m.inject_mismatch(self.gain, 2)


def fake_pdk(tmp: Path) -> Pdk:
    return Pdk(path=tmp / "gf180mcuD", variant="gf180mcuD", source="test")


class RequestTests(unittest.TestCase):
    def test_population(self):
        with tempfile.TemporaryDirectory() as t:
            req = m.mc_request(Path(t) / "tb.spice", fake_pdk(Path(t)), m.CORNERS, m.TEMPS_C, m.SUPPLIES_V, n=m.MC_N)
        self.assertEqual([p["name"] for p in req["corners"]["process"]], ["typical", "ff", "ss", "fs", "sf"])
        for p in req["corners"]["process"]:
            self.assertEqual(p["sections"][1:], list(G.PASSIVE_SECTIONS))  # typical passives
        self.assertEqual(req["corners"]["temperature_c"], [27.0])
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [3.30], "vcm": [1.65]})
        self.assertEqual(req["monte_carlo"], {"n": 300, "seed": 45, "vary": "mismatch"})
        self.assertEqual(req["analysis"]["kind"], "ac")
        self.assertTrue(req["analysis"]["args"].startswith(f"dec {G.AC_PPD:g} {G.AC_FSTART:g} {G.AC_FSTOP:g}\n"))
        self.assertIn(m.analysis_tail(), req["analysis"]["args"])
        self.assertTrue(all("spice" in x and "expr" not in x for x in req["measurements"]))
        self.assertTrue(req["options"]["keep_artifacts"])
        self.assertNotIn("pdk_root", req["models"])
        self.assertIn("design.ngspice", m.inject_mismatch(G.TESTBENCH.read_text(), 1))


DET_RECORD = "20990101-000000-abcdef0"
REVISED_DUT = "* revised DUT for tests\n.subckt opamp_two_stage a b\n.ends\n"
REVISED_SHA = hashlib.sha256(REVISED_DUT.encode()).hexdigest()


def det_records(tmp: Path, sha: str = REVISED_SHA) -> Path:
    d = tmp / "gain-records"
    d.mkdir()
    (d / f"{DET_RECORD}.md").write_text(f"- **DUT**: x (wrapper-normalised; normalised sha256 `{sha}`), ...\n")
    return d


class GateTests(unittest.TestCase):
    def test_gate(self):
        with tempfile.TemporaryDirectory() as t:
            recs = det_records(Path(t))
            ok = m.campaign_gate(REVISED_SHA, DET_RECORD, current_sha=REVISED_SHA, records_dir=recs)
            self.assertEqual(ok, [])
            cases = {
                "no pin": (None, DET_RECORD, REVISED_SHA, "requires --revised-dut-sha256"),
                "malformed pin": ("abc", DET_RECORD, REVISED_SHA, "not a lowercase sha256"),
                "pre-redesign pin": (m.PRE_REDESIGN_DUT_SHA256, DET_RECORD, m.PRE_REDESIGN_DUT_SHA256, "pre-redesign"),
                "stale pin": (REVISED_SHA, DET_RECORD, "f" * 64, "stale pin"),
                "no record": (REVISED_SHA, None, REVISED_SHA, "requires --deterministic-record"),
                "bad record id": (REVISED_SHA, "../x", REVISED_SHA, "not a record id"),
                "absent record": (REVISED_SHA, "20990101-000000-0000000", REVISED_SHA, "does not exist"),
            }
            for name, (pin, rec, cur, want) in cases.items():
                with self.subTest(name):
                    errs = m.campaign_gate(pin, rec, current_sha=cur, records_dir=recs)
                    self.assertTrue(any(want in e for e in errs), errs)
        with tempfile.TemporaryDirectory() as t:
            recs = det_records(Path(t), sha="e" * 64)
            errs = m.campaign_gate(REVISED_SHA, DET_RECORD, current_sha=REVISED_SHA, records_dir=recs)
            self.assertTrue(any("does not cite" in e for e in errs))

    def test_current_committed_export_is_the_pre_redesign_dut(self):
        # Until #42 lands, the committed export is the pre-redesign DUT, so a
        # full campaign on this checkout is refused whatever is pinned.
        cur = m.dut_sha256(m.load_dut_text())
        if cur == m.PRE_REDESIGN_DUT_SHA256:
            self.assertTrue(m.campaign_gate(cur, DET_RECORD, current_sha=cur))


def args_for(**kw):
    ap = argparse.ArgumentParser()
    m.add_args(ap)
    a = ap.parse_args([])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class SubmissionTests(unittest.TestCase):
    """`klt` is mocked: one request, the population, the gate, no fallback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.base = self.t / "exp"
        self.base.mkdir()
        self.recs = det_records(self.t)
        self.calls = []
        tb = self.t / "tb.spice"
        tb.write_text("* tb\n")
        self.patches = [
            mock.patch.object(m, "load_dut_text", lambda: REVISED_DUT),
            mock.patch.object(m, "find_pdk", lambda: fake_pdk(self.t)),
            mock.patch.object(m, "materialise", lambda work, pdk, mismatch=1: tb),
            mock.patch.object(m, "ngspice_version", lambda: "test"),
            mock.patch.object(m, "klt_version", lambda: "test"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_campaign(self, klt, **kw):
        def fake(req, outdir, backend, workdir, *, retries, wait_s):
            self.calls.append((req, backend))
            return klt(req)
        a = args_for(revised_dut_sha256=REVISED_SHA, deterministic_record=DET_RECORD, **kw)
        err = io.StringIO()
        with mock.patch.object(m, "run_klt_retrying", fake), mock.patch.object(m, "run_klt") as local, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = m.run_campaign(a, base=self.base, records_dir=self.recs)
        self.assertFalse(local.called, "a local klt run happened")
        return rc, err.getvalue()

    def written(self):
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))

    def test_failed_submit_has_no_fallback_and_writes_nothing(self):
        def klt(req):
            raise KltError("klt sim error: batch submit failed")
        rc, err = self.run_campaign(klt)
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.calls), 1)
        req, backend = self.calls[0]
        self.assertIsNone(backend)  # klt's own resolution ($KLT_SIM_BACKEND), never "local"
        self.assertEqual(req["monte_carlo"], {"n": 300, "seed": 45, "vary": "mismatch"})
        self.assertEqual(len(req["corners"]["process"]), 5)
        self.assertEqual(req["corners"]["supply_v"], {"vdd": [3.30], "vcm": [1.65]})
        self.assertIn("no local fallback", err)
        self.assertEqual(self.written(), [])

    def test_gate_refusals_submit_nothing(self):
        for kw in ({"revised_dut_sha256": None}, {"revised_dut_sha256": "f" * 64}, {"deterministic_record": None}):
            self.calls.clear()
            a = args_for(**{"revised_dut_sha256": REVISED_SHA, "deterministic_record": DET_RECORD, **kw})
            with mock.patch.object(m, "run_klt_retrying", lambda *x, **y: self.calls.append(x)), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = m.run_campaign(a, base=self.base, records_dir=self.recs)
            self.assertEqual((rc, self.calls, self.written()), (2, [], []), kw)

    def test_local_backend_is_refused_before_submission(self):
        rc, err = self.run_campaign(lambda req: {}, backend="local")
        self.assertEqual((rc, self.calls), (2, []))
        self.assertIn("--backend local is refused", err)
        with self.assertRaises(m.BackendRefused):
            m.check_backend("LOCAL")

    def test_existing_record_path_is_refused_before_submission(self):
        rid = "20990101-000000-abcdef0"
        (self.base / "records").mkdir()
        (self.base / "records" / f"{rid}.md").write_text("historical\n")
        with mock.patch.object(m, "allocate_record_id", lambda root: (rid, None)):
            rc, err = self.run_campaign(lambda req: {})
        self.assertEqual((rc, self.calls), (2, []))
        self.assertIn("append-only", err)
        self.assertEqual((self.base / "records" / f"{rid}.md").read_text(), "historical\n")

    def test_incomplete_campaign_writes_nothing(self):
        def klt(req):
            rep, logs = mc_report(WANT5, 300, GOOD_LOG, skip={(NOM, 7)})
            self.logs = logs
            return rep
        with mock.patch.object(m, "extract", lambda rep, want, n: m.__dict__["_extract_real"](
                rep, want, n, read_log=self.logs.__getitem__)):
            rc, err = self.run_campaign(klt)
        self.assertEqual(rc, 2)
        self.assertIn("NO RECORD WRITTEN", err)
        self.assertEqual(self.written(), [])

    def test_complete_campaign_writes_one_append_only_record(self):
        def klt(req):
            rep, logs = mc_report(WANT5, 3, GOOD_LOG)
            self.logs = logs
            return rep
        with mock.patch.object(m, "MC_N", 3), mock.patch.object(m, "extract", lambda rep, want, n: m.__dict__[
                "_extract_real"](rep, want, n, read_log=self.logs.__getitem__)):
            rc, err = self.run_campaign(klt)
        self.assertEqual(rc, 0, err)
        files = self.written()
        rid = next(Path(f).stem for f in files if f.startswith("records/") and f.endswith(".md"))
        for need in (f"records/{rid}.md", f"netlist-snapshots/{rid}.spice", f"corners/{rid}/samples.csv",
                     f"corners/{rid}/klt-report.json"):
            self.assertIn(need, files)
        md = (self.base / "records" / f"{rid}.md").read_text()
        self.assertIn(REVISED_SHA, md)
        self.assertIn(DET_RECORD, md)
        self.assertIn("item 6", md)
        csv_rows = (self.base / "corners" / rid / "samples.csv").read_text().splitlines()
        self.assertEqual(len(csv_rows), 1 + 5 * 3)
        with self.assertRaises(FileExistsError):
            G.claim_record_paths(self.base, rid, plots=False)


m._extract_real = m.extract


class NoRecordsTests(unittest.TestCase):
    def test_suite_writes_nothing_into_the_experiment(self):
        for d in ("records", "corners", "netlist-snapshots", "probes"):
            self.assertFalse((HERE / d).exists(), f"{d}/ must not exist until the #42-gated campaign runs")


if __name__ == "__main__":
    unittest.main(verbosity=1)
