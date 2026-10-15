#!/usr/bin/env python3
"""Fail loudly when a sim/selftest.sh prerequisite is missing (issue #52).

Checks klt, ngspice, xschem and the gf180mcu PDK, naming each missing item and the
place that was searched. The PDK that find_pdk() selects must also carry the
pinned open_pdks revision: its SOURCES file is read and the open_pdks hash
compared (full hash, exact match) against the expected revision. A mismatch,
a missing SOURCES file or a SOURCES file without an open_pdks line (unknown
provenance) is a failure with expected/actual diagnostics. This is what makes
a restored PDK cache trustworthy: the cache key alone does not verify the
restored contents.

xschem must also be the pinned release (ci_netlist_check.PINNED_XSCHEM_VERSION,
the one that exported design/netlist/): `xschem --version` is parsed and
compared exactly; another release, or unparseable output, is a failure with
expected/actual diagnostics. If $SELFTEST_XSCHEM_VERSION is set (the workflow
pin) it must equal PINNED_XSCHEM_VERSION.

Expected revision: --expect-rev, else $GF180_PDK_REV, else harness.PINNED_PDK_REV.
If $GF180_PDK_REV is set it must equal harness.PINNED_PDK_REV (so the
workflow pin and the harness pin cannot drift apart silently).

No simulator is run; nothing is written.

    python3 sim/ci_prereqs.py              # tools + xschem version + PDK + revision
    python3 sim/ci_prereqs.py --pdk-only   # PDK + revision only
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_netlist_check import PINNED_XSCHEM_VERSION, xschem_version_problem  # noqa: E402
from harness import DEFAULT_VARIANT, PINNED_PDK_REV, Pdk, PdkNotFound, find_pdk  # noqa: E402

FULL_HASH = re.compile(r"^[0-9a-f]{40}$")


def pdk_revision_problem(pdk: Pdk, expected: str) -> str | None:
    """Return a diagnostic if the selected PDK is not at `expected`, else None."""
    sources = pdk.path / "SOURCES"
    where = f"{pdk.path} (resolved: {pdk.path.resolve()}, via {pdk.source})"
    if not sources.is_file():
        return (
            f"gf180mcu PDK revision unknown: no SOURCES file at {sources}\n"
            f"  expected open_pdks {expected}\n"
            f"  actual   <unknown provenance>\n"
            f"  PDK      {where}"
        )
    actual = pdk.version
    if actual == "unknown":
        return (
            f"gf180mcu PDK revision unknown: {sources} has no 'open_pdks <hash>' line\n"
            f"  expected open_pdks {expected}\n"
            f"  actual   <unknown provenance>\n"
            f"  PDK      {where}"
        )
    if actual != expected:
        return (
            f"gf180mcu PDK revision mismatch (from {sources})\n"
            f"  expected open_pdks {expected}\n"
            f"  actual   open_pdks {actual}\n"
            f"  PDK      {where}\n"
            f"  fix: volare enable --pdk gf180mcu {expected}"
        )
    return None


def expected_revision(cli: str | None) -> tuple[str | None, str | None]:
    """Return (expected_rev, problem)."""
    env = os.environ.get("GF180_PDK_REV")
    if env and env != PINNED_PDK_REV:
        return None, (
            "GF180_PDK_REV disagrees with sim/harness.py PINNED_PDK_REV\n"
            f"  GF180_PDK_REV  {env}\n"
            f"  PINNED_PDK_REV {PINNED_PDK_REV}"
        )
    expected = cli or env or PINNED_PDK_REV
    if not FULL_HASH.match(expected):
        return None, f"expected PDK revision must be a full 40-hex open_pdks hash, got {expected!r}"
    return expected, None


def xschem_pin_problem(exe: str) -> str | None:
    """Return a diagnostic unless `exe` is the pinned xschem (and env agrees)."""
    env = os.environ.get("SELFTEST_XSCHEM_VERSION")
    if env and env != PINNED_XSCHEM_VERSION:
        return (
            "SELFTEST_XSCHEM_VERSION disagrees with sim/ci_netlist_check.py PINNED_XSCHEM_VERSION\n"
            f"  SELFTEST_XSCHEM_VERSION {env}\n"
            f"  PINNED_XSCHEM_VERSION   {PINNED_XSCHEM_VERSION}"
        )
    return xschem_version_problem(exe, PINNED_XSCHEM_VERSION)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdk-only", action="store_true", help="skip the klt/ngspice/xschem PATH and version checks")
    ap.add_argument("--expect-rev", help="expected open_pdks full hash (default: $GF180_PDK_REV or the harness pin)")
    args = ap.parse_args(argv)

    problems: list[str] = []
    if not args.pdk_only:
        path_env = os.environ.get("PATH", "")
        for tool in ("klt", "ngspice", "xschem"):
            found = shutil.which(tool)
            if not found:
                problems.append(f"{tool} not found on PATH (searched: {path_env})")
                continue
            if tool == "xschem":
                bad = xschem_pin_problem(found)
                if bad:
                    problems.append(bad)
                    continue
                print(f"ok: xschem -> {found} (XSCHEM V{PINNED_XSCHEM_VERSION} == pinned)")
            else:
                print(f"ok: {tool} -> {found}")

    expected, rev_problem = expected_revision(args.expect_rev)
    if rev_problem:
        problems.append(rev_problem)

    try:
        pdk = find_pdk()
    except PdkNotFound as e:
        variant = os.environ.get("PDK", DEFAULT_VARIANT)
        searched = [
            f"$GF180_PDK_PATH={os.environ.get('GF180_PDK_PATH', '<unset>')}",
            f"$PDK_ROOT/{variant} (PDK_ROOT={os.environ.get('PDK_ROOT', '<unset>')})",
            f"~/.volare/{variant} (= {Path('~').expanduser() / '.volare' / variant})",
        ]
        problems.append("gf180mcu PDK not found (searched: " + "; ".join(searched) + f")\n{e}")
    else:
        if expected is not None:
            bad = pdk_revision_problem(pdk, expected)
            if bad:
                problems.append(bad)
            else:
                print(
                    f"ok: gf180mcu PDK {pdk.variant} -> {pdk.path} "
                    f"(resolved {pdk.path.resolve()}, via {pdk.source}, open_pdks {pdk.version} == expected)"
                )

    for p in problems:
        print(f"MISSING PREREQUISITE: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
