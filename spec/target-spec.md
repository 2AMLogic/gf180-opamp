# Target specification — gf180-opamp

- **Status**: **DRAFT** — engineering input, not yet ratified. Two proposed
  decision records exist (`spec/decision-records/0001-topology-and-cl.md`,
  topology + `CL`; `spec/decision-records/0002-performance-target-bounds.md`,
  recommended bounds for several §2 performance rows); ratification of this
  table as a whole is a separate, future issue — per the ratification-via-PR
  policy ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)),
  that ratification act is the operator's approval of the PR that flips this
  `Status` field, not an edit any decision record makes on its own.
- **Date**: 2026-09-05
- **Assembled by**: Loom Builder agent, issue #2 (bootstrap/scaffolding pass)
- **Scope**: 3.3 V primary variant only. The 5 V device-flavor stretch row is
  named but explicitly not opened here — per `CLAUDE.md`, opening it requires
  its own `spec/` decision record, never a silent addition alongside this
  table.

This file is the block's single consolidated target-spec table, following the
row set `CLAUDE.md` already names: *"gain, GBW/PM into stated CL, slew,
noise, offset with statistical basis, CMRR/PSRR, swing, power) at PVT
corners."* Before this file existed, that row set lived only as prose in
`CLAUDE.md` and `README.md`. Nothing in this pass performs circuit design,
schematic capture, or simulation — every numeric target below is either an
engineering placeholder proposal `[P]` or explicitly `[TBD]` pending
gf180mcu device data that does not exist in this repo yet
(`design/`, `sim/`, `layout/`, and `measurements/` all currently hold only
placeholder `README.md` files, verified against `main` @ the "Initial commit").

## How to read this table

**Value tags** — every non-definitional value carries one, following
[`gf180-temp-por/spec/target-spec.md`](https://github.com/2AMLogic/gf180-temp-por/blob/main/spec/target-spec.md)'s
convention, so a future reviewer can tell a carried decision from a new
proposal at a glance:

| Tag | Meaning |
|---|---|
| **[DR-n]** | Carried unchanged from decision record `n`. `[DR-1]` = [`spec/decision-records/0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md). `[DR-2]` = [`spec/decision-records/0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md). |
| **[P]** | **Proposed by this bootstrap pass** — an engineering placeholder with no measured gf180mcu data behind it yet (e.g. carried from the block's own README/CLAUDE.md framing, or a structural convention borrowed from a sibling repo's ratified spec). Needs an explicit ratification decision before it binds. |
| **[TBD]** | Deliberately unset — no gf180mcu device data exists yet to propose even a placeholder number. Filled in once gm/ID device characterization (`sim/`) and PVT-cornered testbenches exist. Tracked collectively under the gap-to-T1 tracker, [#7](https://github.com/2AMLogic/gf180-opamp/issues/7) (item 5, "Full PVT corner simulation vs a ratified spec"), rather than one issue per row — no per-row characterization issue has been filed yet. |

**Status** column values: `not started` (no `sim/` evidence exists for this
row at all — true of every row in this pass) — there is no `ratifiable` or
`conditional` row yet, unlike the more mature siblings this table's shape is
borrowed from.

**Binding corner** — the corner at which a row's hard edge is expected to
bind, reasoned from the topology's *generic* behavior (a two-stage
Miller-compensated op-amp) since no schematic exists yet to simulate. This is
a **prediction**, not a measurement — CLAUDE.md's "no claim without a
testbench" applies to any future *pass/fail* verdict on these rows, not to
this placeholder prediction of where the number will eventually bind. A
`sim/` record's full PVT grid supersedes the prediction once it exists.

## 1. Global operating conditions

| Parameter | Value | Notes |
|---|---|---|
| Supply voltage, VDD | **3.3 V ±10% → 2.97–3.63 V** [P] | Primary variant. Matches `gf180-bandgap`'s and `gf180-ldo`'s ratified 3.3 V primary rows — the fleet-wide convention for this PDK's wave-1 target, per `CLAUDE.md`. |
| Supply voltage, VDD (stretch) | **5 V — not opened** [P] | GF180MCU's 5 V-tolerant device flavors are surveyed but not characterized (`README.md`). Per `CLAUDE.md`, opening this row requires its own decision record; it is named here only so a future DR has a place to point at, not to imply the row is in scope. Never mixed with 3.3 V-flavor devices in one variant. |
| Operating temperature | **−40…+125 °C** [P] | Matches the fleet-wide convention (`gf180-bandgap`, `gf180-temp-por`) for a commercial-grade PDK part. No gf180mcu-specific device data has been checked against this range yet — proposed by analogy, not measured. |
| Corner grid | **`typical, ff, ss, fs, sf` (MOS) [DR-1]** | Adopted by the gm/ID characterization study (issue #10, [`sim/gm-id-characterization/`](../sim/gm-id-characterization/README.md)) — gf180mcu's own top-level MOS `.LIB` corner bundles in `sm141064.ngspice` (`fs` = fast NMOS / slow PMOS, `sf` = the reverse), the same five names `gf180-bandgap`'s ratified grid uses. Ratified by [decision record 0001](decision-records/0001-topology-and-cl.md) on the basis of the study's own process/temperature spread data (bounded, sane behavior across all five corners). Resistor/BJT/cap corners are still not chosen, since this block's passive device menu isn't picked yet — see [`porting-plan.md`](porting-plan.md) §1. |
| Load capacitance, CL | **2 pF [DR-1]** | Ratified by [decision record 0001](decision-records/0001-topology-and-cl.md), adopted from `sg13g2-opamp`'s DR-0001 for cross-PDK comparability of GBW/phase-margin/slew targets across the three-foundry twins. |

## 2. Performance targets

| Parameter | Target | Stretch | Statistical basis | Binding corner (predicted) | Status |
|---|---|---|---|---|---|
| Open-loop DC gain | **≥ 60 dB [DR-2]** | ≥ 70 dB [DR-2] | — (deterministic corner-worst-case candidate) | SS / −40 °C (lowest gm, highest output impedance loss) | not started (proposed target, not simulated — see [DR-2]) |
| GBW (into stated CL = 2 pF [DR-1] above) | **[TBD]** — needs a chosen input-pair bias current and compensation capacitor `Cc` (amplifier sizing, deferred to the schematic-entry issue); see [DR-2] §(c) | — | — | SS / −40 °C / low VDD (slowest devices) | not started |
| Phase margin (at GBW, same CL) | **≥ 60° [P]** | ≥ 45° at the FF/hot corner if 60° is unreachable there | — (deterministic corner-worst-case) | FF / 125 °C (fastest devices, most peaking risk) | not started |
| Slew rate | **[TBD]** — needs a chosen tail current and `Cc` (amplifier sizing, deferred to the schematic-entry issue); see [DR-2] §(c) | — | — | SS / −40 °C / low VDD (lowest tail-current headroom) | not started |
| Input-referred noise | **[TBD]** — band not yet chosen; even a thermal-floor estimate needs a chosen bias current, and no flicker (`1/f`) coefficient exists in this PDK's committed device data; see [DR-2] §(c) | — | n/a until a band is set | n/a | not started |
| Input-referred offset | **[TBD]** — needs mismatch-model data or a Monte-Carlo testbench, neither committed yet; see [DR-2] §(c) | — | **3σ, mismatch MC N≥300 + process corners [DR-2]** — ratified by [DR-2], matching `gf180-bandgap`'s ratified statistical-basis convention and the same convention independently proposed by the `sg13g2-opamp`/`sky130-opamp` twins; sample count not yet re-derived for this topology | to be determined once a topology is drawn — likely SS/FF split-corner pairing on the input differential pair | not started |
| CMRR | **[TBD]** — needs a schematic and a common-mode AC testbench; the committed gm/ID sweep characterizes each device in isolation with no shared tail node or common-mode stimulus; see [DR-2] §(c) | — | — (deterministic corner-worst-case) | to be determined | not started |
| PSRR | **[TBD]** — needs a schematic and a supply-injection AC testbench; the committed gm/ID sweep has no supply-voltage axis at all (stated directly in its own record); see [DR-2] §(c) | — | — (deterministic corner-worst-case) | to be determined | not started |
| Output swing | **≥ 2.3 Vpp (≈78% of VDD,min) [DR-2]** | ≥ 2.6 Vpp (≈88%) [DR-2] | — | low VDD / worst output-stage headroom corner | not started (proposed target, not simulated — see [DR-2]) |
| Quiescent power | **[TBD]** — needs a chosen input-pair and output-stage bias current (amplifier sizing, deferred to the schematic-entry issue); see [DR-2] §(c) | — | — (deterministic corner-worst-case) | FF / 125 °C / 3.63 V (leakage + fastest devices) — matches `gf180-bandgap`'s ratified Iq binding-corner convention | not started |
| Area | **[TBD]** — a post-layout quantity; no `layout/` content exists yet beyond a placeholder | — | n/a (not a PVT line) | n/a | not started |

Every `[TBD]` row above is deliberately left unset rather than guessed, per
`CLAUDE.md`'s "no claim without a testbench." Two rows (DC gain, output
swing) now carry a `[DR-2]` target, argued from the committed gm/ID device
data and [DR-1]'s topology/`CL` choices without any new amplifier-sizing
decision — see
[`spec/decision-records/0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md)
for the full argument and citations, including why each remaining `[TBD]`
row still needs either an amplifier-sizing pass, a testbench class this
repo has not built yet, or a layout that does not exist yet. Filling the
still-`[TBD]` rows requires, at minimum, a topology decision (tracked in
[`porting-plan.md`](porting-plan.md), settled by
[decision record 0001](decision-records/0001-topology-and-cl.md)) and a
gm/ID device-characterization pass committed to `sim/` (already committed,
`sim/gm-id-characterization/records/20260909-052956-79c6a45.md`), followed
by an actual sizing pass and PVT-cornered testbenches, per `CLAUDE.md`'s
"gm/ID first, committed to `sim/` before sizing."

## 3. What this table is not

- **Not ratified.** Two proposed decision records exist
  (`spec/decision-records/0001-topology-and-cl.md`,
  `spec/decision-records/0002-performance-target-bounds.md`), but this table
  as a whole is not. Ratification (per `CLAUDE.md`'s two-key mechanism — an
  EE key and a market key, applied via the ratification-via-PR policy,
  [2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)) is a
  future PR's job — the operator's approval of that PR is the ratification
  act itself, not a separate edit to this file. Per the generalized
  2026-08-28 ruling cited in the original issue body, a scope-only spec DR
  ratified with both keys needs no separate per-PR operator statement — but
  that ruling applies at ratification time, not to this DRAFT.
- **Not a commitment that every `[TBD]` row will end up non-trivial.** The
  corner grid and load capacitance rows were determined jointly with the
  topology decision — see [decision record 0001](decision-records/0001-topology-and-cl.md).
  The DC gain and output swing rows now carry a proposed (unsimulated)
  target and stretch, argued without a schematic — see
  [decision record 0002](decision-records/0002-performance-target-bounds.md).
  Other still-`[TBD]` rows (GBW, slew rate, noise, offset target, CMRR,
  PSRR, quiescent power, area) each carry a one-line note on exactly what
  evidence is missing, per [DR-2] §(c).
- **Not opening the 5 V stretch row.** It is named, not scoped in.

## 4. Sources

- `CLAUDE.md` (this repo) — the row set and the 3.3 V-primary/5 V-stretch
  scope rule.
- [`gf180-bandgap` README](https://github.com/2AMLogic/gf180-bandgap#target-specification-ratified-2026-07-31-see-issue-1-and-35) — ratified target-spec table shape (Target / Stretch / Corner binding columns), statistical-basis wording (`3σ, mismatch MC N≥300 + process corners`), and binding-corner convention this table borrows.
- [`gf180-temp-por/spec/target-spec.md`](https://github.com/2AMLogic/gf180-temp-por/blob/main/spec/target-spec.md) — the standalone `spec/target-spec.md` file precedent (rather than inline in `README.md`) and the `[DR-n]`/`[P]`/`[TBD-#n]` value-tag convention.
- [`gf180-ldo/spec/architecture-survey.md`](https://github.com/2AMLogic/gf180-ldo/blob/main/spec/architecture-survey.md) — the "mark device-level numbers pending a future issue rather than guessing" practice this table follows for every `[TBD]` row.
- [Gap-to-T1 tracker, #7](https://github.com/2AMLogic/gf180-opamp/issues/7) — the artifact-presence checklist this spec's eventual evidence trail (`sim/`, `layout/`) will need to satisfy.
- [`spec/decision-records/0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md) — the topology (input-pair polarity, output-stage class, cascode-or-not) and `CL` decision behind this file's `[DR-1]`-tagged rows, argued from `sim/gm-id-characterization/records/20260909-052956-79c6a45.md`.
- [`spec/decision-records/0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md) — recommended bounds for §2's `[TBD]` performance rows: the `[DR-2]`-tagged DC-gain and output-swing targets, the offset row's ratified statistical-basis convention, and the per-row "evidence missing" reasons for every row still `[TBD]`.
