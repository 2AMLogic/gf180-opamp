#!/usr/bin/env python3
"""Tests for ci_netlist_check.py and the xschem version pin in ci_prereqs.py.
Needs xschem (the pinned release) and the gf180mcu PDK for the export cases
(skipped, or failed under SIM_REQUIRE_PREREQS=1, if xschem is absent); the
validator and version-pin cases use fake xschem scripts and need nothing.
Writes only to temp dirs."""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_netlist_check as c  # noqa: E402
import ci_prereqs  # noqa: E402

HAVE = shutil.which("xschem") is not None
NEED = os.environ.get("SIM_REQUIRE_PREREQS") == "1"
COMMITTED = c.NETLIST.read_text()
PIN = c.PINNED_XSCHEM_VERSION
WORKFLOW = c.REPO / ".github" / "workflows" / "selftest.yml"


def write_fake(path: Path, body: str, version: str | None = PIN) -> str:
    """Executable fake xschem: answers --version with `version`, else runs body."""
    banner = f"XSCHEM V{version}" if version else "not an xschem banner"
    path.write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" --version "*) echo "{banner}"; '
        'echo "Copyright (C) 1998-2024 Stefan Schippers"; exit 0;; esac\n' + body)
    path.chmod(0o755)
    return str(path)


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


class XschemVersion(unittest.TestCase):
    """The exporter version is pinned and asserted (no xschem needed)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nlcheck-ver-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_parse(self):
        self.assertEqual(c.parse_xschem_version(
            "XSCHEM V3.4.7\nCopyright (C) 1998-2024 Stefan Schippers\n"), "3.4.7")
        self.assertEqual(c.parse_xschem_version("XSCHEM V3.4.4\n"), "3.4.4")
        self.assertIsNone(c.parse_xschem_version("xschem: unknown option\n"))
        self.assertIsNone(c.parse_xschem_version(""))

    def test_match_passes(self):
        fake = write_fake(self.tmp / "xs", "exit 0\n")
        self.assertIsNone(c.xschem_version_problem(fake))
        self.assertIsNone(ci_prereqs.xschem_pin_problem(fake))

    def test_mismatch_fails_clearly(self):
        fake = write_fake(self.tmp / "xs", "exit 0\n", version="3.4.4")
        msg = c.xschem_version_problem(fake)
        self.assertIn("mismatch", msg)
        self.assertIn(f"expected XSCHEM V{PIN}", msg)
        self.assertIn("actual   XSCHEM V3.4.4", msg)
        self.assertEqual(ci_prereqs.xschem_pin_problem(fake), msg)

    def test_unparseable_fails(self):
        fake = write_fake(self.tmp / "xs", "exit 0\n", version=None)
        self.assertIn("version unknown", c.xschem_version_problem(fake))

    def test_mismatch_stops_check_before_export(self):
        # A wrong xschem must fail as a version error (exit 2), not run the
        # export and fail as a diff. The body would leave a marker if run.
        marker = self.tmp / "exported"
        fake = write_fake(self.tmp / "xs", f': > "{marker}"\nexit 10\n', version="3.4.4")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = c.main(["--xschem", fake])
        self.assertEqual(rc, 2)
        self.assertIn("xschem version mismatch", err.getvalue())
        self.assertFalse(marker.exists())

    def test_env_pin_must_agree(self):
        fake = write_fake(self.tmp / "xs", "exit 0\n")
        with mock.patch.dict(os.environ, {"SELFTEST_XSCHEM_VERSION": "3.4.4"}):
            self.assertIn("disagrees", ci_prereqs.xschem_pin_problem(fake))
        with mock.patch.dict(os.environ, {"SELFTEST_XSCHEM_VERSION": PIN}):
            self.assertIsNone(ci_prereqs.xschem_pin_problem(fake))

    def test_workflow_pins_same_version_and_no_apt_xschem(self):
        wf = WORKFLOW.read_text()
        m = re.search(r'^\s*SELFTEST_XSCHEM_VERSION:\s*"([^"]+)"', wf, re.M)
        self.assertIsNotNone(m, "selftest.yml must set SELFTEST_XSCHEM_VERSION")
        self.assertEqual(m.group(1), PIN)
        self.assertRegex(wf, re.compile(r'^\s*XSCHEM_COMMIT:\s*"[0-9a-f]{40}"', re.M),
                         "selftest.yml must pin xschem to a full commit hash")
        for ln in wf.splitlines():
            if "apt-get install" in ln:
                self.assertNotRegex(ln, r"\bxschem\b", "xschem must not come from apt")

    @needs_xschem
    def test_installed_xschem_is_pinned(self):
        self.assertIsNone(c.xschem_version_problem("xschem"))


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
        fake = write_fake(self.tmp / "fake-xschem", "exit 3\n")
        self.assertEqual(self.run_check(xschem=fake), 1)

    def test_exit10_without_output_fails(self):
        fake = write_fake(self.tmp / "fake-xschem", "exit 10\n")
        self.assertEqual(self.run_check(xschem=fake), 1)

    def test_empty_export_fails(self):
        fake = write_fake(
            self.tmp / "fake-xschem",
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && d=$2; shift; done\n'
            ': > "$d/opamp_two_stage.spice"\nexit 10\n')
        self.assertEqual(self.run_check(xschem=fake), 1)


if __name__ == "__main__":
    unittest.main()
