#!/usr/bin/env bash
# Regenerate the layout device pilot (layout/pilot/) from the schematic netlist.
#   usage: layout/pilot/regen.sh
# Tool/PDK pin checks and the pinned uv environment live in layout/_regen.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$HERE/../_regen.sh" "$HERE/tools/pilot.py"
