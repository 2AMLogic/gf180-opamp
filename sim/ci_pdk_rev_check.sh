#!/usr/bin/env bash
# sim/ci_pdk_rev_check.sh -- prove the PDK revision gate has teeth (issue #52).
#
# Builds scratch PDK fixtures in the volare default layout
# ($HOME/.volare/gf180mcuD -> volare/gf180mcu/versions/<rev>/gf180mcuD, which
# is exactly what an actions/cache restore of ~/.volare produces) and runs
# `sim/ci_prereqs.py --pdk-only` against each with HOME pointed at the
# fixture. The pinned revision must pass; a wrong revision, a missing SOURCES
# file and a SOURCES file without an open_pdks line must each fail with
# expected/actual diagnostics. A GF180_PDK_REV that disagrees with the
# harness pin must also fail. No real PDK, no simulator, nothing written to
# the real tree.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

pinned="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import harness; print(harness.PINNED_PDK_REV)' "$repo/sim")"
wrong="0123456789abcdef0123456789abcdef01234567"

# make_fixture <name> <dir-rev> <SOURCES content or __NONE__>
make_fixture() {
  local name="$1" rev="$2" sources="$3"
  local home="$work/$name"
  local vdir="$home/.volare/volare/gf180mcu/versions/$rev/gf180mcuD"
  mkdir -p "$vdir/libs.tech/ngspice"
  : >"$vdir/libs.tech/ngspice/sm141064.ngspice"
  if [ "$sources" != "__NONE__" ]; then
    printf '%s\n' "$sources" >"$vdir/SOURCES"
  fi
  ln -s "volare/gf180mcu/versions/$rev/gf180mcuD" "$home/.volare/gf180mcuD"
  echo "$home"
}

# run_check <home> [extra env...] -- writes combined output to $work/out.txt and
# returns ci_prereqs' status. Extra CLI args come from the EXTRA_ARGS array.
EXTRA_ARGS=()
run_check() {
  local home="$1"; shift
  env -u GF180_PDK_PATH -u PDK_ROOT -u PDK -u GF180_PDK_REV HOME="$home" "$@" \
    python3 "$repo/sim/ci_prereqs.py" --pdk-only ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} >"$work/out.txt" 2>&1
}

expect_pass() {
  local name="$1"; shift
  if ! run_check "$@"; then
    echo "FAIL: '$name' should pass the PDK revision gate but did not" >&2
    cat "$work/out.txt" >&2
    exit 1
  fi
  echo "ok: $name passes"
}

# expect_fail <name> <required-substring> <home> [env...]
expect_fail() {
  local name="$1" needle="$2"; shift 2
  if run_check "$@"; then
    echo "FAIL: '$name' was ACCEPTED by the PDK revision gate" >&2
    cat "$work/out.txt" >&2
    exit 1
  fi
  if ! grep -qF -- "$needle" "$work/out.txt"; then
    echo "FAIL: '$name' failed, but without the expected diagnostic '$needle'" >&2
    cat "$work/out.txt" >&2
    exit 1
  fi
  echo "ok: $name is rejected ($needle)"
}

h_pinned="$(make_fixture pinned "$pinned" "open_pdks $pinned")"
h_wrong="$(make_fixture wrong "$wrong" "open_pdks $wrong")"
# A cache whose directory name claims the pin but whose contents are another revision.
h_lying="$(make_fixture lying "$pinned" "open_pdks $wrong")"
h_nosrc="$(make_fixture nosources "$pinned" "__NONE__")"
h_noline="$(make_fixture noline "$pinned" "gf180mcu deadbeef")"

expect_pass "pinned revision (restored cache)" "$h_pinned"
expect_pass "pinned revision with GF180_PDK_REV set" "$h_pinned" GF180_PDK_REV="$pinned"
grep -qF "open_pdks $pinned == expected" "$work/out.txt" \
  || { echo "FAIL: pass output lacks the verified revision" >&2; cat "$work/out.txt" >&2; exit 1; }

expect_fail "wrong revision" "actual   open_pdks $wrong" "$h_wrong"
grep -qF "expected open_pdks $pinned" "$work/out.txt" \
  || { echo "FAIL: mismatch output lacks the expected revision" >&2; cat "$work/out.txt" >&2; exit 1; }
expect_fail "pinned-named dir with wrong SOURCES" "actual   open_pdks $wrong" "$h_lying"
expect_fail "missing SOURCES (unknown provenance)" "<unknown provenance>" "$h_nosrc"
expect_fail "SOURCES without open_pdks line (unknown provenance)" "<unknown provenance>" "$h_noline"
expect_fail "GF180_PDK_REV disagrees with harness pin" "GF180_PDK_REV disagrees" "$h_pinned" GF180_PDK_REV="$wrong"
EXTRA_ARGS=(--expect-rev "${pinned:0:12}")
expect_fail "abbreviated expected revision" "full 40-hex" "$h_pinned"
EXTRA_ARGS=()

echo "ok: PDK revision gate accepts only the pinned open_pdks revision"
