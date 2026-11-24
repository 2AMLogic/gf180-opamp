# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
_Generated: 2026-11-24T11:30:00Z (snapshot; compare against live labels)_
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#441**: sim: scope slew-swing-power passive axes per figure (DR-0009)

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(held)*

## Important

`loom:important`; taken before unlabelled work.

- **#62**: spec: ratify noise band/bound and offset 3-sigma bound via a decision record (DR-3 residuals e1, e2) *(held)*
- **#241**: T1 items 2, 3, 4, 11: compose the full op-amp core layout from the pilot cells and mint klt DRC, LVS and ERC evidence *(held)*
- **#242**: T1 item 5: cite the retained klt sim reports per spec row, with declared gaps for rows not yet covered *(held)*

## Ready

Human-approved issues (`loom:issue`) ready for implementation — excluding held (`loom:blocked`/`loom:operator`/`loom:frozen`), `loom:building`, and issues closed by an open `loom:pr` PR (listed under Approved).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#322**: Scope the passive RZ x CC matrix per figure: slew-swing-power re-runs 660 provably identical SPICE cells per full-scope study

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#441**: sim: scope slew-swing-power passive axes per figure (DR-0009)

## Proposed

Issues carrying `loom:curated`.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)* *(held)*
- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load *(curated)* *(held)*
- **#322**: Scope the passive RZ x CC matrix per figure: slew-swing-power re-runs 660 provably identical SPICE cells per full-scope study *(curated)* *(building)*

## Proposed (Architect / Hermit)

_None._

## Epics

_None._

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 1 |
| Important | 3 |
| Ready (`loom:issue`, net of holds/building/merge-pending) | 0 |
| In Progress (`loom:building`) | 1 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 1 |
| Curated | 3 |
| Architect / Hermit proposals | 0 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
