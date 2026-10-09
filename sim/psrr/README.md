# `sim/psrr/` — power-supply rejection ratio (PSRR+ / PSRR−)

Measurement evidence for the **PSRR** row of `spec/target-spec.md` (issue #39;
supplies what DR-3 residual (e4) names as missing: a supply-injection AC
testbench on the committed schematic). **It issues no pass or fail verdict
and proposes no bound.** The row's bound is not ratified. `spec/target-spec.md`
and the decision records are untouched.

## Definitions and references

- **Input-referred** PSRR+ = 20 log10 |Ad / Avdd| and
  PSRR− = 20 log10 |Ad / Avss|, where Asupply = vout / v(rail) uses the
  actual rail phasor. Ad comes from the same bench's differential run. The
  **output supply feedthrough** −20 log10 |Asupply| is a different quantity.
  The record reports it separately and labels it as such.
- One rail is driven with 1 V AC at a time. The other rail and both inputs
  are AC-quiet.
- The DUT's **`vss` port is a driven node** (`Vss vss 0 dc 0 ac {acss}`), not
  simulator node 0. PSRR− therefore moves only the DUT's ground rail.
- The inputs (`Vcm`, the DC servo), the load `CL` and `Vdd` are referenced to
  **simulator ground**. They stay still while the DUT's vss moves. The guard
  rejects a bench that references any of them to `vss`.
- **Bias-generator rejection is excluded.** The ideal 10 µA `Ibias` (from vdd
  into `ibias`) carries zero small-signal current. An on-chip reference
  would add its own supply sensitivity.
- Matched devices: the figures are systematic, not mismatch-limited.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, instantiated
  as `opamp_two_stage`. The testbench declares no transistor.
- **Bench**: the CMRR bench's DC servo (`sim/cmrr/README.md`), with
  `Xdut vdd vss vinp vinn vout ibias`. It sits at the unity-buffer DC point,
  the loop is open at every swept frequency, and vout is unloaded.
- **Three excitations**, each one `klt sim` corner request over the ratified
  45-point grid: `dm` (±0.5 V on the inputs), `vdd` (1 V on vdd) and `vss`
  (1 V on vss).
- **Reported per point**: PSRR+ and PSRR− at "DC" (the verified 0.1–1 Hz
  plateau), 1 kHz, 10 kHz, 100 kHz, 1 MHz and at f_u. Also reported: the
  worst-case corner of each figure, Ad, Avdd and Avss at the plateau, and the
  curves.

Extraction, the numerical floor, interpolation, the near-cancellation flag
and the operating-point checks are shared with `sim/cmrr/run_cmrr.py` and
described in its README.

## Validation (blocking: any failure writes no record, exit 2)

| check | criterion |
|---|---|
| grid completeness | 45/45 points per excitation, rawfile + log retained, identical axes, finite data |
| `dm` run | vd = 1 V, vc = 0, both rails quiet, all within 1e-6 V |
| supply runs | driven rail = 1 V and the other rail quiet within 1e-6 V; both inputs quiet within 1e-9 V; input-leak term \|Ad·vd\| ≤ 1e-6 of the output |
| operating point | as in `sim/cmrr/`, for all three excitations |
| Ad vs the gain bench | within 0.05 dB from 0.1 Hz to f_u. The gain bench measures Ad + Acm/2, and Acm/2 is negligible below f_u; the CMRR record checks the exact sum over the whole sweep |
| local vs grid | the local nominal triple reproduces the grid's nominal point within 0.05 dB |
| servo isolation | tau = 1e16 and 1e20 s move PSRR± ≤ 0.01 dB; tau = 1e-3 s must be rejected |
| negative controls | stimulus fixture `Rft vdd vout 100k` must lower PSRR+ by ≥ 6 dB; `Rft vss vout 100k` must lower PSRR− by ≥ 6 dB |

The feedthrough fixtures are lines appended to the bench for the control
only. All three excitations of a control carry the line, so Ad and Asupply
are measured on the same circuit. The production DUT and the baseline bench
are unchanged, and the fixtures are recorded in
`netlist-snapshots/<rid>-controls.spice`.

## Running

```bash
python3 sim/psrr/test_psrr.py            # offline tests (+ 1 local nominal triple)
python3 sim/psrr/run_psrr.py --smoke     # nominal point, local, no record
python3 sim/psrr/run_psrr.py --batch-runner-version-check warn \
        --batch-submit-retries 10        # full grid + record (append-only)
```

Prerequisites, execution policy (fleet only for the grid, no local fallback)
and environment notes (`~/.spiceinit`, `models.pdk_root`, klt's client-side
PDK provenance, `python3 -s`) are the same as in `sim/cmrr/README.md`.

## Evidence layout (append-only)

```
records/<rid>.md                             claim, references, worst cases, all points, method, studies, controls
records/<rid>-plots/*.png                    PSRR+ and PSRR- of all 45 points; nominal transfers
corners/<rid>/<point>.<dm|vdd|vss>.dat       actual vout, vinp, vinn, vdd, vss phasors (re/im)
corners/<rid>/<point>.<dm|vdd|vss>.{log,cir} ngspice log (with the printed operating point), klt deck
corners/<rid>/<point>.psrr.dat               freq, |Ad|, |Avdd|, |Avss|, PSRR+, PSRR- (dB)
corners/<rid>/klt-report.<mode>.json         sanitised klt reports
corners/<rid>/controls/                      local studies and controls
netlist-snapshots/<rid>.spice                DUT + testbench + the three requests + generated deck
netlist-snapshots/<rid>-controls.spice       the fixture controls, separately
```
