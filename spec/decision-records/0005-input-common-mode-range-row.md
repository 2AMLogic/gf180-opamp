# 0005: Input common-mode range row — propose 1.20 V ≤ VCM ≤ 2.70 V (follower-biased, all 45 PVT points) from measured evidence

- **Status**: **proposed — not ratified.** This record adds one row to
  `spec/target-spec.md` §2 and proposes its bound. It changes no existing
  row, bound, tag or condition. Under the ratification-via-PR standing
  policy ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)),
  which [0003](0003-target-spec-ratification.md) applied to this table,
  the ratification act for this row is the operator's approval of the PR
  that carries this record. In this repo's pipeline that is Judge review
  plus Champion/operator merge. Until that act happens, nothing here binds:
  the row is a proposal and the comparisons it feeds are provisional.
  This line will still read `proposed` after the merge, because no
  automation rewrites a merged record. This is the same known wart that
  [0003](0003-target-spec-ratification.md)'s status line describes. A later
  reader should treat the merge commit of the carrying PR as the
  ratification act, and nothing earlier.
- **Date**: 2026-10-09
- **Decided by**: Builder agent, issue #60 (proposal and evidence). The
  operator's approval of the carrying PR is the ratification act.
- **Related**: #60 (this record's issue); #42 (phase margin, which this
  record does not touch);
  [`sim/input-common-mode/records/20261009-222613-871d1a6.md`](../../sim/input-common-mode/records/20261009-222613-871d1a6.md)
  (the evidence);
  [`sim/input-common-mode/README.md`](../../sim/input-common-mode/README.md)
  (the bench, its criteria and validity rules);
  [`sim/cmrr/`](../../sim/cmrr/README.md) (the servo bench and the Ad/Acm
  solve, both reused);
  [0003](0003-target-spec-ratification.md) (the ratification act and residual
  register; this record leaves both unchanged);
  [`spec/target-spec.md`](../target-spec.md) "Consumers" (the `gf180-ldo`
  and `gf180-bandgap` input-range rows this record re-grades);
  the `sg13g2-opamp` twin's
  [0006-icmr-row-resolution.md](https://github.com/2AMLogic/sg13g2-opamp/blob/main/spec/decision-records/0006-icmr-row-resolution.md)
  (also a draft ICMR proposal; its shape is mirrored here, but no number is
  copied)

## Context

`spec/target-spec.md` has no input common-mode range (ICMR) row. Neither
[0003](0003-target-spec-ratification.md) ratified one nor did its residual
register hold one open. Both consumer tables therefore graded "Input range"
**unknown**. Before #60, every AC, CMRR and PSRR bench in `sim/` biased the
inputs at VCM = VDD/2. No committed evidence said whether the amplifier
works with its inputs at `gf180-ldo`'s VREF = 1.20 V.

#60 added a bench and a 45-point measurement, and asked for a decision record
that opens the row. The issue also set two rules for this record: argue the
range from the measured evidence and the consumers' needs, and leave every
existing ratified bound and the 5 V scope unchanged.

## Evidence (committed; reproducible from retained data)

Source: [record `20261009-222613-871d1a6`](../../sim/input-common-mode/records/20261009-222613-871d1a6.md).
Running `python3 sim/input-common-mode/run_input_common_mode.py --recompute 20261009-222613-871d1a6`
re-derives every number below from the committed archives.

**What was measured.** The committed sized schematic (DUT export unchanged)
was placed on the CMRR bench's buffered DC servo, with `CL = 2 pF` [DR-1] and
an ideal `Ibias = 10 µA`. The grid is the full ratified PVT grid: process
{typical, ff, ss, fs, sf} × T {−40, 27, 125 °C} × VDD {2.97, 3.30, 3.63 V}.
Each of the 45 points carries its own VCM scan from 0 to VDD at ≤ 50 mV
spacing, and every scan includes 1.20 V and VDD/2 explicitly. Every
pass/fail transition was refined to a 5 mV bracket. In total there are
6 165 (point, VCM) samples × 2 AC excitations, run as four `klt sim` batch
requests on the Spot fleet:

- scan: `klt-sim-e40e856b6264` and `klt-sim-20c7b31c53a5`;
- refinement: `klt-sim-366191c054aa` and `klt-sim-d123d35f3dfc`.

At each sample:

- **Gain.** Ad is solved jointly with Acm from the actual input phasors of
  the two excitations, as the CMRR driver does. The gain criterion is the
  minimum of the open-loop **DC-plateau gain** |Ad| over 0.1–1 Hz, and it
  must be ≥ 60 dB. This is not the closed-loop follower gain.
- **Saturation.** `abs(vds) − abs(vdsat)` must be ≥ 0 for the input pair,
  tail, mirror and output stage, with a 1 mV numerical tolerance. The record
  repeats the whole analysis at 0 mV.
- **Output tracking and currents.** The follower output must stay within
  0.10 V of VCM, and the pair and tail currents must be non-zero.

**Validity.** A sample is invalid, and so never counts as passing, when its
data is missing or non-finite, when the excitation is wrong, when its paired
operating points disagree by > 1 µV, or when no Ad plateau exists.
Contiguous passing intervals are never bridged across a non-passing sample.

| Quantity | Value |
|---|---|
| Samples | 6 165: 3 450 pass, 2 245 fail, 470 invalid. All invalid samples lie at VCM ≤ 0.75 V, in the starved-tail region, and none falls inside any passing interval. |
| Passing intervals | exactly one contiguous interval at every one of the 45 points |
| **45-point intersection** | **[1.185, 2.705] V**, one component, which contains 1.20 V |
| Low endpoint | 1.185 V, set by SS / −40 °C / 2.97 V. The limit is the tail XM5's headroom (VCM ≥ VGS,pair + Vdsat,tail). The adjacent sample at 1.180 V fails with XM5 −2.8 mV, so the bracket is 5 mV. At 1.185 V itself XM5 is +0.6 mV. |
| High endpoint | 2.705 V, set by FS / 125 °C / 2.97 V. The limit is the output stage XM6's headroom: in the follower, vout = VCM, so XM6 runs out of VSD as VCM nears VDD. Plateau gain there is 60.93 dB. The adjacent sample at 2.710 V fails at 59.73 dB, so the bracket is 5 mV. |
| **1.20 V explicit sample** | **passes at 45/45** combinations: 0 fail, 0 invalid |
| Worst plateau gain at 1.20 V | 94.22 dB (SS / 125 °C / 2.97 V) |
| Smallest device margin at 1.20 V | XM5 **+11.1 mV** (SS / −40 °C / 2.97 V). Across all nine SS points it is +11.1 to +12.9 mV, and at SF it is +36.6 to +41.2 mV. |
| Tolerance sensitivity | With 0 mV instead of the 1 mV tolerance, the intersection and the 1.20 V verdict are unchanged. Fifteen per-point endpoints sit inside the tolerance band; none of them sets an intersection endpoint. |
| Controls | Midrail samples reproduce the committed CMRR record's Ad plateau at 45/45 points (max deviation 0.0000 dB). Servo-isolation change ≤ 0.00001 dB (limit 0.01 dB). An inadequate servo (tau = 1 ms) and unequal CM drive are both rejected. A local nominal unit reproduces the grid's nominal sample. |

An independent re-derivation from the committed archives gave the same
1.20 V verdict and the same two endpoint brackets. It used a separate
minimal parser and solve, not the driver's code.

**Limitations, kept distinct from the result:**

1. **Follower-biased.** The upper edge is an output-stage limit specific to
   the unity-gain follower: the output sits at VCM. This is not an input
   range for an arbitrary output voltage, and the input pair's own upper
   limit was not separately measured.
2. **Schematic, matched devices, passives at typical.** Mismatch, systematic
   offset and the RZ/CC passive spread are not swept. The bench-validity
   tolerances (1 mV saturation, 0.10 V output) are not performance bounds.
3. **Ideal bias current.** Ibias is an ideal 10 µA. A bias-current
   tolerance moves XM5's Vdsat and therefore the low edge. That was not
   measured.
4. **Sampled coverage.** The data proves the sampled points pass, not
   continuous behaviour between samples. Endpoints are quoted on the passing
   side of 5 mV brackets.

## Consumer needs

- **`gf180-ldo` (error-amp slot).** Both inputs sit at VREF, which is
  `gf180-bandgap`'s 1.20 V output; the divider tap is regulated to VREF.
  The consumer names VREF = 1.20 V and states **no amplifier-level
  tolerance**. The required interval is therefore the point
  **[1.20 V, 1.20 V]**. No ± band is invented here.
- **`gf180-bandgap` (unspecified amplifier slot).** It states no
  amplifier-level input-range requirement.

## Decision (proposed)

Add one row to `spec/target-spec.md` §2:

| Row | Proposed bound | Basis |
|---|---|---|
| **Input common-mode range** (follower-biased: unity-gain-follower DC operating point, output within 0.10 V of VCM; all 45 PVT points at VDD 2.97–3.63 V; `CL = 2 pF` [DR-1]; `Ibias = 10 µA`) | **1.20 V ≤ VCM ≤ 2.70 V** [DR-5, proposed] | **Deterministic corner-worst-case.** Over this interval every sampled VCM gives a DC-plateau gain ≥ 60 dB ([DR-2]'s gain bound, reused rather than restated) and keeps the input pair, tail, mirror and output stage saturated at all 45 PVT points. |

How each edge was chosen:

- **Lower edge, 1.20 V.** This is the consumer's stated input. It lies on
  the passing side of the measured 1.185 V endpoint and is itself an
  explicit sample that passes at 45/45. The edge is not set at 1.185 V
  because a design-to bound placed on the last passing sample would leave
  the consumer's own point 15 mV inside a 5 mV bracket. Anchoring at 1.20 V
  states exactly what the evidence supports and what the consumer needs.
- **Upper edge, 2.70 V.** This is the measured 2.705 V endpoint rounded
  *inward* (down) to 10 mV. It is covered at all 45 points, because every
  sample between 1.20 V and 2.70 V passes at every point. Nothing is
  rounded outward.

The proposal ratifies no new performance claim beyond this row. The 60 dB
gain criterion is the ratified [DR-2] gain bound, used only as the
in-range test. The row's **measured status** is that it is covered at 45/45
PVT points (record above). Its **binding corners** are SS / −40 °C / 2.97 V
for the low edge (tail headroom) and FS / 125 °C / 2.97 V for the high edge
(output-stage headroom in the follower).

**Flagged, not hidden: the low edge is thin.** At SS and 1.20 V the tail
device has only +11 to +13 mV of saturation margin, and the measured edge
is only 15 mV below 1.20 V. The unmodelled effects above (mismatch and
offset, a bias-current tolerance) could consume that margin. This record
does not claim they will not. If the operator wants guard-band at 1.20 V,
the response is a design change to tail headroom, which is outside #60's
scope. It is not a looser row.

### Consumer comparisons this row enables (non-normative, `spec/target-spec.md` "Consumers")

- **`gf180-ldo`, input range.** The verdict is **meets for the nominal
  1.20 V input** (required interval [1.20, 1.20] V ⊂ the proposed row; the
  explicit sample passes 45/45). It stays **unknown for any tolerance around
  VREF**, because the consumer specifies none. That comparison stays open
  until a tolerance is stated, and the measured 15 mV low-side headroom at
  SS is the number it would be checked against.
- **`gf180-bandgap`, input range.** The verdict stays **unknown**, because
  there is no amplifier-level requirement to compare against. Only the basis
  text changes: it now cites this row instead of "no row exists".

## Alternatives considered

- **Set the row to the raw measured intersection, [1.185, 2.705] V.**
  Rejected. Endpoints taken from the last passing samples of 5 mV brackets
  claim resolution the data does not have. The low end adds nothing the
  consumer needs, and the high end is not rounded inward.
- **Add an invented ± tolerance around 1.20 V (for example ±50 mV) and
  grade the LDO against it.** Rejected. The consumer states no tolerance,
  and at SS a −50 mV band would fail (the low edge is 1.185 V). Inventing a
  requirement in order to grade it is exactly what #60's revision forbids.
- **State the upper edge relative to VDD (for example VDD − 0.27 V).**
  Considered and not proposed. The per-VDD upper edges are 2.705–2.795 V at
  2.97 V, 3.045–3.120 V at 3.30 V and 3.375–3.450 V at 3.63 V, so a
  VDD-relative edge would be wider at the higher rails. #60 asked for the
  absolute intersection across all 45 points, and an absolute bound is the
  conservative statement. A VDD-relative form can be proposed later if a
  consumer needs inputs near the upper rail.
- **Leave the row open in a residual register (no bound).** Rejected,
  because the evidence exists and the LDO's comparison depends on a row
  existing.
- **Re-size the tail for more headroom at 1.20 V.** Out of scope. #60
  excludes design resizing, and any change would invalidate every committed
  record.

## Consequences

- `spec/target-spec.md` gains the ICMR row tagged `[DR-5]` as **proposed**.
  Existing rows, bounds, tags, §1 conditions and the 5 V row
  (named-not-opened `[P]`) are unchanged.
- The `gf180-ldo` "Input range" verdict becomes **meets (nominal 1.20 V)
  / unknown (tolerance unspecified)**. The `gf180-bandgap` "Input range"
  verdict stays **unknown**, and only its basis changes.
- The measured status depends on the DUT. A design change, for example from
  #42, requires a new ICMR record before this row's measured status can be
  cited again. The low-edge margin is the number most likely to move.
- [0003](0003-target-spec-ratification.md)'s residual register is not
  edited. This row is added by this record, not by 0003.

## Out of scope

- Mismatch-inclusive or offset-inclusive ICMR, input range at an arbitrary
  output voltage, and a separate input-pair upper limit.
- Bias-current tolerance and RZ/CC passive corners.
- The 5 V stretch rail.
- Any change to existing bounds (gain, GBW, PM, slew, swing, power) or to
  the open rows (noise, offset, CMRR, PSRR, area).
- Adding ICMR to the aggregate characterization report
  (`sim/report/characterization_report.py`). That is a separate follow-up.
