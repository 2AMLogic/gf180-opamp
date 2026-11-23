# 0009: Scope the slew-swing-power passive RZ x CC matrix per figure

- **Status**: decided by the operator (ruling 2026-11-23, option (a)). It
  narrows what the `sim/slew-swing-power` passive side study *runs*; it sets
  no spec value and changes no `spec/target-spec.md` row, bound or condition.
- **Date**: 2026-11-23
- **Decided by**: operator (`rjwalters`, attended `/fleet:decide` session,
  round 20261121-r19, r19-q270, slate r19-s23); implemented by Builder, issue
  #322
- **Related**: #322 (this change), #97 (DR-3 section (d) RZ-tracking
  obligation), #150 (full 405-cell envelope), DR-0003, DR-0004
  (`reltol` policy; the measurement note below leans on it),
  [`sim/passive_corners.py`](../../sim/passive_corners.py),
  [`sim/slew-swing-power/run_slew_swing_power.py`](../../sim/slew-swing-power/run_slew_swing_power.py)

## Context

`sim/slew-swing-power --passive-corners` applied the uniform 3 x 3 RZ x CC
passive matrix to all three of its figures. At full scope that is 405 cells
per figure, 1215 per run, on the vCPU-budgeted Spot fleet. Hashing every `.dat`
of the two existing full-scope records shows most of that is copies:

| Record | power distinct / 405 | swing distinct / 405 | slew distinct / 405 |
|---|---|---|---|
| `sim/slew-swing-power/corners/20261018-142357-91dab49` | 45 | 105 | 405 |
| `sim/slew-swing-power/corners/20261011-052206-c635487` | 45 | 105 | 405 |

Varying only CC leaves power and swing unchanged (1 distinct value per
group); varying only RZ leaves power unchanged and moves swing in 3 groups;
slew separates on both axes. `sim/gain-gbw-pm` is 405/405 distinct and is not
affected. The passive substitution demonstrably reaches the netlist (slew
separates every cell), so this is not a bench artifact.

## Decision

Per-figure passive axes (`FIGURE_PASSIVE_AXES` in `sim/passive_corners.py`),
a dropped axis being held at `typical`:

| Figure | Passive axes swept | Cells, full | Cells, limited |
|---|---|---|---|
| slew | RZ x CC (3 x 3) | 405 | 27 |
| swing | RZ only (CC = typical) | 135 | 9 |
| power | none (RZ = CC = typical) | 45 | 3 |

That removes 660 of 1215 cells per full-scope run (60 of 81 limited).

- **Invariance is asserted, not re-derived, on future runs.** The records
  written by the reduced runner state the dropped axes per figure and cite the
  two records above as the basis, instead of re-printing all-`+0.0` tables.
- **Audit stays available.** `sim/passive_corners.py` keeps the uniform 3 x 3
  as the default of every helper (`passive_combos`, `passive_process_axis`,
  `passive_expected_keys`, `apply_passive_matrix`); an audit over the full
  matrix needs only the runner to pass `ALL_PASSIVE_AXES`. No new CLI flag is
  added by this record.
- **`sim/gain-gbw-pm` is unchanged**, as are `sim/psrr` and `sim/cmrr`.
- **Existing records and waveforms are append-only and untouched.**
- **Spec rows are untouched.** `spec/target-spec.md` already grounds the
  Quiescent power row on the MOS/temperature/supply grid with passives
  typical; the DR-3 section (d) RZ-tracking obligation is measured by slew
  (and `sim/gain-gbw-pm`), which keep their full coverage.

## Consequences

- A future change that makes power depend on RZ/CC, or swing depend on CC,
  would not be noticed by a reduced run. Re-run the uniform 3 x 3 audit after
  any compensation or bias retune that could create such a dependence.
- Note (not part of the ruling): a Hermit measurement on the same record
  puts swing's RZ-axis deviation at <= 2.3e-8 relative, far below ngspice's
  default `reltol` (DR-0004), suggesting swing could drop RZ too (45 cells).
  Narrowing swing further needs its own ruling; this record does not do it.

## Verification

The reduced full-scope run (`sim/slew-swing-power/records/` entry referenced in
the carrying PR) is compared cell by cell against `20261018-142357-91dab49`;
the reduced run must reproduce the original values for every cell it keeps.
