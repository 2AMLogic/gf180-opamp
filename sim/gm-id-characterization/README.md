# `sim/gm-id-characterization/` — gf180mcu 3.3 V MOS gm/ID sweep

The first concrete engineering task for this block (`spec/porting-plan.md`
§4, `CLAUDE.md`'s "gm/ID first" ordering, issue #10). Characterizes
gf180mcu's 3.3 V MOS devices (`nfet_03v3`, `pfet_03v3`) via ngspice —
gm/ID, gm/gds (intrinsic gain) and fT vs a threshold-referenced overdrive
(`Vov = Vgs − Vth(op)`) across five channel lengths and the PDK's five MOS
process corners at three temperatures — so a future topology/sizing pass
(`spec/porting-plan.md` §4's first two open bullets) can be done from the
repo alone, without re-deriving device numbers.

**This is measured device data, not a spec verdict.** `spec/target-spec.md`
is DRAFT and every performance row is still `[TBD]` — nothing here claims a
pass/fail against a ratified number (see each record's "Claim" field).

## What was measured

- **Devices**: `nfet_03v3`, `pfet_03v3` (the block's 3.3 V-primary flavor).
- **Geometry**: W = 10 µm fixed; L ∈ {0.28, 0.5, 1, 2, 4} µm — the model's
  minimum length (`lmin` in `sm141064.ngspice`) to ~14× minimum.
- **Bias**: a single DC gate-voltage sweep (0.01–3.3 V, 0.01 V step) at a
  fixed, representative Vds = 1.65 V (half of the nominal 3.3 V supply) and
  Vsb = 0 — the operating region a two-stage amplifier's gain/input-pair
  devices actually sit in.
- **Extraction**: BSIM4's own operating-point parameters (`gm`, `gds`, `id`,
  `cgg`, `vth`) are read directly off the model at every sweep step via
  ngspice's `@m.<inst>.m0[<param>]` accessor — no finite-difference
  derivative, no separate AC run. `Vov` uses the per-point `vth` BSIM4
  itself reports (DIBL/body-effect corrected), not a fixed constant-current
  threshold.
- **Derived**: `gm/ID = gm/id`, intrinsic gain `gm/gds`, and
  `fT = gm / (2·π·cgg)`.

## PVT corner grid

`typical, ff, ss, fs, sf` (gf180mcu's own top-level MOS `.LIB` corner
bundles; `fs` = fast NMOS / slow PMOS, `sf` = the reverse) × −40/27/125 °C —
15 points per run. See `corners/README.md` for the full derivation and the
supply-axis subset justification. This is the grid `spec/target-spec.md` §1
names as `gf180-bandgap`'s likely template for this block's corner-grid
row; running it here is what turns that `[TBD]` into a `[P]` (see
"What this feeds", below).

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
python3 sim/gm-id-characterization/run_gmid.py
```

This mints a new `<record-id>` (`<YYYYMMDD-HHMMSS>-<git-sha>`), runs the
full 15-point corner grid, and writes:

- `corners/<record-id>/<corner>_<temp>c_nosupply.{log,dat}` — raw ngspice
  transcripts and `wrdata` output, one pair per PVT point;
- `netlist-snapshots/<record-id>.spice` — a frozen copy of
  `testbench/tb_gmid.spice`, the DUT fragment used;
- `records/<record-id>.md` — the append-only summary record (gm/ID, gm/gds,
  fT tables at representative overdrives, a corner/temperature spread
  table, and links to the plots below);
- `records/<record-id>-plots/*.png` — gm/ID, gm/gds and fT vs Vov plots per
  device, overlaid across all five swept lengths, at the nominal
  (`typical`, 27 °C) corner — the summary artifact per this issue's
  Acceptance Criteria #2.

PDK resolution order (first hit wins): `$GF180_PDK_PATH` (a gf180mcu variant
directory containing `libs.tech/`), else `$PDK_ROOT` (+ `$PDK`, default
`gf180mcuD`), else the default volare install path `~/.volare/gf180mcuD`.

Re-running mints a new record rather than overwriting the previous one —
`sim/`'s append-only evidence convention (`spec/porting-plan.md` §1):
nothing under `corners/`, `netlist-snapshots/` or `records/` is ever edited
or deleted, only added to.

## What this feeds

- `spec/target-spec.md` §1's **Corner grid** row (currently `[TBD]`) — this
  study is the first evidence that gf180mcu's `typical/ff/ss/fs/sf` MOS
  corner set runs cleanly for this block's device flavor; `target-spec.md`
  has been updated to `[P]` citing this study (ratifying the spec itself
  remains a separate, future decision-record issue, per this issue's own
  scope).
- The future **topology decision** and **CL choice**
  (`spec/porting-plan.md` §4's first two open bullets, explicitly out of
  scope for this issue) — both need gm/ID-vs-overdrive data for the
  candidate devices to size input-pair, mirror and output-stage transistors
  against a target gain/bandwidth/power point.
- The gap-to-T1 tracker, [#7](https://github.com/2AMLogic/gf180-opamp/issues/7),
  item 9 ("Testbenches shipped").

## Design notes / limitations

- This is a **device-level** characterization, not a circuit-level one: no
  supply rail exists to sweep (see `corners/README.md`), and no mismatch/
  Monte Carlo distribution is measured here (a future
  `sim/device-mos-mismatch/`-style study, mirroring `gf180-bandgap`'s
  convention, would own that).
- `Vov` is defined against BSIM4's own per-point `vth`, which already
  includes DIBL and body-effect at the simulated bias — this is a more
  faithful "how much overdrive do I actually have" number than a fixed
  constant-current threshold, but it is *not* the same convention
  `gf180-bandgap/sim/device-mos-vth` uses for its own |Vth| corner table
  (constant-current, diode-connected). The two are complementary, not
  interchangeable: that study answers "what does this device's Vth measure
  as", this one answers "what gm/ID do I get at a given overdrive above
  wherever Vth is right now".
- Fresh implementation, not vendored from `gf180-bandgap/sim/harness`: this
  script has no dependency on that repo's harness library (PDK discovery,
  corner grid, record formatting are all reimplemented locally, scoped to
  this one experiment) — see issue #10's own guidance on this point.
