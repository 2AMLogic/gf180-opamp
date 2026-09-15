# `sim/gain-gbw-pm/` — open-loop DC gain / GBW / phase margin

The repo's **first circuit-level spec-row testbench** under `sim/` (issue
#19), following `sim/gm-id-characterization/`'s device-level study. Measures
open-loop DC gain, gain-bandwidth product (GBW) and phase margin — three
numbers from one `.ac` sweep — against a **provisional, smoke-level**
two-stage Miller-compensated op-amp netlist, across the DR-0001-ratified
`typical/ff/ss/fs/sf` process corners at −40/27/125 °C.

**This is evidence, not a spec verdict.** No `design/` schematic exists yet
for this block (`spec/decision-records/0001-topology-and-cl.md` is
*proposed*, not ratified) and `spec/target-spec.md`'s phase-margin row is
`[P]` (proposed, unratified) with every other performance row `[TBD]`. Every
number this experiment records is measured data pending a real sizing pass
and spec ratification — see each record's "Claim" field for the exact
wording.

## What was measured

- **Topology** (DR-0001): NMOS input differential pair with a PMOS
  current-mirror load (first stage), PMOS common-source gain device (second
  stage), Miller compensation cap `Cc` between the two stages, `CL = 2 pF`
  at the output (`spec/target-spec.md` §1, `[DR-1]`).
- **Bias simplification (explicit)**: the tail current and the second-stage
  bias current are **ideal SPICE current sources**, not a self-biased
  mirror/reference network. This factors out bias-generator design — a real
  engineering task for a future sizing issue — and keeps the netlist robust
  across all 15 PVT points, since an ideal current source's infinite output
  impedance never degrades either stage's intrinsic gain the way a real
  mirror transistor's own finite `rds` would.
- **Device sizing (illustrative only, not a sizing pass)**: input pair at
  `L = 0.28 µm` (short channel, chosen for `fT` — `nfet_03v3` `fT ≈ 25.12
  GHz` at `L=0.28µm`, `Vov=200mV`, `typical` corner,
  `sim/gm-id-characterization/records/20260909-052956-79c6a45.md`, the same
  figure DR-0001 cites for its own input-pair decision). Second-stage PMOS
  at `L = 4 µm` (long channel, chosen for `gm/gds` — `pfet_03v3` `gm/gds ≈
  2122.7` at `L=4µm`, `Vov=200mV`, same record/corner, the same figure
  DR-0001 cites for its own output-stage decision). Exact `W`, bias
  currents and `Cc` were tuned empirically (see
  `testbench/tb_gain_gbw_pm.spice`'s header) to converge to a sane
  small-signal operating point — this is **not** a gm/ID-driven sizing
  pass.
- **Testbench technique**: the standard "big resistor" open-loop AC trick —
  a `Rfb = 1e15 Ω` resistor from the output back to the inverting input
  DC-closes the loop (self-biases the whole amplifier) while being AC-open
  (negligible admittance at any swept frequency vs. the gate capacitance it
  competes with), letting a single `.ac` sweep of the non-inverting input
  yield the true open-loop transfer function directly as `Vout/Vip`.

## PVT corner grid

`typical, ff, ss, fs, sf` (DR-0001-ratified MOS corner grid) × −40/27/125 °C
— 15 points per run, at the fixed nominal 3.3 V supply (no ±10 % supply
sweep in this pass). See `sim/gm-id-characterization/corners/README.md` for
the grid's own derivation; DR-0001 ratified it for reuse by future
PVT-cornered testbenches, which this experiment is the first to do.

## Cold-start: reproducing every record

Requires `ngspice` and the pinned gf180mcu PDK revision, plus Python 3 with
`numpy` and `matplotlib`.

**Pinned PDK revision**: gf180mcu (`gf180mcuD` variant) at open_pdks commit
`c6d73a35f524070e85faff4a6a9eef49553ebc2b`, installed via
[volare](https://github.com/efabless/volare):

```bash
pip install volare
volare enable --pdk gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b
```

Then, from a clean checkout:

```bash
# Full 15-point PVT sweep -- mints a new append-only record.
python3 sim/gain-gbw-pm/run_gain_gbw_pm.py
# or, via the repo-level one-command driver:
./sim/characterize.sh

# Fast sanity check (typical/27C only, no record written):
python3 sim/gain-gbw-pm/run_gain_gbw_pm.py --smoke
# or:
./sim/selftest.sh
```

The full run mints a new `<record-id>` (`<YYYYMMDD-HHMMSS>-<git-sha>`), runs
the 15-point corner grid, and writes:

- `corners/<record-id>/<corner>_<temp>c.{log,dat}` — raw ngspice transcripts
  and `wrdata` (frequency, real, imaginary) output, one pair per PVT point;
- `netlist-snapshots/<record-id>.spice` — a frozen copy of
  `testbench/tb_gain_gbw_pm.spice`, the DUT fragment used;
- `records/<record-id>.md` — the append-only summary record (a
  corner/temperature table of DC gain / GBW / phase margin, a sanity flag
  per point, and a link to the Bode plot below);
- `records/<record-id>-plots/bode_typical_27c.png` — magnitude and phase
  vs. frequency at the nominal (`typical`, 27 °C) corner, with the extracted
  GBW marked.

PDK resolution order (first hit wins): `$GF180_PDK_PATH` (a gf180mcu variant
directory containing `libs.tech/`), else `$PDK_ROOT` (+ `$PDK`, default
`gf180mcuD`), else the default volare install path `~/.volare/gf180mcuD`.

Re-running mints a new record rather than overwriting the previous one —
`sim/`'s append-only evidence convention: nothing under `corners/`,
`netlist-snapshots/` or `records/` is ever edited or deleted, only added to.

## One-command driver

`sim/characterize.sh` (full PVT sweep, mints a record) and `sim/selftest.sh`
(fast `--smoke` check, no record written) live at the `sim/` root, mirroring
`gf180-comparator`'s `characterize.sh`/`selftest.sh` split — chosen over a
per-testbench-only script so the pattern is already in place at the `sim/`
root for the next testbench to extend, even though this experiment is
currently the only one they drive (`sim/gm-id-characterization/` predates
this pattern and is still run directly per its own README).

## What this feeds

- `spec/target-spec.md` §2's `Open-loop DC gain`, `GBW` and `Phase margin`
  rows — all currently `[TBD]` or `[P]` (phase margin only). This study is
  the first evidence against any of the three, but does **not** flip any
  tag itself (ratification is a separate, future decision-record issue,
  per this issue's own scope, same convention
  `sim/gm-id-characterization/README.md` follows for its own contribution
  to `target-spec.md`).
- The future schematic-entry and sizing-pass issues — this experiment
  establishes the `sim/<name>/` structure, harness shape, and one-command
  driver pattern (`characterize.sh`/`selftest.sh`) that those issues'
  eventual real testbenches can reuse directly, once a real `design/`
  schematic and a gm/ID-driven sizing pass exist to simulate.
- The gap-to-T1 tracker, [#7](https://github.com/2AMLogic/gf180-opamp/issues/7),
  items 5, 9 and 11 ("first spec-row testbench... with a one-command
  driver").

## Design notes / limitations

- **Provisional netlist, not a sizing pass.** Device widths, lengths and
  bias currents were chosen to converge to sane small-signal behaviour, not
  derived from a gm/ID-driven sizing methodology against a target gain/
  bandwidth/power point. A future sizing issue should supersede this
  netlist entirely once `design/` has a real schematic to simulate.
- **Ideal bias current sources.** `Itail` and `Ibias2` are SPICE `I`
  elements, not a real mirror/reference network — see
  `testbench/tb_gain_gbw_pm.spice`'s header. This is a deliberate
  simplification to factor out bias-generator design; it also means this
  testbench cannot yet speak to quiescent power (a future spec row) since
  no real bias-generator current is modeled.
- **No supply-voltage corner.** Only the nominal 3.3 V is simulated; the
  ±10 % supply axis (`spec/target-spec.md` §1) is not yet exercised by this
  testbench.
- **"DC gain" is the swept response's peak magnitude, not a literal `f→0`
  sample.** This netlist's inverting-input gate capacitance is small enough
  (short-channel input pair) that the "big resistor" feedback trick's own
  low-frequency artifact (an R-C zero from `Rfb` loading that small gate
  capacitance) sits within a few hundred Hz of DC rather than far below it
  — visible as the low-frequency rise-then-peak shape in the Bode plot
  before the real single-pole roll-off. The *peak* value was verified to
  converge (to within ~0.3 dB, scanning `Rfb` from `1e13`–`1e20` Ω) and is
  reported as "open-loop DC gain"; GBW and phase margin are extracted from
  the roll-off region well above the peak, which is insensitive to `Rfb`'s
  exact value. See `run_gain_gbw_pm.py`'s `extract()` docstring and each
  record's own "Extraction method" field.
- **No mismatch/Monte Carlo, no noise, no CMRR/PSRR.** This experiment is
  scoped to the single AC sweep that yields gain/GBW/PM together; every
  other classic row (`CLAUDE.md`'s list) needs its own future testbench.
- **Fresh implementation, not vendored from any sibling repo's harness.**
  Mirrors `sim/gm-id-characterization/run_gmid.py`'s structure (PDK
  discovery code is copied, not imported, keeping each experiment
  self-contained per that study's own convention) rather than depending on
  a shared harness library.
