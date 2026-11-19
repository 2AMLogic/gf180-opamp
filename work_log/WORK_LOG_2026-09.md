# Work Log Archive — 2026-09

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

### 2026-09-15

- **PR #22**: Add sim/gain-gbw-pm/: first circuit-level spec-row testbench with a one-command driver
- **PR #21**: feat: add gm/ID-sized two-stage op-amp schematic and DC-OP check
- **PR #20**: spec: propose bounds for target-spec.md [TBD] performance rows (DR-2)
- **Issue #19** (closed): First spec-row testbench under sim/ with one-command driver
- **Issue #18** (closed): Ratify spec/target-spec.md via decision record: propose bounds for [TBD] performance rows
- **Issue #17** (closed): Schematic entry: two-stage op-amp topology (DR-0001) in design/

### 2026-09-09

- **PR #15**: refactor: remove dead code in run_gmid.py
- **PR #13**: spec: topology + CL decision record (0001) for the two-stage Miller op-amp
- **PR #11**: feat: add gm/ID characterization sweep for gf180mcu 3.3V MOS
- **Issue #14** (closed): Remove dead code in run_gmid.py: unused NOMINAL_VDD constant and unused build_plots record param
- **Issue #12** (closed): spec: topology + CL decision record (0001) for the two-stage Miller op-amp, cited against the gm/ID study (#10) — the gf180 twin of sg13g2-opamp#6
- **Issue #10** (closed): sim: gm/ID characterization sweep of gf180mcu 3.3 V MOS — the first engineering task (porting-plan §4, gm/ID-first)
