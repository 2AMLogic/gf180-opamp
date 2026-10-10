# sim/cmrr-mc — mismatch-aware CMRR (issue #61; DR-3 residual (e3))

The systematic CMRR record (`sim/cmrr/`) measures perfectly matched devices, so
its worst DC plateau (95.42 dB) is an optimistic upper bound. This experiment
adds the mismatch-inclusive statistic: a `klt sim` `monte_carlo` request over
the five MOS process corners, N = 300 per corner, seed 45, at 27 °C / 3.30 V /
VCM = VDD/2 — the same axes and seed as `sim/offset-mc/`, so the two
statistics describe the same device draws.

It measures; it judges nothing. Bounds are proposed in
[`spec/decision-records/0006-cmrr-psrr-bounds.md`](../../spec/decision-records/0006-cmrr-psrr-bounds.md).

## Why one deck per sample

`sim/cmrr/run_cmrr.py` gets Ad and Acm from two requests (a differential and a
common-mode excitation). Under Monte Carlo both excitations of one sample must
see the same sampled devices, so here both run in ONE ngspice process: control
commands appended to the `.ac` analysis line run the differential sweep, an
`op`, `alter` the two AC sources to the common-mode drive, the second sweep and
a second `op`, then solve `vout = Ad·vd + Acm·vc` per frequency from the actual
input phasors of both sweeps (the systematic driver's solve) and print the
scalars into the log. Per-sample parity is checked, not assumed: the DC output
printed after each sweep carries the sampled offset and must agree to 1 µV.

`measurements[].expr` is not used: the batch fleet's runner (klt 0.5.0)
rejects any measurement without a `spice` card
(probes `20261009-233828-95dfc2a`, failed job; `20261009-234034-95dfc2a`,
3/3 valid distinct samples, `environment.monte_carlo` populated).

## Statistics

Per grid point and frequency (DC plateau 0.1–1 Hz, 1 kHz, 10 kHz, 100 kHz,
1 MHz, and the differential unity-gain frequency f_u): mean, σ, skew,
empirical min / p1 / p5 of the per-sample CMRR (dB), and two 3σ figures:

- **linear 3σ**: `mean(Ad dB) − 20 log10(mean|Acm| + 3σ|Acm|)` — the figure the
  `sg13g2-opamp` twin ratified (its DR-0004). Acm is close to additive and
  zero-mean in the linear domain; the dB image is skewed (Acm can cancel).
- **dB 3σ**: `mean − 3σ` of the per-sample CMRR in dB (the issue's proposal),
  shown for comparison.

Temperature and supply are NOT sampled under mismatch (the stated population);
the systematic 45-point grid covers them for matched devices. `--grid full`
runs the 45-point T/VDD grid under mismatch (13 500 units).

## Controls (all in the record, blocking)

- **switch-off** (`sw_stat_mismatch = 0`): spread exactly zero and values equal
  to the committed systematic record `20261009-105631-30ec86d`.
- **process-only** (`vary: "process"`): recorded as measured; not a σ = 0
  control with this PDK deck (2AMLogic/klayout-tools#2937).
- **mirror-imbalance** (XM3 W −10 % in a DUT copy, same Monte Carlo): mean |Acm|
  must rise by > 3 standard errors and both 3σ figures must fall.
- per-sample: excitation within 1e-6 V, dm/cm parity, flat DC plateau, `op`
  near VCM; per-point: exactly N distinct samples; offset σ > 1 mV (mismatch
  visibly acts; klt's `family_mismatch` reports `active: null` for an
  `.include`d DUT, 2AMLogic/klayout-tools#2928).

## Running

```bash
python3 sim/cmrr-mc/test_cmrr_mc.py               # offline tests (+ 1 local unit)
python3 sim/cmrr-mc/run_cmrr_mc.py --smoke         # one local unit, no record
python3 sim/cmrr-mc/run_cmrr_mc.py --probe         # N=3 AC+MC probe (batch)
python3 sim/cmrr-mc/run_cmrr_mc.py --batch-runner-version-check warn \
        --batch-submit-retries 10                  # full run + record (append-only)
```

On a dispatch worker the Monte Carlo requests go to the Spot batch fleet
(`$KLT_SIM_BACKEND=batch`); the job ids are in the record. If a submit fails
the driver stops and writes no record; it never falls back to a local grid.

## Evidence layout (append-only)

```
records/<rid>.md                         statistics, controls, validation
corners/<rid>/samples.csv                one row per sample incl. all three seeds
corners/<rid>/klt-report.*.json          sanitised klt reports (grid + controls)
corners/<rid>/controls/*.csv             control samples
netlist-snapshots/<rid>.spice            DUT + testbench + requests
probes/<rid>.{md,json}                   capability probes
```
