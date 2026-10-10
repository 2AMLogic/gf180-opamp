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

CI (and you, locally) can check the committed netlist is current without
writing into the repo: `python3 sim/ci_netlist_check.py` exports into a temp
dir and compares, normalizing only the `** sch_path:` comment line. It treats
exit 10 as success but still requires a complete, non-empty export.

**Pinned exporter: xschem 3.4.7** (upstream tag `3.4.7`, commit
`92dd8fe5f4d5c1057489710d8a22f18fdc9d7ed0`). The committed netlist was
exported with it, and other releases format the same circuit differently
(xschem 3.4.4, the ubuntu-24.04 apt package, adds a blank line after
`**.subckt` and wraps device lines and `+` continuations differently). That
formatting is not normalized away, so regenerate and check with exactly this
release: `xschem --version` must print `XSCHEM V3.4.7`.
`sim/ci_netlist_check.py` (constant `PINNED_XSCHEM_VERSION`) and
`sim/ci_prereqs.py` refuse any other version with an expected/actual message
before exporting. CI builds it from source (`.github/workflows/selftest.yml`,
`SELFTEST_XSCHEM_VERSION` / `XSCHEM_COMMIT`). For a local build, see
`sim/README.md` -> "CI selftest". Changing the pin means regenerating the
netlist with the new release in its own justified change.

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
characterization of this schematic lives under `sim/` instead: gain, GBW and
phase margin
([`sim/gain-gbw-pm/records/20261010-161514-0127eb7.md`](../sim/gain-gbw-pm/records/20261010-161514-0127eb7.md)),
noise
([`20261010-163507-0127eb7`](../sim/noise/records/20261010-163507-0127eb7.md)),
offset mismatch Monte Carlo
([`20261010-185141-e4be1ac`](../sim/offset-mc/records/20261010-185141-e4be1ac.md)),
CMRR
([`20261010-163601-0127eb7`](../sim/cmrr/records/20261010-163601-0127eb7.md))
PSRR
([`20261010-163854-0127eb7`](../sim/psrr/records/20261010-163854-0127eb7.md))
and slew / swing / power
([`20261010-161852-0127eb7`](../sim/slew-swing-power/records/20261010-161852-0127eb7.md)).
The small-signal numbers quoted in `opamp_sizing.md` are the original gm/ID
predictions, kept as the sizing record; where a measured value exists the
`sim/` record, not the prediction, is the evidence (the Miller compensation was
retuned in issue #42, RZ 11.23 kOhm / CC 0.578 pF; phase margin now measures
71.99 degrees worst over the 45 points, against 57.34 before the retune). The
remaining gap is tracked by
[#7](https://github.com/2AMLogic/gf180-opamp/issues/7).

## Prerequisites

`xschem`, `ngspice`, Python 3, and the pinned gf180mcu PDK revision
(open_pdks `c6d73a35f524070e85faff4a6a9eef49553ebc2b`, `gf180mcuD` variant)
— see
[`sim/gm-id-characterization/README.md`](../sim/gm-id-characterization/README.md)
for the volare install recipe this repo uses.
