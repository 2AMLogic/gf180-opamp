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
  DC gain and GBW pass at 45/45 points (worst 93.79 dB and 10.42 MHz, both at
  SS / 125 C / 2.97 V); slew rate, output swing and quiescent power also pass
  at 45/45 points (worst 14.51 V/us at SS / 125 C / 2.97 V, 2.46 Vpp at
  SS / 125 C / 2.97 V, and 310.98 uW at FF / -40 C / 3.63 V; record
  [`20261009-142137-1dab1db`](sim/slew-swing-power/records/20261009-142137-1dab1db.md));
  these PASS verdicts cover the MOS / temperature / supply grid only, with
  passives at typical; phase margin **fails** its 60 degree target, passing
  at only 15/45 points (worst 57.34 degrees at FS / 125 C / 2.97 V). Record:
  [`20261009-055759-2524b3e`](sim/gain-gbw-pm/records/20261009-055759-2524b3e.md).
  The circuit repair is tracked in [#42](https://github.com/2AMLogic/gf180-opamp/issues/42).
- **Measured, bound still open (no verdict):** input-referred noise
  ([`20261009-082007-68b4567`](sim/noise/records/20261009-082007-68b4567.md),
  45 points), input offset mismatch Monte Carlo
  ([`20261010-083043-ddf96db`](sim/offset-mc/records/20261010-083043-ddf96db.md),
  45-point PVT grid x 300 samples per point; worst |mean| + 3 sigma 15.458 mV
  at typical / 27 C / 3.30 V), CMRR
  ([`20261009-105631-30ec86d`](sim/cmrr/records/20261009-105631-30ec86d.md))
  and PSRR
  ([`20261009-105929-30ec86d`](sim/psrr/records/20261009-105929-30ec86d.md)),
  both systematic-only (matched devices, 45 points). Their numeric bounds are
  not ratified.
- **Not yet measured:** post-layout verification (no layout evidence; area is
  open).

The selected records use typical passives; the 45-point grid verdicts above do not sweep RZ/CC. A
separate 27-cell gain/GBW/PM passive-corner study
([`20261009-234014-55b400c`](sim/gain-gbw-pm/records/20261009-234014-55b400c.md))
shows PM >= 60 degrees at only 7/27 cells and GBW PASS at 24/27 cells with
FAIL at 3/27: all three failing cells sit at one MOS/T/VDD point (SS / 125 C /
2.97 V) with CC worst, one per RZ level (typical/best/worst = 9.663/9.656/9.673
MHz; worst 9.656 MHz, below the 10 MHz bound). That gain/GBW/PM study is
the only committed passive sweep; slew, swing, power and the other figures
remain at typical passives only. The `klt signoff` verdict of
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

## License

Apache-2.0.
