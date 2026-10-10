# 0006: CMRR and PSRR rows — propose bounds with a stated frequency and statistical basis (DR-3 residuals (e3), (e4))

- **Status**: **proposed — not ratified.** This record proposes numeric bounds
  for two rows that [0003](0003-target-spec-ratification.md) left `[TBD]`. It
  changes no other row, bound, tag or condition, and it relaxes nothing that is
  ratified. Under the ratification-via-PR standing policy
  ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)), which
  [0003](0003-target-spec-ratification.md) and [0005](0005-input-common-mode-range-row.md)
  applied to this table, the ratification act is the operator's approval of the
  PR that carries this record. In this repo's pipeline that is Judge review
  plus Champion/operator merge. Until that act happens nothing here binds: the
  two rows are proposals and the comparisons they feed are provisional. This
  line will still read `proposed` after the merge, because no automation
  rewrites a merged record. A later reader should treat the merge commit of the
  carrying PR as the ratification act, and nothing earlier.
- **Date**: 2026-10-10
- **Decided by**: Builder agent, issue #61 (proposal and evidence). The
  operator's approval of the carrying PR is the ratification act.
- **Related**: #61 (this record's issue);
  [`sim/cmrr-mc/records/20261010-035206-978f088.md`](../../sim/cmrr-mc/records/20261010-035206-978f088.md)
  (the mismatch-inclusive CMRR statistic, new);
  [`sim/cmrr/records/20261009-105631-30ec86d.md`](../../sim/cmrr/records/20261009-105631-30ec86d.md)
  (systematic CMRR, 45 points);
  [`sim/psrr/records/20261009-105929-30ec86d.md`](../../sim/psrr/records/20261009-105929-30ec86d.md)
  (PSRR+ and PSRR−, systematic, 45 points);
  [`sim/offset-mc/records/20261009-072205-96bf3cc.md`](../../sim/offset-mc/records/20261009-072205-96bf3cc.md)
  (same corners, N and seed);
  [0002](0002-performance-target-bounds.md) (the `3σ, mismatch MC N≥300 + process corners`
  convention and the 60 dB DC-gain bound this record reuses);
  [0003](0003-target-spec-ratification.md) (residuals (e3), (e4), which this
  record answers with proposals); the `sg13g2-opamp` twin's
  [0004-cmrr-mismatch-inclusive-ratification.md](https://github.com/2AMLogic/sg13g2-opamp/blob/main/spec/decision-records/0004-cmrr-mismatch-inclusive-ratification.md)
  (the linear-domain 3σ definition is mirrored; no number is copied)

## Context

`spec/target-spec.md` §2 carries CMRR and PSRR as `[TBD]` with measurements but
no bound. Two facts made a bound that simply read off the existing records
misleading:

1. **The CMRR measurement was systematic only.** Every device in the
   committed 45-point CMRR grid is perfectly matched, so its worst DC plateau
   (95.42 dB, SS / 125 °C / 2.97 V) is an optimistic upper bound. The row said
   so: mismatch-limited CMRR was not covered.
2. **PSRR+ is not flat in frequency.** The committed PSRR record shows PSRR+
   falling from 98.45 dB at DC to 0.86 dB at the differential unity-gain
   frequency (SF / −40 °C / 2.97 V). A DC-plateau-only bound would misstate
   the row.

This record therefore (a) uses a new mismatch Monte Carlo CMRR record as the
CMRR statistic, and (b) states the frequency basis explicitly for both rows.

## Evidence (committed)

### CMRR under mismatch — record `20261010-035206-978f088`

One `klt sim` `monte_carlo` request (`vary: "mismatch"`, seed 45) over the five
MOS process corners, N = 300 each = 1 500 samples, at 27 °C / 3.30 V / VCM =
VDD/2: the same axes and seed as the offset Monte Carlo (1 476 of 1 500 samples
reproduce that record's offset to 10 µV). It ran on the Spot batch fleet; job
`klt-sim-5e847e468844` (grid), with three control jobs
(`klt-sim-762d072820d2`, `klt-sim-69775f83a8b5`, `klt-sim-1cfab2fdea61`).

Both CMRR excitations of one sample run in one ngspice process, so Ad and Acm
always come from the same sampled devices (per-sample parity is checked: the
two DC operating points agree to 1 µV while the offset varies by mV).

| Figure (worst of the five corners) | systematic, same corners (27 °C / 3.30 V) | **linear 3σ** | dB 3σ (`mean − 3σ`) | empirical sample min | p5 |
|---|---|---|---|---|---|
| DC plateau (0.1–1 Hz) | 99.46 – 101.36 dB | **82.71 dB** (typical) | 68.84 dB | 79.28 dB (SF) | 84.94 dB |
| 1 kHz | 99.46 – 101.36 dB | **82.65 dB** (typical) | 68.84 dB | 79.28 dB (SF) | 84.94 dB |
| 10 kHz | 99.46 – 101.36 dB | **82.65 dB** (typical) | 69.12 dB | 79.28 dB (SF) | 84.94 dB |
| 100 kHz | 99.34 – 101.24 dB | 82.66 dB (typical) | 71.49 dB | 79.28 dB (SF) | 84.93 dB |
| 1 MHz | 93.26 – 96.02 dB | 82.19 dB (SS) | 78.57 dB | 78.84 dB (SF) | 84.13 dB |
| at f_u | 61.82 – 68.03 dB | 61.02 dB (SS) | 60.98 dB | 61.05 dB (SS) | 61.36 dB |

- **Mismatch costs 16.5 – 18.3 dB at DC** (systematic minus linear 3σ, per
  corner) and nothing material at f_u: at f_u, CMRR ≈ 1/|Acm| and the
  mismatch spread is only 0.3 – 0.5 dB.
- **The statistic is the linear-domain 3σ.** The per-sample dB image is
  skewed (skew +1.1 to +1.5 at DC, because a sample can cancel Acm and send
  CMRR up with no risk). Mean − 3σ in dB then lies 10 dB below the worst
  observed sample of 1 500 and is not a meaningful lower tail. The linear
  figure, `mean(Ad dB) − 20 log10(mean|Acm| + 3σ|Acm|)`, sits between the
  empirical minimum and p5 at every corner, as a +3σ point should at N = 300.
  The dB figure is reported beside it and is the more pessimistic of the two.
  N = 300 cannot resolve a 0.135 % tail, so neither is a sample statistic.
- **Controls passed.** Switch-off (mismatch off) gives zero spread and
  reproduces the committed systematic record to ≤ 0.0001 dB at DC through
  1 MHz (≤ 0.004 dB at f_u). The mirror-imbalance control (XM3 W −10 %) raises
  mean |Acm| by +0.546 V/V (5.4 standard errors of the difference; the criterion is 3) and lowers the
  linear 3σ from 82.71 to 80.19 dB. The process-only run is recorded as
  measured (it is not a σ = 0 control with this PDK deck,
  2AMLogic/klayout-tools#2937).
- **Not covered by this record**: temperature and supply under mismatch (see
  Decision, "Statistical basis"), resistor and capacitor mismatch (the PDK
  deck defines none for resistors; the MIM cap has none), passives other than
  typical.

### CMRR over PVT — systematic record `20261009-105631-30ec86d`

Worst over the 45 PVT points: DC 95.42 dB (SS / 125 °C / 2.97 V), 10 kHz
95.42 dB, 100 kHz 95.29 dB, 1 MHz 89.19 dB, f_u 60.99 dB (SS / −40 °C /
2.97 V).

### PSRR over PVT — record `20261009-105929-30ec86d` (systematic)

| Figure | PSRR+ worst (dB) | at | PSRR− worst (dB) | at |
|---|---|---|---|---|
| DC plateau | 98.45 | FS / −40 °C / 2.97 V | 101.01 | SS / −40 °C / 2.97 V |
| 1 kHz | 81.24 | SS / 125 °C / 2.97 V | 101.01 | SS / −40 °C / 2.97 V |
| 10 kHz | 61.31 | SS / 125 °C / 2.97 V | 100.89 | SS / −40 °C / 2.97 V |
| 100 kHz | 41.31 | SS / 125 °C / 2.97 V | 93.48 | SS / 125 °C / 2.97 V |
| 1 MHz | 21.31 | SS / 125 °C / 2.97 V | 72.60 | SS / 125 °C / 2.97 V |
| at f_u | **0.86** | SF / −40 °C / 2.97 V | 37.14 | SS / −40 °C / 2.97 V |

PSRR+ follows the open-loop gain (the VDD-to-output gain is roughly flat, about
−3 to −7 dB), so it reaches about 0 dB at f_u. PSRR− changes sign across the
grid at four points, so its low-frequency values there (up to 156 dB) are
cancellations, not margin; the worst-case figure is the one that counts.

## Consumer needs

Stated honestly, the consumers impose **no amplifier-level CMRR row and no
amplifier-level PSRR row**.

- **`gf180-ldo` (error-amp slot).** Its architecture survey sets a
  **system** PSRR requirement of **> 50 dB at 1 kHz (stretch > 60 dB)** and
  describes it as loop-gain-dominated at 1 kHz, inside the expected loop
  unity-gain bandwidth (tens to low hundreds of kHz). It states no CMRR
  requirement and no PSRR requirement above 1 kHz. Its error-amp inputs sit at
  VREF, so common-mode excursions at the inputs are small.
- **`gf180-bandgap` (unspecified amplifier slot).** Its PSRR row is at the
  output-reference node, DC – 1 kHz, a system quantity. It states nothing at the
  amplifier level.

So no bound below is traced to a requirement the consumers state for this
block. Each is derived from the nearest quantified requirement, and says so.
Where a number has no consumer behind it, that is flagged for the operator.

## Decision (proposed)

Replace the `[TBD]` target in the CMRR and PSRR rows of `spec/target-spec.md`
§2 with the following proposed bounds.

### CMRR

| Item | Proposal |
|---|---|
| **Bound** | **CMRR ≥ 60 dB** at the DC plateau (0.1 – 1 Hz) **and** at 10 kHz |
| **Statistical basis** | **Mismatch-inclusive linear-domain 3σ** per process corner: `mean(Ad dB) − 20 log10(mean\|Acm\| + 3σ\|Acm\|)`, mismatch Monte Carlo N = 300, five process corners, seed 45. Taken at the worst of the five corners. |
| **Temperature and supply** | **A stated choice, not a silent one.** The mismatch population is at 27 °C / 3.30 V only. The row's claim over PVT is the systematic 45-point grid combined with the nominal mismatch penalty: worst systematic CMRR 95.42 dB minus the worst per-corner mismatch penalty 18.31 dB gives an **estimate of 77.1 dB** at the DC plateau and about the same at 10 kHz. That is an estimate, not a measurement. A mismatch Monte Carlo over the T/VDD grid is a follow-up (below), and the driver already supports it (`--grid full`). |
| **Reported, not bounded** | 1 kHz (equals DC to 0.01 dB), 100 kHz, 1 MHz, and the CMRR at f_u |
| **Binding corner** | measured mismatch-inclusive: typical (82.71 dB DC, 82.65 dB at 10 kHz, 27 °C / 3.30 V); systematic over PVT: SS / 125 °C / 2.97 V |

**Why 60 dB.** No consumer states a CMRR. The requirement used is internal and
dimensionless: an op-amp that is used as a follower (the configuration of the
DC offset, ICMR and CMRR benches) turns a common-mode change ΔVCM into an
output error ΔVCM/CMRR, while finite gain gives an error of about Vin/A. Requiring
**CMRR ≥ A_DC,min = 60 dB**, the ratified DC-gain bound ([0002](0002-performance-target-bounds.md)),
keeps the common-mode error no larger than the gain-error budget the row set
already tolerates. The bound is set from that rule, not from where the data
falls; the data then clears it by 22.7 dB at the worst corner and 17 dB against
the PVT estimate, which is a comparable cushion to the DC-gain row (93.79 dB
measured against 60 dB).

**Why DC and 10 kHz.** The measured CMRR is flat from DC to 10 kHz (the
mismatch-induced error is an offset-like, DC-accurate term), so one in-band
frequency beside DC is the most that can differ. 10 kHz is a decade above the
only frequency any consumer names (1 kHz) and inside, at the low end of, the LDO
loop's expected unity-gain band (tens of kHz and up). 1 kHz is reported only because it
equals DC.

**Why not 100 kHz, 1 MHz or f_u.** No consumer exercises them: the LDO loop
bandwidth is tens to low hundreds of kHz, and f_u is 10 – 16 MHz. At f_u the
CMRR is ≈ 1/|Acm|, about 61 – 68 dB, fixed by the topology and almost
independent of mismatch (spread 0.3 – 0.5 dB), so a bound there would restate
the gain–bandwidth design rather than constrain matching. The values stay in
the measured-status cell so the row does not hide them.

### PSRR (PSRR+ and PSRR−)

| Item | Proposal |
|---|---|
| **Bound** | **PSRR+ ≥ 60 dB and PSRR− ≥ 60 dB** at the DC plateau and at 1 kHz; **PSRR+ ≥ 50 dB and PSRR− ≥ 50 dB** at 10 kHz |
| **Definition** | Input-referred, `20 log10 \|Ad / Asupply\|` with one rail driven at a time (the committed bench) |
| **Statistical basis** | Deterministic corner-worst-case over the 45 PVT points. **Systematic only; mismatch is not covered, and a PSRR mismatch Monte Carlo is out of scope here.** Supply-path rejection is mostly topology-limited, but the PSRR− low-frequency cancellation sign flips (above) are exactly the kind of term mismatch perturbs, so the PSRR− DC figure is the least certain. A follow-up is filed (below). |
| **Reported, not bounded** | 100 kHz, 1 MHz and the PSRR at f_u |
| **Binding corner** | PSRR+ : FS / −40 °C / 2.97 V (DC), SS / 125 °C / 2.97 V (1 kHz, 10 kHz); PSRR− : SS / −40 °C / 2.97 V |

**Why these numbers.** The `gf180-ldo` survey's system figure is > 50 dB at
1 kHz with a 60 dB stretch. An amplifier-level floor at 1 kHz equal to the
stretch figure (60 dB) means the error amplifier does not by itself consume the
stretch margin. The same 60 dB applies at DC. At 10 kHz the 50 dB baseline is
used one decade up, where PSRR+ has already lost 20 dB. The consumer states
nothing at 10 kHz; this is the one PSRR number with no consumer behind it, and
the operator may drop it without affecting the others.

**Margins** (measured worst against bound): PSRR+ 38.5 dB at DC, 21.2 dB at
1 kHz, 11.3 dB at 10 kHz; PSRR− 41.0 dB at DC and 1 kHz, 50.9 dB at 10 kHz.

**Why no bound at f_u, and why that is disclosed.** PSRR+ is 0.86 dB at f_u:
supply noise near 10 – 16 MHz passes to the input unattenuated. No consumer has
a requirement there (the LDO states PSRR at 1 kHz only), the figure is a
structural Miller property rather than a design margin, and 100 kHz (41.31 dB),
1 MHz (21.31 dB) and f_u values are all in the row's measured-status cell. The
proposal does not claim PSRR above 10 kHz.

### Rows changed

`spec/target-spec.md` §2: the **CMRR** row and the **PSRR** row only. Each
gets its proposed bound tagged `[DR-6] — proposed, not ratified`, its
statistical basis, its frequency basis and its binding corner. Their
status cells cite the records above. The `[DR-n]` tag legend gains the `[DR-6]`
exception, worded as `[DR-5]` is. No other row, no stretch value and no 5 V
content changes.

## Alternatives considered

- **Bound the systematic figure (about 95 dB).** Rejected. It is an upper
  bound on matched devices; the mismatch-inclusive figure is 17 dB lower at DC
  and the issue's premise is that the row must not rest on the optimistic one.
- **Use `mean − 3σ` in dB as the statistic.** Rejected as the bound's
  definition, kept as a reported figure. It is skewed and sits below every
  observed sample (68.84 dB against a worst sample of 79.28 dB in 1 500). The
  linear figure is the one the twin ratified and the one that tracks the
  empirical tail here.
- **A DC-only bound.** Rejected for PSRR (PSRR+ loses 98 dB by f_u) and
  rejected for CMRR (a single frequency would hide the CMRR roll-off beyond
  100 kHz).
- **A PSRR bound at f_u.** Rejected. It would have to be about 0 dB, which says
  nothing, and no consumer asks for it.
- **A tighter CMRR bound near the data (for example 75 dB).** Rejected. It would
  be set by where the data passes, not by a requirement, and the T/V mismatch
  population does not exist yet to support the margin.
- **Run the full 45-point mismatch grid before proposing.** Not done here. It
  is 13 500 fleet units and needs no change to the proposal's rule; the nominal
  penalty plus the systematic grid already gives a 17 dB cushion over the
  bound. It is requested as a follow-up instead.

## Limitations, kept distinct from the result

1. **Schematic level, passives at typical.** RZ and CC spread is not swept, and
   resistor and capacitor mismatch are absent from this PDK deck.
2. **Mismatch at one temperature and supply.** The PVT figure is an estimate
   (systematic worst minus nominal penalty), not a measurement.
3. **PSRR is systematic only.**
4. **N = 300 per corner.** The 3σ point is an extrapolation of the sample
   mean and σ, not an observed tail.
5. **Ideal bias.** `Ibias` is an ideal 10 µA and the input common mode is
   VDD/2, as in the committed benches. The consumer's actual input common mode
   (1.20 V, [0005](0005-input-common-mode-range-row.md)) was not simulated for
   CMRR.
6. **No consumer states a CMRR requirement.** The 60 dB rule is derived, and
   the operator may substitute another.

## Follow-ups

- **CMRR mismatch Monte Carlo over the T/VDD grid** (`run_cmrr_mc.py --grid full`,
  45 points × N = 300) to replace the PVT estimate with a measurement.
- **PSRR mismatch Monte Carlo**, with the PSRR− cancellation points as the first
  target.

Filed as #98 (CMRR T/VDD mismatch grid) and #99 (PSRR mismatch); both reference #61.
