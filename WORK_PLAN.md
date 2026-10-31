# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence

## Important

`loom:important`; taken before unlabelled work.

- **#62**: spec: ratify noise band/bound and offset 3-sigma bound via a decision record (DR-3 residuals e1, e2)
- **#241**: T1 items 2, 3, 4, 11: compose the full op-amp core layout from the pilot cells and mint klt DRC, LVS and ERC evidence
- **#242**: T1 item 5: cite the retained klt sim reports per spec row, with declared gaps for rows not yet covered

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence

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

- **#98**: sim: CMRR mismatch Monte Carlo over the T/VDD grid (45 points x N=300) *(curated)*
- **#190**: sim: extend PSRR mismatch characterization to the full temperature and supply grid *(curated)*
- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

_None._

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 1 |
| Important | 3 |
| Ready (`loom:issue`) | 1 |
| In Progress (`loom:building`) | 0 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 0 |
| Curated | 3 |
| Architect / Hermit proposals | 0 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
