# sim/

ngspice testbenches and append-only results.

## One-command driver

- `./characterize.sh` — regenerates every testbench's full PVT-cornered
  evidence (mints new append-only records). Multi-corner grids go through
  `klt sim`, so they run on whichever backend `klt` selects (the Spot batch
  fleet on a dispatch worker via `KLT_SIM_BACKEND=batch`); never hand-loop
  `ngspice -b` over a grid. The offset-mismatch step is pinned to the full
  grid (`--grid full`, issue #167): 45 points × N = 300 = 13 500 units —
  9× the standalone nominal run's 1500 — sharded over 3 fleet jobs.
- `./selftest.sh` — fast check (no records written): the extraction and
  source-guard unit tests plus a single local nominal point.

Mirrors `gf180-comparator`'s `characterize.sh`/`selftest.sh` split. See
`gain-gbw-pm/README.md` for what each currently drives.

## CI selftest

`.github/workflows/selftest.yml` runs `./selftest.sh` on every push to `main`
and every PR: ngspice (apt, `ubuntu-24.04` = ngspice 42), the selftest klt and
the pinned gf180mcu PDK are provisioned, `ci_prereqs.py` fails naming any
missing tool (`klt`, `ngspice`, `xschem`), an xschem other than the pinned
release, or the PDK with every path searched, and
`SIM_REQUIRE_PREREQS=1` turns the suites' skip-when-unavailable paths (PDK /
klt / ngspice / committed gain dataset) into failures; local runs without the
variable still skip. `ci_regression_check.sh` then breaks a copy of a testbench source guard
and an extraction in each simulator-free experiment (including
`ibias-cl-sensitivity`) and in the shared `harness.py` and `passive_corners.py`
modules (the latter via the slew-swing-power suite, as it has no suite of its
own) on purpose, and requires the relevant unit suite to fail. The selftest also runs `report/spec_citation_check.py` (issue #112,
no simulator): every record cited in the Status column of
`spec/target-spec.md` §2 must exist, and must be the record
`report/selection.json` selects for that row's experiment unless its clause
labels it `superseded`/`historical` or the `passive-corner` study (records of
another experiment, such as the CMRR row's `cmrr-mc` record, are checked for
existence only); `ci_regression_check.sh` proves it can fail. The job fails if it leaves any file modified or created (no
`records/`). It never runs a PVT/Monte Carlo grid and needs no credentials.

The selftest also runs `ci_netlist_check.py` (issue #76): it exports
`design/opamp_two_stage.sch` with xschem into a scratch temp dir and compares
it with the committed `design/netlist/opamp_two_stage.spice`, ignoring only the
checkout-specific `** sch_path:` comment. Missing xschem, a failed or empty
export, a missing symbol or an empty subcircuit fail it. Reproduce locally
with `python3 sim/ci_netlist_check.py` (tests: `python3 sim/test_netlist_check.py`);
a mismatch means regenerate the netlist per `design/README.md`.

It also runs `python3 ../design/check_dc_op.py` (issue #96), the schematic
bias/convergence smoke check (one nominal local ngspice point; the selftest
fails if it exits non-zero). It writes nothing unless `--deck` is passed, which
the selftest does not do, so the checkout stays clean.

**xschem pin: 3.4.7**, built from upstream commit
`92dd8fe5f4d5c1057489710d8a22f18fdc9d7ed0` (what tag `3.4.7` points at),
not from apt (ubuntu-24.04 ships 3.4.4, which formats the export differently).
Three places hold it and must agree: `selftest.yml` `SELFTEST_XSCHEM_VERSION`
(plus `XSCHEM_COMMIT` for the source build, cached on that commit), and
`ci_netlist_check.py` `PINNED_XSCHEM_VERSION`. `ci_prereqs.py` fails if the
workflow variable disagrees with the constant or if `xschem --version` on
PATH is not `XSCHEM V3.4.7`; `ci_netlist_check.py` also stops with exit 2 on
a version mismatch instead of reporting a diff. `test_netlist_check.py`
checks the workflow pin statically and the version gate with fake xschem
binaries. To get the pinned xschem locally (build deps as in the workflow;
installs to `/usr/local` unless you pass `--prefix`):

```bash
git init xschem-src && cd xschem-src
git remote add origin https://github.com/StefanSchippers/xschem.git
git fetch --depth 1 origin 92dd8fe5f4d5c1057489710d8a22f18fdc9d7ed0
git checkout FETCH_HEAD
./configure && make -j2 && sudo make install     # or ./configure --prefix=$HOME/xschem-3.4.7
xschem --no_x -q --version | head -1             # must print XSCHEM V3.4.7
```

**Two distinct klt pins** (do not conflate):

| Where | Pin | Role |
|---|---|---|
| `selftest.yml` `SELFTEST_KLT_VERSION` | `klayout-tools==0.7.0` | client that runs the smoke simulations |
| `signoff.yml` | `klayout-tools==0.7.0` | grader whose output must reproduce `manifests/gf180-opamp.signoff.json` |

Bump each independently; the signoff pin moves only with a regenerated record
(`manifests/README.md`). The PDK pin is `GF180_PDK_REV`, which must equal
`harness.py`'s `PINNED_PDK_REV`. The `~/.volare` cache is keyed on that
revision, but the key is not trusted as proof of the contents:
`ci_prereqs.py` reads the selected PDK's `SOURCES` file and fails, with
expected/actual hashes, unless its `open_pdks` revision is exactly the
pinned full hash (a missing `SOURCES` or one without an `open_pdks` line is
unknown provenance and also fails). `ci_pdk_rev_check.sh` proves that gate
on scratch fixtures in the volare layout (pinned passes; wrong revision,
pinned-named dir with wrong contents, and unknown provenance fail) without
needing the real PDK. volare itself is pinned (`VOLARE_VERSION`).

Reproduce locally (needs `klt`, `ngspice`, xschem 3.4.7 (above), numpy/matplotlib, and the PDK):

```bash
volare enable --pdk gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b
python3 sim/ci_prereqs.py
sim/ci_pdk_rev_check.sh
SIM_REQUIRE_PREREQS=1 sim/selftest.sh
sim/ci_regression_check.sh
git status --porcelain   # must be empty
```

## Shared harness module

`harness.py` is the one master copy of the cross-experiment sim-harness
helpers — gf180mcu PDK discovery (`find_pdk`), ngspice version provenance,
append-only record-id allocation, and the per-corner ngspice deck executor.
Since issue #58 it also holds the `klt sim` wrapper every experiment driver
uses — `KltError`, `run_klt` (optional `env`), `run_klt_retrying` (re-submits
only a capacity-refused batch submit; never changes backend), `remote_of`,
`klt_version`, `batch_block`, `sanitise_report` — plus `claim_record_paths`
(append-only record paths; `plots=False` for an experiment without plots)
and `load_dut_text` (the committed DUT export, wrapper-normalised).
Every runner (`design/check_dc_op.py`, every `sim/<experiment>/run_*.py`)
imports it; deck composition, testbench guards, extraction and plotting stay
per-experiment.

This **reverses the earlier copy-not-import convention** (each runner
carrying its own copy of these helpers) per issue #30 and its operator
ruling of 2026-10-02: the copies had drifted — `find_pdk`'s failure
guidance lost the pinned PDK revision in two of three copies — and the
per-bench copying would have multiplied that drift across every future
classic-row testbench. The canonical variants kept in `harness.py` resolve
both recorded drifts. Per REUSE.md (rule 9) no fleet-level harness master
exists to take by pinned reference (`gf180-bandgap/sim/harness` is
single-consumer in-tree; no `reuse.lock.json` pins it), so this repo's
module is the master — and a fact-check for stamped copies in the twin
repos found none to stamp: `sky130-opamp` already extracted its own
`sim/lib/spice_harness.py`, `sg13g2-opamp` has no Python PDK-discovery
layer, and the PDK-discovery core is gf180mcu-specific (per-PDK harness
material is per-repo by design under REUSE.md's two-PDKs rule).

## Solver tolerance convention

Every deck under `sim/` runs at ngspice's default solver tolerance — no
`reltol`, `abstol`, or `vntol` override line in any `.spice` file or
`.spiceinit`. See
[`spec/decision-records/0004-ngspice-reltol-policy.md`](../spec/decision-records/0004-ngspice-reltol-policy.md)
(mirrors `sg13g2-opamp`'s DR-0005) for the rationale and the screen any
future tightening proposal must clear before a deck may add one.

## Characterization report

`report/characterization_report.py` aggregates the committed records into one
report per spec row (`reports/characterization-report.md` and `.json`). It
reads only committed Markdown records and `spec/target-spec.md`; it never runs
a simulator or touches the network, and its output is byte-identical for
identical inputs (sorted keys, no timestamps/hostnames/absolute paths).

- Selection is explicit: `report/selection.json` lists one record per
  experiment. `slew-swing-power` may instead name a record per row
  (`{"power": ..., "slew": ..., "swing": ...}`), because a record of it can
  judge a subset of its three rows (`--figures`); a record selected for a row
  it did not judge is an error. A record that a sibling record `**Supersedes**` is rejected
  (opt in knowingly with `allow_superseded`); records measuring different DUT
  versions (compared on the 16-hex normalised-netlist prefix) are rejected
  with exit 2.
- Every row of `spec/target-spec.md` Sec. 2 appears, plus a post-layout line.
  Status is `measured-verdict` (ratified bound + a record that judged it),
  `measured-no-bound` (worst value shown, bound open, never a verdict),
  `not-measured` (no committed record) or `proposed-not-graded` (the row's
  bound is tagged in-row "proposed, not ratified" by a decision record, e.g.
  DR-5's input common-mode range; no verdict, counted neither as judged nor as
  not measured, the spec's status text is reproduced verbatim). Coverage, limitations, per-source
  sha256, DUT hash, PDK revision and tool versions are listed.
- Existing records have no structured sidecars, so the verdict/worst-case
  lines are extracted from the Markdown (cross-checked against each record's
  own per-point table); historical records are never modified.
- Input common-mode range (issue #90): the selected `input-common-mode`
  record is attached to DR-5's `proposed-not-graded` row as information only
  (no verdict, no worst value beside the proposed bound, still outside the
  ratified counts). The report re-derives the conservative common interval
  (every disjoint component kept apart, never bridged), its edge-binding
  corners and brackets, the transition resolution, the explicit 1.20 V sample
  at every point and the smallest saturation margins from the record's
  per-point tables, then cross-checks them against the retained
  `input-common-mode/corners/<rid>/samples.csv` (its sha256 is reported). A
  disagreement, a malformed or overlapping interval, or a missing
  `samples.csv` is an error. Without a selected ICMR record the row stays
  explicitly missing; nothing is read from the spec status text. ICMR records
  carry no measurement fingerprint, so their configuration freshness is
  reported as unknown. If DR-5 is ratified (the in-row tag removed) while an
  ICMR record is selected, generation fails until the report learns to grade
  the row.
- Measurement-configuration freshness (issue #85), additive to the DUT gate:
  a record may carry a versioned `**Measurement fingerprint**` header line
  plus the canonical inputs it hashes (`## Measurement fingerprint inputs`).
  The report verifies the inputs hash to the stated value, recomputes the
  CURRENT effective inputs offline (`sim/<experiment>/measurement_config.py`
  plus the committed bench) and rejects a mismatch like a stale DUT, naming
  the experiment to rerun and the input groups that changed (`--archival`
  downgrades it to a disclosed limitation). Normalisation (`harness.py`,
  `canonical_bench_lines`): comments, blank lines, whitespace, case and
  `.include` paths are ignored; values are not numerically re-parsed. Excluded:
  record IDs, workspace paths, backend/retry options, extraction code. Instrumented
  experiments (issues #85, #89): `gain-gbw-pm`, `noise`, `offset-mc`, `cmrr`,
  `psrr` and `slew-swing-power`, each with a stdlib
  `sim/<experiment>/measurement_config.py` that its runner imports its
  fingerprinted constants from (one source; per-experiment tests compare the
  runner's klt request with the fingerprint inputs, and
  `sim/test_measurement_config.py` fails when a configuration constant does
  not move the fingerprint). Beyond the bench and corner/model axes each
  fingerprints what is specific to its figure: noise bands, spot frequencies
  and fit window; offset-MC sample count, seed, `vary` mode and the
  `sw_stat_mismatch` switch; CMRR/PSRR excitation modes, servo and isolation
  settings and the operating-point print; slew/swing/power timing, sweep and
  criterion settings **per figure** (the record retains only the figures it
  measured, so a power-only record never certifies slew or swing
  configuration, and the report recomputes exactly that figure set). Records
  written before the migration, and every older record, are reported as
  "measurement-configuration freshness unknown" (per source, per row and in
  the report limitations), never as current. To migrate a further experiment,
  give it a `measurement_config.py` exposing `EXPERIMENT`,
  `FINGERPRINT_VERSION`, `TESTBENCHES_REL` and `inputs(texts, retained)` (see
  `noise/measurement_config.py`), emit the header line and inputs block from
  its driver (`harness.fingerprint_header_lines` /
  `fingerprint_inputs_section`), and register it in `MEASUREMENT_CONFIG`.
  Historical
  records are never edited and no historical fingerprint is fabricated.
- Supplementary passive-corner studies (issue #121): the RZ x CC side
  studies (`run_gain_gbw_pm.py --passive-corners`,
  `run_slew_swing_power.py --passive-corners`) are selected explicitly under
  `supplementary` in `selection.json`
  (`gain-gbw-pm-passive-corners`, `slew-swing-power-passive-corners`), never
  under `experiments`. The report hashes each selected study, checks it is a
  study record of its experiment (title), not superseded, of the same DUT as
  the selected records and the current netlist, and judged against the
  current ratified bounds; it parses every RZ x CC table (a missing,
  duplicate or malformed cell, or a per-cell PASS/FAIL that contradicts its
  value beyond display precision, is an error) and re-derives each figure's
  pass count, worst cell and range, which must agree with the study's verdict
  header. The derived observations appear in a separate "Supplementary
  passive-corner studies" section and as row-detail notes of the rows they
  inform; they never change a row's verdict, points or the ratified-row
  summary, and the stated scope is part of every observation: the limited
  keys (3 MOS/T/VDD points x 3 RZ x 3 CC = 27 cells) are not a full
  passive-by-PVT cross product, while the `-full` keys (issue #150,
  `gain-gbw-pm-passive-corners-full`, `slew-swing-power-passive-corners-full`;
  `--passive-scope full`) are the 5 x 3 x 3 x 9 = 405-cell envelope. A record
  selected under a key of the other scope is an error. Measurement-configuration
  freshness follows the same policy as selected records (stale: error unless
  `--archival`; unknown: disclosed).
- Bound-free PSRR passive study (issue #173): the full 405-cell PSRR RZ x CC
  study (`run_psrr.py --passive-corners --passive-scope full`, issue #155) is
  selected explicitly under `psrr-passive-corners-full` and attached to the
  PSRR row as measured information with NO verdict: the row's DR-6 bound is
  proposed, not ratified, so the adapter reads no bound (if the row is ever
  ratified while the study is selected, generation fails until the report
  learns to grade it). The population comes from the record's hash-verified
  fingerprint inputs (`passive_study`, so the passive section map and factors
  follow the freshness policy); the per-cell table must hold exactly those
  405 cells (missing, duplicate, unexpected, malformed or nonfinite cells are
  errors) and `corners/<rid>/partitions.json` must cover them exactly per
  excitation. PSRR+ / PSRR- at 1 kHz and 10 kHz are re-derived from the
  retained `<cell>.psrr.dat` curves (aggregate sha256 reported), checked
  against the rounded table, the binding-cell and delta tables (a misnamed or
  wrong extremum is an error; ties at retained precision are accepted) and
  the selected 45-point PSRR record's typical-passive worst values. The
  report shows worst / best cells, the worst over the typical-passive cells,
  the passive shift of the worst case and the deltas versus typical passives,
  plus the study's own limitations (matched devices, bias-generator supply
  sensitivity excluded, no passive mismatch or layout parasitics,
  client-versus-fleet provenance); the evidence sidecar lists it under
  `report_supplementary` as ungraded. The primary PSRR selection and the
  graded counts are unchanged.
- Regenerate: `python3 sim/report/characterization_report.py` (after editing
  `selection.json`); verify the committed copy: `... --check` (exit 1 if
  stale). `characterize.sh` ends with `--latest --update-manifest`, selecting
  the newest record of each experiment (for `slew-swing-power`, the newest
  record that judged each row; side-study records such as the gain-gbw-pm, slew-swing-power and PSRR
  `--passive-corners` studies are never selected by recency, and the manifest's
  explicit `supplementary` selection is carried over unchanged); `selftest.sh` runs
  `report/test_report.py` and `--check`.

## Experiments

- [`gm-id-characterization/`](gm-id-characterization/README.md) — gf180mcu
  3.3 V MOS (`nfet_03v3`, `pfet_03v3`) gm/ID, gm/gds and fT vs overdrive,
  across the `typical/ff/ss/fs/sf` process corners at −40/27/125 °C. The
  first characterization study for this block (`CLAUDE.md`'s "gm/ID first"
  ordering, issue #10) — fed the topology/sizing decisions
  ([DR-0001](../spec/decision-records/0001-topology-and-cl.md),
  [`design/opamp_sizing.md`](../design/opamp_sizing.md)) and
  `spec/target-spec.md` §1's corner-grid row. (Predates
  `characterize.sh`/`selftest.sh`; still run directly per its own README.)
- [`gain-gbw-pm/`](gain-gbw-pm/README.md) — open-loop DC gain, GBW and phase
  margin of the **committed sized schematic**
  (`design/netlist/opamp_two_stage.spice`, instantiated as
  `opamp_two_stage`; the testbench declares no transistor) across the full
  ratified grid — `typical/ff/ss/fs/sf` × −40/27/125 °C × 2.97/3.30/3.63 V,
  45 points expressed as one `klt sim` request — with a per-row verdict and
  binding corner against the ratified bounds in `spec/target-spec.md` §2, a
  feedback-isolation study and deterministic negative controls. The repo's
  first circuit-level spec-row evidence (issues #19, #38; tracker #7
  items 5 and 9). Selected record
  [`20261010-161514-0127eb7`](gain-gbw-pm/records/20261010-161514-0127eb7.md)
  (measured on the #42 compensation retune, RZ 11.23 kOhm / CC 0.578 pF):
  gain, GBW and phase margin pass at 45/45 points (worst 93.79 dB, 11.910 MHz,
  71.99 degrees). The pre-retune design missed PM at 30/45 points (worst 57.34
  degrees; records `20261009-055759-2524b3e`, `20261010-020141-1e51d1c`,
  retained unchanged).
  Opt-in `--vcm-fixed 1.20` re-runs the 45 points with the input common mode
  held at the LDO consumer's 1.20 V instead of VDD/2 (issue #125; diagnostic
  record under `gain-gbw-pm/fixed-vcm/`, default records untouched; evidence for
  #42, no consumer contract).
- [`offset-mc/`](offset-mc/README.md) — mismatch Monte Carlo of the
  **input offset** of the committed sized schematic (unity follower, DC):
  `typical/ff/ss/fs/sf` × N = 300 at 27 °C / 3.30 V, one `klt sim`
  `monte_carlo` request (1500 units on the batch fleet; the standalone
  default — `characterize.sh` pins `--grid full` for the 45-point × 300 =
  13 500-unit grid, issue #106/#167), per-corner mean,
  sigma, 3 sigma and worst corner, with a switch-off control, an imbalance
  control and the PDK mismatch-model audit. Statistical basis for the offset
  row; proposes no bound (issue #45; DR-3 residual (e2)).
- [`noise/`](noise/README.md) — **input-referred noise** of the committed sized
  schematic (closed-DC-loop / open-AC-loop, as the gain bench) across the full
  45-point grid as one `klt sim` `.noise` request: spot densities
  (10 Hz–100 kHz), integrated rms over 100 Hz–1 MHz and three alternative
  bands, thermal floor and 1/f corner, the PDK flicker-model audit and
  extraction validation against ngspice's totals and the gain bench. Measured
  only: no verdict, no bound or band proposed (issue #46; DR-3 residual (e1)).
- [`cmrr-mc/`](cmrr-mc/README.md) — **mismatch-aware CMRR** of the committed
  sized schematic: `typical/ff/ss/fs/sf` × N = 300 (the `offset-mc/` population
  and seed) at 27 °C / 3.30 V, one `klt sim` `monte_carlo` request with an AC
  analysis carrying both excitations of each sample in ONE deck, per-corner
  mean / sigma / mean−3σ / min / p5 of CMRR at DC, 1 kHz–1 MHz and f_u, with
  switch-off, process-only and mirror-imbalance controls. Statistic only: the
  record itself proposes no bound; decision record 0006 does (DR-6, proposed,
  not ratified) (issue #61; DR-3 residual (e3)).
- [`cmrr/`](cmrr/README.md) — **CMRR** of the committed sized schematic across
  the full 45-point grid: differential and common-mode excitations (two
  `klt sim` requests) on a DC-servo bench that admits equal AC drive on both
  inputs, Ad and Acm solved jointly from the actual input phasors, CMRR at the
  verified 0.1–1 Hz plateau, 1 kHz–1 MHz and at the differential unity-gain
  frequency, worst-case corner per figure, isolation/numerical-floor studies
  and a load-mirror-imbalance negative control. Systematic-only (matched
  devices); measured, no verdict (issue #39; DR-3 residual (e3)).
- [`psrr/`](psrr/README.md) — **PSRR+ and PSRR−** (input-referred) of the
  committed sized schematic across the full 45-point grid: differential, `vdd`
  and `vss` excitations (three `klt sim` requests) with the DUT's `vss` port
  driven, the same summaries and worst-case corners, output feedthrough
  reported separately, rail-to-output feedthrough negative controls. Measured,
  no verdict (issue #39; DR-3 residual (e4)).
- [`psrr-mc/`](psrr-mc/README.md) — **mismatch-aware PSRR+ and PSRR−** of the
  committed sized schematic: `typical/ff/ss/fs/sf` × N = 300 (the `offset-mc/`
  and `cmrr-mc/` population and seed) at 27 °C / 3.30 V, one `klt sim`
  `monte_carlo` request with an AC analysis carrying the differential, `vdd`
  and `vss` excitations of each sample in ONE deck (per-sample dm/vdd/vss
  parity checked), per-corner mean / sigma / linear 3σ / min of PSRR+ and
  PSRR− at DC, 1 kHz–1 MHz and f_u beside the DR-6 proposed bounds, with
  switch-off, process-only and mirror-imbalance controls. Reported, not graded
  (issue #99; follow-up of #61 / DR-6). It carries its own
  `measurement_config.py` fingerprint (#89 convention); it is not selected by
  the characterization report. No full record is committed yet (only the N=3
  fleet probe `20261010-234232-efa5c81`), so PSRR remains systematic-only.
- [`slew-swing-power/`](slew-swing-power/README.md) — slew rate (unity-gain
  follower, CL = 2 pF, slower of rise/fall, 20–80 % slope), output swing
  (inverting unity gain; first of −3 dB gain collapse or M6/M7 leaving
  saturation, per DR-2 §(b)) and quiescent power (total supply power, no load)
  of the **committed sized schematic** across the same 45-point grid, each as
  one `klt sim` request, with a verdict and binding corner per figure against
  the ratified bounds (≥ 10 V/µs, ≥ 2.3 Vpp, ≤ 350 µW) (issue #44; tracker #7
  item 5).
- [`input-common-mode/`](input-common-mode/README.md) — **follower-biased input
  common-mode range** of the committed sized schematic: a VCM scan 0..VDD
  (≤ 50 mV, refined to 5 mV at transitions) inside each of the 45 PVT points on
  the CMRR bench's DC-servo, Ad/Acm solved from the actual input phasors,
  per-device saturation margins from the retained operating-point log, every
  contiguous passing interval per point, the 45-point intersection and the
  explicit 1.20 V sample. Two `klt sim` requests per scan/refinement round
  (paired `vdd`/`vcm` axes on the batch fleet); measured evidence for the ICMR
  row proposed by decision record 0005 (issue #60).
- [`ibias-cl-sensitivity/`](ibias-cl-sensitivity/README.md) — **sensitivity
  data** (no verdict, no spec change) of GBW, PM, slew and quiescent power to
  the external bias current (8–12 µA around 10 µA) and the load capacitance
  (1, 2, 4, 10 pF) at the nominal and PM-/GBW-/slew-/power-binding corner
  points, one small `klt sim` request per sweep value, reusing the
  `gain-gbw-pm` and `slew-swing-power` benches; the 10 µA / 2 pF points are
  controls against the committed records (issue #114; evidence for #42).
- [`step-response/`](step-response/README.md) — **closed-loop follower step
  response** of the committed sized schematic: unity-gain follower, CL = 2 pF,
  a 100 mV step about VCM (rising and falling edge), overshoot, 1 % and 0.1 %
  settling time and monotonicity across the same 45-point grid as one
  `klt sim` request, worst points identified, alongside the gain record's PM.
  Evidence only: no spec row exists for settling/overshoot and none is
  proposed (that would need a `spec/` decision record) (issue #113; context
  #42).

## Coverage and gaps

As of the records selected in `report/selection.json`, benches exist for gain,
GBW, phase margin, slew rate, output swing, quiescent power, noise, offset
(mismatch Monte Carlo), CMRR (systematic, plus a mismatch Monte Carlo) and
PSRR. Slew rate (worst 15.50 V/us), output swing (worst 2.465 Vpp) and
quiescent power (worst 310.98 uW) pass their ratified bounds at 45/45 points
in the selected record
[`slew-swing-power/records/20261010-161852-0127eb7.md`](slew-swing-power/records/20261010-161852-0127eb7.md)
(after the #42 retune; historical pre-retune record
`20261009-142137-1dab1db`: slew worst 14.51 V/us, same swing and power
worst values); the slew and power binding corners
(ss / 125 C / 2.97 V and ff / -40 C / 3.63 V) differ in temperature from the
spec's predicted SS / -40 C / low VDD and FF / 125 C / 3.63 V (see
`slew-swing-power/README.md`). Common
limitations: each selected record is at typical passives (RZ/CC); gain/GBW/PM and
slew/swing/power have separate 27-cell passive-corner studies at three PVT
points and 405-cell full-envelope studies (issue #150: all PASS) as well
([`gain-gbw-pm/records/20261010-161720-0127eb7.md`](gain-gbw-pm/records/20261010-161720-0127eb7.md):
PM 27/27 pass, worst 64.47 deg; GBW 27/27 pass, worst 10.726 MHz;
[`slew-swing-power/records/20261010-162823-0127eb7.md`](slew-swing-power/records/20261010-162823-0127eb7.md):
27/27 per row, slew worst 14.25 V/us); offset covers the full 45-point
T/VDD grid x 300 samples per point
([`offset-mc/records/20261010-185141-e4be1ac.md`](offset-mc/records/20261010-185141-e4be1ac.md):
worst |mean| + 3 sigma 15.458 mV at typical / 27 C / 3.30 V; the earlier
five-corner x 300-sample record at 27 C / 3.30 V only, `20261009-072205-96bf3cc`,
worst 15.635 mV at sf, is kept as a historical record); the CMRR mismatch
Monte Carlo
([`cmrr-mc/records/20261010-202454-e4be1ac.md`](cmrr-mc/records/20261010-202454-e4be1ac.md):
worst linear 3-sigma DC CMRR 82.71 dB at typical / 27 C / 3.30 V) samples
five process corners x 300 at 27 C / 3.30 V only (no temperature or supply
axis), so CMRR across temperature and supply is systematic-only (matched
devices), and PSRR is systematic-only at every point. Noise, offset, CMRR and
PSRR are measured without a verdict because their numeric bounds are not
ratified. There is no layout or post-layout evidence. The 5 V stretch row is
not opened. These records are schematic-level evidence below the T1 tier.
