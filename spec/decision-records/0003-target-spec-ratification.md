# 0003: Target-spec ratification (partial) — targets ratified ahead of measurement, residuals explicitly open

- **Status**: `proposed` — **but read this line through**: per the
  ratification-via-PR standing policy
  ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)), the
  operator's approval of the PR that carries this record **is the
  ratification act** — in this repo's pipeline, the two-key mechanism
  evaluates it as Judge review plus Champion/operator merge (issue #24's
  verified framing). **This line will still read `proposed` after that
  approval lands**, because no automation rewrites a merged PR body — that
  is the known wart issue #24 names. So that a later reader is not misled:
  **as of the merge commit of the PR carrying this record, everything in
  this record's Decision section is RATIFIED**, by construction of the
  mechanism, not by any later edit. The header word `proposed` describes
  this text's state *while under review*, nothing more.
- **Date**: 2026-09-21
- **Decided by**: Builder agent, issue #24 (per-row dispositions + this
  record); the operator, whose approval of the carrying PR is the
  ratification act (ratification-via-PR policy, 2am#357)
- **Related**: `spec/target-spec.md` (this record flips its `Status` field
  and disposes every row), [0001-topology-and-cl.md](0001-topology-and-cl.md)
  and [0002-performance-target-bounds.md](0002-performance-target-bounds.md)
  (both carried into force with this act), [0002-target-spec-ratification.md
  in the `sg13g2-opamp` three-foundry twin](https://github.com/2AMLogic/sg13g2-opamp/blob/main/spec/decision-records/0002-target-spec-ratification.md)
  (partial-ratification shape this record mirrors), [DR-002 of
  `sky130-comparator`](https://github.com/2AMLogic/sky130-comparator)
  (merged via its PR #29, 2026-09-16 — the "ratified vs explicitly-open,
  never silent" worked example issue #24 cites),
  [`design/opamp_sizing.md`](../../design/opamp_sizing.md) (the committed
  sizing pass whose design point grounds the three new bounds below),
  `sim/gm-id-characterization/records/20260909-052956-79c6a45.md` (device
  evidence), `sim/gain-gbw-pm/records/20260915-221407-1bb9a74.md` (the
  provisional sweep this record explicitly does **not** cite — see
  Context), [gap-to-T1 tracker #7](https://github.com/2AMLogic/gf180-opamp/issues/7)
  (item 5; post-merge comment per "Post-merge follow-through"),
  [`manifests/gf180-opamp.signoff.json`](../../manifests/gf180-opamp.signoff.json)
  (item 5 row of the verdict of record)

## Context

`spec/target-spec.md` has carried `Status: DRAFT` since its 2026-09-06
bootstrap. Its ratification is gap-to-T1 tracker #7 item 5's stated
prerequisite — *"verdicts against a draft spec are provisional by
construction"* — and the manifest's machine-rendered item-5 row
(`manifests/gf180-opamp.signoff.json`, landed by #23 / PR #25) carries
the same note. Two decision records already exist as ratified-input
candidates: DR-1 (topology, corner grid, `CL = 2 pF`, 2026-09-09) and DR-2
(DC-gain and output-swing bounds, offset statistical basis, 2026-09-15),
each explicitly deferring the whole-table ratification act to "a future
spec-ratification issue" — this record is that act.

**Exactly what simulation evidence exists in this repo today**, so the
target-vs-met split below is checkable rather than asserted:

1. `sim/gm-id-characterization/` (issue #10 / PR #11) — a real, committed,
   five-corner × three-temperature bare-device sweep. Device-level
   evidence, not circuit-level.
2. `design/check_dc_op.py` (issue #17 / PR #21) — a **nominal**, one-corner
   (`typical`, 27 °C, VDD = 3.3 V) DC operating-point check of the sized
   schematic. No AC, no transient, no PVT.
3. `sim/gain-gbw-pm/` (issue #19 / PR #22) — a
   15-point PVT sweep of open-loop DC gain / GBW / phase margin whose own
   record and README **explicitly disclaim it** as a provisional,
   smoke-level result from a **hand-built** netlist authored *before* the
   real schematic landed, and disconnected from the sized
   `design/opamp_two_stage.sch`. **This ratification does not cite that
   sweep toward meeting any row.** It is named here only to preempt the
   "but a PVT sweep exists" misreading.

**What grounds the three new bounds below** is the committed sizing pass
(issue #17 / PR #21, `design/opamp_sizing.md`), which chose the design
point — `IBIAS = 10 µA` (external), tail 10 µA, output branch 60 µA,
`CC = 0.619 pF` as drawn, `RZ = 2.11 kΩ` — and published its predictions
(§6): GBW 15.5 MHz, slew 16.2 V/µs, open-loop DC gain 97.7 dB predicted /
95.8 dB as-simulated DC-OP product, quiescent 264 µW predicted / 266.3 µW
as-simulated (80.70 µA × 3.3 V). Each of those is a **prediction from
gm/ID data and textbook relations, not a measurement** (the sizing
document's own words), and this record treats them exactly that way: as
the grounding for a *target*, never as evidence of *met*.

No bound below relaxes anything. This is the table's **first**
ratification; every ratified number is set at or below the evidence
available to it, and the guardrail stated in this repo's `CLAUDE.md` —
**agents do not relax a ratified spec to make results pass** — applies to
every one of them from the moment this act lands. A future measured
shortfall against any bound below is a design failure to fix (or a
tradeoff to take to the operator in a **superseding decision record**,
named as such), never a silent or bundled-down bound change.

## Decision

Ratify the table **partially**: every row below gets an explicit verdict —
**RATIFIED, as a target** (never "met"; no circuit-level PVT evidence
exists) — or **explicitly OPEN**, with the reason named. No row is left
silent. The split mirrors the partial-ratification precedent of the
`sg13g2-opamp` twin's DR-0002 (its measured rows ratified, its offset
statistical basis, mismatch-inclusive CMRR, and Area registered as
residuals) and `sky130-comparator`'s DR-002 (three ratified, two open,
*"explicitly, not silently"*).

### (a) §1 operating conditions — RATIFIED, targets/conditions of operation

| `spec/target-spec.md` §1 row | Verdict | Grounds (cited) |
|---|---|---|
| Supply voltage 3.3 V ±10% (2.97–3.63 V) | **RATIFIED [DR-3]** | The fleet-wide convention for this PDK's wave-1 targets — `gf180-bandgap` and `gf180-ldo` (same PDK) both carry ratified identical rows — and this block's own charter (`CLAUDE.md` "3.3 V primary"); every committed gf180mcu device argument in this repo is stated at that rail. |
| Supply voltage, 5 V (stretch) | **Deliberately NOT OPENED** — the sole `[P]` left in the table post-ratification | Ratifying the table ratifies the *closure*: `CLAUDE.md` opens the 5 V row only via its own decision record, never alongside this pass. The row stays named-not-opened, exactly as the twin's HV row does post-ratification. |
| Operating temperature −40…+125 °C | **RATIFIED [DR-3]** | Fleet-wide convention for commercial-grade parts on this PDK (`gf180-bandgap`, `gf180-temp-por`), and — stronger — the committed gm/ID study actually exercised exactly −40/27/125 °C across all five corners with bounded, sane behavior (the same evidence class DR-1 used to ratify the corner grid). |
| Corner grid `typical, ff, ss, fs, sf` | **RATIFIED — carried into force [DR-1]** | DR-1 ratified this row itself on the study's spread data; DR-3 is the "future spec-ratification issue" DR-1's status line has been waiting for. |
| Load capacitance `CL = 2 pF` | **RATIFIED — carried into force [DR-1]** | Same: DR-1 adopted it from the `sg13g2-opamp` twin for cross-PDK comparability; ratification act closes it. |

### (b) §2 performance rows — RATIFIED as targets, none met

| `spec/target-spec.md` §2 row | Bound ratified | Grounds (cited) |
|---|---|---|
| Open-loop DC gain | **≥ 60 dB**, stretch ≥ 70 dB `[DR-2 → carried by DR-3]` | DR-2's argued bound: committed gm/ID device data cross-derated by the `sg13g2-opamp` twin's own estimate-to-measurement haircut (95.8 dB ideal product → ≈65.6–73.0 dB plausible band), independently corroborated by `sky130-opamp`'s parallel-resistance estimate landing in the same 60–65 dB band. Target sits below the entire derated band. |
| GBW (into `CL = 2 pF` `[DR-1]`) | **≥ 10 MHz** `[DR-3]` | The sizing pass's committed design point: `gm1 = 60.3 µS` (as-simulated 58.64 µS) with `CC = 0.619 pF` gives `gm1/(2π·CC)` ≈ 15.5 MHz predicted / 15.0 MHz as-simulated nominal (design/opamp_sizing.md §4, §6). Target set ≈ 35% below the prediction to absorb the un-quantified PVT degradation; twin precedents: `sky130-opamp`'s sizing example ≈ 16 MHz into the same CL; `sg13g2-opamp`'s ratified-measured ≥ 4.74 MHz on the much lower-headroom 1.2 V rail (cross-PDK comparability noted, no number copied). |
| Phase margin (at GBW, same CL) | **≥ 60°** `[DR-3]` | Fleet convention for this topology class: the `sg13g2-opamp` twin ratified the identical ≥ 60° bound (measured worst 76.4°); `sky130-opamp` proposes ≥ 60°; and this repo's own committed sizing pass **designs to it** (nulling resistor `RZ`, non-dominant pole ≥ 2.2× GBW, 40.6 MHz = 2.6× GBW at the chosen point). See (d) for what this does **not** ratify. |
| Slew rate | **≥ 10 V/µs** `[DR-3]` | Sizing-pass prediction `Itail/CC = 10 µA / 0.619 pF ≈ 16.2 V/µs` (§6); target ≈ 38% below it. Twin precedents: `sky130-opamp` ≈ 20 V/µs sizing example; `sg13g2-opamp` ratified-measured ≥ 7.51 V/µs (worse edge). |
| Output swing | **≥ 2.3 Vpp** (≈78% of VDD,min), stretch ≥ 2.6 Vpp (≈88%) `[DR-2 → carried by DR-3]` | DR-2's Vov-headroom arithmetic from the committed sweep (worst row: 2.37 V at 2 × 300 mV overdrive, worst-case-low rail), cross-fleet corroborated at ≈ 79–86% by the twins' own estimates. |
| Quiescent power | **≤ 350 µW** worst-case corner `[DR-3]` | As-simulated nominal 80.70 µA × 3.3 V = 266.3 µW; worst-case edge derived by applying the same-topology twin's **measured** corner-spread ratio (`sg13g2-opamp`, 99.9→119.7 µA ≈ ×1.2, `sim/open-loop-ac/`) to the current and then the max rail: 80.70 µA × 1.2 ≈ 96.8 µA × 3.63 V ≈ 351 µW; target set just under the derived edge, matching DR-2's below-the-conservative-row convention. |

Every RATIFIED cell above is a **target to design toward**. None is met:
the only AC/transient-shaped data in `sim/` is the provisional hand-built
`sim/gain-gbw-pm/` sweep, which this record does not cite (Context ¶3),
and the DC-OP check is a single nominal corner with no AC analysis. A
future pass/fail verdict on any of these rows requires the PVT-cornered
testbench suite of tracker #7 item 5 — which is, precisely, the reason
issue #24 ratifies the targets *now*: `CLAUDE.md`'s directive is to set
targets from device data and external precedent **before** simulation
exists to be tempted by.

### (c) Why targets, not measurements, are the ratified currency of this pass

The `sg13g2-opamp` twin's DR-0002 could ratify **measured worst-case**
bounds because 45 benches existed when it ran. This repo has none (its
one PVT sweep is the disclaimed provisional of Context ¶3). Issue #24's
scope is explicit that this gap does **not** block ratification: *"for
any row with no measured evidence, ratify it as a **target** and say
so. Do not ratify a row as met on a placeholder."* Every bound in (b)
therefore binds as a design-to target with an explicit not-met status in
the spec row itself, and the target-vs-met distinction is carried in the
`Status` vocabulary of the updated table: future PVT results will read
against these numbers as pass/fail, and future re-ratifications will
supersede them **with the operator in the loop**, never silently.

### (d) Phase margin — the bound is ratified; the conditional 45° escape is NOT

The current row's stretch cell reads "≥ 45° at the FF/hot corner if 60°
is unreachable there". That conditional is a **pre-authorized
relaxation** — it authorizes, in the spec itself, weakening ≥ 60° to 45°
if the measurement someday misses — which is exactly the
"relax-to-make-results-pass" shape the mechanism forbids, baked in
before any measurement exists to be tempted by. **Ratified: ≥ 60°, flat,
no conditional escape.** If PVT AC data later shows 60° genuinely
unreachable at a corner, the remedy is a superseding decision record
presenting the tradeoff to the operator (the `sky130-opamp` twin carries
the same conditional in its still-DRAFT table; nothing there forces this
table to ratify its own escape hatch).

One committed-repository sentence needs answering head-on:
`design/opamp_sizing.md` §8 says of `RZ` (a fixed poly resistor that does
not track `1/gm6` over PVT): *"Quantify before ratifying a phase-margin
number."* This record complies by scope: it ratifies **only the bound**
(≥ 60°, the fleet-wide design-to number), not any predicted or achieved
phase-margin *value* — no phase-margin figure is claimed, predicted, or
recorded as expected anywhere in the ratified table. The
RZ-tracking quantification the sizing pass calls for remains exactly
what it named: the row's **verification** obligation (tracker #7 item
5), and the sizing pass itself names the remedy should that
verification fail: replacing the fixed `RZ` with a triode-PMOS resistor
that tracks `1/gm6` is "the standard remedy if the PVT sweep shows it
is needed" (opamp_sizing.md §4.5). A measured shortfall triggers that
redesign path — never a weakening of the 60° bound.

### (e) Residual register — explicitly OPEN, kept open here

Each row below keeps `[TBD]` in the table, now carrying an explicit
open-verdict note instead of a bare deferral:

- **(e1) Input-referred noise — OPEN.** The bias-current half of DR-2's
  named gap is discharged (the sizing pass chose `gm1`), but a ratifiable
  bound is a *full-band figure*: no flicker (`1/f`) model or coefficient
  exists in this repo's committed device data (the gm/ID sweep is a DC
  operating-point characterization with no AC/noise analysis), and the
  integration band has not been chosen. Band precedent for the eventual
  noise bench: the `sg13g2-opamp` twin ratified 100 Hz–1 MHz with the
  band chosen and defended by that bench itself. A thermal-floor-only
  number here would not rate the full-band row (DR-2 §(c)'s own
  reasoning, still standing).
- **(e2) Input-referred offset — numeric target OPEN; statistical basis
  carried into force `[DR-2]`.** `3σ, mismatch MC N≥300 + process
  corners` is ratified with DR-2; the numeric 3σ figure needs the MC
  mismatch pass that does not exist (`sim/gm-id-characterization/` is
  explicitly not a mismatch characterization; `design/opamp_sizing.md` §8
  records the basis as not-run). Mirrors the twin's residual (b).
- **(e3) CMRR — OPEN.** Needs a schematic and a common-mode AC testbench
  (committed device sweep has no shared tail node or CM stimulus).
  DR-2 §(c), unchanged.
- **(e4) PSRR — OPEN.** Needs a schematic and a supply-injection AC
  testbench; the committed device sweep has no supply-voltage axis (its
  own record states this). DR-2 §(c), unchanged.
- **(e5) Area — OPEN.** A post-layout quantity; `layout/` holds only a
  placeholder. Not PVT-derivable by construction. Mirrors the twin's
  residual (a).

## Alternatives considered

- **Ratify only the `[DR-2]` rows (DC gain, output swing) and the phase
  margin verdict, leaving GBW/slew/power `[TBD]`.** Rejected. DR-2 left
  those rows `[TBD]` because *no sizing existed to read a bound from* ("a
  GBW/slew/power estimate would require this record to originate a
  sizing choice no prior record in this repo has made — better done as,
  and left to, the dedicated sizing pass"). That sizing pass has since
  landed (issue #17 / PR #21, `design/opamp_sizing.md`), committed a
  concrete design point, and published its predictions; reading bounds
  off it now requires **no new sizing decision by this record** — which
  was DR-2's entire objection. Leaving these rows open would also
  under-serve the issue's stated purpose: tracker items 6/7/8 grade
  against spec rows, and more ratified target rows means more gradeable
  rows.
- **Take the targets from `sim/gain-gbw-pm`'s existing PVT sweep
  numbers.** Rejected. That sweep's own record and README disclaim it as
  provisional, smoke-level, and disconnected from the sized schematic —
  citing it in a ratification would launder a disclaimed result into an
  authoritative one (the exact "false one" issue #24 warns against).
- **Keep the conditional 45° stretch.** Rejected — see (d); it is a
  pre-authorized relaxation.
- **Wait for the PVT quantification `design/opamp_sizing.md` asks for
  before ratifying the phase-margin bound.** Rejected for the bound
  itself: issue #24's guardrail says set targets from device data and
  external precedent *now*, before simulation exists to be tempted by —
  treating the sizing pass's caution as a blocker on the bound would
  invert the ordering the issue fixes. The caution governs the claiming
  of PM *values*, which this record does not do; see (d).
- **Ratify noise/offset/CMRR/PSRR/area targets by analogy to the twins'
  numbers.** Rejected — DR-2's own reasoning holds: a bandgap loop's
  PSRR, another PDK's noise floor, and an un-run mismatch study are
  "guesses dressed as citations". These rows stay explicitly OPEN.
- **Flip the whole table to RATIFIED.** Rejected — a false ratification;
  the five residuals of (e) name their own missing evidence and stay
  open, partial beats false (the worked example issue #24 cites).

## Consequences

- `spec/target-spec.md`'s `Status` field flips **DRAFT → RATIFIED
  (partial)**, with the per-row ratified-vs-open split in the table
  itself; the ratification act is the operator's approval of the PR
  carrying this record (this header's Status paragraph is written so the
  post-merge `proposed`-line wart cannot mislead — issue #24's known-wart
  note addressed explicitly).
- DR-1 and DR-2 are **carried into force** with the same act: each
  record's status line gains an acted-on-by pointer. `[DR-1]`-tagged
  rows (corner grid, `CL`) and `[DR-2]`-tagged bounds (DC gain, output
  swing, offset statistical basis) become ratified table content, no
  longer pending-proposal input.
- §1's `[P]` VDD and temperature rows become `[DR-3]`; the **sole
  remaining `[P]`** in the table is the deliberately un-opened 5 V
  stretch row (matching the twin's post-ratification shape).
- Tracker #7 item 5's spec-ratification prerequisite is discharged (see
  "Post-merge follow-through" — the comment lands on #7 after the merge,
  because recording "the spec is ratified" before the approval ratifying
  it would be writing history ahead of its own act). Item 5 itself stays
  unmet: the remaining gap, exactly as the tracker's 2026-09-15
  verified-correction entry words it, is "spec ratification *and* a PVT
  sim of the actual committed/sized schematic" — this closes the first
  half; the second half needs the real-schematic-derived testbench the
  provisional sweep is not.
- Tracker items 6/7/8 (MC, post-layout, characterization report) can now
  grade against ratified target rows — the reason this issue was
  gating that block in the first place.
- **The guardrail carries forward verbatim and in force** (per
  `CLAUDE.md`): agents do not relax a ratified spec to make results
  pass. No future measurement, sweep, or milestone may weaken any bound
  ratified above; a measured shortfall is a design fix or a superseding
  decision record taken to the operator, and a superseding record must be
  *named* as superseding.
- The bounds ratified here are, and stay, engineering targets for this
  canary block — `CL` stays 2 pF, the 5 V stretch row stays closed, and
  no constraint lands here that the committed topology/sizing evidence
  did not argue.

## Post-merge follow-through

- The PR that carries this record posts an outcome comment on tracker #7
  recording: the ratified-spec prerequisite of item 5 is discharged by
  this act; the hand-maintained list's old "still-unratified spec"
  reason is retired; the remaining item-5 gap is the PVT sim of the
  actual sized schematic; items 6/7/8 now have ratified target rows to
  grade against. The comment is posted at merge time, not in the PR
  body, for the reason stated in Consequences ¶3.
- DR-1's and DR-2's status-line pointers (this record) are the durable
  in-repo record of the carried-into-force act; the tracker comment is
  the durable on-forge record.

## Out of scope

No circuit, schematic, netlist, or simulation change of any kind lands
with this record. No sizing decision originates here — the three new
bounds read the committed sizing pass; they do not extend it. The
`manifests/` verdict-of-record and the committed `klt` record are not
touched (a spec status is not an evidence envelope; item 5 will re-render
`unmet`/`no_evidence` correctly until PVT evidence exists — and CI
enforces its byte-for-byte reproduction). The 5 V stretch row is not
opened. No bound is relaxed — the conditional 45° phase-margin escape is
removed because it *is* a relaxation, not to replace it with one. The
tracker body is not edited (comment only, post-merge). Ratifying the
`[TBD]`-to-target rows' eventual *met* status is the PVT testbench
suite's job (tracker #7 item 5's remaining half), not this record's.
