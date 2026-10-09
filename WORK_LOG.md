# Work Log

Merged pull requests and closed issues recorded by Guide.

### 2026-10-09

- **PR #57**: feat(sim): slew-rate, output-swing and quiescent-power testbenches (#44)
- **PR #56**: ci: run sim/selftest.sh in CI with pinned tools/PDK and strict prerequisites
- **PR #55**: docs: reconcile current-status prose with committed characterization evidence
- **PR #54**: chore(ratification): install ee-key and market-key reviewer trees (#41)
- **PR #53**: feat(sim): aggregate T1 characterization report from committed records
- **PR #49**: feat(sim): CMRR and PSRR testbenches and 45-point records (#39)
- **PR #48**: feat(sim): input-referred noise bench and 45-point evidence (#46)
- **PR #47**: feat(sim): input-offset mismatch Monte Carlo of the committed schematic (#45)
- **PR #43**: feat(sim): gain/GBW/PM of the committed schematic over the 45-point PVT grid (#38)
- **Issue #44** (closed): T1 item 5: add slew-rate, output-swing and quiescent-power testbenches for the three ratified-but-unmeasured rows
- **Issue #52** (closed): Run simulation extraction tests and nominal smoke checks in CI
- **Issue #51** (closed): Reconcile current-status documentation with committed characterization evidence
- **Issue #41** (closed): Install ratification/ee-key and ratification/market-key reviewer trees (product#151)
- **Issue #50** (closed): T1 item 8: generate a consolidated characterization report from selected evidence
- **Issue #39** (closed): T1 items 5 and 9: add CMRR and PSRR testbenches on the committed schematic so the two open spec rows can be given bounds
- **Issue #46** (closed): T1 item 5: add input-referred noise experiment to discharge residual (e1)
- **Issue #45** (closed): T1 item 5: add mismatch Monte Carlo input-offset experiment to discharge residual (e2)
- **Issue #38** (closed): T1 item 5: run gain, GBW and phase margin on the committed sized schematic across the full PVT grid

### 2026-10-03

- **PR #36**: refactor: consolidate sim-harness helpers into sim/harness.py
- **Issue #30** (closed): Remove triplicated sim-harness helpers: consolidate Pdk discovery + record bookkeeping into one shared module

### 2026-09-28

- **PR #35**: docs(spec): record ngspice solver-tolerance convention (DR-0004)
- **PR #33**: ci: pin actions/checkout to a commit digest in signoff.yml
- **Issue #34** (closed): ngspice solver-tolerance convention: keep the default (coordination from sg13g2-opamp DR-0005)
- **Issue #32** (closed): Pin actions/checkout to a digest so PR #28's Renovate quarantine actually binds

### 2026-09-23

- **Issue #16** (closed): Champion: Merge-Risk Hold Digest

### 2026-09-22

- **PR #29**: feat: add integrator view and spec consumers section for fleet reuse
- **Issue #27** (closed): 2am: reuse rule 9 — name this block's consumers in the spec, carry their requirement rows, publish the integrator view as data

### 2026-09-21

- **PR #26**: spec: ratify target-spec.md — partial ratification via DR-3
- **PR #25**: feat: add a klt signoff block manifest as the graded T1 verdict of record
- **Issue #24** (closed): spec: ratify target-spec.md — it is DRAFT, which blocks T1 item 5 (and items 6/7/8 that grade against its rows)
- **Issue #23** (closed): Commit a klt signoff block manifest so this block's T1 state is graded, not hand-read

### 2026-09-15

- **PR #22**: Add sim/gain-gbw-pm/: first circuit-level spec-row testbench with a one-command driver
- **PR #21**: feat: add gm/ID-sized two-stage op-amp schematic and DC-OP check
- **PR #20**: spec: propose bounds for target-spec.md [TBD] performance rows (DR-2)
- **Issue #19** (closed): First spec-row testbench under sim/ with one-command driver
- **Issue #18** (closed): Ratify spec/target-spec.md via decision record: propose bounds for [TBD] performance rows
- **Issue #17** (closed): Schematic entry: two-stage op-amp topology (DR-0001) in design/

### 2026-09-09

- **PR #15**: refactor: remove dead code in run_gmid.py
- **PR #13**: spec: topology + CL decision record (0001) for the two-stage Miller op-amp
- **PR #11**: feat: add gm/ID characterization sweep for gf180mcu 3.3V MOS
- **Issue #14** (closed): Remove dead code in run_gmid.py: unused NOMINAL_VDD constant and unused build_plots record param
- **Issue #12** (closed): spec: topology + CL decision record (0001) for the two-stage Miller op-amp, cited against the gm/ID study (#10) — the gf180 twin of sg13g2-opamp#6
- **Issue #10** (closed): sim: gm/ID characterization sweep of gf180mcu 3.3 V MOS — the first engineering task (porting-plan §4, gm/ID-first)
