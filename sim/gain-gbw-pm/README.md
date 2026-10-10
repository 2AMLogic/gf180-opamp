# `sim/gain-gbw-pm/` — open-loop DC gain / GBW / phase margin

Spec-row evidence for three ratified rows of `spec/target-spec.md` §2 —
**open-loop DC gain** (≥ 60 dB, stretch ≥ 70 dB), **GBW** (≥ 10 MHz into
CL = 2 pF) and **phase margin** (≥ 60°) — measured on the **committed sized
schematic** across the full ratified grid (issue #38; tracker #7 items 5 and
9). It replaces the earlier hand-built, schematic-disconnected testbench of
issue #19; those two records stay in `records/` unchanged (append-only) and
are superseded as evidence by the newest record here.

## What is measured

- **Device under test**: `design/netlist/opamp_two_stage.spice`, the xschem
  export of `design/opamp_two_stage.sch`, instantiated as
  `Xdut vdd 0 vinp vinn vout ibias opamp_two_stage`. The testbench declares
  **no transistor**: every device, including the committed Miller cap `CC`
  and nulling resistor `RZ`, comes from the export. The export is consumed
  through `subckt_from_export()` in `design/check_dc_op.py` (uncomment
  xschem's `**.subckt`/`**.ends`, drop `.end`; nothing else), so the DC
  operating-point check and this bench read the design through one
  conversion.
- **Bias and load**: 10 µA driven *into* `ibias` (the polarity
  `check_dc_op.py` uses), `CL = 2 pF` on `vout`, input common mode tracking
  VDD/2.
- **Grid (45 points)**: MOS process `typical, ff, ss, fs, sf` × temperature
  −40, 27, 125 °C × supply 2.97, 3.30, 3.63 V (±10 %). One small-signal
  `.ac` sweep per point, `0.1 Hz – 1 GHz`, 20 points/decade.
- **Passive-section policy**: every MOS corner is paired with the same
  `res_typical` and `mimcap_typical` sections (what the nominal DC check
  uses). The grid varies MOS corner, temperature and supply. It does **not**
  claim independent corners of `RZ` or `CC` -- the default grid never does. The
  opt-in `--passive-corners` mode (issue #70) sweeps `res_{typical,ff,ss}` x
  `mimcap_{typical,ff,ss}` independently at fs/125 C/2.97 V, ss/125 C/2.97 V and
  nominal as ONE `klt sim` request (see record `20261009-234014-55b400c`, which
  supersedes `20261009-233341-95dfc2a` with a corrected verdict header from the same data). The
  committed `RZ`/`CC` are used as drawn and never retuned here — their
  phase-margin consequence is simply part of the measurement.

## Measurement technique

**DC-closed, AC-open loop.** A huge inductor `Lfb` (1e9 H) from `vout` to
`vinn` closes the loop at DC — the amplifier self-biases as a unity-gain
buffer at `vinn = vout = VCM`, the operating point `check_dc_op.py`
verifies — while 2π·f·`Lfb` ≥ ~6e8 Ω even at the sweep's 0.1 Hz start, far
above the output resistance, so the loop is open at every swept frequency. A
huge `Cfb` (1e9 F) from `vinn` to ground makes `vinn` an AC ground; the AC
stimulus (1 V) sits on `vinp` only. A *small* `Lfb` would load the output
and show up as a gain that rises with frequency — the failure the plateau
check below exists to catch.

**Gain** is `v(vout) / (v(vinp) − v(vinn))`, using the actual differential
input phasor, not an assumed 1.

**Extraction** (`extract_metrics()` in `run_gain_gbw_pm.py`):

1. *Validation*: ≥ 20 finite points, positive strictly increasing
   frequency, non-zero magnitude.
2. *DC gain = low-frequency plateau*, not the peak. The lowest decade
   (0.1–1 Hz) must be flat to within 0.10 dB and have a phase within 10° of 0
   (this also verifies input polarity: `vinp` is the non-inverting input). The
   DC gain is the mean dB over that band.
3. *Phase*: `numpy.unwrap` over the whole sweep, referenced to the plateau
   phase, so a response that passes −180° is not folded back.
4. *GBW*: the **first descending** 0 dB crossing (`dB[i] ≥ 0 > dB[i+1]`),
   interpolated linearly in (log₁₀ f, dB). *Phase margin* = 180° + the
   unwrapped phase interpolated at that frequency.
5. *Never passes*: a missing crossing (sweep ends above 0 dB, or starts
   below it), any further 0 dB crossing (gain peaking back above 0 dB), a
   non-flat plateau, wrong polarity, malformed or non-finite data, or a
   failed simulation. An invalid point fails **every** row.

**Verdicts** are per point and per row against the ratified bound; each
row's *binding corner* is the worst point (an invalid point binds first).
The 70 dB gain stretch is reported separately and is not a mandatory row.

**Feedback-isolation study.** The nominal point is re-measured with
`Lfb = Cfb` ∈ {1e8, 1e9, 1e10} (gain stays within 0.10 dB, PM within
0.5°, GBW within 0.5 %), plus a deliberately inadequate 1e4 that the plateau
check must reject.

**Negative controls**, deterministic, recorded separately from the grid:
the Miller capacitor `XCC` removed from the DUT, and `ibias` driven at 0 A.
Each simulates successfully and the checks must fail; if a control passes
every row the driver exits non-zero.

**Operating point.** A separate `op` analysis of the same testbench (same
grid) flags `|vout − VCM| > 100 mV` and, where the executing runner returns
`expr` measurements, any DUT MOSFET out of saturation; a local single `op`
unit records the nominal device-level bias. Flags are recorded, not spec
rows.

## Execution: one `klt sim` request

The 45-point grid is **one `klt sim` corner-matrix request** (process bundle
× `supply_v` × `temperature_c`), not a loop of `ngspice` runs. Which backend
runs it is `klt`'s decision — `--backend`, the request, or
`$KLT_SIM_BACKEND` (the Spot batch fleet on a dispatch worker). Outputs come
back in the klt report; the batch job id is in `environment.remote` and is
copied into the record. If a batch submit fails the driver reports the error
and writes **no record** — it never falls back to a local grid. Only
single-unit runs (the nominal `op`, the isolation study, the negative
controls, `--smoke`) run locally.

`vdd` and `vcm` are swept together by index so VCM tracks VDD/2. The process
axis uses gf180mcu's bundle form: `{name: ff, sections: [ff, res_typical,
mimcap_typical]}`.

The testbench `.include`s the PDK's `design.ngspice` (global parameters) and
`opamp_two_stage.dut.spice`; the driver materialises both into a per-run work
directory (a copy of the PDK file and the wrapper-normalised export) and
rewrites only those two include paths, so klt stages them as the netlist's
include closure.

## Cold start

Requires `ngspice`, `klt`, Python 3 with `numpy` and `matplotlib`, and the
pinned gf180mcu PDK revision:

```bash
pip install volare
volare enable --pdk gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b
```

PDK resolution (first hit wins): `$GF180_PDK_PATH` (a gf180mcu variant
directory with `libs.tech/`), else `$PDK_ROOT` (+ `$PDK`, default
`gf180mcuD`), else `~/.volare/gf180mcuD`.

```bash
# Full 45-point grid + studies — mints a new append-only record.
python3 sim/gain-gbw-pm/run_gain_gbw_pm.py
./sim/characterize.sh                  # same, via the repo-level driver

# Useful flags (all forwarded to klt):
#   --backend local|batch|...          override klt's backend choice
#   --batch-submit-retries N --batch-retry-wait-s S
#                                      re-submit when the shared fleet's
#                                      concurrency cap refuses the submit
#   --batch-runner-version-check warn  run on a fleet runner older than the
#                                      client (see the record for what ran)
#   --strict                           exit 1 when a ratified row misses

# Fast checks, no record written:
python3 sim/gain-gbw-pm/test_gain_gbw_pm.py   # extraction + source guards
python3 sim/gain-gbw-pm/run_gain_gbw_pm.py --smoke
./sim/selftest.sh                      # both of the above
```

Exit status: 0 when the evidence is complete — 45 valid simulations, the
negative controls fail as they must, the isolation study is stable —
**including when a spec row misses** (a miss is a result; `--strict` makes it
exit 1). A failed submit or an incomplete grid exits 2 with no record.

Each run writes, under a fresh `<record-id>` (`<YYYYMMDD-HHMMSS>-<git-sha>`);
the driver refuses to overwrite an existing id:

- `corners/<id>/<process>_<T>c_<vdd>v.{log,cir,dat}` — klt's ngspice log, the
  generated corner deck, and `freq, Re/Im(vout/vdiff), Re/Im(vdiff)` for each
  of the 45 points (supply is in the name, so all 45 survive);
- `corners/<id>/klt-report.json`, `klt-op-report.json` — sanitised klt
  reports (environment, job id, per-corner status); `corners/<id>/controls/`
  — the isolation and negative-control runs;
- `netlist-snapshots/<id>.spice` — the request (conditions), the **DUT
  contents**, the testbench and the nominal klt deck: enough to reproduce the
  measured design after the schematic changes;
- `records/<id>.md` and `records/<id>-plots/*.png` — the per-row verdicts,
  binding corners, all 45 points, operating-point and polarity checks,
  isolation study, controls, and the PDK revision used.

## Fixed common-mode option (`--vcm-fixed`, issue #125)

The default bench tracks VCM = VDD/2 and is unchanged. `--vcm-fixed VOLTS`
holds the amplifier input common mode at one value at every supply point (the
`supply_v.vcm` axis of the same one-request klt matrix becomes a constant); the
LDO consumer's documented nominal input is 1.20 V
(`spec/decision-records/0005-input-common-mode-range-row.md`, which also records
the 15 mV low-side headroom at SS and that the consumer tolerance is
unspecified -- none is invented here).

```bash
python3 sim/gain-gbw-pm/run_gain_gbw_pm.py --smoke --vcm-fixed 1.20   # one local nominal point
python3 sim/gain-gbw-pm/run_gain_gbw_pm.py --vcm-fixed 1.20           # smoke, then the 45-point grid
```

- The value is validated before any tool is touched or anything is submitted
  (finite, > 0 V, below every supply point; not combinable with
  `--passive-corners` / `--recompute-passive` / `--recompute-fixed-vcm`). The driver runs the nominal
  point locally first and does not submit the grid if it fails, then checks the
  klt report really carried the requested VCM at every point.
- Same bench, extraction, bounds, ideal 10 uA bias, 2 pF, typical passives. The
  common-mode policy is part of the measurement fingerprint (`vcm_rule` is
  `fixed:1.2`, versus `vdd/2`; default fingerprints are byte-identical to before)
  and of the record header.
- Evidence is written under `fixed-vcm/{records,corners,netlist-snapshots}/`,
  **not** `records/` / `corners/`, so the report generator and the shared
  45-point cross-checks (`latest_gain_dir`) keep selecting the VDD/2 records.
  Failed, missing or invalid points are listed explicitly in the record (exit 1);
  a failed submit writes no record and never falls back to a local grid (exit 2).
- Exit semantics match the default grid: a ratified-bound miss is a diagnostic
  result (exit 0, misses printed); with `--strict` it exits 1 **after** the
  record and corner data are written.
- Records retain the canonical measurement-fingerprint inputs (JSON that
  rehashes to the header fingerprint). `--recompute-fixed-vcm RECORD` writes a
  new record superseding RECORD from its committed `fixed-vcm/corners/RECORD/`
  data and the testbench in its netlist snapshot, with no simulator.
- The record compares each point with the newest VDD/2 record's stored data
  (gain, GBW, PM deltas) and reports the per-point DC follower error
  `|vout - VCM|` from an operating-point request on the same grid (vout-only on
  the fleet) plus one local device-level nominal operating point.

Result, record `fixed-vcm/records/20261010-134147-1739a3d.md` (supersedes
`20261010-133217-43b32f6`, which omitted its fingerprint-inputs section;
regenerated from that record's committed data, no new simulation, every
number unchanged; 45/45 points valid): relative to the VDD/2 baseline, DC gain moves -0.60 .. +0.42 dB, GBW
-2.7 .. -0.3 %, PM +0.04 .. +0.62 deg; the pass counts against the unchanged
bounds are identical (gain 45/45, GBW 45/45, PM 15/45), worst PM 57.38 deg
(fs / 125 C / 2.97 V) versus 57.34 deg, and the nominal follower error is zero
with all DUT MOSFETs saturated. The PM shortfall therefore persists at the
consumer's operating point; it is evidence for #42, not a consumer contract or a
T1 completion claim.

## Guards

`test_gain_gbw_pm.py` (stdlib `unittest`; no simulator) covers:

- extraction against synthetic responses with analytically known crossover
  and phase (two-pole), phase wrapping through −180°, an absent crossing,
  peaking / multiple crossings, a rising low-frequency response, wrong
  polarity, non-finite and malformed data;
- the source guard: the testbench must `.include` the design-derived DUT and
  the PDK design file, declare no MOSFET or PDK device, instantiate
  `opamp_two_stage` exactly once, and stay a circuit body (no
  `.lib/.temp/.control/.end`); the materialised DUT must contain every
  device line of the committed export unchanged, with no duplicates. The
  driver runs the same guards before every simulation;
- verdict aggregation / binding corners, the 45 unique points and
  filenames, and append-only record paths.

## Limitations

- Deterministic corners only — no mismatch / Monte Carlo (offset has its own
  future testbench), no noise, CMRR/PSRR (#39), slew, swing or power.
  A mismatch Monte Carlo runner for these three rows now exists in
  [`../gain-gbw-pm-mc/`](../gain-gbw-pm-mc/README.md) (issue #129). It reuses
  this bench, extraction and guards. **No mismatch evidence exists yet**:
  its N = 300 campaign is gated on #42's revised DUT and deterministic
  record, so the rows here still rest on deterministic corners only.
- Passive corners are not independently exercised (see the policy above).
- GBW and phase margin are defined by the first 0 dB crossing of the
  small-signal response with `CL = 2 pF` on the output and the committed
  `RZ`/`CC`; no external compensation is added.
