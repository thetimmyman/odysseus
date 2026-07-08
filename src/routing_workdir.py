"""src/routing_workdir.py — Phase 3 "Safe Execution" worktree lifecycle (spec
Section 15): per-task temporary git worktrees under the harness data root,
patch application via `git apply` (always `--check` first, never `git am`),
the failed-patch revert used between attempts, and jailed worktree removal.

HARD RULE: no function in this module may ever run `git commit`, `git merge`,
or `git push` (nor `git am`, which creates commits) -- spec Section 15's
"NEVER auto-commit / auto-merge / push" is enforced by construction: the only
git subcommands invoked here are rev-parse / status / worktree / apply /
checkout / clean / prune. Promoting an applied patch into a real branch or
commit is a HUMAN action, outside this harness.

data_root() is the single place the harness resolves its data directory. It
honors an ODYSSEUS_DATA_DIR env override -- read at CALL time, not import
time, so host CLIs on the Framework can target /mnt/framework-data/
odysseus-data instead of the checkout's ./data, and tests can monkeypatch the
env -- falling back to the same repo-root "data" dir routing_executor's
ARCHIVE_ROOT historically hardcoded. routing_executor imports it from here so
both resolve identically."""
import os
import re
import subprocess
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# uuid-ish charset only: a run_id becomes a path component under the worktree
# jail, so it must never carry separators, dots, or anything shell-ish.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_GIT_TIMEOUT = 60


def data_root() -> str:
    """The harness data directory: ODYSSEUS_DATA_DIR when set (resolved per
    call so a monkeypatched/exported env is honored without re-import), else
    <repo-root>/data -- the same fallback routing_executor always used."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    if override:
        return os.path.realpath(override)
    return os.path.join(_REPO_ROOT, "data")


def worktrees_root() -> str:
    """The jail all temp worktrees live under (and must never escape)."""
    return os.path.join(data_root(), "routing", "worktrees")


def _git(args, timeout: int = _GIT_TIMEOUT):
    """Run git with an argv list (no shell), captured output, bounded time."""
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def create_worktree(repo_path: str, run_id: str, base_ref: str = "HEAD",
                    allow_dirty: bool = False) -> str:
    """Create a detached temp worktree for `run_id` at `base_ref` under
    worktrees_root() and return its path.

    Spec Section 15 clean-worktree requirement: the SOURCE repo must have an
    empty `git status --porcelain` unless explicitly waived via
    allow_dirty=True (plumbed from the CLIs' --allow-dirty flag) -- applying
    model patches on top of un-snapshotted local edits makes the verification
    result meaningless and risks masking whose change broke what."""
    if not run_id or not _RUN_ID_RE.match(run_id):
        raise ValueError(
            f"invalid run_id {run_id!r}: must match {_RUN_ID_RE.pattern} "
            "(it becomes a path component under the worktree jail)"
        )
    check = _git(["-C", repo_path, "rev-parse", "--is-inside-work-tree"])
    if check.returncode != 0 or check.stdout.strip() != "true":
        raise ValueError(f"{repo_path!r} is not a git repository: {check.stderr.strip()[:500]}")

    if not allow_dirty:
        status = _git(["-C", repo_path, "status", "--porcelain"])
        if status.returncode != 0:
            raise RuntimeError(f"git status failed in {repo_path!r}: {status.stderr.strip()[:500]}")
        if status.stdout.strip():
            raise RuntimeError(
                f"source repo {repo_path!r} has uncommitted changes; commit/stash them "
                "or explicitly waive the clean-worktree requirement with --allow-dirty"
            )

    target = os.path.join(worktrees_root(), run_id)
    if os.path.exists(target):
        raise RuntimeError(f"worktree path {target!r} already exists; refusing to reuse it")
    os.makedirs(worktrees_root(), exist_ok=True)

    out = _git(["-C", repo_path, "worktree", "add", "--detach", target, base_ref])
    if out.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {out.stderr.strip()[:1000]}")
    return target


def apply_patch(worktree_path: str, patch_text: str) -> dict:
    """Apply a unified diff to the worktree with `git apply` (a `--check`
    dry-run first, then the real apply -- never `git am`, no commit of any
    kind). Returns {"applied": bool, "error": str|None, "changed_files":
    [...]} where changed_files comes from `git status --porcelain`.

    The patch is written to a tempfile OUTSIDE the worktree -- writing it
    inside (e.g. <worktree>/.routing-patch.diff) could collide with real repo
    content, show up in `git status`, and leak into the very tree the sandbox
    is about to execute."""
    if not patch_text or not patch_text.strip():
        return {"applied": False, "error": "empty patch text", "changed_files": []}
    if not patch_text.endswith("\n"):
        patch_text += "\n"  # git apply rejects a diff missing its final newline

    fd, patch_file = tempfile.mkstemp(prefix="routing-patch-", suffix=".diff")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(patch_text)

        check = _git(["-C", worktree_path, "apply", "--check", patch_file])
        if check.returncode != 0:
            return {
                "applied": False,
                "error": f"git apply --check failed: {check.stderr.strip()[:2000]}",
                "changed_files": [],
            }
        out = _git(["-C", worktree_path, "apply", patch_file])
        if out.returncode != 0:
            return {
                "applied": False,
                "error": f"git apply failed after --check passed: {out.stderr.strip()[:2000]}",
                "changed_files": [],
            }
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass

    status = _git(["-C", worktree_path, "status", "--porcelain"])
    changed_files = []
    for line in status.stdout.splitlines():
        if len(line) <= 3:
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename: report the destination path
            path = path.split(" -> ", 1)[1]
        changed_files.append(path.strip('"'))
    return {"applied": True, "error": None, "changed_files": changed_files}


def revert_worktree(worktree_path: str) -> None:
    """The failed-patch reset between attempts (spec Section 15: "failed
    patches reverted before the next attempt"): restore all tracked files and
    delete anything untracked the patch (or a sandboxed command) created, so
    the next attempt starts from a pristine base_ref tree."""
    out = _git(["-C", worktree_path, "checkout", "--", "."])
    if out.returncode != 0:
        raise RuntimeError(f"git checkout -- . failed: {out.stderr.strip()[:1000]}")
    out = _git(["-C", worktree_path, "clean", "-fd"])
    if out.returncode != 0:
        raise RuntimeError(f"git clean -fd failed: {out.stderr.strip()[:1000]}")


def remove_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove a temp worktree and prune stale registrations. Jail check
    first: the realpath of `worktree_path` must live strictly under
    worktrees_root() (realpath+commonpath, the same convention as
    routing_context.safe_repo_path) -- `git worktree remove --force` deletes
    the directory, so an unjailed path here would be an arbitrary-delete."""
    jail = os.path.realpath(worktrees_root())
    candidate = os.path.realpath(worktree_path)
    if candidate == jail or os.path.commonpath([jail, candidate]) != jail:
        raise ValueError(
            f"refusing to remove {worktree_path!r}: resolves outside the "
            f"worktree jail {jail!r}"
        )
    out = _git(["-C", repo_path, "worktree", "remove", "--force", candidate])
    if out.returncode != 0:
        raise RuntimeError(f"git worktree remove failed: {out.stderr.strip()[:1000]}")
    # Best-effort tidy of any leftover admin entries; failure is harmless.
    _git(["-C", repo_path, "worktree", "prune"])
