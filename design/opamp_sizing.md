# `opamp_two_stage` — gm/ID sizing derivation

This is the citation trail behind every device size in
[`opamp_two_stage.sch`](opamp_two_stage.sch), per `CLAUDE.md`'s **"gm/ID
first, committed to `sim/` before sizing"** rule. Every device number below
is read out of one measured record —
[`sim/gm-id-characterization/records/20260909-052956-79c6a45.md`](../sim/gm-id-characterization/records/20260909-052956-79c6a45.md)
(`typical` corner, 27 °C) — or out of that same run's raw corner data. None
of it is guessed, and none of it is carried over from a sibling repo's
circuit.

**What this document is not.** It is a *sizing derivation*, not a
verification record. The only simulation this design is committed against is
the nominal DC operating-point smoke check in
[`check_dc_op.py`](check_dc_op.py) (one corner, no AC analysis). Every
small-signal figure quoted below — DC gain, GBW, phase margin, slew rate —
is a **prediction** from gm/ID data and textbook two-stage relations, not a
measured result: `spec/target-spec.md` §2's rows stay `[TBD]` until a
PVT-cornered testbench exists under `sim/` (gap-to-T1 tracker
[#7](https://github.com/2AMLogic/gf180-opamp/issues/7) item 5), which is out
of scope for issue #17.

## 1. What was fixed before sizing started

| Constraint | Value | Source |
|---|---|---|
| Topology | NMOS input pair, PMOS common-source output device, NMOS current-sink load, **no cascode**, Miller compensation between stages | [DR-0001](../spec/decision-records/0001-topology-and-cl.md) |
| Load capacitance `CL` | 2 pF | DR-0001 / `spec/target-spec.md` §1 `[DR-1]` |
| Supply | 3.3 V ±10 % | `spec/target-spec.md` §1 |
| Phase margin | ≥ 60° | `spec/target-spec.md` §2 `[P]` — the only §2 row that is not `[TBD]`, so it is the one performance row this sizing pass can legitimately design *to* |
| Device flavor | `nfet_03v3` / `pfet_03v3` only | `CLAUDE.md` (3.3 V primary; no flavor mixing without a decision record) |

Everything else in §2 is `[TBD]`, so GBW and quiescent current are *chosen*
here as a self-consistent canary-block design point, not met against a
target. The choices: **GBW ≈ 15 MHz into `CL` = 2 pF at a total quiescent
current of 80 µA (264 µW)**.

## 2. Where the numbers come from

Two kinds of number are read out of the gm/ID study:

1. **gm/ID, gm/gds and fT vs. Vov** — directly from the record's own tables
   (`typical`, 27 °C). Quoted rows are reproduced verbatim below so they can
   be grepped against the record.
2. **Current density ID/W vs. Vov** — *not* tabulated in the record, but
   present in the same run's raw corner data, which the record links as
   `corners/20260909-052956-79c6a45/`. The study swept W = 10 µm devices, so
   ID/W at a given Vov follows directly. Reproduce the three densities used
   here with:

   ```bash
   python3 - <<'PY'
   import numpy as np
   d = np.loadtxt('sim/gm-id-characterization/corners/'
                  '20260909-052956-79c6a45/typical_27c_nosupply.dat')
   LENS = [0.28, 0.5, 1.0, 2.0, 4.0]; PAR = ['gm', 'gds', 'id', 'cgg', 'vth']
   def col(dev, L, p):           # wrdata writes (x, y) pairs in request order
       i = (0 if dev == 'n' else 1) * 25 + LENS.index(L) * 5 + PAR.index(p)
       return d[:, 2 * i + 1]
   vs = d[:, 0]
   for dev, L, vov in [('n', 1.0, 0.10), ('p', 1.0, 0.20), ('n', 2.0, 0.20)]:
       idv, vth = col(dev, L, 'id'), col(dev, L, 'vth')
       m = np.isfinite(idv) & (idv > 0); x = (vs - vth)[m]; o = np.argsort(x)
       print(f'{dev}fet L={L}um Vov={vov*1e3:.0f}mV  '
             f'ID/W = {np.interp(vov, x[o], idv[m][o]) / 10e-6:.4f} uA/um')
   PY
   ```

   ```
   nfet L=1.0um Vov=100mV  ID/W = 1.3840 uA/um
   pfet L=1.0um Vov=200mV  ID/W = 0.7686 uA/um
   nfet L=2.0um Vov=200mV  ID/W = 1.6888 uA/um
   ```

   The same snippet reproduces the record's published gm/ID table exactly
   (e.g. `nfet_03v3` L = 1 µm at Vov = 200 mV → 8.04), which is the check
   that the column indexing above matches the record's own.

## 3. Channel-length choices

### Input pair (M1/M2): `nfet_03v3`, **L = 1 µm**

DR-0001 decided the *polarity* (NMOS) from the fT gap at L = 0.28 µm; it did
not fix a length — "no device widths, lengths, bias currents, or mirror
ratios are chosen here" (DR-0001, *Out of scope*). The length is chosen here
from the record's fT and gm/gds columns against this design's actual GBW:

| L (µm) | fT @ Vov=100 mV | fT / GBW | gm/gds @ Vov=100 mV | gate area at ID = 5 µA |
|---|---|---|---|---|
| 0.28 | 17.08 GHz | ≈ 1100× | 30.0 | 0.22 µm² |
| 0.5 | 4.72 GHz | ≈ 310× | 176.7 | 0.78 µm² |
| **1** | **1.09 GHz** | **≈ 70×** | **392.5** | **3.6 µm²** |
| 2 | 0.23 GHz | ≈ 15× | 584.6 | 16 µm² |

At a 15 MHz GBW the input pair's own fT is nowhere near the limiting
resource: L = 1 µm still leaves ≈ 70× margin over GBW, while buying **13×**
the intrinsic gain and **16×** the gate area (i.e. 4× better Vth-mismatch
σ) of the 0.28 µm row DR-0001 used for its illustrative estimate. L = 2 µm
would buy another 1.5× gain for 4.4× the area and only ≈ 15× fT margin, so
1 µm is the knee.

**This does not contradict DR-0001, it strengthens it**, on both of the
axes DR-0001 argued:

- *Polarity*: the NMOS-vs-PMOS fT gap DR-0001 rejected a PMOS input pair on
  (≈ 4.4× at L = 0.28 µm, Vov = 200 mV) is *larger* at the chosen length —
  `nfet_03v3` 1.09 GHz vs `pfet_03v3` 0.18 GHz at L = 1 µm, Vov = 100 mV,
  ≈ 6.1×.
- *Gain*: DR-0001's non-cascoded DC-gain estimate used the input pair's
  `gm/gds = 29.0` (L = 0.28 µm). At L = 1 µm that term is 392.5, so the
  estimate can only improve — see §6, where it lands at 95.8 dB, essentially
  on top of DR-0001's own 95.8 dB figure once the mirror and sink loading
  DR-0001 explicitly excluded are included.

### Output gain device (M6) and mirror load (M3/M4): `pfet_03v3`, **L = 1 µm**

DR-0001's *Consequences* section names a candidate `L ≈ 1–4 µm` class for
the output PMOS and asks that a sizing pass which goes short re-examine
whether the non-cascoded gain estimate still closes. L = 1 µm is the short
end of that named class, so §6 does exactly that re-examination — and it
closes, at 95.8 dB.

The cost of going longer is width, because current density collapses with
length: at Vov = 200 mV, `pfet_03v3` needs 0.7686 µA/µm at L = 1 µm but only
0.1603 µA/µm at L = 4 µm, so the 60 µA output device would be W = 78 µm at
L = 1 µm versus W = 374 µm at L = 4 µm — **19× the gate area** (78 µm² vs
1500 µm²) to buy `gm/gds` 573.3 → 2122.7. With the gain budget already
closing at 95.8 dB and `spec/target-spec.md`'s area row `[TBD]`, spending
that area is unjustified for a canary block.

**M3/M4 must share M6's length.** In a two-stage Miller amplifier the
output-stage current is set by M6's source-gate voltage, which equals
M4's at balance. Equal L is what makes that a current-mirror ratio that
tracks over corners rather than two differently-scaling devices. Hence
M3/M4 = M6 = `pfet_03v3` at L = 1 µm.

### Bias mirror family (MB1, M5, M7): `nfet_03v3`, **L = 2 µm**

The tail and the output sink are current sources: what matters is output
resistance and matching, not speed. `nfet_03v3` L = 2 µm at Vov = 200 mV
gives `gm/gds = 564.0` (vs 372.2 at L = 1 µm) at 1.6888 µA/µm, which keeps
the sink's gds from dominating the output node (§6) at a modest width.
All three share L = 2 µm so the 1 : 1 : 6 mirror ratio tracks.

## 4. Bias point and compensation

Design point, in the order the choices were actually made:

1. **`IBIAS` = 10 µA**, supplied externally on the `ibias` pin (there is no
   on-chip reference in this block; a bandgap/`Iptat` source is a different
   block's job). MB1 turns it into the mirror gate voltage.
2. **Output-stage current from the phase-margin row.** With a nulling
   resistor removing the right-half-plane zero, the classic condition for
   PM ≈ 60° is that the non-dominant pole sits at ≥ 2.2× GBW:

   ```
   p2 = gm6 / (2*pi*CL) >= 2.2 * GBW
   ```

   At GBW = 15 MHz and CL = 2 pF that needs `gm6 ≥ 415 µS`. Taking the
   `pfet_03v3` L = 1 µm, Vov = 200 mV row (`gm/ID = 8.51`):
   `I6 = 415 µA/8.51 ≈ 49 µA`. Rounded up to the next clean mirror ratio,
   **I6 = I7 = 60 µA = 6 × IBIAS**, giving `gm6 = 60 µA × 8.51 = 510 µS`
   and `p2 = 40.6 MHz`.
3. **Tail current from GBW.** `Itail = 1 × IBIAS = 10 µA`, so
   `ID1 = ID2 = 5 µA`. At the `nfet_03v3` L = 1 µm, Vov = 100 mV row
   (`gm/ID = 12.07`): `gm1 = 5 µA × 12.07 = 60.3 µS`.

   Vov = 100 mV (moderate inversion) rather than 50 mV or 200 mV: the
   record's own gm/ID column has nearly flattened by 50 mV (14.95 vs 12.07
   at 100 mV — only 24 % more gm for half the overdrive), and a device that
   close to subthreshold has a gm that is strongly Vth- and
   temperature-dependent, which this pass has no PVT bench to bound yet; at
   the other end, 200 mV drops gm/ID to 8.04, a third less gm for the same
   5 µA. 100 mV also keeps `gm1 > gm3` (12.07 vs 8.51), the condition that
   keeps the mirror load's Vth mismatch from dominating input-referred
   offset.
4. **`CC` from GBW**: `CC = gm1/(2*pi*GBW) = 60.3 µS / (2π·15 MHz) = 0.64 pF`.
   Drawn as a 17.4 µm × 17.4 µm `cap_mim_2f0_m4m5_noshield` — 302.8 µm² at
   2 fF/µm² ≈ 0.61 pF nominal; the PDK model, measured in ngspice at
   `mimcap_typical`/27 °C, gives **0.619 pF** including fringe.
5. **`RZ` = 1/gm6** = 1/510 µS = 1.96 kΩ, drawn as `ppolyf_u_1k` at
   W = 2 µm / L = 4 µm (2 squares of a 1 kΩ/□ unsilicided p-poly). The PDK
   model, measured in ngspice at `res_typical`/27 °C, gives **2.11 kΩ** —
   3 % above 1/gm6, which places the zero in the *left* half-plane rather
   than leaving a marginal RHP one.

   `RZ` is a fixed poly resistor and therefore does not track `1/gm6` over
   PVT; that is a known limitation to be quantified by the AC/PVT testbench
   (tracker #7 item 5), not here. A triode-PMOS `RZ` that does track is the
   standard remedy if the PVT sweep shows it is needed.

### Systematic-offset (balance) condition

For the output stage to sit at its design current with the input pair
balanced, the two mirror ratios must agree:

```
(W6/L6)/(W4/L4) = 2 * (W7/L7)/(W5/L5)
      72/6 = 12  =  2 * (36/2)/(6/2) = 2 * 6 = 12     OK
```

The simulated closed-loop output offset is **−0.00 mV** (§7), which is this
condition holding.

## 5. Device table

Width follows from the chosen current and the ID/W row: `W = ID / (ID/W)`,
rounded to a clean finger count. All NMOS mirror devices are 3 µm/finger and
all PMOS devices are 3 µm/finger, so each mirror ratio is also a finger-count
ratio — the layout-friendly form.

| Device | Model | Role | L (µm) | target Vov | ID | gm/ID row | ID/W row | W = ID/(ID/W) | **as drawn** |
|---|---|---|---|---|---|---|---|---|---|
| M1, M2 | `nfet_03v3` | input pair | 1 | 100 mV | 5 µA | 12.07 | 1.3840 µA/µm | 3.61 µm | **W = 3.6 µm, nf = 2** |
| M3, M4 | `pfet_03v3` | mirror load | 1 | 200 mV | 5 µA | 8.51 | 0.7686 µA/µm | 6.51 µm | **W = 6 µm, nf = 2** |
| M5 | `nfet_03v3` | tail source | 2 | 200 mV | 10 µA | 8.22 | 1.6888 µA/µm | 5.92 µm | **W = 6 µm, nf = 2** |
| M6 | `pfet_03v3` | stage-2 CS gain device | 1 | 200 mV | 60 µA | 8.51 | 0.7686 µA/µm | 78.1 µm | **W = 72 µm, nf = 24** |
| M7 | `nfet_03v3` | stage-2 current sink | 2 | 200 mV | 60 µA | 8.22 | 1.6888 µA/µm | 35.5 µm | **W = 36 µm, nf = 12** |
| MB1 | `nfet_03v3` | bias mirror diode | 2 | 200 mV | 10 µA | 8.22 | 1.6888 µA/µm | 5.92 µm | **W = 6 µm, nf = 2** |
| CC | `cap_mim_2f0_m4m5_noshield` | Miller capacitor | — | — | — | — | — | 0.64 pF → 302.8 µm² | **17.4 µm × 17.4 µm (0.619 pF)** |
| RZ | `ppolyf_u_1k` | nulling resistor | — | — | — | — | — | 1.96 kΩ → 2 squares | **W = 2 µm, L = 4 µm (2.11 kΩ)** |

Rounding notes: M3/M4 go 6.51 → 6 µm and M6 78.1 → 72 µm *together*, keeping
the 1 : 12 ratio exact (the balance condition above matters more than either
absolute width); the pair then sits at 0.833 µA/µm, i.e. Vov ≈ 209 mV rather
than 200 mV. M5/MB1 go 5.92 → 6 µm and M7 35.5 → 36 µm, keeping 1 : 1 : 6
exact at Vov ≈ 199 mV.

## 6. Predicted performance (gm/ID + textbook relations — not measured)

Using only the record's `gm/gds` rows at the chosen lengths and overdrives:

| Term | Value | From |
|---|---|---|
| `gm1` | 60.3 µS | 5 µA × 12.07 (`nfet_03v3`, L = 1 µm, Vov = 100 mV) |
| `gds1` | 0.154 µS | `gm1`/392.5 (same row's `gm/gds`) |
| `gds4` | 0.074 µS | `gm4`/573.3 (`pfet_03v3`, L = 1 µm, Vov = 200 mV) |
| stage-1 gain | 265 (48.5 dB) | `gm1/(gds1+gds4)` |
| `gm6` | 510 µS | 60 µA × 8.51 (`pfet_03v3`, L = 1 µm, Vov = 200 mV) |
| `gds6` | 0.890 µS | `gm6`/573.3 |
| `gds7` | 0.874 µS | `gm7`/564.0 (`nfet_03v3`, L = 2 µm, Vov = 200 mV) |
| stage-2 gain | 289 (49.2 dB) | `gm6/(gds6+gds7)` |
| **open-loop DC gain** | **76 600 (97.7 dB)** | product |
| **GBW** | **15.5 MHz** | `gm1/(2π·CC)`, CC = 0.619 pF |
| non-dominant pole | 40.6 MHz (2.6× GBW) | `gm6/(2π·CL)`, CL = 2 pF — clears the ≥ 2.2× the 60° PM row needs |
| **slew rate** | **16.2 V/µs** | `Itail/CC` |
| `gm6/gm1` | 8.5 | the ratio `RZ` exists to make acceptable (≥ 10 would be needed without it) |
| **quiescent power** | **264 µW** | 80 µA × 3.3 V (10 µA bias + 10 µA tail + 60 µA output) |

**Re-examining DR-0001's gain estimate, as its *Consequences* section asks.**
DR-0001 put the non-cascoded two-stage product at ≈ 95.8 dB from two
`gm/gds` rows alone, and flagged that mirror/tail loading and mismatch would
erode it. Including the mirror load's `gds4` and the sink's `gds7` — the
loading DR-0001 excluded — and using this pass's chosen lengths, the
prediction is 97.7 dB, and the simulated operating point (§7) gives 95.8 dB.
**The non-cascoded DC-gain budget closes.** No superseding decision record
is needed, and no cascode is added.

## 7. As-simulated nominal operating point

From `python3 design/check_dc_op.py` (`typical` + `res_typical` +
`mimcap_typical`, 27 °C, VDD = 3.3 V, unity-gain buffer at VCM = 1.65 V,
IBIAS = 10 µA, CL = 2 pF). Regenerable, not append-only evidence — a
*record* for these numbers would need the full PVT bench that is out of
scope here.

```
node voltages
  v(vout  ) =   1.6500 V
  v(ibias ) =   0.8742 V
  v(n1    ) =   2.3042 V
  v(n2    ) =   2.3036 V
  v(tail  ) =   0.6433 V
  v(nz    ) =   1.6500 V

device operating points
  dev   role                        W/L   ID(uA)   |Vgs|   |Vth|   |Vov|   |Vds|  |Vdsat|   gm(uS)  gm/gds  region
  M1    input pair (vinn)         3.6/1    4.977  1.0067  0.8974  0.1093  1.6609   0.1484    58.64   404.6  sat
  M2    input pair (vinp)         3.6/1    4.977  1.0067  0.8974  0.1093  1.6603   0.1484    58.64   404.6  sat
  M3    mirror load, diode          6/1    4.977  0.9958  0.7828  0.2130  0.9958   0.1930    40.08   360.3  sat
  M4    mirror load, output         6/1    4.977  0.9958  0.7828  0.2130  0.9964   0.1930    40.09   360.5  sat
  M5    tail current source         6/2    9.954  0.8742  0.6679  0.2062  0.6433   0.1974    80.51   371.0  sat
  M6    stage-2 CS gain dev        72/1   60.750  0.9964  0.7828  0.2137  1.6500   0.1934   487.85   527.9  sat
  M7    stage-2 current sink       36/2   60.750  0.8742  0.6679  0.2062  1.6500   0.1974   489.04   549.5  sat
  MB1   bias mirror diode           6/2   10.000  0.8742  0.6679  0.2062  0.8742   0.1974    80.80   442.3  sat

derived from this operating point (not an AC measurement)
  stage-1 gain gm2/(gds2+gds4) =    229.0 (47.2 dB)
  stage-2 gain gm6/(gds6+gds7) =    268.9 (48.6 dB)
  two-stage product            =    61569 (95.8 dB)
  gm6/gm1 (RHP-zero ratio)     =     8.32
  total supply current 80.70 uA -> 266.3 uW
  closed-loop output offset -0.00 mV
```

How the simulation compares to the gm/ID prediction:

| Quantity | gm/ID prediction | simulated OP | note |
|---|---|---|---|
| `ID(M1)` | 5.00 µA | 4.977 µA | mirror ratio holds |
| `Vov(M1)` | 100 mV | 109 mV | **body effect**: M1/M2 sit at Vsb = 0.64 V (source on the tail node) while the record swept Vsb = 0, so the same current density lands at slightly more overdrive |
| `gm1` | 60.3 µS | 58.64 µS | −2.8 %, the same body-effect shift |
| `ID(M6)` | 60.0 µA | 60.75 µA | +1.25 %, the 6 µm/72 µm rounding |
| `gm6` | 510 µS | 487.9 µS | −4.3 %; M6 is at Vov = 214 mV after rounding, and gm/ID falls with Vov |
| open-loop DC gain | 97.7 dB | 95.8 dB | the mirror load M4 sits at \|Vds\| = 1.00 V, not the record's 1.65 V, so its `gm/gds` is 360 rather than 573 |
| `gm6/gm1` | 8.5 | 8.3 | still < 10, i.e. `RZ` is load-bearing |
| quiescent power | 264 µW | 266 µW | |

Every device is in saturation with ≥ 50 mV of `|Vds| − |Vdsat|` margin (M5,
the tightest, has 446 mV), the mirror delivers its design currents, and the
closed-loop output sits on the input common mode.

## 8. What is still open

- **No AC, transient, noise, offset, CMRR/PSRR or PVT data exists.** GBW,
  phase margin, slew rate, gain and power above are predictions. Filling
  `spec/target-spec.md` §2 needs the PVT-cornered testbench under `sim/`
  that tracker #7 item 5 tracks.
- **`RZ` does not track `1/gm6` over PVT** (see §4.5). Quantify before
  ratifying a phase-margin number.
- **Input-referred offset has no statistical basis yet.** The input pair's
  3.6 µm² gate area and the `gm1 > gm3` choice are the two design decisions
  that bear on it; the `3σ, mismatch MC N≥300` basis `spec/target-spec.md`
  §2 names has not been run.
- **The record is a Vsb = 0, Vds = 1.65 V characterization.** Two devices
  here sit away from that bias — the input pair at Vsb = 0.64 V (body
  effect) and the mirror load at |Vds| = 1.0 V (lower output resistance) —
  which is where the prediction-vs-simulation gaps in §7 come from. A future
  gm/ID study that adds a Vsb axis would tighten the input-pair sizing; the
  present pass handles it by checking the drawn design in simulation.
- **Input common-mode range and output swing** are untested. The nominal
  point (VCM = 1.65 V) leaves M5 with 446 mV of `Vds` headroom, but the
  low-VCM edge where M5 leaves saturation has not been swept.
