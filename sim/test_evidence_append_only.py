#!/usr/bin/env python3
"""Tests for ci_evidence_append_only.py using temporary git repos (no simulator)."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_evidence_append_only as g  # noqa: E402

KINDS = [
    "sim/noise/records/r1.md",
    "sim/noise/records/r1-plots/p.png",
    "sim/noise/corners/r1/c.cir",
    "sim/noise/netlist-snapshots/r1.cir",
    "sim/cmrr-mc/probes/p.json",
    "sim/gain-gbw-pm/fixed-vcm/records/r2.md",
    "sim/gain-gbw-pm/fixed-vcm/corners/r2/c.cir",
]


class Repo:
    def __init__(self, tmp):
        self.p = Path(tmp)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.p), *a], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, text="x\n"):
        f = self.p / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def commit(self, msg="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)


def run(repo, *args, allow=None):
    argv = ["--repo", str(repo.p), *args]
    al = repo.p / "allow.txt"
    al.write_text(allow or "")
    argv += ["--allowlist", str(al)]
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = g.main(argv)
    return rc, err.getvalue()


class T(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.r = Repo(self._t.name)
        for k in KINDS:
            self.r.write(k)
        self.r.write("sim/noise/run_noise.py", "a\n")
        self.r.commit("base")
        self.r.git("branch", "base")
        self.r.git("checkout", "-q", "-b", "pr")

    def test_add_only_and_non_evidence_pass(self):
        self.r.write("sim/noise/records/r9.md")
        self.r.write("sim/gain-gbw-pm/fixed-vcm/records/r9.md")
        self.r.write("sim/noise/run_noise.py", "b\n")
        self.r.write("sim/noise/testbench/tb.spice")
        self.r.commit()
        rc, err = run(self.r, "--base", "base")
        self.assertEqual(rc, 0, err)

    def test_modify_each_kind_fails_naming_file(self):
        for k in KINDS:
            with self.subTest(k):
                self.r.write(k, "changed\n")
                self.r.commit()
                rc, err = run(self.r, "--base", "base")
                self.assertEqual(rc, 1)
                self.assertIn(f"M\t{k}", err)
                self.r.git("reset", "-q", "--hard", "base")

    def test_delete_fails(self):
        (self.r.p / KINDS[0]).unlink()
        self.r.commit()
        rc, err = run(self.r, "--base", "base")
        self.assertEqual(rc, 1)
        self.assertIn(f"D\t{KINDS[0]}", err)

    def test_rename_fails_on_delete(self):
        self.r.git("mv", KINDS[0], "sim/noise/records/renamed.md")
        self.r.commit()
        rc, err = run(self.r, "--base", "base")
        self.assertEqual(rc, 1)
        self.assertIn(f"D\t{KINDS[0]}", err)

    def test_allowlist_exempts_exact_path_only(self):
        self.r.write(KINDS[0], "changed\n")
        self.r.write(KINDS[1], "changed\n")
        self.r.commit()
        rc, err = run(self.r, "--base", "base", allow=f"# c\n{KINDS[0]}  #159\n")
        self.assertEqual(rc, 1)
        self.assertNotIn(KINDS[0], err)
        self.assertIn(KINDS[1], err)
        rc, _ = run(self.r, "--base", "base",
                    allow=f"{KINDS[0]} #159\n{KINDS[1]} #160\n")
        self.assertEqual(rc, 0)

    def test_malformed_allowlist_fails(self):
        for bad in (f"{KINDS[0]}\n", f"{KINDS[0]} issue\n", "sim/a b #1\n"):
            rc, err = run(self.r, "--base", "base", allow=bad)
            self.assertEqual(rc, 1, bad)
            self.assertIn("malformed", err)

    def test_push_modes(self):
        before = self.r.git("rev-parse", "HEAD")
        self.r.write("sim/noise/records/r9.md")
        self.r.commit()
        self.r.write(KINDS[0], "changed\n")
        self.r.commit()
        self.assertEqual(run(self.r, "--before", before)[0], 1)  # multi-commit push
        self.assertEqual(run(self.r)[0], 1)                       # HEAD~1..HEAD
        zeros = "0" * 40
        self.assertEqual(run(self.r, "--before", zeros)[0], 1)    # new branch -> HEAD~1

    def test_merge_commit_head_vs_first_parent(self):
        self.r.write(KINDS[0], "changed\n")
        self.r.commit()
        self.r.git("checkout", "-q", "main")
        self.r.git("merge", "-q", "--no-ff", "pr", "-m", "merge")
        self.assertEqual(run(self.r)[0], 1)

    def test_root_commit_passes(self):
        with tempfile.TemporaryDirectory() as d:
            r = Repo(d)
            r.write(KINDS[0])
            r.commit()
            self.assertEqual(run(r)[0], 0)

    def test_missing_history_fails_visibly(self):
        rc, err = run(self.r, "--base", "nonexistent")
        self.assertEqual(rc, 2)
        self.assertIn("cannot compare", err)
        rc, _ = run(self.r, "--before", "1" * 40)
        self.assertEqual(rc, 2)

    def test_shallow_clone_without_parent_fails(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "s"
            subprocess.run(["git", "clone", "-q", "--depth", "1",
                            f"file://{self.r.p}", str(dest)], check=True,
                           capture_output=True)
            s = Repo.__new__(Repo)
            s.p = dest
            self.assertEqual(run(s)[0], 2)

    def test_matcher(self):
        for k in KINDS:
            self.assertTrue(g.is_evidence(k), k)
        for n in ("sim/report/selection.json", "sim/noise/run_noise.py",
                  "sim/noise/testbench/tb.spice", "sim/README.md",
                  "sim/reports/x.md", "spec/records/x.md"):
            self.assertFalse(g.is_evidence(n), n)

    def test_committed_allowlist_is_valid(self):
        allowed, errors = g.load_allowlist(g.ALLOWLIST)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
