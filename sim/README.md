# sim/

ngspice testbenches and append-only results.

## One-command driver

- `./characterize.sh` — regenerates every testbench's full PVT-cornered
  evidence (mints new append-only records). Multi-corner grids go through
  `klt sim`, so they run on whichever backend `klt` selects (the Spot batch
  fleet on a dispatch worker via `KLT_SIM_BACKEND=batch`); never hand-loop
  `ngspice -b` over a grid.
- `./selftest.sh` — fast check (no records written): the extraction and
  source-guard unit tests plus a single local nominal point.

Mirrors `gf180-comparator`'s `characterize.sh`/`selftest.sh` split. See
`gain-gbw-pm/README.md` for what each currently drives.

## CI selftest

`.github/workflows/selftest.yml` runs `./selftest.sh` on every push to `main`
and every PR: ngspice (apt, `ubuntu-24.04` = ngspice 42), the selftest klt and
the pinned gf180mcu PDK are provisioned, `ci_prereqs.py` fails naming any
missing tool (`klt`, `ngspice`) or the PDK with every path searched, and
`SIM_REQUIRE_PREREQS=1` turns the suites' skip-when-unavailable paths (PDK /
klt / ngspice / committed gain dataset) into failures; local runs without the
variable still skip. `ci_regression_check.sh` then breaks a copy of the
gain-bench source guard and extraction on purpose and requires the unit suite
to fail. The job fails if it leaves any file modified or created (no
`records/`). It never runs a PVT/Monte Carlo grid and needs no credentials.

**Two distinct klt pins** (do not conflate):

| Where | Pin | Role |
|---|---|---|
| `selftest.yml` `SELFTEST_KLT_VERSION` | `klayout-tools==0.7.0` | client that runs the smoke simulations |
| `signoff.yml` | `klayout-tools==0.5.0` | grader whose output must reproduce `manifests/gf180-opamp.signoff.json` |

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

Reproduce locally (needs `klt`, `ngspice`, numpy/matplotlib, and the PDK):

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
- Regenerate: `python3 sim/report/characterization_report.py` (after editing
  `selection.json`); verify the committed copy: `... --check` (exit 1 if
  stale). `characterize.sh` ends with `--latest --update-manifest`, selecting
  the newest record of each experiment (for `slew-swing-power`, the newest
  record that judged each row); `selftest.sh` runs
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
  [`20261009-055759-2524b3e`](gain-gbw-pm/records/20261009-055759-2524b3e.md):
  gain and GBW pass at 45/45 points, phase margin fails its 60 degree target
  (15/45 pass, worst 57.34 degrees).
- [`offset-mc/`](offset-mc/README.md) — mismatch Monte Carlo of the
  **input offset** of the committed sized schematic (unity follower, DC):
  `typical/ff/ss/fs/sf` × N = 300 at 27 °C / 3.30 V, one `klt sim`
  `monte_carlo` request (1500 units on the batch fleet), per-corner mean,
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
  switch-off, process-only and mirror-imbalance controls. Statistic only: no
  bound proposed (issue #61; DR-3 residual (e3); bound in decision record 0006).
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

## Coverage and gaps

As of the records selected in `report/selection.json`, benches exist for gain,
GBW, phase margin, slew rate, output swing, quiescent power, noise, offset
(mismatch Monte Carlo), CMRR and PSRR. Slew rate (worst 14.51 V/us), output
swing (worst 2.465 Vpp) and quiescent power (worst 310.98 uW) pass their
ratified bounds at 45/45 points; the slew and power binding corners
(ss / 125 C / 2.97 V and ff / -40 C / 3.63 V) differ in temperature from the
spec's predicted SS / -40 C / low VDD and FF / 125 C / 3.63 V (see
`slew-swing-power/README.md`). Common
limitations: passives (RZ/CC) are at typical only; offset covers five process
corners x 300 samples at 27 C / 3.30 V only (no temperature or supply axis);
CMRR and PSRR are systematic-only (matched devices). Noise, offset, CMRR and
PSRR are measured without a verdict because their numeric bounds are not
ratified. There is no layout or post-layout evidence. The 5 V stretch row is
not opened. These records are schematic-level evidence below the T1 tier.
