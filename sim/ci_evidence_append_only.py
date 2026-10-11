#!/usr/bin/env python3
"""Fail if a change modifies or deletes committed sim/ evidence (issue #159).

CLAUDE.md: "sim/ results are append-only evidence".  harness.py enforces that
at write time only; this guard enforces it on the committed history.

Evidence = any file under sim/<exp>/ with a directory component named
records, corners, netlist-snapshots or probes (at any depth, so
sim/gain-gbw-pm/fixed-vcm/records/... is covered).  Only status A (added) is
allowed.  --no-renames is used, so a rename shows as D+A and fails on the D.

Comparison range (stdlib only, no simulator, no credentials):
  --base REF     PR mode: git diff REF...HEAD (merge-base of REF and HEAD)
  --before SHA   push mode: git diff SHA..HEAD (the push event's "before");
                 an all-zero SHA (new branch) falls back to HEAD~1
  neither        HEAD~1..HEAD; a root commit counts as all adds
A comparison revision that cannot be resolved (shallow clone, missing fetch)
is a hard failure, never a silent pass.

Allow-list (sim/evidence_append_only_allowlist.txt): one exact path per line
followed by the authorizing issue, "sim/x/records/y.md  #123".  Blank lines
and lines starting with '#' are ignored.  An entry without an issue reference
fails the guard.  An empty list is valid.

Exit: 0 clean, 1 violation or bad allow-list, 2 cannot compare.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

EVIDENCE_DIRS = {"records", "corners", "netlist-snapshots", "probes"}
ALLOWLIST = Path(__file__).resolve().parent / "evidence_append_only_allowlist.txt"
ROOT = "<root commit>"
ENTRY_RE = re.compile(r"^(\S+)\s+#(\d+)$")


class CompareError(Exception):
    pass


def is_evidence(path: str) -> bool:
    parts = path.split("/")
    return (len(parts) >= 4 and parts[0] == "sim"
            and any(p in EVIDENCE_DIRS for p in parts[2:-1]))


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise CompareError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def resolves(repo: Path, rev: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q",
                           rev + "^{commit}"], capture_output=True).returncode == 0


def diff_range(repo: Path, base: str | None, before: str | None) -> str:
    if base:
        if not resolves(repo, base):
            raise CompareError(f"base revision {base!r} not found (shallow checkout? fetch it)")
        return f"{base}...HEAD"
    if before and set(before) != {"0"}:
        if not resolves(repo, before):
            raise CompareError(f"before revision {before!r} not found (shallow checkout? "
                               "use fetch-depth: 0)")
        return f"{before}..HEAD"
    if resolves(repo, "HEAD~1"):
        return "HEAD~1..HEAD"
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise CompareError("HEAD has no parent in a shallow clone; use fetch-depth: 0")
    return ROOT  # genuine root commit: everything is an add


def load_allowlist(path: Path) -> tuple[set[str], list[str]]:
    allowed, errors = set(), []
    if not path.exists():
        return allowed, errors
    for n, line in enumerate(path.read_text().splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = ENTRY_RE.match(s)
        if not m:
            errors.append(f"{path.name}:{n}: malformed entry {s!r} "
                          "(want '<exact path>  #<issue>')")
        else:
            allowed.add(m.group(1))
    return allowed, errors


def violations(repo: Path, rng: str, allowed: set[str]) -> list[str]:
    if rng == ROOT:
        out = git(repo, "diff-tree", "--root", "-r", "--no-commit-id", "--name-status",
                  "--no-renames", "-z", "HEAD")
    else:
        out = git(repo, "diff", "--name-status", "--no-renames", "-z", rng)
    toks = out.split("\0")
    bad, i = [], 0
    while i + 1 < len(toks):
        status, path = toks[i], toks[i + 1]
        i += 2
        if is_evidence(path) and status != "A" and path not in allowed:
            bad.append(f"{status}\t{path}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", help="PR mode: base ref, diff BASE...HEAD")
    ap.add_argument("--before", help="push mode: previous tip SHA, diff BEFORE..HEAD")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    a = ap.parse_args(argv)
    repo = Path(a.repo)

    allowed, errors = load_allowlist(Path(a.allowlist))
    if errors:
        print("\n".join(f"ERROR: {e}" for e in errors), file=sys.stderr)
        return 1
    try:
        rng = diff_range(repo, a.base, a.before)
        bad = violations(repo, rng, allowed)
    except CompareError as e:
        print(f"ERROR: cannot compare against history: {e}", file=sys.stderr)
        return 2
    if bad:
        print(f"ERROR: committed sim/ evidence is append-only; changed in {rng}:",
              file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        print("Add a new record instead, or list the exact path with its authorizing "
              "issue in sim/evidence_append_only_allowlist.txt.", file=sys.stderr)
        return 1
    print(f"ok: no evidence file modified or deleted in {rng}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
