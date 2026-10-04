# Shared-Tree Operating Rules

A checkout of this repository may be shared: more than one person, agent, or
automation session can be working in the same directory at the same time.
Never assume you have exclusive ownership of a checkout.

## The rule

Before modifying any checkout: run `git status -sb`, identify the current
branch, worktree, and upstream, inspect uncommitted changes and recent
ownership, never assume exclusive ownership, and stop or use a separate
worktree if another live session owns it.

## Git pre-edit checklist

Copy-paste this gate and run it in the target checkout before making any
edit:

```bash
# 1. Current branch, upstream, and dirty state in one view.
git status -sb

# 2. Which worktree am I in, and what other worktrees exist for this repo?
git rev-parse --show-toplevel
git worktree list

# 3. Upstream tracking and how far ahead/behind we are.
git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || echo "no upstream"
git rev-list --left-right --count HEAD...@{u} 2>/dev/null

# 4. Uncommitted changes: what exists, and when was it last touched?
git diff --stat
git diff --name-only
stat -c '%y %n' $(git diff --name-only) 2>/dev/null

# 5. Recent commit ownership in this checkout (who has been committing here lately?).
git log --format='%h %an <%ae> %ar %s' -5

# 6. Any lock files suggesting an in-progress git operation by another session?
ls .git/index.lock .git/HEAD.lock 2>/dev/null && echo "LOCK PRESENT - stop"
```

## Decision gate

After running the checklist, decide:

1. **Clean tree, expected branch, no other live session** — proceed with the
   edit.
2. **Dirty tree with changes you did not make** — do not overwrite, stash,
   reset, or commit them. Another session may be mid-edit. Stop and confirm
   ownership, or move your work to a separate worktree.
3. **Unexpected branch, unexpected worktree path, or lock files present** —
   stop. Do not "fix" the state; another session may own it.
4. **Any doubt about exclusive ownership** — use a separate worktree:

   ```bash
   git worktree add ../<repo>-<task> -b <task-branch>
   ```

   Work there instead. This keeps your changes isolated from any other live
   session in the original checkout.

## Prohibited on a shared tree

Without confirmed exclusive ownership, never run:

- `git reset --hard`, `git checkout -- <path>`, `git restore`, or
  `git clean` — these destroy someone else's uncommitted work.
- `git stash` of changes you did not create — it hides another session's
  work.
- `git commit -a` or staging files you did not modify — it sweeps up
  another session's changes into your commit.
- Branch switches (`git checkout <branch>`, `git switch`) in a dirty tree.

## Creating a separate worktree (safe path)

When the gate says the tree may be shared, create your own worktree from the
desired base:

```bash
git fetch origin
git worktree add ../<repo>-<task> -b <task-branch> origin/<base-branch>
cd ../<repo>-<task>
```

When finished and merged, clean up:

```bash
git worktree remove ../<repo>-<task>
```
