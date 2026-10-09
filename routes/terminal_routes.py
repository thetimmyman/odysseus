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
STARTUP_TIMEOUT_S = 5
REAP_TIMEOUT_S = 1
MAX_CHILD_TASKS = 512
MAX_CHILD_ADDRESS_SPACE = 32 * 1024 ** 3
CGROUP_MEMBERSHIP_PATH = Path("/proc/self/cgroup")
CGROUP_MOUNTS_PATH = Path("/proc/self/mountinfo")
CGROUP_PIDS_PATH = Path("/sys/fs/cgroup/pids.max")
CGROUP_ROOT = "/sys/fs/cgroup"
RESOURCE_LIMITS_UNAVAILABLE = "Terminal resource limits could not be applied. Contact the administrator, then reconnect."
COMMAND_CAPACITY_UNAVAILABLE = (
    "Terminal cannot launch commands within its resource limits. "
    "Close unused terminals or contact the administrator, then reconnect."
)
WORKSPACE_UNAVAILABLE = (
    "Selected project folder is unavailable. Choose another folder or clear it, then reconnect."
)
# Bound TIOCSWINSZ dimensions: no huge grids (memory) or 0x0 (div-by-zero).
MIN_COLS, MAX_COLS = 2, 500
MIN_ROWS, MAX_ROWS = 1, 300
DEFAULT_COLS, DEFAULT_ROWS = 80, 24

WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011
WS_TRY_AGAIN_LATER = 1013

SESSION_COOKIE = "odysseus_session"


class TerminalStartupError(Exception):
    """An actionable startup refusal, safe to send to the authenticated caller."""


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
    """Defaults apply only when an owned session genuinely has no saved root."""
    if session_id:
        from core.models import _session_manager
        from src.tool_execution import _is_sensitive_path
        if _session_manager is None:
            raise TerminalStartupError("Conversation storage is unavailable. Try again before reconnecting.")
        try:
            session = _session_manager.get_session(session_id)
        except KeyError:
            session = None
        if session is None or session.owner != owner:
            raise TerminalStartupError("Conversation is unavailable. Open a conversation you own, then reconnect.")
        root = getattr(session, "project_root", None)
        if root is not None and root != "":
            try:
                if not isinstance(root, str):
                    raise ValueError("invalid workspace")
                root = os.path.realpath(os.path.expanduser(root))
                if not os.path.isdir(root) or _is_sensitive_path(root):
                    raise ValueError("unavailable workspace")
            except (OSError, TypeError, ValueError) as exc:
                raise TerminalStartupError(WORKSPACE_UNAVAILABLE) from exc
            return root
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


def _has_scoped_process_limit() -> bool:
    """Recognise only a bounded, read-only private cgroup-v2 root.

    Unknown host layouts keep the UID-wide fallback. Neither an environment
    flag nor a container marker establishes a kernel-enforced task ceiling.
    """
    try:
        if CGROUP_MEMBERSHIP_PATH.read_text(encoding="ascii").splitlines() != ["0::/"]:
            return False
        mounts = []
        for line in CGROUP_MOUNTS_PATH.read_text(encoding="ascii").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            if len(fields) >= 6 and fields[4] == CGROUP_ROOT:
                mounts.append((fields, separator, after.split()))
        # Reject ambiguous overmounts as well as unsupported mount layouts.
        if len(mounts) != 1:
            return False
        fields, separator, filesystem = mounts[0]
        options = set(fields[5].split(","))
        if (
            not separator or len(filesystem) < 3 or filesystem[0] != "cgroup2"
            or fields[3] != "/" or "ro" not in options or "rw" in options
        ):
            return False
        ceiling = CGROUP_PIDS_PATH.read_text(encoding="ascii").strip()
        return ceiling.isascii() and ceiling.isdecimal() and 1 <= int(ceiling) <= MAX_CHILD_TASKS
    except (OSError, UnicodeError, ValueError):
        return False


def _apply_child_resource_limits() -> None:
    """Lower child limits; preserve stricter inherited bounds and fail closed."""
    import resource as _res

    limits = [(_res.RLIMIT_AS, MAX_CHILD_ADDRESS_SPACE), (_res.RLIMIT_CORE, 0)]
    if not _has_scoped_process_limit():
        limits.insert(0, (_res.RLIMIT_NPROC, MAX_CHILD_TASKS))
    for kind, ceiling in limits:
        inherited = _res.getrlimit(kind)
        lowered = tuple(ceiling if value == _res.RLIM_INFINITY else min(value, ceiling) for value in inherited)
        _res.setrlimit(kind, lowered)


def _probe_command_capacity() -> None:
    """One immediately reaped child checks initial fork capacity, not readiness."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    while True:
        try:
            waited, status = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    if waited != pid or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise OSError("Terminal capacity probe failed")


async def _wait_for_exec(fd: int) -> None:
    """CLOEXEC EOF admits exec; a bounded child error refuses startup."""
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    os.set_blocking(fd, False)
    loop.add_reader(fd, ready.set)
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT_S):
            while True:
                await ready.wait()
                ready.clear()
                try:
                    error = os.read(fd, 64)
                except BlockingIOError:
                    continue
                if not error:
                    return
                if error == b"cwd":
                    raise TerminalStartupError(WORKSPACE_UNAVAILABLE)
                if error == b"limits":
                    raise TerminalStartupError(RESOURCE_LIMITS_UNAVAILABLE)
                if error == b"capacity":
                    raise TerminalStartupError(COMMAND_CAPACITY_UNAVAILABLE)
                raise TerminalStartupError("Terminal shell could not start. Try reconnecting.")
    except TimeoutError as exc:
        raise TerminalStartupError("Terminal shell startup timed out. Try reconnecting.") from exc
    finally:
        loop.remove_reader(fd)


async def _spawn_pty(cwd: str, cols: int, rows: int) -> tuple[int, int]:
    """Fork a non-root login shell on a new PTY with a sanitised env; returns
    (pid, master_fd). Fixed argv, no shell interpolation."""
    cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
    rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))

    shell = os.environ.get("SHELL") or "/bin/bash"
    if not os.path.exists(shell):
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"

    # Python pipe descriptors are non-inheritable: successful exec closes the
    # writer. Keep fork on this thread; only the parent wait is asynchronous.
    error_fd, child_error_fd = os.pipe()
    try:
        pid, master_fd = pty.fork()
    except BaseException:
        os.close(error_fd)
        os.close(child_error_fd)
        raise
    if pid == 0:
        # child
        os.close(error_fd)
        stage = b"cwd"
        try:
            os.chdir(cwd)
            stage = b"limits"
            _apply_child_resource_limits()
            stage = b"capacity"
            _probe_command_capacity()
            stage = b"exec"
            env = {
                "TERM": "xterm-256color",
                "HOME": os.environ.get("HOME", "/app"),
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "USER": os.environ.get("USER", "odysseus"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                "PS1": r"\u@odysseus:\w\$ ",
            }
            argv0 = "-" + os.path.basename(shell)
            os.execvpe(shell, [argv0], env)
        except BaseException:
            try:
                os.write(child_error_fd, stage)
            finally:
                os._exit(127)
    os.close(child_error_fd)
    try:
        await _wait_for_exec(error_fd)
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            raise TerminalStartupError("Terminal shell exited during startup. Try reconnecting.")
        # Non-blocking master so the reader never wedges the event loop.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            _set_winsize(master_fd, cols, rows)
        except OSError:
            pass
        return pid, master_fd
    except BaseException:
        await _reap(pid, master_fd)
        raise
    finally:
        os.close(error_fd)


async def _reap(pid: int, master_fd: int) -> None:
    """Kill the shell's process group and close the master fd. Idempotent."""
    try:
        # Establish that this is still our unreaped child before signals. Keep
        # the master open until then: closing it first can HUP the group leader.
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except (ChildProcessError, OSError):
            return
        if signal is not None:
            try:
                # forkpty's child may not yet own a group on startup failure.
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    deadline = time.monotonic() + REAP_TIMEOUT_S
    while True:
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except (ChildProcessError, OSError):
            return
        if time.monotonic() >= deadline:
            logger.warning("terminal child %s did not reap within cleanup deadline", pid)
            return
        await asyncio.sleep(0.01)


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

        # accept() yields: recheck then reserve synchronously before the startup
        # handshake yields again. Pending starts count against the same cap.
        if len(reg) >= MAX_CONCURRENT_PTYS:
            await websocket.close(code=WS_TRY_AGAIN_LATER)
            return

        # The connection id keeps multiple terminals per (owner, session)
        # distinct; the owner in every key blocks cross-owner lookup.
        conn_id = secrets.token_hex(8)
        reg_key = (owner, session_id, conn_id)
        reg[reg_key] = {"owner": owner, "session_id": session_id, "alive": True, "starting": True}

        try:
            cwd = _resolve_cwd(websocket, user, session_id)
            pid, master_fd = await _spawn_pty(cwd, DEFAULT_COLS, DEFAULT_ROWS)
        except BaseException as e:
            reg.pop(reg_key, None)
            if not isinstance(e, Exception):
                raise
            logger.exception("terminal WS spawn failed")
            message = str(e) if isinstance(e, TerminalStartupError) else "Terminal could not start. Try reconnecting."
            try:
                await websocket.send_text(json.dumps({"type": "error", "msg": message}))
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
            await _reap(pid, master_fd)  # GATE 5: kill child + close fd on disconnect
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
