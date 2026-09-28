"""Temporary git worktree lifecycle: create, `git apply` patches, revert, jailed removal.

Hard rule: nothing here may run commit, merge, push or `git am`; promoting a
patch is a human action.

data_root() is the harness's single data-dir resolver; it reads
ODYSSEUS_DATA_DIR at call time."""
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
    """ODYSSEUS_DATA_DIR (read per call), else <repo-root>/data."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    if override:
        return os.path.realpath(override)
    return os.path.join(_REPO_ROOT, "data")


def worktrees_root() -> str:
    """The jail all temp worktrees live under (and must never escape)."""
    return os.path.join(data_root(), "routing", "worktrees")


def _git(args, timeout: int = _GIT_TIMEOUT):
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def create_worktree(repo_path: str, run_id: str, base_ref: str = "HEAD",
                    allow_dirty: bool = False) -> str:
    """Create a detached temp worktree for `run_id` at `base_ref`.

    The source repo must be clean unless allow_dirty=True: local edits would
    make the verification result meaningless."""
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
    """`git apply --check`, then apply; never commits.

    The patch file lives outside the worktree so it can't leak into the tree
    the sandbox executes."""
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
    """Restore tracked files and delete untracked ones, so the next attempt starts pristine."""
    out = _git(["-C", worktree_path, "checkout", "--", "."])
    if out.returncode != 0:
        raise RuntimeError(f"git checkout -- . failed: {out.stderr.strip()[:1000]}")
    out = _git(["-C", worktree_path, "clean", "-fd"])
    if out.returncode != 0:
        raise RuntimeError(f"git clean -fd failed: {out.stderr.strip()[:1000]}")


def remove_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove a temp worktree; the path must be jailed under worktrees_root(),
    since `git worktree remove --force` deletes it."""
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
