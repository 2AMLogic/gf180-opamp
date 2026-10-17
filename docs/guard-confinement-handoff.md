# Guard-confinement evidence: upstream handoff preparation (issue #214)

Status: **prepared, not transferred.** No upstream filing exists for these
exact cases and no replacement location has been reviewed, so the three
evidence files below stay in this repository. This document changes no guard,
hook, setting or evidence file; it only maps the existing cases to the owners
who could act on them. Nothing here was re-run (the 2026-10-11 recorded runs
in the reproduction doc are the only measurements).

## Files in the package (unchanged; sha256 at preparation time)

| File | sha256 |
| --- | --- |
| `docs/guard-confinement-reproduction.md` | `0975ec8db83b097c7c27f95a26c43d76b5d6d5765ae765799f68fca21e8047d7` |
| `docs/fixtures/guard-confinement-cases.json` | `230be55be97aeda49d10860c9051e251483199e1d9487350d02d13c0d5b96cba` |
| `scripts/guard-confinement-repro.sh` | `6193148b76b19ead642d130c1dfc0ba4f9f9c462de06f0147790c4c8ed0b1f50` |

Retained locally and **not** part of the handoff: `docs/sweep-baseline.md`,
`scripts/test-sweep-baseline.sh`, and the README "Orchestration notes" section.

## Evidence classes (must stay distinct in any transfer)

1. **Historical observation** - the auditor's two claimed
   `worktree-write-confinement` denials of 2026-10-11 (00:02:39Z and
   01:58:49Z). Unverified and attributed; `.loom/logs/guard-decisions.log` was
   absent, so no command text exists. **Not reproduced** by any synthetic case.
   Must not be relabelled as reproduced, and old evidence must not be used to
   certify current guard behavior.
2. **Synthetic cases** - all 29 payloads in the fixture
   (`provenance: "synthetic"`), written from the audit prose and documented
   rules.
3. **Reproduced findings** - measured on 2026-10-11 against repo commit
   `47dd65cd16f3ecf473f3b37b4187c6c112101b30`, dispatcher
   `guard-destructive.sh` sha256 `3d9eed6e...e26c`, `guard-worktree-paths.sh`
   `89b44376...02c12f`, `guard-loom-workflow.sh` `16962078...9ba58`: totals
   `{"DISCREPANCY":4,"MATCH":15,"RECORDED":10}` (full hashes and per-case table
   in the reproduction doc).
4. **Limitations** - hook-only; settings wiring and `LOOM_WORKTREE_PATH`-set
   sessions not exercised; canonical Repo guard not installed, so nothing says
   how it decides.

## Owner mapping (exact cases)

| Cases | Observation | Proposed owner | Why |
| --- | --- | --- | --- |
| A1-A3, B1-B2, D4, F5 | allow controls, MATCH | `rjwalters/repo` (regression) | Generic Bash write-confinement guard; controls guard against over-blocking. |
| C1-C4 | `mktemp` shapes; C2, C4 denied `...-unresolved-var` (recorded, no contract presumed) | `rjwalters/repo` | Policy question on `wt_write_mktemp_same_command_safe()` coverage in the canonical guard. |
| D1-D3, D5-D6 | checkpoint/lease/registry script strings; all allowed (recorded) | `rjwalters/loom` | Those scripts and the historical denial context are Loom's; only informs where the unrecovered original command may have differed. |
| E1-E9 (negative controls) | unresolved / main-checkout / relative / rebound-variable writes denied (E3 recorded) | `rjwalters/repo` | Confinement expectations; **must travel with the cases** so a replacement never loses the negative tests. |
| F1, F2 (Bash, other agent's worktree, incl. symlink) | allowed; expected deny - DISCREPANCY | `rjwalters/repo` | Generic guard decides Bash redirects. |
| F3, F4 (Write tool, other agent's worktree, incl. symlink) | allowed; expected deny - DISCREPANCY | `rjwalters/loom` | `guard-worktree-paths.sh` is Loom-owned and documents it cannot verify session ownership (#4245). |
| Resync / vendoring of any fix | - | `rjwalters/loom` | `.loom/hooks/guard-destructive-generic.sh` is a vendored copy; fixes arrive via the resync flow, never by editing it here. |

Related umbrella `rjwalters/loom#11113` is **not** receipt of these cases: it
has not been checked against the F-family and C2/C4 shapes.

## Transfer condition (gate before any local deletion)

All must hold before a removal PR is opened; none holds today.

- [ ] An exact, reviewed upstream location (issue/PR/doc path) per owner above
      contains the cases, the classes in "Evidence classes", and usable
      reproduction material (runner or equivalent).
- [ ] Access to that location verified.
- [ ] Negative cases E1-E9 and F1-F4 present in the replacement.
- [ ] Working links from `docs/sweep-baseline.md` and the README to the
      replacement are ready.

Git history, closed #102/#146, or an unreviewed filing do not satisfy this.
Upstream filing is an operator action (this task does not authorize it); the
rjwalters/repo and rjwalters/loom trackers were not touched.

## Later removal PR (scope when the condition is met)

May remove only the three files in the table above and adjust inbound links
(`README.md`, `docs/sweep-baseline.md`), name the reviewed replacement, and
show `rg "guard-confinement"` finds no dangling references.
