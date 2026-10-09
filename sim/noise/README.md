# `sim/noise/` — input-referred noise

Measurement evidence for the **input-referred noise** row of
`spec/target-spec.md` (issue #46; supplies what DR-3 residual (e1) names as
missing: an AC/noise analysis on the committed design and data to choose the
integration band). **It issues no pass or fail verdict and proposes no bound or
band** — the row's bound is not ratified. `spec/target-spec.md` is untouched.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, the xschem
  export, instantiated as `opamp_two_stage`; the testbench declares no
  transistor (same DUT/testbench guards as `sim/gain-gbw-pm/`, reused from that
  driver).
- **Arrangement**: identical to the gain bench — `Lfb` closes the loop at DC
  and is an AC open, `Cfb` AC-grounds `vinn`, `CL` = 2 pF, `Ibias` = 10 µA,
  vcm = VDD/2. The AC source on `vinp` is therefore the open-loop amplifier's
  differential input, and ngspice's `.noise v(vout) Vcm` input-referred density
  is the **differential-input-referred** noise. The input reference is
  *verified*, not assumed: `20 log10(onoise/inoise)` equals the gain bench's
  committed `|vout/vdiff|` at every sweep point of every grid point
  (deviation 0.0000 dB), and a feedback-isolation study (Lfb = Cfb of 1e8 and
  1e10) moves nothing.
- **Conditions**: the ratified 45-point grid (5 MOS corners × −40/27/125 °C ×
  2.97/3.30/3.63 V), `res_typical` + `mimcap_typical` on every corner (RZ/CC
  passive spread is not swept). One `klt sim` request.
- **Sweep**: `.noise dec 20` from 0.1 Hz to 10 MHz (161 points). 0.1 Hz so the
  1 Hz band edge and the 1/f fit have margin; 10 MHz so the thermal floor is
  visible above the 1/f corner (≈ 0.2–0.5 MHz).
- **Reported per point**: input-referred density at 10 Hz, 100 Hz, 1 kHz,
  10 kHz, 100 kHz; integrated rms over 100 Hz–1 MHz (the `sg13g2-opamp` twin
  precedent in DR-3 (e1)) and three alternatives (10 Hz–100 kHz,
  100 Hz–100 kHz, 1 Hz–10 kHz); thermal floor and 1/f corner.

## Extraction

- **Integration**: exact piecewise power-law integration of density² between
  sweep points (log-log linear interpolation; exact for flat and for any 1/f^a
  segment, so independent of the sweep density). Band edges between sweep
  points are log-log interpolated.
- **Thermal floor and 1/f corner**: least-squares fit of `S(f) = Sw + K/f^a`
  to density² over 1 Hz–10 MHz, relative-error weights, `a` scanned 0.5–1.5
  with linear least squares for `Sw`, `K`. Floor = `sqrt(Sw)`; corner =
  `(K/Sw)^(1/a)` (1/f power equals thermal power). A fit is reported only
  when its rms relative residual is ≤ 5 % (observed 1.8–2.9 %; the n- and
  p-channel exponents differ, `ef` 0.95 vs 1.12, so a single `a` is
  approximate).

## Validation (blocking: a failed check writes no record)

| check | tolerance |
|---|---|
| sweep complete, strictly increasing, finite, positive, ≥ 20 pts/decade | exact |
| our integral of `onoise_spectrum` vs ngspice `onoise_total` (printed in the log) | 0.5 % (observed < 0.001 %) |
| `onoise/inoise` vs the gain bench's committed `|vout/vdiff|`, all 45 points | 0.05 dB (observed 0.0000) |
| ngspice `inoise_total` vs our integral, 20 pts/dec | 8 % (observed −5.4 … −4.9 %) |
| dense control (200 pts/dec, local nominal unit): primary-band rms vs the grid | 0.5 % (observed −0.006 %) |
| dense control: ngspice `inoise_total` vs our integral | 1 % (observed −0.52 %) |
| isolation study Lfb = Cfb ∈ {1e8, 1e10}: spots and primary-band rms | 0.5 % (observed 0.000 %) |

**ngspice `inoise_total` is not the reference for the input-referred
integral.** It approaches ours only when the sweep is dense (−0.5 % at 200 pts/dec): at
20 pts/dec it reads ~5 % low and converges toward our value as the sweep is
refined (probe: 3.291e-5 → 3.167e-5 → 3.145e-5 V at 20/100/400 pts/dec over
100 Hz–10 kHz, against ours 3.138e-5 V at every density). The *output* total
agrees with our integral to better than 0.001 % at 20 pts/dec, which validates
the integrator. The density is the primary result; band rms is derived from it.

## Flicker-model audit (pinned PDK)

The record repeats this audit, generated from the installed library. For
gf180mcuD, open_pdks `c6d73a35…`, `sm141064.ngspice`:

- `fnoimod = 1` (BSIM4 unified flicker model) on all `nfet_03v3` and
  `pfet_03v3` model cards (96 each); `ef` = 0.95 (n), 1.12 (p);
  `noia/noib/noic` bound to `nfet_03v3_noia/…` and `pfet_03v3_noia/…`.
- The parameters are defined in `.lib noise_corner`, which each of the
  `typical`, `ff`, `ss`, `fs`, `sf` sections pulls in; `fnoicor = 0` in
  `design.ngspice` (as-extracted; 1 would be worst-case flicker).
- **Data confirms it**: density rises ∝ f^−0.5 below the corner (13 µV/√Hz at
  1 Hz → 4.3 µV/√Hz at 10 Hz at the nominal point) and the fitted 1/f power
  exceeds the thermal floor by > 10⁵ at 1 Hz at all 45 points. The result is
  **not thermal-only**, so DR-2 §(c)'s caveat does not apply to it.

## Execution

One `klt sim` request on the batch fleet (`$KLT_SIM_BACKEND=batch`; the job id
is in the record). The fleet runner is klt 0.5.0 against client 0.7.0, so the
run uses `--batch-runner-version-check warn`. The first submit without it was
refused by the runner version gate (no simulation ran). No ngspice grid is
hand-launched; if the batch submit fails the driver stops and writes no record.
The controls (three nominal single units) run locally.

klt has no `.meas noise` path and its rawfile `write` dumps only the current
plot (the integrated-total plot after `.noise`). The request therefore appends
`print noise2.inoise_total noise2.onoise_total` and `setplot noise1` to
`analysis.args`; klt places those lines verbatim in its `.control` block, so
the totals reach the retained log and the spectrum reaches the rawfile. This
works on the 0.5.0 fleet runner. Related generic tool gaps are filed as
2AMLogic/klayout-tools#2938 and #2893.

## Running

```bash
python3 sim/noise/test_noise.py              # offline tests (+ 2 local checks)
python3 sim/noise/run_noise.py --smoke       # nominal point, local, no record
python3 sim/noise/run_noise.py --batch-runner-version-check warn \
        --batch-submit-retries 10            # full grid + record (append-only)
```

Needs `klt`, `ngspice`, the pinned gf180mcu PDK (`find_pdk` in
`sim/harness.py`), Python 3 with `numpy` and `matplotlib`, and the committed
gain-bench dataset (`sim/gain-gbw-pm/corners/`, used for the input-reference
check).

## Evidence layout (append-only)

```
records/<rid>.md                  claim, flicker audit, spots, band rms, floor/corner, validation
records/<rid>-plots/*.png         density overlay, band scatter
corners/<rid>/<point>.{dat,log,cir}   per point: freq inoise onoise; ngspice log with totals; klt deck
corners/<rid>/klt-report.json     sanitised klt report of the grid
corners/<rid>/controls/           dense and isolation single units
netlist-snapshots/<rid>.spice     DUT + testbench + request + generated deck
```
