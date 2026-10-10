# `sim/offset-mc/` — input-offset mismatch Monte Carlo

Statistical-basis evidence for the **offset** row of `spec/target-spec.md`
(issue #45; supplies the mismatch evidence DR-3 residual (e2) and
`design/opamp_sizing.md` §8 name as missing). It measures the static
input-referred offset of the **committed sized schematic** over the five MOS
process corners with per-instance device mismatch sampled, and reports mean,
sigma, 3 sigma and the worst corner. **It proposes no numeric offset bound**;
ratifying one is a separate decision.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, the xschem
  export, instantiated as `Xdut vdd 0 vinp vout vout ibias opamp_two_stage`.
  The testbench declares **no transistor** and the committed netlist is never
  modified (the imbalance control perturbs an in-memory *copy*). The export is
  read through `subckt_from_export()` in `design/check_dc_op.py`, as in
  `sim/gain-gbw-pm/`.
- **Technique**: unity-gain follower (`vinn` tied to `vout`), `vinp` at
  VCM = 1.65 V, DC. Offset = `vout − vinp`, taken from ngspice's own
  `.meas dc vos_v FIND par('v(vout)-v(vinp)')` (full precision); `vout_v` and
  `vinp_v` are measured too and cross-checked (ngspice prints those to 6
  significant digits, so they agree with `vos_v` only to ±1e-5 V). The follower
  error equals the input-referred offset times A/(1+A), a ~0.1 % factor at
  ≥ 60 dB gain, negligible here.
- **The mean includes the systematic offset** (sizing §4 balance condition);
  the **sigma is the mismatch distribution**. They are separated in the record:
  the systematic term is also measured directly with mismatch switched off.
- **Conditions** (issue scope): five MOS corners `typical/ff/ss/fs/sf` ×
  27 °C × 3.30 V, `res_typical` + `mimcap_typical` passives on every corner
  (RZ/CC spread is not sampled). No T/VDD axes.
- **Statistics**: `monte_carlo = {n: 300, seed: 45, vary: "mismatch"}` per
  corner, 5 × 300 = **1500 units**, one `klt sim` request. Corner-to-corner
  *global* spread is covered by the five corners; `sw_stat_global` stays 0.
  Sample sigma uses n−1; at N = 300 its relative standard error is ≈ 4 %.
  Skew and excess kurtosis are recorded per corner as a normality sanity check.

## PDK mismatch-model audit (gf180mcuD, open_pdks `c6d73a35…`)

- Mismatch is gated by the model-level parameter **`sw_stat_mismatch`**
  (default 0 in `design.ngspice`), with global statistics gated separately by
  `sw_stat_global`. There is no `_mm` corner section: every MOS corner section
  pulls in the same `fets_mm` library.
- The **plain `nfet_03v3` / `pfet_03v3` subcircuits** that the committed
  netlist instantiates (`sm141064.ngspice`, section `fets_mm`) *do* carry the
  mismatch hook: `m0 … delvto='mis_vth*sw_stat_mismatch'
  mulu0='1-mis_k*sw_stat_mismatch'`, with Pelgrom-style
  `mis_vth = agauss(0, 0.7071·par_vth·1e-6/√(Leff·Weff), 1)` and the same form
  for `mis_k` (`par_vth = 0.007148`, `par_k = 0.007008`). So **no device-flavor
  change is needed** (CLAUDE.md forbids mixing flavors without a record).
- The testbench turns it on with `.param sw_stat_mismatch=1` *after* the
  `design.ngspice` include (the PDK default 0 would otherwise win). The source
  guard enforces both the line and its ordering, and that `sw_stat_global` is
  untouched.
- **Evidence the committed devices respond**: with the switch on, a
  10-sample batch probe (typical) gave offsets of several mV; the full run's
  per-corner sigma is 4.3–5.0 mV; with the switch off, a 40-sample Monte Carlo
  gives sigma = **0.000000 mV** and a single deterministic unit gives
  −0.014 mV (see the record's Controls). The result is consistent with a hand
  Pelgrom estimate (≈ 4 mV for the input pair plus the mirror).
- **Resistors have no mismatch** in this deck (`mis_r` is hard-coded 0; the
  sigma formula ships commented out) and the MIM cap has none; only the MOS
  family is sampled. `RZ`/`CC` mismatch is therefore *not* represented.
- `klt sim`'s `family_mismatch` report says `active: null` here: it scans only
  the top-level netlist (the testbench), and the transistors live in the
  `.include`d DUT (known klt gap, 2AMLogic/klayout-tools#2928, reproduced
  there). The response is established by the measured sigma instead.

## Results

Latest record: [`records/20261009-072205-96bf3cc.md`](records/20261009-072205-96bf3cc.md)
(mV, offset = `vout − vinp`; per-sample data and seeds in
`corners/20261009-072205-96bf3cc/offset_samples.csv`).

| corner | mean | sigma | 3 sigma | mean−3s | mean+3s |
|---|---|---|---|---|---|
| typical | +0.047 | 4.902 | 14.707 | −14.659 | +14.754 |
| ff | −0.308 | 4.354 | 13.062 | −13.370 | +12.754 |
| ss | +0.299 | 4.434 | 13.303 | −13.004 | +13.602 |
| fs | −0.120 | 4.340 | 13.021 | −13.141 | +12.901 |
| sf | −0.619 | 5.006 | 15.017 | −15.635 | +14.398 |

**Worst corner: `sf`** — by sigma (5.006 mV) and by |mean| + 3 sigma
(15.635 mV). Systematic offset (typical, mismatch off) is only −0.014 mV: the
offset is dominated by the mismatch spread, not by the §4 balance condition.
These are measurements, not verdicts: no bound is judged.

## Controls and findings

- **Switch off** (`sw_stat_mismatch=0`): Monte Carlo sigma = 0 exactly, mean
  = the deterministic systematic offset. This is the deterministic negative
  control for the mismatch path.
- **Imbalance**: M1 `W=3.6u → 3.96u` in a snapshot copy (mismatch off) shifts
  the offset from −0.014 mV to −8.484 mV. `test_offset_mc.py` asserts the shift
  exceeds 1 mV (it runs two single local deterministic units).
- **`vary: "process"` is NOT a sigma = 0 control with this deck.** The issue
  expected it to hold the mismatch path fixed. klt reports a constant
  `mismatch_seed`, but ngspice receives one combined `.options seed` that is
  derived from `(process_seed, mismatch_seed)` and changes with the process
  seed; this deck draws mismatch from ngspice's RNG (`agauss`), so the
  per-instance values still change (measured sigma ≈ 5.6 mV at N = 40,
  recorded as measured, not asserted). The switch-off control above replaces
  it. Filed generically as 2AMLogic/klayout-tools#2937.

## Execution

One `klt sim` request on the batch fleet (`$KLT_SIM_BACKEND=batch`; the job id
is in the record). The fleet runner is klt 0.5.0 against client 0.7.0, so the
run uses `--batch-runner-version-check warn`; the client expands the Monte
Carlo units and ships explicit per-sample seeds, so the spread is the proof
the runner honoured them. Only `.meas dc` cards (no `expr`) are used for that
reason. 1500 units took ≈ 524 s wall; the two 40-sample controls are separate
small requests. No ngspice grid is hand-launched here; if the batch submit
fails the driver stops and writes **no record** (the result is *not run*, there
is no local fallback). The raw fleet report of a clean grid is also kept in
`$TMPDIR` so a post-processing failure never wastes a completed grid.

## PVT grid (`--grid full`, issue #106)

`run_offset_mc.py --grid full` extends the same mismatch Monte Carlo to the
45-point MOS × T × VDD grid of the other benches (T −40 / 27 / 125 °C, VDD
2.97 / 3.30 / 3.63 V from `sim/gain-gbw-pm/measurement_config.py`, VCM =
VDD/2 paired by index) with N = 300 per point: ONE `klt sim` request of
45 × 300 = 13 500 units, executed wherever `klt` resolves the backend (the
batch fleet on a dispatch worker; the job id is the record's
`environment.remote`). Nothing is hand-launched and a failed submit writes no
record. Every point goes through the same per-sample validation as the
nominal run, with `vinp` checked against that point's own VDD/2 (which also
proves the supply/VCM alters were applied); klt's per-point rollup is
cross-checked; the same controls run at typical / 27 °C / 3.30 V.

The grid record (title `# Offset Monte Carlo PVT grid record`) is a separate
append-only record: mean, sigma, 3 sigma and the linear 3-sigma offset
|mean| + 3 sigma per point, a T × VDD summary of the worst corner, the worst
point, and the worst figure set next to the 27 °C / 3.30 V record's
(`20261009-072205-96bf3cc`, recomputed from its committed samples). It
proposes no bound (spec issue #62 decides that). Its measurement fingerprint
covers the full-grid axes (`"grid": "full"` in the inputs); the nominal
fingerprint is unchanged. The characterization report still cites the 27 °C
record for the offset row (the grid record is listed as a side study in
`sim/report/characterization_report.py`).

## Running

```bash
python3 sim/offset-mc/test_offset_mc.py        # offline tests (+ 2 local deterministic units)
python3 sim/offset-mc/run_offset_mc.py --smoke # one deterministic local unit, no record
python3 sim/offset-mc/run_offset_mc.py --batch-runner-version-check warn \
        --batch-submit-retries 10             # 5 corners at 27 C / 3.30 V + record (append-only)
python3 sim/offset-mc/run_offset_mc.py --grid full --batch-runner-version-check warn \
        --batch-submit-retries 10             # 45-point PVT grid + record (append-only)
```

Needs `klt`, `ngspice` and the pinned gf180mcu PDK (`find_pdk` in
`sim/harness.py`); the driver and tests are stdlib-only (no numpy).
`sim/harness.py` now imports numpy lazily (only `run_corner` needs it) so this
experiment runs on a bare Python.

## Evidence layout (append-only)

```
records/<rid>.md                       statistics, controls, validation
corners/<rid>/offset_samples.csv       one row per sample incl. all three seeds
corners/<rid>/klt-report.json          sanitised klt report of the grid
corners/<rid>/controls/*.json          control reports
netlist-snapshots/<rid>.spice          DUT + testbench + request (+ the one perturbed line)
```

A re-run mints a new record id; existing paths are never overwritten.

## Limitations

- The default run is nominal temperature and supply only (issue #45 scope);
  the T/VDD axes are covered by `--grid full` (issue #106, above).
- MOS mismatch only (see audit); passive (RZ/CC) mismatch and global process
  spread beyond the five corners are not sampled.
- Static follower offset; it says nothing about offset drift, CMRR/PSRR-induced
  offset or input-referred noise.
