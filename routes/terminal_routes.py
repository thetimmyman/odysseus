"""Terminal routes — interactive PTY shell over a WebSocket (roadmap Tier 2 #6).

This is the HIGHEST-RISK endpoint in the app: a pseudo-terminal bridged to a
network socket is remote code execution by design. Every control below is
load-bearing; do not relax any of them.

SECURITY MODEL (multi-user, network-exposed app — LAN + Tailscale, not public):
  1. AUTH ON THE HANDSHAKE. FastAPI/Starlette HTTP auth middleware
     (``AuthMiddleware`` is a ``BaseHTTPMiddleware``) does NOT run for WebSocket
     scopes, so ``websocket.state.current_user`` is never populated. We re-do
     the cookie/bearer auth here, by hand, and require admin, BEFORE calling
     ``websocket.accept()``. A socket that accepts first and checks later is a
     vuln, so on any failure we ``close()`` with a policy-violation code and
     never accept.
  2. ORIGIN ALLOWLIST (anti-CSWSH). A WebSocket is NOT subject to the same-origin
     policy and cookies ride along automatically, so any website the user visits
     could open ws://framework:7000/... with the user's cookie and get a root
     shell (cross-site WebSocket hijacking). We validate the ``Origin`` header
     against the app's own ALLOWED_ORIGINS allowlist before accept().
  3. NON-ROOT SHELL. The uvicorn process already runs as the unprivileged
     ``odysseus`` user (uid 1000, dropped via gosu in docker/entrypoint.sh), and
     ``os.forkpty`` inherits that uid. We NEVER escalate, never setuid(0). The
     spawn is argv-only (``os.execvpe`` via ``pty.fork``), so the shell binary
     path is fixed and not shell-interpolated. cwd is the caller's own session
     ``project_root`` if set, else a safe default under /app/work or HOME.
  4. ADMIN-ONLY + OWNER-SCOPED REGISTRY. Bound to the existing admin gate (this
     is Tim's tool, not the wife/child accounts). The PTY registry in
     ``app.state`` is keyed by ``(owner, session_id)`` and the owner is
     re-validated on every input/resize/attach so one user can never attach to,
     write to, or resize another user's PTY.
  5. NO CONTROL-PATH INJECTION + RESOURCE CAPS. The shell argv is fixed; only the
     bytes the human types go to the PTY (expected). Resize dimensions are
     clamped to bounded ints before ``TIOCSWINSZ``. There is an idle timeout, a
     hard max-lifetime, and a max-concurrent-PTY cap. The child is killed and the
     master fd closed on disconnect / timeout / error.

Mirrors routes/shell_routes.py (PTY + admin gate) and routes/git_routes.py
(owner + session project_root confinement).
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

# POSIX-only, exactly like shell_routes.py: pty/fcntl/termios don't exist on
# Windows. The terminal feature is a Linux-container-only capability; on a
# non-POSIX host the WS endpoint refuses with a clean close instead of crashing
# app import.
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

# ── Resource caps ────────────────────────────────────────────────────────────
# These are hard ceilings; a terminal session is interactive but must never be
# able to pin a core forever or fork-bomb the registry.
IDLE_TIMEOUT_S = int(os.getenv("TERMINAL_IDLE_TIMEOUT_S", str(30 * 60)))      # 30 min no I/O
MAX_LIFETIME_S = int(os.getenv("TERMINAL_MAX_LIFETIME_S", str(8 * 60 * 60)))  # 8 h absolute
MAX_CONCURRENT_PTYS = int(os.getenv("TERMINAL_MAX_PTYS", "6"))               # across all owners
READ_CHUNK = 65536
# Resize bounds — TIOCSWINSZ takes unsigned shorts; keep them sane so a client
# can't ask for a 60000x60000 grid (memory) or a 0x0 (div-by-zero in apps).
MIN_COLS, MAX_COLS = 2, 500
MIN_ROWS, MAX_ROWS = 1, 300
DEFAULT_COLS, DEFAULT_ROWS = 80, 24

# WebSocket close codes
WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011
WS_TRY_AGAIN_LATER = 1013

SESSION_COOKIE = "odysseus_session"


# ── Auth: re-implemented for the WebSocket scope ──────────────────────────────
def _resolve_ws_user(websocket: WebSocket) -> Optional[str]:
    """Resolve the authenticated username for a WebSocket handshake.

    The HTTP ``AuthMiddleware`` (a BaseHTTPMiddleware) is NOT invoked on the
    WebSocket ASGI path, so we cannot rely on ``websocket.state.current_user``.
    We validate the session cookie ourselves here, mirroring the cookie-auth
    branch of ``AuthMiddleware.dispatch``. Returns the username or None.

    Bearer ``ody_`` tokens come through as the "api" pseudo-user and are NOT
    admins, so even if a future client sent one it could never pass the admin
    gate. We only honour the cookie path here.
    """
    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    # No auth configured at all → fully-trusted localhost dev mode only. Mirror
    # shell_routes/_require_admin's "no auth_manager" branch (returns/allows).
    if auth_manager is None:
        return None
    token = websocket.cookies.get(SESSION_COOKIE)
    if not auth_manager.validate_token(token):
        return None
    return auth_manager.get_username_for_token(token)


def _ws_is_admin(websocket: WebSocket, user: Optional[str]) -> bool:
    """True only for a configured admin account. Reserved sentinels ("api",
    "internal-tool") are never admins on this path — a WS carries no internal
    token, and bearer callers map to "api"."""
    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    if auth_manager is None:
        # Auth explicitly not configured: dev/localhost single-user. Allow, same
        # as shell_routes._require_admin's empty-auth_manager branch.
        return True
    if not user or user in ("api", "internal-tool"):
        return False
    return bool(auth_manager.is_admin(user))


def _allowed_origins() -> set[str]:
    """The app's own origin allowlist — reused verbatim from the ALLOWED_ORIGINS
    env the CORS middleware already uses (http(s)://framework:7000, the LAN IP,
    the Tailscale name, localhost). Normalised (lowercase, no trailing slash)."""
    raw = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1")
    out: set[str] = set()
    for o in raw.split(","):
        o = o.strip().rstrip("/").lower()
        if o:
            out.add(o)
    return out


def _origin_ok(websocket: WebSocket) -> bool:
    """Validate the handshake Origin against the allowlist (anti-CSWSH).

    A missing Origin header is allowed ONLY for non-browser clients that connect
    from loopback (e.g. a local CLI / curl test) — browsers ALWAYS send Origin on
    a WebSocket handshake, so a browser-originated cross-site hijack can never
    have an absent Origin. Any present Origin must be in the allowlist."""
    origin = websocket.headers.get("origin")
    if origin is None:
        client = websocket.client
        host = (client.host if client else "") or ""
        return host in ("127.0.0.1", "::1", "localhost")
    return origin.strip().rstrip("/").lower() in _allowed_origins()


# ── Owner-scoped PTY registry ────────────────────────────────────────────────
def _registry(websocket: WebSocket) -> dict:
    """The process-wide PTY registry, lazily created in app.state.

    Keyed by ``(owner, session_id)`` so a lookup is owner-scoped by construction;
    we additionally re-check the owner on every operation."""
    st = websocket.app.state
    reg = getattr(st, "terminal_ptys", None)
    if reg is None:
        reg = {}
        st.terminal_ptys = reg
    return reg


def _resolve_cwd(websocket: WebSocket, owner: Optional[str], session_id: Optional[str]) -> str:
    """Pick a safe, owner-scoped starting cwd for the shell.

    Prefer the caller's OWN session project_root (owner-checked inside
    _get_session_project_root — returns None on cross-owner / missing / not-dir).
    Else fall back to /app/work (the mounted repos root) if it exists, else HOME.
    Never a path the caller doesn't own."""
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
    """Apply a bounded TIOCSWINSZ. Dimensions are clamped to sane caps so a
    client can never request a pathological grid size."""
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
    # struct winsize { ws_row, ws_col, ws_xpixel, ws_ypixel } — all unsigned short.
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _spawn_pty(cwd: str, cols: int, rows: int) -> tuple[int, int]:
    """Fork a NON-root login shell attached to a new PTY. Returns (pid, master_fd).

    Runs as the current process uid (the unprivileged ``odysseus`` user; we never
    setuid). The child execs a fixed shell binary via argv (``os.execvpe``) —
    there is no shell-string interpolation in the spawn path. A minimal, sanitised
    environment is handed to the child."""
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))

    shell = os.environ.get("SHELL") or "/bin/bash"
    if not os.path.exists(shell):
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"

    pid, master_fd = pty.fork()
    if pid == 0:
        # ── CHILD ──
        # New session/controlling-tty already established by pty.fork().
        try:
            if cwd and os.path.isdir(cwd):
                os.chdir(cwd)
        except OSError:
            pass
        # Resource caps in the child: contain a fork-bomb / memory-bomb so one
        # line typed in the terminal cannot take the box down. NPROC stops
        # `:(){ :|:& };:`; AS bounds runaway allocation (generous 32 GiB so normal
        # dev/build tools still work); the container sets a pids_limit backstop too.
        try:
            import resource as _res
            _res.setrlimit(_res.RLIMIT_NPROC, (512, 512))
            _res.setrlimit(_res.RLIMIT_AS, (32 * 1024 ** 3, 32 * 1024 ** 3))
            _res.setrlimit(_res.RLIMIT_CORE, (0, 0))
        except Exception:
            pass
        # Minimal sanitised environment. We pass HOME/PATH/USER through but pin
        # TERM so xterm.js renders correctly. No secrets are injected here.
        env = {
            "TERM": "xterm-256color",
            "HOME": os.environ.get("HOME", "/app"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "USER": os.environ.get("USER", "odysseus"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PS1": r"\u@odysseus:\w\$ ",
        }
        # Login shell so the user's profile/PATH apply. argv[0] of "-bash"
        # requests a login shell from bash/sh.
        argv0 = "-" + os.path.basename(shell)
        try:
            os.execvpe(shell, [argv0], env)
        except Exception:
            os._exit(127)
    # ── PARENT ──
    # Set the master non-blocking so our reader never wedges the event loop.
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
        # ──────────────────────────────────────────────────────────────────────
        # GATE 1 + 2: AUTH AND ORIGIN ARE CHECKED **BEFORE** accept().
        # On any failure we close with a policy-violation code and never accept.
        # ──────────────────────────────────────────────────────────────────────
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

        # Owner identity for the registry. In no-auth dev mode `user` may be
        # None; key on a stable "" owner so the registry still works.
        owner = user or ""
        session_id = websocket.query_params.get("session_id") or None

        # GATE 5 (cap): refuse if we're at the global concurrent-PTY ceiling.
        reg = _registry(websocket)
        # Opportunistically drop dead entries before counting.
        for k in list(reg.keys()):
            ent = reg.get(k)
            if ent and not ent.get("alive", True):
                reg.pop(k, None)
        if len(reg) >= MAX_CONCURRENT_PTYS:
            await websocket.close(code=WS_TRY_AGAIN_LATER)
            return

        # Auth + origin + cap all passed → NOW we accept the socket.
        await websocket.accept()

        # GATE 4: owner-scoped registry key. A connection id keeps multiple
        # terminals per (owner, session) distinct, but every key carries the
        # owner so cross-owner lookup is impossible by construction.
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
            """Re-validate ownership on every control op (defense-in-depth even
            though the registry key is owner-scoped)."""
            return ent is not None and ent.get("owner") == owner

        async def _pty_to_ws():
            """Pump PTY master → WebSocket. Uses the loop reader on the
            non-blocking master fd so we never block the event loop."""
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
            """Pump WebSocket → PTY master. Only `input`/`resize`/`ping` control
            messages are honoured; the shell argv is fixed, so nothing here can
            change WHAT runs — only the bytes the human types."""
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
                # Unknown types are ignored.
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
                # Reap-check: has the shell exited on its own?
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
