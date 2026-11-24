#!/usr/bin/env bash
# Regenerate the first-stage sub-block (layout/first_stage/) from the schematic netlist.
#   usage: layout/first_stage/regen.sh
# Tool/PDK pin checks and the pinned uv environment live in layout/_regen.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$HERE/../_regen.sh" "$HERE/tools/first_stage.py"
