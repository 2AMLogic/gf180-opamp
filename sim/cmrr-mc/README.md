# `sim/cmrr-mc/` — mismatch-aware CMRR (Monte Carlo)

Statistical evidence for the **CMRR** row of `spec/target-spec.md` (issue #61;
DR-3 residual (e3)). The systematic CMRR record
([`sim/cmrr/`](../cmrr/README.md), `20261009-105631-30ec86d`) simulates
perfectly matched devices, so its 95.42 dB worst DC plateau is an optimistic
upper bound: real CMRR is set by random mismatch in the input pair and the
load mirror. This experiment measures CMRR **with per-instance MOS mismatch
sampled** and reports the per-corner statistics. **It issues no verdict and
proposes no bound**; the bound is proposed by decision record
[0006](../../spec/decision-records/0006-cmrr-psrr-bounds.md).

## The problem it solves: one sample, two excitations

CMRR needs two AC excitations of the same amplifier: a differential one (for
Ad) and a common-mode one (for Acm). The systematic driver runs them as two
separate `klt sim` requests and solves them jointly. Under Monte Carlo that
breaks: each request draws its own devices, so Ad and Acm would come from two
different amplifiers.

Here **both excitations of a sample run in one ngspice process, on one parsed
circuit.** The driver appends control commands to the `.ac` analysis line
(`analysis.args`, which `klt sim` places verbatim in its `.control` block; the
systematic driver already uses this for its operating-point print):

```
ac dec 20 0.1 1e9             (dm drive from the .param line: acp = +0.5, acn = -0.5)  -> plot ac1
op                            (DC operating point after the dm sweep)                  -> plot op1
alter vcm acmag=1
alter vnac acmag=1
ac dec 20 0.1 1e9             (cm drive: acp = acn = 1)                                -> plot ac2
op                                                                                    -> plot op2
<exact per-frequency solve of vout = Ad vd + Acm vc from both sweeps' actual phasors>
<print every scalar figure>
```

The gf180mcu per-instance mismatch values (`agauss` in the `fets_mm` MOS
subcircuits, gated by `sw_stat_mismatch`) are drawn once, when the netlist is
parsed, so both sweeps see the same devices. Each sample proves it: the DC
output after the dm sweep and after the cm sweep (it carries the sampled
offset, sigma about 5 mV) must agree to 1 uV (**dm/cm parity**).

**Capability probe** (issue #61 step (a), [`probes/`](probes/)):

- [`20261009-233828-95dfc2a`](probes/20261009-233828-95dfc2a.md): the request
  also declared one `measurements[].expr`. The klt 0.5.0 fleet runner rejected
  the whole job (`each request.measurements[] entry requires 'name' and
  'spice'`; batch job `klt-sim-a98805096510` failed, no sample ran). The
  runner/client skew is tracked at 2AMLogic/klayout-tools#2917 / #2948.
- [`20261009-234034-95dfc2a`](probes/20261009-234034-95dfc2a.md): no `expr`;
  the scalars are `print`ed by the tail and read from each sample's retained
  log. Job `klt-sim-d87b7e224ba4` (Spot m7i.4xlarge): 3/3 valid, distinct
  samples, parity 0 uV, `environment.monte_carlo` populated. **Decision:
  option 1** (one deck, one request).

The probe's three DC offsets equal `sim/offset-mc/`'s samples 0-2 at
typical exactly (same klt per-sample seeds, same devices drawn): the CMRR
population here is the offset population of record `20261009-072205-96bf3cc`.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, the xschem
  export, through `harness.load_dut_text()`; the testbench declares no
  transistor.
- **Bench**: `testbench/tb_cmrr_mc.spice`. Its circuit is guarded to be
  **line-for-line** the systematic bench (`sim/cmrr/testbench/tb_cmrr.spice`:
  the DC servo that admits equal AC drive, CL = 2 pF, ideal Ibias = 10 uA,
  VCM = VDD/2). Only its single `.param` line differs: it adds
  `sw_stat_mismatch=1` after the `design.ngspice` include. `sw_stat_global`
  is never touched (global spread is the five MOS corners).
- **Population** (default `--grid nominal`, issue #61): process
  `typical/ff/ss/fs/sf` × 27 °C × 3.30 V, `monte_carlo = {n: 300, seed: 45,
  vary: "mismatch"}`: the same corners, N and seed as `sim/offset-mc/`, so
  1500 units in one `klt sim` request on the batch fleet. Temperature and
  supply are **not** sampled under mismatch. `--grid full` runs the 45 PVT
  points × 300 (13 500 units) instead.
- **Per sample** (printed into the log by ngspice, read by the driver): CMRR,
  Ad and |Acm| at the DC plateau (0.1-1 Hz mean), 1 kHz, 10 kHz, 100 kHz,
  1 MHz (exact sweep points) and at the differential unity-gain frequency f_u
  (`meas`, first falling 0 dB crossing of |Ad|); the DC offset; the parity;
  the excitation errors; the plateau flatness.

## Statistics

Per grid point and frequency, from the N valid samples:

| statistic | definition | role |
|---|---|---|
| **linear 3-sigma** | `mean(Ad dB) - 20 log10(mean|Acm| + 3 sigma|Acm|)` | the CMRR of the +3-sigma common-mode gain. The `sg13g2-opamp` twin's ratified definition (its DR-0004). |
| dB 3-sigma | `mean - 3 sigma` of CMRR in dB | issue #61's first proposal |
| min, p1, p5 | empirical | normality cross-check: N = 300 cannot resolve a 0.135 % tail |
| skew | of CMRR in dB | the dB image is skewed: a draw can cancel the systematic Acm and push CMRR up by tens of dB, which has no risk attached |

Which statistic carries a bound is decided in decision record 0006, not here.

## Validation (blocking: any failure writes no record, exit 2)

| check | criterion |
|---|---|
| population | every grid point contributes exactly N distinct sample indices 0..N-1, every sample valid |
| scalars | every printed figure present and finite; 201-point sweep, the spot frequencies exactly on the grid |
| excitations | dm: vd = 1 V, vc = 0; cm: vc = 1 V (to 1e-6 V); residual \|vd/vc\| <= 1e-6 |
| **dm/cm parity** | DC output after the two sweeps agrees to 1 uV (same devices in both) |
| operating point | vinp = VCM; \|vout - VCM\| <= 100 mV |
| plateau | Ad, CMRR and \|Acm\| flat over 0.1-1 Hz to 0.1 dB |
| f_u | inside the sweep |
| mismatch acts | offset sigma > 1 mV at every point (klt's `family_mismatch` cannot confirm it for an `.include`d DUT, 2AMLogic/klayout-tools#2928) |
| switch-off control | `sw_stat_mismatch=0`, 4 samples per corner: zero spread, and the committed systematic record reproduced (DC/spots 0.01 dB; f_u 0.5 %, CMRR at f_u 0.1 dB because `meas` interpolates linearly in f) |
| negative control | the systematic driver's load-mirror imbalance (XM3 W 6u -> 5.4u in a DUT copy), same Monte Carlo at typical, N = 300: mean \|Acm\| at DC rises by > 3 standard errors and both 3-sigma statistics fall |

Also recorded, without a criterion: a `vary: "process"` run at typical
(klt feeds ngspice one combined seed, so the RNG-drawn mismatch still varies:
2AMLogic/klayout-tools#2937, the `sim/offset-mc/` finding), and the per-sample
link to the offset Monte Carlo.

A sample whose |Acm| falls below the systematic record's demonstrated
numerical floor (2.62e-10 V/V) is clamped there and its CMRR is a lower bound;
there is never a division by zero.

## Running

```bash
python3 sim/cmrr-mc/test_cmrr_mc.py                 # offline tests (+ 2 local nominal units)
python3 sim/cmrr-mc/run_cmrr_mc.py --smoke          # one local unit, mismatch on, no record
python3 sim/cmrr-mc/run_cmrr_mc.py --probe \
        --batch-runner-version-check warn           # N=3 capability probe (probe record)
python3 sim/cmrr-mc/run_cmrr_mc.py --batch-runner-version-check warn \
        --batch-submit-retries 10                   # population + controls + record
```

Every Monte Carlo request goes through `klt sim` (the Spot batch fleet on a
dispatch worker, `$KLT_SIM_BACKEND=batch`). The driver never launches an
ngspice grid and never falls back to a local grid: a failed submit writes no
record. `--keep-work DIR` keeps the reports and logs of a run that failed
validation (never commit it). Use `python3 -s` if a user-site numpy shadows
the distro one.

## Evidence layout (append-only)

```
probes/<rid>.{md,json}                   capability probes (step (a))
records/<rid>.md                         statistics, worst points, controls, method
corners/<rid>/samples.csv                one row per sample: klt seeds, offset, parity, every figure
corners/<rid>/controls/*.csv             switch-off, process-only, imbalance samples
corners/<rid>/klt-report.<tag>.json      sanitised klt reports (job ids, runner versions)
netlist-snapshots/<rid>.spice            testbench + DUT + the control DUT's changed line + all requests
```

## Limitations

- MOS mismatch only. The deck has no resistor or MIM-cap mismatch (see
  `sim/offset-mc/README.md`'s PDK audit); RZ/CC are at typical.
- With `--grid nominal`, temperature and supply are not sampled under
  mismatch; the systematic grid covers them for matched devices only.
- Ideal bias current; schematic level; the bias generator's own rejection
  is excluded.
