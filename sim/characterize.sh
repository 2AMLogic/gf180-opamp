#!/usr/bin/env bash
# sim/characterize.sh -- one-command driver: regenerate every sim/ testbench's
# full PVT-cornered evidence from a cold checkout.
#
# Mirrors gf180-comparator's sim/characterize.sh + sim/selftest.sh split
# (full run vs. fast smoke check) -- see sim/README.md's "Experiments" list
# and each experiment's own README for the cold-start prerequisites (ngspice,
# the pinned gf180mcu PDK revision, numpy/matplotlib).
#
# Currently runs one experiment (sim/gain-gbw-pm/, issue #19 -- the repo's
# first spec-row testbench); sim/gm-id-characterization/ predates this script
# and is still run directly (`python3 sim/gm-id-characterization/run_gmid.py`)
# per its own README, since this script's job is the *one-command driver*
# acceptance criterion for the newly-added spec-row testbenches, not a
# retroactive wrapper for the pre-existing device study. Future spec-row
# testbenches should add their own `run_<name>.py --smoke`-shaped driver and
# a line below.
#
# Each run mints a new append-only record under the experiment's own
# records/ -- nothing here overwrites a previous run.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== sim/gain-gbw-pm: full 15-point PVT sweep =="
python3 gain-gbw-pm/run_gain_gbw_pm.py
