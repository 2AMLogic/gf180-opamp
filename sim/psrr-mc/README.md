# sim/psrr-mc — mismatch-aware PSRR+ / PSRR− (issue #99; follow-up of #61 / DR-6)

The systematic PSRR record (`sim/psrr/`, current record
[`20261010-163854-0127eb7`](../psrr/records/20261010-163854-0127eb7.md)) measures
perfectly matched devices. Decision record 0006 proposes PSRR+ and PSRR− bounds
(≥ 60 dB at the DC plateau and 1 kHz, ≥ 50 dB at 10 kHz) on that systematic
evidence only, and says so. Supply-to-output gain through a differential pair
with a mirror load is exactly the kind of term a mismatch-sized perturbation
changes: Avss changes sign at four 125 °C points of the systematic grid, so
the matched PSRR− there is a cancellation, not margin.

This experiment adds the mismatch-inclusive statistic: a `klt sim`
`monte_carlo` request over the five MOS process corners, N = 300 per corner,
seed 45, at 27 °C / 3.30 V / VCM = VDD/2 — the population of `sim/offset-mc/`
and `sim/cmrr-mc/`, so all three statistics describe the same device draws.

It measures; it judges nothing. The record compares each figure with the DR-6
proposed bounds in a table and states any figure below a bound; it neither
grades nor edits DR-6 or `spec/target-spec.md`.

## Why one deck per sample

`sim/psrr/run_psrr.py` gets Ad, Avdd and Avss from three requests. Under Monte
Carlo all three excitations of one sample must see the same sampled devices,
so here they run in ONE ngspice process (the technique of `sim/cmrr-mc/`):
control commands appended to the `.ac` analysis line run

```
ac (dm: vinp +0.5, vinn -0.5 V AC, rails quiet)        -> ac1
op
alter vcm acmag=0; alter vnac acmag=0; alter vdd acmag=1
ac (vdd: 1 V AC on vdd only)                           -> ac2
op
alter vdd acmag=0; alter vss acmag=1
ac (vss: 1 V AC on the DUT's vss only)                 -> ac3
op
```

then compute, per frequency and from the actual phasors, Ad = vout / vd (ac1)
and Asupply = vout / v(rail) (ac2, ac3) — the systematic driver's definitions —
and print the scalars into the log. The circuit is `sim/psrr/testbench/tb_psrr.spice`
line for line (guarded), with `sw_stat_mismatch` on its single `.param` line.
Per-sample parity is checked, not assumed: the DC output printed after each
supply sweep must agree with the one after the dm sweep to 1 µV (it carries the
sampled offset, σ ≈ 5 mV).

`measurements[].expr` is not used: the batch fleet's runner (klt 0.5.0)
rejects any measurement without a `spice` card (see `sim/cmrr-mc/` probes and
2AMLogic/klayout-tools#3028), so the figures are printed by the tail and read
from each sample's retained log.

## Statistics

For PSRR+ and PSRR−, per corner and frequency (DC plateau 0.1–1 Hz, 1 kHz,
10 kHz, 100 kHz, 1 MHz, and at the differential unity-gain frequency f_u):
mean, σ, skew, empirical min / p1 / p5 of the per-sample PSRR (dB), and

- **linear 3σ**: `mean(Ad dB) − 20 log10(mean|Asupply| + 3σ|Asupply|)` — the
  analogue of the `cmrr-mc` CMRR figure; Asupply's mismatch term is close to
  additive in the linear domain, while the dB image is skewed (Asupply can
  cancel through zero);
- **dB 3σ**: `mean − 3σ` of the per-sample PSRR in dB, for comparison.

**DC definition.** Per sample, DC is the low-frequency plateau value: the
0.1–1 Hz band mean for a flat band (the systematic record's definition), or —
for a near-cancellation knee (below) — the 0.1 Hz value, the f→0 asymptote.
The band mean would misstate a deep knee by up to ~10 dB.

**Numerical floor and near-cancellation.** |Asupply| below 1.45e-10 V/V (10×
the superposition residual demonstrated by the systematic record) is clamped
elementwise in the deck, so PSRR is never a division by zero; a clamped figure
is a lower bound (`(>=)` in the tables) and the record counts the samples that
hit the floor. The record also counts, per corner, the samples whose Avdd/Avss
plateau sign is opposite to the matched device's (switch-off control): there
the mismatch term exceeds and cancels the systematic one.

**Near-cancellation knees.** A draw whose supply term cancels the systematic
one leaves the residual to the reactive path through the DUT's dominant pole:
|Asupply| ≈ sqrt(Re² + (k·f)²) — flat below the corner f_c = |Re|/k, rising
toward +20 dB/dec above it. With f_c inside or near the 0.1–1 Hz band the
band has no plateau, by physics: no fixed dB tolerance covers it (f_c = 2.4 Hz
alone gives ~1 dB of in-band swing), while every bench-quality check passes
and the figure sits orders of magnitude above the numerical floor. The deck
therefore prints the clamped 0.1–1 Hz |Asupply| band profile per rail, and the
DC-band validity rule is: **flat to 0.1 dB, or a near-cancellation knee** — a
band profile that is monotonically non-decreasing (rising, single-knee,
minimum at the 0.1 Hz edge). Anything falling or humped has no mechanism in
this bench and blocks the run. Knee samples are flagged (`knee_rails` in
`samples.csv`, counted per corner in the record) and report the 0.1 Hz
asymptote as their DC figure. Diagnosed after the first full-grid attempt
(PR #151) was blocked by its 0.1 dB plateau gate on 4 such draws and, by
design, wrote no record: the 900-draw diagnostic (batch job
`klt-sim-ba5466061590`) showed the signature — a 7.7 dB monotone rise at
|Avdd| = 1.9e-3 V/V, 1.3e7× the floor, with parity, Ad plateau and the other
rail perfectly flat.

Temperature and supply are NOT sampled under mismatch (the stated population);
the systematic 45-point grid covers them for matched devices, and the four
Avss sign-flip points are all at 125 °C. A T/VDD mismatch grid is out of scope
here (file separately if the 27 °C result is marginal; #98 is the CMRR
counterpart).

## Controls (all in the record, blocking)

- **switch-off** (`sw_stat_mismatch = 0`, 4 samples per corner): spread exactly
  zero and PSRR+ / PSRR− equal to the committed systematic record
  `20261010-163854-0127eb7` (DC and spots within 0.01 dB on identical grid
  samples; f_u within 0.5 % and PSRR at f_u within 0.1 dB, the `meas`
  linear-in-f vs log-f interpolation difference). Verified locally on a single
  unit at typical: max deviation 5e-5 dB.
- **process-only** (`vary: "process"`, typical, 20 samples): recorded as
  measured; not a σ = 0 control with this PDK deck (klt feeds ngspice one
  combined seed, 2AMLogic/klayout-tools#2937).
- **negative control: mirror imbalance** (XM3 W 6u → 5.4u in a DUT copy, the
  same mismatch Monte Carlo at typical, N = 300): for **both** PSRR+ and PSRR−,
  mean |Asupply| at DC must rise by more than 3 standard errors of the
  difference and the linear 3σ PSRR must fall.

  *Why this direction.* A load-mirror imbalance adds a systematic Asupply term;
  whether |Asupply| rises or falls depends on that term's sign relative to the
  matched one. Local matched-device single units (typical / 27 °C / 3.30 V,
  `sw_stat_mismatch = 0`) give:

  | DUT | Avdd at 0.1 Hz (V/V) | Avss at 0.1 Hz (V/V) |
  |---|---|---|
  | committed (XM3 W 6u) | +0.579 | −0.164 |
  | XM3 W 5.4u (−10 %, the control) | +1.182 | −1.854 |
  | XM3 W 6.6u (+10 %) | +0.038 | +1.379 |

  W −10 % adds a term of the same sign as the matched one on BOTH rails, so
  |Avdd| and |Avss| both rise and both PSRR figures fall; with W +10 % Avdd
  would partly cancel (PSRR+ rises) and Avss would flip sign — the opposite
  direction is not a degradation on PSRR+. Under mismatch the expected |Asupply|
  grows with the magnitude of the systematic term, so the −10 % copy is the
  control that must visibly degrade the statistic on both rails.
- **per sample**: dm excitation (vd = 1, vc = 0, rails quiet to 1e-6 V); each
  supply sweep (driven rail = 1 V, other rail quiet to 1e-6 V, inputs quiet to
  1e-9 V, input-leak term |Ad·vd| < 1e-6 of the output); dm/vdd/vss parity
  (1 µV); DC band of Ad, PSRR± and |Asupply| flat to 0.1 dB or a
  near-cancellation knee (monotonically non-decreasing band profile, above;
  anything else blocks); `op` near VCM (vinp = VCM, |vout − VCM| ≤ 0.1 V);
  f_u inside the sweep.
- **per point**: exactly N distinct sample indices, all valid; offset σ > 1 mV
  (mismatch visibly acts; klt's `family_mismatch` reports `active: null` for an
  `.include`d DUT, 2AMLogic/klayout-tools#2928).

Any failure writes no record.

## Running

```bash
python3 sim/psrr-mc/test_psrr_mc.py               # offline tests (+ 1 local unit)
python3 sim/psrr-mc/run_psrr_mc.py --smoke         # one local unit, no record
python3 sim/psrr-mc/run_psrr_mc.py --probe \
        --batch-runner-version-check warn          # N=3 AC+MC probe (batch)
python3 sim/psrr-mc/run_psrr_mc.py --batch-runner-version-check warn \
        --batch-submit-retries 10                  # full run + record (append-only)
```

On a dispatch worker the Monte Carlo requests go to the Spot batch fleet
(`$KLT_SIM_BACKEND=batch`); the job ids are in the record. If a submit fails
the driver stops and writes no record; it never falls back to a local grid.

## Evidence layout (append-only)

```
records/<rid>.md                         statistics, DR-6 comparison, controls, fingerprint
corners/<rid>/samples.csv                one row per sample incl. all three seeds
corners/<rid>/klt-report.*.json          sanitised klt reports (grid + controls)
corners/<rid>/controls/*.csv             control samples
netlist-snapshots/<rid>.spice            DUT + testbench + requests
probes/<rid>.{md,json}                   capability probes
```

`measurement_config.py` is the single source of the fingerprinted
configuration (issue #89 convention): the bench, the three-sweep tail, the
dm drive, the figure definitions and floor, the Monte Carlo request, the
control settings and the corner/model axes. `sim/test_measurement_config.py`
fails when a configuration constant does not move the fingerprint.
