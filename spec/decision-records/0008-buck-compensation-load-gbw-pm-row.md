# 0008: Load-specific GBW/PM row for the gf180-buck type-III compensation load — preparation record (issue #295)

- **Status**: **proposed — preparation draft. No numeric value is proposed,
  ratified or judged here.** This record carries operator ruling r7-q29
  (2026-11-06, recorded on #295): *open a load-specific GBW/PM row through a
  preparation-first decision record that states the consumer's actual load and
  a bounded validation bench, sets no number until simulated (as
  [0007](0007-noise-offset-requirement-basis.md) did), and brings any re-size
  and any re-opened ratified row back for ratification.* It changes no row,
  bound, tag or condition of `spec/target-spec.md`, authorizes no simulation,
  fleet submission or spend, and changes no circuit. Approving the PR that
  carries it ratifies **only** the staging and rules below, never a number.
- **Date**: 2026-11-06
- **Decided by**: Builder agent, issue #295 (preparation). The operator's
  approval of the carrying PR is the only act that could make any part of this
  binding; a later record carries the numbers.
- **Related**: #295; `2AMLogic/gf180-buck` #82 and its
  `spec/decision-records/0004-error-amplifier-gbw.md` (the consumer's request);
  [0001](0001-topology-and-cl.md) (CL = 2 pF);
  [0003](0003-target-spec-ratification.md) (GBW ≥ 10 MHz, PM ≥ 60° rows);
  [0005](0005-input-common-mode-range-row.md) (ICMR, proposed);
  [0007](0007-noise-offset-requirement-basis.md) (the preparation-first
  pattern followed here).
- **Claims made**: none measured, none simulated. Every number below is read
  from the sources named in §2 on 2026-11-06 or is hand arithmetic on them,
  and is labelled so. The consumer's own figures (PM −51.6°, 8 MHz stand-in)
  are its simulated, typical-corner, pre-layout results, quoted not
  reproduced.

## 1. Three things kept apart

| Step | What it is | State |
|---|---|---|
| **Design-to requirement** | A GBW/PM limit derived from the consumer's actual load by an explicit argument | **Not derivable yet**: the bench in §4 has not run. The row stays unopened. |
| **Ratification** | The operator's approval of a later PR carrying the derived row (and any re-size) | Not requested. Not satisfied by this record. |
| **Measured compliance** | PASS/FAIL of committed evidence against a ratified row, FAILs disclosed | Not possible before the two steps above. |

The consumer's "about 8 MHz" comes from a **single-pole stand-in** for the
amplifier, not from this amplifier in its load. It is an input to the question,
not a requirement, and is not adopted as a row bound here.

## 2. The ratified row, and why it is not the consumer's question

The ratified row is GBW ≥ 10 MHz and PM ≥ 60° into a **2 pF capacitive load**
at the open-loop output ([0001](0001-topology-and-cl.md),
[0003](0003-target-spec-ratification.md); measured 45/45 PASS, worst GBW
11.910 MHz). It **stays as it is**: this record does not touch it, and nothing
in it implies the row is wrong. The consumer's problem is that the amplifier
sits in a loop with a different load and a different input condition. Three
differences, from reading the sources (not from simulation):

### 2.1 The actual load (consumer netlist, read 2026-11-06)

`gf180-buck` `design/netlists/compensator.spice` and `design/compensator.md`
(the design-to values; ideal R/C, "nothing here is measured"):

```
Rtop VOUT FBN 10k
R3   VOUT X   204
C3   X    FBN 780p
Rbot FBN  GND 5k
R2   FBN  Z   4.55k
C1   Z    VC  1.75n
C2   FBN  VC  35.7p
XEA  VDD33 GND VREF FBN VC IBIAS opamp_two_stage   (vdd vss vinp vinn vout ibias)
```

Type-III (Venable), inverting. Zeros fz1 = fz2 = 20 kHz, poles fp1 = fp2 =
1 MHz, crossover target 150 kHz, 2 MHz switching, 1.8 V output from a 0.6 V
reference. The EA output `VC` also drives the PWM comparator input
(`pwm_comp.spice`, gate of an `nfet_03v3` 8 µm / 1 µm); that capacitance is not
quantified here and is an input to §4 (read from the consumer's netlist at
execution, not assumed).

### 2.2 What differs from the CL = 2 pF row (hand reading, unverified)

1. **The load is feedback-network impedance, not a capacitor.** `C1` blocks DC,
   so the DC load is light; but above fz1 (20 kHz) the EA output sees
   `Zf = (R2 + 1/sC1) ‖ 1/sC2` to the virtual-ground node `FBN`. Around 1 MHz
   `1/(2π·1 MHz·35.7 pF) = 4.46 kΩ`, comparable to R2 = 4.55 kΩ, so the output
   drives a resistive-capacitive load of order 4–5 kΩ, not 2 pF, at the
   frequencies that set the loop. The same output stage was sized for CL only.
2. **The relevant gain condition is the noise gain, not unity-gain GBW into
   CL.** At high frequency `1/β ≈ 1 + Zf/(Zi ‖ Rbot)` with `Zi ≈ Rtop ‖ R3`
   (≈ 200 Ω): `1 + 4.55 kΩ / (≈196 Ω) ≈ 24` (hand arithmetic, ≈ 27 dB). The
   amplifier's open-loop gain must clear that, with margin, out to the
   compensator's fp1/fp2 (1 MHz) and the loop's crossover, in *this* load. A
   GBW figure into 2 pF does not say where Aol(f) crosses `1/β` into 4–5 kΩ.
   This is a hypothesis for §4 to test, not a finding.
3. **The input common mode is outside the proposed ICMR.** The EA's `vinp` is
   `VREF` = 0.6 V and `vinn` = `FBN` ≈ 0.6 V (reference 1.2 V halved,
   `compensator.md`). The input pair is NMOS ([0001](0001-topology-and-cl.md),
   `XM1`/`XM2` `nfet_03v3`), and the proposed ICMR is **1.20 V ≤ VCM ≤ 2.70 V**
   at all PVT points ([0005](0005-input-common-mode-range-row.md), proposed,
   low edge set by tail headroom). 0.6 V is 0.6 V below that edge. Whether the
   amplifier operates at all at VCM = 0.6 V, and with what gain and GBW, is
   **unknown** and is the first thing §4 must establish: it may explain more of
   the consumer's −51.6° than any GBW figure, and it would mean the 8 MHz
   stand-in does not model the real amplifier. Raising bias (the consumer's
   option C) cannot address an input-range shortfall, which is consistent with
   its "PM −34° at 80 µA" observation, but that is an observation about the
   consumer's run, not a conclusion drawn here.

None of 2.2 is a claim that the amplifier cannot serve; each is a reason the
2 pF row neither proves nor disproves it.

## 3. Consumer inputs and open items

The consumer netlist above is the load of record **as of `gf180-buck` main on
2026-11-06**. The bench re-reads it at execution time and records the commit
sha; a changed load reopens §2.1, it is not silently followed.

| # | To | Item | Why needed |
|---|---|---|---|
| C1 | `gf180-buck` | Confirm the EA supply is the 3.3 V control rail (`VDD33`, its DR-0003), with the ±10 % band (2.97–3.63 V) this repo's grid uses. | Fixes the supply axis. |
| C2 | `gf180-buck` | Confirm the EA reference and feedback node are held at ≈ 0.6 V in every operating mode, including start-up (soft-start ramps the reference from 0). | Fixes the VCM axis, and shows whether a soft-start VCM sweep is in scope. |
| C3 | `gf180-buck` | State the PWM-comparator input capacitance on `VC` and the VC swing needed (ramp window 0.613 V p-p; DC level vs duty). | Completes the load and the output-swing requirement. |
| C4 | `gf180-buck` | Whether R13's PM ≥ 45°, GM ≥ 10 dB at 100–250 kHz crossover are the pass criteria this amplifier is graded against. | The criterion the derived row must support. |

C1–C4 are posted on the consumer's tracker (not here). An unanswered item is
recorded as "assumed" with the consumer that must confirm it; it does not
block Stage A below.

## 4. Bounded validation bench (proposal; execution is a separate approval)

Nothing here has been run. Stages are ordered by cost, each with a stop rule;
a later stage is requested only if an earlier one leaves the question open.
The ratified 10 MHz / 2 pF bench and its records are not modified; new
evidence is new, append-only records.

### Stage A — single probe, local, no fleet

One `ngspice -b` operating point plus one `.ac` at **typical / 27 °C / 3.30 V**
(a single unit, which the host rules allow locally): the committed
`opamp_two_stage` netlist (consumed through `subckt_from_export()` like the
existing benches, no transistor declared in the testbench) in the **exact §2.1
network**, VREF/`FBN` at 0.6 V, `ibias` 10 µA. Report: whether the input pair
conducts at VCM = 0.6 V and the operating-point regions of `XM1`/`XM2`/`XM5`;
Aol(f) into the real load; the open-loop crossing of `1/β`. **Stop rule:** if
the amplifier is not in a valid operating region at 0.6 V, stop; Stage B is not
run on an invalid point, and the result is reported as the finding (§5).

### Stage B — the real load across the existing PVT grid (batch)

Only if Stage A shows the amplifier operating.

| Item | Proposal |
|---|---|
| Question | Does this amplifier, in the §2.1 network, give the compensator transfer and the buck loop what the loop needs, across PVT? |
| Axes | The existing 45 points: `typical, ff, ss, fs, sf` × −40/27/125 °C × 2.97/3.30/3.63 V; typical passives, as every existing grid row. |
| Quantities | (1) `Gc_real(f) / Gc_ideal(f)` magnitude and phase, 10 kHz–10 MHz, where `Gc_ideal` is the consumer's ideal-op-amp type-III transfer; (2) Aol(f) in the load and its crossing of `1/β`; (3) EA output operating-point range and swing into the load; (4) the consumer-level PM and GM, obtained by inserting (1) into the consumer's published averaged plant read from its repo (read only; no consumer file is modified), reported against C4's criteria. |
| Execution | `klt sim` request with the 45 corners (batch backend, `--wait-for-capacity 600`); a failed submit is reported, not retried locally; at most two full re-submits. |
| Failures | Every failed or invalid point is kept and counted; nothing is dropped to obtain a bound. |
| Cost | Wall time and Spot cost are **unknown until a pre-submit estimate is produced**. The existing 45-point `.ac` bench is the closest precedent (single `.ac` per point); the estimate is made from its records at execution, not asserted here. |
| Stop rules | Stop and report if any point is invalid, an integrity check fails, wall time exceeds 2× the estimate, or the fleet refuses the runner-version gate without the documented `warn` path. |

### Stage C — out of scope here

Mismatch Monte Carlo in this load, independent passive corners, soft-start
VCM sweeps beyond C2's answer, closed-loop switched simulation, layout
parasitics, 5 V operation. Each needs its own scope; 5 V remains
named-not-opened.

### Approval gate

Stage A is a single local probe and may proceed with this PR's approval. Stage B runs
only after a separate operator approval that cites this record's PR, the
answers to C1–C4 (or their recorded "assumed" status) and the pre-submit
estimate. This record is not that approval.

## 5. How the outcome is routed (rules fixed before the answer)

1. **Open the row only from simulation.** A load-specific row, if opened, is
   derived mechanically from Stage B: its bound is the amplifier-side quantity
   the consumer's criterion (C4) needs, evaluated at the worst of the 45
   points, with the margin policy stated in the later record. A measured
   maximum plus margin is not a derivation. The row is a **new row** with its
   own load and conditions; the ratified 10 MHz / 2 pF row is untouched.
2. **If the amplifier meets the loop need as is**, the later record opens the
   row with the measured status and no re-size.
3. **If it cannot** (including the Stage A invalid-input-range outcome), the
   later record names the re-size (Cc, gm1, bias, or an input-stage change for
   a 0.6 V common mode) and **which ratified rows it re-opens** (gain, GBW, PM,
   slew, ICMR, power, noise, offset, CMRR/PSRR are all candidates, and a
   different input pair would touch most). It comes back for ratification; it
   does not edit the table, and no ratified row is relaxed to make a result
   pass. A change to the input-pair polarity reopens
   [0001](0001-topology-and-cl.md).
4. **If the target is out of this repo's scope**, the record says so, and the
   consumer records its own decision per its DR-0004 §4; it is not forced onto
   this block.
5. **Unset stays unset.** Without Stage B results the row remains unopened and
   the consumer's verdict is `unknown`, not `meets` and not `fails`.

## 6. What this record does not do

- It proposes no GBW, PM or other numeric value and changes no row.
- It does not run or authorize Stage B, any fleet submission or spend.
- It does not re-open any ratified row; it names what a later record would
  have to re-open if a re-size is needed.
- It does not alter historical records, evidence, CL = 2 pF, PVT scope, the
  3.3 V primary rail, or the classic-row definitions.
- It does not add `gf180-buck` to the target spec's Consumers section: that
  section follows `2AMLogic/2am` `repos.yml` `consumes:`, which is the update
  trigger and was not read or changed here. Whether to add the consumer there
  is the later record's decision.
- Closing #295 needs the later numeric (or out-of-scope) record; this PR uses
  `Part of #295`.

## 7. Exit criteria for the next record

The next record (DR-9 or next free number) may be opened when: C1–C4 are
answered or recorded "assumed"; Stage A output is committed; and the operator
has approved Stage B. It must cite those, apply §5 mechanically, and keep
derivation, ratification and measured verdict as separate sections.
