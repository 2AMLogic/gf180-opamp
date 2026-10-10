# Sweep main-clean baseline recipe

A sweep coordinator snapshots the main checkout's `git status` once at sweep
start, then checks each later wave against that snapshot, so that only dirt
introduced *during* the sweep is reported. This page describes the supported
way to do that in this repository while Loom worktree isolation is active.

It uses the installed helper `.loom/scripts/check-main-clean.sh` unchanged.
It adds no guard toggle, exemption or privileged entrypoint, and it does not
move the durable phase checkpoints (`.loom/sweep-checkpoint/issue-<N>.json`)
or change lease renewal.

## Why not `.loom/sweep-checkpoint/`?

Once a Loom-managed worktree exists, the Bash `PreToolUse` guard denies Bash
writes that land in the main checkout. A bundled command such as

```bash
# DENIED: do not do this
cat .loom/sweep-checkpoint/issue-102.json && mkdir -p .loom/sweep-checkpoint && \
  ./.loom/scripts/check-main-clean.sh --snapshot .loom/sweep-checkpoint/main-clean-baseline.txt
```

is rejected as a whole, because of the explicit `mkdir` into the main
checkout. Redirects into the main checkout (`> /home/ubuntu/GitHub/gf180-opamp/...`)
and redirects to an unresolved destination (`> $V/fresh.json`) are denied for
the same reason. Those denials are intended; keep them. Store the baseline
outside the repository instead.

## Recipe

Run each step as its own tool call. Do not bundle them.

1. **Allocate a unique scratch directory**, outside the repository:

   ```bash
   mktemp -d /tmp/gf180-opamp-sweep-XXXXXX
   ```

2. **Read the path it printed** (for example
   `/tmp/gf180-opamp-sweep-k3Q9xz`) and record the literal baseline file path
   in the coordinator's run context, e.g.
   `/tmp/gf180-opamp-sweep-k3Q9xz/main-clean-baseline.txt`. Use one path per
   sweep. Do not reuse another sweep's path.

3. **Snapshot once, before the first wave**, passing the literal absolute path
   (not a shell variable). The cwd must be the main checkout or one of its
   worktrees:

   ```bash
   /home/ubuntu/GitHub/gf180-opamp/.loom/scripts/check-main-clean.sh --snapshot /tmp/gf180-opamp-sweep-k3Q9xz/main-clean-baseline.txt
   ```

4. **Check after each wave** using the same literal path:

   ```bash
   /home/ubuntu/GitHub/gf180-opamp/.loom/scripts/check-main-clean.sh --baseline /tmp/gf180-opamp-sweep-k3Q9xz/main-clean-baseline.txt
   ```

   Exit 0: no new dirt (dirt already present at snapshot time is ignored).
   Exit 3: the main checkout has changes that were not in the snapshot; they
   are listed on stderr. Exit 2: usage error or not inside a git repository.

If the path contains spaces, quote it in both invocations.

### Why this is allowed

The guard judges each command on its resolved write targets. The commands
above write only to a literal `/tmp` path outside the repository, so the guard
has nothing to deny. A variable destination (`--snapshot "$B"`, `> $V/x`) is
unresolvable to the guard and is denied when it could land in the repository,
which is why the literal path is spelled out.

### Main checkout resolution

The helper finds the main checkout with `git rev-parse --git-common-dir`, whose
parent directory is always the main checkout, both when called from the main
checkout and when called from a linked worktree such as
`.loom/worktrees/issue-N`. The snapshot and every later check therefore
describe the same main checkout, whichever directory the coordinator calls
from. (The cwd must be inside the repository: run from elsewhere, the helper
exits 2.)

## Lifecycle

- **Retention.** Keep the baseline file for the whole sweep and pass the same
  literal path to every `--baseline` check.
- **Cleanup.** When the sweep ends (merged, aborted or abandoned), remove the
  scratch directory with its literal path, e.g.
  `rm -r /tmp/gf180-opamp-sweep-k3Q9xz`. Nothing is left in the repository.
- **Missing baseline.** If the file is gone mid-run (for example `/tmp` was
  cleaned or the host rebooted), the helper prints
  `WARNING: ... baseline file '...' is missing or unreadable` and falls back
  to a whole-status check. Any dirt at all, including dirt that was already
  there at sweep start, then fails with exit 3. A missing baseline can
  never hide dirt. The coordinator should **report** the missing baseline in
  its run log or PR/issue comment, and should **not** silently take a new
  snapshot: a snapshot taken mid-run would treat dirt from earlier waves as
  pre-existing and hide it.

## Regression check

`scripts/test-sweep-baseline.sh` exercises this recipe in a disposable fixture
under a fresh `mktemp -d` directory, with copies of the installed hooks and
helper and a managed worktree sentinel, so worktree isolation is active. It
asserts with `jq` on the hook JSON (deny hooks exit 0, so the exit status
alone says nothing):

- the literal-path snapshot and baseline invocations get no deny or ask
  decision from the configured Bash hooks, run from main or from a worktree;
- a main-checkout `mkdir`, a main-checkout redirect, an unresolved `$V/...`
  redirect and the historical bundled form are still denied by write
  confinement, and the mkdir is allowed when no managed worktree exists, which
  shows the deny comes from isolation. Denied payloads are only passed to the
  hook on stdin; they are never executed;
- snapshot and baseline succeed on a clean fixture (path with spaces, called
  from main and from a linked worktree), new tracked and untracked dirt after
  the snapshot is detected, pre-existing dirt is ignored, and a missing
  baseline warns and still fails on dirt without creating a replacement file;
- this repository's own `git status` is unchanged by the run.

```bash
scripts/test-sweep-baseline.sh
```
