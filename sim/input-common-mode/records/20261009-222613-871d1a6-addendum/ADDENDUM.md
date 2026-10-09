# Addendum to ICMR record `20261009-222613-871d1a6`

- **Date**: 2026-10-09; issue #60, PR #67 (review follow-up)
- **Record**: [`../20261009-222613-871d1a6.md`](../20261009-222613-871d1a6.md)

Records under `sim/` are append-only evidence, so the record file is left
byte-for-byte as committed. This addendum adds disclosures and corrects
presentation only. It changes no number, verdict, endpoint, sample count or
provenance in the record. All of it can be checked against the committed
archives with
`python3 sim/input-common-mode/run_input_common_mode.py --recompute 20261009-222613-871d1a6`.
That command re-derives 6165 samples, the intersection [1.185, 2.705] V and
the 1.20 V verdict (meets, 45/45), and confirms that `samples.csv` agrees.

## 1. Retained rawfiles are trimmed (disclosure)

The record's evidence line says `corners/20261009-222613-871d1a6/data/*.tar.gz`
holds the "retained `.raw` and `.log` per sample and excitation". It does not
say that the `.raw` files are **trimmed**:

- Each committed `.raw` keeps only the vectors the extraction reads:
  `frequency`, `v(vinp)`, `v(vinn)` and `v(vout)`. Their value lines are
  copied verbatim from the fleet's rawfile, and the header's variable count
  is rewritten to match. The other vectors of the original rawfile are not
  committed.
- Every ngspice `.log` is kept **whole**, including the operating-point print
  (vout, vinp, vinn and every DUT MOSFET's vds, vdsat and id). The saturation
  margins and currents come from that print.
- The trimmed set holds everything `--recompute` and the record's numbers
  use: about 17 MB of `data/` archives, against an estimated ~135 MB for full
  rawfiles. The trimming routine is unit-tested to keep the vectors it retains bit-identical
  (`test_trim_raw_keeps_used_vectors_bit_identical`). The same disclosure
  appears in [`../../README.md`](../../README.md) under "Evidence layout" and
  in [DR-0005](../../../../spec/decision-records/0005-input-common-mode-range-row.md)'s
  evidence section.

## 2. "Validity failures" lines: number masking garbled the text (presentation)

The record's "Validity failures (preserved)" section groups invalid samples
by reason, with every number masked. The driver's mask also swallowed signs
and range dashes, so the printed group text came out garbled, for example
`no Ad plateau over ## Hz` (`0.1-1` became `##`). The **counts are
correct**. The driver now masks numbers as `N`, leaves signs and dashes in
place, and prints one verbatim example per group. Re-derived from the
committed archives, the record's two groups read:

- 469 x no Ad plateau over N-N Hz: varies by N dB (> N dB) (e.g. ff / -40 C / 2.97 V / VCM 0.000 V: no Ad plateau over 0.1-1 Hz: varies by 7.397 dB (> 0.1 dB))
- 1 x Ad plateau phase N deg is not ~N (wrong polarity) (e.g. sf / -40 C / 3.63 V / VCM 0.050 V: Ad plateau phase 180.0 deg is not ~0 (wrong polarity))

## 3. `--keep-work` cache identity (provenance note)

The record's execution block says the four reports were reused from the
`--keep-work` cache, matched as "byte-identical requests (netlist path
aside)". At the time, the cache key did not include the **content** of the
materialised netlist. This record is not affected: between the original
submissions and the record commit, the DUT export, the ICMR testbench, the
CMRR bench it reuses and the shared harness are unchanged (`git diff d67274b
871d1a6 -- design/ sim/input-common-mode/testbench/ sim/cmrr/ sim/harness.py`
is empty). From now on the driver adds the
sha256 of the materialised netlist and every file it includes to the cache
key, so a changed DUT or bench always re-submits.
