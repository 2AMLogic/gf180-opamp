# `corners/` — raw per-run ngspice logs

Per `sim/README.md`'s directory convention, this holds one subdirectory per
`<record-id>`, each containing the raw ngspice transcript + `wrdata` data
file for every PVT point that run's `records/<record-id>.md` summarizes:

```
corners/
  <record-id>/
    <corner>_<temp>c_nosupply.log   # ngspice -b stdout/stderr transcript
    <corner>_<temp>c_nosupply.dat   # wrdata output: (Vsweep, param) pairs
```

## Adopted PVT grid for this study

- **Process**: `typical, ff, ss, fs, sf` — the five top-level `.LIB` MOS
  corner bundles gf180mcu's `sm141064.ngspice` ships (`fs` = fast NMOS / slow
  PMOS, `sf` = the reverse; verified directly against the model file's own
  `.LIB fs` / `.LIB sf` sections, which pull in `nfet_03v3_fs`+`pfet_03v3_fs`
  and `nfet_03v3_sf`+`pfet_03v3_sf` respectively). This is the grid
  `spec/target-spec.md` §1 named as `gf180-bandgap`'s likely template for
  this block's corner-grid row — this study is the first to actually run it
  on this PDK for this block, confirming it works unmodified as the MOS
  corner set (`gf180-bandgap/sim/harness/corners.py`'s `"mos"` corner set
  uses the identical five names).
- **Temperature**: −40 °C, 27 °C, 125 °C — the fleet-wide convention named in
  `CLAUDE.md`.
- **Supply**: not applicable at the device level — see each record's
  "Corner matrix run" field for the full justification. A representative
  Vds = 1.65 V (half of the nominal 3.3 V supply) is used instead; the
  ±10 % supply axis applies once an actual circuit stage with a supply rail
  exists to sweep.

15 points total (5 process × 3 temperature) per run, covering both
`nfet_03v3`/`pfet_03v3` polarities and all 5 swept lengths in a single
ngspice invocation per point (see `../testbench/tb_gmid.spice`).
