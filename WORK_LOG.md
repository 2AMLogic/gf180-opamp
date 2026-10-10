# Work Log

Merged pull requests and closed issues recorded by Guide.

### 2026-10-10

- **PR #116**: sim: closed-loop follower step response (overshoot, settling) across the PVT grid (#113)
- **PR #115**: feat(#112): check target-spec.md Sec. 2 record citations against selection.json
- **PR #111**: feat: attach cross-checked ICMR evidence to the ungraded proposed row
- **PR #110**: docs(#95): cite passive-corner study in README and aggregate report
- **PR #109**: feat(#97): --passive-corners RZ x CC side study for slew/swing/power
- **PR #108**: ci(#107): extend ci_regression_check.sh teeth coverage to every sim experiment
- **PR #104**: refactor(#94): consolidate materialise() work-dir staging into harness.stage_workdir
- **PR #103**: selftest: gate design/check_dc_op.py in CI (#96)
- **PR #100**: spec: propose CMRR and PSRR bounds with a mismatch-aware CMRR statistic (DR-6; #61)
- **PR #92**: feat(#89): extend measurement freshness fingerprints to the remaining characterization experiments
- **Issue #113** (closed): sim: closed-loop follower step response (overshoot and settling) across the PVT grid
- **Issue #112** (closed): ci: check that target-spec.md measured-record citations match sim/report/selection.json
- **Issue #90** (closed): Include measured input common-mode evidence in the aggregate report without ratifying its bound
- **Issue #95** (closed): README and aggregate report: cite the passive-corner study instead of claiming passives were never swept
- **Issue #107** (closed): ci: extend ci_regression_check.sh mutation (teeth) coverage to every sim experiment
- **Issue #88** (closed): Auditor: review stash guard denial during isolated driver validation
- **Issue #94** (closed): Consolidate duplicated materialise() work-dir staging into sim/harness.py
- **Issue #96** (closed): selftest: run design/check_dc_op.py so the schematic bias smoke check is gated in CI
- **Issue #61** (closed): spec: ratify CMRR and PSRR bounds with a mismatch-aware CMRR statistic (DR-3 residuals e3, e4)
- **PR #91**: docs(#82): document absolute scratch cleanup paths in layout/README
- **PR #87**: feat(#85): track measurement-configuration freshness alongside DUT identity
- **PR #86**: feat(#84): validate integrator view against interface and signoff tier
- **PR #81**: Reject characterization evidence that no longer matches the current DUT
- **PR #80**: refactor(#68): consolidate sibling-driver loader and gain-bench reader
- **PR #78**: ci(#76): check committed netlist matches xschem export of the schematic
- **Issue #85** (closed): Track measurement-configuration freshness alongside DUT identity in characterization evidence
- **Issue #84** (closed): Validate the integrator view against the committed interface and signoff tier
- **Issue #83** (closed): Auditor Capability Request: Python unavailable for local report validation
- **Issue #82** (closed): layout: document absolute scratch cleanup paths for validation
- **Issue #76** (closed): Check that the committed simulation netlist still represents the xschem schematic
- **Issue #75** (closed): Reject characterization evidence that no longer matches the current DUT
- **Issue #68** (closed): Consolidate sibling-driver loader and gain-bench reader duplicated across sim runners

### 2026-10-09

- **PR #74**: sim(gain-gbw-pm): RZ x CC passive-corner axis and record (#70)
- **PR #73**: signoff: bump pinned klt grader to 0.7.0 and regenerate the signoff record
- **PR #72**: docs: reconcile slew/swing/power status with committed PASS record (#69)
- **PR #67**: sim+spec: follower-biased input common-mode range (45 PVT x VCM scan) and proposed ICMR row via DR-0005 (#60)
- **Issue #71** (closed): signoff: bump the pinned klt grader past 0.5.0 and regenerate the signoff record
- **Issue #70** (closed): T1 item 5: quantify RZ/CC passive-corner sensitivity of phase margin, GBW and slew (DR-3 section d obligation)
- **Issue #69** (closed): docs: reconcile README and target-spec status for slew, swing and power with committed PASS records
- **Issue #60** (closed): spec: add an input common-mode range row (testbench + decision record) — consumers currently grade it unknown

### 2026-10-09

- **PR #65**: refactor(sim): move duplicated klt sim wrapper helpers into harness.py
- **PR #64**: T1: cite the characterization report as hash-pinned item-8 evidence
- **PR #57**: feat(sim): slew-rate, output-swing and quiescent-power testbenches (#44)
- **PR #56**: ci: run sim/selftest.sh in CI with pinned tools/PDK and strict prerequisites
- **PR #55**: docs: reconcile current-status prose with committed characterization evidence
- **PR #54**: chore(ratification): install ee-key and market-key reviewer trees (#41)
- **PR #53**: feat(sim): aggregate T1 characterization report from committed records
- **PR #49**: feat(sim): CMRR and PSRR testbenches and 45-point records (#39)
- **PR #48**: feat(sim): input-referred noise bench and 45-point evidence (#46)
- **PR #47**: feat(sim): input-offset mismatch Monte Carlo of the committed schematic (#45)
- **PR #43**: feat(sim): gain/GBW/PM of the committed schematic over the 45-point PVT grid (#38)
- **Issue #58** (closed): Consolidate duplicated klt sim wrapper helpers into sim/harness.py
- **Issue #59** (closed): T1: cite the characterization report as hash-pinned item-8 evidence
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
