#!/usr/bin/env bash
# Regenerate the layout device pilot (layout/pilot/) from the schematic netlist.
#   usage: layout/pilot/regen.sh
# Verifies pinned tool / PDK identities first and refuses to run on a mismatch.
# Needs on PATH: klt (at KLT_PIN), uv.  Installs nothing host-wide (uv builds a throwaway env).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KLT_PIN="klt 0.8.0"                    # klayout-tools release (uvx --from klayout-tools==0.8.0)
KLAYOUT_PIN="0.30.12"                  # klayout python (cell labelling; same as klt's own engine)
PDK_NAME="gf180mcuD"
PDK_PIN="open_pdks f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"

got_klt="$(klt --version)"
[ "$got_klt" = "$KLT_PIN" ] || { echo "klt identity mismatch: got '$got_klt', want '$KLT_PIN'" >&2; exit 2; }
got_pdk="$(klt pdk list 2>/dev/null | awk -v n="$PDK_NAME" '$1==n {print $2" "$3; exit}')"
[ "$got_pdk" = "$PDK_PIN" ] || { echo "PDK identity mismatch: got '$got_pdk', want '$PDK_PIN'" >&2; exit 2; }
[ -d "$HOME/.ciel/$PDK_NAME" ] || { echo "PDK not under ~/.ciel/$PDK_NAME" >&2; exit 2; }

# Single-process, flat, mp=1: modest on the shared host. No ngspice is run.
exec uv run --no-project --with "klayout==$KLAYOUT_PIN" \
  python -I "$HERE/tools/pilot.py"
