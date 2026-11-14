# Roadmap

gf180-opamp is a two-stage Miller-compensated op-amp on GF180MCU (3.3 V
primary), built to carry a full, reproducible evidence trail and to expose
klayout-tools gaps along the way.

Status as of 2026-11-13, `main` @ `affb68e3`.

The verdict of record for the evidence ladder is
[`manifests/gf180-opamp.signoff.json`](manifests/gf180-opamp.signoff.json),
tracked in [#7](https://github.com/2AMLogic/gf180-opamp/issues/7).

## Where it stands

**Schematic-level characterization is broad; there is no block layout; the
block is below T1 (4 of 11 items met).**

| T1 item | Status in the signoff record | What it waits on |
|---|---|---|
| 1 Design sources | met | — |
| 2 Layout | unmet, `no_evidence` | core layout, #241 |
| 3 DRC clean | unmet, `no_evidence` | #241 |
| 4 LVS clean | unmet, `no_evidence` | #241 |
| 5 Full corner verification vs a ratified spec | unmet, `no_evidence` | per-row citation, #242 (parked on #240) |
| 6 Statistical claims carry Monte Carlo evidence | unmet, `no_evidence` | ratified statistical rows (#62, DR-0006); its own issue is split out of #7 after that |
| 7 Post-layout verification | unmet, `no_evidence` | `klt pex` on the core, #279 (after #241) |
| 8 Characterization report | met | — |
| 9 Testbenches shipped | met | — |
| 10 Repo hygiene | met | — |
| 11 Power delivery (structural) | unmet, `no_evidence` | `klt erc` on the core, #241 |

Done and evidenced:

- **Spec.** `spec/target-spec.md` is ratified in part (DR-0003). Gain, GBW,
  phase margin, slew, swing and power are ratified targets. Noise and offset
  bounds are open (DR-0007 is a preparation draft with no numbers); the CMRR,
  PSRR (DR-0006) and input common-mode (DR-0005) rows are proposed, not
  ratified. The 5 V stretch row is unopened.
- **Ratified rows measured.** All six pass at 45/45 PVT points on the gm/ID-sized
  schematic, passives at typical (records `20261010-161514-0127eb7`,
  `20261010-161852-0127eb7`). Full-envelope passive studies (405 cells) exist
  for gain/GBW/PM, slew/swing/power and PSRR as supplementary evidence.
- **Open rows measured, no verdict.** Noise, offset, CMRR, PSRR and their
  mismatch Monte Carlos, input common-mode range and step response.
- **PDK re-pin.** `sim/harness.py` pins open_pdks `f6eeac7` and the cited
  experiments were re-run under it (PRs #243 to #294). The report still selects
  the October `c6d73a3` records; switching it is the open part of #240.
- **Layout pilot.** `layout/pilot/` holds seven isolated device cells, each
  `klt drc` clean (curated deck) and `klt lvs` match under klt 0.8.0 and
  `f6eeac7`. It is not block layout.
- **Consumer load probe.** `sim/buck-load-gbw-pm/` holds two Stage A records of
  this amplifier in the gf180-buck compensation network (DR-0008). No row is
  opened.

## Now

1. **Land the full-grid PSRR mismatch record** (#190, PR #310, in review).
   Done when `sim/psrr-mc/records/20261108-220319-bb1a6a0.md` and its artifact
   manifest are on `main`. The report selection is unchanged by that PR.
2. **Compose the first-stage sub-block from the pilot generators** (no issue
   yet; first slice of #241). Done when `layout/first_stage/` holds a GDS for
   XM1 to XM5 with a one-command `regen.sh`, a clean `klt drc` envelope and a
   `klt lvs` match against a sub-netlist cut from the schematic netlist. The
   pin and deck are already on `main`, so it does not wait on #240.
3. **Make the pilot's LVS negative controls reproducible** (no issue yet).
   They were run by hand and are not part of `regen.sh`. Done when `regen.sh`
   runs one wrong reference per device kind, records the mismatch in
   `results.json`, and fails if a wrong reference matches.
4. **Offline consistency check for the pilot** (no issue yet). Done when a
   no-PDK test in CI fails if a pilot cell's dimensions drift from the
   schematic netlist or `results.json` disagrees with `layout/pilot/evidence/`.
5. **Documentation corrections** (no issue yet). `spec/README.md` does not
   index DR-0006, and the README status says PSRR is at typical passives only
   although `sim/psrr/records/20261011-102729-6612014.md` is a full-envelope
   study. Done when both read correctly and the spec-citation checks pass.
6. **Remove dead parameters in the sim drivers** (#320, awaiting approval).
   Done when the six listed elements are gone and `sim/selftest.sh` passes.

## Next

- **Full core layout with DRC, LVS and ERC evidence** (#241; T1 items 2, 3, 4,
  11). Follows Now item 2. Done when the signoff record renders those four
  items `met`. Carries two placement decisions the pilot left open: the MIM
  plate orientation (`n2` is on the bottom plate today) and a metal-stack
  record for `gf180mcuD` in `spec/`.
- **Per-row citation for T1 item 5** (#242). Unblocked when #240 closes. Done
  when the manifest declares the full row set, cites one `klt sim` envelope
  per covered row, and lists the unratified rows as declared gaps.
- **Post-layout delta report** (#279; T1 item 7). Unblocked by #241. Done
  when a `klt pex` report with a delta row per ratified row is cited and its
  `body_bias` status is stated in `manifests/README.md`.
- **Full-grid CMRR mismatch Monte Carlo** (#98, via #140). The driver already
  shards the grid; it waits on batch capacity. Done when a 45-point x N=300
  record exists and is compared with the DR-0006 estimate.

## Later

- **Noise and offset bounds.** Which band and allocation do the consumers
  need? DR-0007 lists six questions for gf180-ldo and gf180-bandgap that come
  before any number.
- **Load-specific GBW/PM row for gf180-buck.** Does Stage B (the 45-point
  batch of DR-0008) run, or is the consumer ruled out of scope?
- **Passive-corner coverage of the remaining figures.** Do noise, offset, CMRR
  and input common-mode need RZ x CC sweeps, or does a written argument do?
- **PDK-runset DRC.** The curated deck checks a subset of rules, and the
  pilot's last PDK-runset run failed on rules filed as klayout-tools #2999,
  #3000 and #3001. What is the route to a runset-clean core?
- **5 V stretch row.** Which consumer needs it? It opens only by its own record.

## Blocked and waiting

| Item | Waits on |
|---|---|
| #240 report and signoff switch to the `f6eeac7` records | A decision on how the report verifies records whose corner bytes live only in the artifact store (options are in the issue). Also holds #242. |
| #62 noise and offset bounds | Consumer answers to DR-0007 Q1 to Q6. |
| #295 load-specific GBW/PM row | Separate approval for Stage B (DR-0008 section 4). |
| DR-0005, DR-0006 rows | Ratification; both records read `proposed`. |
| #98 / #140 full-grid CMRR Monte Carlo | Batch capacity; the park does not release on its own (see #140). |
| #274 live `klt artifact verify` | A recorded campaign on a worker with artifact-store access. |
| #214 | Appears superseded by PR #317; needs a close-or-keep call. |

The labelled backlog has no ready issue today: #190 is in review and every
other open issue is parked or awaiting approval. Now items 2 to 5 are new work
that needs none of the decisions above.

## Not planned

- Relaxing a ratified bound to make a result pass, hand-edited layout, or
  hand-looped `ngspice` grids.
- Mixing 3.3 V and 5 V device flavours in one variant.
- Any T1 claim before the signoff record renders it.
