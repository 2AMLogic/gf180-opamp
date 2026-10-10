# layout/pilot — GF180 device layout pilot (issue #77)

**Scope: isolated device test cells only.** Seven cells, one per distinct
device size in `design/netlist/opamp_two_stage.spice`, each generated alone,
labelled, extracted and checked. This is a tool/PDK-path capability check.
It is **not** a floorplan, **not** block layout, and **not** full-block DRC or
LVS signoff, and the bounding boxes below are **not** a ratified or measured
block area. No area budget is set or implied here. Final compensation
dimensions (`XCC`, `XRZ`) await #42; the pilot uses the netlist's current
values purely as probe sizes.

## Regenerate (one command, from a clean checkout)

```
layout/pilot/regen.sh
```

It refuses to run unless these identities match, then rebuilds every file
under `gds/ ref/ extracted/ evidence/` and `results.json` (about 2 minutes,
single process, no ngspice).

| Item | Pinned identity |
|---|---|
| klayout-tools | `klt 0.7.0+g5e5b55992a7f` (a post-tag build, so `uvx --from klayout-tools==X` cannot reproduce it; `klt` must already be on PATH at this build) |
| PDK | `gf180mcuD`, `open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b` (`~/.volare/gf180mcuD`) |
| Foundry runset variant | `--variant=D` (5LM, metal_top 11K, MIM option B) |
| Foundry DRC/LVS engine | system `klayout` binary, `KLayout 0.28.16` (what the PDK's `run_drc.py`/`run_lvs.py` shell out to) |
| klayout python (labelling) | `klayout==0.30.12` via a throwaway `uv run --with` env |
| runset CLI dep | `docopt==0.6.2` (same throwaway env) |

Nothing is installed on the host. The PDK's metal stack was not previously
pinned in this repo; `D` is chosen because the schematic's
`cap_mim_2f0_m4m5_noshield` is the M4/M5 MIM of the 5-metal option. A stack
decision record, if wanted, belongs in `spec/`.

## What each cell is

`tools/pilot.py` reads each instance card from the schematic, calls
`klt gen`, then adds net-name text labels at the generator's port locations
(a `klt gen` cell carries no labels, so `klt extract` has no pins without
this step). `ad/as/pd/ps` source/drain geometry parameters on the schematic
cards are not carried into the references; only W, L, nf, m (MOS) and the
passive dimensions are compared.

| Cell | Schematic | Model | Generator and params | Pin mapping (schematic net to cell pin) |
|---|---|---|---|---|
| `pilot_nfet_mb1_m5` | XMB1, XM5 | `nfet_03v3` L=2u W=6u nf=2 | `mos_array` w_um=3 (=W/nf), l=2, fingers=2 | D, G, S, B from the card's d g s b |
| `pilot_nfet_m1_m2` | XM1, XM2 | `nfet_03v3` L=1u W=3.6u nf=2 | `mos_array` w_um=1.8, l=1, fingers=2 | same |
| `pilot_nfet_m7` | XM7 | `nfet_03v3` L=2u W=36u nf=12 | `mos_array` w_um=3, l=2, fingers=12 | same |
| `pilot_pfet_m3_m4` | XM3, XM4 | `pfet_03v3` L=1u W=6u nf=2 | `mos_array` w_um=3, l=1, fingers=2 | same |
| `pilot_pfet_m6` | XM6 | `pfet_03v3` L=1u W=72u nf=24 | `mos_array` w_um=3, l=1, fingers=24 | same |
| `pilot_mim_cc` | XCC | `cap_mim_2f0_m4m5_noshield` 17.4u x 17.4u | `cap_array` plate 17.4 x 17.4, num=1 | BOT = schematic `n2` (card pin 1), TOP = `nz` (pin 2) |
| `pilot_rpoly_rz` | XRZ | `ppolyf_u_1k` r_width=2u r_length=4u | `res_array` flavor 1k, length 4, width 2, num=1, dummy=0 | A = `vout`, B = `nz`, bulk = `vss` (substrate) |

Model and terminal facts verified here:

- **MOS**: `klt gen` `w_um` is the per-finger width and fingers are strapped in
  parallel; the extractor reports `fingers` devices of `W/nf` and `klt lvs`
  `combine_devices` folds them to the schematic's total `W` (the PDK model's
  `W` is total width with `nf` separate). Every cell has a gate contact and a
  tap/guard ring labelled `B`; the nfet ring ties to the p-substrate and the pfet
  ring to the n-well. Models extract as `nfet_03v3` / `pfet_03v3`; the 06V0/dualgate
  flavours are not drawn (3.3 V devices only).
- **MIM**: both klt's extractor and the foundry LVS runset emit the device
  terminals in the order (BOT, TOP) for the labelled plates, extracted as
  `cap_mim_2f0_m4m5_noshield` with `c_width`/`c_length` equal to the drawn
  plate. The PDK model is symmetric in its two terminals, so this does not change
  simulation, but the netlist puts `n2` (the high-impedance first-stage output)
  on the bottom plate, the one with substrate parasitics that the PDK model
  does not include. Plate orientation is a placement-time decision to take with
  #42.
- **Poly resistor**: geometry alone cannot distinguish `ppolyf_u_1k/2k/3k`; the
  flavour is selected at extraction by `--deck-option poly_res=1k` (matching the
  schematic) and recorded in each LVS request. The substrate is the third
  terminal.

## Results (actual runs; `results.json` and `evidence/` hold the raw output)

| Cell | Footprint um (not an area claim) | DRC, PDK runset | DRC, klt curated | LVS, klt | LVS, PDK runset |
|---|---|---|---|---|---|
| `pilot_nfet_mb1_m5` | 8.04 x 7.92 | **FAIL** (contact_OFFGRID) | clean | match | match |
| `pilot_nfet_m1_m2` | 6.04 x 6.72 | **FAIL** (contact_OFFGRID) | clean | match | match |
| `pilot_nfet_m7` | 33.84 x 7.92 | **FAIL** (contact_OFFGRID) | clean | match | match |
| `pilot_pfet_m3_m4` | 6.04 x 7.92 | **FAIL** (contact_OFFGRID) | clean | match | match |
| `pilot_pfet_m6` | 40.8 x 7.92 | **FAIL** (contact_OFFGRID) | clean | match | match |
| `pilot_mim_cc` | 19.52 x 20.48 | **PASS** | clean | match | not run (see below) |
| `pilot_rpoly_rz` | 4.84 x 2.0 | **FAIL** (SB.4, SB.10) | clean | match | match |

- "DRC, PDK runset" is the GF180MCU `run_drc.py` main runset, flat, default
  table set. Antenna and density are not enabled, which is another reason
  nothing here is signoff.
- klt LVS compares device parameters: W and L for MOS, C and A for the MIM
  (the PDK model carries no separate width/length parameter), L and W for the
  resistor. Negative controls (a deliberately wrong reference dimension) were
  run by hand while building this and each produced a `device.property`
  mismatch, so the match status is sensitive to the parameters compared.
  The negative controls are not part of `regen.sh`.
- The `klt lvs` warnings counted in `evidence/*.lvs.klt.json` are
  `topology` entries saying an unused device class has no counterpart; there
  are zero error-severity findings.
- **MIM, PDK-runset LVS: not run.** The runset's comparer did not accept any
  capacitor reference-card form tried (it read the reference's area and
  perimeter as 0 and then reported the nets unmatched). This is unresolved; it is
  recorded as inconclusive, not as pass or fail. klt's own LVS is the only LVS
  verdict for this cell.

### Failures, causes, and filed tool gaps

- `contact_OFFGRID` on all five MOS cells: the guard-ring tap contacts
  from `klt gen mos_array add_guard_ring` are off the 0.005 um grid. The same
  device without the ring has none, but then the body is untied, so the ring
  stays in the pilot. Filed as klayout-tools #2999.
- `SB.4` and `SB.10` on the poly resistor: salicide-block spacing to contact
  and poly extension beyond the block. Filed as klayout-tools #3000.
- klt's curated `gf180mcu` DRC deck says `clean` on both classes of failure
  because it has no rule for them. Filed as klayout-tools #3001.

No cell was edited by hand to make it pass, and no spec value was changed.

## Next step for full placement (not done here)

1. Wait for #42 to fix the final `XCC`/`XRZ` values, then regenerate those two
   cells; rerun `regen.sh`.
2. Resolve or work around #2999 and #3000 (or hand-fix the contact grid and
   SB geometry in a documented post step) until the PDK-runset DRC is clean on
   every pilot cell, before any of them is composed.
3. Matching groups to place as units: the input pair `XM1/XM2` (common-centroid
   `diff_pair` or `mos_array` topology with dummies), the PMOS mirror
   `XM3/XM4`, and the bias mirror `XMB1/XM5/XM7` (ratio 1 : 1 : 6 by W, same L=2u).
   The pilot cells above are single devices and do not establish matching.
4. Compose with `klt gen-compose` (or draw), add power rails and signal routing,
   then run PDK-runset DRC (with antenna and density) and LVS on the whole
   block against `design/netlist/opamp_two_stage.spice`, plus extraction
   for post-layout sim. Only that run may be cited as block-level layout
   evidence, and area is ratified separately by the usual spec process.

## Files

- `cells.json`: cell table (schematic instances to cell names)
- `regen.sh`, `tools/pilot.py`: the recipe
- `gds/`: generated, labelled layouts
- `ref/`: schematic-derived reference netlists (`*.klt.spice`, `*.foundry.spice`)
- `extracted/`: extracted netlists (klt and PDK runset)
- `evidence/`: raw klt JSON (`gen`, `drc`, `extract`, `lvs` and the LVS requests)
- `results.json`: per-cell summary, including the PDK-runset DRC rule counts
