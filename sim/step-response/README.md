# `sim/step-response/` — closed-loop follower step response (overshoot, settling)

**Evidence only; no spec row, no verdict.** This experiment measures the
small-step response of the unity-gain follower, the configuration the error-amp
slots in `gf180-ldo` / `gf180-bandgap` (`spec/target-spec.md` §5) actually use,
on the **committed sized schematic** across the full ratified 45-point PVT grid
(issue #113). It exists so a reviewer of the phase-margin miss (#42: PM below
the ratified 60° at 30/45 points, worst 57.34° at FS / 125 °C / 2.97 V) can see
what that margin means in the time domain: overshoot, settling time and whether
the response rings.

`spec/target-spec.md` has **no settling-time or overshoot row**. This
experiment proposes no bound and judges nothing against one; it records the
numbers and identifies the worst points. Adding a settling or overshoot row
would need a `spec/` decision record (a possible follow-up, not part of this
experiment). Nothing in `spec/` is touched here.

The layout mirrors [`../slew-swing-power/`](../slew-swing-power/README.md):
`testbench/`, a `run_*.py` driver on `sim/harness.py`, a stdlib
`measurement_config.py` (fingerprint), `records/`, `netlist-snapshots/`,
`corners/`, and a no-simulator unit-test file.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, included
  as-is through the shared `subckt_from_export()` normalisation and
  instantiated as `Xdut vdd 0 vinp vout vout ibias opamp_two_stage`
  (unity-gain follower, `vinn = vout`). **The testbench declares no
  transistor.** The bench and DUT guards are the gain driver's (one copy):
  the bench must include the design-derived DUT, have exactly one `Xdut`, be a
  circuit body; the materialised DUT must contain every line of the committed
  export unchanged and in order (a stale, resized or hand-edited DUT is
  rejected before anything is simulated).
- **Load**: `CL = 2 pF` [DR-1] on `vout`, nothing else — identical to the slew
  bench and to the CL of the GBW/PM record.
- **Bias**: 10 µA into `ibias`.
- **Common mode**: VCM **tracks VDD/2** (1.485 / 1.65 / 1.815 V at 2.97 /
  3.30 / 3.63 V), the convention of every other experiment in `sim/` and of the
  gain record whose PM this experiment illuminates. At the nominal supply VCM
  is 1.65 V, the value named in the issue.
- **Step**: ±50 mV about VCM (100 mV), fixed in volts: input low until
  100 ns, a 1 ns rising edge, a 1 ns falling edge at 600 ns, run to 1.1 µs.
  Both edges are measured; per point the **worse** edge is reported (both are
  in the table).
- **Grid (45 points)**: MOS process `typical, ff, ss, fs, sf` × −40, 27,
  125 °C × 2.97, 3.30, 3.63 V; every MOS corner paired with `res_typical` /
  `mimcap_typical` (no independent RZ / CC corner coverage is claimed — the
  PM-relevant passives are at typical).
- **One `klt sim` request carrying all 45 points.** Where it runs is `klt`'s
  decision (`--backend`, `$KLT_SIM_BACKEND`: the Spot batch fleet on a
  dispatch worker). The driver never loops ngspice over the grid and never
  falls back to a local grid: a refused or failed submit writes **no record**
  and exits 2. Only the three single-unit controls (nominal point) run locally.

## Step size and window vs the small-signal GBW

The ratified GBW ≥ 10 MHz bounds the closed-loop time constant of the
follower at `tau = 1/(2π·GBW) ≤ 15.9 ns` (the measured GBW spans 10.4–16.5 MHz,
`tau` 9.6–15.3 ns). Each edge window is 500 ns ≈ **31 tau** at the bound; a
single-pole response reaches 0.1 % in 6.9 tau ≈ 110 ns, and the measured
nominal response reaches 0.1 % in ~85 ns. The sample interval (`.tran 1n`,
which also bounds ngspice's internal step) is ≤ tau/10. The settled level is
read over the **last 50 ns** of each window. A unit test pins these ratios.

**Is 100 mV small-signal?** Mostly. At the nominal point the steepest output
slope during the step is ≈ 6 V/µs, below the ≈ 14.5 V/µs slew rate of the same
bench (record `sim/slew-swing-power/records/20261009-142137-1dab1db.md`), so
the output stage is not slewing. The rising and falling edges are not identical (nominal overshoot 7.3 %
vs 9.4 %), so the input pair is not perfectly linear at ±50 mV; the record
reports both edges rather than hiding that asymmetry. The step size is the
issue's; a smaller step would be a different experiment.

## Extraction

Per edge, from the rawfile klt retains (`v(vinp)`, `v(vout)`):

1. **Validity** — `v0` = `vout` 10 ns before the edge must equal `vinp` to
   20 mV; `vfinal` = the time-averaged `vout` over the 50 ns reference window
   at the end of the edge window (ending 10 ns before the next edge, or at the
   end of the run) must equal `vinp` there to 20 mV; the realised step
   `vfinal − v0` must be 100 mV ± 20 % in the edge's direction. Otherwise the
   point is **INVALID** (the follower did not follow), never a fast settle.
2. `y = (vout − v0) / (vfinal − v0)` over the edge window (end points
   interpolated).
3. **Overshoot** = `max(y) − 1` (%). **Preshoot** = `−min(y)` (a wrong-way
   start). **Monotonic** iff the largest fall of `y` below its running maximum
   and the preshoot are both ≤ 0.1 % of the step (so any overshoot above 0.1 %
   makes the response non-monotonic).
4. **Settling time** to band `b` (1 % and 0.1 % of the realised step) = time
   from the input edge (start of the 1 ns ramp) to the last exit of
   `|y − 1|` from `b`, interpolated linearly. Settling is relative to the
   output's own settled level, so the follower's systematic offset does not
   enter. **Not settled** is reported (and ranked as the worst settling
   result) when the output is still outside `b` inside the reference window,
   **or** when the output drifts by more than `0.1·b` across the reference
   window — a slow tail would otherwise hide in the window mean. The drift rule
   is deliberately conservative: a tail whose time constant is comparable to
   the window can be reported "not settled" at a band it would reach just
   after the window.
5. **10–90 % time**, reported only to show how slew-affected the step is.

## Controls and cross-checks

- *Cross-checks*: ngspice `.meas` values (pre-edge levels, the maximum in the
  rising window, the minimum in the falling window) must agree with the
  rawfile extraction to 0.5 mV (0.5 % of the step); a disagreement blocks the
  record.
- *Re-derivation*: every point's `corners/<rid>/<point>.dat` holds every
  rawfile sample (`time_s vout_v vinp_v`); before writing the record the driver
  re-reads each file and re-runs the extraction, and requires the same
  validity, overshoot, settling times and monotonic flag. The count is in the
  record.
- *Controls* (single local units at the nominal point, recorded apart from
  the grid): `cl-x10` (CL = 20 pF, lower phase margin) must overshoot more than
  nominal by > 1 percentage point and be non-monotonic — the extraction sees
  added ringing; `ibias-zero` (dead amplifier) must never yield a valid,
  1 %-settled step. A failed control makes the driver exit non-zero.
- *Unit tests* (`test_step_response.py`, no simulator): first-order
  (monotonic; settling equal to `tau·ln(1/b)`), underdamped second-order
  (overshoot equal to `exp(−πζ/√(1−ζ²))`, settling equal to a 1 ps brute-force
  reference), sustained ringing and slow tails (not settled), preshoot and
  sub-tolerance reversals, stuck / clipped / offset / malformed waveforms
  (invalid), the worse edge reported, systematic offset ignored, worst-point
  ranking, control logic, the data file round trip, `.meas` disagreement
  detection, the bench guards, the **stale-DUT guard** (a resized, dropped or
  duplicated device is rejected before anything is written), bench timing vs
  the configuration, and the window-vs-GBW ratios.
  `sim/ci_regression_check.sh` mutates the shared DUT-include guard, the
  stale-DUT guard and the overshoot extraction and requires this suite to
  fail each time.

## Running

```sh
python3 sim/step-response/run_step_response.py --batch-runner-version-check warn   # 45-point grid + controls + record
python3 sim/step-response/run_step_response.py --smoke    # one nominal point, local, no record
python3 sim/step-response/test_step_response.py           # unit tests, no simulator
```

Prerequisites are the siblings': `klt`, `ngspice`, the pinned gf180mcu PDK
and `numpy` / `matplotlib`. The driver imports
`../gain-gbw-pm/run_gain_gbw_pm.py` (DUT normalisation, source guards, request
skeleton) and `../slew-swing-power/run_slew_swing_power.py` (rawfile parser),
so there is one copy of each. Useful flags: `--backend`,
`--batch-submit-retries N`, `--batch-runner-version-check warn`.

Each run mints a new append-only record id and writes only new files:

```
records/<rid>.md, records/<rid>-plots/step_all45.png
netlist-snapshots/<rid>.spice            DUT + testbench + klt request + nominal deck
corners/<rid>/<process>_<T>c_<vdd>v.{log,cir,dat}, klt-report.json
corners/<rid>/controls/*
```

The aggregate report (`sim/report`) does not select this experiment: there is
no spec row for it to fill.

## Records

| Record | Result | Notes |
|---|---|---|
| [`20261010-082214-a2cc91f`](records/20261010-082214-a2cc91f.md) | 45/45 points valid. Worst overshoot **11.30 %** at fs / 125 °C / 3.63 V (11.28 % at fs / 125 °C / 2.97 V, the gain record's lowest-PM point, 57.34°); worst settling **84.1 ns to 1 %** and **122.7 ns to 0.1 %**, both at fs / 125 °C / 2.97 V; non-monotonic (overshoot > 0.1 %) at 45/45; preshoot ≤ 0.08 % | One fleet request (`aws-batch-fleet`, job `klt-sim-aae64f5afc3d`, Spot; runner klt 0.5.0, `runner_version_check: warn`, as for the sibling grids). `.meas` cross-checks 45/45, re-derivation 45/45, controls behave. Its Method section does not spell out the reference-window drift rule; the rule was applied (the extraction code is the committed one) and the largest drift over the grid is 1e-7 % of the step, so it decides nothing in this record. Later records state it |

### Reading the first record (observations, not a verdict)

- **Overshoot tracks the phase margin.** Using the gain record's PM per
  point: PM ≥ 60° points overshoot 7.40–8.70 %, PM < 60° points 9.14–11.30 %
  (worse edge). The ordering by corner is the PM ordering (fs worst, sf best;
  125 °C worst); supply barely matters.
- **The falling edge rings more** than the rising edge at every point (by
  about 2 percentage points, e.g. 7.31 % vs 9.37 % nominal) — the ±50 mV step
  is not perfectly small-signal for the input pair; see "Is 100 mV
  small-signal?" above.
- **The 1 % settling time is discontinuous at fs / 125 °C.** There the first
  undershoot after the overshoot peak reaches 1.17 % of the step (78 ns after
  the edge) on the falling edge, just outside the 1 % band, so that edge's 1 %
  settling moves from the first to the second ring lobe (≈ 62 → 84 ns); at
  fs / 27 °C the same undershoot is 0.98 % and stays inside. This is a property
  of the band definition, not a separate effect.
- Every response settles well inside the window (worst 0.1 % at 123 ns of a
  500 ns window); no point is "not settled".

Whether ≈ 11 % overshoot / ≈ 120 ns 0.1 % settling is acceptable for the
consumers in `spec/target-spec.md` §5 is **not** decided here; that is a
question for #42 and, if a bound is wanted, a `spec/` decision record.
