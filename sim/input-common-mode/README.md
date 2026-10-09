# `sim/input-common-mode/` — follower-biased input common-mode range (ICMR)

Measurement evidence for the **input common-mode range** row proposed by
decision record 0005 (issue #60). Before this experiment no `sim/` bench moved
the input common mode off VDD/2, so neither consumer table in
`spec/target-spec.md` could grade its input-range row. The experiment finds, at
each of the 45 PVT points, the VCM interval over which the amplifier is a
working open-loop gain stage with its input pair, tail, mirror and output stage
saturated, and intersects those intervals.

**What is and is not claimed.** This is *follower-biased* ICMR: the DC output
settles where the unity-gain follower puts it, and a sample passes only while
that output stays within 0.10 V of VCM. It is not an arbitrary-output-voltage
input range and not an offset-accuracy claim. The 1 mV saturation tolerance and
the 0.10 V output tolerance are bench-validity tolerances, not performance
bounds. Sampled coverage does not prove continuous behaviour between samples;
endpoints are the nearest verified *passing* samples and the bracket to the
adjacent non-passing sample is reported as the uncertainty. No design resizing
or compensation repair is in scope (phase margin is issue #42).

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, instantiated as
  `opamp_two_stage`; the testbench declares no transistor. DUT conversion and
  guards are the gain/CMRR drivers' (reused).
- **Bench** (`testbench/tb_input_common_mode.spice`): the CMRR bench's circuit
  body — ideal 10 µA `Ibias`, `CL` = 2 pF [DR-1], and the buffered DC servo
  (`Esb`/`Rsv`/`Csv`/`Esv`, tau = 1e18 s). A unity-gain-follower operating
  point at DC, open loop at every swept frequency, independent AC drive on both
  inputs. The measured gain is the **open-loop DC-plateau gain** (|Ad| over
  0.1–1 Hz, minimum compared), *not* the closed-loop follower gain.
- **Axes**: process {typical, ss, ff, fs, sf} × T {−40, 27, 125 °C} ×
  VDD {2.97, 3.30, 3.63 V}, each with its own VCM scan 0..VDD at ≤ 50 mV
  spacing that always contains 1.20 V, VDD/2 and VDD. `klt sim`'s
  `corners.supply_v` pairs `vdd` and `vcm` **by index**, so a request repeats
  every VDD once per VCM sample; the 45 PVT points each contain a full scan
  (≈ 3 060 units per excitation), the experiment is not 45 scalar samples.
- **Excitations**: `dm` (vinp +0.5 V, vinn −0.5 V AC) and `cm` (vinp = vinn = 1 V
  AC); Ad and Acm are solved jointly from the actual input phasors
  (`sim/cmrr/run_cmrr.py`'s solve, reused unchanged). The `.ac` grid is the
  shared one starting at 0.1 Hz, stopped at 10 Hz (the plateau band is
  0.1–1 Hz; the rest of the shared sweep is irrelevant to a DC-plateau gain).
- **Operating point**: `analysis.args` is extended (as in the CMRR bench) with
  `op` + `print` of vout, vinp, vinn and every DUT MOSFET's `vds`, `vdsat`, `id`
  into the retained ngspice log, so no fleet expression-measurement support is
  needed.

## Pass, fail, invalid

A sample (PVT point, VCM) **passes** when all hold:

| criterion | value |
|---|---|
| Ad plateau over 0.1–1 Hz | flat to 0.1 dB, ~0° phase; its **minimum** ≥ 60 dB (the ratified gain bound, `G.GAIN_MIN_DB`) |
| saturation, input pair XM1/XM2, tail XM5, mirror XM3/XM4, output stage XM6/XM7 | `abs(vds) − abs(vdsat)` ≥ −1 mV; negative margins inside the tolerance are **flagged** |
| follower output | `abs(vout − VCM)` ≤ 0.10 V (the CMRR bench's OP tolerance) |
| currents | input-pair and tail current non-zero (> 1 nA) |

XM-B1 (the bias reference diode) is retained in the log and CSV but is not part
of the criterion. A sample is **invalid** (never passing, never bridged) when:
a result or log is missing, any printed field is missing or non-finite, the
analysis did not complete, the AC excitation is not the intended one (the
CMRR bench's 1e-6 V / 1e-6 checks, so unequal drive is rejected), the two
excitations' operating points disagree by more than 1 µV, `v(vinp)` is not at
VCM, or no Ad plateau can be established.

## Ranges

- **Every contiguous passing interval per PVT point.** Two passing samples join
  only if they are adjacent in the table and ≤ 50 mV apart; a failing, invalid
  or missing sample ends an interval, nothing is bridged.
- **Refinement.** Every adjacent pass/non-pass pair further apart than 5 mV is
  filled at 5 mV spacing by additional paired batch requests (up to three
  rounds). Refined points are requested at every temperature and process of
  the request that needed them and all returned samples are kept.
- **Endpoints** are the passing-side samples of each bracket; the uncertainty is
  the distance to the adjacent non-passing sample (or "scan edge"). The record
  repeats the analysis with saturation tolerance 0 mV (tolerance sensitivity).
- **Intersection** across all 45 PVT points, every component kept, and the
  component containing 1.20 V selected. An empty intersection is an explicit
  result; criteria are never weakened.
- **1.20 V** (the LDO's VREF, a point interval `[1.20 V, 1.20 V]`) is graded as
  an explicit sample at all 45 combinations: *meets* (45/45 pass), *fails* (any
  valid sample fails) or *unknown* (no failure but a sample is invalid/missing).
  No tolerance around 1.20 V is invented.
- **Worst point**: the lowest plateau gain and the smallest device margin, with
  conditions, at 1.20 V and inside the selected component.

## Controls (blocking: any failure writes no record, exit 2)

| control | criterion |
|---|---|
| midrail reproduction | at VDD/2 the scan's mean Ad over 0.1–1 Hz equals the committed CMRR record's at all 45 points within 0.05 dB |
| local vs scan | a local nominal pair reproduces the scan's nominal midrail sample within 0.05 dB |
| servo isolation | tau = 1e16 and 1e20 s move the plateau gain ≤ 0.01 dB |
| inadequate servo | tau = 1e-3 s (the loop closes at AC) must be rejected |
| unequal CM drive | acn = 0.99 must be rejected |
| retained-data recomputation | all samples are re-derived from the retained archives through a separate read path and agree with `samples.csv` and the headline numbers |

## Running

```bash
python3 sim/input-common-mode/test_input_common_mode.py   # offline tests (+ local nominal units)
python3 sim/input-common-mode/run_input_common_mode.py --smoke
python3 sim/input-common-mode/run_input_common_mode.py --batch-runner-version-check warn \
        --batch-submit-retries 60        # full scan + refinement + record (append-only)
python3 sim/input-common-mode/run_input_common_mode.py --recompute <record id>
```

Prerequisites are the CMRR experiment's: `klt`, `ngspice`, the pinned gf180mcu
PDK, numpy/matplotlib, and the committed CMRR dataset
(`sim/cmrr/corners/`, used for the midrail reproduction control). The scan always
goes through `klt sim` (the Spot batch fleet on a dispatch worker,
`$KLT_SIM_BACKEND=batch`); the driver never launches an ngspice grid and never
falls back to a local grid. If a batch submit fails it stops with the error and
writes no record. A capacity refusal is retried (same backend) by
`--batch-submit-retries`. Only single-unit controls and the smoke test run
locally. `--keep-work DIR` keeps all klt output and caches each report, so
re-running with the same directory re-uses finished requests (and never
re-submits an identical one); never commit that directory.

## Evidence layout (append-only)

```
records/<rid>.md                       headline, intervals per PVT point, explicit 1.20 V table, validity failures, controls
records/<rid>-plots/*.png              passing intervals of all 45 points; gain/margin vs VCM
corners/<rid>/samples.csv              every sample: status (1 mV and strict), gain, margins and currents of every MOSFET, reasons
corners/<rid>/data/<point>.tar.gz      retained rawfile (.raw, trimmed to frequency/v(vinp)/v(vinn)/v(vout)) and ngspice log (.log) per VCM sample and excitation
corners/<rid>/requests/*.json.gz       every klt request (netlist path omitted)
corners/<rid>/reports/*.json.gz        sanitised klt reports (batch job ids, runner versions)
corners/<rid>/controls/*.log           local control logs
netlist-snapshots/<rid>.spice          DUT + testbench + conditions + generated deck
```

The committed rawfiles keep only the four vectors the extraction reads (`RAW_KEEP`;
value lines copied verbatim, so `--recompute` parses bit-identical vectors). klt's
rawfile also carries every other node voltage and source current (20 vectors);
committing those for ~12 000 units would be ~135 MB of evidence no figure uses.
The full ngspice log (with the operating-point print) is kept for every unit.

Every run mints a new record id; a record is never overwritten. Results must be
regenerated if the DUT changes (for example after #42); each record names the
DUT sha256 it measured.
