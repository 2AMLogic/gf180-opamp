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
- **#98**: sim: CMRR mismatch Monte Carlo over the T/VDD grid (45 points x N=300)
- **#140**: cmrr-mc: full grid as a single batch request exceeds the 3600 s fleet job limit
- **#241**: T1 items 2, 3, 4, 11: compose the full op-amp core layout from the pilot cells and mint klt DRC, LVS and ERC evidence
- **#242**: T1 item 5: cite the retained klt sim reports per spec row, with declared gaps for rows not yet covered
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#347**: Collapse 3 duplicate StaleDutTests and fake_pdk copies in runner tests into sim/test_harness.py

## In Progress

Issues currently being built (`loom:building`).

- **#320**: Remove dead parameters and an unraised exception class in the sim drivers
- **#330**: test: offline consistency check tying layout/pilot to the schematic netlist and its evidence
- **#331**: docs: index DR-0006 in spec/README.md and correct the README's passive-corner coverage statement
- **#338**: layout/pilot: negative-control gate's layout_sha256 half is vacuously true if klt stops emitting the field

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#353**: test: offline layout/pilot consistency check binding evidence to the schematic
- **#354**: fix: require layout_sha256 present before the pilot negative-control gate trusts it

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#345**: sim: drop six unread parameters and the unraised GateError from the drivers
- **#352**: docs: index DR-0006 and name the PSRR full-envelope study as supplementary evidence

## Proposed

Issues carrying `loom:curated`.

- **#98**: sim: CMRR mismatch Monte Carlo over the T/VDD grid (45 points x N=300) *(curated)*
- **#140**: cmrr-mc: full grid as a single batch request exceeds the 3600 s fleet job limit *(curated)*
- **#240**: Re-pin open_pdks to f6eeac7 via ciel, with a revision-checked PDK search, before minting layout evidence *(curated)*
- **#295**: Raise the unity-gain bandwidth target to >= 8 MHz for a type-III compensator load *(curated)*
- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(curated)*
- **#328**: layout: compose the first-stage sub-block (XM1 to XM5) from the pilot generators with klt DRC and LVS evidence *(curated)*
- **#330**: test: offline consistency check tying layout/pilot to the schematic netlist and its evidence *(curated)*
- **#331**: docs: index DR-0006 in spec/README.md and correct the README's passive-corner coverage statement *(curated)*
- **#337**: Stop four benches importing sim/cmrr/run_cmrr.py as a library: hoist its bench-agnostic half into a shared module *(curated)*
- **#338**: layout/pilot: negative-control gate's layout_sha256 half is vacuously true if klt stops emitting the field *(curated)*
- **#350**: sim/test_harness.py: pin the add_batch_args dest contract that batch_block()/submit_request() read *(curated)*
- **#351**: Scope NOT_FINGERPRINTED by (experiment, constant) so a shared constant name cannot silently exempt a second module *(curated)*

## Proposed (Architect / Hermit)

- **#321**: Stop committing uncompressed point data: adopt the proven .dat.gz convention for new sim records *(hermit)*
- **#322**: Scope the passive RZ x CC matrix per figure: slew-swing-power re-runs 660 provably identical SPICE cells per full-scope study *(hermit)*
- **#337**: Stop four benches importing sim/cmrr/run_cmrr.py as a library: hoist its bench-agnostic half into a shared module *(hermit)*
- **#340**: Consolidate 8 duplicate claim_record_paths append-only tests into sim/test_harness.py *(hermit)*

## Epics

_None._

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 1 |
| Important | 6 |
| Ready (`loom:issue`) | 1 |
| In Progress (`loom:building`) | 4 |
| PRs awaiting review | 2 |
| Approved PRs awaiting merge | 2 |
| Curated | 12 |
| Architect / Hermit proposals | 4 |
| Active epics | 0 |
<!-- guide:plan-body:end -->
