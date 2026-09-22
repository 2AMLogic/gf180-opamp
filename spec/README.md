# spec/

Target specification and decision records. Spec changes require a decision record.

- [`target-spec.md`](target-spec.md) — the block's single consolidated
  target-spec table (**RATIFIED (partial)** via
  [`decision-records/0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md)),
  including the non-normative **Consumers** section naming the fleet blocks
  that consume this one, the requirement rows they impose, and the
  structured integrator view (`manifests/integrator.json`).
- [`porting-plan.md`](porting-plan.md) — same-PDK architecture survey against
  `gf180-bandgap`/`gf180-ldo`, and the open-items tracker for topology/CL/
  device-characterization work; §5 records where findings about a consumer's
  own block live (the consumer's tracker, not this repo).
- [`decision-records/`](decision-records/) — one file per decision, following
  [`gf180-bandgap`'s `NNNN-<slug>.md` numbering and
  template](https://github.com/2AMLogic/gf180-bandgap/blob/main/spec/decision-records/TEMPLATE.md)
  (Status / Context / Decision / Alternatives considered / Consequences).
  Currently holds
  [`0001-topology-and-cl.md`](decision-records/0001-topology-and-cl.md)
  (input-pair polarity, output-stage class, cascode-or-not, and `CL`),
  [`0002-performance-target-bounds.md`](decision-records/0002-performance-target-bounds.md)
  (recommended bounds for the performance rows), and
  [`0003-target-spec-ratification.md`](decision-records/0003-target-spec-ratification.md)
  (the ratification record and its residual register).
