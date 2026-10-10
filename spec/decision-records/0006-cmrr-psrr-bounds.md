# 0006: CMRR and PSRR rows — propose mismatch-inclusive CMRR and frequency-based PSRR+/PSRR− bounds

- **Status**: **proposed — not ratified.** This record proposes numeric bounds
  for the two rows [0003](0003-target-spec-ratification.md) left open as
  residuals (e3) CMRR and (e4) PSRR, and states the frequency and statistical
  basis of each. It relaxes no ratified row, bound or tag. Under the
  ratification-via-PR standing policy
  ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)) that
  [0003](0003-target-spec-ratification.md) and
  [0005](0005-input-common-mode-range-row.md) apply, the ratification act is
  the operator's approval of the PR that carries this record. Until then
  nothing here binds, and the `spec/target-spec.md` CMRR and PSRR rows read
  "proposed". As with 0003 and 0005, this status line will still read
  `proposed` after the merge; treat the merge commit of the carrying PR as the
  ratification act, and nothing earlier.
- **Date**: 2026-10-10
- **Decided by**: Builder agent, issue #61 (proposal and evidence). The
  operator's approval of the carrying PR is the ratification act.
- **Related**: #61 (this record's issue); [0003](0003-target-spec-ratification.md)
  residuals (e3), (e4); [0002](0002-performance-target-bounds.md) (the `3σ`
  offset basis this record mirrors);
  [`sim/cmrr-mc/records/20261010-035625-191d9e9.md`](../../sim/cmrr-mc/records/20261010-035625-191d9e9.md)
  (mismatch-inclusive CMRR evidence);
  [`sim/cmrr/records/20261009-105631-30ec86d.md`](../../sim/cmrr/records/20261009-105631-30ec86d.md)
  (systematic CMRR, 45 points);
  [`sim/psrr/records/20261009-105929-30ec86d.md`](../../sim/psrr/records/20261009-105929-30ec86d.md)
  (PSRR+ / PSRR−, 45 points); [`sim/cmrr-mc/README.md`](../../sim/cmrr-mc/README.md).

## Context

Both rows read `[TBD]`. The committed CMRR record is systematic only: every
device is perfectly matched, so its 95.42 dB worst DC plateau is an optimistic
upper bound. The committed PSRR record shows PSRR+ falling to 0.86 dB at the
differential unity-gain frequency f_u (SF / −40 °C / 2.97 V), so a bound on
the DC plateau alone would misstate how the amplifier rejects supply noise.
Issue #61 asked for bounds with the frequency and statistical basis stated,
and for the CMRR bound to rest on the mismatch-inclusive number.

## Evidence

### Mismatch-inclusive CMRR (new, append-only record `20261010-035625-191d9e9`)

Five MOS corners × N = 300 per-instance mismatch samples (`vary: "mismatch"`,
seed 45, 27 °C, 3.30 V, VCM = VDD/2; the population of
[`sim/offset-mc/`](../../sim/offset-mc/README.md)), run through four `klt sim`
requests on the Spot batch fleet. The grid job is `klt-sim-943a3df1319e`
(1 500 units, 1 204 s). The Ad and Acm of one sample come from **one ngspice
process and one parsed circuit** (both excitations in one deck), so they
belong to the same devices; each sample proves it by a 1 µV agreement of the
DC output after the two sweeps. The step (a) capability probes are under
[`sim/cmrr-mc/probes/`](../../sim/cmrr-mc/probes/).

Validity checks, all passed: exactly 300 distinct valid samples at each of the
five points; dm/cm parity on every sample; the `sw_stat_mismatch=0` control
reproduces the committed systematic record to 0.01 dB at DC and the spots;
offset σ 4.3–5.0 mV (mismatch acts); mirror-imbalance negative control (XM3
−10 %) raises mean |Acm| by 0.546 V/V (5.4 standard errors) and lowers both
3σ statistics (82.71 → 80.19 dB linear, 68.84 → 65.77 dB in dB).

Worst over the five corners (dB):

| frequency | linear 3σ (worst corner) | dB 3σ (worst) | sample minimum | p5 |
|---|---|---|---|---|
| DC plateau | **82.71** (typical) | 68.84 | 79.28 (SF) | 84.94 |
| 1 kHz | 82.65 | 68.84 | 79.28 | 84.94 |
| 10 kHz | **82.65** (typical) | 69.12 | 79.28 | 84.94 |
| 100 kHz | 82.66 | 71.49 | 79.28 | 84.93 |
| 1 MHz | 82.19 (SS) | 78.57 | 78.84 | 84.13 |
| f_u | 61.02 (SS) | 60.98 | 61.05 | 61.36 |

Where "linear 3σ" is `mean(Ad dB) − 20 log10(mean|Acm| + 3σ|Acm|)`, the CMRR
of the +3σ common-mode gain (the `sg13g2-opamp` twin's definition). The dB
image is skewed (+1.1 to +1.5 at DC) because a mismatch draw can cancel the
systematic Acm and push CMRR up by tens of dB; that carries no risk, so the
linear-domain figure is the one to bound. The empirical sample minimum
(79.28 dB over 1 500 samples) is the normality cross-check: it sits 3.4 dB
below the linear 3σ figure, i.e. the 3σ figure is not obviously optimistic
about the sampled tail, but N = 300 per corner cannot resolve a 0.135 % tail.
The mismatch lowers the DC plateau by about 18 dB against the matched typical
value (100.64 dB), which confirms the systematic record's warning.

At f_u the CMRR is unchanged by mismatch (σ 0.3–0.5 dB; 61.02 dB linear 3σ at
SS against 60.99 dB systematic worst over the 45 points).

### Systematic CMRR over PVT (`20261009-105631-30ec86d`)

Worst DC plateau 95.42 dB (SS / 125 °C / 2.97 V), 100.64 dB at typical /
27 °C / 3.30 V: temperature and supply move the matched figure by up to
**5.2 dB**. Worst at f_u 60.99 dB; 100 kHz 95.29; 1 MHz 89.19.

### PSRR (`20261009-105929-30ec86d`, 45 points, systematic)

| figure | PSRR+ worst (dB) | PSRR− worst (dB) |
|---|---|---|
| DC plateau | 98.45 (FS / −40 °C / 2.97 V) | 101.01 (SS / −40 °C / 2.97 V) |
| 1 kHz | 81.24 | 101.01 |
| 10 kHz | 61.31 | 100.89 |
| 100 kHz | 41.31 | 93.48 |
| 1 MHz | 21.31 | 72.60 |
| f_u | **0.86** (SF / −40 °C / 2.97 V) | 37.14 |

PSRR+ falls at 20 dB/decade from about 1 kHz (the Miller-compensated second
stage couples VDD to the output) down to unity at f_u. Avss changes sign at 4
grid points, so PSRR− at low frequency is a cancellation at those points; the
bound below uses the worst-case figure, not those.

## Consumer needs

Neither consumer states an amplifier-level CMRR or PSRR requirement
([`spec/target-spec.md`](../target-spec.md) "Consumers"). `gf180-bandgap`'s
PSRR row is a system-level quantity at its output-reference node over
DC–1 kHz, `gf180-ldo`'s loop rejection is a regulator quantity, and
`gf180-ldo` estimates its loop UGBW in the tens to low hundreds of kHz.
No number can therefore be traced to a consumer. The bounds below are
**evidence-derived design-to targets**, and the record says so rather than
inventing a requirement. What the consumers do fix is the band that matters:
DC to about 1 kHz for the bandgap, and up to a few hundred kHz for the LDO
loop. That is why the proposal bounds DC, 1 kHz and 10 kHz for PSRR+ and
reports (does not bound) the higher frequencies. The operator may raise,
lower or drop any bound; each is separately removable.

## Decision (proposed)

### CMRR

- **Bound: CMRR ≥ 77 dB at the DC plateau (0.1–1 Hz) and at 10 kHz.**
- **Statistical basis: linear 3σ over mismatch** (`mean(Ad dB) − 20 log10(mean|Acm| + 3σ|Acm|)`),
  mismatch MC N = 300 × five MOS corners, mirroring the ratified offset basis
  of [0002](0002-performance-target-bounds.md) ("3σ, mismatch MC N ≥ 300 + process corners").
  Worst corner: 82.71 dB at DC, 82.65 dB at 10 kHz.
- **Temperature and supply are not sampled under mismatch.** This is a stated
  choice, not a silent one: the 77 dB bound is the mismatch figure (82.65 dB)
  less the full 5.2 dB systematic DC spread across −40…125 °C and
  2.97…3.63 V (rounded down). That subtraction assumes the two effects add in
  dB, which is pessimistic if mismatch dominates Acm (it does: the mismatch-sampled mean |Acm| of 1.44 V/V at typical is about
  7.8 dB above the matched 0.585 V/V implied by the 100.64 dB systematic figure), and not proved either way. The operator can instead
  request the 13 500-sample `--grid full` run (`run_cmrr_mc.py --grid full`);
  until then 77 dB is a conservative proposal, not a measured PVT worst case.
- **Reported, no bound**: 1 kHz, 100 kHz and 1 MHz (82.66 / 82.19 dB, flat with
  the plateau up to 1 MHz) and f_u (61.02 dB, systematic-limited and
  mismatch-insensitive). The dB 3σ (68.84 dB) and the sample minimum
  (79.28 dB) are reported only; the dB image is the skewed one.
- The 77 dB bound is **20 dB below** the committed systematic 95.42 dB. That
  gap is the finding of this record, and the reason the systematic number must
  never be quoted as the CMRR.

### PSRR

PSRR is bounded on the existing systematic 45-point grid; a PSRR mismatch
Monte Carlo is **out of scope** (supply-path rejection here is set by the
topology, and the matched figure is the right order of magnitude). A
follow-up issue can open it if a consumer asks. The PSRR rows are
systematic-only, and the row text says so.

| bound | proposed | worst measured | margin |
|---|---|---|---|
| PSRR+ DC plateau | ≥ 95 dB | 98.45 | 3.45 |
| PSRR+ 1 kHz | ≥ 78 dB | 81.24 | 3.24 |
| PSRR+ 10 kHz | ≥ 58 dB | 61.31 | 3.31 |
| PSRR− DC plateau | ≥ 98 dB | 101.01 | 3.01 |
| PSRR− 10 kHz | ≥ 97 dB | 100.89 | 3.89 |
| PSRR− 100 kHz | ≥ 90 dB | 93.48 | 3.48 |

- **Reported, no bound**: PSRR+ at 100 kHz (41.31), 1 MHz (21.31) and at f_u
  (**0.86 dB**); PSRR− at 1 MHz (72.60) and at f_u (37.14). **No bound is
  proposed for PSRR+ at f_u**, deliberately: no consumer requirement supports
  one and the amplifier does not reject supply noise there. The row text
  must show 0.86 dB so the DC plateau is never read alone.
- The margins (about 3 dB) are the evidence margin to the worst of 45 grid
  points; they are not a statistical allowance for passives or for the
  excluded bias generator (the ideal 10 µA bias has no supply rejection of its
  own in this bench).
- **Statistical basis: deterministic corner-worst-case**, as the rows already
  state.

### Rows changed in `spec/target-spec.md`

Only the CMRR and PSRR rows, and only in the target and statistical-basis
cells and the status text, marked **proposed** (the 0005 precedent). The
reference list gains this record. No other row changes. The existing records
are untouched.

## Alternatives considered

- **Bound the systematic 95.42 dB.** Rejected: it is the matched-device upper
  bound; the issue asks for the mismatch-inclusive number.
- **dB 3σ (mean − 3σ) as the statistic.** Reported, not bounded: it is 10 dB
  below the sample minimum at DC because the dB image is skewed; it would
  bound the wrong tail.
- **Bound PSRR+ at f_u.** Rejected for lack of a consumer requirement.
- **Sub-77 dB or a 5 V row.** The 5 V rail stays closed; nothing here opens it.

## Limitations

1. CMRR mismatch is MOS only; the deck has no resistor or MIM mismatch
   (RZ/CC at typical).
2. Schematic level, ideal bias current, no bias-generator rejection.
3. Temperature and supply unsampled under mismatch (above).
4. Fleet runner is klt 0.5.0 against client 0.7.0: the switch-off control ties
   the fleet to the committed systematic record to 0.01 dB, which bounds the
   skew.
5. klt's `family_mismatch` reports `active: null` for an `.include`d DUT
   (2AMLogic/klayout-tools#2928); mismatch activity is instead confirmed by
   the record's offset σ and its mirror-imbalance and parity checks.
