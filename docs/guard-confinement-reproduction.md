# Guard confinement reproduction and evidence package (issue #146)

Purpose: give upstream a reviewable, repeatable record of how the installed
Bash and Edit/Write `PreToolUse` guards decide on scratch-path, checkpoint /
lease / registry cleanup, and cross-worktree writes. This is evidence only. It
changes no guard, hook, generated skill or setting, and it never touches a real
checkpoint, lease or run registry.

## Evidence classes (kept separate)

| Class | What it is | Content here |
| --- | --- | --- |
| Historical observation | The original auditor's report (issue #146). | Two claimed denials tagged `worktree-write-confinement` (2026-10-11T00:02:39Z Judge verdict body under `/tmp`; 2026-10-11T01:58:49Z bundled checkpoint delete, lease release, registry cleanup and `/tmp` body write). **`.loom/logs/guard-decisions.log` is absent, so no original command text exists and none is invented.** Unverified, attributed to the auditor. |
| Synthetic case | A payload written for this package from the audit prose and the documented rules. | Every case in `docs/fixtures/guard-confinement-cases.json` (`provenance: "synthetic"`). None claims to be the historical command. |
| Reproduced defect | A synthetic case whose measured decision contradicts a stated contract. | See "Results" and "Discrepancies". |

The historical denials are **not reproduced** by any synthetic case below (see
D-family results), so this package cannot confirm or refute them.

## Files

- `docs/fixtures/guard-confinement-cases.json` - 29 inert payloads in six
  families (A literal scratch, B literal variable, C mktemp, D
  checkpoint/lease/registry, E negative controls, F other-agent worktree), each
  with `expect.policy` (`allow`, `deny`, or `record` = no presumed contract),
  optional `expect.tag_prefix`, and rationale. Placeholders `@MAIN@ @WT@ @WT2@
  @SCRATCH@ @LINK@` are substituted by `jq` inside the fixture.
- `scripts/guard-confinement-repro.sh` - the repeatable runner.

## Reproduce (from a fresh checkout)

```bash
jq -e '[.cases[].id] | length == (unique | length)' docs/fixtures/guard-confinement-cases.json
scripts/guard-confinement-repro.sh --out "$(mktemp -d)/result.json"   # or --keep to retain the fixture
```

What the runner does:

1. Refuses to run unless a classifier self-test passes: fake hooks that are
   missing, exit non-zero, print non-JSON, print JSON without
   `hookEventName`, or write to `hook-errors.log` must classify as `error:*`;
   an empty-output hook must classify as `allow-empty`; a well-formed deny as
   `deny`.
2. Builds a fresh `mktemp -d` fixture git repo with copies of the installed
   `.loom/hooks` and `.loom/scripts`, a mirrored `.claude/skills/repo` if the
   checkout has one, two synthetic managed worktrees (`issue-1` = the acting
   agent, `issue-2` = another agent, each with a `.loom-managed` sentinel), an
   external scratch dir, and a scratch symlink to `issue-2`. Follows the setup
   of `.loom/hooks/tests/test-guard-worktree-paths.sh` and
   `scripts/test-sweep-baseline.sh`.
3. For every case, builds the PreToolUse JSON with `jq` and writes it to each
   guard process on **stdin only**. Command strings are never eval'd, sourced
   or executed. The effective chain is `guard-destructive.sh` (dispatcher, which
   `exec`s the selected implementation) plus `guard-loom-workflow.sh` for Bash
   payloads, and `guard-worktree-paths.sh` for Write payloads. The decision tag
   is read from a per-call decision log (`LOOM_GUARD_DECISION_LOG=1`,
   `LOOM_GUARD_DECISION_LOG_FILE`, set for the harness process only).
4. Classifies each hook process: `allow-empty` (exit 0, empty stdout, nothing
   written to `hook-errors.log`; the protocol's documented allow), `allow-json`,
   `deny`, `ask`, or an error: `error:missing-hook`, `error:exit-N`,
   `error:timeout`, `error:malformed`, `error:hook-error-logged` (the hook's ERR
   trap fired and failed open - not evidence of allow). Case verdicts:
   `MATCH`, `DISCREPANCY`, `TAG-MISMATCH`, `RECORDED`, `INCONCLUSIVE` (an error
   and no deny). Exit 0 / 1 (discrepancy) / 2 (inconclusive or setup failure).
5. Records the selected guard path, sha256, selection reason, the other hook
   hashes, repo commit, tool versions and `guards` config in the result JSON,
   and checks that the primary checkout's `git status --porcelain` is unchanged.
   The selection is computed by mirroring the dispatcher's four read-only grep
   probes against the fixture's `.claude/skills/repo/hooks/guard-destructive.sh`
   (the dispatcher itself does not report which guard it exec'd).

The runner only reads installed hook files. It does not run checkpoint, lease or
registry scripts; those strings appear only as stdin data to the guards.

## Recorded runs

Host `loom-worker`, Linux 7.0.0-1013-aws, bash 5.2.21, jq 1.7, git 2.43.0.
Repo commit `47dd65cd16f3ecf473f3b37b4187c6c112101b30` (clean apart from this
package). `guards` config in `.loom/config.json`: none (`{}`); all `LOOM_*`
overrides unset.

Selected implementation (both runs): **`.loom/hooks/guard-destructive-generic.sh`**
(vendored fallback), sha256 `64d4f0956cb15e03d7b9fcb582d0081dac9a218bb1d237c9844e37048fe88acb`,
because `.claude/skills/repo/hooks/guard-destructive.sh` (canonical Repo Skills)
is not installed. Dispatcher `guard-destructive.sh` sha256
`3d9eed6e01de13bcfdb1f473f2c3363ac8debce73c7cbb8afcca9d8a8091e26c`;
`guard-loom-workflow.sh` `169620785f8a14a3f1f31668cafd64ec3f684f7336d6de15dbf282c7fdd9ba58`;
`guard-worktree-paths.sh` `89b4437644ade83d7f04d456abd738e346a9467387411c8f174f66ab7f02c12f`.
Because the canonical guard is absent here, nothing in this package says how the
canonical guard decides; the vendored copy is the installed, active one.

Two independent runs, in separate scratch fixtures (`/tmp/guard-confinement-repro.j9YknA`
and `/tmp/guard-confinement-repro.HeIiCx`, both removed afterwards): after
normalizing the fixture root and dropping the free-text reason, the two result
documents were **byte-identical** (no environmental difference observed). Both:
`{"DISCREPANCY":4,"MATCH":15,"RECORDED":10}`, no errors, primary checkout
status unchanged. Exit status 1 (discrepancies).

### Results (observed; same in both runs)

| Case | Expected | Observed | Tag (decided by) | Verdict |
| --- | --- | --- | --- | --- |
| A1 literal redirect | allow | allow | - | MATCH |
| A2 quoted body path | allow | allow | - | MATCH |
| A3 heredoc body | allow | allow | - | MATCH |
| B1 literal var, redirect | allow | allow | - | MATCH |
| B2 literal var, quoted | allow | allow | - | MATCH |
| C1 `D=$(mktemp -d); > $D/verdict.md` | record | allow | - | RECORDED |
| C2 `D=$(mktemp -d <tmpl>); cp ... $D/` | record | **deny** | `worktree-write-confinement-unresolved-var` (guard-destructive) | RECORDED |
| C3 `D=$(mktemp -d)`, quoted `"$D/..."` | record | allow | - | RECORDED |
| C4 inline `> $(mktemp -d)/verdict.md` | record | **deny** | `worktree-write-confinement-unresolved-var` | RECORDED |
| D1 checkpoint delete alone | record | allow | - | RECORDED |
| D2 lease release alone | record | allow | - | RECORDED |
| D3 registry cleanup alone | record | allow | - | RECORDED |
| D4 follow-up body to literal scratch | allow | allow | - | MATCH |
| D5 bundle of D1-D4 (cwd = worktree) | record | allow | - | RECORDED |
| D6 same bundle, cwd = main checkout | record | allow | - | RECORDED |
| E1 unresolved `$V/fresh.json` | deny | deny | `...-unresolved-var` | MATCH |
| E2 var rebound to main | deny | deny | `...-unresolved-var` | MATCH |
| E3 var rebound main then scratch | record | deny | `...-unresolved-var` | RECORDED |
| E4 literal redirect into main | deny | deny | `worktree-write-confinement` | MATCH |
| E5 mkdir into main | deny | deny | `worktree-write-confinement` | MATCH |
| E6 relative redirect, cwd main | deny | deny | `worktree-write-confinement` | MATCH |
| E7 scratch then unresolved | deny | deny | `...-unresolved-var` | MATCH |
| E8 scratch then main | deny | deny | `worktree-write-confinement` | MATCH |
| E9 mktemp then main | deny | deny | `worktree-write-confinement` | MATCH |
| F1 redirect into other agent's worktree | deny | **allow** | - | **DISCREPANCY** |
| F2 redirect via symlink to it | deny | **allow** | - | **DISCREPANCY** |
| F3 Write tool into it | deny | **allow** | - | **DISCREPANCY** |
| F4 Write tool via symlink to it | deny | **allow** | - | **DISCREPANCY** |
| F5 Write tool, own worktree (control) | allow | allow | - | MATCH |

All `allow` results here are `allow-empty` (documented empty-output allow) with
exit 0, zero stderr bytes, and nothing in `hook-errors.log`; no
hook error, timeout or malformed output occurred in either run.

## Case disposition and explanations

- **A, B**: contract holds. Literal and same-command-literal scratch writes are
  allowed, consistent with `docs/sweep-baseline.md`.
- **C (mktemp)**: the installed source proves provenance only for the exact
  strings `NAME=$(mktemp -d)` / `NAME=$(mktemp)` (`wt_write_mktemp_same_command_safe()`,
  `guard-destructive-generic.sh` ~line 7690, #6949/#6520, heredoc-body masked).
  C1 and C3 hit that rule and allow. C2 (custom template argument) and C4
  (inline substitution, no assignment) are outside it by design and fail closed
  with `-unresolved-var`; the source comment says custom templates are
  deliberately excluded. Whether those should allow is an upstream policy
  question, not asserted here. Note the C2 deny is *not* about the `cp`: the
  template itself is a literal scratch path, yet the shape is not provable.
- **D (checkpoint / lease / registry)**: none of the individual segments or the
  bundle produced a deny in this fixture, from a worktree or the main-checkout
  cwd; the follow-up body write to a literal path is allowed. The guard does not
  treat those script invocations as write targets at all, so there is no
  role-owned exemption involved and none is proposed. Consequence: **the
  historical `worktree-write-confinement` denial is not reproduced.** The
  original command probably contained a write shape not captured in the audit
  prose (for example a variable- or `$(mktemp)`-derived `/tmp` path like C2/C4, a
  relative target, or a main-checkout path). That is a hypothesis only; recovering
  the real text needs the original `guard-decisions.log`.
- **E (negative controls)**: all hold, and scratch / mktemp handling in the same
  command (E7-E9) does not weaken them. E3 (`S=main; S=scratch`) is denied as
  unresolved-var, i.e. conflicting bindings fail closed; no contract was
  presumed so it is recorded.
- **F (other agent's worktree)**: all four cross-worktree cases are **allowed**,
  including through a symlink. Ownership evidence: the guards recognise a
  worktree solely by the `.loom-managed` sentinel / git registration of the
  directory; `guard-worktree-paths.sh` documents "this check cannot verify it
  belongs to the acting session - see #4245". With `LOOM_WORKTREE_PATH` unset
  (the daemon-dispatched sweep shape, #3719) there is no per-agent identity to
  compare. Per the issue, confinement is required, so these are recorded as
  **upstream discrepancies**, not new approved behavior. Variants with
  `LOOM_WORKTREE_PATH` set (tmux/manual sessions) were not exercised: unexercised,
  inconclusive, not evidence of either decision.

## Source ownership and resync path

- Active implementation: `.loom/hooks/guard-destructive-generic.sh`, a vendored
  copy; do not hand-edit. Canonical generic guard:
  [rjwalters/repo `hooks/repo/guard-destructive.sh`](https://github.com/rjwalters/repo/blob/main/hooks/repo/guard-destructive.sh).
  Loom dispatcher and fallback:
  [`defaults/hooks/guard-destructive.sh`](https://github.com/rjwalters/loom/blob/main/defaults/hooks/guard-destructive.sh),
  [`defaults/hooks/guard-destructive-generic.sh`](https://github.com/rjwalters/loom/blob/main/defaults/hooks/guard-destructive-generic.sh).
  `guard-worktree-paths.sh` is Loom-owned.
- Upstream regression suites to extend with these shapes:
  `tests/hooks/test-guard-destructive-write-confinement.sh`,
  `test-guard-destructive-mktemp-canon.sh`,
  `test-guard-destructive-session-scratch.sh` (Loom).
- Related umbrella: [rjwalters/loom#11113](https://github.com/rjwalters/loom/issues/11113).
  Not cross-referenced as a duplicate: it has not been checked against the
  precise F-family and C2/C4 shapes here, and no historical event was
  reproduced. Add the link to a specific member only after a maintainer
  confirms the shape matches.
- Any fix belongs upstream with its regression suite, then reaches this repo
  through the supported Loom release / resync flow. Nothing in this repo's
  `.loom/hooks/*`, generated skills or guard settings is edited by this package.
  Upstream publication is not a dependency of this issue.

## Limits

Hook-only: no real Claude Code session, so the settings wiring (`.claude/settings.json`
matchers) was not exercised; Bash payloads use the two hooks configured for the
Bash matcher. The selected-guard determination mirrors the dispatcher probes
rather than tracing the `exec`. The canonical Repo Skills guard is not installed
here and was not evaluated.
