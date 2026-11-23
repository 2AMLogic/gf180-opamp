# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
_Generated: 2026-11-23T03:23:15Z (snapshot; compare against live labels)_
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(held)*

## Important

`loom:important`; taken before unlabelled work.

- **#62**: spec: ratify noise band/bound and offset 3-sigma bound via a decision record (DR-3 residuals e1, e2) *(held)*
- **#241**: T1 items 2, 3, 4, 11: compose the full op-amp core layout from the pilot cells and mint klt DRC, LVS and ERC evidence *(held)*
- **#242**: T1 item 5: cite the retained klt sim reports per spec row, with declared gaps for rows not yet covered *(held)*
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence *(held)*

## Ready

Human-approved issues (`loom:issue`) ready for implementation — excluding held (`loom:blocked`/`loom:operator`/`loom:frozen`), `loom:building`, and issues closed by an open `loom:pr` PR (listed under Approved).

_None._

## In Progress

Issues currently being built (`loom:building`).

_None._

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

_None._

## Proposed

Issues carrying `loom:curated`.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)* *(held)*
- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load *(curated)* *(held)*
- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(curated)* *(held)*
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence *(curated)* *(held)*

## Proposed (Architect / Hermit)

- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(hermit)* *(held)*
- **#322**: Scope the passive RZ x CC matrix per figure: slew-swing-power re-runs 660 provably identical SPICE cells per full-scope study *(hermit)* *(held)*

## Epics

_None._

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 1 |
| Important | 4 |
| Ready (`loom:issue`, net of holds/building/merge-pending) | 0 |
| In Progress (`loom:building`) | 0 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 0 |
| Curated | 4 |
| Architect / Hermit proposals | 2 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
