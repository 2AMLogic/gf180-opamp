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

## Shared harness module

`harness.py` is the one master copy of the cross-experiment sim-harness
helpers — gf180mcu PDK discovery (`find_pdk`), ngspice version provenance,
append-only record-id allocation, and the per-corner ngspice deck executor.
Every runner (`design/check_dc_op.py`, both `sim/<experiment>/run_*.py`)
imports it; deck composition, extraction and plotting stay per-experiment.

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
  experiment. A record that a sibling record `**Supersedes**` is rejected
  (opt in knowingly with `allow_superseded`); records measuring different DUT
  versions (compared on the 16-hex normalised-netlist prefix) are rejected
  with exit 2.
- Every row of `spec/target-spec.md` Sec. 2 appears, plus a post-layout line.
  Status is `measured-verdict` (ratified bound + a record that judged it),
  `measured-no-bound` (worst value shown, bound open, never a verdict) or
  `not-measured` (no committed record). Coverage, limitations, per-source
  sha256, DUT hash, PDK revision and tool versions are listed.
- Existing records have no structured sidecars, so the verdict/worst-case
  lines are extracted from the Markdown (cross-checked against each record's
  own per-point table); historical records are never modified.
- Regenerate: `python3 sim/report/characterization_report.py` (after editing
  `selection.json`); verify the committed copy: `... --check` (exit 1 if
  stale). `characterize.sh` ends with `--latest --update-manifest`, selecting
  the newest record of each experiment; `selftest.sh` runs
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

## Coverage and gaps

As of the records selected in `report/selection.json`, benches exist for gain,
GBW, phase margin, noise, offset (mismatch Monte Carlo), CMRR and PSRR. Common
limitations: passives (RZ/CC) are at typical only; offset covers five process
corners x 300 samples at 27 C / 3.30 V only (no temperature or supply axis);
CMRR and PSRR are systematic-only (matched devices). Noise, offset, CMRR and
PSRR are measured without a verdict because their numeric bounds are not
ratified. No bench is committed yet for slew rate, output swing or quiescent
power, and there is no layout or post-layout evidence. The 5 V stretch row is
not opened. These records are schematic-level evidence below the T1 tier.
