#!/usr/bin/env python3
"""CI check for artifact-store evidence manifests (issue #271). Stdlib only.

Checks every `manifests/artifacts/*.manifest.json`:

* schema: `{"schema_version": 1, "entries": [...]}`, every entry with a
  sha256, a positive size, `kind: evidence`, a producer, a unique `name`, and
  a `uri` that starts with `artifact:` and ends in the entry's sha256 (so a
  manifest never names a bucket, an ARN or a presigned URL);
* the manifest text contains no `s3://` or `arn:aws:`;
* no stored object's bytes are also tracked in git (matched by size, then
  sha256, over the tracked files): evidence bytes live in the store, not here.

It needs neither klt nor the broker: `klt artifact verify <manifest>` is a
worker-side check, not a CI one.

Usage: python3 sim/ci_artifact_manifests.py [--repo-root DIR]
Exit 0 clean, 1 on any problem, 2 on a usage/environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_store import MANIFEST_DIR, entry_problem, sha256_file  # noqa: E402

FORBIDDEN = ("s3://", "arn:aws:")


def manifest_files(repo_root: Path) -> list[Path]:
    return sorted((repo_root / MANIFEST_DIR).glob("*.manifest.json"))


def check_manifest(path: Path) -> tuple[list[str], list[dict]]:
    """Return `(problems, entries)` for one manifest file."""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        doc = json.loads(text)
    except (OSError, ValueError) as exc:
        return [f"{path.name}: unreadable or not JSON ({exc})"], []
    for bad in FORBIDDEN:
        if bad in text:
            problems.append(f"{path.name}: contains `{bad}` (a manifest names no storage location)")
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or doc.get("schema_version") != 1:
        return problems + [f"{path.name}: not {{schema_version: 1, entries: [...]}}"], []
    if not entries:
        problems.append(f"{path.name}: no entries")
    seen: set[str] = set()
    good: list[dict] = []
    for i, e in enumerate(entries):
        why = entry_problem(e)
        if why:
            problems.append(f"{path.name}: entry {i}: {why}")
            continue
        if e["name"] in seen:
            problems.append(f"{path.name}: duplicate name {e['name']}")
        seen.add(e["name"])
        good.append(e)
    return problems, good


def tracked_files(repo_root: Path) -> list[Path]:
    p = subprocess.run(["git", "-C", str(repo_root), "ls-files", "-z"], capture_output=True, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {p.stderr.decode(errors='replace').strip()}")
    return [repo_root / n.decode() for n in p.stdout.split(b"\0") if n]


def tracked_collisions(entries: list[dict], files: list[Path]) -> list[str]:
    """`name` of every entry whose bytes (size + sha256) match a tracked file."""
    by_size: dict[int, dict[str, str]] = {}
    for e in entries:
        by_size.setdefault(e["size"], {})[e["sha256"]] = e["name"]
    out = []
    for f in files:
        try:
            if f.is_symlink() or not f.is_file():
                continue
            hashes = by_size.get(f.stat().st_size)
            if not hashes:
                continue
            sha, _ = sha256_file(f)
        except OSError:
            continue
        if sha in hashes:
            out.append(f"{hashes[sha]}: its bytes are tracked in git at {f}")
    return out


def run(repo_root: Path) -> list[str]:
    problems: list[str] = []
    all_entries: list[dict] = []
    for m in manifest_files(repo_root):
        p, entries = check_manifest(m)
        problems += p
        all_entries += entries
    if all_entries:
        problems += tracked_collisions(all_entries, tracked_files(repo_root))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args(argv)
    try:
        problems = run(args.repo_root.resolve())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    n = len(manifest_files(args.repo_root.resolve()))
    print(f"artifact manifests: {n} checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
