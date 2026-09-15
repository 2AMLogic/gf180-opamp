# design/

Schematics (xschem) and netlists.

```
design/
  xschemrc                     repo xschem config: resolves the PDK, adds repo symbol libraries
  opamp_two_stage.sch          the amplifier schematic
  opamp_two_stage.sym          its symbol (must stay next to the .sch)
  opamp_sizing.md              gm/ID sizing derivation + citations
  check_dc_op.py               nominal DC operating-point smoke check
  netlist/                     xschem-generated .spice netlists -- regenerated, never hand-edited
```

## The schematic

[`opamp_two_stage.sch`](opamp_two_stage.sch) is the block's two-stage
Miller-compensated op-amp: an NMOS input differential pair with a PMOS
current-mirror load, driving a PMOS common-source output device loaded by an
NMOS current sink, with a Miller capacitor and series nulling resistor
between the two stages and no cascode anywhere — the topology ratified in
[`spec/decision-records/0001-topology-and-cl.md`](../spec/decision-records/0001-topology-and-cl.md),
whose argument is not restated here.

Pins: `vdd vss vinp vinn vout ibias` (in that order — the symbol's pin
declaration order, the `.sch`'s `iopin` order and the netlisted subcircuit's
port order all match). `ibias` takes an externally supplied 10 µA reference
current; there is no on-chip reference in this block.

Every device size comes from gm/ID methodology applied to
[`sim/gm-id-characterization/records/20260909-052956-79c6a45.md`](../sim/gm-id-characterization/records/20260909-052956-79c6a45.md).
The full derivation — the target overdrive, the gm/ID and ID/W row behind
each width, the compensation arithmetic, and the as-simulated operating
point — is in [`opamp_sizing.md`](opamp_sizing.md). **Change sizes there and
here together**: that document is the citation trail `CLAUDE.md`'s "gm/ID
first" rule requires.

**Hierarchical schematic-cell symbols must live next to their `.sch`, not in
a `symbols/` subdirectory.** xschem auto-descends into a child schematic only
when the referencing symbol is found at the same relative path as a
same-named `.sch` file. A symbol filed elsewhere netlists as an empty
subcircuit — no error, just missing devices.

## Regenerating the netlist

`design/netlist/opamp_two_stage.spice` is a **generated artifact**: it is
whatever xschem exports from `opamp_two_stage.sch`, committed unedited so
the diff of a schematic change is reviewable. After any schematic edit,
regenerate it from the repository root with:

```bash
xschem -n -x -q --rcfile design/xschemrc -o design/netlist design/opamp_two_stage.sch
```

(`-n` netlist, `-x` no GUI, `-q` quit when done, `--rcfile` forces this
repo's config regardless of cwd or of any `~/.xschem/xschemrc` on the
machine.) **`xschem -q` exits 10 on a successful netlist run** — check that
`design/netlist/opamp_two_stage.spice` was rewritten, not the exit status,
if you wrap this in a script. `design/xschemrc` finds the gf180mcu install by the same rules as
`sim/gm-id-characterization/run_gmid.py` — `GF180_PDK_PATH`, then
`PDK_ROOT` + `PDK`, then `~/.volare/gf180mcuD` — sources the PDK's own
xschemrc so the gf180mcu device symbols resolve, and adds `design/` plus
every `sim/<experiment-slug>/testbench/`.

To open the schematic interactively:

```bash
xschem --rcfile design/xschemrc design/opamp_two_stage.sch
```

## Checking the operating point

```bash
python3 design/check_dc_op.py
```

Composes a deck from the generated netlist (PDK models at `typical` +
`res_typical` + `mimcap_typical`, 27 °C, VDD = 3.3 V), wraps the amplifier
in a unity-gain buffer at VCM = 1.65 V with `CL` = 2 pF and 10 µA into
`ibias`, runs `ngspice -b`, and asserts that the operating point converged,
that every MOSFET is on and saturated with ≥ 50 mV of `|Vds| − |Vdsat|`
margin, that the bias mirror delivers its 10 µA / 60 µA design currents, and
that the closed-loop output sits on the input common mode. Exit 0 = PASS.

Closing the loop is what makes the DC operating point well defined: open
loop, a ~96 dB amplifier's output node is pinned by device mismatch rather
than by design.

This is a **smoke check, not characterization** — one corner, no AC
analysis, and it deliberately writes no `sim/` record. PVT-cornered
verification against `spec/target-spec.md`'s performance rows needs a real
testbench under `sim/` and is tracked by the gap-to-T1 tracker,
[#7](https://github.com/2AMLogic/gf180-opamp/issues/7) item 5. Every
small-signal number quoted in `opamp_sizing.md` is a gm/ID prediction until
that bench exists.

## Prerequisites

`xschem`, `ngspice`, Python 3, and the pinned gf180mcu PDK revision
(open_pdks `c6d73a35f524070e85faff4a6a9eef49553ebc2b`, `gf180mcuD` variant)
— see
[`sim/gm-id-characterization/README.md`](../sim/gm-id-characterization/README.md)
for the volare install recipe this repo uses.
