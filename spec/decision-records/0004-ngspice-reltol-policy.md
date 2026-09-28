# 0004: ngspice solver-tolerance convention — keep the default (mirrors `sg13g2-opamp` DR-0005)

- **Status**: decided — this record sets no spec value and binds no
  `spec/target-spec.md` row: no bench, deck, or spec bound is touched by
  the PR carrying it. It records this repo's answer to the cross-foundry
  coordination question opened in `sg13g2-opamp`#68: is a tightened
  ngspice solver tolerance (`reltol`/`abstol`/`vntol`) the standing
  convention across the three-foundry-twin `sim/` trees, or does each
  repo keep ngspice's default? The answer recorded here is the status
  quo, not a new constraint: no deck in this repo has ever carried a
  tolerance override, and this record does not add one.
- **Date**: 2026-09-28
- **Decided by**: Builder agent, issue #34
- **Related**: #34 (the issue this decides), `sg13g2-opamp`'s
  [`spec/decision-records/0005-ngspice-reltol-policy.md`](https://github.com/2AMLogic/sg13g2-opamp/blob/main/spec/decision-records/0005-ngspice-reltol-policy.md)
  (DR-0005, deciding `sg13g2-opamp`#68 — the measured screen this record
  cites by reference rather than re-running),
  `sg13g2-opamp`'s `sim/gm-id-characterization/README.md` §
  "Cross-host reproducibility envelope" (the reading-rule template this
  record adopts if this repo ever needs one),
  [`sim/gm-id-characterization/README.md`](../../sim/gm-id-characterization/README.md)
  and
  [`sim/gm-id-characterization/testbench/tb_gmid.spice`](../../sim/gm-id-characterization/testbench/tb_gmid.spice)
  (this repo's own gm/ID harness, cited below for why its extraction
  method differs from the twin's), `CLAUDE.md` (the three-foundry-twin
  rule this record is coordination under, and "gm/ID first")

## Context

`sg13g2-opamp`#68 asked whether the three-foundry-twin fleet (`gf180-opamp`,
`sg13g2-opamp`, `sky130-opamp`) should adopt a tightened ngspice solver
tolerance tree-wide, motivated by a measured ~20% cross-host spread on
`sg13g2-opamp`'s gm/ID study's finite-difference `gds` / `gm_gds` columns.
`sg13g2-opamp`'s DR-0005 answered that question for its own repo with a
measured screen — all eight of its deterministic benches, run at default
tolerance and at several tightened candidates on one host — finding that
every tightened value broke at least one bench's convergence (its
input-CMR bench loses 6–21 of 45 points from `reltol=1e-5` down to
`1e-9` via DC gmin/source-stepping collapse; its slew-rate bench loses
39/45 to TRAN timestep collapse at `1e-9`), while no candidate both closed
the gm/ID envelope at print precision and kept every bench convergent.
DR-0005's decision: keep ngspice's default tolerance tree-wide in
`sg13g2-opamp`, with the gm/ID cross-host spread documented as a per-bench
reading rule rather than pinned away by a tighter solver setting.

Per `CLAUDE.md`'s three-foundry-twin rule — "keep bench structure identical
across the twins" — a solver-tolerance convention is exactly the kind of
cross-cutting methodology choice that should read the same across all
three repos, so this repo and `sky130-opamp` were each asked to record the
matching answer. **This repo has not run DR-0005's screen itself** — that
would require running this repo's own bench suite twice (default vs one or
more tightened candidates) and is out of scope here (see "Out of scope").
This record instead does two things: (1) states plainly, without
re-deriving DR-0005's measurement, that this repo's status quo already
matches the decision DR-0005 reached (no deck here has ever carried a
`reltol`/`abstol`/`vntol` override), and (2) commits this repo to the same
future-proposal gate DR-0005 sets, so that if tightening is ever proposed
here it is screened rather than adopted by convenience.

**What this repo's harness actually looks like today**, so the parallel to
DR-0005's subject is accurate rather than assumed: this repo has two
committed benches, `sim/gm-id-characterization/` (Python driver +
non-templated `tb_gmid.spice`, issue #10) and `sim/gain-gbw-pm/` (a
provisional, smoke-level netlist, issue #19); neither's `.spice` deck sets
`.options reltol`/`abstol`/`vntol`, and neither this repo's `.spiceinit`
(there is none committed) nor any bench harness script injects one.
Unlike `sg13g2-opamp`'s gm/ID study, **this repo's gm/ID extraction does
not use a finite-difference derivative for `gds`**: `tb_gmid.spice`'s own
header states it reads `gm`, `gds`, `id`, `cgg`, `vth` directly off BSIM4's
own small-signal operating-point accessors
(`@m.<inst>.m0[<param>]`) at each DC sweep step — "no separate AC analysis
or finite-difference derivative is needed; BSIM4 computes these as part of
its own Newton-Raphson operating point at each DC step." DR-0005's ~20%
`gds` spread traces to a *finite-difference* central-difference
amplification (a ~1700x fractional-signal amplification on a small
solver-tolerance residual) that this repo's direct-accessor extraction
does not have the same mechanism for. This record therefore does **not**
claim this repo has observed, or is guaranteed to observe, the same
cross-host spread on its own `gds`/`gm_gds` columns — only that the
*convention* (default tolerance, no override) is adopted here for the same
reason DR-0005 gives (a screen is the only way to justify tightening, and
none has been run), and that if a comparable spread is ever observed on
any finite-difference-derived column in this repo's benches, the response
is the same reading-rule discipline DR-0005 models, not a silent tightening.

## Decision

**Keep ngspice's default solver tolerance in this repo's `sim/` tree: no
`reltol`, `abstol`, or `vntol` override line in any deck, testbench
template, or `.spiceinit`, in any bench under `sim/`.** This is a
recording of the existing status quo — every committed record in this
repo's `sim/gm-id-characterization/` and `sim/gain-gbw-pm/` was already
produced at ngspice's default tolerance — not a new restriction adopted
today.

**A future observation of a cross-host or cross-run spread on any
finite-difference-derived (or otherwise tolerance-sensitive) column in
this repo's benches is documented as a per-bench reading rule, following
`sg13g2-opamp`'s "Cross-host reproducibility envelope" section as the
template**, not resolved by tightening the solver tolerance without first
running the screen below.

**Any future proposal to tighten this repo's solver tolerance must first
run `sg13g2-opamp`'s DR-0005 screen against this repo's own bench suite**
— every deterministic bench, run at default tolerance and at the
candidate value(s) on one host, reporting which recorded columns move,
the maximum relative delta, and any convergence-failure count — and must
clear the same gate DR-0005 set: zero broken points across every bench at
the proposed value, closure of any targeted envelope at print precision,
and no material runtime regression. A candidate that fails any leg is
rejected, exactly as DR-0005's own tightened candidates were.

## Alternatives considered

- **Run this repo's own version of DR-0005's screen now, rather than
  deferring it.** Rejected for this record's scope: issue #34 scopes this
  as a mechanical coordination task — recording the shared non-decision
  across the three-foundry twins — not a new measurement campaign. This
  repo currently has only two committed benches versus `sg13g2-opamp`'s
  eight, and neither uses the finite-difference `gds` extraction that
  motivated DR-0005's investigation in the first place, so there is no
  known problem this record needs to screen away; a screen remains
  available as the required gate the moment tightening is proposed here.
- **Pin a tightened tolerance now, on the theory that gf180mcu's models
  are less convergence-sensitive than IHP SG13G2's.** Rejected: unverified
  and exactly the kind of untested assumption DR-0005's "no
  gmin/itl-rescue workaround" rule warns against — a tolerance change with
  no screen behind it is not a convention this repo can defend if a future
  bench's convergence regresses.
- **Wait until this repo has more benches before recording anything.**
  Rejected: the coordination issue asks for the same answer from all
  three twins now, while the status quo is genuinely a non-decision (no
  repo has ever pinned a tolerance) — recording it costs nothing and
  keeps the fleet's methodology statements in sync, per `CLAUDE.md`'s
  twin rule.

## Consequences

- No deck, template, or harness script in this repo is edited by this
  record. No `spec/target-spec.md` row changes.
- `sim/README.md` gains a short pointer to this record so future bench
  authors in this repo know the convention and the required screen before
  proposing a tolerance override (see "Post-merge follow-through").
- `sim/gm-id-characterization/README.md` gains a brief note pointing at
  this record, so a future reader who notices a cross-host `gds`/`gm_gds`
  discrepancy in this repo's own records knows the documented-not-pinned
  convention applies here too, and where the template for the reading-rule
  writeup lives.
- Any future PR proposing a `reltol`/`abstol`/`vntol` override in this
  repo's `sim/` tree must cite this record and present the DR-0005-style
  screen result; a proposal without that screen is out of process.

## Post-merge follow-through

- `sim/README.md` gains a short "Solver tolerance convention" line
  pointing at this record (rides this PR).
- `sim/gm-id-characterization/README.md` gains a one-line pointer noting
  that a future cross-host spread on its columns is a reading-rule
  candidate under this record, not an automatic tightening trigger (rides
  this PR).
- No rollout beyond the two doc pointers above: no template is edited, no
  record is re-minted, no spec row moves.

## Out of scope

- Running DR-0005's screen against this repo's own bench suite — no
  measurement of this repo's benches at any tightened tolerance is
  performed here; the decision above is the status-quo convention plus
  the gate a future proposal must clear, not a fresh measurement.
- Editing `sim/gm-id-characterization/testbench/tb_gmid.spice`,
  `sim/gain-gbw-pm/`'s netlist, or any other `.spice` deck — no tolerance
  line is added to, or removed from, any file (there was none to remove).
- Claiming this repo's gm/ID `gds`/`gm_gds` columns show, or do not show,
  the same ~20% cross-host spread `sg13g2-opamp` measured — this repo's
  extraction method differs (direct BSIM4 accessor, not finite difference;
  see Context) and no cross-host comparison of this repo's own records has
  been run.
- Any change to `spec/target-spec.md` or any ratified bound — this is a
  simulation-methodology record, not a spec change.
