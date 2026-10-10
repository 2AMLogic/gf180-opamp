# layout/

Layout (klayout-tools driven) and DRC/LVS signoff artifacts.

## Troubleshooting: scratch cleanup after `cd`

The destructive-command guard (`.loom/hooks/guard-destructive-generic.sh`)
runs as a hook and resolves relative `rm` targets against the **hook's cwd**
(the directory the session started in), not against the **shell cwd** that an
earlier `cd` in the same compound command would produce. A cleanup that is
correct in the shell can therefore be classified as an outside-worktree target
and denied (`rm-scope-outside-repo`).

Minimal denial example (hook cwd is the worktree root; the second target
resolves outside it):

```bash
cd layout/pilot/evidence && rm -f _rq.json ../ref/_bad.spice
```

Equivalent cleanup with absolute paths inside the current worktree, in two
separate steps.

1. Print the worktree root on its own:

   ```bash
   git rev-parse --show-toplevel
   ```

2. Paste that output into the cleanup as **literal text**, in place of
   `<worktree-root>` below. Quote every path so a root containing spaces
   still works:

   ```bash
   rm -f "<worktree-root>/layout/pilot/evidence/_rq.json" "<worktree-root>/layout/pilot/ref/_bad.spice"
   ```

Do not store the root in a shell variable or splice it in with `$(...)`
(for example `"$WT_ROOT/..."` or `"$(git rev-parse --show-toplevel)/..."`).
The guard does not expand variables or command substitution in `rm` targets.
It treats them as unresolved and denies them, so the target must be a literal
path.

Name only the specific disposable scratch files you created. Do not use
globs or directory removal for this workaround.

### Not covered by this workaround

- Unresolved directory or target expressions (for example `cd "$UNKNOWN_DIR"`,
  or shell variables and `$(...)` inside `rm` targets); the guard cannot
  prove where the targets land and denies them.
- Symlink escapes: do not use this for scratch targets, or ancestors of
  them, that are symlinks.
- Protected paths.
- Targets outside the current worktree.

An allow result from the guard classifies the path only. It is not proof that
a target is disposable; confirm that yourself before deleting.

Do not edit the generated hook, disable guards, or look for a bypass. For
future guard investigations, the canonical source is Repo Skills:
[`hooks/repo/guard-destructive.sh`](https://github.com/rjwalters/repo/blob/main/hooks/repo/guard-destructive.sh).
