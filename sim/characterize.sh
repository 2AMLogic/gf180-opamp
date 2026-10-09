#!/usr/bin/env bash
# sim/characterize.sh -- one-command driver: regenerate every sim/ testbench's
# full PVT-cornered evidence from a cold checkout.
#
# Mirrors gf180-comparator's sim/characterize.sh + sim/selftest.sh split
# (full run vs. fast smoke check) -- see sim/README.md's "Experiments" list
# and each experiment's own README for the cold-start prerequisites (ngspice,
# the pinned gf180mcu PDK revision, numpy/matplotlib).
#
# Currently runs seven experiments (sim/gain-gbw-pm/: open-loop gain / GBW /
# phase margin of the committed sized schematic over the full 45-point PVT
# grid, issues #19 and #38; sim/offset-mc/: mismatch Monte Carlo of the
# input offset, 5 corners x N=300, issue #45; sim/noise/: input-referred
# noise over the same 45-point grid, issue #46; sim/cmrr/ and sim/psrr/:
# common-mode and supply rejection over the same grid, two and three
# excitation requests respectively, issue #39; sim/slew-swing-power/: slew
# rate, output swing and quiescent power over the same grid, one request per
# figure, issue #44; sim/input-common-mode/: follower-biased input common-mode
# range, a VCM scan 0..VDD inside each of the 45 PVT points plus 5 mV
# transition refinement, issue #60); sim/gm-id-characterization/ predates this script
# and is still run directly (`python3 sim/gm-id-characterization/run_gmid.py`)
# per its own README, since this script's job is the *one-command driver*
# acceptance criterion for the newly-added spec-row testbenches, not a
# retroactive wrapper for the pre-existing device study. Future spec-row
# testbenches should add their own `run_<name>.py --smoke`-shaped driver and
# a line below.
#
# After all drivers succeed (set -e), the last step regenerates the aggregate
# characterization report (sim/reports/) from the NEWEST record of each
# experiment (issue #50); it runs no simulator and takes no driver flags.
#
# Extra arguments are forwarded to EVERY driver below, so only flags all
# drivers accept (--backend, --batch-*) belong on the command line.
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

echo "== sim/offset-mc: mismatch Monte Carlo, 5 MOS corners x N=300 (1500 units) =="
python3 offset-mc/run_offset_mc.py "$@"

echo "== sim/noise: input-referred noise, full 45-point PVT grid =="
python3 noise/run_noise.py "$@"

echo "== sim/cmrr: CMRR (differential + common-mode excitations), full 45-point PVT grid =="
python3 cmrr/run_cmrr.py "$@"

echo "== sim/psrr: PSRR+ / PSRR- (differential, vdd and vss excitations), full 45-point PVT grid =="
python3 psrr/run_psrr.py "$@"

echo "== sim/slew-swing-power: slew / swing / quiescent power, full 45-point PVT grid x 3 figures =="
python3 slew-swing-power/run_slew_swing_power.py "$@"

echo "== sim/input-common-mode: follower-biased ICMR, VCM scan 0..VDD per PVT point + 5 mV refinement =="
python3 input-common-mode/run_input_common_mode.py "$@"

echo "== sim/report: aggregate characterization report from the newly minted records =="
python3 report/characterization_report.py --latest --update-manifest
