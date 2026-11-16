# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#352**: docs: index DR-0006 and name the PSRR full-envelope study as supplementary evidence
- **#383**: chore(loom): resync installed Loom to v0.19.1302

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence

## Important

`loom:important`; taken before unlabelled work.

- **#62**: spec: ratify noise band/bound and offset 3-sigma bound via a decision record (DR-3 residuals e1, e2)
- **#241**: T1 items 2, 3, 4, 11: compose the full op-amp core layout from the pilot cells and mint klt DRC, LVS and ERC evidence
- **#242**: T1 item 5: cite the retained klt sim reports per spec row, with declared gaps for rows not yet covered
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#394**: sim/harness.py: state_phrase renders a leading `; ` when exit_reason carries only unit counts

## In Progress

Issues currently being built (`loom:building`).

- **#331**: docs: index DR-0006 in spec/README.md and correct the README's passive-corner coverage statement

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#352**: docs: index DR-0006 and name the PSRR full-envelope study as supplementary evidence
- **#383**: chore(loom): resync installed Loom to v0.19.1302

## Proposed

Issues carrying `loom:curated`.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)*
- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load *(curated)*
- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(curated)*
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence *(curated)*
- **#331**: docs: index DR-0006 in spec/README.md and correct the README's passive-corner coverage statement *(curated)*
- **#394**: sim/harness.py: state_phrase renders a leading `; ` when exit_reason carries only unit counts *(curated)*

## Proposed (Architect / Hermit)

- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(hermit)*
- **#322**: Scope the passive RZ x CC matrix per figure: slew-swing-power re-runs 660 provably identical SPICE cells per full-scope study *(hermit)*
- **#340**: Consolidate 8 duplicate claim_record_paths append-only tests into sim/test_harness.py *(hermit)*

## Epics

_None._

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 2 |
| Very important | 1 |
| Important | 4 |
| Ready (`loom:issue`) | 1 |
| In Progress (`loom:building`) | 1 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 2 |
| Curated | 6 |
| Architect / Hermit proposals | 3 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
