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

Equivalent cleanup with absolute paths inside the current worktree. Identify
the worktree root first, then quote every path so a root containing spaces
still works:

```bash
WT_ROOT="$(git rev-parse --show-toplevel)"
rm -f "$WT_ROOT/layout/pilot/evidence/_rq.json" "$WT_ROOT/layout/pilot/ref/_bad.spice"
```

Name only the specific disposable scratch files you created. Do not use
globs or directory removal for this workaround.

### Not covered by this workaround

- Unresolved directory expressions (for example `cd "$UNKNOWN_DIR"`); the
  guard cannot prove where the targets land and denies them.
- Symlink escapes: do not use this for scratch targets, or ancestors of
  them, that are symlinks.
- Protected paths.
- Targets outside the current worktree.

An allow result from the guard classifies the path only. It is not proof that
a target is disposable; confirm that yourself before deleting.

Do not edit the generated hook, disable guards, or look for a bypass. For
future guard investigations, the canonical source is Repo Skills:
[`hooks/repo/guard-destructive.sh`](https://github.com/rjwalters/repo)
(https://github.com/rjwalters/repo).
