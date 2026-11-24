#!/usr/bin/env bash
# Regenerate the output-stage sub-block (layout/output_stage/) from the schematic netlist.
#   usage: layout/output_stage/regen.sh
# Tool/PDK pin checks and the pinned uv environment live in layout/_regen.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$HERE/../_regen.sh" "$HERE/tools/output_stage.py"
