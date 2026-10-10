# gf180-opamp

A two-stage Miller-compensated operational amplifier on GF180MCU on
[GlobalFoundries GF180MCU](https://github.com/google/gf180mcu-pdk), a 180 nm open CMOS PDK — designed by AI agents driving
[klayout-tools](https://github.com/2AMLogic/klayout-tools) and the
open-source xschem + ngspice flow.

**Status: schematic-level characterization, below T1.** The 3.3 V gm/ID
device-characterization study is committed
([`sim/gm-id-characterization/`](sim/gm-id-characterization/README.md)), the
topology is decided ([DR-0001](spec/decision-records/0001-topology-and-cl.md)),
and a gm/ID-sized schematic of that topology exists in
[`design/`](design/README.md). The target spec is **partially ratified**
([DR-0003](spec/decision-records/0003-target-spec-ratification.md)): the
ratified rows are design-to targets, five rows are held open, and nothing is
ratified as met. Target ratification, measurement availability and measured
compliance are three different things, as of the committed records named
below:

- **Measured against a ratified bound (45-point PVT grid, schematic level):**
  after the Miller-compensation retune of [#42](https://github.com/2AMLogic/gf180-opamp/issues/42)
  (RZ 2.11 kOhm -> 11.23 kOhm, CC 0.619 pF -> 0.578 pF), DC gain, GBW and
  phase margin all pass at 45/45 points (worst 93.79 dB and 11.910 MHz, both at
  SS / 125 C / 2.97 V, and 71.99 degrees at FS / 125 C / 2.97 V; record
  [`20261010-161514-0127eb7`](sim/gain-gbw-pm/records/20261010-161514-0127eb7.md));
  slew rate, output swing and quiescent power also pass at 45/45 points
  (worst 15.50 V/us at SS / 125 C / 2.97 V, 2.46 Vpp at SS / 125 C / 2.97 V,
  and 310.98 uW at FF / -40 C / 3.63 V; record
  [`20261010-161852-0127eb7`](sim/slew-swing-power/records/20261010-161852-0127eb7.md)).
  Before the retune, PM passed at only 15/45 points (worst 57.34 degrees) and
  GBW worst was 10.42 MHz (records `20261010-020141-1e51d1c`,
  `20261009-142137-1dab1db`, kept unchanged). These PASS verdicts cover the
  MOS / temperature / supply grid only, with passives at typical.
- **Measured, bound still open (no verdict):** input-referred noise
  ([`20261010-163507-0127eb7`](sim/noise/records/20261010-163507-0127eb7.md),
  45 points), input offset mismatch Monte Carlo
  ([`20261010-185141-e4be1ac`](sim/offset-mc/records/20261010-185141-e4be1ac.md),
  45-point PVT grid x 300 samples per point; worst |mean| + 3 sigma 15.458 mV
  at typical / 27 C / 3.30 V), CMRR
  ([`20261010-163601-0127eb7`](sim/cmrr/records/20261010-163601-0127eb7.md))
  and PSRR
  ([`20261010-163854-0127eb7`](sim/psrr/records/20261010-163854-0127eb7.md)),
  both systematic-only (matched devices, 45 points); the characterization
  report also lists the CMRR mismatch Monte Carlo record
  ([`20261010-202454-e4be1ac`](sim/cmrr-mc/records/20261010-202454-e4be1ac.md),
  five corners x 300 samples at 27 C / 3.30 V only) beside it as separate,
  ungraded information. Their numeric bounds are
  not ratified.
- **Not yet measured:** post-layout verification (no layout evidence; area is
  open).

The selected 45-point records use typical passives; they do not sweep RZ/CC.
Two 27-cell passive-corner studies on the retuned design (RZ and CC each at
the PDK typical/ff/ss sections, at FS / 125 C / 2.97 V, SS / 125 C / 2.97 V
and typical / 27 C / 3.30 V) cover only those three points, not the full 45:
gain/GBW/PM
([`20261010-161720-0127eb7`](sim/gain-gbw-pm/records/20261010-161720-0127eb7.md))
shows PM >= 60 degrees at 27/27 cells (worst 64.47 degrees) and GBW >= 10 MHz
at 27/27 cells (worst 10.726 MHz, SS / 125 C / 2.97 V, RZ ff, CC ss); slew,
swing and power
([`20261010-162823-0127eb7`](sim/slew-swing-power/records/20261010-162823-0127eb7.md))
pass 27/27 per row (slew worst 14.25 V/us). No independent passive-corner
pass is claimed for the full grid, and the other figures remain at typical
passives only. The `klt signoff` verdict of
record ([`manifests/`](manifests/README.md)) is still below T1; no T1 claim
is made. The 5 V device flavors remain surveyed but not characterized, and
the 5 V stretch row stays unopened.

**Built agent-native.** Every specification, decision record, testbench, and
line of documentation here is produced by AI agents working from a ratified
spec and an append-only evidence trail — not human-authored work that agents
merely assisted with. Verification is the product: every claim traces to a
recorded result under PVT corners. Where the agents hit friction with the
open-source tooling — most often
[klayout-tools](https://github.com/2AMLogic/klayout-tools) — that friction is
filed as a public issue against the tool itself, so the fix benefits everyone
using this PDK, not just this repo.

## Why this block, on this PDK

The three-foundry op-amp twin on GF180MCU (see sg13g2-opamp for the
program). GF180 adds one honest wrinkle the other twins lack: the PDK's
5 V-tolerant device flavors. The primary spec is 3.3 V — same as this repo's
sibling canaries and their ratified specs — with the 5 V analog rail carried
as an explicitly-labelled stretch row, in line with how the Chipalooza
Challenge #5 work across the gf180 canaries treats that rail: scoped in
only by a decision record, never silently.

The block is also deliberate bench infrastructure: LDO error amplifiers,
ADC drivers, and filter stages across the gf180 canaries embed op-amps that
have never been standalone-characterized on this PDK.

## Target specification (RATIFIED, partial)

Same row structure as the twins, at 3.3 V primary (5 V flavor as a labelled
stretch row pending a decision record). Targets are design-to bounds ratified
by [DR-0003](spec/decision-records/0003-target-spec-ratification.md); rows
whose evidence was missing at ratification stay `[TBD]`.

The full per-row table, with per-row value tags, status and binding corners,
lives in [`spec/target-spec.md`](spec/target-spec.md) — the noise, offset,
CMRR, PSRR and area rows are still `[TBD]` (open bounds), although noise,
offset, CMRR and PSRR now have committed measurements (see the status above).
See
[`spec/porting-plan.md`](spec/porting-plan.md) for what carries over from
this block's nearest same-PDK siblings (`gf180-bandgap`, `gf180-ldo`) and
`sg13g2-bandgap`'s amp-characterization testbench shape, and the
[gap-to-T1 tracker](https://github.com/2AMLogic/gf180-opamp/issues/7) for the
block's current distance from the klayout-tools T1 ("sim-validated")
design-evidence tier.

## Orchestration notes

Sweep coordinators that need a main-clean baseline should follow
[`docs/sweep-baseline.md`](docs/sweep-baseline.md): it covers the unique
scratch path outside the repository, the snapshot and baseline commands,
retention, cleanup and what happens if the baseline goes missing. The recipe
is checked by
[`scripts/test-sweep-baseline.sh`](scripts/test-sweep-baseline.sh).

## License

Apache-2.0.
