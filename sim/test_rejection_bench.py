#!/usr/bin/env python3
"""Unit tests for `rejection_bench`, the rejection benches' shared record
writers (issue #337). No simulator, no PDK.

These close the coverage gap the #337 curation found: the helpers hoisted
from the CMRR driver had essentially no unit tests (only `worst_case` had
one), so the next relocation of this text would have had no guard. Each
test asserts on the EMITTED TEXT the record carries.
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rejection_bench as rb  # noqa: E402


class FakeRejection:
    """The `Rejection` attributes the emitters read (duck-typed)."""

    def __init__(self, dc_db, spot_db=None, at_fu_db=10.0, fu_hz=1e6, plateau_ok=True,
                 plateau_spread_db=0.01, low_db=None, lower_bound=False):
        self.dc_db = dc_db
        self.spot_db = {f: (spot_db or {}).get(f, dc_db + 1.0) for f in rb.SPOT_HZ}
        self.at_fu_db = at_fu_db
        self.fu_hz = fu_hz
        self.plateau_ok = plateau_ok
        self.plateau_spread_db = plateau_spread_db
        self.low_db = dc_db if low_db is None else low_db
        self.lower_bound = lower_bound

    def figure(self, which):
        if which == "dc":
            return self.dc_db
        if which == "fu":
            return self.at_fu_db
        return self.spot_db[float(which)]


def remote_report(job="j-42", provider="aws", spot=True, instance="c7g.2xl", state="succeeded"):
    return {"environment": {"remote": {"job_id": job, "provider": provider, "spot": spot,
                                       "instance_type": instance, "state": state,
                                       "runner_klt_version": "0.8.0", "client_klt_version": "0.8.0",
                                       "runner_compatibility": "ok"},
                            "engine": "ngspice", "engine_version": "42"},
            "provenance": {"pdk": {"version": "A-1", "source": "volare"}}}


def local_report():
    return {"environment": {"engine": "ngspice", "engine_version": "42"},
            "provenance": {"pdk": {"version": "A-1", "source": "volare"}}}


class PdkLinesTests(unittest.TestCase):
    def test_states_revision_and_client_resolved_provenance(self):
        pdk = SimpleNamespace(path=Path("/p"), variant="gf180mcuD", version="A-1", source="volare")
        lines = rb.pdk_lines(pdk, {"dm": remote_report(), "cm": local_report()})
        self.assertEqual(
            lines[0],
            "- **PDK revision**: gf180mcuD, open_pdks `A-1` (harness `find_pdk`, via volare); the "
            "local single units are pinned to it (`models.pdk_root`).")
        self.assertIn("SUBMITTING CLIENT (A-1 (volare))", lines[1])
        self.assertIn("committed gain-bench record", lines[1])

    def test_klt_pdk_version_defaults_to_n_a(self):
        self.assertEqual(rb.klt_pdk_version({}), "n/a")
        self.assertEqual(rb.klt_pdk_version({"provenance": {"pdk": {}}}), "n/a")


class ExecutionLinesTests(unittest.TestCase):
    def test_remote_and_local_lines(self):
        walls = {"dm": 61.4, "cm": 30.0}
        lines = rb.execution_lines({"dm": remote_report(), "cm": local_report()}, walls)
        self.assertEqual(lines[0],
                         "  - `dm`: backend `aws`, job `j-42`, Spot c7g.2xl, state `succeeded`, "
                         "runner klt `0.8.0` vs client `0.8.0` (compatibility `ok`); "
                         "engine `ngspice 42`; wall 61 s")
        self.assertEqual(lines[1],
                         "  - `cm`: `klt sim` local backend (no `environment.remote`); "
                         "engine `ngspice 42`; wall 30 s")


class RequestCommentTests(unittest.TestCase):
    def test_renders_as_bulleted_json_without_the_netlist(self):
        lines = rb.request_comment({"backend": "local", "netlist": "/abs/path.spice", "corners": 3})
        self.assertTrue(all(ln.startswith("* ") for ln in lines))
        self.assertEqual(lines[0], "* {")
        self.assertNotIn("netlist", "".join(lines))
        self.assertIn('"backend": "local"', "".join(lines))


class WriteSnapshotTests(unittest.TestCase):
    def test_embeds_conditions_dut_testbench_and_deck(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.spice"
            tb = rb.REPO_ROOT / "sim" / "cmrr" / "testbench" / "tb_cmrr.spice"
            rb.write_snapshot(path, "20261115-000001-abcdef0", "issue #39", tb,
                              {"dm": {"backend": "local", "corners": 3}}, "Vdd vdd 0 dc 3.3")
            text = path.read_text()
        self.assertEqual(text.splitlines()[0],
                         "* netlist snapshot for record 20261115-000001-abcdef0 (issue #39)")
        self.assertIn("* ---- conditions: klt sim request, excitation `dm` (grid) ----", text)
        self.assertIn("* ---- DUT: design/netlist/opamp_two_stage.spice, wrapper-normalised "
                      "(file opamp_two_stage.dut.spice) ----", text)
        self.assertIn("* ---- testbench: sim/cmrr/testbench/tb_cmrr.spice "
                      "(verbatim; the driver rewrites only its .param line) ----", text)
        self.assertIn("* ---- klt-generated deck of the nominal point, first excitation (corner.cir) ----", text)
        self.assertIn("* | Vdd vdd 0 dc 3.3", text)
        self.assertIn(tb.read_text(), text)  # verbatim


class TableEmitterTests(unittest.TestCase):
    def test_figure_table_orders_by_corner_and_flags_bounds(self):
        values = {
            ("ff", -40.0, 2.97): FakeRejection(90.0, lower_bound=True),
            ("typical", 27.0, 3.30): FakeRejection(120.0, plateau_ok=False, plateau_spread_db=0.5),
        }
        lines = []
        rb.figure_table(lines.append, values, "CMRR")
        self.assertEqual(
            lines[0],
            "| point | CMRR DC | 1 kHz | 10 kHz | 100 kHz | 1 MHz | at f_u | f_u (MHz) | note |")
        self.assertEqual(lines[1], "|---|---|---|---|---|---|---|---|---|")
        self.assertEqual(lines[2],
                         "| typical / 27 C / 3.30 V | 120.00 | 121.00 | 121.00 | 121.00 | 121.00 | "
                         "10.00 | 1.000 | no plateau (spread 0.500 dB); 0.1 Hz value 120.00 dB |")
        self.assertEqual(lines[3],
                         "| ff / -40 C / 2.97 V | >= 90.00 | >= 91.00 | >= 91.00 | >= 91.00 | >= 91.00 | "
                         ">= 10.00 | 1.000 | lower bound (numerical floor) |")

    def test_worst_table_reports_the_lowest_and_missing_points(self):
        values = {
            ("typical", 27.0, 3.30): FakeRejection(120.0, at_fu_db=5.0),
            ("ff", -40.0, 2.97): FakeRejection(90.0, at_fu_db=float("nan")),
        }
        lines = []
        rb.worst_table(lines.append, values, "PSRR+")
        self.assertEqual(
            lines[0],
            "| PSRR+ figure | worst (lowest) value (dB) | worst-case corner (process / T / VDD) | "
            "best (dB) | points without the figure |")
        self.assertEqual(lines[1], "|---|---|---|---|---|")
        self.assertEqual(lines[2],
                         "| DC plateau (0.1-1 Hz) | **90.00** | ff / -40 C / 2.97 V | 120.00 | 0 |")
        self.assertEqual(lines[3], "| 1 kHz | **91.00** | ff / -40 C / 2.97 V | 121.00 | 0 |")
        self.assertEqual(lines[7],
                         "| at f_u (differential unity gain) | **5.00** | typical / 27 C / 3.30 V | "
                         "5.00 | 1 |")

    def test_worst_case_and_fmt_db(self):
        vals = {("typical", 27.0, 3.30): FakeRejection(120.0), ("ff", -40.0, 2.97): FakeRejection(90.0)}
        self.assertEqual(rb.worst_case(vals, "dc"), (90.0, ("ff", -40.0, 2.97), 0))
        empty = {("typical", 27.0, 3.30): FakeRejection(float("nan"))}
        v, k, miss = rb.worst_case(empty, "dc")
        self.assertTrue(math.isnan(v))
        self.assertIsNone(k)
        self.assertEqual(miss, 1)
        self.assertEqual(rb.fmt_db(float("nan")), "n/a")
        self.assertEqual(rb.fmt_db(12.345, lb=True), ">= 12.35")


class SaveModePointTests(unittest.TestCase):
    def test_writes_log_deck_and_columnar_phasors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            log, deck = cdir / "p.log", cdir / "p.cir"
            log.write_text("log line\n")
            deck.write_text("deck line\n")
            vec = {"frequency": np.array([1 + 0j, 2 + 0j]),
                   "v(vout)": np.array([1 + 2j, 3 + 4j])}
            rb.save_mode_point(cdir, ("typical", 27.0, 3.30), "dm",
                               {"log": log, "deck": deck, "vec": vec}, ("v(vout)",))
            dat = (cdir / "typical_27c_3.30v.dm.dat").read_text()
            log_text = (cdir / "typical_27c_3.30v.dm.log").read_text()
            deck_text = (cdir / "typical_27c_3.30v.dm.cir").read_text()
        header, rows = dat.splitlines()[0], dat.splitlines()[1:]
        self.assertEqual(header.strip(), "# freq_hz rev(vout) imv(vout)")
        self.assertEqual(rows[0].split(), ["1.000000000000e+00", "1.000000000000e+00", "2.000000000000e+00"])
        self.assertEqual(log_text, "log line\n")
        self.assertEqual(deck_text, "deck line\n")


if __name__ == "__main__":
    unittest.main()
