#!/usr/bin/env bash
# guard-confinement-repro.sh - hook-only reproduction of the scratch-path /
# checkpoint-cleanup guard cases in docs/fixtures/guard-confinement-cases.json
# (issue #146). See docs/guard-confinement-reproduction.md.
#
# Safety model
#   * Every payload is DATA. It is built with jq and written to a guard
#     process's stdin. No command string from the fixture is ever eval'd,
#     sourced, expanded or executed by this script.
#   * Everything runs in a fresh mktemp fixture repository holding copies of
#     the installed .loom/hooks and .loom/scripts and synthetic .loom-managed
#     sentinels. The real checkout's checkpoints, leases and run registry are
#     never read or written; this script only reads installed hook files.
#   * No guard policy, hook, generated skill or setting is modified.
#
# Result classes per hook process (execution failure != allow):
#   allow-empty   exit 0, empty stdout, nothing logged to hook-errors.log
#                 (the hook protocol's documented allow)
#   allow-json    exit 0, well-formed JSON with permissionDecision=allow
#   deny | ask    exit 0, well-formed JSON with that permissionDecision
#   error:missing-hook | error:exit-N | error:timeout | error:malformed |
#   error:hook-error-logged (exit 0 but the hook's ERR trap fired: a fail-open
#   allow caused by an internal error, which is NOT evidence of allow)
# Per-case verdicts: MATCH, DISCREPANCY, TAG-MISMATCH, RECORDED (no presumed
# contract), INCONCLUSIVE (an error and no deny).
#
# Usage: scripts/guard-confinement-repro.sh [--out FILE] [--keep] [--selftest-only]
#   --out FILE   write the full JSON result document to FILE (default: inside
#                the scratch root, printed on stdout)
#   --keep       keep the scratch fixture after the run
# Exit: 0 all MATCH/RECORDED; 1 any DISCREPANCY/TAG-MISMATCH; 2 any
#       INCONCLUSIVE, setup failure or failed classifier self-test.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CASES="$REPO_ROOT/docs/fixtures/guard-confinement-cases.json"
SRC_HOOKS="$REPO_ROOT/.loom/hooks"
SRC_SCRIPTS="$REPO_ROOT/.loom/scripts"

OUT=""
KEEP=0
SELFTEST_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT="${2:?--out needs a file}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --selftest-only) SELFTEST_ONLY=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

for tool in git jq sha256sum timeout; do
    command -v "$tool" >/dev/null 2>&1 || { echo "required tool '$tool' not found" >&2; exit 2; }
done
for f in "$CASES" "$SRC_HOOKS/guard-destructive.sh" "$SRC_HOOKS/guard-destructive-generic.sh" \
         "$SRC_HOOKS/guard-loom-workflow.sh" "$SRC_HOOKS/guard-worktree-paths.sh"; do
    [[ -r "$f" ]] || { echo "required file missing: $f" >&2; exit 2; }
done
jq -e '(.cases|length>0) and ([.cases[].id]|length==(unique|length))' "$CASES" >/dev/null \
    || { echo "fixture invalid or case ids not unique" >&2; exit 2; }

# Clear every override that could switch isolation off or pin a worktree.
unset LOOM_GUARD_WORKTREE_ISOLATION LOOM_WORKTREE_PATH LOOM_PROJECT_ROOT CLAUDE_PROJECT_DIR

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/guard-confinement-repro.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { [[ "$KEEP" == 1 ]] || rm -rf -- "$TMP_ROOT"; }
trap cleanup EXIT
RES="$TMP_ROOT/results"; mkdir -p "$RES"

# ---- classifier -----------------------------------------------------------
# run_hook <hook-path> <payload-file> <cwd> <project-dir> <tag> -> sets H_* vars
run_hook() {
    local hook="$1" payload="$2" cwd="$3" proj="$4" tag="$5"
    local declog="$RES/decision-$tag.jsonl" errlog_dir errlog before after out ec
    H_CLASS="" H_DECISION="" H_TAG="" H_REASON="" H_EXIT="" H_STDERR_BYTES=0
    if [[ ! -r "$hook" ]]; then H_CLASS="error:missing-hook"; H_DECISION="error"; return; fi
    errlog="$(dirname "$hook")/../logs/hook-errors.log"
    before=0; [[ -f "$errlog" ]] && before="$(wc -l < "$errlog")"
    out="$(cd "$cwd" 2>/dev/null && timeout 30 env LOOM_GUARD_DECISION_LOG=1 \
            LOOM_GUARD_DECISION_LOG_FILE="$declog" CLAUDE_PROJECT_DIR="$proj" \
            bash "$hook" < "$payload" 2>"$RES/stderr-$tag.txt")"
    ec=$?
    H_EXIT="$ec"
    H_STDERR_BYTES="$(wc -c < "$RES/stderr-$tag.txt")"
    after=0; [[ -f "$errlog" ]] && after="$(wc -l < "$errlog")"
    if [[ "$ec" == 124 ]]; then H_CLASS="error:timeout"; H_DECISION="error"; return; fi
    if [[ "$ec" != 0 ]]; then H_CLASS="error:exit-$ec"; H_DECISION="error"; return; fi
    if (( after > before )); then H_CLASS="error:hook-error-logged"; H_DECISION="error"; return; fi
    if [[ -z "$out" ]]; then H_CLASS="allow-empty"; H_DECISION="allow"; return; fi
    local d
    d="$(printf '%s' "$out" | jq -er '
        select(.hookSpecificOutput.hookEventName=="PreToolUse")
        | .hookSpecificOutput.permissionDecision
        | select(. == "allow" or . == "deny" or . == "ask")' 2>/dev/null)" \
        || { H_CLASS="error:malformed"; H_DECISION="error"; return; }
    H_DECISION="$d"
    case "$d" in allow) H_CLASS="allow-json" ;; *) H_CLASS="$d" ;; esac
    H_REASON="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""' | head -c 240)"
    if [[ -s "$declog" ]]; then
        H_TAG="$(tail -n 1 "$declog" | jq -r '.pattern // ""' 2>/dev/null)"
    fi
}

# ---- classifier self-test: missing / failing / malformed hooks -------------
selftest() {
    local d="$TMP_ROOT/selftest" ok=1 p="$TMP_ROOT/selftest/payload.json"
    mkdir -p "$d/h/hooks" "$d/h/logs"
    printf '%s' '{"tool_name":"Bash","tool_input":{"command":"true"},"cwd":"/"}' > "$p"
    printf '#!/usr/bin/env bash\nexit 3\n' > "$d/h/hooks/exit3.sh"
    printf '#!/usr/bin/env bash\necho not-json\n' > "$d/h/hooks/garbage.sh"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$d/h/hooks/empty.sh"
    printf '#!/usr/bin/env bash\necho x >> "$(dirname "$0")/../logs/hook-errors.log"\nexit 0\n' > "$d/h/hooks/errlog.sh"
    printf '#!/usr/bin/env bash\nprintf "%%s" "{\\"hookSpecificOutput\\":{\\"hookEventName\\":\\"PreToolUse\\",\\"permissionDecision\\":\\"deny\\",\\"permissionDecisionReason\\":\\"x\\"}}"\n' > "$d/h/hooks/deny.sh"
    printf '#!/usr/bin/env bash\nprintf "%%s" "{\\"hookSpecificOutput\\":{\\"permissionDecision\\":\\"deny\\"}}"\n' > "$d/h/hooks/noevent.sh"
    local pair name want
    for pair in "missing.sh:error:missing-hook" "exit3.sh:error:exit-3" "garbage.sh:error:malformed" \
                "noevent.sh:error:malformed" "errlog.sh:error:hook-error-logged" \
                "empty.sh:allow-empty" "deny.sh:deny"; do
        name="${pair%%:*}"; want="${pair#*:}"
        run_hook "$d/h/hooks/$name" "$p" / / "selftest-${name%.sh}"
        if [[ "$H_CLASS" != "$want" ]]; then
            echo "SELFTEST FAIL: $name -> $H_CLASS (wanted $want)" >&2; ok=0
        fi
    done
    [[ "$ok" == 1 ]]
}
if ! selftest; then echo "classifier self-test failed; refusing to run" >&2; exit 2; fi
echo "classifier self-test: ok (missing/exit/malformed/hook-error-logged are errors; empty output is allow)"
[[ "$SELFTEST_ONLY" == 1 ]] && exit 0

# ---- stage the isolated fixture repository -------------------------------
g() { git -C "$1" -c user.name=fixture -c user.email=fixture@example.invalid \
          -c commit.gpgsign=false -c init.defaultBranch=main "${@:2}"; }
FIX="$TMP_ROOT/main"; WT="$FIX/.loom/worktrees/issue-1"; WT2="$FIX/.loom/worktrees/issue-2"
SCRATCH="$TMP_ROOT/scratch"; LINK="$TMP_ROOT/scratch-link-to-agent2"
mkdir -p "$FIX/.loom" "$SCRATCH"
g "$FIX" init -q
cp -R "$SRC_HOOKS" "$FIX/.loom/hooks"
cp -R "$SRC_SCRIPTS" "$FIX/.loom/scripts"
# Mirror a Repo Skills install, if the repository has one, so the dispatcher
# probes see the same canonical guard they would see in the real checkout.
if [[ -d "$REPO_ROOT/.claude/skills/repo" ]]; then
    mkdir -p "$FIX/.claude/skills"; cp -R "$REPO_ROOT/.claude/skills/repo" "$FIX/.claude/skills/repo"
fi
mkdir -p "$FIX/.loom/logs"
if [[ -r "$REPO_ROOT/.loom/config.json" ]]; then cp "$REPO_ROOT/.loom/config.json" "$FIX/.loom/config.json"; fi
printf '%s\n' '.loom/' '.claude/' > "$FIX/.gitignore"
printf '%s\n' '# fixture' > "$FIX/README.md"
g "$FIX" add .gitignore README.md
g "$FIX" commit -q -m fixture
g "$FIX" worktree add -q -b feature/issue-1 "$WT" >/dev/null 2>&1
g "$FIX" worktree add -q -b feature/issue-2 "$WT2" >/dev/null 2>&1
printf '%s\n' '# Loom-managed worktree marker (fixture)' '# Issue: 1' > "$WT/.loom-managed"
printf '%s\n' '# Loom-managed worktree marker (fixture)' '# Issue: 2' > "$WT2/.loom-managed"
ln -s "$WT2" "$LINK"
if [[ ! -f "$WT/.loom-managed" || ! -f "$WT2/.loom-managed" || -n "$(g "$FIX" status --porcelain)" ]]; then
    echo "fixture setup failed" >&2; exit 2
fi

# ---- selected implementation (mirrors the dispatcher's probes, read-only) --
DISPATCHER="$FIX/.loom/hooks/guard-destructive.sh"
CANON="$FIX/.claude/skills/repo/hooks/guard-destructive.sh"
VEND="$FIX/.loom/hooks/guard-destructive-generic.sh"
SELECTED="$VEND"; SELECTED_WHY="canonical guard absent -> vendored fallback"
if [[ -r "$CANON" ]]; then
    if grep -q 'repo#29' "$CANON" && grep -q 'worktree-write-confinement' "$CANON" \
       && grep -qF -- '--comment|--search' "$CANON" && grep -qF -- '--arg|--argjson' "$CANON" \
       && grep -q -- 'gh-comment-body-literal-at' "$CANON"; then
        SELECTED="$CANON"; SELECTED_WHY="canonical guard present and passes all probes"
    else
        SELECTED_WHY="canonical guard present but fails a probe -> vendored fallback"
    fi
fi
sha() { sha256sum "$1" | cut -d' ' -f1; }
REPO_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
REPO_DIRTY="$(git -C "$REPO_ROOT" status --porcelain -- . ':!docs/guard-confinement-reproduction.md' ':!docs/fixtures' ':!scripts/guard-confinement-repro.sh' ':!docs/sweep-baseline.md' | wc -l)"
PRIMARY_MAIN="$(cd "$(git -C "$REPO_ROOT" rev-parse --git-common-dir)/.." && pwd -P)"
STATUS_BEFORE="$(git -C "$PRIMARY_MAIN" status --porcelain 2>/dev/null)"
GUARDS_CFG="$(jq -c '.guards // {}' "$REPO_ROOT/.loom/config.json" 2>/dev/null || echo '"unreadable-or-absent"')"

# ---- evaluate each case ---------------------------------------------------
BASH_HOOKS=("$FIX/.loom/hooks/guard-destructive.sh" "$FIX/.loom/hooks/guard-loom-workflow.sh")
EW_HOOKS=("$FIX/.loom/hooks/guard-worktree-paths.sh")
: > "$RES/cases.jsonl"
n=0
while IFS= read -r idx; do
    n=$((n+1))
    cid="$(jq -r ".cases[$idx].id" "$CASES")"
    payload="$RES/payload-$cid.json"
    # Build the stdin payload; placeholders are replaced inside jq only.
    jq -c --argjson i "$idx" --arg M "$FIX" --arg W "$WT" --arg W2 "$WT2" --arg S "$SCRATCH" --arg L "$LINK" '
        def sub: gsub("@MAIN@";$M) | gsub("@WT@";$W) | gsub("@WT2@";$W2) | gsub("@SCRATCH@";$S) | gsub("@LINK@";$L);
        .cases[$i] | {hook_event_name:"PreToolUse", tool_name, tool_input:(.tool_input | walk(if type=="string" then sub else . end)), cwd:(.cwd|sub)}
    ' "$CASES" > "$payload"
    cwd="$(jq -r '.cwd' "$payload")"
    chain="$(jq -r ".cases[$idx].chain" "$CASES")"
    if [[ "$chain" == "bash" ]]; then hooks=("${BASH_HOOKS[@]}"); else hooks=("${EW_HOOKS[@]}"); fi
    hres="[]"
    for h in "${hooks[@]}"; do
        run_hook "$h" "$payload" "$cwd" "$FIX" "$cid-$(basename "$h" .sh)"
        hres="$(jq -c --arg h "$(basename "$h")" --arg c "$H_CLASS" --arg d "$H_DECISION" --arg t "$H_TAG" \
            --arg r "$H_REASON" --arg e "$H_EXIT" --argjson sb "${H_STDERR_BYTES:-0}" \
            '. + [{hook:$h, class:$c, decision:$d, tag:$t, exit:$e, stderr_bytes:$sb, reason_head:$r}]' <<<"$hres")"
    done
    jq -c --argjson i "$idx" --argjson hooks "$hres" '
        .cases[$i] as $c
        | ($hooks | map(.decision)) as $ds
        | (if any($ds[]; .=="deny") then "deny" elif any($ds[]; .=="ask") then "ask"
           elif any($ds[]; .=="error") then "error" else "allow" end) as $dec
        | ($hooks | map(select(.decision=="deny" or .decision=="ask") | .tag) | first // "") as $tag
        | ($hooks | map(select(.decision=="deny" or .decision=="ask") | .hook) | first // "") as $by
        | ($c.expect.policy) as $p
        | (if $dec=="error" then "INCONCLUSIVE"
           elif $p=="record" then "RECORDED"
           elif $p=="allow" then (if $dec=="allow" then "MATCH" else "DISCREPANCY" end)
           else (if $dec=="deny" then
                   (if ($c.expect.tag_prefix // "") == "" or ($tag | startswith($c.expect.tag_prefix)) then "MATCH" else "TAG-MISMATCH" end)
                 else "DISCREPANCY" end) end) as $v
        | {id:$c.id, family:$c.family, provenance:$c.provenance, chain:$c.chain, cwd_role:$c.cwd,
           expected:$c.expect, observed:{decision:$dec, tag:$tag, decided_by:$by}, verdict:$v, hooks:$hooks}
    ' "$CASES" >> "$RES/cases.jsonl"
done < <(jq -r '.cases | keys[]' "$CASES")

STATUS_AFTER="$(git -C "$PRIMARY_MAIN" status --porcelain 2>/dev/null)"

# ---- assemble the result document ------------------------------------------
RESULT="$(jq -s --arg root "$TMP_ROOT" --arg commit "$REPO_COMMIT" --argjson dirty "$REPO_DIRTY" \
    --arg disp "$(sha "$DISPATCHER")" --arg sel "$SELECTED" --arg selrel "${SELECTED#$FIX/}" \
    --arg selsha "$(sha "$SELECTED")" --arg why "$SELECTED_WHY" \
    --arg lw "$(sha "$FIX/.loom/hooks/guard-loom-workflow.sh")" --arg wp "$(sha "$FIX/.loom/hooks/guard-worktree-paths.sh")" \
    --arg bash "$(bash --version | head -1)" --arg jqv "$(jq --version)" --arg gitv "$(git --version)" \
    --arg os "$(uname -sr)" --arg guards "$GUARDS_CFG" \
    --arg before "$STATUS_BEFORE" --arg after "$STATUS_AFTER" --arg primary "$PRIMARY_MAIN" '
    {
      environment: {fixture_root:$root, repo_commit:$commit, repo_dirty_paths_excluding_deliverables:$dirty,
        dispatcher_sha256:$disp, selected_guard_relpath:$selrel, selected_guard_sha256:$selsha, selection_reason:$why,
        guard_loom_workflow_sha256:$lw, guard_worktree_paths_sha256:$wp, repo_guards_config:($guards|fromjson? // $guards),
        bash:$bash, jq:$jqv, git:$gitv, os:$os},
      primary_checkout: {path:$primary, status_unchanged:($before==$after)},
      summary: (group_by(.verdict) | map({(.[0].verdict): length}) | add),
      cases: .
    }' "$RES/cases.jsonl")"
[[ -n "$OUT" ]] || OUT="$TMP_ROOT/result.json"
printf '%s\n' "$RESULT" > "$OUT"

echo "fixture: $TMP_ROOT"
echo "result : $OUT"
jq -r '.environment | "repo commit : \(.repo_commit)\nselected    : \(.selected_guard_relpath) sha256=\(.selected_guard_sha256) (\(.selection_reason))"' <<<"$RESULT"
jq -r '.cases[] | [.id, .expected.policy, .observed.decision, (.observed.tag|if .=="" then "-" else . end), .observed.decided_by, .verdict] | @tsv' <<<"$RESULT" | column -t -s $'\t'
echo "summary: $(jq -c .summary <<<"$RESULT")"
if [[ "$STATUS_BEFORE" == "$STATUS_AFTER" ]]; then echo "primary checkout status: unchanged"; else
    echo "primary checkout status: CHANGED during run (may be live orchestration; inspect)"; diff <(printf '%s\n' "$STATUS_BEFORE") <(printf '%s\n' "$STATUS_AFTER") || true; fi

rc=0
if jq -e '.summary | has("INCONCLUSIVE")' <<<"$RESULT" >/dev/null; then rc=2
elif jq -e '.summary | (has("DISCREPANCY") or has("TAG-MISMATCH"))' <<<"$RESULT" >/dev/null; then rc=1; fi
exit "$rc"
