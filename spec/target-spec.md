# Target specification — gf180-opamp

- **Status**: **RATIFIED (partial)** — per the ratification-via-PR policy
  ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)), the
  ratification act is the operator's approval of the PR that carries
  [`spec/decision-records/0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md)
  (issue #24; reviewed through this repo's two-key pipeline — Judge review
  plus Champion/operator merge). That record disposes **every row of this
  table individually**: each row below is **ratified as a target** (a
  design-to bound — **never "met"** by the ratification act itself, which
  cited no circuit-level PVT evidence; the ratification-time snapshot is
  [0003](decision-records/0003-target-spec-ratification.md)'s Context,
  unchanged) or **explicitly open** in [0003](decision-records/0003-target-spec-ratification.md)'s
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
residual register. Three things are kept distinct throughout: *target
ratification* (this table's dispositions), *measurement availability*
(committed records under `sim/`) and *measured compliance* (a verdict
against a ratified bound). Schematic-level, passives-at-typical PVT
measurements now exist for gain, GBW, phase margin, noise, CMRR and PSRR
(45-point grid), and for offset mismatch (five process corners at 27 °C /
3.30 V only); they are cited per row in §2 and are not repeated here.

## How to read this table

**Value tags** — every non-definitional value carries one, following
[`gf180-temp-por/spec/target-spec.md`](https://github.com/2AMLogic/gf180-temp-por/blob/main/spec/target-spec.md)'s
convention, so a future reviewer can tell a carried decision from a new
proposal at a glance:

| Tag | Meaning |
|---|---|
| **[DR-n]** | Carried unchanged from decision record `n`, **and ratified** — carried into force by [0003](decision-records/0003-target-spec-ratification.md), the ratification act for this whole table. `[DR-1]` = [`spec/decision-records/0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md). `[DR-2]` = [`spec/decision-records/0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md). `[DR-3]` = [`spec/decision-records/0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md) (the ratification record itself). **Exception:** `[DR-5]` = [`spec/decision-records/0005-input-common-mode-range-row.md`](decision-records/0005-input-common-mode-range-row.md) tags a row **proposed** after DR-3. It is ratified only by the operator's approval of the PR that carries 0005 (2am#357), and every use of the tag says so in-row. |
| **[P]** | **Proposed, not ratified** — an engineering placeholder. Post-ratification the **only** `[P]` value left is §1's deliberately un-opened 5 V stretch row: its non-opening is structural (`CLAUDE.md` requires its own decision record to open), so it stays `[P]` rather than being ratified or removed — the same shape the `sg13g2-opamp` twin's post-ratification table uses for its own HV row. |
| **[TBD]** | Deliberately unset and **explicitly open** — [0003](decision-records/0003-target-spec-ratification.md)'s residual register names the exact missing evidence per row (never a silent open). Filled in by a decision record once the open bound can be argued from evidence — for noise, offset, CMRR and PSRR the measurements now exist (§2) and only the ratified bound/band is missing; for area, layout is still missing. Tracked collectively under the gap-to-T1 tracker, [#7](https://github.com/2AMLogic/gf180-opamp/issues/7) (item 5, "Full PVT corner simulation vs a ratified spec"), rather than one issue per row — no per-row characterization issue has been filed yet. |

**Status** column values in use: `Ratified [DR-3] as target — not met` (the
row's bound is a ratified design-to target and no committed bench has
measured it, so it is not met; **nothing is ratified as met**; no row
currently carries this value);
`Ratified [DR-3] as target — measured: PASS/FAIL …` (a committed record
judged the bound, cited in-row); `Open [DR-3] — <reason>` (a `[TBD]` row
held open by the residual register's named missing evidence, with any
measurement that already exists cited in-row but issuing no verdict). The pre-ratification vocabulary (`not started`)
described the same rows before the [0003](decision-records/0003-target-spec-ratification.md)
pass.

**Binding corner** — the corner at which a row's hard edge is expected to
bind, reasoned from the topology's *generic* behavior (a two-stage
Miller-compensated op-amp) at ratification time, before the schematic was
simulated. This is a **prediction**, not a measurement — CLAUDE.md's "no
claim without a testbench" applies to any *pass/fail* verdict on these rows.
A `sim/` record's full PVT grid supersedes the prediction where one exists:
the gain, GBW and phase-margin records name measured worst corners in the
Status column (which differ from the predictions for GBW and phase margin),
and the other rows' predictions stand until measured.

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
| Open-loop DC gain | **≥ 60 dB [DR-2]** | ≥ 70 dB [DR-2] | — (deterministic corner-worst-case candidate) | SS / −40 °C (lowest gm, highest output impedance loss) | **Ratified [DR-3] as target — measured: PASS at all 45 PVT points** (worst 93.79 dB at SS / 125 °C / 2.97 V; the ≥ 70 dB stretch also holds at 45/45; [record `20261009-055759-2524b3e`](../sim/gain-gbw-pm/records/20261009-055759-2524b3e.md); see [DR-3](decision-records/0003-target-spec-ratification.md) §(b)) |
| GBW (into stated CL = 2 pF [DR-1] above) | **≥ 10 MHz [DR-3]** — sized design point: `gm1 = 60.3 µS` (as-simulated 58.64 µS) with `CC = 0.619 pF` gives ≈ 15.5 MHz predicted nominal ([`design/opamp_sizing.md`](../design/opamp_sizing.md) §4, §6); target set ≈ 35% below the prediction for PVT margin ([DR-3](decision-records/0003-target-spec-ratification.md) §(b)) | — | — | SS / −40 °C / low VDD (slowest devices) | **Ratified [DR-3] as target — measured: PASS at all 45 PVT points** (worst 10.42 MHz at SS / 125 °C / 2.97 V; [record `20261009-055759-2524b3e`](../sim/gain-gbw-pm/records/20261009-055759-2524b3e.md)) |
| Phase margin (at GBW, same CL) | **≥ 60° [DR-3]** | — (the pre-ratification conditional "≥ 45° at FF/hot if unreachable" escape was rejected by [DR-3](decision-records/0003-target-spec-ratification.md) §(d) as a pre-authorized relaxation; a genuine future impossibility goes through a superseding DR, never the table) | — (deterministic corner-worst-case) | FF / 125 °C (fastest devices, most peaking risk) | **Ratified [DR-3] as target — measured: FAIL, 15/45 PVT points pass** (worst 57.34° at FS / 125 °C / 2.97 V; typical / 27 °C / 3.30 V is 59.49°; [record `20261009-055759-2524b3e`](../sim/gain-gbw-pm/records/20261009-055759-2524b3e.md); RZ PVT-tracking quantification is the row's verification obligation — [DR-3](decision-records/0003-target-spec-ratification.md) §(d)) |
| Slew rate | **≥ 10 V/µs [DR-3]** — sized prediction `Itail/CC = 10 µA / 0.619 pF ≈ 16.2 V/µs` ([`design/opamp_sizing.md`](../design/opamp_sizing.md) §6); target ≈ 38% below it ([DR-3](decision-records/0003-target-spec-ratification.md) §(b)) | — | — | SS / −40 °C / low VDD (lowest tail-current headroom) | **Ratified [DR-3] as target — measured: PASS at all 45 PVT points** (worst 14.51 V/µs at SS / 125 °C / 2.97 V; [record `20261009-142137-1dab1db`](../sim/slew-swing-power/records/20261009-142137-1dab1db.md); MOS/temperature/supply grid only, passives typical) |
| Input-referred noise | **[TBD]** — **Open [DR-3]**, residual (e1): a ratifiable bound is a *full-band* figure; the sizing pass's chosen `gm1` discharges [DR-2] §(c)'s bias-current half, the integration band remains unchosen, and a noise measurement now exists (candidate bands only) — a thermal-floor-only number would not rate this row | — | n/a until a band is set | n/a | Open [DR-3] — measured, no verdict: 45-point `.noise` grid, highest 62.90 µV rms over 100 Hz–1 MHz at SS / 125 °C / 3.63 V, candidate bands only ([record `20261009-082007-68b4567`](../sim/noise/records/20261009-082007-68b4567.md)); no ratified band or bound (see [DR-3](decision-records/0003-target-spec-ratification.md) §(e1)) |
| Input-referred offset | **[TBD]** — **Open [DR-3]**, residual (e2) as of ratification: a numeric 3σ target needed a mismatch Monte-Carlo pass or PDK mismatch-model data; the pass has since been committed but no bound is ratified (the `3σ` basis itself is ratified — see the statistical-basis column); see [DR-3](decision-records/0003-target-spec-ratification.md) §(e2) | — | **3σ, mismatch MC N≥300 + process corners [DR-2]** — ratified by [DR-2], carried into force by [DR-3]; matching `gf180-bandgap`'s ratified convention and the same convention independently proposed by the `sg13g2-opamp`/`sky130-opamp` twins; sample count not yet re-derived for this topology | to be determined once a topology is drawn — likely SS/FF split-corner pairing on the input differential pair | Open [DR-3] — measured, no verdict: mismatch MC, five process corners × N = 300 at 27 °C / 3.30 V only (no temperature or supply axis); σ 4.34–5.01 mV, worst \|mean\| + 3σ 15.64 mV at SF ([record `20261009-072205-96bf3cc`](../sim/offset-mc/records/20261009-072205-96bf3cc.md)); no numeric bound ratified |
| CMRR | **[TBD]** — **Open [DR-3]**, residual (e3) as of ratification: needed a schematic and a common-mode AC testbench (the gm/ID sweep characterizes each device in isolation); the bench has since been committed, the bound remains unratified; see [DR-3](decision-records/0003-target-spec-ratification.md) §(e3) | — | — (deterministic corner-worst-case) | to be determined | Open [DR-3] — measured, no verdict: 45-point grid, systematic-only (perfectly matched devices; mismatch-limited CMRR not covered), lowest DC-plateau 95.42 dB at SS / 125 °C / 2.97 V ([record `20261009-105631-30ec86d`](../sim/cmrr/records/20261009-105631-30ec86d.md)); no ratified bound |
| PSRR | **[TBD]** — **Open [DR-3]**, residual (e4) as of ratification: needed a schematic and a supply-injection AC testbench (the gm/ID sweep has no supply-voltage axis); the bench has since been committed, the bound remains unratified; see [DR-3](decision-records/0003-target-spec-ratification.md) §(e4) | — | — (deterministic corner-worst-case) | to be determined | Open [DR-3] — measured, no verdict: 45-point grid, systematic-only, lowest PSRR+ DC-plateau 98.45 dB at FS / −40 °C / 2.97 V, falling to 0.86 dB at the differential unity-gain frequency (SF / −40 °C / 2.97 V) ([record `20261009-105929-30ec86d`](../sim/psrr/records/20261009-105929-30ec86d.md)); no ratified bound |
| Input common-mode range (follower-biased: unity-gain-follower DC operating point, output within 0.10 V of VCM; `CL` = 2 pF [DR-1], `Ibias` = 10 µA) | **1.20 V ≤ VCM ≤ 2.70 V at all PVT points [DR-5] — proposed, not ratified** (in-range test: DC-plateau gain ≥ 60 dB [DR-2] and input pair, tail, mirror and output stage saturated; [0005](decision-records/0005-input-common-mode-range-row.md)) | — | — (deterministic corner-worst-case; systematic only, mismatch not covered) | measured: SS / −40 °C / 2.97 V (low edge, tail headroom); FS / 125 °C / 2.97 V (high edge, output-stage headroom in the follower) | **Proposed [DR-5] — measured: covered at all 45 PVT points** (conservative 45-point intersection [1.185, 2.705] V at 5 mV brackets; 1.20 V passes 45/45, smallest margin XM5 +11.1 mV at SS / −40 °C / 2.97 V — a thin low edge; [record `20261009-222613-871d1a6`](../sim/input-common-mode/records/20261009-222613-871d1a6.md)) |
| Output swing | **≥ 2.3 Vpp (≈78% of VDD,min) [DR-2]** | ≥ 2.6 Vpp (≈88%) [DR-2] | — | low VDD / worst output-stage headroom corner | **Ratified [DR-3] as target — measured: PASS at all 45 PVT points** (worst 2.46 Vpp at SS / 125 °C / 2.97 V; the ≥ 2.6 Vpp stretch holds at 35/45; [record `20261009-142137-1dab1db`](../sim/slew-swing-power/records/20261009-142137-1dab1db.md); MOS/temperature/supply grid only, passives typical) |
| Quiescent power | **≤ 350 µW worst-case corner [DR-3]** — as-simulated nominal 80.70 µA × 3.3 V = 266.3 µW; worst-case edge derived via the same-topology twin's measured ×1.2 corner-current spread (`sg13g2-opamp`, 99.9→119.7 µA) at the max rail: ≈ 351 µW, target set just under it ([DR-3](decision-records/0003-target-spec-ratification.md) §(b); [`design/opamp_sizing.md`](../design/opamp_sizing.md) §7) | — | — (deterministic corner-worst-case) | FF / 125 °C / 3.63 V (leakage + fastest devices) — matches `gf180-bandgap`'s ratified Iq binding-corner convention | **Ratified [DR-3] as target — measured: PASS at all 45 PVT points** (worst 310.98 µW at FF / −40 °C / 3.63 V; [record `20261009-142137-1dab1db`](../sim/slew-swing-power/records/20261009-142137-1dab1db.md); MOS/temperature/supply grid only, passives typical) |
| Area | **[TBD]** — **Open [DR-3]**, residual (e5): a post-layout quantity; no `layout/` content exists yet beyond a placeholder ([DR-3](decision-records/0003-target-spec-ratification.md) §(e5)) | — | n/a (not a PVT line) | n/a | Open [DR-3] — no layout |

Slew rate, output swing and quiescent power are measured and pass at 45/45
PVT points ([record `20261009-142137-1dab1db`](../sim/slew-swing-power/records/20261009-142137-1dab1db.md)).
Area and post-layout verification depend on layout, which has no committed
evidence. All measured results above are schematic-level with passives at
typical only: the PVT grid varies the MOS corner, temperature and supply, and
independent resistor/capacitor corners are not covered (record policy line,
see also `20261009-143715-4d5aa43`), so "PASS" means MOS/T/VDD-grid only.

The input common-mode range row is **not** one of
[DR-3](decision-records/0003-target-spec-ratification.md)'s dispositions.
[0005](decision-records/0005-input-common-mode-range-row.md) adds it
afterwards (issue #60) as a **proposal**: under the same
ratification-via-PR policy, its ratification act is the operator's approval
of the PR carrying 0005, and until then the bound binds nothing. It adds a
row and changes no existing row, bound or tag.

Every `[TBD]` row above is deliberately left **open** rather than
guessed — an explicit
[DR-3](decision-records/0003-target-spec-ratification.md) open-verdict with
its residual-register entry, never a silent one — per `CLAUDE.md`'s "no
claim without a testbench." Every ratified target above is a **design-to
bound, not a met result** (three have now been judged by a committed record: gain and GBW pass, phase margin fails): the [DR-2] bounds (DC gain, output swing) were
argued from the committed gm/ID device data and [DR-1]'s topology/`CL`
choices without any new amplifier-sizing decision; the remaining
[DR-3](decision-records/0003-target-spec-ratification.md) §(b) bounds
(GBW, phase margin, slew rate, quiescent power) read the committed sizing
pass's design point and predictions
([`design/opamp_sizing.md`](../design/opamp_sizing.md)), which is why they
could be set without originating any new sizing decision. Confirming or
revising any ratified target requires the remaining PVT-cornered benches
(slew, swing, power) and layout evidence tracked by gap-to-T1 tracker
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
  as met.** The ratification act cited no circuit-level evidence; the
  provisional hand-built early sweep it disclaimed has been superseded by
  the committed schematic-level
  [`sim/gain-gbw-pm/`](../sim/gain-gbw-pm/README.md) record cited in §2.
- **Not a commitment that every open row will end up non-trivial.** The
  corner grid and load capacitance rows were determined jointly with the
  topology decision — see [decision record 0001](decision-records/0001-topology-and-cl.md).
  The DC gain and output swing rows carry their bounds from
  [decision record 0002](decision-records/0002-performance-target-bounds.md).
  The open rows (noise, offset target, CMRR, PSRR, area) each name what
  is still missing (for the first four, a ratified bound rather than a
  measurement; for area, layout), per [DR-3](decision-records/0003-target-spec-ratification.md) §(e).
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
- [`spec/decision-records/0005-input-common-mode-range-row.md`](decision-records/0005-input-common-mode-range-row.md) — the **proposed** input common-mode range row (issue #60): the measured 45-point follower-biased ICMR ([`sim/input-common-mode/`](../sim/input-common-mode/README.md)), the proposed 1.20–2.70 V bound and its basis, and the consumer re-grading. It is not ratified before the operator's approval of its carrying PR.
- [`design/opamp_sizing.md`](../design/opamp_sizing.md) — the committed sizing pass (issue #17 / PR #21) whose design point and predictions ground the `[DR-3]` GBW, slew-rate, and quiescent-power targets.
- [`sg13g2-opamp`/`sky130-opamp` twins](https://github.com/2AMLogic/sg13g2-opamp) — the three-foundry twin target-specs this table's ratification aligns with in shape: the `sg13g2-opamp` twin's DR-0002 partial-ratification (measured rows ratified, residuals registered) is the precedent [0003](decision-records/0003-target-spec-ratification.md) mirrors; the `sky130-opamp` twin's sizing-estimate `[P]` conventions and DR-001/DR-002 records are its still-DRAFT counterpart.

## Consumers (non-normative)

This section is **non-normative context**: it names the fleet blocks that
consume this one and carries the requirement rows they impose, so the
question *"can `gf180-ldo` / `gf180-bandgap` use this block?"* is answerable
from this repo rather than by reading four. It asserts **no new target and
changes no row value** — [0003](decision-records/0003-target-spec-ratification.md)'s
per-row dispositions are untouched, and no `meets` verdict below is a claim
that this block **meets its own targets**: this table's rows are ratified
design-to bounds (measured status per row in §2: phase margin currently fails
its own target), so every verdict is a *target-vs-requirement* comparison
only. `unknown` marks rows resting on
this table's open residuals. The structured integrator view (top cell, port
list, netlist/GDS paths, area, maturity rung) is published as data at a
fixed path — [`manifests/integrator.json`](../manifests/integrator.json) —
not as prose here.

**Source of truth for the consumer list**:
[`2AMLogic/2am` `repos.yml`](https://github.com/2AMLogic/2am/blob/main/repos.yml)
`consumes:` map — as of 2026-09-22 it records exactly two consumers of
`gf180-opamp`:

- `gf180-bandgap` → `consumes: [gf180-opamp]` — no slot named.
- `gf180-ldo` → `consumes: [gf180-opamp, gf180-bandgap]`, annotated *"error
  amp; VREF is a top-level port (its DR-0021) a bandgap plugs into"* — the
  `error amp` clause is this block's slot; the `VREF` clause describes
  `gf180-bandgap`'s.

A **new `consumes:` entry in `repos.yml` is the update trigger** for this
section — a consumer not named there gets no row here. Findings about a
consumer's own embedded block belong on **that consumer's tracker**, not
here: see [`porting-plan.md`](porting-plan.md) §5.

### `gf180-ldo` — error-amp slot

`gf180-ldo` embeds its own error amplifier today
([`error_amp.sch`](https://github.com/2AMLogic/gf180-ldo/blob/main/design/error_amp.sch)):
**not the same block** — a host-loop amplifier sized to its regulation
loop's needs, carrying no amplifier-level GBW/PM/slew/CMRR rows of its own;
the shape argument is recorded once in
[`porting-plan.md`](porting-plan.md) §2, not re-derived here. Notably, its
own [`spec/architecture-survey.md`](https://github.com/2AMLogic/gf180-ldo/blob/main/spec/architecture-survey.md)
§5 shortlists a **two-stage Miller-compensated OTA** (its candidate 3,
~10–15 µA bias allocation) as a primary option for exactly this slot — the
shape this block is.

| Requirement row | `gf180-ldo` imposes | This block's ratified spec | Verdict | Basis |
|---|---|---|---|---|
| Port list | error-amp slot: reference input + feedback-divider-tap input, pass-FET-gate output, single supply (survey §2 rows 1–2, §3.1) | `vdd`, `vss` (inout); `vinp`, `vinn` (in); `vout` (out); `ibias` (in) — recorded as data in [`manifests/integrator.json`](../manifests/integrator.json) | **meets** (shape only — the ratified table carries no port row, so this is a structural match against the committed port list, not a spec verdict) | [`manifests/integrator.json`](../manifests/integrator.json) vs survey §2/§3.1 |
| Rails | 3.3 V ±10% single supply (survey row 1); its 5 V input stretch flags an amplifier-headroom question still open (survey §3.4) | VDD **3.3 V ±10%** [DR-3]; 5 V stretch row **named-not-opened** [P] | **meets** (3.3 V, same ratified row); **unknown** (5 V — not opened) | §1 VDD rows; opening 5 V requires its own decision record, never a consumer row |
| Input range | inputs sit at VREF (`gf180-bandgap`'s 1.20 V output) and the divider tap ≈ VREF (survey §2 row 2). The consumer names VREF = 1.20 V with **no amplifier-level tolerance**, so the required interval is the point [1.20 V, 1.20 V]. | Input common-mode range **1.20 V ≤ VCM ≤ 2.70 V** at all PVT points, follower-biased [DR-5] (**proposed**, not ratified before its PR's approval) | **meets** for the nominal 1.20 V input; **unknown** for any tolerance around VREF (none is specified) | §2 input-common-mode row and [0005](decision-records/0005-input-common-mode-range-row.md): 1.20 V lies inside the proposed bound, and the explicit 1.20 V sample passes at 45/45 PVT points ([record `20261009-222613-871d1a6`](../sim/input-common-mode/records/20261009-222613-871d1a6.md)). The measured low edge is 1.185 V at SS / −40 °C / 2.97 V, which is the headroom any future VREF tolerance would be compared against. No tolerance is invented here. |
| Speed | no amplifier-level GBW row; loop UGBW estimated in the tens-to-low-hundreds of kHz (survey §2 row 5) | GBW **≥ 10 MHz** into CL = 2 pF [DR-3]; slew **≥ 10 V/µs** [DR-3] | **meets** (ratified target ≥ 10 MHz ≫ ~0.1 MHz loop need) | §2 GBW row — target-vs-requirement comparison only; the GBW row's own measured status (PASS at 45/45 points) is cited in §2 and is not altered by this comparison |
| Offset | no numeric amplifier-level row; load-reg < 1% is system-level (survey §2 row 7) | **[TBD] open** | **unknown** | §2 offset row, open [DR-3 residual e2] |
| Noise | no amplifier-level row | **[TBD] open** | **unknown** | §2 noise row, open [DR-3 residual e1] |
| Area budget | whole-regulator < 0.1 mm² ex pad ring (survey row 8); no amplifier carve-out stated | **[TBD] open** | **unknown** | §2 area row, open [DR-3 residual e5] |

### `gf180-bandgap` — amplifier slot (unspecified)

`gf180-bandgap` embeds its own amplifier
([`bandgap_amp.sch`](https://github.com/2AMLogic/gf180-bandgap/blob/main/design/bandgap_amp.sch)):
**not the same block** — a low-bandwidth reference-loop error amplifier, per
the same [`porting-plan.md`](porting-plan.md) §2 argument. Its ratified spec
rows (output reference, PSRR, output noise, Iq) are all **system-level**
quantities at the bandgap's output-reference node; none imposes an
amplifier-level requirement on this block, so most rows below are `Unknown`
by the consumer's own omission — named, not silently dropped.

| Requirement row | `gf180-bandgap` imposes | This block's ratified spec | Verdict | Basis |
|---|---|---|---|---|
| Port list | no amplifier-level port requirement stated (its amplifier is an internal host-loop sub-block) | port list as recorded in [`manifests/integrator.json`](../manifests/integrator.json) | **unknown** | [`porting-plan.md`](porting-plan.md) §2 — no amplifier-level rows exist to impose |
| Rails | supply 3.3 V ±10%, "also 5 V flavor" named (its ratified spec table) | VDD **3.3 V ±10%** [DR-3]; 5 V stretch row **named-not-opened** [P] | **meets** (3.3 V, same ratified row); **unknown** (5 V — not opened) | §1 VDD rows; opening 5 V requires its own decision record |
| Input range | no amplifier-level row | Input common-mode range **1.20 V ≤ VCM ≤ 2.70 V**, follower-biased [DR-5] (**proposed**) | **unknown** | no requirement to compare against. This block's row now exists (§2 input-common-mode row, [0005](decision-records/0005-input-common-mode-range-row.md)), but `gf180-bandgap` states no amplifier-level input range. |
| Speed | no amplifier-level speed row (its PSRR row is at the output-reference node, DC–1 kHz) | GBW/slew rows as above | **unknown** (no requirement to compare) | [`porting-plan.md`](porting-plan.md) §2–§3 |
| Offset | no amplifier-level row (output-reference ±2% is system-level) | **[TBD] open** | **unknown** | §2 offset row, open [DR-3 residual e2] |
| Noise | no amplifier-level row (output-noise row is at the output-reference node, band TBD) | **[TBD] open** | **unknown** | §2 noise row, open [DR-3 residual e1] |
| Area budget | whole-block < 0.05 mm²; no amplifier carve-out | **[TBD] open** | **unknown** | §2 area row, open [DR-3 residual e5] |
