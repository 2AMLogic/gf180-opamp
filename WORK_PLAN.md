# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#307**: docs(spec): add DR-0008 preparation record for load-specific GBW/PM row

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

- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load

## In Progress

Issues currently being built (`loom:building`).

- **#190**: sim: extend PSRR mismatch characterization to the full temperature and supply grid

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#307**: docs(spec): add DR-0008 preparation record for load-specific GBW/PM row

## Proposed

Issues carrying `loom:curated`.

- **#98**: sim: CMRR mismatch Monte Carlo over the T/VDD grid (45 points x N=300) *(curated)*
- **#140**: cmrr-mc: full grid as a single batch request exceeds the 3600 s fleet job limit *(curated)*
- **#190**: sim: extend PSRR mismatch characterization to the full temperature and supply grid *(curated)*
- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)*
- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load *(curated)*

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
| Ready (`loom:issue`) | 1 |
| In Progress (`loom:building`) | 1 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 1 |
| Curated | 5 |
| Architect / Hermit proposals | 0 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
