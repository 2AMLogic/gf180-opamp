# gf180-opamp

A two-stage Miller-compensated operational amplifier on GF180MCU on
[GlobalFoundries GF180MCU](https://github.com/google/gf180mcu-pdk), a 180 nm open CMOS PDK — designed by AI agents driving
[klayout-tools](https://github.com/2AMLogic/klayout-tools) and the
open-source xschem + ngspice flow.

**Status: just opened.** Nothing is designed yet. The first work is
the gm/ID device-characterization study at 3.3 V, with the 5 V device flavors surveyed but not yet characterized.

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

## Target specification (DRAFT — engineering to ratify)

Same row structure as the twins, at 3.3 V primary (5 V flavor as a labelled
stretch row pending a decision record). Rows filled only from committed
benches at PVT corners.

The full per-row table, with per-row value tags (`[P]`/`[TBD]`) and binding
corners, lives in [`spec/target-spec.md`](spec/target-spec.md) — every
performance row is currently `[TBD]`, since no gm/ID device characterization
or PVT-cornered simulation exists in this repo yet. See
[`spec/porting-plan.md`](spec/porting-plan.md) for what carries over from
this block's nearest same-PDK siblings (`gf180-bandgap`, `gf180-ldo`) and
`sg13g2-bandgap`'s amp-characterization testbench shape, and the
[gap-to-T1 tracker](https://github.com/2AMLogic/gf180-opamp/issues/7) for the
block's current distance from the klayout-tools T1 ("sim-validated")
design-evidence tier.

## License

Apache-2.0.
