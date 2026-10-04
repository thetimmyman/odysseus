"""In-browser working-tree review and commit for the caller's session project.

Security:
  * Every git call runs with cwd = the caller's own session project_root, which
    must be the toplevel of its git work tree (no parent or nested repos).
  * Never shell=True; only allowlisted subcommands, with paths and the commit
    message passed after `--` or via `-F`, never as flags.
  * Admin-only and owner-scoped.
  * Diff and file-count caps bound the response size.
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

# Hard ceilings so a diff never blows up the response or model context.
MAX_DIFF_BYTES = int(os.getenv("GIT_REVIEW_MAX_DIFF_BYTES", str(512 * 1024)))   # 512 KB
MAX_STATUS_ENTRIES = int(os.getenv("GIT_REVIEW_MAX_STATUS", "2000"))
MAX_COMMIT_MSG_LEN = int(os.getenv("GIT_REVIEW_MAX_MSG", "8000"))
GIT_TIMEOUT_S = int(os.getenv("GIT_REVIEW_TIMEOUT_S", "30"))

# The only git subcommands this router will exec.
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
    """Reject non-admin callers; git mutation is as powerful as shell exec."""
    auth_manager = getattr(request.app.state, "auth_manager", None)
    if not auth_manager:
        # No auth configured: trusted localhost dev only.
        return
    user = getattr(request.state, "current_user", None)
    # Middleware validated the internal token + loopback before setting this.
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
    """Return the realpath of the caller's own session project_root, or raise."""
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
    """Return the git toplevel for `root`, requiring it to equal `root` so a
    session can't reach into a parent or nested repo."""
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
    """Run allowlisted `git <args>` in `cwd` with a fixed identity and no ambient
    config, so clients can't inject `-c`, env or config side effects."""
    if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
        raise HTTPException(400, "Unsupported git operation")

    # GIT_CONFIG_GLOBAL/SYSTEM=/dev/null and a pinned HOME neutralise on-disk
    # config (aliases, core.hooksPath, ~/.gitconfig).
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
    """Resolve a client path to a repo-relative path confined to `root` (and not
    `root` itself). The result is only ever placed after `--`."""
    if raw_path is None or not str(raw_path).strip():
        raise HTTPException(400, "path is required")
    raw = str(raw_path).strip()
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
    # A leading '-' is argv-ambiguous even after `--`; reject it outright.
    if rel.startswith("-") or rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        raise HTTPException(403, "invalid path")
    return rel


def _parse_status(porcelain: str, limit: int):
    """Parse `git status --porcelain=v1 -z` into (entries, truncated)."""
    entries = []
    truncated = False
    # A rename/copy record is followed by its origin path, which is skipped.
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

    # Sync `def`: subprocess.run blocks, so FastAPI runs this in a threadpool.

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

        # Best-effort; never fatal.
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

        # Paths go after `--` so they can never be parsed as flags.
        args = ["diff", "--no-color", "--no-ext-diff"]
        if staged:
            args.append("--cached")
        args += ["--", rel]
        res = _run_git(root, args)
        # Without --exit-code, non-zero means a real error (e.g. bad path).
        if res.returncode != 0:
            raise HTTPException(400, f"git diff failed: {_decode(res.stderr)[:500]}")
        raw = res.stdout or b""
        truncated = False
        if len(raw) > MAX_DIFF_BYTES:
            raw = raw[:MAX_DIFF_BYTES]
            truncated = True
        diff_text = _decode(raw)

        # Untracked file with no staged diff: show its content as all-added.
        is_untracked = False
        if not staged and not diff_text.strip():
            lf = _run_git(root, ["ls-files", "--others", "--exclude-standard", "-z", "--", rel])
            if lf.returncode == 0 and _decode(lf.stdout).strip("\x00"):
                is_untracked = True
                nd = _run_git(root, ["diff", "--no-color", "--no-ext-diff", "--no-index", "--", os.devnull, rel])
                # --no-index exits 1 when files differ.
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
        # Unstages only; never touches the working tree.
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

        # Only the staged index is committed (no -a); refuse empty commits clearly.
        diffidx = _run_git(root, ["diff", "--cached", "--name-only", "-z"])
        if diffidx.returncode == 0 and not _decode(diffidx.stdout).strip("\x00"):
            raise HTTPException(400, "Nothing staged to commit")

        # Message via a temp file with `-F`, never on the argv.
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

        sha = None
        rp = _run_git(root, ["rev-parse", "--short", "HEAD"])
        if rp.returncode == 0:
            sha = _decode(rp.stdout).strip() or None
        return {"ok": True, "commit": sha, "output": _decode(res.stdout)[:1000]}

    return router
