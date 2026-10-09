#!/usr/bin/env python3
"""Fail loudly when a sim/selftest.sh prerequisite is missing (issue #52).

Checks klt, ngspice and the gf180mcu PDK, naming each missing item and the
place that was searched. No simulator is run; nothing is written.

    python3 sim/ci_prereqs.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import DEFAULT_VARIANT, PdkNotFound, find_pdk  # noqa: E402


def main() -> int:
    problems: list[str] = []
    path_env = os.environ.get("PATH", "")
    for tool in ("klt", "ngspice"):
        found = shutil.which(tool)
        if found:
            print(f"ok: {tool} -> {found}")
        else:
            problems.append(f"{tool} not found on PATH (searched: {path_env})")
    try:
        pdk = find_pdk()
        print(f"ok: gf180mcu PDK {pdk.variant} -> {pdk.path} (via {pdk.source}, open_pdks {pdk.version})")
    except PdkNotFound as e:
        variant = os.environ.get("PDK", DEFAULT_VARIANT)
        searched = [
            f"$GF180_PDK_PATH={os.environ.get('GF180_PDK_PATH', '<unset>')}",
            f"$PDK_ROOT/{variant} (PDK_ROOT={os.environ.get('PDK_ROOT', '<unset>')})",
            f"~/.volare/{variant} (= {Path('~').expanduser() / '.volare' / variant})",
        ]
        problems.append("gf180mcu PDK not found (searched: " + "; ".join(searched) + f")\n{e}")
    for p in problems:
        print(f"MISSING PREREQUISITE: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
