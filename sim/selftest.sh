#!/usr/bin/env bash
# sim/selftest.sh -- fast smoke check: confirm each sim/ testbench still
# converges to a sane (typical, 27C) operating point, without running the
# full 45-point PVT grid or writing any append-only record.
#
# Mirrors gf180-comparator's sim/selftest.sh (the fast counterpart to
# sim/characterize.sh's full run). Intended for a quick "did I break the
# testbench" check, e.g. after editing a testbench fragment, not as an
# evidence-generating run.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== sim/gain-gbw-pm: extraction + source-guard tests (no simulator) =="
python3 gain-gbw-pm/test_gain_gbw_pm.py

echo "== sim/gain-gbw-pm: smoke test (typical, 27C, 3.30 V, one local point) =="
python3 gain-gbw-pm/run_gain_gbw_pm.py --smoke

echo "== sim/slew-swing-power: extraction + source-guard tests (no simulator) =="
python3 slew-swing-power/test_slew_swing_power.py

echo "== sim/slew-swing-power: smoke test (typical, 27C, 3.30 V, one local point per figure) =="
python3 slew-swing-power/run_slew_swing_power.py --smoke
