# Work Log

Merged pull requests and closed issues recorded by Guide.

### 2026-10-23

- **PR #276**: ci: raise selftest job timeout to 60 minutes
- **Issue #275** (closed): ci: selftest 'Prove the selftest can fail' step cancelled at ~30m on docs-only PR #273
- **PR #272**: feat(sim): store new run evidence in the artifact store, commit manifests
- **Issue #271** (closed): Adopt the common artifact store for sim evidence (manifests in git, bytes via klt artifact)

### 2026-10-20

- **PR #269**: refactor(sim): share the MC mismatch-switch guard in harness
- **Issue #226** (closed): Consolidate duplicated sw_stat_mismatch switch guard across four MC runners into sim/harness.py
- **PR #267**: docs(spec): add noise/offset consumer-derived requirement basis (DR-0007 draft)

### 2026-10-19

- **PR #264**: sim: staged-model f6eeac7 re-runs of gain-gbw-pm-mc and cmrr; stage client models inside run_klt_retrying

### 2026-10-18

- **PR #261**: refactor: share retained-CSV loading and CMRR/PSRR figure compare across report crosschecks
- **Issue #260** (closed): Hoist duplicated retained-CSV preamble from five report crosschecks into report_common
- **PR #259**: sim: cmrr-mc Monte Carlo re-run under f6eeac7 with client models staged
- **PR #257**: fix(sim): stage client model files on batch submits and correct the staging note
- **PR #256**: docs(sim): record c6d73a3 vs f6eeac7 model-file comparison and the staging blocker
- **PR #254**: sim: new-pin ICMR, step-response and passive-corner study records (f6eeac7)
- **PR #252**: sim: nominal offset Monte Carlo re-run under open_pdks f6eeac7
- **PR #251**: docs: Guide document maintenance update
- **PR #250**: sim: re-run five cited experiments under open_pdks f6eeac7

### 2026-10-17

- **PR #244**: refactor: hoist MC snapshot writer, request sanitiser and sha256 into harness
- **Issue #238** (closed): Hoist duplicated MC snapshot writer and request sanitiser into sim/harness.py
- **PR #245**: refactor: hoist per-bench fingerprint wrappers into harness.fingerprint_api
- **Issue #237** (closed): Collapse 14 copy-pasted fingerprint wrappers in sim/*/measurement_config.py into a harness factory
- **PR #243**: build: re-pin open_pdks to f6eeac7 via ciel, search ~/.ciel before ~/.volare
- **PR #236**: refactor(sim): hoist corner indexing, point persistence and re-derivation into harness
- **Issue #235** (closed): Hoist transient-runner corner indexing, point persistence and re-derivation loops into sim/harness
- **PR #234**: refactor(sim): hoist MC submit wrapper and scalar parser into harness
- **Issue #232** (closed): Hoist duplicated MC submit() and parse_scalars() into sim/harness
- **PR #230**: refactor(sim): hoist duplicated transient and MC runner helpers into harness
- **Issue #229** (closed): Consolidate remaining byte-identical runner helpers (step-response/overdrive-recovery, MC runners) into sim/harness.py
- **PR #228**: refactor(sim): hoist describe_backend and fmt_finite into harness, use remote_of
- **Issue #225** (closed): Consolidate triplicated describe_backend/_fmt and inline remote lookups into sim/harness.py
- **PR #224**: refactor(sim): remove unused helpers, imports and locals
- **Issue #223** (closed): Remove dead code in sim/: unused helpers, imports, locals
- **PR #220**: refactor(sim): share Ibias/CL testbench override helpers via harness
- **Issue #218** (closed): Consolidate duplicated Ibias/CL testbench override helpers into sim/harness.py
- **PR #221**: refactor: split characterization_report.py into sibling modules
- **Issue #219** (closed): Split sim/report/characterization_report.py (4,915 lines) into sibling modules
- **PR #216**: docs: add owner-mapped handoff for guard-confinement evidence

### 2026-10-16

- **PR #211**: sim(cmrr-mc): record seed re-probe on fleet klt 0.7.0 (still not partition-invariant)

### 2026-10-15

- **PR #207**: refactor(sim): share generic bench guards and logical_lines in harness
- **Issue #105** (closed): Consolidate duplicated guard_dut and guard_testbench core into sim/harness.py
- **PR #79**: feat(#77): GF180 layout device pilot (isolated cells, regen recipe, DRC/LVS evidence)
- **Issue #77** (closed): Prepare a reproducible GF180 layout device pilot before full op-amp placement

### 2026-10-14

- **PR #203**: Record enable-acquisition evidence at all 45 PVT points (#191)
- **Issue #191** (closed): sim: characterize supply and external-bias enable acquisition across PVT

### 2026-10-13

- **PR #200**: report: integrate ungraded follower step-response evidence
- **PR #199**: sim: configuration freshness for CMRR-MC and gain/GBW/PM-MC (#197)
- **Issue #198** (closed): report: integrate ungraded follower step-response evidence
- **Issue #197** (closed): sim: add configuration freshness for CMRR and gain/GBW/PM mismatch evidence

### 2026-10-12

- **PR #196**: sim(psrr-mc): full T/VDD grid mismatch characterization tooling (#190)

### 2026-10-11

- **Issue #189** (closed): Auditor guard telemetry: force-op detached prompt during evidence guard test authoring
- **PR #193**: sim: supply and external-bias enable acquisition across PVT (bench, extraction, tests; evidence pending)
- **PR #188**: sim: input-overdrive and output-saturation recovery across the PVT grid
- **PR #187**: report: derive offset temperature drift and systematic-vs-statistical split (#184)
- **PR #186**: sim: fingerprint the input-common-mode measurement configuration
- **PR #185**: report: include the committed PSRR mismatch Monte Carlo record in the characterization report
- **PR #181**: feat(#99): commit the full PSRR+/PSRR- mismatch Monte Carlo record
- **PR #178**: report: integrate the gain/GBW/PM mismatch Monte Carlo record as ungraded, hash-pinned evidence
- **PR #177**: ci: fail PRs that modify or delete committed sim/ evidence (#159)
- **PR #176**: sim: re-measure step response and ibias/CL sensitivity on the retuned DUT
- **PR #175**: report: integrate ungraded full-passive PSRR evidence (bound-free supplementary adapter)
- **PR #174**: fix: require exact 45 default stems in latest_gain_dir selection
- **PR #171**: sim/report: check live verdict prose against the characterization report
- **PR #170**: fix: reject gain verdict-token, binding-corner and stretch-count contradictions in extract_gain
- **PR #169**: fix(sim): pin offset driver to full PVT grid in one-command campaign
- **PR #165**: signoff: cite artifact-anchored envelopes for T1 items 1, 9, 10 (#160)
- **PR #164**: sim: standalone full RZ/CC x PVT PSRR study at 1 kHz and 10 kHz
- **PR #163**: docs: correct pre-retune verdict prose in spec/target-spec.md and manifests/README.md
- **PR #162**: refactor(sim): share MC percentile/skew/extract between cmrr-mc and psrr-mc
- **PR #158**: docs: guard confinement reproduction and evidence package (#146)
- **PR #157**: sim: gain/GBW/PM mismatch Monte Carlo campaign on the retuned DUT (#142)
- **PR #152**: sim: extend independent RZ/CC verification to the full 405-cell PVT envelope
- **PR #151**: test(sim/psrr-mc): offset-sigma gate test, IMBALANCE_RAILS pin, gate mutation (#99, partial)
- **PR #149**: sim(cmrr-mc): seed partition-invariance probe for #140 (seeds NOT invariant; chunked grid parked)
- **PR #148**: fix(report): preserve published evidence bundle when candidate validation fails
- **PR #145**: feat(sim/psrr-mc): PSRR+/PSRR- mismatch Monte Carlo driver, controls and tests (record pending)
- **Issue #184** (closed): report: derive offset temperature drift and systematic-vs-statistical split from the committed offset Monte Carlo
- **Issue #183** (closed): sim: input-overdrive and output-saturation recovery across the PVT grid
- **Issue #182** (closed): report: include the committed PSRR mismatch Monte Carlo record in the characterization report
- **Issue #180** (closed): sim: add measurement-configuration freshness for input common-mode evidence
- **Issue #179** (closed): Auditor: review force-op:detached guard decisions
- **Issue #173** (closed): report: integrate ungraded full-passive PSRR evidence
- **Issue #172** (closed): report: integrate descriptive gain/GBW/PM mismatch evidence
- **Issue #168** (closed): report: validate gain verdict tokens and binding corners against per-point evidence
- **Issue #167** (closed): characterize: preserve full-PVT offset coverage in the default campaign
- **Issue #161** (closed): sim: latest_gain_dir() now picks the 405-cell passive study dir, breaking the default CMRR/PSRR/noise gain-bench cross-check
- **Issue #160** (closed): signoff: determine which klt envelopes honestly back T1 items 1, 9 and 10, cite them or file the tool gap
- **Issue #159** (closed): ci: fail PRs that modify or delete committed sim/ evidence (enforce append-only beyond write time)
- **Issue #156** (closed): sim/report: extend the spec citation drift check to catch stale verdict prose
- **Issue #155** (closed): sim: standalone full RZ/CC x PVT PSRR study at 1 kHz and 10 kHz
- **Issue #154** (closed): docs: correct pre-retune verdict prose in spec/target-spec.md and manifests/README.md (PM no longer fails)
- **Issue #153** (closed): Consolidate duplicated MC stats helpers (percentile/_skew/extract) in cmrr-mc and psrr-mc
- **Issue #150** (closed): sim: extend independent RZ/CC verification to the full 405-cell PVT envelope
- **Issue #147** (closed): report: preserve the published evidence bundle when candidate validation fails
- **Issue #146** (closed): Auditor: capture reproducible scratch-path and checkpoint guard cases for upstream review
- **Issue #142** (closed): sim: run the gain/GBW/PM mismatch Monte Carlo campaign on the retuned DUT now that the #42 gate is open
- **Issue #139** (closed): Re-measure step-response and ibias/CL sensitivity on the retuned compensation DUT
- **Issue #99** (closed): sim: PSRR+ / PSRR- mismatch Monte Carlo

- **PR #143**: docs(#141): reconcile sim/README coverage-and-gaps with selected records
- **Issue #141** (closed): docs: reconcile sim/README.md coverage-and-gaps section with the 45-point offset record and post-retune results

### 2026-10-10

- **PR #138**: design(#42): retune Miller compensation so phase margin meets 60 deg at 45/45 PVT points
- **PR #137**: feat(#114): ibias and CL sensitivity record
- **PR #135**: sim: mismatch Monte Carlo runner for gain, GBW and phase margin (#129)
- **PR #134**: docs(#133): reconcile status prose with passive-corner side study
- **PR #132**: ci(#131): extend regression-check mutations to ibias-cl-sensitivity and shared sim modules
- **PR #130**: report: derive and hash supplementary passive-corner evidence (#121)
- **PR #128**: docs(#102): sweep main-clean baseline recipe and regression check
- **PR #126**: sim: gain/GBW/PM at the LDO 1.20 V input (fixed-VCM option + 45-point record)
- **PR #127**: feat(#124): report the committed CMRR mismatch Monte Carlo record
- **PR #123**: report: support the committed full-PVT offset Monte Carlo record (#120)
- **PR #122**: feat(#114): ibias and CL sensitivity experiment (record pending)
- **PR #119**: feat(#97): passive-corner side-study record for slew/swing/power
- **PR #118**: sim: offset Monte Carlo over the 45-point T/VDD grid (--grid full, N=300 per point)
- **Issue #42** (closed): Design: phase margin misses the ratified 60 deg target at 30/45 PVT points (worst 57.34 deg at FS / 125 C / 2.97 V)
- **Issue #114** (closed): sim: ibias and CL sensitivity of GBW, PM, slew and power (evidence for the PM shortfall)
- **Issue #129** (closed): sim: mismatch Monte Carlo of gain, GBW and phase margin (statistical evidence for the PM/GBW rows)
- **Issue #133** (closed): docs: reconcile README, sim/README and target-spec status with the committed slew/swing/power passive-corner study
- **Issue #131** (closed): ci: extend ci_regression_check.sh mutation coverage to ibias-cl-sensitivity and shared sim modules
- **Issue #121** (closed): report: derive and hash supplementary passive-corner evidence
- **Issue #102** (closed): Document and test a confined sweep main-clean baseline invocation
- **Issue #125** (closed): sim: characterize gain GBW and PM at the LDO 1.20 V input operating point
- **Issue #124** (closed): report: include separately scoped mismatch CMRR evidence
- **Issue #120** (closed): report: support the committed full-PVT offset Monte Carlo record
- **Issue #101** (closed): Auditor: retain rm-scope-outside-repo guard pending scoped lexical proof
- **Issue #97** (closed): sim: measure slew, swing and power across RZ/CC passive corners (replace the analytic slew scaling)
- **Issue #106** (closed): sim: extend offset Monte Carlo to the 45-point T/VDD grid (N=300 per point)
- **Issue #89** (closed): Extend measurement freshness checks to the remaining selected characterization experiments
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
