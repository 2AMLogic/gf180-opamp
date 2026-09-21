# `manifests/` — the `klt signoff` verdict of record

This directory holds gf180-opamp's **block manifest** for
[`klt signoff --manifest`](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/signoff.md)
— the machine-graded statement of this block's gap to T1 on the
[klayout-tools design-evidence ladder](https://github.com/2AMLogic/klayout-tools/blob/main/docs/design-evidence-tiers.md).
It exists so the block's T1 state is **rendered, not hand-read**: every T1
item comes back `met`/`unmet` with an explicit `reason`, and CI regenerates
the verdict on every push, so evidence that no longer matches the manifest
fails the build instead of rotting (issue #23). The fleet-side consumer of
this file is the roll-up proposed in 2AMLogic/2am#956; the block's gap-to-T1
tracker (#7) points here as the verdict of record.

## Files

| File | What it is |
|---|---|
| `gf180-opamp.json` | The block manifest: `block` (how this repo is identified in a fleet roll-up), `kind`, and the per-item `evidence` map. |
| `design-evidence-tiers.md` | The T1–T4 checklist, vendored verbatim from klayout-tools and pinned (see "Why the checklist is vendored"). |
| `gf180-opamp.signoff.json` | The committed `klt signoff --manifest` JSON record — the verdict of record. |
| `README.md` | This file. |

## Current verdict, and why the `evidence` map is empty

`kind` is `"analog"`, confirmed against the block itself — a two-stage
Miller-compensated op-amp with no digital or mixed-signal partition — not
assumed from the issue that requested the manifest.

The `evidence` map is **deliberately empty**. As of this manifest the repo
contains no `klt` evidence envelopes at all: no layout (so no `klt drc`, no
`klt lvs`, no `klt pex` reports), no `klt sim` corner runs, no `klt yield`
campaign — the committed `sim/` records are raw ngspice results, which are
not `klt` envelopes. Per the issue #23 framing, an all-`unmet` manifest with
`reason: no_evidence` on every item is the **correct** honest state: it
replaces hand-written prose that goes stale with a mechanical read that
cannot. Every row currently renders `unmet`/`no_evidence`, `tier` is
`null`, and the command exits `3` — that is the point, not a failure.

## Regenerating the record

From the repo root:

```bash
klt signoff --manifest manifests/gf180-opamp.json \
            --tiers-doc manifests/design-evidence-tiers.md \
            --format json > manifests/gf180-opamp.signoff.json
```

`klt signoff` exits `3` while the block is below T1 (report produced, `tier:
null`); `0` once every T1 item is `met`. Gate on the record's `tier` field,
not the exit code — the CI job below accepts both `0` and `3` and fails on
anything else. The record is byte-deterministic for a fixed klt build,
checklist revision, and manifest, which is what lets CI hold it to a
byte-comparison.

## Why the checklist is vendored

`klt` bundles its own copy of `design-evidence-tiers.md`, but the klt release
pinned in CI (**0.5.0**, published 2026-09-15) predates the checklist's
eleventh item — "**Power delivery (structural)**", added upstream in
[klayout-tools#2025](https://github.com/2AMLogic/klayout-tools/pull/2025)
commit `428951e` (2026-09-19). Against the bundled copy this manifest would
render only ten rows and leave #23's "item 11 has a row, even if `unmet`"
unsatisfiable. The vendored copy is taken verbatim from klayout-tools at
commit `b15edf5e`, pinned so that every report states which checklist
revision graded it: the record quotes each item's text, so editing or
re-vendoring the checklist changes the record and fails CI until the record
is regenerated in the same change.

To re-vendor (a checklist bump is a deliberate act, not something CI does):

```bash
git -C <klayout-tools checkout> show <commit>:docs/design-evidence-tiers.md \
    > manifests/design-evidence-tiers.md
```

then update the commit hash in this README and regenerate the record, all in
one PR — mirroring how the pinned klt version is bumped (below).

## Citing evidence in the manifest

When a real `klt` evidence envelope lands in this repo — a `klt drc` report,
`klt lvs`, `klt sim`, `klt yield`, `klt pex` — add it to the `evidence` map
and regenerate the record in the same change. Rules the grader enforces,
restated from the [upstream contract](https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli/signoff.md):

- **Pin `content_hash` on every citation.** The pinned hash is the envelope's
  own `provenance.input.content_hash`; a citation whose input drifted renders
  `unmet` (`stale_evidence`) rather than quietly passing against the wrong
  revision. A manifest entry with no pinned hash cannot have its freshness
  verified at all.
- **Items 3–8 are kind-restricted**: item 3 accepts only `drc`, item 4 only
  `lvs`, item 5 only `sim` (analog column), item 6 only `yield`, item 7 only
  `pex`, item 8 only the opt-in `generic` envelope. Anything else is
  `wrong_kind`.
- **Items 1, 2, 9 and 10 merit no padding.** The tool grades them on "some
  passing envelope was cited", not topical relevance — re-citing an
  unrelated envelope to green a row is the exact hand-read dishonesty this
  manifest exists to end. Leave them `unmet`/`no_evidence` until an artifact
  that genuinely backs the claim exists.
- **Do not cite item 11 while the klt pin is 0.5.0.** Item-11 grading rules
  (`klt erc`/`lvs`/`place-and-route` supply evidence) are newer than this
  release; under a build without them a citation falls through to the
  unrestricted grading path and could render `met` from rules that do not
  exist in the running build. Upgrade the klt pin first (below), then cite.

## The klt pin

CI installs `klayout-tools==0.5.0` — the same code that produced the
committed record, whose exit the byte-comparison holds. When a newer
klayout-tools release ships (in particular one with item-11 grading and the
graded-by-build machinery), bump the pin in `.github/workflows/signoff.yml`,
regenerate `gf180-opamp.signoff.json`, and commit both in the same PR.
Until then the record stays exactly reproducible from the pinned release.
