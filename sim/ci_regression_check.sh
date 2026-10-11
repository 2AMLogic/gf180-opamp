#!/usr/bin/env bash
# sim/ci_regression_check.sh -- prove the selftest can actually fail (issues #52, #107).
#
# Copies the repo's sim/ and design/ trees to a scratch dir, deliberately
# breaks one testbench source guard and one extraction in EACH simulator-free
# experiment (gain-gbw-pm, noise, offset-mc, cmrr, cmrr-mc, gain-gbw-pm-mc, psrr,
# psrr-mc, slew-swing-power, input-common-mode, step-response,
# overdrive-recovery, ibias-cl-sensitivity),
# and requires that experiment's unit suite to FAIL for each (after a control
# run of the unmutated suite). Issue #131 adds the shared modules whose suites
# are wired into selftest.sh: sim/harness.py (test_harness.py) and
# sim/passive_corners.py (covered by the slew-swing-power suite; it has no
# suite of its own). A green run of this script means the guard/extraction
# tests have teeth. It does the
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
expect_fail cmrr-mc test_cmrr_mc.py harness.py \
  "extraction: duplicate sample index accepted" \
  'if idx is None or idx in seen[k]:' 'if False:'

# gain-gbw-pm-mc (issue #129): the Monte Carlo bench guard, the fixed-denominator
# sample accounting and the #42 campaign gate must each have teeth.
control gain-gbw-pm-mc test_gain_gbw_pm_mc.py
expect_fail gain-gbw-pm-mc test_gain_gbw_pm_mc.py gain-gbw-pm-mc/run_gain_gbw_pm_mc.py \
  "guard: circuit-identical-to-gain-bench check removed" \
  '    if rest != _code(gain):' '    if False:'
expect_fail gain-gbw-pm-mc test_gain_gbw_pm_mc.py gain-gbw-pm-mc/run_gain_gbw_pm_mc.py \
  "guard: mismatch switch may precede the design.ngspice include" \
  'if not inc or sw[0] < inc[0]:' 'if False:'
expect_fail gain-gbw-pm-mc test_gain_gbw_pm_mc.py gain-gbw-pm-mc/run_gain_gbw_pm_mc.py \
  "accounting: failed/missing samples dropped from the failure count" \
  '        n_invalid = n_expected - n
' '        n_invalid = 0
'
expect_fail gain-gbw-pm-mc test_gain_gbw_pm_mc.py gain-gbw-pm-mc/run_gain_gbw_pm_mc.py \
  "accounting: duplicate sample index accepted" \
  '        if idx in out[k]:' '        if False:'
expect_fail gain-gbw-pm-mc test_gain_gbw_pm_mc.py gain-gbw-pm-mc/run_gain_gbw_pm_mc.py \
  "gate: stale revised-DUT pin accepted" \
  '    if pin != current_sha:' '    if False:'

control psrr test_psrr.py
expect_fail psrr test_psrr.py psrr/run_psrr.py \
  "guard: supply-run Vss AC-drive line no longer required" \
  '"testbench lost `Vss vss 0 dc 0 ac {acss}`", errs)' '"x", [])'
expect_fail psrr test_psrr.py psrr/run_psrr.py \
  "extraction: supply-run rail excitation check disabled" \
  'if err > C.EXC_TOL:' 'if False:'

# psrr-mc (issue #99): the circuit-identity guard, the per-sample dm/vss parity
# check, the fewer-than-N sample accounting and the imbalance control.
control psrr-mc test_psrr_mc.py
expect_fail psrr-mc test_psrr_mc.py psrr-mc/run_psrr_mc.py \
  "guard: circuit-identical-to-systematic-PSRR-bench check removed" \
  'if _circuit_lines(text) != _circuit_lines(SYSTEMATIC_TB.read_text()):' 'if False:'
expect_fail psrr-mc test_psrr_mc.py psrr-mc/run_psrr_mc.py \
  "extraction: dm/supply parity check disabled" \
  'if abs(vals[f"pmc_par_{r}"]) > PARITY_TOL_V:' 'if False:'
expect_fail psrr-mc test_psrr_mc.py harness.py \
  "accounting: fewer than N distinct samples accepted" \
  'if got != list(range(n_expected)):' 'if False:'
expect_fail psrr-mc test_psrr_mc.py psrr-mc/run_psrr_mc.py \
  "control: imbalance shift criterion disabled" \
  'if not shift > CONTROL_SE_MULT * se:' 'if False:'
expect_fail psrr-mc test_psrr_mc.py psrr-mc/run_psrr_mc.py \
  "gate: per-point offset sigma > 1 mV check disabled" \
  'if not sg > MIN_OFFSET_SIGMA_V:' 'if False:'
expect_fail psrr-mc test_psrr_mc.py psrr-mc/run_psrr_mc.py \
  "gate: near-cancellation knee accepted without monotonicity" \
  'if steps and min(steps) >= 1.0 - KNEE_STEP_SLACK:' 'if True:'
expect_fail psrr-mc test_psrr_mc.py psrr-mc/run_psrr_mc.py \
  "stats: knee DC read from the band mean instead of the 0.1 Hz asymptote" \
  '            a0 = self.vals[f"pmc_{rail}_aband{BAND[0]}"]' '            a0 = self.vals[f"pmc_asup_{rail}_dc"]'

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

# overdrive-recovery (issue #183) reuses the gain driver's testbench and DUT
# guards, so the guard mutations land in gain-gbw-pm/run_gain_gbw_pm.py, and
# the step driver's window/settling helpers.
control overdrive-recovery test_overdrive_recovery.py
expect_fail overdrive-recovery test_overdrive_recovery.py gain-gbw-pm/run_gain_gbw_pm.py \
  "guard: shared gain-driver DUT-include check removed" \
  'if DUT_INCLUDE_NAME not in targets:' 'if False:'
expect_fail overdrive-recovery test_overdrive_recovery.py gain-gbw-pm/run_gain_gbw_pm.py \
  "guard: stale/altered DUT line no longer rejected" \
  'errs.append(f"export line missing or altered in the DUT: {ln[:70]}")' 'pass'
expect_fail overdrive-recovery test_overdrive_recovery.py overdrive-recovery/run_overdrive_recovery.py \
  "extraction: recovery band halved" \
  'band_v = BAND_FRAC * abs(v_ref)' 'band_v = 0.5 * BAND_FRAC * abs(v_ref)'

# ibias-cl-sensitivity (issue #131): the substitution helpers' exactly-one-match
# guard and the control-vs-committed-record comparison.
control ibias-cl-sensitivity test_ibias_cl_sensitivity.py
expect_fail ibias-cl-sensitivity test_ibias_cl_sensitivity.py ibias-cl-sensitivity/run_ibias_cl_sensitivity.py \
  "guard: Ibias substitution no longer requires exactly one match" \
  'if n != 1:
        raise RuntimeError(f"testbench Ibias' 'if False:
        raise RuntimeError(f"testbench Ibias'
expect_fail ibias-cl-sensitivity test_ibias_cl_sensitivity.py ibias-cl-sensitivity/run_ibias_cl_sensitivity.py \
  "extraction: control comparison never reports a failure" \
  '                bad.append(f"control {fig} {lab}: {v[field]:.4g} vs record {ref[i]:.4g}")
    return bad' '                bad.append(f"control {fig} {lab}: {v[field]:.4g} vs record {ref[i]:.4g}")
    return []'

# Shared modules (issue #131). harness.py has its own suite (test_harness.py, in
# the sim/ root). passive_corners.py has no suite of its own: its helpers are
# exercised by the slew-swing-power suite, which is the one that must fail.
control . test_harness.py
expect_fail . test_harness.py harness.py \
  "guard: stage_workdir ignores testbench guard errors" \
  '    errs = guard_tb(tb_text)
    if errs:' '    errs = guard_tb(tb_text)
    if False:'
expect_fail slew-swing-power test_slew_swing_power.py passive_corners.py \
  "extraction: worst passive level maps to the ff section" \
  '"worst": "ss"}' '"worst": "ff"}'

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
  '[record `20261010-161514-0127eb7`](../sim/gain-gbw-pm/records/20261010-161514-0127eb7.md)' \
  '[record `20261010-020141-1e51d1c`](../sim/gain-gbw-pm/records/20261010-020141-1e51d1c.md)'

# Derived offset temperature-drift view (issue #184): the report suite must catch a units
# regression (uV -> mV scaling dropped) and a drift slope computed with the wrong temperature span.
control report test_report.py
expect_fail report test_report.py report/characterization_report.py \
  "arithmetic: offset drift view reports mV where it labels uV" \
  'mean, sig = s["mean"] * 1e3, s["sigma"] * 1e3  # mV -> uV' \
  'mean, sig = s["mean"], s["sigma"]  # mV -> uV'
expect_fail report test_report.py report/characterization_report.py \
  "arithmetic: end-to-end drift divides by the wrong temperature span" \
  '(ms[-1] - ms[0]) / span,' '(ms[-1] - ms[0]) / (span + 1),'

# Committed-evidence append-only guard (issue #159): in a throwaway git repo, a
# modified and a deleted record must make the guard fail (exit 1), and an
# add-only change must pass (control). Stdlib + git only, no simulator.
guard="$repo/sim/ci_evidence_append_only.py"
ev="$work/evidence-repo"
mkdir -p "$ev/sim/noise/records"
git -C "$ev" init -q -b main
git -C "$ev" config user.email ci@example.invalid
git -C "$ev" config user.name ci
echo original > "$ev/sim/noise/records/r1.md"
git -C "$ev" add -A && git -C "$ev" commit -q -m base
git -C "$ev" branch base
: > "$work/empty-allowlist.txt"
run_guard() { python3 "$guard" --repo "$ev" --allowlist "$work/empty-allowlist.txt" --base base; }
# each case runs on its own branch off base
guard_case() {  # NAME EXPECT(pass|fail) MUTATION-COMMANDS...
  local name="$1" expect="$2"; shift 2
  git -C "$ev" checkout -q -B "case-$name" base
  "$@"
  git -C "$ev" add -A && git -C "$ev" commit -q -m "$name"
  if run_guard >"$work/g.txt" 2>&1; then rc=0; else rc=1; fi
  if [ "$expect" = pass ] && [ "$rc" -ne 0 ]; then
    echo "FAIL: [evidence guard] $name was rejected" >&2; cat "$work/g.txt" >&2; exit 1
  fi
  if [ "$expect" = fail ]; then
    [ "$rc" -ne 0 ] || { echo "FAIL: [evidence guard] $name not caught" >&2; exit 1; }
    grep -q 'sim/noise/records/r1.md' "$work/g.txt" \
      || { echo "FAIL: [evidence guard] $name: message does not name the file" >&2; exit 1; }
  fi
  echo "ok: [evidence guard] $name -> $expect"
}
add_record()    { echo added > "$ev/sim/noise/records/r2.md"; }
modify_record() { echo changed > "$ev/sim/noise/records/r1.md"; }
delete_record() { rm "$ev/sim/noise/records/r1.md"; }
guard_case add-only-control pass add_record
guard_case modified-record fail modify_record
guard_case deleted-record fail delete_record
