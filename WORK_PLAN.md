# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs stuck under a `loom:operator` merge-risk hold — implementation work is done, only a human merge decision is missing.

- **#79**: feat(#77): GF180 layout device pilot (isolated cells, regen recipe, DRC/LVS evidence)

## Operator Priority

Issues the operator starred (`loom:operator-priority`); land these first.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#99**: sim: PSRR+ / PSRR- mismatch Monte Carlo

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

_None._

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#79**: feat(#77): GF180 layout device pilot (isolated cells, regen recipe, DRC/LVS evidence)

## Proposed

Issues carrying `loom:curated`.

- **#7**: Track the gap to T1 sim-validated / bronze (klayout-tools design-evidence tiers) *(curated)*
- **#77**: Prepare a reproducible GF180 layout device pilot before full op-amp placement *(curated)*
- **#98**: sim: CMRR mismatch Monte Carlo over the T/VDD grid (45 points x N=300) *(curated)*
- **#99**: sim: PSRR+ / PSRR- mismatch Monte Carlo *(curated)*
- **#140**: cmrr-mc: full grid as a single batch request exceeds the 3600 s fleet job limit *(curated)*

## Proposed (Architect / Hermit)

- **#62**: spec: ratify noise band/bound and offset 3-sigma bound via a decision record (DR-3 residuals e1, e2) *(architect)*
- **#105**: Consolidate duplicated guard_dut and guard_testbench core into sim/harness.py *(hermit)*
- **#142**: sim: run the gain/GBW/PM mismatch Monte Carlo campaign on the retuned DUT now that the #42 gate is open *(architect)*

## Epics

_None._

## Backlog Balance

| Tier | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Operator priority | 0 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 1 |
| PRs awaiting review | 0 |
| Approved PRs awaiting merge | 1 |
| Curated | 5 |
| Architect / Hermit proposals | 3 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
