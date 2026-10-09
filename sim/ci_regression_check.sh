#!/usr/bin/env bash
# sim/ci_regression_check.sh -- prove the selftest can actually fail (issue #52).
#
# Copies the repo's sim/ and design/ trees to a scratch dir, deliberately
# breaks the gain-bench testbench source guard (so a testbench that no longer
# includes the DUT is accepted) and an extraction (GBW off by 2x), and requires
# the simulator-free unit suite to FAIL for each. A green run of this script
# means the guard/extraction tests have teeth. No simulator, no records written
# to the real tree.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

expect_fail() {
  local name="$1" pattern="$2" replacement="$3"
  rm -rf "$work/t"; mkdir "$work/t"
  cp -R "$repo/sim" "$repo/design" "$work/t/"
  local f="$work/t/sim/gain-gbw-pm/run_gain_gbw_pm.py"
  python3 - "$f" "$pattern" "$replacement" <<'PY'
import sys
p, a, b = sys.argv[1:]
s = open(p).read()
if a not in s:
    sys.exit(f"mutation anchor not found: {a!r}")
open(p, "w").write(s.replace(a, b, 1))
PY
  if python3 "$work/t/sim/gain-gbw-pm/test_gain_gbw_pm.py" >"$work/out.txt" 2>&1; then
    echo "FAIL: deliberate regression '$name' was NOT caught by the unit suite" >&2
    tail -20 "$work/out.txt" >&2
    exit 1
  fi
  echo "ok: deliberate regression '$name' is caught (suite failed as required)"
}

# Control: the unmutated copy must pass, otherwise the failures above prove nothing.
python3 "$repo/sim/gain-gbw-pm/test_gain_gbw_pm.py" >"$work/ctl.txt" 2>&1 \
  || { echo "FAIL: unmutated suite does not pass" >&2; tail -20 "$work/ctl.txt" >&2; exit 1; }
echo "ok: control (unmutated) suite passes"

expect_fail "guard: DUT include no longer required" \
  'if DUT_INCLUDE_NAME not in targets:' 'if False:'

expect_fail "extraction: phase margin offset by 10 degrees" \
  '    pm = 180.0 + ph' '    pm = 190.0 + ph'
