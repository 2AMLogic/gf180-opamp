# 0002: Recommended bounds for `target-spec.md` §2's `[TBD]` performance rows

- **Status**: proposed (input to a future spec-ratification act; per the
  2am#357 ratification-via-PR policy, the operator's approval of the PR
  that lands this record *is* the ratification act — this record does not
  itself flip `spec/target-spec.md`'s `Status` field, which remains
  `DRAFT`)
- **Date**: 2026-09-15
- **Decided by**: Builder agent, issue #18
- **Related**: [gap-to-T1 tracker #7](https://github.com/2AMLogic/gf180-opamp/issues/7)
  (item 5's ratification prerequisite, and the 2026-09-14 human-ruling
  comment naming this DR as the second of three next steps),
  [2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357) (the
  ratification-via-PR standing policy this record's process follows),
  `spec/target-spec.md` (§2, every row this record addresses),
  [`spec/decision-records/0001-topology-and-cl.md`](0001-topology-and-cl.md)
  (the topology and `CL = 2 pF` [DR-1] target these bounds are argued
  "into"), `sim/gm-id-characterization/records/20260909-052956-79c6a45.md`
  (the device-level evidence cited throughout)

## Context

`spec/target-spec.md` is `Status: DRAFT`. Every row in its §2 "Performance
targets" table (DC gain, GBW, slew rate, input-referred noise, input-referred
offset, CMRR, PSRR, output swing, quiescent power, area) is `[TBD]` except
phase margin, already proposed `[P]` (`≥ 60°`, not touched by this record —
it is not a `[TBD]` row). This is item 5's stated prerequisite in the
gap-to-T1 tracker (#7): *"Full PVT corner simulation vs a ratified spec...
requires the spec table itself to be ratified."* It is also the second of
three concrete next steps named in #7's 2026-09-14 human-ruling comment: *"a
spec-ratification DR as a PR turning `spec/target-spec.md`'s `[TBD]` rows
into recommended bounds with the gm/ID evidence, per the ratification-via-PR
ruling... No circuit change in that PR."*

**No schematic exists yet in this repo** (`design/` holds only a placeholder
`README.md`, verified against `origin/main` @ `1bb9a74`, 2026-09-15). This
record therefore follows the two-track approach [DR-1] itself modeled: (a)
for rows that reduce to a methodology or statistical-basis choice (not a
simulated number), ratify the convention directly; (b) for rows where a
first-principles bound can be argued from the committed gm/ID device data
and this repo's own [DR-1] topology/rail choices *without* picking any new
bias current, compensation capacitor, or device width (i.e. without
performing amplifier sizing), propose a bound and show the arithmetic; (c)
every other row — where no such argument is possible without an actual
sizing pass — is left `[TBD]`, with a one-line note naming exactly what
evidence is missing, per `CLAUDE.md`'s "no claim without a testbench."

This record makes **no amplifier-sizing decision**: no device width, length
(beyond the specific gm/ID-table rows already cited by name), bias current,
mirror ratio, or compensation capacitor value is chosen here. Where an
argument below needs a representative overdrive voltage to read a row out of
the gm/ID device table, it reuses `Vov = 200 mV`, the same representative
point [DR-1] already cites repeatedly (its gain estimate, its `fT`
comparisons) — not a new sizing choice, and every swing calculation below is
additionally checked across the full swept `Vov = 50–300 mV` range so the
proposed bound does not depend on that one point holding.

## Decision

### (a) Statistical basis and binding-corner methodology — ratified

**Input-referred offset, statistical basis: `3σ, mismatch MC N≥300 + process
corners` — [DR-2].** `spec/target-spec.md` already carries this as a `[P]`
proposal, citing it as matching `gf180-bandgap`'s convention. That citation
is now verified directly: `gf180-bandgap`'s ratified target-spec table
(README.md, ratified 2026-07-31, issue #1/#35) states its output-reference
row's statistical basis as exactly `3σ, mismatch MC N≥300 + process corners,
−40…125 °C`. Both same-PDK sibling twins targeting the identical two-stage
op-amp topology — `sg13g2-opamp` and `sky130-opamp` — independently propose
the same convention for their own offset rows (each cites `gf180-bandgap`'s
convention by name), so this is not a single-source borrow but a
fleet-wide-converged practice for this exact statement shape. This record
ratifies the **statistical-basis convention** for the offset row — it does
not, and cannot, ratify a *numeric* offset target: no mismatch-model data or
Monte-Carlo testbench exists in this repo (`sim/gm-id-characterization/` is
a bare-device DC sweep, not a mismatch characterization — see its own
"Statistical convention: N/A" line), so the offset row's **Target** column
stays `[TBD]` (see "(c)" below); only its **Statistical basis** column moves
to `[DR-2]`.

**Binding-corner convention — reaffirmed, not newly ratified.** Every §2 row
already states a "Binding corner (predicted)" column value, reasoned in
`target-spec.md`'s own "How to read this table" section as a *prediction*
from the topology's generic behavior, explicitly not a ratified decision in
its own right (no schematic exists to simulate against yet). This record
does not change that column for any row — it is descriptive prose, not a
decision this record has grounds to ratify or alter without simulation
evidence. (Note for a future revision of this record: `sg13g2-opamp`'s own
sizing pass found its *measured* worst corners for DC gain, GBW, and
quiescent power did **not** match this same generic prediction pattern once
an actual schematic was simulated — see its `spec/target-spec.md` §2 "Status:
Measured (not yet ratified)" rows. That is evidence this prediction
methodology should be treated as a placeholder to be superseded by
measurement, not as a bound in itself — consistent with how
`target-spec.md`'s own text already frames it.)

### (b) Argued bounds from gm/ID data and [DR-1]'s own choices — proposed

**Open-loop DC gain: Target ≥ 60 dB, Stretch ≥ 70 dB (worst-case corner) —
[DR-2].**

[DR-1]'s own illustrative (non-binding) DC-gain estimate — the product of
the short-channel NMOS input pair's intrinsic gain (`L = 0.28 µm`,
`typical`, `Vov = 200 mV`, `gm/gds = 29.0`) and the long-channel PMOS output
stage's intrinsic gain (`L = 4 µm`, `typical`, `Vov = 200 mV`,
`gm/gds = 2122.7`) — gives `29.0 × 2122.7 ≈ 61,558` (`≈ 95.8 dB`). [DR-1]
itself already flags this figure as optimistic: it is a bare product of two
devices' own intrinsic gain, with no finite mirror-load or tail-current-
source loading folded in (a real stage's gain is `gm × (ro,gain ‖
ro,load)`, strictly less than `gm × ro,gain` alone), and no mismatch
degradation.

A concrete cross-fleet data point quantifies how much that kind of estimate
typically overstates a real, simulated result. `sg13g2-opamp`'s own DR-0001
made the identical style of estimate for its own topology and reached
`≈ 68 dB` (cited directly in this repo's [DR-1]: *"the SG13G2 twin's
equivalent estimate reached only ≈68 dB"*). That repo has since run an
actual schematic-level sizing pass and PVT AC sweep
(`sg13g2-opamp/sim/open-loop-ac/records/20260910-221601-22feaba.csv`,
issue #9), and its `spec/target-spec.md` now reports a **measured** DC-gain
range of `37.8 dB` (worst-case corner) to `45.2 dB` (best-case corner) — a
haircut of `68 − 45.2 ≈ 22.8 dB` at best-case and `68 − 37.8 ≈ 30.2 dB` at
worst-case, relative to that repo's own DR-0001-style ideal-product
estimate.

Applying the same haircut range to this repo's own `≈ 95.8 dB` ideal
estimate (not claiming the same circuit, only the same style-of-estimate
optimism, since both are the identical "bare `gm/gds`-product, no finite
loading, no mismatch" method applied to a two-stage non-cascoded Miller
topology): `95.8 − 30.2 ≈ 65.6 dB` (worst-case-style haircut) to
`95.8 − 22.8 ≈ 73.0 dB` (best-case-style haircut) — a plausible real-circuit
range of roughly **65.6–73.0 dB**. Independently, `sky130-opamp`'s own
2026-09-15 sizing-pass estimate for the same topology class (parallel-
resistance-based, not a bare product, so a structurally different and more
conservative method) landed at `≈ 60–65 dB` and proposed a `Target ≥ 60 dB`
on that basis. Both independent estimation routes — this record's
cross-fleet-derated `gm/gds` product, and `sky130-opamp`'s independent
parallel-resistance calculation — converge in the same 60–73 dB band despite
starting from very different raw numbers (`95.8 dB` here vs.
`sky130-opamp`'s own directly-computed parallel-resistance figure), which is
the basis for proposing a target below the entire derated range rather than
at its center.

**Target: ≥ 60 dB.** Set below every value in the 65.6–73.0 dB derated
range above, and matching `sky130-opamp`'s own independently-derived
target for the same topology class, to leave margin for effects this
device-level estimate omits entirely (non-unity mirror ratios, finite
tail-current-source loading, mismatch, and this record's binding-corner
prediction of `SS / −40 °C` not yet being simulated). **Stretch: ≥ 70 dB**,
near the top of the derated range, achievable if a future sizing pass
lands close to the intrinsic-gain figures [DR-1] cites. This is a
**target to design toward**, not a claim that any sizing meets it — per
`CLAUDE.md`'s "no claim without a testbench," confirming it requires the
schematic-entry follow-on issue's sizing pass and a PVT-cornered AC
testbench.

**Output swing: Target ≥ 2.3 Vpp (≈ 78% of the 2.97 V worst-case-low rail),
Stretch ≥ 2.6 Vpp (≈ 88%) — [DR-2].**

[DR-1]'s output stage is a PMOS common-source gain device (source at `VDD`,
drain at the output node) loaded by an NMOS constant-current sink (source at
ground, drain at the output node) — the swing-limiting headroom on each rail
is that device's own overdrive voltage: `Vout,max ≈ VDD − |Vov,PMOS|` and
`Vout,min ≈ Vov,NMOS`, so
`swing ≈ VDD − (Vov,PMOS + Vov,NMOS)`. At the worst-case-low rail
(`VDD = 2.97 V`, `spec/target-spec.md` §1) and reading both devices at the
**same** representative overdrive point (the design freely chooses an
overdrive per device; this record does not choose one, it instead checks
the bound across the sweep's full range so the proposed target does not
depend on any single sizing choice):

| Assumed `Vov` (both output devices) | Headroom loss (`2×Vov`) | Swing at `VDD = 2.97 V` | % of rail |
|---|---|---|---|
| 100 mV | 0.20 V | 2.77 V | 93% |
| 200 mV | 0.40 V | 2.57 V | 86% |
| 300 mV | 0.60 V | 2.37 V | 80% |

(headroom figures follow directly from the definition above; no gm/gds or
fT column is needed for this calculation, only the `Vov` sweep axis itself,
which spans exactly this 50–300 mV range in
`sim/gm-id-characterization/records/20260909-052956-79c6a45.md`.) Even at
the sweep's largest tabulated overdrive (`Vov = 300 mV`, the most
headroom-hungry design point in the record, and hence the most conservative
row of this table), the estimated swing is `2.37 V` (`≈ 80%` of the
worst-case-low rail). `sky130-opamp`'s own independent estimate for the
same topology class reached a broadly consistent `≈ 79%` of its own
worst-case-low rail using its own PDK's `Vov` figures at its chosen sizing
point — corroborating that an 80%-class fraction is a reasonable
cross-fleet order of magnitude for this topology, not an artifact of one
PDK's numbers.

**Target: ≥ 2.3 Vpp** (≈ 78% of the 2.97 V worst-case-low rail), set
slightly below the `Vov = 300 mV` row (`2.37 V`) to leave margin for the
tail/mirror-device headroom this two-device swing model omits (a real
single-ended output stage's swing is also constrained by the *first*
stage's own output headroom feeding the second stage's gate, not modeled
here). **Stretch: ≥ 2.6 Vpp** (≈ 88%), near the `Vov = 200 mV` row,
achievable if the eventual sizing pass favors smaller output-device
overdrives. As with the DC-gain row, this is a target to design toward, not
a simulated or measured claim.

### (c) Rows left `[TBD]` — evidence missing, named explicitly

Every row below stays `[TBD]` because no argument is possible from the
committed gm/ID device sweep or [DR-1]'s topology/`CL` choices without
either (i) picking a new bias current, compensation capacitor, or mirror
ratio — i.e., performing amplifier sizing, explicitly out of scope for this
record (see "Out of scope" below) — or (ii) a circuit behavior the
committed device data structurally cannot represent at all.

- **GBW** (into `CL = 2 pF` [DR-1]) — `GBW ≈ gm1 / (2π·Cc)` requires both an
  input-pair bias current (to convert the sweep's `gm/ID` ratio into an
  absolute `gm1`) and a compensation-capacitor value `Cc`. Neither is chosen
  anywhere in this repo yet ([DR-1]'s own "Out of scope" explicitly defers
  "device widths, lengths, bias currents, or mirror ratios" to the
  schematic-entry follow-on issue). Missing: a chosen bias current and `Cc`.
- **Slew rate** — `SR = I_SS / Cc` requires an absolute tail current and
  `Cc`, for the same reason as GBW. Missing: a chosen bias current and `Cc`.
- **Input-referred noise** — even the thermal-floor-only estimate
  `sky130-opamp`'s own sizing pass used (`en² ∝ 1/gm1`) requires an absolute
  `gm1`, which requires a chosen bias current; this repo has made none.
  Separately, no flicker (`1/f`) noise coefficient exists in the committed
  gm/ID sweep (a DC operating-point characterization only — `gm`, `gds`,
  `id`, `cgg`, `vth`, no AC/noise analysis) or anywhere else in this repo, so
  even a future bias-current choice would only bound the thermal floor, not
  a full-band figure. Missing: a chosen bias current, and a flicker-noise
  model/coefficient this PDK's device sweep does not provide.
- **Input-referred offset (Target column only — statistical basis is
  ratified above)** — a numeric 3σ offset figure requires either a
  dedicated mismatch Monte-Carlo pass (Pelgrom-style `AVT` coefficients) or
  PDK mismatch-model data; neither exists in this repo
  (`sim/gm-id-characterization/`'s own record states "Statistical
  convention: N/A -- this record is the process/temperature corner matrix,
  not a mismatch/Monte Carlo distribution claim"). Missing: mismatch-model
  data or a Monte-Carlo testbench.
- **CMRR** — set by the first stage's common-mode-to-differential-mode
  conversion (tail-current-source output impedance and mirror asymmetry
  under a common-mode input step), a small-signal *circuit* behavior the
  committed gm/ID sweep cannot represent: each device in that sweep is
  characterized in isolation, referred to its own source node, with no
  representation of a shared tail node or a common-mode stimulus at all.
  Missing: a schematic and a common-mode AC testbench.
- **PSRR** — the committed gm/ID sweep record states this directly: *"Supply:
  not applicable -- each DUT is a two-terminal-style device sweep referred
  to its own source node... The +/-10% supply axis is a circuit-level
  property; it applies to future PSRR/line-regulation-style benches on an
  actual amplifier stage, not to a bare device characterization."* There is
  no supply-voltage sweep axis anywhere in the committed data to argue a
  PSRR bound from. Missing: a schematic and a supply-injection AC testbench.
  (`gf180-bandgap`'s own ratified PSRR row, `> 60 dB DC–1 kHz` / `> 30 dB @
  1 MHz`, was considered as a cross-fleet numeric precedent and rejected —
  a bandgap reference loop's PSRR is set by a structurally different
  feedback path than a standalone two-stage op-amp's open-loop PSRR, so
  borrowing its number would be a guess dressed as a citation, not a bound
  argued from this repo's own topology.)
- **Quiescent power** — `P_Q = I_Q × VDD` requires an absolute quiescent
  current, which requires the same bias-current sizing choice GBW and slew
  rate above are missing. Missing: a chosen bias current (input-pair tail
  current and output-stage bias current).
- **Area** — a post-layout quantity; no `layout/` content exists in this
  repo beyond a placeholder `README.md`, and area has no gm/ID-derived basis
  at all. Missing: a completed layout.

## Alternatives considered

- **Also bound GBW, slew rate, and quiescent power using an illustrative
  self-consistent sizing example**, the way `sky130-opamp`'s own 2026-09-15
  sizing pass did (choosing an explicit `Cc`, tail current, and `gm2/gm1`
  ratio to derive concrete MHz/V-µs/µW numbers). **Rejected for this
  record.** That approach requires picking absolute bias currents and a
  compensation capacitor — an amplifier-sizing decision this issue's "Out of
  Scope" section explicitly excludes ("Any circuit/schematic change") and
  which [DR-1] itself already deferred to "the schematic-entry follow-on
  issue" (tracker #7 item 1). Unlike the DC-gain and output-swing bounds
  above (which reuse [DR-1]'s own already-cited `Vov = 200 mV` design point
  and need no new absolute current), a GBW/slew/power estimate would
  require *this* record to originate a sizing choice no prior record in this
  repo has made — better done as, and left to, the dedicated sizing pass.
- **Propose a numeric offset target by analogy to `gf180-bandgap`'s ratified
  `1.20 V ±2%` output-accuracy row.** Rejected — that figure is a
  *reference-voltage accuracy* bound for a specific bandgap topology, not an
  input-referred offset voltage for a differential-pair op-amp; the units
  and the topology it is set against are both incompatible, so translating
  the number would not be an argued bound, it would be a guess.
- **Leave the offset row's statistical basis `[P]` rather than ratifying
  it**, since no mismatch data exists in this repo yet. Considered, but
  rejected: the `[DR-n]` tag's own definition in `target-spec.md` requires
  only that the *convention itself* — not a numeric value derived from it —
  be settled on real evidence; the fleet-wide convergence across
  `gf180-bandgap` (ratified), `sg13g2-opamp`, and `sky130-opamp` on the
  identical statement is exactly that kind of settled evidence, and [DR-1]
  itself already ratified the corner-grid row on an analogous
  "methodology is settled, even though not every consumer of it is sized
  yet" basis.
- **Propose a numeric PSRR bound by analogy to `gf180-bandgap`'s ratified
  row.** Rejected — see "(c)" above; a bandgap loop's PSRR path and a
  standalone op-amp's open-loop PSRR are not the same transfer function, and
  no data in this repo argues a specific number either way.

## Consequences

- `spec/target-spec.md` §2's **Open-loop DC gain** row moves from `[TBD]` to
  `Target ≥ 60 dB [DR-2]`, `Stretch ≥ 70 dB [DR-2]`.
- `spec/target-spec.md` §2's **Output swing** row moves from `[TBD]` to
  `Target ≥ 2.3 Vpp (≈78% of VDD,min) [DR-2]`, `Stretch ≥ 2.6 Vpp (≈88%)
  [DR-2]`.
- `spec/target-spec.md` §2's **Input-referred offset** row's *Statistical
  basis* column moves from `[P]` to `3σ, mismatch MC N≥300 + process
  corners [DR-2]`; its *Target* column stays `[TBD]` (no numeric bound
  proposed — see "(c)" above).
- Every other `[TBD]` §2 row (GBW, slew rate, input-referred noise, CMRR,
  PSRR, quiescent power, area) is unchanged, each now carrying a one-line
  note in `target-spec.md` naming what evidence is missing (bias-current/
  `Cc` sizing, mismatch data, a common-mode or supply-injection AC
  testbench, or a completed layout), superseding the collective "tracked
  under #7" note with a per-row reason.
- This record does not change `spec/target-spec.md`'s or its own `Status`
  field — both remain `DRAFT`/`proposed`. Ratification of the table as a
  whole (and of this record) is the operator's PR-approval act, per the
  2am#357 policy cited above.
- Every proposed numeric bound above (DC gain, output swing) is a **target
  to design toward**, not a simulated or measured result — confirming or
  revising either bound is explicitly the job of the schematic-entry
  follow-on issue's sizing pass and this block's eventual PVT-cornered AC
  testbenches, not this record.

## Out of scope

This record does **not** perform any amplifier sizing — no device width,
length (beyond the specific already-published gm/ID-table rows cited by
name), bias current, mirror ratio, or compensation-capacitor value is
chosen here, matching [DR-1]'s own scope discipline. It does not flip
`spec/target-spec.md`'s or its own `Status` field to ratified (the
operator's PR approval is that act, per 2am#357). It does not open the 5 V
stretch row. It contains no circuit-design or schematic change of any kind.
