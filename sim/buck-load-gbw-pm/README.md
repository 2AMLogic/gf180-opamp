# `sim/buck-load-gbw-pm/` — Stage A probe: this op-amp into gf180-buck's compensation load

Evidence for issue #295 under `spec/decision-records/0008-buck-compensation-load-gbw-pm-row.md`
§4 **Stage A** (and nothing else): one corner (typical / 27 °C / 3.30 V), the
committed `opamp_two_stage` through the shared harness (`subckt_from_export()`
via `harness.load_dut_text`), the consumer's exact type-III network, VREF = FBN =
0.6 V, 10 µA bias. **It sets no GBW/PM row, relaxes nothing, and does not touch
the ratified 10 MHz / 2 pF row.** Stage B (the 45-point batch) needs a separate
operator approval and is not run here; this driver uses no `klt sim` batch and no
fleet — it is a short sequential list of single local `ngspice -b` units.

## Files

- `testbench/consumer_load.inc` — the consumer network (`gf180-buck`
  `design/netlists/compensator.spice`, sha recorded in the file) and the
  PWM-comparator input approximation. The runner guards the values against
  DR-0008 §2.1; a changed consumer netlist reopens the DR, it is not followed.
- `testbench/tb_region.spice` — closure-independent region check (`VC` forced).
- `testbench/tb_ac.spice` — DC-closed / AC-open small-signal bench
  (`Lfb` + `Vset` servo; technique of `sim/gain-gbw-pm/README.md`).
- `testbench/tb_beta.spice` — 1/β(f) from the passive network alone.
- `run_stage_a.py` — driver; `--smoke` runs only the region check and writes nothing.
- `test_stage_a.py` — no-simulator self-test (plateau/crossing extraction,
  region verdict, bench-fault vs finding classification, isolation rules, guards).
- `records/<id>.md`, `corners/<id>/`, `netlist-snapshots/<id>.spice` — append-only
  evidence; a re-run mints a new id and never overwrites.

## Run

```bash
python3 sim/buck-load-gbw-pm/test_stage_a.py      # no simulator
python3 sim/buck-load-gbw-pm/run_stage_a.py       # needs ngspice + the pinned gf180mcu PDK; mints a record
```

Exit 0 for a valid result or a finding; 1 for a bench fault (fix and re-run under
the same approval); 2 if `ngspice` is missing.

## Classification (DR-0008 §4)

A result is a **finding** only when the closure-independent region check itself
shows XM1/XM2/XM5 not on and saturated at VCM = 0.6 V; Stage B is then not run.
Servo non-convergence, a failed `Lfb` isolation study or plateau check, or a
small-signal bench region the region check does not reproduce is a **bench
fault**. `VC_bias` is an assumption (C3) and is reported at ±0.15 V.
