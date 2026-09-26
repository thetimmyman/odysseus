"""Interactive PTY shell over a WebSocket: remote code execution by design, so
every control below is load-bearing.

  1. Auth on the handshake: HTTP auth middleware doesn't run for WebSocket
     scopes, so cookie auth + admin are checked here before accept(); failures
     close with a policy-violation code and never accept.
  2. Origin allowlist: WebSockets bypass same-origin and carry cookies, so the
     Origin is checked before accept() to stop cross-site WebSocket hijacking.
  3. Non-root shell: forkpty inherits the unprivileged app uid; never setuid.
     argv-only spawn, cwd is the caller's session project_root or a safe default.
  4. Admin-only, owner-scoped registry keyed by (owner, session_id), with the
     owner re-checked on every input/resize/attach.
  5. Fixed shell argv, clamped resize, idle timeout, max lifetime and a PTY cap;
     child killed and fd closed on disconnect/timeout/error.
"""

import asyncio
import json
import logging
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Optional

# pty/fcntl/termios are POSIX-only; on other hosts the endpoint refuses cleanly
# instead of breaking app import.
try:
    import fcntl
    import pty
    import signal
    import termios
except ImportError as exc:  # pragma: no cover - Windows
    fcntl = None
    pty = None
    signal = None
    termios = None
    _PTY_IMPORT_ERROR = exc
else:
    _PTY_IMPORT_ERROR = None

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

PTY_SUPPORTED = (
    pty is not None and fcntl is not None and termios is not None
    and hasattr(os, "forkpty") and hasattr(os, "setsid")
)

# Hard ceilings so a session can't pin a core forever or fork-bomb the registry.
IDLE_TIMEOUT_S = int(os.getenv("TERMINAL_IDLE_TIMEOUT_S", str(30 * 60)))      # 30 min no I/O
MAX_LIFETIME_S = int(os.getenv("TERMINAL_MAX_LIFETIME_S", str(8 * 60 * 60)))  # 8 h absolute
MAX_CONCURRENT_PTYS = int(os.getenv("TERMINAL_MAX_PTYS", "6"))               # across all owners
READ_CHUNK = 65536
# Bound TIOCSWINSZ dimensions: no huge grids (memory) or 0x0 (div-by-zero).
MIN_COLS, MAX_COLS = 2, 500
MIN_ROWS, MAX_ROWS = 1, 300
DEFAULT_COLS, DEFAULT_ROWS = 80, 24

WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011
WS_TRY_AGAIN_LATER = 1013

SESSION_COOKIE = "odysseus_session"


def _resolve_ws_user(websocket: WebSocket) -> Optional[str]:
    """Return the admin-eligible username for a WebSocket handshake, or None.

    AuthMiddleware doesn't run on the WebSocket path, so validate the session
    cookie here. Bearer tokens map to the non-admin "api" user and are ignored.
    """
    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    # No auth configured: trusted localhost dev only.
    if auth_manager is None:
        return None
    token = websocket.cookies.get(SESSION_COOKIE)
    if not auth_manager.validate_token(token):
        return None
    return auth_manager.get_username_for_token(token)


def _ws_is_admin(websocket: WebSocket, user: Optional[str]) -> bool:
    """True only for a configured admin; "api" and "internal-tool" never qualify."""
    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    if auth_manager is None:
        # Auth not configured: localhost single-user dev.
        return True
    if not user or user in ("api", "internal-tool"):
        return False
    return bool(auth_manager.is_admin(user))


def _allowed_origins() -> set[str]:
    """ALLOWED_ORIGINS (shared with CORS), normalised: lowercase, no trailing slash."""
    raw = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1")
    out: set[str] = set()
    for o in raw.split(","):
        o = o.strip().rstrip("/").lower()
        if o:
            out.add(o)
    return out


def _origin_ok(websocket: WebSocket) -> bool:
    """Validate the handshake Origin against the allowlist.

    A missing Origin is allowed only from loopback: browsers always send one on
    a WebSocket handshake, so a cross-site hijack never lacks it."""
    origin = websocket.headers.get("origin")
    if origin is None:
        client = websocket.client
        host = (client.host if client else "") or ""
        return host in ("127.0.0.1", "::1", "localhost")
    return origin.strip().rstrip("/").lower() in _allowed_origins()


def _registry(websocket: WebSocket) -> dict:
    """Process-wide PTY registry in app.state, keyed by (owner, session_id)."""
    st = websocket.app.state
    reg = getattr(st, "terminal_ptys", None)
    if reg is None:
        reg = {}
        st.terminal_ptys = reg
    return reg


def _resolve_cwd(websocket: WebSocket, owner: Optional[str], session_id: Optional[str]) -> str:
    """Starting cwd: the caller's own session project_root, else /app/work, else HOME."""
    if session_id:
        try:
            from src.tool_execution import _get_session_project_root
            root = _get_session_project_root(session_id, owner)
            if root and os.path.isdir(root):
                return root
        except Exception:
            pass
    for candidate in ("/app/work", os.path.expanduser("~")):
        try:
            if candidate and os.path.isdir(candidate):
                return candidate
        except OSError:
            continue
    return "/"


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Apply TIOCSWINSZ with dimensions clamped to sane bounds."""
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
    # struct winsize { ws_row, ws_col, ws_xpixel, ws_ypixel } — all unsigned short.
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _spawn_pty(cwd: str, cols: int, rows: int) -> tuple[int, int]:
    """Fork a non-root login shell on a new PTY with a sanitised env; returns
    (pid, master_fd). Fixed argv, no shell interpolation."""
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))

    shell = os.environ.get("SHELL") or "/bin/bash"
    if not os.path.exists(shell):
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"

    pid, master_fd = pty.fork()
    if pid == 0:
        # child
        try:
            if cwd and os.path.isdir(cwd):
                os.chdir(cwd)
        except OSError:
            pass
        # Contain fork/memory bombs: NPROC stops fork loops, AS caps allocation
        # (32 GiB so build tools still work); the container also sets pids_limit.
        try:
            import resource as _res
            _res.setrlimit(_res.RLIMIT_NPROC, (512, 512))
            _res.setrlimit(_res.RLIMIT_AS, (32 * 1024 ** 3, 32 * 1024 ** 3))
            _res.setrlimit(_res.RLIMIT_CORE, (0, 0))
        except Exception:
            pass
        # Pass HOME/PATH/USER through, pin TERM for xterm.js; no secrets.
        env = {
            "TERM": "xterm-256color",
            "HOME": os.environ.get("HOME", "/app"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "USER": os.environ.get("USER", "odysseus"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PS1": r"\u@odysseus:\w\$ ",
        }
        # argv[0] "-bash" requests a login shell so the profile/PATH apply.
        argv0 = "-" + os.path.basename(shell)
        try:
            os.execvpe(shell, [argv0], env)
        except Exception:
            os._exit(127)
    # parent: non-blocking master so the reader never wedges the event loop.
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        _set_winsize(master_fd, cols, rows)
    except OSError:
        pass
    return pid, master_fd


def _reap(pid: int, master_fd: int) -> None:
    """Kill the shell's process group and close the master fd. Idempotent."""
    if signal is not None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass


def setup_terminal_routes() -> APIRouter:
    router = APIRouter(tags=["terminal"])

    @router.websocket("/ws/terminal")
    async def terminal_ws(websocket: WebSocket):
        """Interactive PTY shell over a WebSocket. Admin-only, owner-scoped.

        Protocol (text frames, JSON):
          client→server: {"type":"input","data":"<utf8>"}
                         {"type":"resize","cols":N,"rows":N}
                         {"type":"ping"}
          server→client: {"type":"output","data":"<utf8>"}
                         {"type":"exit","code":N} | {"type":"error","msg":"…"}
        """
        # Auth and origin are checked before accept(); any failure closes
        # with a policy-violation code.
        if not _origin_ok(websocket):
            logger.warning("terminal WS rejected: bad Origin %r", websocket.headers.get("origin"))
            await websocket.close(code=WS_POLICY_VIOLATION)
            return

        user = _resolve_ws_user(websocket)
        if not _ws_is_admin(websocket, user):
            logger.warning("terminal WS rejected: not admin (user=%r)", user)
            await websocket.close(code=WS_POLICY_VIOLATION)
            return

        if not PTY_SUPPORTED:
            await websocket.close(code=WS_INTERNAL_ERROR)
            return

        # No-auth dev mode: `user` may be None, so key on "".
        owner = user or ""
        session_id = websocket.query_params.get("session_id") or None

        reg = _registry(websocket)
        for k in list(reg.keys()):
            ent = reg.get(k)
            if ent and not ent.get("alive", True):
                reg.pop(k, None)
        if len(reg) >= MAX_CONCURRENT_PTYS:
            await websocket.close(code=WS_TRY_AGAIN_LATER)
            return

        await websocket.accept()

        # The connection id keeps multiple terminals per (owner, session)
        # distinct; the owner in every key blocks cross-owner lookup.
        conn_id = secrets.token_hex(8)
        reg_key = (owner, session_id, conn_id)

        cwd = _resolve_cwd(websocket, owner, session_id)
        try:
            pid, master_fd = _spawn_pty(cwd, DEFAULT_COLS, DEFAULT_ROWS)
        except Exception as e:  # pragma: no cover
            logger.exception("terminal WS spawn failed")
            try:
                await websocket.send_text(json.dumps({"type": "error", "msg": f"spawn failed: {e}"}))
            finally:
                await websocket.close(code=WS_INTERNAL_ERROR)
            return

        started = time.monotonic()
        entry = {
            "owner": owner,
            "session_id": session_id,
            "pid": pid,
            "master_fd": master_fd,
            "alive": True,
            "started": started,
            "last_io": started,
        }
        reg[reg_key] = entry
        logger.info(
            "terminal WS open: owner=%s session=%s pid=%s cwd=%s (active=%d)",
            owner or "(none)", session_id, pid, cwd, len(reg),
        )

        loop = asyncio.get_running_loop()
        closing = asyncio.Event()

        def _owns(ent: dict) -> bool:
            """Re-validate ownership on every control op (defense in depth)."""
            return ent is not None and ent.get("owner") == owner

        async def _pty_to_ws():
            """Pump PTY master → WebSocket without blocking the event loop."""
            data_avail = asyncio.Event()
            loop.add_reader(master_fd, data_avail.set)
            try:
                while not closing.is_set():
                    await data_avail.wait()
                    data_avail.clear()
                    try:
                        chunk = os.read(master_fd, READ_CHUNK)
                    except BlockingIOError:
                        continue
                    except OSError:
                        break  # fd closed / child gone → EOF
                    if not chunk:
                        break  # EOF — shell exited
                    entry["last_io"] = time.monotonic()
                    try:
                        await websocket.send_text(json.dumps(
                            {"type": "output", "data": chunk.decode("utf-8", errors="replace")}
                        ))
                    except Exception:
                        break
            finally:
                try:
                    loop.remove_reader(master_fd)
                except (OSError, ValueError):
                    pass
                closing.set()

        async def _ws_to_pty():
            """Pump WebSocket → PTY. Only input/resize/ping are honoured; the argv
            is fixed, so nothing here can change what runs."""
            while not closing.is_set():
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if not _owns(entry):  # owner re-check on every op
                    break
                if mtype == "input":
                    data = msg.get("data")
                    if not isinstance(data, str):
                        continue
                    entry["last_io"] = time.monotonic()
                    try:
                        os.write(master_fd, data.encode("utf-8", errors="replace"))
                    except OSError:
                        break
                elif mtype == "resize":
                    try:
                        cols = int(msg.get("cols", DEFAULT_COLS))
                        rows = int(msg.get("rows", DEFAULT_ROWS))
                    except (TypeError, ValueError):
                        continue
                    entry["last_io"] = time.monotonic()
                    try:
                        _set_winsize(master_fd, cols, rows)  # clamped inside
                    except OSError:
                        pass
                elif mtype == "ping":
                    entry["last_io"] = time.monotonic()
            closing.set()

        async def _watchdog():
            """Enforce idle + max-lifetime timeouts and notice child exit."""
            while not closing.is_set():
                await asyncio.sleep(5)
                now = time.monotonic()
                if now - entry["started"] > MAX_LIFETIME_S:
                    logger.info("terminal WS pid=%s hit max lifetime", pid)
                    break
                if now - entry["last_io"] > IDLE_TIMEOUT_S:
                    logger.info("terminal WS pid=%s idle timeout", pid)
                    break
                try:
                    wpid, _ = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid:
                        break
                except ChildProcessError:
                    break
                except OSError:
                    pass
            closing.set()

        tasks = [
            asyncio.create_task(_pty_to_ws()),
            asyncio.create_task(_ws_to_pty()),
            asyncio.create_task(_watchdog()),
        ]
        try:
            await closing.wait()
        finally:
            entry["alive"] = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _reap(pid, master_fd)        # GATE 5: kill child + close fd on disconnect
            reg.pop(reg_key, None)
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(json.dumps({"type": "exit", "code": 0}))
            except Exception:
                pass
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.close()
            except Exception:
                pass
            logger.info("terminal WS closed: owner=%s pid=%s (active=%d)", owner or "(none)", pid, len(reg))

    return router
