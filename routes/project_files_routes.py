import os
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from src.auth_helpers import effective_user
from routes.session_routes import _verify_session_owner
from src.tool_execution import (
    _get_session_project_root,
    _resolve_tool_path,
    _is_sensitive_path,
)

logger = logging.getLogger(__name__)

MAX_READ_BYTES = int(os.getenv("PROJECT_FILES_MAX_BYTES", str(2 * 1024 * 1024)))  # 2 MB
MAX_ENTRIES = int(os.getenv("PROJECT_FILES_MAX_ENTRIES", "2000"))


class _WriteBody(BaseModel):
    session_id: Optional[str] = None
    path: str
    content: str


def _confined(request: Request, session_id: Optional[str], raw_path=None):
    """Return (owner, root, target_realpath). target == root when raw_path is None.

    Owner-checked (effective_user, bearer-aware) + cross-owner 404
    (_verify_session_owner) + root-confined via the agent's own _resolve_tool_path.
    """
    if not session_id or not str(session_id).strip():
        raise HTTPException(400, "session_id is required")
    owner = effective_user(request)                          # bearer-aware owner
    _verify_session_owner(request, session_id)               # 404 cross-owner (DB + ghost)
    root = _get_session_project_root(session_id, owner)      # None on missing/cross-owner/not-dir
    if not root:
        raise HTTPException(404, "No project root set for this session")
    if raw_path is None:
        return owner, root, root
    try:
        resolved = _resolve_tool_path(raw_path, session_id, owner)  # raises ValueError
    except ValueError as e:
        raise HTTPException(403, str(e))
    return owner, root, resolved


def setup_project_files_routes():
    router = APIRouter(prefix="/api/project-files", tags=["project-files"])

    # NOTE: `def` (sync), not `async def` — os.scandir / file IO is blocking and
    # FastAPI offloads sync routes to a threadpool (root may be an NFS mount).
    @router.get("/tree")
    def project_files_tree(
        request: Request,
        session_id: Optional[str] = Query(None),
        path: Optional[str] = Query(None),
    ):
        owner, root, target = _confined(request, session_id, path)
        if not os.path.isdir(target):
            raise HTTPException(400, "Not a directory")
        entries, truncated = [], False
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    name = entry.name
                    if name.startswith("."):          # hide dotfiles by default
                        continue
                    if name.endswith(".odytmp"):       # hide our atomic-write temp files
                        continue
                    abspath = os.path.realpath(entry.path)
                    if _is_sensitive_path(abspath):    # .ssh/.env/etc never listed
                        continue
                    # symlink-escape guard: drop children that resolve outside root
                    try:
                        _resolve_tool_path(abspath, session_id, owner)
                    except ValueError:
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    try:
                        size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    entries.append({"name": name, "path": abspath,
                                    "is_dir": is_dir, "size": size})
        except OSError as e:
            raise HTTPException(400, f"Cannot list directory: {e}")
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = None if target == root else os.path.dirname(target)
        return {"root": root, "dir": target, "parent": parent,
                "entries": entries, "truncated": truncated}

    @router.get("/read")
    def project_files_read(
        request: Request,
        session_id: Optional[str] = Query(None),
        path: str = Query(...),
    ):
        _, _, resolved = _confined(request, session_id, path)
        if _is_sensitive_path(resolved):              # never read secrets (.env*, .ssh, app DBs…)
            raise HTTPException(403, "Refusing to read a sensitive file")
        if os.path.isdir(resolved):
            raise HTTPException(400, "Path is a directory")
        try:
            # Single open + fstat + bounded read: the fd pins one inode, so a
            # concurrent /write os.replace() can't swap the file under us (no
            # TOCTOU), and we never read more than MAX_READ_BYTES.
            with open(resolved, "rb") as f:
                if os.fstat(f.fileno()).st_size > MAX_READ_BYTES:
                    raise HTTPException(413, "File too large to open in the editor")
                raw = f.read(MAX_READ_BYTES + 1)
            if len(raw) > MAX_READ_BYTES:
                raise HTTPException(413, "File too large to open in the editor")
            if b"\x00" in raw[:8192]:                  # binary sniff on bytes actually read
                raise HTTPException(415, "Binary file — not editable as text")
            content = raw.decode("utf-8", errors="replace")
        except HTTPException:
            raise
        except OSError as e:
            raise HTTPException(400, f"Cannot read file: {e}")
        return {"path": resolved, "name": os.path.basename(resolved),
                "content": content, "size": len(content)}

    @router.post("/write")
    def project_files_write(request: Request, body: _WriteBody):
        _, _, resolved = _confined(request, body.session_id, body.path)
        if os.path.isdir(resolved):
            raise HTTPException(400, "Path is a directory")
        if _is_sensitive_path(resolved):              # defense-in-depth
            raise HTTPException(403, "Refusing to write a sensitive file")
        if len(body.content.encode("utf-8")) > MAX_READ_BYTES:
            raise HTTPException(413, "Content too large")
        tmp = resolved + ".odytmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body.content)
            os.replace(tmp, resolved)                 # atomic; no partial truncation
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise HTTPException(400, f"Cannot write file: {e}")
        return {"path": resolved, "size": len(body.content), "ok": True}

    return router
