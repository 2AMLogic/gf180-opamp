#!/usr/bin/env python3
"""Tests for ci_netlist_check.py. Needs xschem and the gf180mcu PDK for the
export cases (skipped, or failed under SIM_REQUIRE_PREREQS=1, if xschem is
absent); the validator cases need nothing. Writes only to temp dirs."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_netlist_check as c  # noqa: E402

HAVE = shutil.which("xschem") is not None
NEED = os.environ.get("SIM_REQUIRE_PREREQS") == "1"
COMMITTED = c.NETLIST.read_text()


def needs_xschem(f):
    if NEED:
        return f
    return unittest.skipUnless(HAVE, "xschem not installed")(f)


class Validator(unittest.TestCase):
    def test_committed_ok(self):
        c.validate_export(COMMITTED)

    def test_empty(self):
        with self.assertRaises(c.CheckError):
            c.validate_export("  \n")

    def test_empty_subckt(self):
        with self.assertRaises(c.CheckError) as cm:
            c.validate_export(".subckt a x y\n.ends\n.end\n")
        self.assertIn("empty", str(cm.exception))

    def test_truncated(self):
        with self.assertRaises(c.CheckError):
            c.validate_export(COMMITTED.rsplit(".end", 1)[0])

    def test_missing_symbol(self):
        with self.assertRaises(c.CheckError):
            c.validate_export(COMMITTED + "*  M3 -  foo  IS MISSING !!!!\n")

    def test_normalize_only_sch_path(self):
        a = "** sch_path: /a/b.sch\nX1 a b nfet L=1u\n"
        b = "** sch_path: /c/d.sch\nX1 a b nfet L=1u\n"
        self.assertEqual(c.normalize(a), c.normalize(b))
        self.assertNotEqual(c.normalize(a), c.normalize(b.replace("1u", "2u")))
        self.assertNotEqual(c.normalize(a), c.normalize(a.replace("a b", "b a")))


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nlcheck-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Private copy of design/ (xschemrc derives the repo root from itself).
        shutil.copytree(c.REPO / "design", self.tmp / "design",
                        ignore=shutil.ignore_patterns("netlist"))
        self.sch = self.tmp / "design" / "opamp_two_stage.sch"

    def run_check(self, **kw):
        args = ["--sch", str(kw.get("sch", self.sch))]
        if "xschem" in kw:
            args += ["--xschem", kw["xschem"]]
        return c.main(args)

    def edit(self, old, new):
        s = self.sch.read_text()
        self.assertIn(old, s)
        self.sch.write_text(s.replace(old, new, 1))

    @needs_xschem
    def test_committed_pair_ok_from_other_path(self):
        # Different checkout path => different sch_path comment, still OK.
        self.assertEqual(self.run_check(), 0)

    @needs_xschem
    def test_width_edit_fails(self):
        self.edit("name=M6 model=pfet_03v3 W=72u", "name=M6 model=pfet_03v3 W=80u")
        self.assertEqual(self.run_check(), 1)

    @needs_xschem
    def test_connectivity_edit_fails(self):
        self.edit("{name=l62 lab=n2}", "{name=l62 lab=n1}")
        self.assertEqual(self.run_check(), 1)

    @needs_xschem
    def test_regenerating_fixes(self):
        self.edit("name=M6 model=pfet_03v3 W=72u", "name=M6 model=pfet_03v3 W=80u")
        out = self.tmp / "regen"
        out.mkdir()
        fresh, _ = c.export_schematic("xschem", self.sch, out)
        new = self.tmp / "new.spice"
        new.write_text(fresh)
        self.assertEqual(c.main(["--sch", str(self.sch), "--netlist", str(new)]), 0)

    @needs_xschem
    def test_missing_symbol_fails(self):
        self.edit("symbols/pfet_03v3.sym", "symbols/nonexistent_03v3.sym")
        self.assertEqual(self.run_check(), 1)

    def test_missing_tool_fails(self):
        self.assertEqual(self.run_check(xschem="no-such-xschem-binary"), 2)

    def test_failed_export_fails(self):
        fake = self.tmp / "fake-xschem"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(0o755)
        self.assertEqual(self.run_check(xschem=str(fake)), 1)

    def test_exit10_without_output_fails(self):
        fake = self.tmp / "fake-xschem"
        fake.write_text("#!/bin/sh\nexit 10\n")
        fake.chmod(0o755)
        self.assertEqual(self.run_check(xschem=str(fake)), 1)

    def test_empty_export_fails(self):
        fake = self.tmp / "fake-xschem"
        fake.write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && d=$2; shift; done\n'
            ': > "$d/opamp_two_stage.spice"\nexit 10\n')
        fake.chmod(0o755)
        self.assertEqual(self.run_check(xschem=str(fake)), 1)


if __name__ == "__main__":
    unittest.main()
