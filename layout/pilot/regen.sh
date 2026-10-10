#!/usr/bin/env bash
# Regenerate the layout device pilot (layout/pilot/) from the schematic netlist.
#   usage: layout/pilot/regen.sh
# Verifies pinned tool / PDK identities first and refuses to run on a mismatch.
# Needs on PATH: klt, uv.  Installs nothing host-wide (uv builds a throwaway env).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KLT_PIN="klt 0.7.0+g5e5b55992a7f"      # klayout-tools build (git 5e5b55992a7f)
KLAYOUT_PIN="0.30.12"                  # klayout python (cell labelling; same as klt's own engine)
KLAYOUT_BIN_PIN="KLayout 0.28.16"      # system klayout binary the foundry run_drc.py/run_lvs.py shell out to
DOCOPT_PIN="0.6.2"                     # run_drc.py / run_lvs.py CLI dependency
PDK_NAME="gf180mcuD"
PDK_PIN="open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b"

got_klt="$(klt --version)"
[ "$got_klt" = "$KLT_PIN" ] || { echo "klt identity mismatch: got '$got_klt', want '$KLT_PIN'" >&2; exit 2; }
got_pdk="$(klt pdk list 2>/dev/null | awk -v n="$PDK_NAME" '$1==n {print $2" "$3; exit}')"
[ "$got_pdk" = "$PDK_PIN" ] || { echo "PDK identity mismatch: got '$got_pdk', want '$PDK_PIN'" >&2; exit 2; }
got_bin="$(klayout -v)"
[ "$got_bin" = "$KLAYOUT_BIN_PIN" ] || { echo "klayout binary mismatch: got '$got_bin', want '$KLAYOUT_BIN_PIN'" >&2; exit 2; }
[ -d "$HOME/.volare/$PDK_NAME" ] || { echo "PDK not under ~/.volare/$PDK_NAME" >&2; exit 2; }

# Single-process, flat, mp=1: modest on the shared host. No ngspice is run.
exec uv run --no-project --with "klayout==$KLAYOUT_PIN" --with "docopt==$DOCOPT_PIN" \
  python -I "$HERE/tools/pilot.py"
