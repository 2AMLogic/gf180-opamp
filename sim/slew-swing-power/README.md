# `sim/slew-swing-power/` — slew rate / output swing / quiescent power

Spec-row evidence for three ratified rows of `spec/target-spec.md` §2 —
**slew rate** (≥ 10 V/µs), **output swing** (≥ 2.3 Vpp, stretch ≥ 2.6 Vpp)
and **quiescent power** (≤ 350 µW at the worst corner) — measured on the
**committed sized schematic** across the full ratified grid (issue #44;
tracker #7 item 5). It is the large-signal / DC sibling of
[`../gain-gbw-pm/`](../gain-gbw-pm/README.md) and follows its layout:
`testbench/`, a `run_*.py` driver on `sim/harness.py`, `records/`,
`netlist-snapshots/`, a no-simulator unit-test file.

The bounds are the ratified ones; this experiment never edits them. A row
that misses is a *result* — recorded as measured, with its binding corner,
and handed to its own design issue (as #42 did for phase margin), not
resized here.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, included
  as-is (through the same `subckt_from_export()` conversion the sibling
  experiment and `design/check_dc_op.py` use) and instantiated as
  `Xdut vdd 0 vinp vinn vout ibias opamp_two_stage`. **No testbench declares
  a transistor**; `test_slew_swing_power.py` guards that, and that each
  bench has exactly one `Xdut`, includes the design-derived DUT, and is a
  circuit body (no `.lib`/`.temp`/`.control`/`.end` — `klt sim` adds those).
- **Bias**: 10 µA driven *into* `ibias` (the polarity `check_dc_op.py`
  uses). Input common mode tracks VDD/2 (`Vdd` and `Vcm` are altered
  together by `klt sim`).
- **Grid (45 points)**: MOS process `typical, ff, ss, fs, sf` × temperature
  −40, 27, 125 °C × supply 2.97, 3.30, 3.63 V. As in the sibling, every MOS
  corner is paired with the same `res_typical` / `mimcap_typical` sections:
  **no independent RZ / CC corner coverage is claimed** (this matters most for
  slew, which scales with `Itail / CC`).
- **One `klt sim` request per figure, each carrying all 45 points** (3
  requests: `power`, `slew`, `swing`), because the three figures need
  different analyses. Where they run is `klt`'s decision (`--backend`, the
  request's `backend`, `$KLT_SIM_BACKEND` — the Spot batch fleet on a dispatch
  worker). The driver never hand-loops `ngspice` over a grid and never falls
  back to a local grid when a batch submit fails: that figure is recorded
  `NOT RUN` with the error.

## Figures and criteria

### Quiescent power (`testbench/tb_power.spice`)

Unity-gain follower at VCM, **no load of any kind** on `vout`. Power is
`−i(Vdd) × VDD` at `Ibias = 10 µA`, read at the first point of a one-step DC
sweep of `Ibias` (the `.op`-equivalent form every `klt` runner supports;
there is no `.meas op`). `Ibias` is drawn through `Vdd`, so this is the
*total* supply current including the bias reference branch. The target-spec
binding-corner prediction is FF / 125 °C / 3.63 V.

### Slew rate (`testbench/tb_slew.spice`)

Unity-gain follower (`vinn = vout`), `CL = 2 pF` [DR-1], input step of
**1.0 Vpp (±0.5 V about VCM)** — far above the ~`Itail/gm1` slewing onset so
the edges are slew-limited — one rising edge (1 µs) and one falling edge
(3 µs) in a single transient. Per edge:

- the levels are the *settled* output just before the edge (low level before
  the rising edge, high level before the falling edge); each must equal the
  input there to within 20 mV, otherwise the output never followed the step
  and the point is **invalid** (never a fast slew of a wrong swing);
- slope = `0.6 × (vhi − vlo) / (t80 − t20)` between the first 20 % and 80 %
  crossings, linearly interpolated.

The reported figure is the **slower of the two edges**. The step is a fixed
±0.5 V about VCM (not a fraction of VDD) so the pulse source is a plain
`pulse(...)` in series with the altered DC `Vcm`; its levels stay within the
input common-mode range at the 2.97 V / −40 °C corners (checked by the
settled-level validity test at all 45 points).

**Batch-fleet timing (why 5 ns sampling and a two-node `.save`).** Two earlier
45-point slew submissions (records `20261009-114727-1dab1db`,
`20261009-121017-1dab1db`) were `NOT RUN`: every point hit the per-point
timeout. Probing the fleet with sub-grids of the same deck showed the per-point
runtime growing steeply with the number of points in the job (3 points: 0.6 s
each; 6: 8 s; 15 at 0.5 ns sampling: > 120 s, i.e. timeout; 15 at 5 ns
sampling: 37 s), while single points run in 0.3 s locally -- a fleet-side
scaling problem outside this repo, filed as `2AMLogic/klayout-tools#2970`; the third submission (record `20261009-142137-1dab1db`) is the complete one.
The driver therefore samples the output every 5 ns (1000 rows instead of
10000; ngspice's internal step is bounded by the same interval, and the
20-80 % window of a >= 10 V/us edge still holds >= 5 samples, interpolated
linearly) and saves only `v(vinp)` and `v(vout)`.

### Output swing (`testbench/tb_swing.spice`)

**Configuration.** Inverting unity gain: `vinp` held at VCM, signal through
`Rg` = 1 MΩ to `vinn`, `Rf` = 1 MΩ from `vout` to `vinn` (ideal gain −1), no
`CL`. Input common mode stays pinned at VCM for the whole sweep, so the
follower's *input* common-mode limits never clip the result: what limits the
output is the output stage, which is what the spec row is about. (The 1 MΩ
feedback network draws ≲ 1 µA from `vout` at the rails against ~80 µA
quiescent.) `Vin` is swept 0 → 3.63 V in 5 mV steps — a fixed range covering
every grid supply.

**Criterion** (the swing edge, walking outward from VCM in each direction, is
the **first** of):

1. *gain collapse*: the incremental closed-loop gain `|dvout/dvin|` falls to
   `1/√2` (−3 dB) of its mid-range value (which must be −1 ± 0.05, else the
   point is invalid); or
2. *output devices leave saturation*: M6 (PMOS common-source gain device,
   limits the upper edge) or M7 (NMOS current sink, limits the lower edge)
   reaches `|Vds| < |Vdsat|`. `Vds` and `Vdsat` of both are saved in the
   rawfile through a `.save` card in the testbench (plain `.meas`/`save all`
   do not expose device internals, and `expr` measurements are not available
   on the fleet runner).

Swing = `vout(upper edge) − vout(lower edge)`, in Vpp; an edge interpolated
linearly between sweep points. A sweep that ends before either condition
occurs makes the point invalid (the swing is never reported as a lower
bound). Both devices must be saturated at the VCM operating point.

**Why this criterion — check against `spec/decision-records/0002-performance-target-bounds.md`
section (b).** (The issue text says "section (c)"; in the committed record the
swing bound is §(b), "Output swing", and §(c) is the list of rows left
`[TBD]`.) DR-2 §(b) defines the row by *output-device headroom*: the stage is
a PMOS common-source device over an NMOS current sink, and
`swing ≈ VDD − (Vov,PMOS + Vov,NMOS)` — i.e. the swing is the range over
which **both output devices stay saturated** — then sets the ≥ 2.3 Vpp target
slightly below that estimate to leave margin for the first stage's own output
headroom. Criterion (2) is exactly that definition, measured instead of
estimated. Criterion (1) additionally ends the swing if the *loop* loses gain
before an output device does (e.g. the first stage's output node, feeding
M6's gate, running out of headroom — the effect DR-2 says its two-device model
omits). A pure gain criterion alone would be wrong for this row: the output
stage has huge W/L, so the loop keeps ≫ 3 dB of gain with M6/M7 well into
triode, and a −3 dB-only swing sits within millivolts of the rails — it
overstates the DR-2 quantity. That variant (and a ±-threshold sensitivity) is
reported in each record's "criterion sensitivity" table for transparency, but
the verdict is judged **only** at the primary criterion above.

The swing is measured with the output unloaded (no resistive load is
specified in `spec/target-spec.md`); a load current would reduce it.

## Controls and cross-checks

- *Cross-checks*: ngspice-native `.meas` values (supply current; pre-edge
  output levels; output extrema) must agree with the rawfile extraction; a
  disagreement blocks the record.
- *Controls* (single local units at the nominal point, recorded apart from
  the grid): `ibias-half` (5 µA) must lower both power (< 0.75×) and slew
  (< 0.8×) — slew really tracks the tail current and power really tracks the
  bias; `ibias-zero` must not pass slew or swing and must draw < 0.1× the
  nominal power. A failed control makes the driver exit non-zero.
- *Unit tests* (`test_slew_swing_power.py`, no simulator): synthetic
  ramps/clips with analytically known slopes and clip levels; the slower edge
  is reported; an output that never settles, is clipped or is stuck is
  invalid; a swing sweep that ends before collapse is invalid; verdict
  direction (power is an upper bound) and binding corner; the ratified
  numbers; the source guards.

## Running

```sh
python3 sim/slew-swing-power/run_slew_swing_power.py            # 3 x 45-point grids + controls + record
python3 sim/slew-swing-power/run_slew_swing_power.py --smoke    # one nominal point per figure, local, no record
python3 sim/slew-swing-power/test_slew_swing_power.py           # unit tests, no simulator
```

Cold-start prerequisites are the sibling's: `klt`, `ngspice`, the pinned
gf180mcu PDK (`volare enable --pdk gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b`)
and `numpy` / `matplotlib`. The driver imports
`../gain-gbw-pm/run_gain_gbw_pm.py` for the shared DUT normalisation, source
guards and `klt` invocation/retry, so there is one copy of those, not two.
Useful flags: `--backend local` (force a backend), `--batch-submit-retries N`,
`--batch-runner-version-check warn`, `--strict` (exit 1 on a spec miss).

Each run mints a new append-only record id and writes only new files:

```
records/<rid>.md, records/<rid>-plots/*.png
netlist-snapshots/<rid>.spice            DUT + testbenches + klt requests + nominal decks
corners/<rid>/<figure>/<process>_<T>c_<vdd>v.{log,cir,dat}, klt-report.json
corners/<rid>/controls/*
```

Records are evidence: the claim line states which rows were measured and
their pass/fail against the ratified bounds with the worst-case corner per
figure.
