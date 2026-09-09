# spec/

Target specification and decision records. Spec changes require a decision record.

- [`target-spec.md`](target-spec.md) — the block's single consolidated
  target-spec table (DRAFT; not yet ratified).
- [`porting-plan.md`](porting-plan.md) — same-PDK architecture survey against
  `gf180-bandgap`/`gf180-ldo`, and the open-items tracker for topology/CL/
  device-characterization work.
- [`decision-records/`](decision-records/) — one file per decision, following
  [`gf180-bandgap`'s `NNNN-<slug>.md` numbering and
  template](https://github.com/2AMLogic/gf180-bandgap/blob/main/spec/decision-records/TEMPLATE.md)
  (Status / Context / Decision / Alternatives considered / Consequences).
  Currently holds
  [`0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md)
  (input-pair polarity, output-stage class, cascode-or-not, and `CL`).
