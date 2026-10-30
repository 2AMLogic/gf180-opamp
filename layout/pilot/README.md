# layout/pilot — GF180 device layout pilot (issue #77)

**Scope: isolated device test cells only.** Seven cells, one per distinct
device size in `design/netlist/opamp_two_stage.spice`, each generated alone,
labelled, extracted and checked. This is a tool/PDK-path capability check.
It is **not** a floorplan, **not** block layout, and **not** full-block DRC or
LVS signoff, and the bounding boxes below are **not** a ratified or measured
block area. No area budget is set or implied here. `XCC`/`XRZ` are sized from
the netlist after the #42 retune (16.8u MIM, 22u resistor); the pilot uses them
purely as probe sizes, and `regen.sh` re-derives them from the schematic.

## Regenerate (one command, from a clean checkout)

```
layout/pilot/regen.sh
```

It refuses to run unless these identities match, then rebuilds every file
under `gds/ ref/ extracted/ evidence/` and `results.json` (about 2 minutes,
single process, no ngspice).

| Item | Pinned identity |
|---|---|
| klayout-tools | `klt 0.8.0` (release; run from a throwaway env first on PATH, e.g. `uv venv V && uv pip install --python V/bin/python klayout-tools==0.8.0 && PATH=V/bin:$PATH layout/pilot/regen.sh`) |
| PDK | `gf180mcuD`, `open_pdks f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7` (`~/.ciel/gf180mcuD`) |
| PDK variant | `gf180mcuD` (5LM, metal_top 11K, MIM option B), drawn and checked with klt's curated `gf180mcu` deck |
| klayout python (labelling) | `klayout==0.30.12` via a throwaway `uv run --with` env |

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
| `pilot_mim_cc` | XCC | `cap_mim_2f0_m4m5_noshield` 16.8u x 16.8u | `cap_array` plate 16.8 x 16.8, num=1 | BOT = schematic `n2` (card pin 1), TOP = `nz` (pin 2) |
| `pilot_rpoly_rz` | XRZ | `ppolyf_u_1k` r_width=2u r_length=22u | `res_array` flavor 1k, length 22, width 2, num=1, dummy=0 | A = `vout`, B = `nz`, bulk = `vss` (substrate) |

Model and terminal facts verified here:

- **MOS**: `klt gen` `w_um` is the per-finger width and fingers are strapped in
  parallel; the extractor reports `fingers` devices of `W/nf` and `klt lvs`
  `combine_devices` folds them to the schematic's total `W` (the PDK model's
  `W` is total width with `nf` separate). Every cell has a gate contact and a
  tap/guard ring labelled `B`; the nfet ring ties to the p-substrate and the pfet
  ring to the n-well. Models extract as `nfet_03v3` / `pfet_03v3`; the 06V0/dualgate
  flavours are not drawn (3.3 V devices only).
- **MIM**: klt's extractor emits the device
  terminals in the order (BOT, TOP) for the labelled plates, extracted as
  `cap_mim_2f0_m4m5_noshield` with `c_width`/`c_length` equal to the drawn
  plate. The PDK model is symmetric in its two terminals, so this does not change
  simulation, but the netlist puts `n2` (the high-impedance first-stage output)
  on the bottom plate, the one with substrate parasitics that the PDK model
  does not include. Plate orientation is a placement-time decision to take when
  the cells are composed.
- **Poly resistor**: geometry alone cannot distinguish `ppolyf_u_1k/2k/3k`; the
  flavour is selected at extraction by `--deck-option poly_res=1k` (matching the
  schematic) and recorded in each LVS request. The substrate is the third
  terminal.

## Results (actual runs; `results.json` and `evidence/` hold the raw output)

| Cell | Footprint um (not an area claim) | DRC, klt curated | LVS, klt |
|---|---|---|---|
| `pilot_nfet_mb1_m5` | 8.04 x 7.92 | clean | match |
| `pilot_nfet_m1_m2` | 6.04 x 6.72 | clean | match |
| `pilot_nfet_m7` | 33.84 x 7.92 | clean | match |
| `pilot_pfet_m3_m4` | 6.04 x 7.92 | clean | match |
| `pilot_pfet_m6` | 40.8 x 7.92 | clean | match |
| `pilot_mim_cc` | 18.92 x 19.88 | clean | match |
| `pilot_rpoly_rz` | 23.04 x 2.44 | clean | match |

- "DRC, klt curated" is klt's curated `gf180mcu` deck only. It checks a subset
  of the foundry rules (`rules_checked` / `rules_skipped` per cell in
  `results.json`), so `clean` is not foundry DRC signoff; antenna and density
  are not covered either.
- Changes versus the `klt 0.7.0` evidence this replaces: every klt verdict is
  unchanged (7 of 7 LVS `match` with the same `mismatch_count` per cell, 7 of 7
  DRC `clean` with 0 violations). The curated deck now checks more rules per
  cell (e.g. 30 vs 20 on the nfet cells), the generated MOS and MIM geometry is
  identical per layer, and `pilot_rpoly_rz` is drawn larger (23.04 x 2.44 vs
  22.84 x 2.0 um) with its device 0.1 um further along x. The earlier evidence
  was generated against `open_pdks c6d73a35`; this run is at the pinned
  `f6eeac7`.
- klt LVS compares device parameters: W and L for MOS, C and A for the MIM
  (the PDK model carries no separate width/length parameter), L and W for the
  resistor. Negative controls (a deliberately wrong reference dimension) were
  run by hand while building this and each produced a `device.property`
  mismatch, so the match status is sensitive to the parameters compared.
  The negative controls are not part of `regen.sh`.
- The `klt lvs` warnings counted in `evidence/*.lvs.klt.json` are
  `topology` entries saying an unused device class has no counterpart; there
  are zero error-severity findings.
### Historical: PDK-runset results (`klt 0.7.0` run, no longer regenerated)

Until issue #282 this pilot also ran the PDK's `run_drc.py` / `run_lvs.py`
(`--variant=D`, flat). Those scripts are not in the ciel install on the
workers (`libs.tech/klayout/` holds only `tech/`) and klt does not drive them,
so the regeneration now uses klt's curated deck only. The last results from
that run, dated 2026-10, are kept here for the record and are not reproduced
by `regen.sh`:

- PDK-runset DRC: the five MOS cells failed `contact_OFFGRID`, the poly
  resistor failed `SB.4` and `SB.10`, the MIM passed.
- PDK-runset LVS: match on the five MOS cells and the resistor; MIM not run
  (the comparer read the reference's area and perimeter as 0 for every
  capacitor card form tried).
- Tool gaps filed from it: `contact_OFFGRID` from `klt gen mos_array
  add_guard_ring` taps off the 0.005 um grid (klayout-tools #2999); `SB.4` /
  `SB.10` salicide-block spacing and poly extension on the resistor
  (klayout-tools #3000); the curated deck says `clean` on both because it has
  no rule for them (klayout-tools #3001). Whether 0.8.0 changes the first two
  is not known: the runset is no longer run here.

No cell was edited by hand to make it pass, and no spec value was changed.

## Next step for full placement (not done here)

1. (#42 has landed; the cells above are regenerated from its values. Rerun
   `regen.sh` if `XCC`/`XRZ` change again.)
2. Re-check #2999 and #3000 (contact grid, salicide-block geometry) with a
   foundry-equivalent DRC before any pilot cell is composed; the curated deck
   cannot see them (#3001).
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
- `ref/`: schematic-derived reference netlists (`*.klt.spice`)
- `extracted/`: klt extracted netlists
- `evidence/`: raw klt JSON (`gen`, `drc`, `extract`, `lvs` and the LVS requests)
- `results.json`: per-cell summary (klt curated DRC and klt LVS)
