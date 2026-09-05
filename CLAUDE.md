# gf180-opamp — agent instructions

Open-source canary block: a two-stage miller-compensated operational amplifier on gf180mcu,
on GlobalFoundries GF180MCU, a 180 nm open CMOS PDK, designed and verified by AI agents.

- **PDK**: GlobalFoundries GF180MCU (https://github.com/google/gf180mcu-pdk). Open-source flow: xschem + ngspice for
  design/sim, klayout-tools (`klt`) for layout work.
- **New block; three-foundry twin** (with sg13g2-opamp and sky130-opamp,
  opened together). Keep bench structure identical across the twins; derive
  all numbers from gf180mcu models.
- **3.3 V primary; 5 V is a stretch row** that only a `spec/` decision
  record can open — mirror how the gf180 Chipalooza briefs treat the 5 V
  rail. Never mix device flavors in one variant without a record.
- **gm/ID first**, committed to `sim/` before sizing.
- **The classic rows are the spec** (gain, GBW/PM into stated CL, slew,
  noise, offset with statistical basis, CMRR/PSRR, swing, power) at PVT
  corners.
- **Friction protocol (the canary's job)**: every time klayout-tools is
  awkward, missing a capability, or wrong for what you need, file an issue at
  `2AMLogic/klayout-tools` describing the tool gap generically — that tracker
  is scoped to the tool, so keep design-specific detail out of it and
  describe the gap, not the design.
- **Verification is the product**: no claim without a testbench; PVT corners
  on every recorded result; `sim/` results are append-only evidence.
- Spec changes go through `spec/` with a decision record; agents do not
  relax the ratified spec to make results pass.
