#!/usr/bin/env bash
# test-sweep-baseline.sh - regression check for the sweep main-clean baseline
# recipe in docs/sweep-baseline.md (issue #102).
#
# What it establishes, entirely inside a disposable fixture under a fresh
# mktemp directory (never in this repository's checkout):
#
#   1. Worktree isolation is active in the fixture (a Loom-managed worktree
#      with a .loom-managed sentinel exists), and the supported invocation --
#      check-main-clean.sh --snapshot/--baseline with an explicit, resolved,
#      absolute scratch path outside the repository -- receives NO deny
#      decision from the installed Bash PreToolUse hooks.
#   2. Bundled main-checkout writes still receive a deny decision:
#      a main-checkout mkdir, a main-checkout file redirect, and a redirect
#      to an unresolved ($VAR) destination. These payloads are only fed to
#      the hook on stdin as JSON; they are never executed.
#   3. Executing the supported pair succeeds: snapshot exits 0, baseline exits
#      0 on a clean main checkout, from the main checkout and from a linked
#      worktree (the helper resolves main via git-common-dir), with a scratch
#      path containing spaces.
#   4. New main-checkout dirt introduced after the snapshot is detected
#      (exit 3), pre-existing dirt recorded in the snapshot is ignored, and a
#      missing baseline cannot mask dirt (warning + whole-status check, exit 3).
#
# Hook decisions are asserted on the hook's JSON output with jq, not on exit
# status: deny hooks exit 0 and print a hookSpecificOutput.permissionDecision.
#
# The hooks and helper are byte-for-byte copies of this repository's
# installed .loom/hooks and .loom/scripts, placed in the fixture so their
# runtime logs land in the fixture too. Nothing installed is modified.
#
# Usage:
#   scripts/test-sweep-baseline.sh
#
# Exit status: 0 if every check passes, 1 otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_HOOKS="$REPO_ROOT/.loom/hooks"
SRC_SCRIPTS="$REPO_ROOT/.loom/scripts"

for tool in git jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "test-sweep-baseline.sh: required tool '$tool' not found" >&2
        exit 1
    fi
done
for f in "$SRC_HOOKS/guard-destructive.sh" "$SRC_HOOKS/guard-destructive-generic.sh" \
         "$SRC_HOOKS/guard-loom-workflow.sh" "$SRC_SCRIPTS/check-main-clean.sh"; do
    if [[ ! -r "$f" ]]; then
        echo "test-sweep-baseline.sh: installed Loom file missing: $f" >&2
        exit 1
    fi
done

# The hook reads its own inherited environment. Clear every override that
# could switch isolation off or pin a worktree, so the fixture exercises the
# default (isolation on) configuration.
unset LOOM_GUARD_WORKTREE_ISOLATION LOOM_WORKTREE_PATH LOOM_PROJECT_ROOT CLAUDE_PROJECT_DIR

PASSED=0
FAILED=0
pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1"; }

# Record this repository's own status so we can prove the run left no dirt.
PRIMARY_MAIN="$(cd "$(git -C "$REPO_ROOT" rev-parse --git-common-dir)/.." && pwd -P)"
PRIMARY_STATUS_BEFORE="$(git -C "$PRIMARY_MAIN" status --porcelain 2>/dev/null)"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sweep-baseline-test.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf -- "$TMP_ROOT"; }
trap cleanup EXIT

FIX="$TMP_ROOT/main"
WT="$FIX/.loom/worktrees/issue-1"
SCRATCH="$TMP_ROOT/sweep scratch"          # deliberately contains a space
BASELINE="$SCRATCH/main-clean baseline.txt"

g() { git -C "$1" -c user.name=fixture -c user.email=fixture@example.invalid \
          -c commit.gpgsign=false -c init.defaultBranch=main "${@:2}"; }

echo "Fixture: $TMP_ROOT"

# ---- Build the disposable fixture ---------------------------------------
mkdir -p "$FIX/.loom" "$SCRATCH"
g "$FIX" init -q
cp -R "$SRC_HOOKS" "$FIX/.loom/hooks"
cp -R "$SRC_SCRIPTS" "$FIX/.loom/scripts"
mkdir -p "$FIX/.loom/logs"
printf '%s\n' '.loom/' > "$FIX/.gitignore"
printf '%s\n' '# fixture' > "$FIX/README.md"
g "$FIX" add .gitignore README.md
g "$FIX" commit -q -m fixture
g "$FIX" worktree add -q -b feature/issue-1 "$WT" >/dev/null 2>&1
printf '%s\n' '# Loom-managed worktree marker (fixture)' '# Issue: 1' > "$WT/.loom-managed"

HELPER="$FIX/.loom/scripts/check-main-clean.sh"
HOOKS=("$FIX/.loom/hooks/guard-destructive.sh" "$FIX/.loom/hooks/guard-loom-workflow.sh")

if [[ -f "$WT/.loom-managed" ]] && [[ -z "$(g "$FIX" status --porcelain)" ]]; then
    pass "fixture main checkout is clean with a managed worktree sentinel present"
else
    fail "fixture setup: expected clean main and a .loom-managed sentinel"
fi

# ---- Hook probes --------------------------------------------------------
# hook_decision <hook> <cwd> <command> -> prints "deny"/"ask"/"allow".
# The command is ONLY embedded in a JSON payload for the hook's stdin.
HOOK_OUT=""
hook_decision() {
    local hook="$1" cwd="$2" cmd="$3" payload
    payload="$(jq -cn --arg c "$cmd" --arg d "$cwd" \
        '{hook_event_name:"PreToolUse", tool_name:"Bash", tool_input:{command:$c}, cwd:$d}')"
    HOOK_OUT="$(cd "$cwd" && printf '%s' "$payload" | CLAUDE_PROJECT_DIR="$FIX" bash "$hook" 2>/dev/null)"
    if [[ -z "$HOOK_OUT" ]]; then
        echo "allow"
        return
    fi
    printf '%s' "$HOOK_OUT" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null \
        || echo "invalid-json"
}

# hook_reason <hook> <cwd> <command> -> the permissionDecisionReason (or empty).
hook_reason() {
    hook_decision "$@" >/dev/null
    [[ -n "$HOOK_OUT" ]] || return 0
    printf '%s' "$HOOK_OUT" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""' 2>/dev/null
}

# expect_all_hooks <expected> <label> <cwd> <command>
# allow: no configured Bash hook may deny/ask. deny: the destructive guard must deny.
expect_allow() {
    local label="$1" cwd="$2" cmd="$3" hook d ok=1
    for hook in "${HOOKS[@]}"; do
        d="$(hook_decision "$hook" "$cwd" "$cmd")"
        if [[ "$d" != "allow" ]]; then
            ok=0
            fail "$label: $(basename "$hook") returned '$d' (expected no decision)"
        fi
    done
    [[ "$ok" -eq 1 ]] && pass "$label: no deny/ask from any Bash hook"
}
# Denials must come from Bash-tool write confinement (worktree isolation),
# not from some unrelated rule, so the reason text is checked too.
expect_deny() {
    local label="$1" cwd="$2" cmd="$3" d reason
    d="$(hook_decision "${HOOKS[0]}" "$cwd" "$cmd")"
    reason="$(hook_reason "${HOOKS[0]}" "$cwd" "$cmd")"
    if [[ "$d" == "deny" && "$reason" == *"Bash-tool write"* ]]; then
        pass "$label: denied by guard-destructive.sh (write confinement)"
    else
        fail "$label: guard-destructive.sh returned '$d' (expected write-confinement deny): ${reason:0:160}"
    fi
}

q() { printf '%q' "$1"; }   # shell-quote a literal path for a command string

echo "Hook decisions (worktree isolation active):"
SNAP_CMD="$(q "$HELPER") --snapshot $(q "$BASELINE")"
BASE_CMD="$(q "$HELPER") --baseline $(q "$BASELINE")"
expect_allow "supported snapshot, cwd=main"     "$FIX" "$SNAP_CMD"
expect_allow "supported baseline, cwd=main"     "$FIX" "$BASE_CMD"
expect_allow "supported snapshot, cwd=worktree" "$WT"  "$SNAP_CMD"
expect_allow "supported baseline, cwd=worktree" "$WT"  "$BASE_CMD"
expect_allow "scratch allocation (separate call)" "$FIX" "mktemp -d /tmp/gf180-opamp-sweep-XXXXXX"

expect_deny "main-checkout mkdir (relative)"   "$FIX" "mkdir -p .loom/sweep-checkpoint"
expect_deny "main-checkout mkdir from worktree" "$WT" "mkdir -p $(q "$FIX/.loom/sweep-checkpoint")"
expect_deny "main-checkout file redirect"       "$FIX" "echo x > $(q "$FIX/README.md")"
expect_deny "main-checkout redirect from worktree" "$WT" "echo x > $(q "$FIX/README.md")"
expect_deny "unresolved redirect destination"   "$FIX" 'echo "{}" > $V/fresh.json'
expect_deny "historical bundled checkpoint form" "$FIX" \
    "mkdir -p .loom/sweep-checkpoint && $(q "$HELPER") --snapshot .loom/sweep-checkpoint/main-clean-baseline.txt"

# Control: the mkdir denial above is caused by isolation being in play. With
# the managed-worktree sentinel moved aside, the same payload is not denied.
# (Still only a hook probe; the mkdir is never executed.)
mv "$WT/.loom-managed" "$TMP_ROOT/sentinel.aside"
d="$(hook_decision "${HOOKS[0]}" "$FIX" "mkdir -p .loom/sweep-checkpoint")"
mv "$TMP_ROOT/sentinel.aside" "$WT/.loom-managed"
if [[ "$d" == "allow" ]]; then
    pass "control: without a managed worktree the mkdir probe is not denied (deny is isolation-driven)"
else
    fail "control: without a managed worktree the mkdir probe returned '$d'"
fi

# ---- Execute the supported invocation in the fixture --------------------
# run_helper <cwd> <args...> -> sets RC and OUT (stdout+stderr).
run_helper() {
    local cwd="$1"; shift
    OUT="$(cd "$cwd" && bash "$HELPER" "$@" 2>&1)"
    RC=$?
}

echo "Helper behavior:"
run_helper "$FIX" --snapshot "$BASELINE"
if [[ "$RC" -eq 0 && -f "$BASELINE" ]]; then
    pass "snapshot to scratch path with spaces exits 0 and writes the file"
else
    fail "snapshot: rc=$RC, file present=$([[ -f "$BASELINE" ]] && echo yes || echo no): $OUT"
fi

run_helper "$FIX" --baseline "$BASELINE"
[[ "$RC" -eq 0 ]] && pass "baseline on clean main exits 0 (cwd=main)" \
    || fail "baseline on clean main, cwd=main: rc=$RC: $OUT"

run_helper "$WT" --baseline "$BASELINE"
if [[ "$RC" -eq 0 && "$OUT" == *"$FIX"* ]]; then
    pass "baseline from linked worktree resolves the main checkout via git-common-dir"
else
    fail "baseline from worktree: rc=$RC: $OUT"
fi

if [[ -z "$(g "$FIX" status --porcelain)" ]]; then
    pass "snapshot/baseline left the fixture main checkout clean"
else
    fail "fixture main dirtied by snapshot/baseline: $(g "$FIX" status --porcelain)"
fi

# New dirt after the snapshot must be detected.
printf '%s\n' 'stray' > "$FIX/stray-output.txt"
run_helper "$WT" --baseline "$BASELINE"
if [[ "$RC" -eq 3 && "$OUT" == *"stray-output.txt"* ]]; then
    pass "new untracked main dirt after snapshot is detected (exit 3)"
else
    fail "new untracked dirt: rc=$RC: $OUT"
fi
rm -f -- "$FIX/stray-output.txt"

printf '%s\n' 'modified' >> "$FIX/README.md"
run_helper "$FIX" --baseline "$BASELINE"
if [[ "$RC" -eq 3 && "$OUT" == *"README.md"* ]]; then
    pass "new tracked modification after snapshot is detected (exit 3)"
else
    fail "new tracked modification: rc=$RC: $OUT"
fi

# Pre-existing dirt recorded in a snapshot is ignored; new dirt still counts.
PRE_BASELINE="$SCRATCH/pre-existing baseline.txt"
run_helper "$FIX" --snapshot "$PRE_BASELINE"
run_helper "$FIX" --baseline "$PRE_BASELINE"
[[ "$RC" -eq 0 ]] && pass "dirt recorded in the snapshot is ignored (exit 0)" \
    || fail "pre-existing dirt baseline: rc=$RC: $OUT"
printf '%s\n' 'later' > "$FIX/later.txt"
run_helper "$FIX" --baseline "$PRE_BASELINE"
if [[ "$RC" -eq 3 && "$OUT" == *"later.txt"* && "$OUT" != *"M README.md"* ]]; then
    pass "only dirt introduced after the snapshot is reported"
else
    fail "post-snapshot dirt with pre-existing dirt: rc=$RC: $OUT"
fi
rm -f -- "$FIX/later.txt"
g "$FIX" checkout -q -- README.md

# A missing baseline must never mask dirt.
MISSING="$SCRATCH/never-written.txt"
printf '%s\n' 'stray' > "$FIX/stray-output.txt"
run_helper "$FIX" --baseline "$MISSING"
if [[ "$RC" -eq 3 && "$OUT" == *"missing or unreadable"* ]]; then
    pass "missing baseline + dirt: warning and whole-status failure (exit 3)"
else
    fail "missing baseline with dirt: rc=$RC: $OUT"
fi
if [[ ! -e "$MISSING" ]]; then
    pass "baseline check does not silently create a replacement baseline"
else
    fail "baseline check created '$MISSING'"
fi
rm -f -- "$FIX/stray-output.txt"

run_helper "$FIX" --baseline "$MISSING"
if [[ "$RC" -eq 0 && "$OUT" == *"missing or unreadable"* ]]; then
    pass "missing baseline on clean main still warns (whole-status check passes)"
else
    fail "missing baseline on clean main: rc=$RC: $OUT"
fi

# ---- The primary checkout must be untouched -----------------------------
PRIMARY_STATUS_AFTER="$(git -C "$PRIMARY_MAIN" status --porcelain 2>/dev/null)"
if [[ "$PRIMARY_STATUS_BEFORE" == "$PRIMARY_STATUS_AFTER" ]]; then
    pass "primary checkout status unchanged by this run"
else
    fail "primary checkout status changed during the run"
fi

echo ""
echo "Results: $PASSED passed, $FAILED failed"
[[ "$FAILED" -eq 0 ]]
