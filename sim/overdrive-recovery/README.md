# `sim/overdrive-recovery/` — input-overdrive / output-saturation recovery

**Evidence only; no spec row, no verdict.** This experiment drives the
unity-gain follower — the configuration the error-amp slots in `gf180-ldo` /
`gf180-bandgap` (`spec/target-spec.md` §5) actually use — so hard that the
output clips at its output-stage limit (VDD − dropout high, the M7-sink limit
low), removes the overdrive, and measures how long the output takes to return
to within **±1 % of its linear value**, plus an explicit **latch check**. It
exists because nothing else in `sim/` exercises large-signal overload: the
step-response experiment is deliberately small-signal, and the slew bench
measures the edge, not what happens after the amplifier has been pinned
against its limit. For a two-stage Miller amplifier this is where the
compensation node and the second stage can recover slowly — and where
second-stage headroom at SS / low-VDD makes the behaviour corner-sensitive.

`spec/target-spec.md` has **no overload-recovery row**. This experiment
proposes no bound and judges nothing against one; it records the numbers,
names the worst points and states explicitly whether any point latched.
Adding a recovery row would need a `spec/` decision record (a possible
follow-up, not part of this experiment). Nothing in `spec/` is touched here.

The layout mirrors [`../step-response/`](../step-response/README.md):
`testbench/`, a `run_*.py` driver on `sim/harness.py`, a stdlib
`measurement_config.py` (fingerprint), `records/`, `netlist-snapshots/`,
`corners/`, and a no-simulator unit-test file.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, included
  as-is through the shared wrapper normalisation (`harness.normalize_dut_text`,
  the same one `design/check_dc_op.py` uses) and
  instantiated as `Xdut vdd 0 vinp vout vout ibias opamp_two_stage`
  (unity-gain follower, `vinn = vout`). **The testbench declares no
  transistor.** The bench and DUT guards are the gain driver's (one copy):
  the bench must include the design-derived DUT, have exactly one `Xdut`, be a
  circuit body; the materialised DUT must contain every line of the committed
  export unchanged and in order (a stale, resized or hand-edited DUT is
  rejected before anything is simulated).
- **Load**: `CL = 2 pF` [DR-1] on `vout`, nothing else — identical to the
  step and slew benches and to the CL of the GBW/PM record.
- **Bias**: 10 µA into `ibias`.
- **Common mode**: VCM **tracks VDD/2** (1.485 / 1.65 / 1.815 V at 2.97 /
  3.30 / 3.63 V), the convention of every other experiment in `sim/`. The
  input rests at VCM between overdrives; the linear (pre-overdrive) output
  level is the recovery reference, so the follower's systematic offset does
  not enter.
- **Overdrive stimulus** (fixed volts, series construction like the step
  bench; the pulse sources `Vup`, `Vdn` carry the signal node on their `n+`
  terminal so the swept `Vcm` stays a plain DC source):

  | event | time | input | output |
  |---|---|---|---|
  | rest | 0 – 400 ns | VCM | settled at the linear value `v_ref` |
  | overdrive high | 400 ns – 1.0 µs | VCM + 1.85 V (= VDD + 0.035…0.365 V) | slews to and clips at the **positive** limit |
  | removal (recovery high) | 1.0 µs | back to VCM | recovery window 1.0 – 2.6 µs |
  | overdrive low | 2.6 – 3.2 µs | VCM − 1.90 V (below ground at every supply) | slews to and clips at the **negative** limit |
  | removal (recovery low) | 3.2 µs | back to VCM | recovery window 3.2 µs – end (4.9 µs) |

- **Saturation requirement**: across the last 50 ns of each 600 ns hold the
  output must have travelled ≥ 0.5 V in the overdriven direction **and** hold
  flat within 5 mV — pinned at its output-stage limit, not still slewing.
  A point that fails this is **INVALID**, never a fast recovery. (The
  negative clip limit sits ≈ 0.54 V above ground at nominal — the M7 sink
  cannot reach 0 V — so a fraction-of-rail test would be meaningless on the
  low side; travel + flatness is corner-robust. The 600 ns hold is ≥ 38
  closed-loop time constants at the ratified GBW bound, long enough for the
  compensation node and second stage to settle into the saturated state.)
- **Recovery time**: from the removal edge (start of its 1 ns ramp) to the
  **last** exit of |vout − v_ref| from a ±1 % band about |v_ref| (the issue's
  "±1 % of the linear output"), interpolated linearly. Because it is the last
  exit, a transient tolerance crossing that leaves the band again never
  counts as recovered.
- **Latch check (explicit)**: a polarity is reported **not recovered** — and
  the point flagged latched — when it is still outside the band at the end of
  its recovery window (1.59 µs high / 1.70 µs low, ≥ 100 tau), when it drifts
  by more than 0.1 bands across the final 50 ns reference window (a slow tail
  the window mean would otherwise hide), or when it settles more than one
  band from `v_ref` (hysteresis: recovered to the wrong level). A latched
  polarity ranks as the worst recovery result. A *hard* latch (the output
  never returning at all) surfaces one polarity earlier, as an INVALID point:
  the next overdrive's pre-overdrive check finds the output far from the
  input, and the reason is recorded in the table.
- **Grid (45 points)**: MOS process `typical, ff, ss, fs, sf` × −40, 27,
  125 °C × 2.97, 3.30, 3.63 V; every MOS corner paired with `res_typical` /
  `mimcap_typical` (the step/gain convention; no independent RZ / CC corner
  coverage is claimed).
- **One `klt sim` request carrying all 45 points.** Where it runs is `klt`'s
  decision (`--backend`, `$KLT_SIM_BACKEND`: the Spot batch fleet on a
  dispatch worker). The driver never loops ngspice over the grid and never
  falls back to a local grid: a refused or failed submit writes **no record**
  and exits 2. Only the three single-unit controls (nominal point) run
  locally. Sampling is 5 ns over the 4.9 µs run (≈ 1000 rows per point) — the
  proven batch-fleet regime (see
  [`../slew-swing-power/`](../slew-swing-power/README.md), "Batch-fleet
  timing"; denser sampling made 45-point jobs hit the per-point timeout).

## Recovery times vs the small-signal GBW

The ratified GBW ≥ 10 MHz bounds the closed-loop time constant at
`tau = 1/(2π·GBW) ≤ 15.9 ns`. The saturation hold is 600 ns = 38 tau; each
recovery window is ≥ 100 tau. A single-pole recovery traverses the ~1.55 V
saturation travel in a slew-limited segment first (≈ 14.5 V/µs at nominal →
≈ 110 ns) and then settles like the small-signal step (≤ ~70 ns to 1 %), so
recovery times of a few hundred ns are expected and the windows have ≥ 5×
headroom. A unit test pins these ratios.

## Extraction

Per polarity, from the rawfile klt retains (`v(vinp)`, `v(vout)`):

1. **Validity** — `v_ref` = time-averaged `vout` over the 50 ns before the
   overdrive must equal `vinp` there to 20 mV (the follower follows); the
   hold must satisfy the saturation requirement above; the recovery window
   must hold ≥ 50 samples. Otherwise the point is **INVALID**.
2. `y = (vout − v_sat) / (v_ref − v_sat)` over the recovery window (end
   points interpolated): 0 = saturated level, 1 = linear value.
3. **Recovery time** = time from the removal edge to the last exit of
   |y − 1| from `band / |v_ref − v_sat|`, interpolated linearly; **not
   recovered** (`inf`) under the three latch conditions above.
4. **Overshoot** = furthest excursion past `v_ref` (mV of output, reported as
   a fraction of the travel); **preshoot** = a wrong-way first move deeper
   into saturation. Evidence only.
5. Per point the **worse** polarity is reported; both are in the table.

## Controls and cross-checks

- *Cross-checks*: ngspice `.meas` values (pre-overdrive level, high-hold
  maximum, low-hold minimum, final level) must agree with the rawfile
  extraction to 20 mV (≈ one recovery band); a disagreement blocks the
  record.
- *Re-derivation*: every point's `corners/<rid>/<point>.dat` holds every
  rawfile sample (`time_s vout_v vinp_v`); before writing the record the
  driver re-reads each file and re-runs the extraction, and requires the same
  validity, recovery times, recovered/not-recovered status and saturation
  levels. The count is in the record.
- *Controls* (single local units at the nominal point, recorded apart from
  the grid): `cl-x5` (CL = 10 pF) must recover slower than nominal by
  > 50 ns — the extraction sees the heavier load; `ibias-zero` (dead
  amplifier) must never yield a valid recovered overdrive. A failed control
  makes the driver exit non-zero.
- *Unit tests* (`test_overdrive_recovery.py`, no simulator): exponential
  recovery with a known time constant (`tau·ln(travel/band)`), underdamped
  ringing (overshoot and last-exit recovery against a 1 ps brute-force
  reference), transient tolerance crossings (the last exit counts), latch
  timeouts, hard latches (invalid at the next polarity), hysteresis
  (recovery to a wrong level), slow tails hidden in the window mean (the
  drift rule), sustained small rings, non-saturating and non-following
  benches (invalid), worse-polarity reporting, worst-point ranking with
  not-recovered-worst, control logic, the data round trip, `.meas`
  disagreement detection, the bench guards, the **stale-DUT guard** (a
  resized, dropped or duplicated device is rejected before anything is
  written), bench timing vs the configuration (including both pulse cards and
  the beyond-the-rails amplitudes at every grid supply), and the
  window-vs-GBW ratios. `sim/ci_regression_check.sh` mutates the shared
  DUT-include guard, the stale-DUT guard and the recovery-band extraction and
  requires this suite to fail each time.

## Running

```sh
python3 sim/overdrive-recovery/run_overdrive_recovery.py              # 45-point grid + controls + record
python3 sim/overdrive-recovery/run_overdrive_recovery.py --smoke      # one nominal point, local, no record
python3 sim/overdrive-recovery/test_overdrive_recovery.py             # unit tests, no simulator
```

Prerequisites are the siblings': `klt`, `ngspice`, the pinned gf180mcu PDK
and `numpy` / `matplotlib`. The driver imports
`../gain-gbw-pm/run_gain_gbw_pm.py` (DUT normalisation, source guards, request
skeleton), `../slew-swing-power/run_slew_swing_power.py` (rawfile parser) and
`../step-response/run_step_response.py` (window-mean and last-exit-settling
helpers), so there is one copy of each. Useful flags: `--backend`,
`--batch-submit-retries N`, `--batch-runner-version-check warn`.

Each run mints a new append-only record id and writes only new files:

```
records/<rid>.md, records/<rid>-plots/recovery_all45.png
netlist-snapshots/<rid>.spice            DUT + testbench + klt request + nominal deck
corners/<rid>/<process>_<T>c_<vdd>v.{log,cir,dat}, klt-report.json
corners/<rid>/controls/*
```

## Records

| Record | Result | Notes |
|---|---|---|
| [`20261011-193317-525511f`](records/20261011-193317-525511f.md) | 45/45 points valid. Worst recovery **165.6 ns to ±1 %** (high overdrive) at sf / 125 °C / 3.63 V; worst low-overdrive recovery 99.6 ns at ff / 125 °C / 3.63 V; worst overshoot 15.93 mV at fs / 125 °C / 2.97 V. **Latched points: none.** | One fleet request (`aws-batch-fleet`, job `klt-sim-c29e88157c79`; runner klt 0.5.0, `runner_version_check: warn`). Cross-checks 45/45, re-derivation 45/45, controls behave |

## Report integration

Like `step-response/`, this experiment is not registered in
`sim/report/selection.json`: the aggregate report covers spec rows and their
supplementary studies, and this bound-free study informs none. It carries a
measurement fingerprint (`measurement_config.py`) so a future report selection
can apply the freshness policy.
