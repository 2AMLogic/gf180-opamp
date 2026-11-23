#!/usr/bin/env bash
# Regenerate the layout device pilot (layout/pilot/) from the schematic netlist.
#   usage: layout/pilot/regen.sh
# Verifies pinned tool / PDK identities first and refuses to run on a mismatch.
# Needs on PATH: klt (at KLT_PIN), uv.  Installs nothing host-wide (uv builds a throwaway env).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KLT_PIN="klt 0.9.3.dev3808+g1ad4a837f657"  # klayout-tools at commit 1ad4a837f657 (has diff_pair pitch_x_um, klt#6242; no release yet)
KLAYOUT_PIN="0.30.12"                  # klayout python: cell labelling AND klt's own engine
export PILOT_KLAYOUT_PIN="$KLAYOUT_PIN"  # tools/pilot.py enforces it on the klt below
PDK_NAME="gf180mcuD"
PDK_PIN="open_pdks f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"

got_klt="$(klt --version)"
[ "$got_klt" = "$KLT_PIN" ] || { echo "klt identity mismatch: got '$got_klt', want '$KLT_PIN'" >&2; exit 2; }
got_pdk="$(klt pdk list 2>/dev/null | awk -v n="$PDK_NAME" '$1==n {print $2" "$3; exit}')"
[ "$got_pdk" = "$PDK_PIN" ] || { echo "PDK identity mismatch: got '$got_pdk', want '$PDK_PIN'" >&2; exit 2; }
[ -d "$HOME/.ciel/$PDK_NAME" ] || { echo "PDK not under ~/.ciel/$PDK_NAME" >&2; exit 2; }

# The klt on PATH resolves its *own* klayout dependency, which installing
# klayout-tools alone leaves unpinned -- a newer engine rewrites the GDS
# timestamps and every content hash in evidence/. pilot.py checks klt's reported
# engine against PILOT_KLAYOUT_PIN and refuses the run on a mismatch, so install
# klayout=="$KLAYOUT_PIN" into the same environment as klt.
# Single-process, flat, mp=1: modest on the shared host. No ngspice is run.
exec uv run --no-project --with "klayout==$KLAYOUT_PIN" \
  python -I "$HERE/tools/pilot.py"
