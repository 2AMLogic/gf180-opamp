# sim/

ngspice testbenches and append-only results.

## One-command driver

- `./characterize.sh` — regenerates every testbench's full PVT-cornered
  evidence (mints new append-only records).
- `./selftest.sh` — fast smoke check (no records written), confirming each
  testbench still converges to a sane operating point.

Mirrors `gf180-comparator`'s `characterize.sh`/`selftest.sh` split. See
`gain-gbw-pm/README.md` for what each currently drives.

## Experiments

- [`gm-id-characterization/`](gm-id-characterization/README.md) — gf180mcu
  3.3 V MOS (`nfet_03v3`, `pfet_03v3`) gm/ID, gm/gds and fT vs overdrive,
  across the `typical/ff/ss/fs/sf` process corners at −40/27/125 °C. The
  first characterization study for this block (`CLAUDE.md`'s "gm/ID first"
  ordering, issue #10) — feeds the future topology/sizing decision and
  `spec/target-spec.md` §1's corner-grid row. (Predates
  `characterize.sh`/`selftest.sh`; still run directly per its own README.)
- [`gain-gbw-pm/`](gain-gbw-pm/README.md) — open-loop DC gain, GBW and phase
  margin of a **provisional, smoke-level** two-stage Miller-compensated
  op-amp netlist (DR-0001 topology), via the "big resistor" open-loop AC
  testbench, across the same `typical/ff/ss/fs/sf` × −40/27/125 °C grid. The
  repo's first **circuit-level** spec-row testbench (issue #19) — evidence
  pending a real sizing pass and spec ratification, not a pass/fail
  verdict.
