# `sim/ibias-cl-sensitivity/` — GBW, PM, slew and power vs. `ibias` and CL

Sensitivity **data** for the external bias current `ibias` and the load
capacitance CL of the committed sized schematic (issue #114). It is evidence
for the phase-margin discussion in #42 (RZ/CC retune vs. a spec decision) and
for the `ibias` tolerance that the integrator view says nothing about. It
**grades nothing**: no verdict, no spec row, bound or `sim/report/selection.json`
entry is touched. Any bound that follows from it goes through a separate
decision record.

## What is measured

Every ratified dynamic row is measured at `ibias` = 10 µA and CL = 2 pF
(`design/README.md`, `spec/target-spec.md` §2, DR-1). This experiment varies
exactly one of those at a time (the sweeps are separate, not a Cartesian
product), reusing the committed benches of the sibling experiments verbatim
except for the single `Ibias` or `CL` line of the materialised copy (the
committed bench files are never edited; each substitution must match exactly
once or the run stops).

| Sweep | Values | Figure (bench, extractor) | Corner points |
|---|---|---|---|
| `ibias` | 8, 9, 10, 11, 12 µA (±20 %), CL = 2 pF | GBW, PM, DC gain (`gain-gbw-pm`) | nominal, PM-binding, GBW-binding |
| CL | 1, 2, 4, 10 pF, `ibias` = 10 µA | GBW, PM, DC gain (`gain-gbw-pm`) | nominal, PM-binding, GBW-binding |
| `ibias` | 8 … 12 µA | quiescent power (`slew-swing-power` power bench; no load) | nominal, power-binding, slew-binding |
| `ibias` | 8 … 12 µA, CL = 2 pF | slew, slower edge (`slew-swing-power` slew bench) | nominal, power-binding, slew-binding |

Corner tuples (process / T / VDD), read from the selected records
`gain-gbw-pm/records/20261010-161514-0127eb7.md` and
`slew-swing-power/records/20261010-161852-0127eb7.md`:

- nominal: typical / 27 °C / 3.30 V
- PM-binding: fs / 125 °C / 2.97 V (71.99° worst after the #42 retune)
- GBW-binding and slew-binding: ss / 125 °C / 2.97 V (11.910 MHz, 15.50 V/µs)
- power-binding: ff / −40 °C / 3.63 V (310.98 µW)

They are constants in `measurement_config.py`; a test checks they lie on the
committed grid and that every one has a row in the committed records.

**The ±20 % `ibias` range and the CL list are exploratory.** The Consumers
section of `spec/target-spec.md` records no amplifier bias tolerance and no CL
other than DR-1's 2 pF, so there is nothing to grade the sweeps against.

## How it runs

Each (figure, sweep value) is ONE small `klt sim` `corners` request restricted
to the named points with klt's `exclude` (18 requests; the 10 µA / 2 pF point
is simulated once and shared by both sweeps). The backend is klt's decision
(`$KLT_SIM_BACKEND=batch` on dispatch workers). The driver never launches a
simulator itself, never loops over a grid, and never falls back to a local run:
if a submit fails it stops with the error, exits 2 and writes **no record**.
`test_ibias_cl_sensitivity.py` guards this on the driver source.

**Control**: the 10 µA / 2 pF points must reproduce the committed records'
per-point GBW, PM, DC gain, power and slew (parsed from the records selected in
`sim/report/selection.json`) within the records' rounding. A mismatch is
written into the record as `CONTROL MISMATCH` and the driver exits 1.

## Reproduce

```
python3 sim/ibias-cl-sensitivity/run_ibias_cl_sensitivity.py
```

On a dispatch worker whose fleet runner's klt is older than the client's, add
`--batch-runner-version-check warn` (as the sibling records do), and
`--batch-submit-retries N` to re-submit refused-for-capacity batch submits.
`--smoke` runs one nominal point as a local single unit and writes nothing.

Needs `numpy` (as the sibling drivers). Tests, no simulator:

```
python3 -m pytest sim/ibias-cl-sensitivity
```

## Evidence layout (append-only)

```
records/<rid>.md                          sensitivity tables, control, fingerprint inputs
corners/<rid>/<unit>/<point>.{log,cir}    per-point logs and generated decks
corners/<rid>/<unit>/klt-report.json      sanitised klt report
corners/<rid>/results.json                every extracted value
netlist-snapshots/<rid>.spice             DUT, benches and request conditions
```

A re-run mints a new record id; existing records are never rewritten.

## Records

| Record | DUT | Notes |
|---|---|---|
| [`20261010-194102-a8928b1`](records/20261010-194102-a8928b1.md) | pre-retune, hash `81fbd914…` | Measured against the **then-selected** records (gain `20261010-020141-1e51d1c`, slew `20261009-142137-1dab1db`), which its header names; the post-retune tuples in this README's "Corner points" section do not retro-apply to it. Retained byte-for-byte. |
| [`20261011-141530-cb03235`](records/20261011-141530-cb03235.md) | **retuned**, hash `33b58dac…` (PR #138) | Same 18-request schedule, same measurement fingerprint `9b51e0a3…`. Its 10 µA / 2 pF controls reproduce the **currently selected** records (`20261010-161514-0127eb7` gain, `20261010-161852-0127eb7` slew/power) within the record-rounding tolerances; no `CONTROL MISMATCH`. Issue #139 asked for this remeasurement. |

### First record vs retuned DUT (comparison, 2026-10-11)

**Descriptive only.** The ±20 % `ibias` range and the CL list remain
exploratory; nothing below introduces a normative bound, and the ratified
60° PM / ≥ 10 MHz GBW rows (DR-1, CL = 2 pF) are not extended to other CL or
`ibias` values.

Shared 10 µA / 2 pF control points:

| Control point | First record `a8928b1` | Retuned record `cb03235` |
|---|---|---|
| nominal GBW / PM | 13.224 MHz / 59.49° | 16.403 MHz / 77.60° |
| PM-binding (fs / 125 °C / 2.97 V) GBW / PM | 11.041 MHz / 57.34° | 12.726 MHz / 71.99° |
| GBW-/slew-binding (ss / 125 °C / 2.97 V) GBW / PM | 10.422 MHz / 58.34° | 11.910 MHz / 72.37° |
| nominal quiescent power @ 10 µA | 273.66 µW | 273.66 µW (identical) |
| nominal slew @ 10 µA | 14.97 V/µs | 16.15 V/µs |
| nominal DC gain | 95.99 dB | 95.99 dB (identical) |

- **PM is now clearly `ibias`-sensitive.** Over the ±20 % sweep the nominal
  PM moves −2.4 % / +1.7 % (75.77–78.89°), where the first record moved only
  ±0.1 % (59.48–59.58°); at the PM-binding corner −2.6 % / +2.2 % (was
  −0.2 % / +0.2 %). The retune's larger null resistor makes PM track the
  bias current's gm changes. GBW sensitivity grew too (nominal −16.8 % /
  +17.1 %, was −13.2 % / +11.8 %).
- **CL sensitivity keeps its shape at a higher level.** Relative deltas are
  near-identical (nominal PM −21.8 % at 4 pF, −46.9 % at 10 pF; was −21.2 %
  / −46.8 %), but in absolute terms CL = 4 pF now reads 60.67° (was
  46.87°) and CL = 10 pF 41.23° (was 31.63°) at nominal; at the PM-binding
  corner CL = 4 pF reads 56.08° (was 45.00°).
- **Gain is flat against both sweeps, and unchanged between the records.**
  Over the ±20 % `ibias` range DC gain moves at most 1.0 % at every corner
  in both records (nominal 96.70 → 95.32 dB for 8 → 12 µA, identical values
  record-for-record), and it is flat in CL to the printed 0.01 dB
  (95.99 / 94.10 / 93.79 dB at the three AC corners for every CL). As with
  power, this reflects the RC retune not moving the DC operating point.
- **Power vs `ibias` is unchanged, slew is up ≈ 8 %.** Quiescent power and
  supply current are identical in both records at every corner (the RC
  retune does not move the DC operating point), while the slew control
  values rose (16.15 vs 14.97 V/µs nominal; 15.50 vs 14.51 V/µs at the
  slew-binding corner). The ±20 % relative shape of both sweeps is the same.
