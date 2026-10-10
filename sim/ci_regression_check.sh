#!/usr/bin/env bash
# sim/ci_regression_check.sh -- prove the selftest can actually fail (issues #52, #107).
#
# Copies the repo's sim/ and design/ trees to a scratch dir, deliberately
# breaks one testbench source guard and one extraction in EACH simulator-free
# experiment (gain-gbw-pm, noise, offset-mc, cmrr, cmrr-mc, psrr,
# slew-swing-power, input-common-mode, step-response), and requires that
# experiment's unit suite to FAIL for each (after a control run of the
# unmutated suite). A green run of this script means the guard/extraction tests
# have teeth. It does the
# same for the spec-citation check (issue #112): one mutation of the checker
# must fail its unit suite, and one stale citation in the scratch copy of
# spec/target-spec.md must fail the checker itself. No simulator, no records
# written to the real tree.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# expect_fail EXPERIMENT TEST_FILE SOURCE_FILE NAME PATTERN REPLACEMENT
#   EXPERIMENT   sim/ subdirectory whose unit suite must fail
#   TEST_FILE    that suite's test script (relative to the experiment dir)
#   SOURCE_FILE  file mutated, relative to sim/ (may be a shared driver the
#                experiment imports, e.g. cmrr/run_cmrr.py for input-common-mode;
#                ../spec/target-spec.md for the spec-citation check)
# The first occurrence of PATTERN is replaced; a missing anchor is fatal.
expect_fail() {
  local exp="$1" test="$2" src="$3" name="$4" pattern="$5" replacement="$6"
  rm -rf "$work/t"; mkdir "$work/t"
  cp -R "$repo/sim" "$repo/design" "$repo/spec" "$work/t/"
  python3 - "$work/t/sim/$src" "$pattern" "$replacement" <<'PY'
import sys
p, a, b = sys.argv[1:]
s = open(p).read()
if a not in s:
    sys.exit(f"mutation anchor not found in {p}: {a!r}")
open(p, "w").write(s.replace(a, b, 1))
PY
  if python3 "$work/t/sim/$exp/$test" >"$work/out.txt" 2>&1; then
    echo "FAIL: [$exp] deliberate regression '$name' was NOT caught by the unit suite" >&2
    tail -20 "$work/out.txt" >&2
    exit 1
  fi
  echo "ok: [$exp] deliberate regression '$name' is caught (suite failed as required)"
}

# control EXPERIMENT TEST_FILE: the unmutated suite must pass, otherwise the
# failures below prove nothing.
control() {
  python3 "$repo/sim/$1/$2" >"$work/ctl.txt" 2>&1 \
    || { echo "FAIL: [$1] unmutated suite does not pass" >&2; tail -20 "$work/ctl.txt" >&2; exit 1; }
  echo "ok: [$1] control (unmutated) suite passes"
}

control gain-gbw-pm test_gain_gbw_pm.py
expect_fail gain-gbw-pm test_gain_gbw_pm.py gain-gbw-pm/run_gain_gbw_pm.py \
  "guard: DUT include no longer required" \
  'if DUT_INCLUDE_NAME not in targets:' 'if False:'
expect_fail gain-gbw-pm test_gain_gbw_pm.py gain-gbw-pm/run_gain_gbw_pm.py \
  "extraction: phase margin offset by 10 degrees" \
  '    pm = 180.0 + ph' '    pm = 190.0 + ph'

control noise test_noise.py
expect_fail noise test_noise.py noise/run_noise.py \
  "guard: Cfb AC ground no longer required" \
  'if not any(re.match(r"^Cfb\s+vinn\s+0\s+\{cfb\}\s*$", ln, re.I) for ln in code):' 'if False:'
expect_fail noise test_noise.py noise/run_noise.py \
  "extraction: band-integrated noise doubled" \
  'band_uv={name: math.sqrt(integrate_power(f, di, lo, hi)) * 1e6' 'band_uv={name: 2 * math.sqrt(integrate_power(f, di, lo, hi)) * 1e6'

control offset-mc test_offset_mc.py
expect_fail offset-mc test_offset_mc.py offset-mc/run_offset_mc.py \
  "guard: DUT include no longer required" \
  'if DUT_INCLUDE_NAME not in targets:' 'if False:'
expect_fail offset-mc test_offset_mc.py offset-mc/run_offset_mc.py \
  "extraction: offset sample shifted by 1 mV" \
  '        off = vos
' '        off = vos + 1e-3
'

control cmrr test_cmrr.py
expect_fail cmrr test_cmrr.py cmrr/run_cmrr.py \
  "guard: gain-bench Lfb/Cfb arrangement no longer rejected" \
  'if any(re.match(r"^Lfb\b", ln, re.I) or' 'if False and any(re.match(r"^Lfb\b", ln, re.I) or'
expect_fail cmrr test_cmrr.py cmrr/run_cmrr.py \
  "extraction: Acm doubled" \
  '        ad, acm = solve_ad_acm(dm, cm)' '        ad, acm = solve_ad_acm(dm, cm); acm = acm * 2'

control cmrr-mc test_cmrr_mc.py
expect_fail cmrr-mc test_cmrr_mc.py cmrr-mc/run_cmrr_mc.py \
  "guard: circuit-identical-to-systematic-bench check removed" \
  'if _circuit_lines(text) != _circuit_lines(SYSTEMATIC_TB.read_text()):' 'if False:'
expect_fail cmrr-mc test_cmrr_mc.py cmrr-mc/run_cmrr_mc.py \
  "extraction: duplicate sample index accepted" \
  'if idx is None or idx in seen[k]:' 'if False:'

control psrr test_psrr.py
expect_fail psrr test_psrr.py psrr/run_psrr.py \
  "guard: supply-run Vss AC-drive line no longer required" \
  '"testbench lost `Vss vss 0 dc 0 ac {acss}`", errs)' '"x", [])'
expect_fail psrr test_psrr.py psrr/run_psrr.py \
  "extraction: supply-run rail excitation check disabled" \
  'if err > C.EXC_TOL:' 'if False:'

control slew-swing-power test_slew_swing_power.py
# slew-swing-power reuses the gain driver's testbench guard (`g.guard_testbench`),
# so the guard mutation lands in gain-gbw-pm/run_gain_gbw_pm.py.
expect_fail slew-swing-power test_slew_swing_power.py gain-gbw-pm/run_gain_gbw_pm.py \
  "guard: shared gain-driver DUT-include check removed" \
  'if DUT_INCLUDE_NAME not in targets:' 'if False:'
expect_fail slew-swing-power test_slew_swing_power.py slew-swing-power/run_slew_swing_power.py \
  "extraction: supply power doubled" \
  'power_uw=idd * vdd * 1e6' 'power_uw=idd * vdd * 2e6'

# input-common-mode reuses the CMRR driver's servo-bench guard, so the guard
# mutation lands in cmrr/run_cmrr.py and must fail the input-common-mode suite.
control input-common-mode test_input_common_mode.py
expect_fail input-common-mode test_input_common_mode.py cmrr/run_cmrr.py \
  "guard: shared servo-bench guard rejects nothing" \
  'errs = G.guard_testbench(text)
    code = code_lines(text)
    for pat in _SERVO_LINES:' 'return []
    code = code_lines(text)
    for pat in _SERVO_LINES:'
expect_fail input-common-mode test_input_common_mode.py input-common-mode/run_input_common_mode.py \
  "extraction: paired-operating-point agreement check disabled" \
  'if worst > OP_AGREE_V:' 'if False:'

# step-response (issue #113) reuses the gain driver's testbench and DUT guards,
# so the guard mutations land in gain-gbw-pm/run_gain_gbw_pm.py.
control step-response test_step_response.py
expect_fail step-response test_step_response.py gain-gbw-pm/run_gain_gbw_pm.py \
  "guard: shared gain-driver DUT-include check removed" \
  'if DUT_INCLUDE_NAME not in targets:' 'if False:'
expect_fail step-response test_step_response.py gain-gbw-pm/run_gain_gbw_pm.py \
  "guard: stale/altered DUT line no longer rejected" \
  'errs.append(f"export line missing or altered in the DUT: {ln[:70]}")' 'pass'
expect_fail step-response test_step_response.py step-response/run_step_response.py \
  "extraction: overshoot offset by 1 % of the step" \
  'overshoot = max(0.0, float(yw.max()) - 1.0)' 'overshoot = max(0.0, float(yw.max()) - 0.99)'

# spec-citation check (issue #112): the checker's unit suite must catch a
# disabled selected-record comparison, and the checker itself (run on the
# scratch copy, whose root it derives from its own path) must catch a spec row
# citing an older record than sim/report/selection.json selects.
control report test_spec_citation_check.py
expect_fail report test_spec_citation_check.py report/spec_citation_check.py \
  "check: cited-vs-selected record comparison disabled" \
  'elif c["rid"] != sel:' 'elif False:'
expect_fail report spec_citation_check.py ../spec/target-spec.md \
  "data: gain row cites the older gain-gbw-pm record again" \
  '[record `20261010-020141-1e51d1c`](../sim/gain-gbw-pm/records/20261010-020141-1e51d1c.md)' \
  '[record `20261009-055759-2524b3e`](../sim/gain-gbw-pm/records/20261009-055759-2524b3e.md)'
