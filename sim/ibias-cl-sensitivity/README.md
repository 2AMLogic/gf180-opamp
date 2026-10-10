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
`gain-gbw-pm/records/20261010-020141-1e51d1c.md` and
`slew-swing-power/records/20261009-142137-1dab1db.md`:

- nominal: typical / 27 °C / 3.30 V
- PM-binding: fs / 125 °C / 2.97 V (57.34° worst)
- GBW-binding and slew-binding: ss / 125 °C / 2.97 V (10.422 MHz, 14.51 V/µs)
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
