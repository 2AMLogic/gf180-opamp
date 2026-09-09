# 0001: Two-stage topology (input-pair polarity, output stage, cascode) and CL target

- **Status**: proposed (input to a future spec-ratification issue; this
  repo has no ratified spec yet — `spec/target-spec.md` itself is still
  DRAFT, matching the identical "proposed, not ratified" posture of
  `sg13g2-opamp`'s own DR-0001)
- **Date**: 2026-09-09
- **Decided by**: Builder agent, issue #12
- **Related**: #10 / PR #11 (the gm/ID device-characterization study this
  record cites), `spec/target-spec.md` (§1 `Corner grid`, `Load capacitance,
  CL`), `spec/porting-plan.md` §4 ("Open items and next steps"), gap-to-T1
  tracker #7 (item 5), `sg13g2-opamp`'s DR-0001 (the three-foundry twin's
  precedent for this exact decision)

## Context

`spec/porting-plan.md` §4 named two open items ahead of any sizing work on
this block: the two-stage Miller-compensated topology's input-pair
polarity, output-stage class, and cascode-or-not; and the load-capacitance
(`CL`) target the GBW/phase-margin rows in `spec/target-spec.md` are stated
"into." Both were explicitly deferred by issue #10 (its own "Out of scope"
section names them as the next consumer of the gm/ID data), which committed
the prerequisite device-characterization record this decision now draws on:
`sim/gm-id-characterization/records/20260909-052956-79c6a45.md` (`nfet_03v3`
/ `pfet_03v3`, W = 10 µm, Vds = 1.65 V, five lengths 0.28–4 µm, five process
corners `typical/ff/ss/fs/sf`, three temperatures −40/27/125 °C).

Every figure quoted below is a literal number from that record's tables,
re-grepped against the source file immediately before this record was
committed — grep it directly (`Vov=`, the device name, and the length) to
reproduce any number cited here. Unless stated otherwise, all figures are
from the `typical` corner at 27 °C, the record's headline table set.

The three-foundry twin `sg13g2-opamp`'s DR-0001 (issue #6 → PR #8, merged
2026-09-09) made this same decision for IHP SG13G2 on a 1.2 V ±10% rail
(1.08–1.32 V worst-case low), reaching NMOS input / PMOS output / no
cascode / `CL = 2 pF`. **That reasoning does not mechanically transfer.**
This repo's own gm/ID data shows a different relative device picture than
SG13G2's PSP103 data did (see Decision below — gf180mcu's PMOS is not
uniformly weaker than its NMOS the way SG13G2's PMOS was), and this block's
primary rail is 3.3 V ±10% (2.97–3.63 V worst-case low, per
`spec/target-spec.md` §1) rather than 1.2 V, so the headroom argument the
twin used to reject a cascode does not carry over unchanged either. Each
choice below is argued independently from gf180mcu's own numbers.

## Decision

**Input-pair polarity: NMOS.** At a representative short-channel input-pair
bias point (`L = 0.28 µm`, `typical`, `Vov = 200 mV`), `nfet_03v3` reaches
`fT ≈ 25.12 GHz` versus `pfet_03v3`'s `fT ≈ 5.73 GHz` at the identical
length/corner/overdrive — NMOS is **≈4.4x faster**. This is a substantially
larger polarity gap than SG13G2 showed for the equivalent comparison
(≈1.8x) and is the deciding factor here: the input pair's own parasitic
capacitance sets the first stage's non-dominant pole (via the mirror node
it feeds), and a faster device buys phase-margin headroom for a fixed
transconductance. Notably, gf180mcu's relative device picture is *not*
uniformly NMOS-favoring the way this fT gap alone might suggest — at the
same bias point `pfet_03v3` actually shows the higher current efficiency
(`gm/ID = 8.09` vs `nfet_03v3`'s `6.93`) and the higher intrinsic gain
(`gm/gds = 51.3` vs `29.0`) — so this is not a case of PMOS being globally
weaker than NMOS on this PDK (contrast the SG13G2 twin, where PMOS trailed
NMOS on every cited axis). The input-pair choice is made on `fT` alone,
because at 0.28 µm the input devices are sized for speed, not for
standalone intrinsic gain — the output stage below is what supplies the
bulk of total DC gain, so the input pair is free to be chosen for its
fastest device.

**Output stage: single-ended, Class-A common-source, PMOS gain device.**
The second (output) gain stage uses a PMOS common-source transistor,
loaded by an NMOS constant-current sink — the canonical two-stage
Miller-compensated shape `CLAUDE.md` names, and the same output-device
polarity the SG13G2 twin chose, though for a distinct, gf180mcu-specific
reason: this PDK's `pfet_03v3` intrinsic gain (`gm/gds`) grows faster with
channel length than `nfet_03v3`'s does, and at the moderate-to-long
lengths an output gain stage would use, the gap is large. At `L = 4 µm`,
`Vov = 200 mV`, `pfet_03v3` reaches `gm/gds = 2122.7` versus `nfet_03v3`'s
`817.1` — PMOS is **≈2.6x** higher. The same ordering holds at `L = 1 µm`,
`Vov = 200 mV` (`pfet_03v3` `gm/gds = 573.3` vs `nfet_03v3` `372.2`, ≈1.5x)
and across the full `Vov = 50–300 mV` sweep at both lengths — this is not a
single-point artifact. Pairing the higher-gm/gds device as the output
stage's gain transistor, driven from the NMOS input pair's single-ended
first-stage output, lets the second stage carry the bulk of total loop
gain from one non-cascoded device (see cascode decision below). This is
not a bias/sizing commitment (device widths, lengths, and currents remain
out of scope, see below) — only the device-polarity and stage-class
choice.

**Cascode: not used (non-cascoded two-stage).** The SG13G2 twin rejected a
cascode primarily on rail-headroom grounds (a 1.08 V worst-case low rail
leaves little Vgs+Vds,sat budget to stack a cascode device on top of the
input pair or output device). That argument does not transfer here: this
block's 3.3 V ±10% rail (2.97 V worst-case low, `spec/target-spec.md` §1)
has roughly 2.75x the worst-case headroom of SG13G2's 1.08 V rail, so a
cascode would not be nearly as headroom-constrained on gf180mcu. The
decision here is instead driven by **gain sufficiency, not headroom
scarcity**: an illustrative (non-binding) DC-gain estimate from `gm/gds`
alone, using the short-channel NMOS input pair (`L = 0.28 µm`, `typical`,
`Vov = 200 mV`, `gm/gds = 29.0`, the same row cited in the input-pair
decision above) times the long-channel PMOS output stage
(`L = 4 µm`, `typical`, `Vov = 200 mV`, `gm/gds = 2122.7`, the row cited
above) gives a two-stage product of `29.0 × 2122.7 ≈ 61,558`
(`20·log10(61558) ≈ 95.8 dB`) — well above the canary-block DC-gain range
either twin's own record treated as plausible (the SG13G2 twin's
equivalent estimate reached only ≈68 dB, and needed the cascode question
answered on headroom grounds because its estimate was closer to a typical
target; this record's estimate already clears that bar by more than 25 dB
with room for mirror/tail loading and mismatch to erode it). Because the
non-cascoded output stage alone already supplies ample estimated gain, and
the 3.3 V rail's larger headroom means a cascode is not needed to *rescue*
a gain shortfall, adding one here would only spend swing headroom (against
the still-`[TBD]` output-swing row in `spec/target-spec.md` §2) for a gain
margin this topology does not need. This is a gf180-specific answer, not a
copy of the twin's headroom-scarcity argument — the same conclusion
(non-cascoded) is reached from the opposite side of the tradeoff (gain
surplus rather than headroom scarcity). Nothing in this record forecloses
re-opening a cascode option in a superseding record if a future sizing
pass finds the non-cascoded estimate does not close once mirror/tail
loading and mismatch are included.

**Corner grid: ratified as `typical, ff, ss, fs, sf`.** The gm/ID
characterization study (issue #10) ran exactly this five-corner MOS set
across `nfet_03v3`/`pfet_03v3`, and the process/temperature spread table
in the source record (`gm/ID` at `Vov = 200 mV`, `L = 1 µm`) shows bounded,
physically sensible behavior across every cell of that grid: `nfet_03v3`
ranges `7.73`–`8.40` across all five corners and three temperatures (a
≈9% spread), and `pfet_03v3` ranges `7.81`–`8.90` (a ≈14% spread), with no
anomalous outlier corner. Because the grid has now actually been exercised
against real gf180mcu models and produced sane, boundable numbers (rather
than being a proposal carried by analogy, as `[P]` denoted), this record
ratifies it as the corner set future PVT-cornered testbenches use — the
same step the SG13G2 twin's DR-0001 took for its own corner grid.

**CL target: 2 pF, adopted from the `sg13g2-opamp` twin for cross-PDK
comparability.** `sg13g2-opamp`'s DR-0001 chose `CL = 2 pF` on independent
engineering judgment (a representative moderate load: large enough to
stand in for external test-pad/probe/ESD parasitic capacitance, small
enough to keep GBW/slew targets meaningful for a canary block's bias
budget), noting at the time that "no twin-repo precedent exists yet." That
precedent now exists, and `CLAUDE.md`'s instruction to "keep bench
structure identical across the twins" applies directly to a load-capacitance
choice that has no gf180mcu-specific reason to differ — nothing in the
gm/ID device data argues for a different CL (device fT/gm-gds data
constrains sizing, not the external load choice). Adopting 2 pF here means
GBW, phase margin, and slew-rate figures measured on this block and on
`sg13g2-opamp` will be directly comparable across the two PDKs at the same
external load, which a differing CL choice would foreclose without a
stated reason to trade that comparability away — and this record has none.

## Alternatives considered

- **PMOS input pair.** Rejected. Despite `pfet_03v3` showing higher
  `gm/ID` (8.09 vs 6.93) and higher `gm/gds` (51.3 vs 29.0) than
  `nfet_03v3` at the same short-channel bias point (`L = 0.28 µm`,
  `typical`, `Vov = 200 mV`), `pfet_03v3`'s `fT` at that point
  (`≈5.73 GHz`) is only ≈23% of `nfet_03v3`'s (`≈25.12 GHz`). Since the
  output stage (below) already supplies the bulk of total DC gain, the
  input pair's own intrinsic-gain edge is not the limiting resource — its
  speed is — so the ≈4.4x fT gap dominates the decision even though PMOS
  wins on the other two axes at this bias point.
- **NMOS output-stage gain device** (paired with a PMOS input pair, or a
  same-polarity NMOS-after-NMOS cascade). Rejected. The characterization
  data shows the opposite length-scaling behavior this alternative would
  need: at `L = 1 µm`, `Vov = 200 mV`, `nfet_03v3` `gm/gds` is `372.2`
  versus `pfet_03v3`'s `573.3` (PMOS ≈1.5x higher); at `L = 4 µm`, the gap
  widens to ≈2.6x (`817.1` vs `2122.7`). An NMOS output-stage gain device
  would forfeit exactly the length-dependent intrinsic-gain growth this
  decision relies on to reach adequate DC gain without a cascode.
- **Cascoded (telescopic or folded) two-stage.** Rejected for this record.
  The non-cascoded `gm/gds`-based estimate above (`≈95.8 dB`) already
  clears a canary-block-class DC-gain range with substantial margin before
  mirror/tail loading and mismatch are accounted for, so a cascode is not
  needed to close the gain budget here — unlike a design where the
  non-cascoded estimate is marginal. Because this block's 3.3 V rail also
  has more headroom than SG13G2's 1.2 V rail (so a cascode would not be as
  costly to add if it were needed), this is a genuine gain-sufficiency
  rejection, not a headroom-scarcity one — but the conclusion is still to
  defer the cascode, not adopt it. Nothing forecloses a superseding record
  reopening this if a future sizing pass finds the estimate does not close
  once mirror/tail loading and mismatch are included.
- **Folded-cascode single-stage** (instead of a two-stage Miller topology
  altogether). Rejected — out of scope by construction: `CLAUDE.md`
  mandates a two-stage Miller-compensated topology for this block; this
  record makes decisions within that mandate (input-pair polarity, output
  class, cascode-or-not), not a decision to abandon it.
- **A different CL to reflect this PDK's own device data (e.g. matched to
  gf180mcu's typical `Cgg` at the sizes this study characterized).**
  Rejected. `CL` is an external load choice, not a device property this
  PDK's data constrains one way or another — nothing in the gm/ID, gm/gds,
  or fT tables argues for a gf180mcu-specific CL value. Diverging from the
  twin's 2 pF without a data-driven reason would only cost cross-PDK
  comparability for no offsetting benefit.
- **A larger CL (e.g. 5–10 pF) to stress-test slew rate / output-stage
  drive strength.** Rejected as the primary target, for the same reason
  the twin rejected it: a larger CL is a reasonable *stretch* row for a
  future spec revision once nominal sizing exists, but as the primary
  GBW/phase-margin target it would force this canary block's bias currents
  higher than its role warrants, per `CLAUDE.md`'s framing of this block as
  a canary, not a heavy-load driver.
- **Leaving the corner grid `[P]` rather than ratifying it.** Considered,
  since the issue's scope explicitly warns against flipping this tag
  silently. Rejected in favor of ratifying: the process/temperature spread
  data cited above shows the grid has now actually been exercised against
  real gf180mcu models with sane, bounded results (not just proposed by
  analogy), which is exactly the condition the `[DR-n]` tag's definition in
  `spec/target-spec.md` requires — and the SG13G2 twin ratified its own
  grid on the same basis.

## Consequences

- `spec/target-spec.md` §1's `Load capacitance, CL` row moves from `[TBD]`
  to `[DR-1]`, `CL = 2 pF`, matching `sg13g2-opamp`'s DR-0001 — enabling
  direct cross-PDK GBW/phase-margin/slew comparison once both blocks reach
  a sizing pass.
- `spec/target-spec.md` §1's `Corner grid` row moves from `[P]` to
  `[DR-1]`, ratifying `typical, ff, ss, fs, sf` (MOS) as the corner set
  future PVT-cornered amplifier testbenches use — no other §1/§2 row is
  touched by this record.
- Every gm/ID-dependent `[TBD]` row in `spec/target-spec.md` §2 (DC gain,
  GBW, slew rate, output swing, quiescent power) can now be sized against a
  concrete topology and a concrete load, citing
  `sim/gm-id-characterization/records/20260909-052956-79c6a45.md` directly
  per `CLAUDE.md`'s "gm/ID first" rule — but no such sizing is performed by
  this record (see "Out of scope" below).
- The non-cascoded architecture choice means output swing and input
  common-mode range are not further constrained by stacked-cascode
  headroom, but the two-stage DC-gain budget now depends on the
  output-stage PMOS device being sized at a moderate-to-long channel
  length (candidate `L ≈ 1–4 µm` class, final length TBD in a future
  sizing pass) to realize the `gm/gds` advantage this record cites — a
  future sizing pass that instead sizes that device short (for area or
  bandwidth reasons) would need to re-examine whether the non-cascoded
  DC-gain estimate in this record still closes.
- This decision is unverified in simulation beyond the device-level gm/ID
  data cited above — no schematic exists yet in this repo, and this
  record's own DC-gain figure is an illustrative `gm/gds` product, not a
  circuit-level AC simulation result. If a future sizing or schematic-level
  pass finds the non-cascoded DC-gain budget does not close (e.g. once
  mirror/tail-device loading and mismatch are included), that finding
  should produce a superseding record, not a silent addition of a cascode.
- This record does not change this file's or `spec/target-spec.md`'s
  overall `Status`: both remain `proposed`/`DRAFT` — a topology and CL
  decision is a prerequisite to filling `[TBD]` performance rows, not a
  ratification of the spec as a whole.

## Out of scope

This record does **not** perform any actual amplifier sizing — no device
widths, lengths, bias currents, or mirror ratios are chosen here. That is
explicitly a follow-on issue (mirroring `sg13g2-opamp`'s own next step), to
cite `sim/gm-id-characterization/records/20260909-052956-79c6a45.md`'s full
gm/ID-vs-`Vov` curves directly per `CLAUDE.md`'s "gm/ID first" rule, once
this record's topology and `CL` choices are available to size against.
Ratifying `spec/target-spec.md` as a whole (a two-key mechanism, per
`CLAUDE.md`) and opening the 5 V stretch row (its own decision record) are
likewise out of scope for this record.
