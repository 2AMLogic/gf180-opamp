# Evidence manifests (`manifests/artifacts/`)

New simulation evidence (issue #271) keeps its **bytes out of git**. A campaign
stages the per-corner tree (`corners/<run-id>/`) in git-ignored scratch
(`sim/build/<bench>-<run-id>/`), stores it with `klt artifact put`, and commits
only:

* `manifests/artifacts/<bench>-<run-id>.manifest.json` — one entry per stored
  file: `name` (the path the file would have had under `sim/`), `sha256`,
  `size`, `kind: evidence`, `producer`, and `uri`;
* the run's Markdown record under `sim/<bench>/records/`, which gains a
  "Stored evidence" section listing each object.

Existing committed evidence is untouched; only new runs go through the store.

## Rules

* Every `uri` is `artifact:<key>` and ends in the entry's sha256. A manifest
  never names a bucket, an ARN or a presigned URL, and the broker URL
  (`$KLT_ARTIFACT_BROKER`) is never written into the repo.
* The writer is **fail closed**: with the broker unset, `klt artifact put`
  missing, the broker unreachable or a put failing, the driver exits nonzero
  and commits no manifest and no record. The staged bytes stay in scratch.
  There is no flag that commits the bytes instead.
* Manifests are append-only, like the rest of the evidence trail.
* CI (`sim/ci_artifact_manifests.py`, run by `selftest.yml`) validates the
  schema and the `artifact:` URIs and fails if a stored object's bytes are also
  tracked in git. It needs neither klt nor the broker.

## Using a manifest

From a host the broker admits (a fleet worker):

```
klt artifact verify manifests/artifacts/<bench>-<run-id>.manifest.json
klt artifact get manifests/artifacts/<bench>-<run-id>.manifest.json#<name> -o <out>
```
