# Porting plan — what carries over from gf180-bandgap, gf180-ldo, and sg13g2-bandgap

**Status: engineering input, not a ratified decision.** This document is the
required reading for anyone starting design work on this block once
[`target-spec.md`](target-spec.md)'s `[TBD]` rows start getting filled in. It
does not ratify anything itself — no decision record exists yet in this
repo — and it does not perform any circuit design, schematic capture, or
simulation; it only names the nearest mature siblings and what a two-stage
Miller-compensated op-amp on gf180mcu can and cannot borrow from them.

**This is a same-PDK port, not a cross-PDK one.** `gf180-bandgap` and
`gf180-ldo` are both on GlobalFoundries GF180MCU — the identical PDK this
repo targets. That means none of the device-menu, model-library, or
voltage-flavor-naming translation work a cross-PDK port (e.g.
`sg13g2-bandgap`'s own IHP SG13G2 port from gf180-bandgap/sky130-bandgap)
requires is needed here. This plan is deliberately lighter than that
precedent's shape — closer in spirit to `gf180-ldo/spec/architecture-survey.md`,
a same-PDK architecture survey, than to a full cross-PDK device-by-device
translation table.

## Sources checked

- [`2AMLogic/gf180-bandgap`](https://github.com/2AMLogic/gf180-bandgap) —
  same-PDK, most mature block in the fleet (schematic, layout, DRC-clean,
  ratified target-spec). `design/bandgap_amp.sch` + `.sym` are its
  error-amplifier sub-block — this repo's nearest same-PDK amplifier design
  precedent.
- [`2AMLogic/gf180-ldo`](https://github.com/2AMLogic/gf180-ldo) — same-PDK.
  `design/error_amp.sch` + `.md` are its error-amplifier design work, and
  `spec/architecture-survey.md` is the structural model this plan's
  "same-PDK architecture survey, not a cross-PDK translation" framing is
  borrowed from — it traces every architectural recommendation back to a
  specific spec row and marks device-level numbers "pending #4" rather than
  guessing them, exactly the discipline this repo's own `[TBD]` tags in
  [`target-spec.md`](target-spec.md) follow.
- [`2AMLogic/sg13g2-bandgap`](https://github.com/2AMLogic/sg13g2-bandgap) —
  different PDK (IHP SG13G2), named for its `bandgap_amp` amp-characterization
  testbench shape, not for its device menu (which does not transfer — SG13G2
  is a different PDK entirely). `spec/porting-plan.md` is the structural
  model for **this** document's "what carries over unchanged" / "what
  changes, and why" / "what does not transfer, and why" section shape.
- [`2AMLogic/klayout-tools`, `docs/design-evidence-tiers.md`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/design-evidence-tiers.md) —
  the T1 ("sim-validated") checklist this plan's linked gap-to-T1 tracker,
  [#7](https://github.com/2AMLogic/gf180-opamp/issues/7), surveys.

## 1. What carries over unchanged

Same PDK means the *process* transfers wholesale; only the *topology* is new
(neither sibling is an op-amp).

- **The verification discipline.** PVT-cornered testbenches (per
  `CLAUDE.md`: gain, GBW/PM into stated CL, slew, noise, offset with
  statistical basis, CMRR/PSRR, swing, power, at PVT corners), append-only
  `sim/` records, no claim without a testbench. Identical across every block
  in the fleet regardless of PDK.
- **The friction protocol.** Tool gaps against `klayout-tools` get filed
  generically, design specifics stay out of that tracker. Unchanged by
  topology or PDK.
- **The gf180mcu device menu itself.** Because this is a same-PDK port, the
  3.3 V-primary MOS flavors, the poly-resistor flavors, and the 5 V-tolerant
  device flavors `gf180-bandgap` and `gf180-ldo` already characterize and use
  are the *same* devices this repo would use — no translation table is
  needed the way `sg13g2-bandgap`'s porting plan needed one to map gf180's
  device menu onto SG13G2's genuinely different one (bipolar device
  selection, voltage-flavor renaming, resistor-flavor TC tradeoffs). Device
  *numbers* (Vth, gm/ID sweeps, resistor sheet resistance) are not yet pulled
  into this repo — that is future device-characterization work — but the
  *menu* to characterize is already known and unchanged from the siblings.
- **The 3.3 V-primary / 5 V-stretch scope rule.** `CLAUDE.md`'s framing
  ("mirror how the gf180 Chipalooza briefs treat the 5 V rail... never mix
  device flavors in one variant without a record") is the identical rule
  `gf180-bandgap`'s and `gf180-ldo`'s own ratified specs already apply — not
  a new decision this repo has to make, just the same one carried over.
- **gm/ID-first sizing.** `CLAUDE.md`'s "gm/ID first, committed to `sim/`
  before sizing" is the same practice both same-PDK siblings follow; nothing
  about a two-stage op-amp topology changes that ordering.
- **The decision-record process itself**, once one is needed. No
  `spec/decision-records/` directory exists in this repo yet (this bootstrap
  pass makes no decisions requiring one — see "Affected files" note in issue
  #2). When the first real decision is made (e.g. picking a compensation
  scheme, or opening the 5 V stretch row), it should follow one of the two
  precedented conventions — `gf180-bandgap`'s `NNNN-<slug>.md` or
  `gf180-ldo`'s `DR-NNNN-<slug>.md` — picked once and kept consistent within
  this repo; the fleet has not converged on one, so neither choice is wrong.

## 2. What changes, and why

Both same-PDK siblings are a bandgap reference and an LDO — neither is an
amplifier-as-the-whole-block the way this repo is. The error amplifiers
embedded in each sibling are sub-blocks tuned to their own host circuit's
needs (a bandgap's low-bandwidth reference loop; an LDO's output-pole-dominant
feedback loop), not standalone-characterized op-amps with their own gain/
GBW/PM/CMRR/PSRR/swing/power spec table. This is exactly the gap `README.md`
already names: *"LDO error amplifiers, ADC drivers, and filter stages across
the gf180 canaries embed op-amps that have never been standalone-characterized
on this PDK."*

| Aspect | gf180-bandgap / gf180-ldo | gf180-opamp | Why it changes |
|---|---|---|---|
| What is being specced | An error amplifier *inside* a larger loop (bandgap reference loop; LDO regulation loop), specced only indirectly through the host block's own rows (PSRR, line/load regulation, output accuracy) | A standalone two-stage Miller-compensated op-amp, specced directly on its own classic rows (gain, GBW/PM into stated CL, slew, noise, offset, CMRR/PSRR, swing, power) | Neither sibling's spec table has an amplifier-level GBW/PM/slew/CMRR row at all — those rows do not exist to copy, only the host-level rows their amplifier subserves. This repo has to originate its own row set from `CLAUDE.md`, not extract one from a sibling's ratified table. |
| Compensation | Sized against each host loop's own stability requirement (bandgap: a low-bandwidth reference node; LDO: an output pole set by the external cap per `gf180-ldo/spec/architecture-survey.md` §4) | Miller compensation between the two gain stages, sized against a stated external `CL` per `CLAUDE.md` — a different compensation topology and a different design freedom (no external LDO output cap or bandgap-loop pole to lean on) | This block's `CL` load-capacitance target (currently `[TBD]` in `target-spec.md`) plays the role `gf180-ldo`'s external 1 µF ±ESR cap plays in that survey — but it has to be chosen for a general-purpose op-amp with no fixed downstream load, whereas both siblings' compensation targets are fixed by their host circuit's own known load. |
| Amplifier-characterization testbench shape | Neither sibling ships a standalone open-loop-gain / GBW / phase-margin / CMRR / PSRR testbench for its embedded amplifier — those quantities are measured only at the host-block level (e.g. `gf180-bandgap`'s output-reference accuracy row, not an amplifier open-loop-gain row) | Needs the full classic-row testbench suite `sg13g2-bandgap`'s `bandgap_amp` characterization work models (open-loop AC sweep for gain/GBW/PM, transient step for slew, noise analysis, CMRR/PSRR AC sweeps, swing sweep, Iq measurement) | `sg13g2-bandgap` is the fleet's nearest example of *characterizing an amplifier sub-block on its own terms* rather than only through a host loop — its testbench shape transfers even though its PDK does not. |
| Input/output common-mode range | Bounded by each host circuit's own fixed internal nodes (e.g. an LDO error amp's inputs pinned to Vref and the feedback-divider tap) | Must be specified in general, since a standalone op-amp has no fixed host context — this is exactly the new "swing" and "CMRR" rows in `target-spec.md` that neither sibling's ratified table carries | A standalone amplifier's common-mode/output-swing spec is a first-class deliverable here; for both siblings it was an internal implementation detail of a larger loop, never separately specified or ratified. |

## 3. What does not transfer, and why

- **Either sibling's literal amplifier schematic.** `gf180-bandgap`'s
  `bandgap_amp.sch` and `gf180-ldo`'s `error_amp.sch` are each sized for a
  narrow, fixed internal role (correcting a bandgap core's PTAT/CTAT
  mismatch; driving a pass-FET gate inside a regulation loop) — neither is a
  general-purpose, standalone two-stage Miller-compensated op-amp meant to
  drive an arbitrary external `CL`. Copying either schematic wholesale would
  inherit sizing decisions made for a different design problem.
- **Either sibling's host-level spec rows as this block's own rows.**
  `gf180-bandgap`'s PSRR row (measured at the bandgap's output-reference
  node) and `gf180-ldo`'s PSRR row (measured at the regulated output) are
  both *system*-level PSRR figures dominated by loop gain around the whole
  reference/regulation loop, not an *amplifier*-level PSRR figure measured
  open-loop or in a standard op-amp test configuration. This repo's PSRR row
  needs its own testbench definition (nearer to `sg13g2-bandgap`'s
  amplifier-level characterization shape), not a copy of either host-level
  number or test setup.
- **`sg13g2-bandgap`'s cross-PDK device-selection decision records** (its
  DR-0001 bipolar-device-selection, DR-0002 supply-voltage-scope). Those
  exist specifically because SG13G2's device menu differs from gf180's/
  sky130's — a question this repo does not face at all, since it is on the
  identical PDK as its two named same-PDK siblings. No equivalent
  device-selection decision record is needed here for that reason; if this
  repo does need a decision record early on, it is more likely to be about
  topology (e.g. output-stage class, cascode-vs-not) or the 5 V-stretch
  scope, not device selection.
- **`gf180-bandgap`'s or `gf180-ldo`'s own numeric mismatch/PVT evidence.**
  Any measured coefficient (resistor mismatch risk bounds, dropout/headroom
  arithmetic) in either sibling's decision records is specific to that
  circuit's own devices and operating point. Only the *practice* of
  measuring rather than assuming transfers, per `CLAUDE.md`'s "no claim
  without a testbench" — the numbers themselves do not.

## 4. Open items and next steps

- **Topology decision.** Neither same-PDK sibling's amplifier schematic
  transfers directly (§3), so the first real design decision this repo needs
  is its own two-stage Miller-compensated topology choice (single-ended vs.
  fully differential first stage, output-stage class, cascode-or-not) — not
  yet made, and out of scope for this bootstrap pass.
- **Load capacitance (`CL`) target.** `target-spec.md`'s GBW/PM rows are
  stated "into stated CL" per `CLAUDE.md`, but no CL value has been chosen
  yet — see §2's "Compensation" row above. Choosing one is part of the
  topology decision, not independent of it.
- **Device characterization.** `CLAUDE.md`'s "gm/ID first" ordering means the
  next concrete step, once this bootstrap pass merges, is a gm/ID
  characterization sweep over gf180mcu's 3.3 V MOS flavors, committed to
  `sim/` before any sizing work — the same practice `gf180-bandgap` and
  `gf180-ldo` both already followed on this PDK.
- **Gap-to-T1 tracker.** [#7](https://github.com/2AMLogic/gf180-opamp/issues/7)
  tracks the block's current distance from the klayout-tools T1
  ("sim-validated") design-evidence tier — every checklist item is currently
  unmet, since no schematic/layout/sim work has started. It is the natural
  place future passes record progress against items 1–10 as design work
  lands.
