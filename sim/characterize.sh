#!/usr/bin/env bash
# sim/characterize.sh -- one-command driver: regenerate every sim/ testbench's
# full PVT-cornered evidence from a cold checkout.
#
# Mirrors gf180-comparator's sim/characterize.sh + sim/selftest.sh split
# (full run vs. fast smoke check) -- see sim/README.md's "Experiments" list
# and each experiment's own README for the cold-start prerequisites (ngspice,
# the pinned gf180mcu PDK revision, numpy/matplotlib).
#
# Currently runs two experiments (sim/gain-gbw-pm/: open-loop gain / GBW /
# phase margin of the committed sized schematic over the full 45-point PVT
# grid, issues #19 and #38; sim/slew-swing-power/: slew rate, output swing and
# quiescent power over the same grid, issue #44); sim/gm-id-characterization/ predates this script
# and is still run directly (`python3 sim/gm-id-characterization/run_gmid.py`)
# per its own README, since this script's job is the *one-command driver*
# acceptance criterion for the newly-added spec-row testbenches, not a
# retroactive wrapper for the pre-existing device study. Future spec-row
# testbenches should add their own `run_<name>.py --smoke`-shaped driver and
# a line below.
#
# Each run mints a new append-only record under the experiment's own
# records/ -- nothing here overwrites a previous run.
#
# Where the 45-point grid executes is `klt sim`'s decision (the request's
# backend, or $KLT_SIM_BACKEND -- the Spot batch fleet on a dispatch worker).
# Extra arguments are forwarded to the driver, e.g.
#   ./characterize.sh --backend local
#   ./characterize.sh --batch-submit-retries 40 --batch-runner-version-check warn

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== sim/gain-gbw-pm: full 45-point PVT grid (5 MOS corners x 3 T x 3 VDD) =="
python3 gain-gbw-pm/run_gain_gbw_pm.py "$@"

echo "== sim/slew-swing-power: slew / swing / quiescent power, full 45-point PVT grid x 3 figures =="
python3 slew-swing-power/run_slew_swing_power.py "$@"
