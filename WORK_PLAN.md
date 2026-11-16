# Work Plan

Current Loom label state. Tracking and blocked items do not constitute ready work.

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#352**: docs: index DR-0006 and name the PSRR full-envelope study as supplementary evidence

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

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#331**: docs: index DR-0006 in spec/README.md and correct the README's passive-corner coverage statement
- **#337**: Stop four benches importing sim/cmrr/run_cmrr.py as a library: hoist its bench-agnostic half into a shared module
- **#338**: layout/pilot: negative-control gate's layout_sha256 half is vacuously true if klt stops emitting the field
- **#350**: sim/test_harness.py: pin the add_batch_args dest contract that batch_block()/submit_request() read
- **#355**: docs: drop the completed ROADMAP 'Documentation corrections' item once #331 lands
- **#358**: Hoist the duplicated main() orchestration shared by step-response and overdrive-recovery into harness
- **#360**: MODULE_REL is the one INTERFACE_CONSTANTS entry its own rule does not actually pin
- **#365**: sim: record/probe exec lines print klt job `state: failed` with no exit-reason context
- **#366**: sim/ci_regression_check.sh: no deliberate-regression entry proves the cmrr-mc chunk-planner and merge guards have teeth

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#371**: docs: prune completed ROADMAP Now items and cite tracked issues
- **#374**: test: add cmrr-mc mutation entries for chunk-plan and merge guards
- **#376**: test: pin MODULE_REL to the loaded module in Contract.test_module_contract

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#352**: docs: index DR-0006 and name the PSRR full-envelope study as supplementary evidence
- **#354**: fix: require layout_sha256 present before the pilot negative-control gate trusts it
- **#362**: refactor: hoist the transient-grid run flow into harness.run_transient_grid
- **#368**: test: pin add_batch_args dest contract for batch_block and submit_request
- **#369**: fix(sim): render klt's exit_reason next to state in record execution lines

## Proposed

Issues carrying `loom:curated`.

- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)*
- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load *(curated)*
- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(curated)*
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence *(curated)*
- **#331**: docs: index DR-0006 in spec/README.md and correct the README's passive-corner coverage statement *(curated)*
- **#337**: Stop four benches importing sim/cmrr/run_cmrr.py as a library: hoist its bench-agnostic half into a shared module *(curated)*
- **#338**: layout/pilot: negative-control gate's layout_sha256 half is vacuously true if klt stops emitting the field *(curated)*
- **#350**: sim/test_harness.py: pin the add_batch_args dest contract that batch_block()/submit_request() read *(curated)*
- **#355**: docs: drop the completed ROADMAP 'Documentation corrections' item once #331 lands *(curated)*
- **#360**: MODULE_REL is the one INTERFACE_CONSTANTS entry its own rule does not actually pin *(curated)*
- **#365**: sim: record/probe exec lines print klt job `state: failed` with no exit-reason context *(curated)*
- **#366**: sim/ci_regression_check.sh: no deliberate-regression entry proves the cmrr-mc chunk-planner and merge guards have teeth *(curated)*
- **#367**: Tighten check_consistency.py's mirror of tools/pilot.py (sharing guard, negctl nf, unknown-schema message) *(curated)*
- **#373**: report: decide whether the full-grid CMRR mismatch record (45 points x N=300) supersedes the nominal record in the characterization report *(curated)*

## Proposed (Architect / Hermit)

- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(hermit)*
- **#322**: Scope the passive RZ x CC matrix per figure: slew-swing-power re-runs 660 provably identical SPICE cells per full-scope study *(hermit)*
- **#340**: Consolidate 8 duplicate claim_record_paths append-only tests into sim/test_harness.py *(hermit)*

## Epics

_None._

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 1 |
| Important | 4 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 9 |
| PRs awaiting review | 3 |
| Approved PRs awaiting merge | 5 |
| Curated | 14 |
| Architect / Hermit proposals | 3 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
