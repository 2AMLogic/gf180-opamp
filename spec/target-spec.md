# Target specification — gf180-opamp

- **Status**: **RATIFIED (partial)** — per the ratification-via-PR policy
  ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)), the
  ratification act is the operator's approval of the PR that carries
  [`spec/decision-records/0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md)
  (issue #24; reviewed through this repo's two-key pipeline — Judge review
  plus Champion/operator merge). That record disposes **every row of this
  table individually**: each row below is **ratified as a target** (a
  design-to bound — **never "met"**: no circuit-level PVT evidence exists
  in this repo yet; the only AC-shaped data is the provisional,
  hand-built, schematic-disconnected
  [`sim/gain-gbw-pm/`](../sim/gain-gbw-pm/README.md) sweep that its own
  record disclaims, which this ratification does not cite) or **explicitly
  open** in [0003](decision-records/0003-target-spec-ratification.md)'s
  residual register. [DR-1] and [DR-2] are carried into force with the
  same act. The **sole remaining `[P]`** value is §1's deliberately
  un-opened 5 V stretch row (opening requires its own decision record per
  `CLAUDE.md`).
- **Date**: 2026-09-05 (bootstrap); **2026-09-21 (ratified (partial), via
  [0003](decision-records/0003-target-spec-ratification.md))**
- **Assembled by**: Loom Builder agent, issue #2 (bootstrap/scaffolding pass);
  Loom Builder agent, issue #24 (ratification pass)
- **Scope**: 3.3 V primary variant only. The 5 V device-flavor stretch row is
  named but explicitly not opened here — per `CLAUDE.md`, opening it requires
  its own `spec/` decision record, never a silent addition alongside this
  table. This ratification ratifies that *closure*; the row stays
  named-not-opened.

This file is the block's single consolidated target-spec table, following the
row set `CLAUDE.md` already names: *"gain, GBW/PM into stated CL, slew,
noise, offset with statistical basis, CMRR/PSRR, swing, power) at PVT
corners."* Before this file existed, that row set lived only as prose in
`CLAUDE.md` and `README.md`. At the 2026-09-06 bootstrap pass no circuit
design, schematic capture, or simulation existed in this repo and every
numeric target was either an engineering placeholder proposal `[P]` or
explicitly `[TBD]`. Since then the gm/ID device study (issue #10 / PR #11),
the sizing pass and schematic (issue #17 / PR #21,
[`design/opamp_sizing.md`](../design/opamp_sizing.md)), and the
[0003](decision-records/0003-target-spec-ratification.md) ratification
record have landed — every value below is now either **ratified as a
target** `[DR-n]` or **explicitly open** `[TBD]` with its reason named
in-row and in [0003](decision-records/0003-target-spec-ratification.md)'s
residual register.

## How to read this table

**Value tags** — every non-definitional value carries one, following
[`gf180-temp-por/spec/target-spec.md`](https://github.com/2AMLogic/gf180-temp-por/blob/main/spec/target-spec.md)'s
convention, so a future reviewer can tell a carried decision from a new
proposal at a glance:

| Tag | Meaning |
|---|---|
| **[DR-n]** | Carried unchanged from decision record `n`, **and ratified** — carried into force by [0003](decision-records/0003-target-spec-ratification.md), the ratification act for this whole table. `[DR-1]` = [`spec/decision-records/0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md). `[DR-2]` = [`spec/decision-records/0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md). `[DR-3]` = [`spec/decision-records/0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md) (the ratification record itself). |
| **[P]** | **Proposed, not ratified** — an engineering placeholder. Post-ratification the **only** `[P]` value left is §1's deliberately un-opened 5 V stretch row: its non-opening is structural (`CLAUDE.md` requires its own decision record to open), so it stays `[P]` rather than being ratified or removed — the same shape the `sg13g2-opamp` twin's post-ratification table uses for its own HV row. |
| **[TBD]** | Deliberately unset and **explicitly open** — [0003](decision-records/0003-target-spec-ratification.md)'s residual register names the exact missing evidence per row (never a silent open). Filled in once the missing evidence lands (mismatch MC, noise bench, CM/PSRR testbenches, layout). Tracked collectively under the gap-to-T1 tracker, [#7](https://github.com/2AMLogic/gf180-opamp/issues/7) (item 5, "Full PVT corner simulation vs a ratified spec"), rather than one issue per row — no per-row characterization issue has been filed yet. |

**Status** column values in use: `Ratified [DR-3] as target — not met` (the
row's bound is a ratified design-to target; **nothing is ratified as met**
— no circuit-level PVT evidence exists yet, per
[0003](decision-records/0003-target-spec-ratification.md) Context); `Open
[DR-3] — <reason>` (a `[TBD]` row held open by the residual register's
named missing evidence). The pre-ratification vocabulary (`not started`)
described the same rows before the [0003](decision-records/0003-target-spec-ratification.md)
pass.

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
| Supply voltage, VDD | **3.3 V ±10% → 2.97–3.63 V** [DR-3] | Primary variant. Matches `gf180-bandgap`'s and `gf180-ldo`'s ratified 3.3 V primary rows — the fleet-wide convention for this PDK's wave-1 target, per `CLAUDE.md`. Ratified as a condition of operation by [DR-3](decision-records/0003-target-spec-ratification.md) §(a): the charter-level rail and the rail every committed gf180mcu device argument in this repo is stated at. |
| Supply voltage, VDD (stretch) | **5 V — not opened** [P] | GF180MCU's 5 V-tolerant device flavors are surveyed but not characterized (`README.md`). Per `CLAUDE.md`, opening this row requires its own decision record; it is named here only so a future DR has a place to point at, not to imply the row is in scope. Never mixed with 3.3 V-flavor devices in one variant. The [0003](decision-records/0003-target-spec-ratification.md) ratification explicitly ratifies this *closure* (§(a), residual note) — the sole `[P]` value left in this table. |
| Operating temperature | **−40…+125 °C** [DR-3] | Fleet-wide convention for a commercial-grade PDK part (`gf180-bandgap`, `gf180-temp-por`), and — the evidence that makes it binding rather than analogy — the committed gm/ID study actually exercised exactly −40/27/125 °C across all five corners with bounded, sane behavior. Ratified by [DR-3](decision-records/0003-target-spec-ratification.md) §(a). |
| Corner grid | **`typical, ff, ss, fs, sf` (MOS) [DR-1]** | Adopted by the gm/ID characterization study (issue #10, [`sim/gm-id-characterization/`](../sim/gm-id-characterization/README.md)) — gf180mcu's own top-level MOS `.LIB` corner bundles in `sm141064.ngspice` (`fs` = fast NMOS / slow PMOS, `sf` = the reverse), the same five names `gf180-bandgap`'s ratified grid uses. Ratified by [decision record 0001](decision-records/0001-topology-and-cl.md) on the basis of the study's own process/temperature spread data (bounded, sane behavior across all five corners); carried into force as ratified table content by [DR-3](decision-records/0003-target-spec-ratification.md) §(a). Resistor/BJT/cap corners are still not chosen, since this block's passive device menu isn't picked yet — see [`porting-plan.md`](porting-plan.md) §1. |
| Load capacitance, CL | **2 pF [DR-1]** | Ratified by [decision record 0001](decision-records/0001-topology-and-cl.md), adopted from `sg13g2-opamp`'s DR-0001 for cross-PDK comparability of GBW/phase-margin/slew targets across the three-foundry twins; carried into force as ratified table content by [DR-3](decision-records/0003-target-spec-ratification.md) §(a). |

## 2. Performance targets

| Parameter | Target | Stretch | Statistical basis | Binding corner (predicted) | Status |
|---|---|---|---|---|---|
| Open-loop DC gain | **≥ 60 dB [DR-2]** | ≥ 70 dB [DR-2] | — (deterministic corner-worst-case candidate) | SS / −40 °C (lowest gm, highest output impedance loss) | **Ratified [DR-3] as target — not met** (no AC evidence exists; see [DR-3](decision-records/0003-target-spec-ratification.md) §(b)) |
| GBW (into stated CL = 2 pF [DR-1] above) | **≥ 10 MHz [DR-3]** — sized design point: `gm1 = 60.3 µS` (as-simulated 58.64 µS) with `CC = 0.619 pF` gives ≈ 15.5 MHz predicted nominal ([`design/opamp_sizing.md`](../design/opamp_sizing.md) §4, §6); target set ≈ 35% below the prediction for PVT margin ([DR-3](decision-records/0003-target-spec-ratification.md) §(b)) | — | — | SS / −40 °C / low VDD (slowest devices) | **Ratified [DR-3] as target — not met** |
| Phase margin (at GBW, same CL) | **≥ 60° [DR-3]** | — (the pre-ratification conditional "≥ 45° at FF/hot if unreachable" escape was rejected by [DR-3](decision-records/0003-target-spec-ratification.md) §(d) as a pre-authorized relaxation; a genuine future impossibility goes through a superseding DR, never the table) | — (deterministic corner-worst-case) | FF / 125 °C (fastest devices, most peaking risk) | **Ratified [DR-3] as target — not met** (RZ PVT-tracking quantification is the row's verification obligation — [DR-3](decision-records/0003-target-spec-ratification.md) §(d)) |
| Slew rate | **≥ 10 V/µs [DR-3]** — sized prediction `Itail/CC = 10 µA / 0.619 pF ≈ 16.2 V/µs` ([`design/opamp_sizing.md`](../design/opamp_sizing.md) §6); target ≈ 38% below it ([DR-3](decision-records/0003-target-spec-ratification.md) §(b)) | — | — | SS / −40 °C / low VDD (lowest tail-current headroom) | **Ratified [DR-3] as target — not met** |
| Input-referred noise | **[TBD]** — **Open [DR-3]**, residual (e1): a ratifiable bound is a *full-band* figure; the sizing pass's chosen `gm1` discharges [DR-2] §(c)'s bias-current half, but no flicker (`1/f`) model exists in this PDK's committed device data and the integration band is unchosen — a thermal-floor-only number would not rate this row | — | n/a until a band is set | n/a | Open [DR-3] — no noise bench, no band (see [DR-3](decision-records/0003-target-spec-ratification.md) §(e1)) |
| Input-referred offset | **[TBD]** — **Open [DR-3]**, residual (e2): numeric 3σ target needs a mismatch Monte-Carlo pass or PDK mismatch-model data, neither committed (the `3σ` basis itself is ratified — see the statistical-basis column); see [DR-3](decision-records/0003-target-spec-ratification.md) §(e2) | — | **3σ, mismatch MC N≥300 + process corners [DR-2]** — ratified by [DR-2], carried into force by [DR-3]; matching `gf180-bandgap`'s ratified convention and the same convention independently proposed by the `sg13g2-opamp`/`sky130-opamp` twins; sample count not yet re-derived for this topology | to be determined once a topology is drawn — likely SS/FF split-corner pairing on the input differential pair | Open [DR-3] — MC pass not run |
| CMRR | **[TBD]** — **Open [DR-3]**, residual (e3): needs a schematic and a common-mode AC testbench; the committed gm/ID sweep characterizes each device in isolation with no shared tail node or common-mode stimulus; see [DR-3](decision-records/0003-target-spec-ratification.md) §(e3) | — | — (deterministic corner-worst-case) | to be determined | Open [DR-3] — no CM AC bench |
| PSRR | **[TBD]** — **Open [DR-3]**, residual (e4): needs a schematic and a supply-injection AC testbench; the committed gm/ID sweep has no supply-voltage axis at all (stated directly in its own record); see [DR-3](decision-records/0003-target-spec-ratification.md) §(e4) | — | — (deterministic corner-worst-case) | to be determined | Open [DR-3] — no supply-injection bench |
| Output swing | **≥ 2.3 Vpp (≈78% of VDD,min) [DR-2]** | ≥ 2.6 Vpp (≈88%) [DR-2] | — | low VDD / worst output-stage headroom corner | **Ratified [DR-3] as target — not met** (see [DR-3](decision-records/0003-target-spec-ratification.md) §(b)) |
| Quiescent power | **≤ 350 µW worst-case corner [DR-3]** — as-simulated nominal 80.70 µA × 3.3 V = 266.3 µW; worst-case edge derived via the same-topology twin's measured ×1.2 corner-current spread (`sg13g2-opamp`, 99.9→119.7 µA) at the max rail: ≈ 351 µW, target set just under it ([DR-3](decision-records/0003-target-spec-ratification.md) §(b); [`design/opamp_sizing.md`](../design/opamp_sizing.md) §7) | — | — (deterministic corner-worst-case) | FF / 125 °C / 3.63 V (leakage + fastest devices) — matches `gf180-bandgap`'s ratified Iq binding-corner convention | **Ratified [DR-3] as target — not met** |
| Area | **[TBD]** — **Open [DR-3]**, residual (e5): a post-layout quantity; no `layout/` content exists yet beyond a placeholder ([DR-3](decision-records/0003-target-spec-ratification.md) §(e5)) | — | n/a (not a PVT line) | n/a | Open [DR-3] — no layout |

Every `[TBD]` row above is deliberately left **open** rather than
guessed — an explicit
[DR-3](decision-records/0003-target-spec-ratification.md) open-verdict with
its residual-register entry, never a silent one — per `CLAUDE.md`'s "no
claim without a testbench." Every ratified target above is a **design-to
bound, not a met result**: the [DR-2] bounds (DC gain, output swing) were
argued from the committed gm/ID device data and [DR-1]'s topology/`CL`
choices without any new amplifier-sizing decision; the remaining
[DR-3](decision-records/0003-target-spec-ratification.md) §(b) bounds
(GBW, phase margin, slew rate, quiescent power) read the committed sizing
pass's design point and predictions
([`design/opamp_sizing.md`](../design/opamp_sizing.md)), which is why they
could be set without originating any new sizing decision. Confirming or
revising any ratified target requires the PVT-cornered testbench suite
tracked by gap-to-T1 tracker
[#7](https://github.com/2AMLogic/gf180-opamp/issues/7) item 5 — per
`CLAUDE.md`'s "gm/ID first, committed to `sim/` before sizing" and the
guardrail that ratification precedes measurement.

## 3. What this table is (and is not)

- **Ratified (partial), not a claim of being met.** The ratification act is
  the operator's approval of the PR carrying [0003](decision-records/0003-target-spec-ratification.md)
  (ratification-via-PR policy,
  [2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357); two-key
  mechanism as this repo's pipeline applies it — Judge review plus
  Champion/operator merge), which disposes every row: §1's operating
  conditions and the §2 target rows are **ratified as targets**, and the
  five open rows are held in [0003](decision-records/0003-target-spec-ratification.md)'s
  residual register with their missing evidence named. **No row is ratified
  as met** — no circuit-level PVT evidence exists yet, and the provisional
  hand-built [`sim/gain-gbw-pm/`](../sim/gain-gbw-pm/README.md) sweep is
  explicitly not evidence toward any row.
- **Not a commitment that every open row will end up non-trivial.** The
  corner grid and load capacitance rows were determined jointly with the
  topology decision — see [decision record 0001](decision-records/0001-topology-and-cl.md).
  The DC gain and output swing rows carry their bounds from
  [decision record 0002](decision-records/0002-performance-target-bounds.md).
  The open rows (noise, offset target, CMRR, PSRR, area) each name exactly
  what evidence is missing, per [DR-3](decision-records/0003-target-spec-ratification.md) §(e).
- **Not opening the 5 V stretch row.** It is named, not scoped in — and the
  [0003](decision-records/0003-target-spec-ratification.md) ratification
  ratifies that closure.

## 4. Sources

- `CLAUDE.md` (this repo) — the row set and the 3.3 V-primary/5 V-stretch
  scope rule.
- [`gf180-bandgap` README](https://github.com/2AMLogic/gf180-bandgap#target-specification-ratified-2026-07-31-see-issue-1-and-35) — ratified target-spec table shape (Target / Stretch / Corner binding columns), statistical-basis wording (`3σ, mismatch MC N≥300 + process corners`), and binding-corner convention this table borrows.
- [`gf180-temp-por/spec/target-spec.md`](https://github.com/2AMLogic/gf180-temp-por/blob/main/spec/target-spec.md) — the standalone `spec/target-spec.md` file precedent (rather than inline in `README.md`) and the `[DR-n]`/`[P]`/`[TBD-#n]` value-tag convention.
- [`gf180-ldo/spec/architecture-survey.md`](https://github.com/2AMLogic/gf180-ldo/blob/main/spec/architecture-survey.md) — the "mark device-level numbers pending a future issue rather than guessing" practice this table follows for every `[TBD]` row.
- [Gap-to-T1 tracker, #7](https://github.com/2AMLogic/gf180-opamp/issues/7) — the artifact-presence checklist this spec's eventual evidence trail (`sim/`, `layout/`) will need to satisfy.
- [`spec/decision-records/0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md) — the topology (input-pair polarity, output-stage class, cascode-or-not) and `CL` decision behind this file's `[DR-1]`-tagged rows, argued from `sim/gm-id-characterization/records/20260909-052956-79c6a45.md`.
- [`spec/decision-records/0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md) — recommended bounds for §2's `[TBD]` performance rows: the `[DR-2]`-tagged DC-gain and output-swing targets, the offset row's ratified statistical-basis convention, and the per-row "evidence missing" reasons for every row still `[TBD]`.
- [`spec/decision-records/0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md) — the ratification record itself (issue #24): the per-row dispositions that flipped this table DRAFT → RATIFIED (partial), the three sizing-grounded target bounds (GBW, slew rate, quiescent power) and the phase-margin bound, the rejected conditional-45° escape, and the residual register behind every open row.
- [`design/opamp_sizing.md`](../design/opamp_sizing.md) — the committed sizing pass (issue #17 / PR #21) whose design point and predictions ground the `[DR-3]` GBW, slew-rate, and quiescent-power targets.
- [`sg13g2-opamp`/`sky130-opamp` twins](https://github.com/2AMLogic/sg13g2-opamp) — the three-foundry twin target-specs this table's ratification aligns with in shape: the `sg13g2-opamp` twin's DR-0002 partial-ratification (measured rows ratified, residuals registered) is the precedent [0003](decision-records/0003-target-spec-ratification.md) mirrors; the `sky130-opamp` twin's sizing-estimate `[P]` conventions and DR-001/DR-002 records are its still-DRAFT counterpart.
