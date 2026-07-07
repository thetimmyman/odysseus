"""Git review routes — in-browser working-tree review + commit.

Roadmap Tier 2 #8. Lets a "vibe coder" review the agent's working-tree changes
in their session's project and commit them, all from the Odysseus UI.

SECURITY (multi-user, network-exposed app):
  * EVERY operation runs with cwd = the CALLER'S OWN session project_root,
    resolved via _get_session_project_root(session_id, owner). Refuse if unset.
    A session can NEVER touch another user's project or any path outside it.
  * The project_root must be inside a git work tree AND the work-tree toplevel
    must equal project_root (we refuse a parent-repo / nested-repo mismatch so
    one session can't operate on a repo that isn't its own project).
  * NEVER shell=True. We build an argv with an ALLOWLIST of git subcommands
    (status, diff, add, reset, commit, rev-parse, ls-files). No arbitrary git
    passthrough, no -c / --exec-path / -C / user-controlled flags. File paths
    and the commit message are passed as positional/`-F` argv elements after a
    `--` separator, never interpolated into a shell.
  * require_admin (these are powerful) AND owner-scope. The panel only ever
    shows the caller's own project.
  * Output caps (diff bytes, file count) so a huge diff can't blow up the
    response or the model context.

This module mirrors routes/project_files_routes.py for the owner +
project_root confinement and routes/shell_routes.py for the admin gate.
"""

import os
import logging
import subprocess
import tempfile
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from routes.session_routes import _verify_session_owner
from src.tool_execution import _get_session_project_root

logger = logging.getLogger(__name__)

# --- output caps ------------------------------------------------------------
# A diff/status response must never be able to blow up the HTTP response or the
# model context. These are generous for human review but hard ceilings.
MAX_DIFF_BYTES = int(os.getenv("GIT_REVIEW_MAX_DIFF_BYTES", str(512 * 1024)))   # 512 KB
MAX_STATUS_ENTRIES = int(os.getenv("GIT_REVIEW_MAX_STATUS", "2000"))
MAX_COMMIT_MSG_LEN = int(os.getenv("GIT_REVIEW_MAX_MSG", "8000"))
GIT_TIMEOUT_S = int(os.getenv("GIT_REVIEW_TIMEOUT_S", "30"))

# The ONLY git subcommands this router will ever exec. Anything else is a 400
# before we ever build an argv. No passthrough, no plumbing-by-name.
_ALLOWED_SUBCOMMANDS = {
    "status", "diff", "add", "reset", "commit", "rev-parse", "ls-files",
}


class _PathBody(BaseModel):
    session_id: Optional[str] = None
    path: str


class _CommitBody(BaseModel):
    session_id: Optional[str] = None
    message: str


def _require_admin(request: Request):
    """Reject non-admin callers. Git mutation (add/reset/commit) is powerful;
    keep it admin-only exactly like shell exec. Mirrors shell_routes._require_admin."""
    auth_manager = getattr(request.app.state, "auth_manager", None)
    if not auth_manager:
        # No auth configured at all — only the fully-trusted localhost dev case.
        return
    user = getattr(request.state, "current_user", None)
    # In-process tool loopback: middleware already validated the internal token
    # + loopback client before setting this marker, so honour it as admin.
    if user == "internal-tool":
        return
    if not user or user == "api":
        raise HTTPException(403, "Admin only")
    if not auth_manager.is_admin(user):
        raise HTTPException(403, "Admin only")


def _reject_cross_site(request: Request):
    """Reject browser cross-site navigations to git-mutating endpoints."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request rejected")


def _owner_root(request: Request, session_id: Optional[str]) -> str:
    """Return the realpath of the caller's OWN session project_root, or raise.

    Owner-checked (effective_user is enforced inside _get_session_project_root's
    owner compare) + cross-owner 404 (_verify_session_owner). This is the single
    cwd for every git call in this module."""
    if not session_id or not str(session_id).strip():
        raise HTTPException(400, "session_id is required")
    from src.auth_helpers import effective_user
    owner = effective_user(request)                     # bearer-aware owner
    _verify_session_owner(request, session_id)           # 404 cross-owner (DB + ghost)
    root = _get_session_project_root(session_id, owner)  # None on missing/cross-owner/not-dir
    if not root:
        raise HTTPException(404, "No project root set for this session")
    return root


def _git_toplevel(root: str) -> str:
    """Return the git work-tree toplevel for `root`, confirming `root` is the
    repo's own toplevel. Raises HTTPException otherwise.

    We require toplevel == root so a session whose project_root is a *subdir*
    of (or a *parent* of) some other repo can't reach in and operate on it.
    """
    res = _run_git(root, ["rev-parse", "--show-toplevel"])
    if res.returncode != 0:
        raise HTTPException(400, "Project root is not inside a git work tree")
    toplevel = os.path.realpath(_decode(res.stdout).strip())
    if toplevel != os.path.realpath(root):
        raise HTTPException(
            400,
            "Project root is not the git work-tree root "
            "(refusing to operate on a parent/nested repo)",
        )
    return toplevel


def _run_git(cwd: str, args: list, *, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    """Run `git <args>` with cwd=`cwd`. ALLOWLIST-gated, never shell=True.

    `args[0]` must be in _ALLOWED_SUBCOMMANDS. We hard-disable any ambient git
    config and force a fixed, non-client-controlled author/committer identity so
    a client can never inject `-c`, env, or alternate-config side effects. The
    argv is fully explicit; nothing here is shell-parsed."""
    if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
        raise HTTPException(400, "Unsupported git operation")

    # Fixed identity + a hardened, client-uncontrollable environment. We do NOT
    # forward arbitrary env; GIT_CONFIG_GLOBAL/SYSTEM=/dev/null neutralises any
    # on-disk config (aliases, hooks-via-core.hooksPath, etc.). HOME is pinned
    # so a stray ~/.gitconfig can't change behaviour.
    env = {
        "PATH": os.getenv("PATH", "/usr/bin:/bin"),
        "HOME": os.getenv("HOME", "/app"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Odysseus",
        "GIT_AUTHOR_EMAIL": "odysseus@localhost",
        "GIT_COMMITTER_NAME": "Odysseus",
        "GIT_COMMITTER_EMAIL": "odysseus@localhost",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    argv = ["git"] + list(args)
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_S,
            check=False,
            text=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "git timed out")
    except OSError as e:
        raise HTTPException(500, f"git failed to start: {e}")


def _decode(b: bytes) -> str:
    return (b or b"").decode("utf-8", errors="replace")


def _rel_in_root(root: str, raw_path: str) -> str:
    """Resolve a client-supplied path to a repo-relative path, confined to root.

    The path is treated as data, never a flag: it is returned as a plain string
    placed AFTER a `--` separator in the argv. We still hard-confine it inside
    `root` (realpath containment, like project_files) so a client can't name a
    file outside the repo, and reject anything that resolves to root itself."""
    if raw_path is None or not str(raw_path).strip():
        raise HTTPException(400, "path is required")
    raw = str(raw_path).strip()
    # Absolute or relative — resolve against root, then confirm containment.
    base = raw if os.path.isabs(raw) else os.path.join(root, raw)
    resolved = os.path.realpath(base)
    root_real = os.path.realpath(root)
    if resolved == root_real:
        raise HTTPException(400, "path must be a file inside the project, not the root")
    try:
        common = os.path.commonpath([resolved, root_real])
    except ValueError:
        raise HTTPException(403, "path is outside the project root")
    if common != root_real:
        raise HTTPException(403, "path is outside the project root")
    rel = os.path.relpath(resolved, root_real)
    # Defense-in-depth: a leading '-' would be argv-ambiguous; we already place
    # paths after `--`, but reject the pathological case outright.
    if rel.startswith("-") or rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        raise HTTPException(403, "invalid path")
    return rel


def _parse_status(porcelain: str, limit: int):
    """Parse `git status --porcelain=v1 -z` output into structured entries.

    Returns (entries, truncated). Each entry: {path, x, y, staged, unstaged,
    untracked}. The -z form is NUL-separated and quoting-free, so filenames
    with spaces/newlines/unicode are safe."""
    entries = []
    truncated = False
    # NUL-separated records. A rename/copy record (R/C) is followed by an extra
    # NUL-terminated field (the origin path) which we consume but don't surface
    # as its own entry.
    tokens = porcelain.split("\x00")
    i = 0
    n = len(tokens)
    while i < n:
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        if len(rec) < 3:
            continue
        x, y = rec[0], rec[1]
        path = rec[3:]
        # Rename/copy: the next token is the source path; skip it.
        if x in ("R", "C") or y in ("R", "C"):
            if i < n:
                i += 1
        if len(entries) >= limit:
            truncated = True
            break
        untracked = (x == "?" and y == "?")
        entries.append({
            "path": path,
            "x": x,
            "y": y,
            "staged": (not untracked) and x not in (" ", "?"),
            "unstaged": untracked or y not in (" ",),
            "untracked": untracked,
        })
    return entries, truncated


def setup_git_routes():
    router = APIRouter(prefix="/api/git", tags=["git"])

    # NOTE: sync `def` (not async) — subprocess.run is blocking and FastAPI
    # offloads sync routes to a threadpool (root may be an NFS mount). This
    # mirrors project_files_routes.

    @router.get("/status")
    def git_status(
        request: Request,
        session_id: Optional[str] = Query(None),
    ):
        _require_admin(request)
        root = _owner_root(request, session_id)
        _git_toplevel(root)
        res = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if res.returncode != 0:
            raise HTTPException(400, f"git status failed: {_decode(res.stderr)[:500]}")
        entries, truncated = _parse_status(_decode(res.stdout), MAX_STATUS_ENTRIES)

        # Current branch (best-effort; never fatal). rev-parse is on the allowlist.
        branch = None
        br = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if br.returncode == 0:
            branch = _decode(br.stdout).strip() or None

        return {
            "root": root,
            "branch": branch,
            "entries": entries,
            "truncated": truncated,
            "clean": len(entries) == 0,
        }

    @router.get("/diff")
    def git_diff(
        request: Request,
        session_id: Optional[str] = Query(None),
        path: str = Query(...),
        staged: bool = Query(False),
    ):
        _require_admin(request)
        root = _owner_root(request, session_id)
        _git_toplevel(root)
        rel = _rel_in_root(root, path)

        # Working-tree diff (default) or staged diff (--cached). Paths go AFTER
        # `--` so they can never be parsed as flags. No color, no ext-diff,
        # no pager.
        args = ["diff", "--no-color", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        args += ["--", rel]
        res = _run_git(root, args)
        # Plain `git diff` (no --exit-code) returns 0 even when there are
        # changes; a non-zero code here is a real error (e.g. bad path).
        if res.returncode != 0:
            raise HTTPException(400, f"git diff failed: {_decode(res.stderr)[:500]}")
        raw = res.stdout or b""
        truncated = False
        if len(raw) > MAX_DIFF_BYTES:
            raw = raw[:MAX_DIFF_BYTES]
            truncated = True
        diff_text = _decode(raw)

        # Untracked file with no staged diff → show its content as an all-added
        # diff so the reviewer sees something. ls-files is on the allowlist.
        is_untracked = False
        if not staged and not diff_text.strip():
            lf = _run_git(root, ["ls-files", "--others", "--exclude-standard", "-z", "--", rel])
            if lf.returncode == 0 and _decode(lf.stdout).strip("\x00"):
                is_untracked = True
                # Render via `diff --no-index /dev/null <file>` — still git, but
                # `diff` is allowlisted and `--no-index` makes it not require a repo.
                nd = _run_git(root, ["diff", "--no-color", "--no-ext-diff", "--no-index", "--", os.devnull, rel])
                # --no-index exits 1 when files differ; that's expected.
                ndraw = nd.stdout or b""
                if len(ndraw) > MAX_DIFF_BYTES:
                    ndraw = ndraw[:MAX_DIFF_BYTES]
                    truncated = True
                diff_text = _decode(ndraw)

        return {
            "path": rel,
            "staged": staged,
            "untracked": is_untracked,
            "diff": diff_text,
            "truncated": truncated,
            "empty": not diff_text.strip(),
        }

    @router.post("/stage")
    def git_stage(request: Request, body: _PathBody):
        _require_admin(request)
        _reject_cross_site(request)
        root = _owner_root(request, body.session_id)
        _git_toplevel(root)
        rel = _rel_in_root(root, body.path)
        # `add --` stages tracked modifications AND untracked files; path is data.
        res = _run_git(root, ["add", "--", rel])
        if res.returncode != 0:
            raise HTTPException(400, f"git add failed: {_decode(res.stderr)[:500]}")
        return {"path": rel, "staged": True, "ok": True}

    @router.post("/unstage")
    def git_unstage(request: Request, body: _PathBody):
        _require_admin(request)
        _reject_cross_site(request)
        root = _owner_root(request, body.session_id)
        _git_toplevel(root)
        rel = _rel_in_root(root, body.path)
        # `reset -- <path>` unstages; it does NOT touch the working tree, so a
        # misclick can never destroy the user's edits.
        res = _run_git(root, ["reset", "--quiet", "--", rel])
        if res.returncode != 0:
            raise HTTPException(400, f"git reset failed: {_decode(res.stderr)[:500]}")
        return {"path": rel, "staged": False, "ok": True}

    @router.post("/commit")
    def git_commit(request: Request, body: _CommitBody):
        _require_admin(request)
        _reject_cross_site(request)
        root = _owner_root(request, body.session_id)
        _git_toplevel(root)

        msg = (body.message or "").strip()
        if not msg:
            raise HTTPException(400, "commit message is required")
        if len(msg) > MAX_COMMIT_MSG_LEN:
            raise HTTPException(413, "commit message too long")

        # Refuse an empty commit early with a clear message (rather than a raw
        # git error) — only the staged index is committed (no -a).
        diffidx = _run_git(root, ["diff", "--cached", "--name-only", "-z"])
        if diffidx.returncode == 0 and not _decode(diffidx.stdout).strip("\x00"):
            raise HTTPException(400, "Nothing staged to commit")

        # Message via a temp FILE read with `-F` — never on the argv, never
        # shell-interpolated. `--cleanup=whitespace` keeps the user's text as-is
        # (minus trailing whitespace). The identity is fixed in _run_git's env.
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="ody-commit-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(msg)
            res = _run_git(root, ["commit", "--cleanup=whitespace", "-F", tmp])
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if res.returncode != 0:
            raise HTTPException(400, f"git commit failed: {_decode(res.stderr)[:500]}")

        # Surface the new commit's short hash + subject for the UI toast.
        sha = None
        rp = _run_git(root, ["rev-parse", "--short", "HEAD"])
        if rp.returncode == 0:
            sha = _decode(rp.stdout).strip() or None
        return {"ok": True, "commit": sha, "output": _decode(res.stdout)[:1000]}

    return router
