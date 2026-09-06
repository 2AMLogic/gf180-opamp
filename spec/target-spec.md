# Target specification — gf180-opamp

- **Status**: **DRAFT** — engineering input, not yet ratified. No decision
  record exists yet; ratification is a future issue, once gm/ID
  device-characterization data lands under `sim/`.
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
| **[DR-n]** | Carried unchanged from decision record `n`. None exist yet in this repo — no row currently carries this tag. |
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
| Corner grid | **[TBD]** | Process corners for gf180mcu's 3.3 V MOS flavor are not yet enumerated in this repo. `gf180-bandgap`'s ratified grid (`tt, ff, ss, fs, sf` plus resistor/BJT corners) is the likely template, but this block's actual device menu (which resistor/cap flavors it uses) is not yet chosen — see [`porting-plan.md`](porting-plan.md) §1. |
| Load capacitance, CL | **[TBD]** | GBW/phase-margin targets are stated "into stated CL" per `CLAUDE.md`; no CL has been chosen yet since no application/bench context exists for this standalone op-amp characterization. |

## 2. Performance targets

| Parameter | Target | Stretch | Statistical basis | Binding corner (predicted) | Status |
|---|---|---|---|---|---|
| Open-loop DC gain | **[TBD]** | — | — (deterministic corner-worst-case candidate) | SS / −40 °C (lowest gm, highest output impedance loss) | not started |
| GBW (into stated CL, [TBD] above) | **[TBD]** | — | — | SS / −40 °C / low VDD (slowest devices) | not started |
| Phase margin (at GBW, same CL) | **≥ 60° [P]** | ≥ 45° at the FF/hot corner if 60° is unreachable there | — (deterministic corner-worst-case) | FF / 125 °C (fastest devices, most peaking risk) | not started |
| Slew rate | **[TBD]** | — | — | SS / −40 °C / low VDD (lowest tail-current headroom) | not started |
| Input-referred noise | **[TBD]** — band not yet chosen | — | n/a until a band is set | n/a | not started |
| Input-referred offset | **[TBD]** | — | **3σ, mismatch MC N≥300 + process corners [P]** — matches `gf180-bandgap`'s ratified statistical-basis convention; sample count not yet re-derived for this topology | to be determined once a topology is drawn — likely SS/FF split-corner pairing on the input differential pair | not started |
| CMRR | **[TBD]** | — | — (deterministic corner-worst-case) | to be determined | not started |
| PSRR | **[TBD]** | — | — (deterministic corner-worst-case) | to be determined | not started |
| Output swing | **[TBD]** | — | — | low VDD / worst output-stage headroom corner | not started |
| Quiescent power | **[TBD]** | — | — (deterministic corner-worst-case) | FF / 125 °C / 3.63 V (leakage + fastest devices) — matches `gf180-bandgap`'s ratified Iq binding-corner convention | not started |
| Area | **[TBD]** | — | n/a (not a PVT line) | n/a | not started |

Every `[TBD]` row above is deliberately left unset rather than guessed, per
`CLAUDE.md`'s "no claim without a testbench" and per this issue's explicit
scope (scaffolding only, no circuit design or simulation). Filling any of
them requires, at minimum, a topology decision (tracked in
[`porting-plan.md`](porting-plan.md)) and a gm/ID device-characterization
pass committed to `sim/`, per `CLAUDE.md`'s "gm/ID first, committed to
`sim/` before sizing."

## 3. What this table is not

- **Not ratified.** No `spec/decision-records/` directory exists yet in this
  repo. Ratification (per `CLAUDE.md`'s two-key mechanism — an EE key and a
  market key) is a future issue's job, once the `[TBD]` rows above have real
  gf180mcu device data behind them. Per the generalized 2026-08-28 ruling
  cited in the original issue body, a scope-only spec DR ratified with both
  keys needs no separate per-PR operator statement — but that ruling applies
  at ratification time, not to this DRAFT.
- **Not a commitment that every `[TBD]` row will end up non-trivial.** Some
  rows (e.g. the corner grid, or the load capacitance) may turn out to be
  determined jointly with a topology decision rather than independently.
- **Not opening the 5 V stretch row.** It is named, not scoped in.

## 4. Sources

- `CLAUDE.md` (this repo) — the row set and the 3.3 V-primary/5 V-stretch
  scope rule.
- [`gf180-bandgap` README](https://github.com/2AMLogic/gf180-bandgap#target-specification-ratified-2026-07-31-see-issue-1-and-35) — ratified target-spec table shape (Target / Stretch / Corner binding columns), statistical-basis wording (`3σ, mismatch MC N≥300 + process corners`), and binding-corner convention this table borrows.
- [`gf180-temp-por/spec/target-spec.md`](https://github.com/2AMLogic/gf180-temp-por/blob/main/spec/target-spec.md) — the standalone `spec/target-spec.md` file precedent (rather than inline in `README.md`) and the `[DR-n]`/`[P]`/`[TBD-#n]` value-tag convention.
- [`gf180-ldo/spec/architecture-survey.md`](https://github.com/2AMLogic/gf180-ldo/blob/main/spec/architecture-survey.md) — the "mark device-level numbers pending a future issue rather than guessing" practice this table follows for every `[TBD]` row.
- [Gap-to-T1 tracker, #7](https://github.com/2AMLogic/gf180-opamp/issues/7) — the artifact-presence checklist this spec's eventual evidence trail (`sim/`, `layout/`) will need to satisfy.
