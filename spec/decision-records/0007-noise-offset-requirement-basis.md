# 0007: Noise and offset rows — consumer-derived requirement basis and bounded validation plan (DR-3 residuals (e1), (e2))

- **Status**: **proposed — preparation draft. No numeric value is proposed,
  ratified or judged here.** This record prepares the requirement basis the
  operator chose on 2026-10-19 (issue #62: *prepare consumer-derived noise and
  offset allocations first*). It changes no row, bound, tag or condition of
  `spec/target-spec.md`; the noise and offset rows stay `[TBD]` (open, DR-3
  residuals (e1), (e2)). It authorizes no simulation, fleet submission, spend,
  circuit change or new measurement. Under the ratification-via-PR policy
  ([2AMLogic/2am#357](https://github.com/2AMLogic/2am/issues/357)), approving
  the PR that carries this record ratifies **only** the staging and the
  rules below (what is an input, how a limit is derived, what the plan may
  cost), never a number.
- **Date**: 2026-10-19
- **Decided by**: Builder agent, issue #62 (preparation). The operator's
  approval of the carrying PR is the only act that could make any part of this
  binding; a later record carries the numbers.
- **Related**: #62; #7 (T1 tracker), #242 (T1 item 5, blocked by #62);
  [0002](0002-performance-target-bounds.md) (the `3σ, mismatch MC N≥300 +
  process corners` basis); [0003](0003-target-spec-ratification.md) (residuals
  (e1), (e2)); [0005](0005-input-common-mode-range-row.md) (ICMR, VCM = 1.20 V);
  evidence: `sim/noise/records/20261018-060933-afc17a1.md`,
  `sim/offset-mc/records/20261010-185141-e4be1ac.md` (45-point grid),
  `sim/offset-mc/records/20261018-130136-fbdbf60.md` (5-corner record).

## 1. Three things kept apart

| Step | What it is | State for noise / offset |
|---|---|---|
| **Design-to requirement** | A limit derived from a consumer allocation by an explicit, unit-carrying transfer argument | **Not yet derivable**: inputs in §3 are missing or unapproved. Rows stay `[TBD]`. |
| **Ratification** | The operator's approval of a later PR carrying the derived limit | Not requested. Not satisfied by this record. |
| **Measured compliance** | PASS/FAIL of committed evidence against a ratified limit, FAILs disclosed | Not possible before the two steps above. Existing measurements stay evidence, not verdicts. |

A measured maximum plus a margin is **not** a derivation and is not accepted as
one. The existing worst figures (offset: 15.458 mV |mean|+3σ over the 45-point
grid; noise: see the noise record's band table) are not used anywhere below to
choose a limit.

## 2. Evidence status (as of `de0e5ded`, 2026-10-19)

The issue text says offset coverage is nominal-only. That is superseded: the
45-point MOS × T × VDD grid at N = 300 per point (13 500 samples, seed 45,
VCM = VDD/2) was committed on 2026-10-10 as
`sim/offset-mc/records/20261010-185141-e4be1ac.md` (issue #106). The 2026-10-09
curator text predates it. What is still **not** covered, and is the real gap:

| Gap | Why it matters |
|---|---|
| **VCM basis.** The grid uses VCM = VDD/2 (1.485–1.815 V). `gf180-ldo` holds the amplifier inputs at VREF = 1.20 V; [0005](0005-input-common-mode-range-row.md) puts the proposed ICMR floor at 1.185 V, so 1.20 V sits 15 mV inside the edge, set by SS / −40 °C / 2.97 V. Offset at the consumer's VCM has not been measured. | Offset may depend on VCM most strongly exactly where the consumer operates. |
| **Passives.** `res_typical` / `mimcap_typical` only; the PDK deck has no resistor or MIM mismatch. | RZ/CC do not carry DC current, so DC offset is unaffected, but noise gain from any passive-defined consumer loop is out of scope here. |
| **Noise band.** Committed band integrals are 100 Hz–1 MHz (twin precedent) plus three alternatives; the `gf180-bandgap` band is 0.1–10 Hz. The sweep starts at 0.1 Hz, so 0.1–10 Hz is *recomputable from retained spectra* but has not been reported. | Band mismatch: the committed headline number answers a question no consumer asked. |
| **Tail yield.** |mean|+3σ at N = 300 is a statistic, not a yield claim. | Must not be presented as one. |

## 3. Consumer allocations: sources and open inputs

Consumers per `2AMLogic/2am` `repos.yml` (recorded in the target spec's
Consumers section, as of 2026-09-22): `gf180-bandgap`, `gf180-ldo`. A new
`consumes:` entry reopens this section. Source values were read on
2026-10-19 from `gf180-bandgap/README.md` (target-spec table) and
`gf180-ldo/spec/architecture-survey.md` (§ target spec, §5).

### 3.1 Offset

| Item | `gf180-ldo` | `gf180-bandgap` |
|---|---|---|
| System accuracy | VOUT = 1.8 V ±2 % (survey row 2) = ±36 mV at the output | VREF = 1.20 V ±2 % untrimmed = ±24 mV; ±0.5 % trimmed = ±6 mV (3σ, mismatch MC N ≥ 300 + corners, −40…125 °C; trim at 27 °C) |
| Operating conditions | 2.97–3.63 V, 0–50 mA load; temperature not stated in the survey | 2.97–3.63 V, −40…125 °C |
| Transfer from amplifier input offset to system output | Unity-gain at the VREF input: ΔVOUT = ΔVos × (1 + Rf1/Rf2). With VREF = 1.20 V and VOUT = 1.8 V the ideal ratio is 1.5, giving **±24 mV VREF-equivalent** for the ±36 mV output window. Divider ratio is "fixed for the primary design" but the value is not committed in the survey. | Not stated. The amplifier sits in the reference loop; its offset reaches VREF through the loop's PTAT/ΔVBE gain, which the README does not give. |
| Amplifier share of accuracy budget | **Not stated.** Survey does not say whether ±2 % includes VREF error, divider mismatch, or the amplifier. | **Not stated.** |
| Source | `gf180-ldo/spec/architecture-survey.md`, rows 2, 7, § on divider | `gf180-bandgap/README.md`, output-reference row |

**Observation (not a requirement).** If the LDO's ±2 % window is taken to
include the bandgap's untrimmed ±2 % reference error, the two figures are the
same ±24 mV at VREF, leaving no room for amplifier offset. Whether the LDO
window is *inclusive* or *additional* (e.g., a trimmed reference, or a
separate 2 % allowance) is a consumer decision. Until a consumer answers, the
offset allocation is **pending consumer approval** and compatibility is
**unknown**; this record does not make that call.

### 3.2 Noise

| Item | `gf180-ldo` | `gf180-bandgap` |
|---|---|---|
| System noise allowance | **None stated** (no noise row) | "not yet quantified"; threshold TBD |
| Band / weighting | **None stated** | 0.1–10 Hz integrated µVrms (add spot noise if an ADC is later fed) |
| Transfer function | Loop-dependent: VREF-path noise reaches VOUT scaled by (1 + Rf1/Rf2) inside the loop bandwidth (survey: UGBW tens to low hundreds of kHz) and is filtered beyond it; the Cout/ESR network is not fixed | Amplifier-in-loop transfer to VREF not stated |
| Amplifier share | **Not stated** | **Not stated** |

The two consumers' bands do not overlap with the committed 100 Hz–1 MHz
integral (0.1–10 Hz is below it; the LDO's relevant band is unspecified and
bounded above by its closed-loop bandwidth). Because the amplifier has a
1/f region (corner ≈ 0.2–0.5 MHz per the noise README), a low-frequency band
and a wideband band weight very different parts of the spectrum; **one noise
figure will not serve both consumers**, and the 100 Hz–1 MHz twin-precedent band
serves neither as stated. That is a conclusion about bands, not about
compatibility (still unknown).

### 3.3 Inputs requested from consumers

Each request is posted on that consumer's tracker (not here) as a numbered
question, and answered, or explicitly marked "not budgeted", before any
numeric derivation. Nothing below is a proposal of the value itself.

| # | To | Question | Needed form |
|---|---|---|---|
| Q1 | `gf180-ldo` | Is VOUT ±2 % inclusive of VREF error? What share is allocated to error-amp input offset? | Statement, mV or % of VOUT, conditions (T, VDD, load) |
| Q2 | `gf180-ldo` | Divider ratio (1 + Rf1/Rf2) and amplifier-input VCM at the tap | Number |
| Q3 | `gf180-ldo` | Output-noise allowance (if any), band/weighting, and amplifier share | µVrms or V/√Hz, band, weighting |
| Q4 | `gf180-bandgap` | Share of the ±2 % / ±0.5 % budget for the amplifier's input offset; offset-to-VREF gain | mV or %, gain as V/V |
| Q5 | `gf180-bandgap` | 0.1–10 Hz output-noise threshold, amplifier share, noise gain from amplifier input to VREF | µVrms, V/V |
| Q6 | `gf180-bandgap` | Amplifier-input VCM at its operating point | V |

## 4. Derivation rules (for the later numeric record)

These fix *how* a limit is obtained, so a later record cannot pick the answer
first and the argument after.

1. **Offset.** `Vos,max = A_off / G_off`, where `A_off` is the consumer's
   amplifier allocation (volts at the system node, at the consumer's stated
   conditions) and `G_off` the transfer to the system node. The statistic
   compared against `Vos,max` is the one ratified by [0002](0002-performance-target-bounds.md):
   3σ, mismatch MC N ≥ 300 + corners. If a consumer's budget is itself a 3σ
   figure, the amplifier's σ is combined **in quadrature** with the other
   contributors, not added linearly; if it is a worst-case budget, linear
   addition applies. The combination rule is stated per consumer.
2. **Noise.** `v_n,out² = ∫ |H(f)|² S_in(f) df` over the consumer's band, with
   `H` the stated or explicitly assumed transfer function. The amplifier
   limit is `v_n,in,max = sqrt(α) · V_n,budget / ‖H‖` for share `α` of the
   *power* budget (contributors add in quadrature). `H` and `α` are inputs
   from §3.3; any assumed value is labeled **assumed** with the consumer who
   must confirm it.
3. **Conflict handling.** If limits derived for the two consumers disagree, the
   record reports both, which is tighter, and whether one row can serve both
   or the block needs per-consumer rows. It does not average or pick the
   looser limit.
4. **Unset stays unset.** A row whose inputs are absent keeps `[TBD]` and the
   consumer verdict `unknown`.
5. **Reachability.** If a derived limit is not reachable by the committed
   design, the record says so and routes it (a superseding sizing record or a
   consumer renegotiation). It does not loosen the limit.

## 5. Bounded validation plan (proposal; execution is a separate approval)

Nothing in this section has been run. Stages are ordered by cost; each stage
has a stop rule, and a later stage is requested only if an earlier one leaves
the question open.

### Stage 0 — offline, no simulation, no cost (can proceed with this PR's approval)

- Re-integrate the retained noise spectra in `sim/noise/corners/20261018-060933-afc17a1/`
  over each *consumer-requested* band once Q3/Q5 define it. Band edges outside
  the retained 0.1 Hz–10 MHz sweep are **rejected**, not extrapolated.
  Output: an additional append-only table, no new simulation.
- Re-derive offset statistics at the consumer's temperature/supply subset
  from `sim/offset-mc/corners/20261010-185141-e4be1ac/offset_samples.csv`
  (45 × 300 retained samples), reporting mean, sample σ (n−1), |mean|+3σ,
  skew, excess kurtosis, and the empirical min/max.
- Check retained-evidence integrity (counts, seed identities, finite values).

### Stage 1 — offset at the consumer VCM (needs execution approval)

| Item | Proposal |
|---|---|
| Question | Does offset at VCM = 1.20 V (and other consumer VCM values, Q2/Q6) differ from the VDD/2 grid? |
| Axes | 5 MOS corners × 3 T (−40/27/125 °C) × 3 VDD (2.97/3.30/3.63 V) = 45 points, VCM per consumer; **not** the historical 45 × N = 300 at every VCM |
| Samples | N = 300 per point is the ratified basis; a first-pass at **N = 100 on the 45 points × one added VCM = 4 500 units** is proposed to detect a VCM dependence of the sigma large enough to matter. If the σ differs by more than the N=100 sampling error (≈ 7 %, normal approximation) from the matching VDD/2 σ, escalate to N = 300 on only the affected points. The decision is made on σ ratios, not on a PASS/FAIL against any limit. |
| Seeds | base seed 45 (same samples as the existing grid, so the comparison is paired) |
| Controls | mismatch-off (σ = 0 exactly); M1 width imbalance shift (>1 mV); both exist in the driver |
| Failures | every failed/invalid unit is kept and counted; nothing is dropped to obtain a bound |
| Interpretation | |mean|+3σ is a statistic, not a tail-yield guarantee; skew/kurtosis reported; the worst of 45 estimates is biased high by sampling and is labeled so |
| Cost | the historical 13 500-unit request ran 3 688 s unsharded and 4 824 s sharded over 3 jobs; wall time and Spot cost for 4 500 units are **unknown until a pre-submit estimate is produced**; scaling the sharded 13 500-unit wall time linearly gives roughly 1 600 s (±50 %, an estimate, not a measurement); Spot cost is a named unknown |

### Stage 2 — noise at the consumer band (only if Stage 0 cannot answer)

- If the consumer band is within 0.1 Hz–10 MHz: Stage 0 suffices; no new run.
- If a band below 0.1 Hz is required, or a corner/parameter not on the grid
  is needed: one `klt sim` `.noise` request on the 45-point grid, ≈ 50–60 s
  per the committed records, ≈ one small fleet job. Includes the
  `fnoicor` setting audit (record lists `fnoicor = unknown` for the latest
  library, 0 as-extracted); a worst-case-flicker (`fnoicor = 1`) sensitivity
  run is proposed **only** if the consumer budget is tight enough for it to matter.

### Stage 3 — explicitly out of scope here

Passive spread (RZ/CC), global process beyond the five corners, VCM axes beyond
those named by Q2/Q6, and the historical 13 500-unit-per-VCM matrix. Each needs
its own scope.

### Executor prerequisites, retries and stops

- Executor: `klt sim` with `KLT_SIM_BACKEND=batch` on a dispatch worker; no
  hand-launched `ngspice` grids; a failed batch submit is reported, not
  retried locally.
- Retries: at most `--batch-submit-retries 10` (existing driver convention); no
  more than two full re-submits of the same request; a non-reproducing shard
  is reported with its job id.
- Stop rules: stop and report if any point has more than 1 % invalid units, if
  an integrity check fails, if wall time exceeds 2× the stated estimate, or if
  the fleet refuses the runner-version gate without the documented `warn` path.
- Approval gate: Stage 1 and 2 run only after a separate operator approval that
  cites this record's PR, the consumer answers to Q1–Q6, and the pre-submit
  estimate. This record is not that approval.

## 6. What this record does not do

- It proposes no numeric noise or offset limit and changes no row.
- It does not choose the consumer allocations; §3 lists what is known and what
  is asked.
- It does not run or authorize any simulation, fleet submission or spend.
- It does not alter historical records, evidence, targets, load conditions
  (CL = 2 pF), PVT scope or the classic-row definitions.
- Closing issue #62 needs the later numeric record; this PR uses `Part of #62`.

## 7. Exit criteria for the next record

The numeric record (DR-8 or next free number) may be opened when: Q1–Q6 are
answered or recorded as "not budgeted" on the consumer trackers; Stage 0 output
is committed; and the operator has approved any Stage 1/2 execution. It must
cite those, apply §4 mechanically, and keep derivation, ratification and
measured verdict as separate sections.
