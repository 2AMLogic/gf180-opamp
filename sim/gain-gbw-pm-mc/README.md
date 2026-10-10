# sim/gain-gbw-pm-mc — mismatch Monte Carlo of gain, GBW and phase margin (issue #129)

Runner, offline tests and documentation for a mismatch Monte Carlo of the
three AC rows `sim/gain-gbw-pm/` measures on matched devices: open-loop DC
gain, GBW and phase margin.

> **No mismatch evidence exists yet.** This directory holds machinery
> only. There is no `records/`, `corners/` or `netlist-snapshots/` here,
> and nothing in `spec/`, `manifests/` or `sim/report/selection.json`
> cites it. The N = 300 campaign is gated on #42 (see
> [Campaign gate](#campaign-gate-42)).

It measures; it judges nothing new. Samples are compared with the ratified
bounds the deterministic driver uses (gain ≥ 60 dB, GBW ≥ 10 MHz into
2 pF, PM ≥ 60°). A sample that lands exactly on a bound passes. No bound is
proposed or relaxed here. Any spec change needs a `spec/` decision record.

## What it reuses (no second metric definition)

- **Bench**: `sim/gain-gbw-pm/testbench/tb_gain_gbw_pm.spice`, staged by the
  gain driver's `materialise` (its testbench and DUT guards run, and the
  DUT comes from the committed export). The runner adds one line after the
  `design.ngspice` include: `.param sw_stat_mismatch=1`. The bench declares
  no transistor. `guard_mc_testbench` requires every other code line to
  match the committed gain bench, the switch to be 0/1 and placed after the
  include, `sw_stat_global` to be left alone, and `Ibias … 10u` /
  `CL vout 0 2p` to be present.
- **Request**: the gain driver's `ac_request` (MOS corner bundled with typical
  `res`/`mimcap` sections, `.ac dec 20 0.1 1e9`, VCM = VDD/2, the three
  `spice` `.meas` cross-checks), plus `monte_carlo` and a control tail.
- **Extraction**: each sample goes through the gain driver's own code:
  `differential_gain` (the actual differential phasor),
  `extract_metrics` (lowest-decade plateau and polarity, unwrapped phase,
  first descending 0 dB crossing interpolated in (log f, dB), re-crossings
  invalid) and `crosscheck` (the `.meas` values). The sibling driver is loaded
  with `harness.load_sibling` under the shared name `gain_gbw_pm_driver`.

### Per-sample transport: the log, not a rawfile

For Monte Carlo AC requests the fleet runner keeps each sample's ngspice log
(this was shown for `sim/cmrr-mc/`). It has not been shown to keep a
per-sample rawfile. So the control tail prints the real and imaginary parts of
`v(vout)`, `v(vinp)` and `v(vinn)` at 15 significant digits, as one
unbroken table between `GMC_VECTORS_BEGIN`/`END` markers.
`parse_log_vectors` reads that table strictly: one block, the expected
header, consecutive indices 0..200, every value numeric and finite, and the
scale column equal to the printed frequency. Anything else makes the sample
invalid. The runner also requires the differential excitation to be 1 V
within 1e-6 V at every frequency.

- `--smoke` checks the transport against the gain driver's rawfile path
  (local single units, mismatch off) and requires identical metrics. On
  2026-10-10 the deviation was 0 in gain, GBW and PM.
- `--probe` is the bounded transport check on the real backend: N = 3 at
  typical / 27 °C / 3.30 V. On 2026-10-10 the N = 3 probe ran on the batch
  fleet as job `klt-sim-a7bd9d91a814` (runner klt 0.5.0, ngspice 46). All 3
  samples came back with their three seeds and a log containing the
  vectors, and all 3 extracted as valid, distinct samples (PM 59.33–59.60°,
  around the committed fleet deterministic nominal of 59.49°). The probe is
  diagnostic: it writes nothing into the repository and is not evidence.

## Population

The later campaign, as one multi-corner `klt sim` request:

| axis | value |
|---|---|
| MOS corners | typical, ff, ss, fs, sf |
| temperature / supply | 27 °C / 3.30 V, VCM = VDD/2 |
| bias / load | ibias = 10 µA, CL = 2 pF |
| passives | typical (`res_typical`, `mimcap_typical`) |
| Monte Carlo | `{n: 300, seed: 45, vary: "mismatch"}` → 5 × 300 = 1500 samples |

Temperature and supply are not sampled under mismatch. Widening to the
T/VDD grid is a follow-up, and only if the first record shows that the
PM/GBW sigma matters.

On a dispatch worker the request goes to the Spot batch fleet
(`$KLT_SIM_BACKEND=batch`). The runner never launches ngspice itself and
never loops over a grid. It refuses `--backend local` for any Monte Carlo
request. If a submit fails it stops and writes nothing: there is **no
local-grid fallback**. `--batch-submit-retries` only re-submits the same
request after a fleet capacity refusal.

## Validation and accounting

- **Identity**: each corner must have exactly the sample indices 0..299,
  each once, each with klt's `seed`, `mismatch_seed` and `process_seed`.
  The following are problems and contribute no sample, so they cannot
  inflate the denominator: an unexpected corner, a malformed or
  out-of-range index, or a repeated index. A missing index becomes an
  explicit invalid row.
- **Validity**: the sample becomes an invalid row with its reason if any of
  these hold: no log, unparseable/non-finite/malformed vectors, a simulator
  error, wrong excitation, no plateau, wrong polarity, no crossing, a
  re-crossing, or a `.meas` disagreement. Invalid rows are never filtered out.
- **Statistics** (per corner and row): mean, sample sigma (ddof = 1) and
  mean − 3σ over the valid samples, with the valid count shown. The failure
  fraction always uses the fixed denominator N = 300. Invalid and missing
  samples count as failures of all three rows. Numerical below-bound
  failures are reported separately from invalid failures.
- **Success**: a statistical record is written only if every corner has
  300 valid unique samples and there are no identity problems. Otherwise
  the runner prints a table labelled `DIAGNOSTIC ONLY`, with valid-only
  figures and their valid counts, and writes nothing.

## Campaign gate (#42)

PM is the one ratified row that currently fails (#42 is redesigning the DUT
for it). A Monte Carlo of the pre-redesign DUT would describe a design
that is about to change. So the full submission is refused unless all of
these hold, and they are checked before any record path is claimed or
anything is submitted:

1. `--revised-dut-sha256 <sha>` is given. It is the wrapper-normalised
   sha256 of the revised DUT (the `normalised sha256` a gain-gbw-pm record
   prints).
2. The pin is not the pre-redesign DUT (`PRE_REDESIGN_DUT_SHA256`, i.e.
   `81fbd914…`).
3. The committed export on this checkout hashes to the pin. A stale pin, or
   a checkout without the revised DUT, is rejected.
4. `--deterministic-record <rid>` names an existing
   `sim/gain-gbw-pm/records/<rid>.md` that cites the pinned hash. This is
   the revised DUT's deterministic acceptance evidence on `origin/main`.

Closing #42 is not enough on its own: the revised DUT and its deterministic
record both have to be on `origin/main`. `--smoke` and `--probe` are
separate diagnostic modes. They take no pin, cannot launch the campaign and
write nothing into the repository. CI tests the gate with fixtures and
never contacts the forge.

Not in scope: T1 item 6 ("Statistical claims carry Monte Carlo evidence",
`manifests/gf180-opamp.signoff.json`) accepts only `yield` envelopes
(`manifests/README.md`), and a `klt sim` Monte Carlo envelope is not one.
Item 6 stays unmet. The runner makes no manifest citation, no
aggregate-report selection and no Gaussian-yield claim.

## Running

```bash
python3 sim/gain-gbw-pm-mc/test_gain_gbw_pm_mc.py          # offline tests (no simulator)
python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py --smoke   # local single units, writes nothing
python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py --probe \
        --batch-runner-version-check warn                  # N=3 transport probe (batch), writes nothing
# after #42 lands (revised DUT + its deterministic record on origin/main):
python3 sim/gain-gbw-pm-mc/run_gain_gbw_pm_mc.py --revised-dut-sha256 <sha256> \
        --deterministic-record <gain-gbw-pm rid> --batch-runner-version-check warn \
        --batch-submit-retries 10                          # full campaign + record (append-only)
```

`sim/selftest.sh` runs the tests and `--smoke`. `sim/ci_regression_check.sh`
mutates the bench guard (circuit identity, switch placement), the sample
accounting (dropped failures, duplicate indices) and the gate (stale pin),
and requires the suite to fail for each mutation. CI writes no `records/`.

## Evidence layout (append-only, written only by a successful campaign)

```
records/<rid>.md                        statistics, method, gate pin, provenance
corners/<rid>/samples.csv               one row per sample: seeds, klt status, valid, reason, metrics, log name
corners/<rid>/<point>.responses.npz     extracted responses (freq, dB, unwrapped phase) per sample
corners/<rid>/klt-report.json           sanitised klt report (job id in environment.remote)
netlist-snapshots/<rid>.spice           bench + switch, DUT (with sha256), request
```

Existing paths are refused (`harness.claim_record_paths`, exclusive-create
writes). A re-run gets a new record id, and historical records are never
touched.
