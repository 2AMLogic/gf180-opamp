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

echo "== design: committed netlist vs fresh xschem export (scratch dir; issue #76) =="
python3 test_netlist_check.py
python3 ci_netlist_check.py

echo "== design: schematic bias/convergence smoke check (one nominal local ngspice point; writes nothing; issue #96) =="
python3 ../design/check_dc_op.py

echo "== sim/report: characterization report generator tests + staleness check (no simulator) =="
python3 report/test_report.py
python3 report/characterization_report.py --check

echo "== sim: per-experiment measurement-configuration fingerprint modules (no simulator) =="
python3 test_measurement_config.py

echo "== sim/gain-gbw-pm: extraction + source-guard tests (no simulator) =="
python3 gain-gbw-pm/test_gain_gbw_pm.py

echo "== sim/gain-gbw-pm: smoke test (typical, 27C, 3.30 V, one local point) =="
python3 gain-gbw-pm/run_gain_gbw_pm.py --smoke

echo "== sim/offset-mc: statistics + extraction + source-guard tests (one control pair runs locally) =="
python3 offset-mc/test_offset_mc.py

echo "== sim/offset-mc: smoke test (typical, 27C, 3.30 V, one deterministic local unit) =="
python3 offset-mc/run_offset_mc.py --smoke

echo "== sim/noise: extraction + source-guard tests (one nominal unit runs locally) =="
python3 noise/test_noise.py

echo "== sim/noise: smoke test (typical, 27C, 3.30 V, one local point) =="
python3 noise/run_noise.py --smoke

echo "== sim/cmrr: extraction + source/request-guard tests (two nominal local pairs run) =="
python3 cmrr/test_cmrr.py

echo "== sim/cmrr: smoke test (typical, 27C, 3.30 V, both excitations, local) =="
python3 cmrr/run_cmrr.py --smoke

echo "== sim/cmrr-mc: statistics + extraction + source-guard tests (one nominal local unit runs) =="
python3 cmrr-mc/test_cmrr_mc.py

echo "== sim/cmrr-mc: smoke test (typical, 27C, 3.30 V, both excitations in one deck, local) =="
python3 cmrr-mc/run_cmrr_mc.py --smoke

echo "== sim/psrr: extraction + source/request-guard tests (one nominal local triple runs) =="
python3 psrr/test_psrr.py

echo "== sim/psrr: smoke test (typical, 27C, 3.30 V, all three excitations, local) =="
python3 psrr/run_psrr.py --smoke

echo "== sim/slew-swing-power: extraction + source-guard tests (no simulator) =="
python3 slew-swing-power/test_slew_swing_power.py

echo "== sim/slew-swing-power: smoke test (typical, 27C, 3.30 V, one local point per figure) =="
python3 slew-swing-power/run_slew_swing_power.py --smoke

echo "== sim/input-common-mode: request/extraction/range tests (local nominal units run) =="
python3 input-common-mode/test_input_common_mode.py

echo "== sim/input-common-mode: smoke test (typical, 27C, 3.30 V, VCM 1.20 V and 1.65 V, both excitations, local) =="
python3 input-common-mode/run_input_common_mode.py --smoke
