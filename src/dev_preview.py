"""Dev Preview — run a repo's dev server inside the Odysseus container and
preview it in the browser.

SECURITY MODEL (admin-only feature on a network-exposed multi-user box):
  * PATH-CONFINED to REPOS_ROOT. Every app_id is realpath + commonpath validated
    to a DIRECT child of REPOS_ROOT containing a package.json — no traversal,
    no symlink escape, no nested dirs.
  * NO ARBITRARY SHELL. Commands are fixed list-arg templates (npm ci / npm run
    <script> -- --hostname 0.0.0.0 --port <port>); subprocess WITHOUT shell=True.
  * PACKAGE-MANAGER ALLOWLIST — npm only (MVP).
  * SCRIPT ALLOWLIST — only a fixed set of dev/start scripts, AND the script
    must actually exist in the app's package.json.
  * SINGLE running dev server (MVP); start() stops any existing one first.
  * KILLABLE — the server runs in its own session (start_new_session) so the
    whole process tree is killed via killpg + kill_process_tree.
  * CAPPED LOGS — bounded deques, never unbounded growth.
  * SECRET-SCRUBBED CHILD ENV — Odysseus's own secrets are stripped from the
    env handed to npm/next so a previewed app can't read them.

The dev server binds container LOOPBACK only (127.0.0.1:<PREVIEW_PORT>) and is
NOT published to the host at all — so neither the LAN/Tailscale NOR sibling
docker containers can reach it. The sole path in is the admin-cookie-gated
in-Odysseus proxy (src/dev_preview_proxy.py) on PROXY_PORT, which serves the app
at its origin root (Codespaces-style) so the Dev Preview iframe can embed it.
Admin-gating lives in the route + proxy layers; this module is the confined
process manager only.

ORPHAN SAFETY: the manager tracks a single server in `_running`, but that state
can desync from reality (a crash mid-stop, a lost reference). So `stop()` ALSO
kills whatever is listening on PREVIEW_PORT (kill-by-port fallback), and
`status()` RECONCILES — if `_running` is empty but the port is live, it reports
an "unmanaged" server so the UI can still offer Stop. Stop is authoritative.
"""

import errno
import glob
import json
import logging
import os
import re
import signal
import socket
import stat as _stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

REPOS_ROOT = os.path.realpath(os.environ.get("DEV_PREVIEW_ROOT", "/app/work"))
PREVIEW_PORT = int(os.environ.get("DEV_PREVIEW_PORT", "3000"))
# The dev server binds container LOOPBACK only; preview goes through the
# admin-gated in-Odysseus proxy on PROXY_PORT (src/dev_preview_proxy.py).
PROXY_PORT = int(os.environ.get("DEV_PREVIEW_PROXY_PORT", "7100"))
PM_ALLOWLIST = {"npm"}
# dev/start-style scripts we will run (must ALSO exist in the app package.json)
SCRIPT_ALLOWLIST = {"dev", "dev:codespace", "dev:turbo", "start"}
LOG_CAP = 4000
READY_MARKERS = ("ready in", "ready -", "✓ ready", "started server", "compiled", "- local:")

# Default-DENY env for the previewed child. A previewed repo runs UNTRUSTED code
# (its next.config.js, build scripts, and process.env are all readable by repo
# code), so the child env is an explicit ALLOWLIST — never a denylist. A denylist
# that misses a prefix leaks live secrets: the 2026-06-05 security review found
# DATA_BRAVE_API_KEY/GOOGLE_API_KEY/SERPER_API_KEY/TAVILY_API_KEY/HUGGING_FACE_HUB_TOKEN
# all slipped past the old prefix list. next/npm need only PATH+HOME+a few locale
# vars; the app reads its own NEXT_PUBLIC_*/secrets from its .env* files on disk.
_ENV_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
    "TMPDIR", "TMP", "TEMP", "PWD", "USER", "LOGNAME", "SHELL", "HOSTNAME",
    "NODE_ENV", "NODE_OPTIONS", "NODE_PATH", "CI", "FORCE_COLOR",
}
# Belt-and-suspenders: even if a var is ever added to the allowlist, never pass
# anything whose NAME looks like a credential.
_SECRET_NAME_RE = re.compile(
    r"(API_?KEY|_TOKEN$|^TOKEN$|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY|"
    r"ACCESS_?KEY|CLIENT_SECRET|AUTH)", re.I)

_lock = threading.RLock()
_running: Optional[dict] = None          # the single live dev server, or None
_install: dict = {}                      # app_id -> {status, logs, started_at, finished_at, code}


# --- path confinement --------------------------------------------------------
def _safe_app_dir(app_id: str) -> str:
    """Return the realpath of a DIRECT child of REPOS_ROOT that holds a
    package.json, or raise ValueError. Rejects traversal / symlink escape."""
    if not app_id or not isinstance(app_id, str):
        raise ValueError("missing app id")
    if "/" in app_id or "\\" in app_id or app_id in (".", ".."):
        raise ValueError("invalid app id")
    cand = os.path.realpath(os.path.join(REPOS_ROOT, app_id))
    if os.path.commonpath([cand, REPOS_ROOT]) != REPOS_ROOT:
        raise ValueError("path escapes repos root")
    if os.path.dirname(cand) != REPOS_ROOT:
        raise ValueError("must be a top-level repo under the repos root")
    if not os.path.isdir(cand) or not os.path.isfile(os.path.join(cand, "package.json")):
        raise ValueError("not an app (no package.json)")
    return cand


def _clean_env() -> dict:
    """Build the previewed child's env from an explicit ALLOWLIST (default-deny),
    so no Odysseus secret can leak into untrusted repo code. Next reads its own
    .env* config from disk, so PATH + HOME + a few locale vars are all it needs."""
    env = {k: v for k, v in os.environ.items()
           if k in _ENV_ALLOWLIST and not _SECRET_NAME_RE.search(k)}
    env.setdefault("PATH", os.environ.get("PATH", ""))
    env.setdefault("HOME", os.environ.get("HOME", "/app"))
    env["NODE_ENV"] = "development"
    return env


def _git_info(d: str):
    def _g(args):
        try:
            r = subprocess.run(["git", "-C", d] + args, capture_output=True,
                               text=True, timeout=5)
            return (r.stdout or "").strip()
        except Exception:
            return ""
    return (_g(["rev-parse", "--abbrev-ref", "HEAD"]) or None,
            _g(["remote", "get-url", "origin"]) or None)


def _check_embeddable(port: int) -> Optional[bool]:
    """True if the running app can be shown in a cross-origin iframe, False if it
    blocks framing (X-Frame-Options DENY/SAMEORIGIN or CSP frame-ancestors
    none/self), None if undetermined. Many apps (e.g. tacticus) send DENY for
    anti-clickjacking, in which case the UI falls back to an Open-in-new-tab."""
    xfo = csp = ""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=6) as r:
            xfo = (r.headers.get("X-Frame-Options") or "").lower()
            csp = (r.headers.get("Content-Security-Policy") or "").lower()
    except urllib.error.HTTPError as e:
        xfo = (e.headers.get("X-Frame-Options") or "").lower()
        csp = (e.headers.get("Content-Security-Policy") or "").lower()
    except Exception:
        return None
    if "deny" in xfo or "sameorigin" in xfo:
        return False
    if "frame-ancestors" in csp:
        seg = csp.split("frame-ancestors", 1)[1].split(";", 1)[0]
        if "'none'" in seg or "'self'" in seg:
            return False
    return True


def _port_free(port: int) -> bool:
    # SO_REUSEADDR mirrors how a real server (next dev) binds, so a lingering
    # TIME_WAIT socket from a just-killed server doesn't read as "in use".
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pids_on_port(port: int) -> set:
    """PIDs holding a LISTEN socket on `port`, via /proc/net/tcp(+6) + /proc/*/fd.
    Used to reconcile + kill orphaned dev servers when `_running` is lost."""
    port_hex = format(port, "04X")
    inodes = set()
    for tcp in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(tcp) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if len(p) > 9 and p[3] == "0A" and p[1].rsplit(":", 1)[-1].upper() == port_hex:
                        inodes.add(p[9])
        except OSError:
            pass
    if not inodes:
        return set()
    socks = {"socket:[" + i + "]" for i in inodes}
    pids = set()
    for d in glob.glob("/proc/[0-9]*"):
        try:
            for fd in os.listdir(d + "/fd"):
                try:
                    if os.readlink(d + "/fd/" + fd) in socks:
                        pids.add(int(d.rsplit("/", 1)[1]))
                        break
                except OSError:
                    pass
        except OSError:
            pass
    return pids


def _kill_port(port: int) -> list:
    """Kill the process tree of anything listening on `port`. Returns killed PIDs.
    The authoritative backstop so Stop works even when `_running` desynced."""
    from core.platform_compat import kill_process_tree
    killed = []
    for pid in _pids_on_port(port):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            kill_process_tree(pid)
        except Exception:
            pass
        killed.append(pid)
    return killed


# --- discovery ---------------------------------------------------------------
def _detect(app_id: str, d: str) -> dict:
    name, scripts = app_id, {}
    try:
        with open(os.path.join(d, "package.json")) as f:
            pj = json.load(f)
        name = pj.get("name") or app_id
        scripts = pj.get("scripts") or {}
    except Exception:
        pass
    branch, remote = _git_info(d)
    running = bool(_running and _running.get("app_id") == app_id)
    inst = _install.get(app_id)
    return {
        "id": app_id,
        "name": name,
        "path": d,
        "package_manager": "npm",
        "has_lockfile": os.path.isfile(os.path.join(d, "package-lock.json")),
        "scripts": list(scripts.keys()),
        "dev_scripts": [s for s in scripts.keys() if s in SCRIPT_ALLOWLIST],
        "installed": os.path.isdir(os.path.join(d, "node_modules")),
        "branch": branch,
        "remote": remote,
        "running": running,
        "port": _running.get("port") if running else None,
        "run_status": _running.get("status") if running else None,
        "embeddable": _running.get("embeddable") if running else None,
        "install_status": inst.get("status") if inst else None,
    }


def list_apps() -> list:
    out = []
    cfg = config()
    if not cfg.get("enabled", True):          # master kill-switch (UI-toggleable)
        return out
    allow = cfg.get("app_allowlist")          # None => all; else only these names
    try:
        names = sorted(os.listdir(REPOS_ROOT))
    except OSError:
        return out
    for name in names:
        if allow and name not in allow:
            continue
        d = os.path.join(REPOS_ROOT, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "package.json")):
            try:
                out.append(_detect(name, d))
            except Exception as e:
                logger.warning("dev_preview: detect %s failed: %r", name, e)
    return out


# --- install -----------------------------------------------------------------
def install(app_id: str) -> dict:
    if not _enabled():
        raise ValueError("Dev Preview is disabled in settings")
    d = _safe_app_dir(app_id)
    with _lock:
        cur = _install.get(app_id)
        if cur and cur.get("status") == "running":
            return {"status": "already_running"}
        logs = deque(maxlen=LOG_CAP)
        _install[app_id] = {"status": "running", "logs": logs,
                            "started_at": time.time(), "finished_at": None, "code": None}
    has_lock = os.path.isfile(os.path.join(d, "package-lock.json"))
    cmd = (["npm", "ci", "--no-audit", "--no-fund"] if has_lock
           else ["npm", "install", "--no-audit", "--no-fund"])
    logs.append("$ " + " ".join(cmd))

    def _run():
        try:
            proc = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    env=_clean_env())
            for line in proc.stdout:
                logs.append(line.rstrip("\n"))
            proc.wait()
            with _lock:
                rec = _install.get(app_id)
                if rec:
                    rec["status"] = "done" if proc.returncode == 0 else "error"
                    rec["code"] = proc.returncode
                    rec["finished_at"] = time.time()
            logs.append(f"[install exit {proc.returncode}]")
        except Exception as e:
            logs.append(f"[install error] {e!r}")
            with _lock:
                rec = _install.get(app_id)
                if rec:
                    rec["status"] = "error"
                    rec["finished_at"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "running"}


# --- start / stop ------------------------------------------------------------
def start(app_id: str, script: str = "dev", port: Optional[int] = None) -> dict:
    if not _enabled():
        raise ValueError("Dev Preview is disabled in settings")
    d = _safe_app_dir(app_id)
    if not os.path.isdir(os.path.join(d, "node_modules")):
        raise ValueError("dependencies not installed — run Install first")
    if script not in SCRIPT_ALLOWLIST:
        raise ValueError("script not allowed (dev/start scripts only)")
    info = _detect(app_id, d)
    if script not in info.get("scripts", []):
        raise ValueError(f"script '{script}' not found in package.json")

    use_port = PREVIEW_PORT
    if port is not None and int(port) != PREVIEW_PORT:
        raise ValueError(f"only port {PREVIEW_PORT} is published/reachable in this build")

    stop()  # single-server MVP

    # A killed server releases its port asynchronously (SIGKILL + child reaping
    # + TIME_WAIT), so poll briefly before declaring the port busy — this makes
    # stop-then-start (restart) reliable.
    freed = False
    for _ in range(16):
        if _port_free(use_port):
            freed = True
            break
        time.sleep(0.25)
    if not freed:
        raise ValueError(f"port {use_port} is already in use")

    # Fixed template — NO arbitrary shell. script is from the allowlist.
    # Bind to container LOOPBACK only — siblings can't reach it; the admin-gated
    # proxy (dev_preview_proxy) is the sole path in.
    cmd = ["npm", "run", script, "--", "--hostname", "127.0.0.1", "--port", str(use_port)]
    logs = deque(maxlen=LOG_CAP)
    logs.append("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=_clean_env(), start_new_session=True)
    except Exception as e:
        raise ValueError(f"failed to start: {e}")

    state = {"app_id": app_id, "port": use_port, "script": script, "pid": proc.pid,
             "proc": proc, "started_at": time.time(), "status": "starting",
             "embeddable": None, "logs": logs}
    with _lock:
        global _running
        _running = state

    def _reader():
        try:
            for line in proc.stdout:
                logs.append(line.rstrip("\n"))
                low = line.lower()
                if state["status"] == "starting" and any(m in low for m in READY_MARKERS):
                    state["status"] = "running"
                    # probe X-Frame-Options/CSP off-thread (iframe vs Open-in-tab)
                    threading.Thread(
                        target=lambda: state.__setitem__("embeddable", _check_embeddable(state["port"])),
                        daemon=True).start()
            proc.wait()
        finally:
            with _lock:
                if _running is state:
                    state["status"] = "stopped" if proc.returncode in (0, None) else "error"

    threading.Thread(target=_reader, daemon=True).start()
    return {"app_id": app_id, "port": use_port, "script": script, "status": "starting"}


def stop(app_id: Optional[str] = None) -> dict:
    """Stop the dev server. Authoritative: kills the tracked process tree AND
    anything still listening on PREVIEW_PORT (so a desynced/orphaned server is
    reaped too). Works even when `_running` was lost."""
    with _lock:
        global _running
        st = _running
        if st and app_id and st.get("app_id") != app_id:
            return {"stopped": False, "reason": "that app is not the running server"}
        _running = None
    # 1. Kill the tracked tree (independent steps — a dead pid on SIGTERM must
    #    not skip SIGKILL or the kill-by-port backstop).
    if st:
        pid = st.get("pid")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except Exception:
                pass
            if sig == signal.SIGTERM:
                time.sleep(0.4)
        try:
            from core.platform_compat import kill_process_tree
            kill_process_tree(pid)
        except Exception:
            pass
        st["status"] = "stopped"
    # 2. Backstop: kill anything STILL on the port (orphan reconcile).
    orphans = _kill_port(PREVIEW_PORT)
    if not st and not orphans:
        return {"stopped": False}
    return {"stopped": True,
            "app_id": st.get("app_id") if st else None,
            "port": PREVIEW_PORT,
            "killed_orphans": orphans}


def status() -> dict:
    with _lock:
        st = _running
    if st:
        return {"running": {
            "app_id": st["app_id"], "port": st["port"], "script": st["script"],
            "status": st["status"], "embeddable": st.get("embeddable"),
            "uptime_s": int(time.time() - st["started_at"]), "unmanaged": False,
        }}
    # RECONCILE: nothing tracked, but is a server still listening? (orphan)
    if not _port_free(PREVIEW_PORT):
        return {"running": {
            "app_id": None, "port": PREVIEW_PORT, "script": None,
            "status": "unmanaged", "embeddable": None, "uptime_s": None,
            "unmanaged": True,
        }}
    return {"running": None}


def get_logs(app_id: str, kind: str = "run") -> dict:
    # The previewed child's stdout can echo its own .env.local secrets — scrub
    # them (+ high-entropy token shapes) BEFORE returning. Reading the values for
    # redaction is server-side only; they are never sent to the client.
    vals = _values_for_redaction(app_id)
    def _scrub(lines):
        return [_redact_log_line(ln, vals) for ln in lines]
    if kind == "install":
        inst = _install.get(app_id)
        return {"kind": "install",
                "status": inst.get("status") if inst else None,
                "lines": _scrub(list(inst["logs"]) if inst else [])}
    with _lock:
        st = _running
    if st and st.get("app_id") == app_id:
        return {"kind": "run", "status": st["status"], "lines": _scrub(list(st["logs"]))}
    return {"kind": "run", "status": None, "lines": []}


# --- per-app detail (read-only: install cmd, scripts, env status) -----------
# Env status compares the app's .env.local against .env.example by KEY NAME only.
# Values are never returned, logged, or stored — a value is read momentarily ONLY
# to classify set vs blank, then immediately discarded — so only the key name +
# set/blank/missing ever leaves this module, and no secret material crosses the
# read-only boundary (the masked value editor is a separate, security-reviewed pass).
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _env_keys(path: str) -> list:
    """Ordered, de-duplicated key NAMES declared in an env file (no values)."""
    out, seen = [], set()
    try:
        with open(path) as f:
            for ln in f:
                if ln.lstrip().startswith("#"):
                    continue
                m = _ENV_LINE_RE.match(ln)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    out.append(m.group(1))
    except OSError:
        pass
    return out


def _env_set_status(path: str) -> dict:
    """{KEY: 'set'|'blank'} for an env file — judged by name+emptiness only. The
    value is read solely to test blank-ness and is immediately discarded; it is
    never stored or returned."""
    out = {}
    try:
        with open(path) as f:
            for ln in f:
                if ln.lstrip().startswith("#"):
                    continue
                m = _ENV_LINE_RE.match(ln)
                if not m:
                    continue
                val = m.group(2).strip()
                if len(val) >= 2 and val[0] in "'\"" and val[-1] == val[0]:
                    val = val[1:-1]
                out[m.group(1)] = "set" if val.strip() else "blank"
    except OSError:
        pass
    return out


def _env_local_gitignored(d: str) -> Optional[bool]:
    """Is .env.local ignored by git? True/False, or None if git can't answer.
    Lets the UI warn when a secrets file would be committable."""
    try:
        r = subprocess.run(["git", "-C", d, "check-ignore", "-q", ".env.local"],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            return True
        if r.returncode == 1:
            return False
    except Exception:
        pass
    return None


def _env_status(d: str) -> dict:
    """Read-only env posture: compares .env.example (template of expected keys)
    against .env.local (what's configured). Per-KEY set/blank/missing + a rollup
    — NEVER any value."""
    ex_path = os.path.join(d, ".env.example")
    local_path = os.path.join(d, ".env.local")
    has_example = os.path.isfile(ex_path)
    has_local = os.path.isfile(local_path)
    expected = _env_keys(ex_path)
    local = _env_set_status(local_path)
    keys = [{"key": k, "status": local.get(k, "missing")} for k in expected]  # set|blank|missing
    set_n = sum(1 for x in keys if x["status"] == "set")
    total = len(keys)
    extra = sorted(k for k in local if k not in set(expected))  # in .env.local, not in template
    if not has_example and not has_local:
        rollup = "none"                 # no env files at all
    elif not has_example and has_local:
        rollup = "configured"           # has local but no template to grade against
    elif not has_local:
        rollup = "missing"              # template exists, nothing configured
    elif total and set_n >= total:
        rollup = "ready"                # every templated key set
    else:
        rollup = "partial"             # some templated keys blank/missing
    return {
        "status": rollup,
        "has_example": has_example,
        "has_local": has_local,
        "set": set_n,
        "total": total,
        "keys": keys,                   # [{key, status}] — names only, no values
        "extra_keys": extra,
        "gitignored": _env_local_gitignored(d),
    }


def app_detail(app_id: str) -> dict:
    """Per-app READ-ONLY config: the detected install command (the same fixed
    template install() would run — never arbitrary), runnable start scripts,
    env-file status (key names + set/blank/missing, NO values), and metadata.
    Path-confined via _safe_app_dir."""
    d = _safe_app_dir(app_id)
    base = _detect(app_id, d)
    install_cmd = ["npm", "ci"] if base["has_lockfile"] else ["npm", "install"]
    base["install_command"] = " ".join(install_cmd)
    base["install_command_argv"] = install_cmd
    base["env"] = _env_status(d)
    base["vault_keys"] = vault_keys(app_id)   # {key, source} list — names only, no values
    return base


# --- masked .env.local editor (WRITE-ONLY; values never returned or logged) --
# THREAT MODEL (red-teamed 2026-06-05): the previewed app runs as the SAME uid as
# this process, in the very directory we write. It is therefore an ACTIVE
# adversary that can swap d/.env.local for a symlink (to /app/.env, a sibling
# app's .env.local, ~/.ssh, ...) at any instant. So the entire read-modify-write
# runs on PINNED fds (dir_fd + O_NOFOLLOW), validates inode identity (fstat
# S_ISREG + euid), creates the temp via O_CREAT|O_EXCL|O_NOFOLLOW with a random
# name, and renameat()s within the pinned dir — the final path is NEVER opened by
# name. Path-name checks are meaningless against a same-uid swap; only fd
# identity is sound. Serialization is single-quote-literal with $, backslash, and
# single-quote FORBIDDEN, which neutralizes dotenv-expand + escape processing.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Keys that steer the node/loader process or the JS prototype chain — refused.
_ENV_KEY_DENY = {
    "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_PATH", "LD_PRELOAD",
    "LD_LIBRARY_PATH", "PATH", "__PROTO__", "CONSTRUCTOR", "PROTOTYPE",
}
# Single-line guarantee: all C0 controls, DEL, NEL, LS, PS, and bidi controls.
_ENV_VALUE_BAD = re.compile(
    "[\x00-\x1f\x7f  ‎‏‪-‮]")
_ENV_VALUE_MAX = 8192


def _env_validate(key: str, value) -> None:
    """Reject anything that could break out of KEY='value' or trigger dotenv
    expansion / escape processing. All messages are STATIC — the value is never
    echoed back, even in an error."""
    if not key or not _ENV_KEY_RE.match(key):
        raise ValueError("invalid key name")
    if key.upper() in _ENV_KEY_DENY:
        raise ValueError("that key name is not allowed")
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    if len(value) > _ENV_VALUE_MAX:
        raise ValueError("value too long")
    if _ENV_VALUE_BAD.search(value):
        raise ValueError("value has a control or line-separator character")
    if "'" in value:
        raise ValueError("value may not contain a single quote (')")
    if "$" in value:
        raise ValueError("value may not contain '$' (env-var expansion)")
    if "\\" in value:
        raise ValueError("value may not contain a backslash")


def _env_parse_single(line: str) -> dict:
    """Parse exactly our own KEY='value' output back to {key: value}, literal."""
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)='(.*)'$", line)
    return {m.group(1): m.group(2)} if m else {}


def _env_serialize_line(key: str, value: str) -> bytes:
    """KEY='value' — single-quote literal. Safe ONLY because _env_validate has
    forbidden ', $, \\, and control chars, so dotenv reads it verbatim. A
    round-trip self-check guards against a serializer bug."""
    line = f"{key}='{value}'"
    if _env_parse_single(line) != {key: value}:
        raise ValueError("internal serialization check failed")
    return line.encode("utf-8")


def _key_line_re(key: str):
    return re.compile(rb"^\s*(?:export\s+)?" + re.escape(key.encode()) + rb"\s*=")


def _open_dir_fd(d: str) -> int:
    return os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _read_env_local_bytes(dfd: int) -> bytes:
    """Read .env.local via a NOFOLLOW fd under dfd. b'' if absent. Raises
    ValueError on a symlink / non-regular / not-ours / oversized file — so a
    same-uid planted symlink is refused, never followed."""
    try:
        ffd = os.open(".env.local", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        return b""
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError(".env.local is a symlink — refusing")
        raise
    try:
        st = os.fstat(ffd)
        if not _stat.S_ISREG(st.st_mode):
            raise ValueError(".env.local is not a regular file — refusing")
        if st.st_uid != os.geteuid():
            raise ValueError(".env.local is not owned by this process — refusing")
        chunks, total = [], 0
        while True:
            b = os.read(ffd, 65536)
            if not b:
                break
            total += len(b)
            if total > (1 << 20):
                raise ValueError(".env.local is unexpectedly large — refusing")
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(ffd)


def _env_write_pinned(d: str, key: str, value: Optional[str], *, clear: bool) -> str:
    """Atomically set/clear KEY in d/.env.local on PINNED fds (see module note).
    Preserves all other lines verbatim; removes EVERY existing assignment of KEY
    (dotenv is last-wins, so a stale duplicate would shadow us). 0600, fsynced,
    renameat within the pinned dir. Whole sequence under _lock (TOCTOU)."""
    with _lock:
        dfd = _open_dir_fd(d)
        try:
            # Fail-closed: must be provably gitignored. (The NOFOLLOW read below
            # is the real symlink defense; this just blocks a committable file.)
            if _env_local_gitignored(d) is not True:
                raise ValueError(".env.local is not gitignored — refusing to write a committable secrets file")
            existing = _read_env_local_bytes(dfd)
            kre = _key_line_re(key)
            out_lines = [ln for ln in existing.split(b"\n") if not kre.match(ln)]
            if out_lines and out_lines[-1] == b"":
                out_lines.pop()                     # drop the trailing-newline empty
            if not clear:
                out_lines.append(_env_serialize_line(key, value))
            data = (b"\n".join(out_lines) + b"\n") if out_lines else b""
            tmpname = ".env.local." + os.urandom(8).hex() + ".tmp"
            tfd = os.open(tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=dfd)
            try:
                tst = os.fstat(tfd)
                if tst.st_uid != os.geteuid() or _stat.S_IMODE(tst.st_mode) != 0o600:
                    raise ValueError("temp file failed ownership/mode check")
                os.write(tfd, data)
                os.fsync(tfd)
            except BaseException:
                try:
                    os.unlink(tmpname, dir_fd=dfd)
                except OSError:
                    pass
                raise
            finally:
                os.close(tfd)
            os.replace(tmpname, ".env.local", src_dir_fd=dfd, dst_dir_fd=dfd)
            try:
                os.fsync(dfd)
            except OSError:
                pass
        finally:
            os.close(dfd)
    return "missing" if clear else ("set" if value.strip() else "blank")


def env_set(app_id: str, key: str, value: str) -> dict:
    """Set KEY=value in the app's .env.local. WRITE-ONLY — the value is never
    returned or logged (only the key name + resulting set/blank status)."""
    if not _enabled():
        raise ValueError("Dev Preview is disabled in settings")
    d = _safe_app_dir(app_id)
    _env_validate(key, value)
    status = _env_write_pinned(d, key, value, clear=False)
    logger.info("dev_preview: env set %s in %s (%s)", key, os.path.basename(d), status)
    return {"ok": True, "key": key, "status": status}


def env_clear(app_id: str, key: str) -> dict:
    if not _enabled():
        raise ValueError("Dev Preview is disabled in settings")
    d = _safe_app_dir(app_id)
    if not key or not _ENV_KEY_RE.match(key):
        raise ValueError("invalid key name")
    status = _env_write_pinned(d, key, None, clear=True)
    logger.info("dev_preview: env clear %s in %s", key, os.path.basename(d))
    return {"ok": True, "key": key, "status": status}


# --- vault sourcing (fetch a secret value into .env.local, value-never-returned)
# SECURITY: both fetchers route through `ssh minipc`, so the vault credentials
# (kubeconfig for k3s; the bw session for Vaultwarden) stay on the MINI-PC, NOT in
# this container — the container only triggers a fetch over the ssh access it
# already has, and the value comes back over that channel straight to the pinned
# writer. The value is NEVER returned to the client or logged. The mapping lives in
# settings.json dev_preview.vault_map[app][KEY] = {source, ...locator...} and holds
# only LOCATORS (k3s ns/secret/key or a Vaultwarden item id/field) — never values.
_SSH_MINIPC = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", "minipc"]
_VAULT_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")   # injection-safe locator token


def _vault_map(app_id: str) -> dict:
    try:
        from src import settings as _settings
        vm = (_settings.get_setting("dev_preview", {}) or {}).get("vault_map", {}) or {}
        m = vm.get(app_id, {}) or {}
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _ssh_env() -> dict:
    return {"HOME": os.environ.get("HOME", "/app"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _ssh_fetch(remote_cmd: str) -> str:
    """Run a fetch command on the mini-PC over ssh; return stdout (the secret
    value), minus a single trailing newline. Raises ValueError on failure. The
    value is NEVER logged OR surfaced in an error: on nonzero exit the caller gets
    a generic 'exit N', and only a high-entropy-redacted tail of stderr is logged
    server-side."""
    try:
        r = subprocess.run(_SSH_MINIPC + [remote_cmd], capture_output=True,
                           text=True, timeout=30, env=_ssh_env())
    except Exception as e:
        raise ValueError(f"vault fetch failed (ssh {e.__class__.__name__})")
    if r.returncode != 0:
        # NEVER surface raw stderr to the client — a vault helper could echo the
        # secret value on error. Log a high-entropy-redacted tail server-side and
        # return a generic message.
        logger.warning("dev_preview: vault fetch exit %s: %s", r.returncode,
                       _HIGH_ENTROPY.sub("[REDACTED]", (r.stderr or "").strip()[:300]))
        raise ValueError(f"vault fetch failed (exit {r.returncode})")
    val = r.stdout
    return val[:-1] if val.endswith("\n") else val


def _fetch_k3s(loc: dict) -> str:
    ns = str(loc.get("namespace") or loc.get("ns") or "")
    secret = str(loc.get("secret") or "")
    key = str(loc.get("key") or "")
    if not all(_VAULT_TOKEN_RE.match(x) for x in (ns, secret, key)):
        raise ValueError("invalid k3s locator (ns/secret/key)")
    # Fetch the raw base64 (NO remote pipe — a `| base64 -d` would mask kubectl's
    # exit code via the pipeline, turning a NotFound into a silent empty value).
    # Decode in Python. Tokens are charset-validated, so interpolation is
    # injection-safe (no shell metacharacters).
    cmd = f"kubectl get secret {secret} -n {ns} -o jsonpath='{{.data.{key}}}'"
    b64 = _ssh_fetch(cmd)
    if not b64:
        raise ValueError(f"k3s key '{key}' not present in secret '{secret}'")
    try:
        import base64 as _b64
        return _b64.b64decode(b64, validate=True).decode("utf-8")
    except Exception:
        raise ValueError("k3s value is not valid base64/utf-8")


def _fetch_vaultwarden(loc: dict) -> str:
    # Delegates to an operator-provisioned helper on the mini-PC that OWNS the bw
    # session/credential (kept OFF this container). It takes an item-id + field and
    # prints the value. Locator uses an item ID (UUID) to dodge name-quoting issues.
    item = str(loc.get("item_id") or loc.get("item") or "")
    field = str(loc.get("field") or "password")
    if not _VAULT_TOKEN_RE.match(item) or not _VAULT_TOKEN_RE.match(field):
        raise ValueError("invalid vaultwarden locator (item_id/field)")
    # Fixed ABSOLUTE remote path — no `$HOME`/`~` (no reliance on remote shell
    # expansion). An operator override must be a plain absolute path with NO shell
    # metacharacters, so the helper invocation can't be turned into injection.
    helper = os.environ.get("DEV_PREVIEW_VW_HELPER", "/home/timmyman/dev-preview-vault-fetch.sh")
    if not re.match(r"^/[A-Za-z0-9._/-]+$", helper):
        raise ValueError("invalid DEV_PREVIEW_VW_HELPER path")
    val = _ssh_fetch(f"{helper} {item} {field}")
    if not val:
        raise ValueError("vaultwarden value empty (is the bw session provisioned on the mini-PC?)")
    return val


_VAULT_FETCHERS = {"k3s": _fetch_k3s, "vaultwarden": _fetch_vaultwarden}


def env_source_from_vault(app_id: str, key: str) -> dict:
    """Fetch KEY's value from its mapped vault and write it to .env.local. The
    value is fetched server-side, written via the pinned writer, and NEVER returned
    or logged — only {ok, key, status, source}."""
    if not _enabled():
        raise ValueError("Dev Preview is disabled in settings")
    d = _safe_app_dir(app_id)
    if not key or not _ENV_KEY_RE.match(key):
        raise ValueError("invalid key name")
    if key.upper() in _ENV_KEY_DENY:
        raise ValueError("that key name is not allowed")
    mapping = _vault_map(app_id).get(key)
    if not isinstance(mapping, dict):
        raise ValueError(f"no vault mapping for '{key}'")
    source = str(mapping.get("source") or "")
    fetch = _VAULT_FETCHERS.get(source)
    if not fetch:
        raise ValueError(f"unknown vault source '{source}'")
    value = fetch(mapping)
    # Same storage invariant as manual entry: reject anything the single-quote
    # serializer can't hold safely (so a sourced value can't break out or expand).
    try:
        _env_validate(key, value)
    except ValueError:
        del value
        raise ValueError("sourced value contains a character that can't be safely "
                         "stored ($, backslash, quote, or control) — set it manually")
    status = _env_write_pinned(d, key, value, clear=False)
    del value
    logger.info("dev_preview: env source %s in %s from %s (%s)",
                key, os.path.basename(d), source, status)
    return {"ok": True, "key": key, "status": status, "source": source}


def vault_keys(app_id: str) -> list:
    """Key NAMES that have a vault mapping for this app + their source (non-secret;
    powers the UI 'Source from vault' button). No locators-as-values, names only."""
    out = []
    for k, m in _vault_map(app_id).items():
        if _ENV_KEY_RE.match(k) and isinstance(m, dict) and m.get("source") in _VAULT_FETCHERS:
            out.append({"key": k, "source": m.get("source")})
    return out


# --- log redaction (scrub .env.local values out of previewed-app stdout) -----
# The previewed app is untrusted code that reads .env.local itself and routinely
# echoes values on error (zod parse errors, console.log(process.env), ...). The
# "never log values" rule must therefore extend to the CHILD's stdout we relay,
# not just this module's own logging. So get_logs() scrubs the current .env.local
# values (read server-side, never returned) plus high-entropy token shapes.
_HIGH_ENTROPY = re.compile(
    r"eyJ[A-Za-z0-9_\-]{20,}"                                   # JWT
    r"|AKIA[0-9A-Z]{16}"                                        # AWS access-key id
    r"|sk-[A-Za-z0-9]{20,}"                                     # sk-style token
    r"|(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])"   # long base64
    r"|(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")             # long hex


def _env_read_kv(d: str) -> dict:
    """{KEY: value} from .env.local — read ONLY to scrub those values out of log
    output server-side. NEVER returned to any client. Tolerant parser; reads via
    the NOFOLLOW pinned reader so a planted symlink yields {} (regex still masks)."""
    try:
        dfd = _open_dir_fd(d)
    except OSError:
        return {}
    try:
        raw = _read_env_local_bytes(dfd)
    except ValueError:
        return {}
    finally:
        os.close(dfd)
    out = {}
    for ln in raw.split(b"\n"):
        s = ln.strip()
        if not s or s.startswith(b"#") or b"=" not in s:
            continue
        k, _, v = s.partition(b"=")
        k = k.replace(b"export ", b"").strip()
        v = v.strip()
        if len(v) >= 2 and v[:1] in (b"'", b'"') and v[-1:] == v[:1]:
            v = v[1:-1]
        try:
            kk, vv = k.decode("ascii"), v.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _ENV_KEY_RE.match(kk):
            out[kk] = vv
    return out


def _values_for_redaction(app_id: Optional[str]) -> list:
    if not app_id:
        return []
    try:
        d = _safe_app_dir(app_id)
    except Exception:
        return []
    vals = [(k, v) for k, v in _env_read_kv(d).items() if v and len(v) >= 4]
    vals.sort(key=lambda kv: len(kv[1]), reverse=True)   # longest first
    return vals


def _redact_log_line(line: str, values: list) -> str:
    for k, v in values:
        if v in line:
            line = line.replace(v, f"[REDACTED:{k}]")
    return _HIGH_ENTROPY.sub("[REDACTED]", line)


# --- config (settings -> env -> default) + live security status -------------
def config() -> dict:
    """Merged dev-preview config. Precedence: settings.json (admin UI) -> env ->
    code default. NOTE coupling for the UI: `proxy_port` is DEPLOYMENT-coupled
    (must match a published Compose port + needs a proxy restart — not a live
    UI write); `repos_root` is the CONTAINER scan path (the host bind dir is the
    deploy-level REPOS_HOST_DIR, a separate thing)."""
    s = {}
    try:
        from src import settings as _settings
        s = _settings.get_setting("dev_preview", {}) or {}
    except Exception:
        s = {}

    def _i(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    # Defensive coercion: settings.json can be hand-edited or backup-restored, so
    # never trust its shapes — a malformed app_allowlist/package_manager/repos_root
    # must not reach list_apps or the start template. (set_config validates writes;
    # this guards reads of an already-bad file.)
    def _allow(v):
        if isinstance(v, list):
            safe = sorted({x for x in v if isinstance(x, str) and _SAFE_APP_NAME.match(x)})
            return safe or None
        return None
    _rr = s.get("repos_root")
    _pm = s.get("package_manager")
    return {
        "repos_root": _rr if (isinstance(_rr, str) and _rr) else REPOS_ROOT,
        "enabled": bool(s.get("enabled", True)),
        "dev_port": _i(s.get("dev_port"), PREVIEW_PORT),
        "proxy_port": _i(s.get("proxy_port"), PROXY_PORT),
        "app_allowlist": _allow(s.get("app_allowlist")),    # coerced: list[safe-name] or None
        "package_manager": _pm if _pm in ("npm",) else "npm",
        # proxy_port + dev_port both need a Compose change + proxy restart to take
        # effect (the proxy binds them at startup), so they're display-only.
        "_deployment_coupled": ["proxy_port", "dev_port"],
        "_container_path_keys": ["repos_root"],
        "_editable": ["enabled", "app_allowlist", "package_manager"],   # runtime-safe UI writes
    }


# --- editable config + vault-map management (UI-writable; settings.json) ------
_CONFIG_EDITABLE = {"enabled", "app_allowlist", "package_manager"}
_CONFIG_READONLY = {"repos_root", "dev_port", "proxy_port"}   # deployment/restart-coupled
_SAFE_APP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _write_dev_preview_settings(dp: dict) -> None:
    from src import settings as _settings
    s = _settings.load_settings()
    s["dev_preview"] = dp
    _settings.save_settings(s)


def _read_dev_preview_settings() -> dict:
    try:
        from src import settings as _settings
        d = _settings.get_setting("dev_preview", {}) or {}
        return dict(d) if isinstance(d, dict) else {}
    except Exception:
        return {}


def _enabled() -> bool:
    return bool(config().get("enabled", True))


def set_config(updates: dict) -> dict:
    """Write the runtime-safe dev_preview config keys to settings.json. REFUSES
    the deployment-coupled keys (proxy_port/dev_port/repos_root) — those need a
    Compose change + restart, not a live UI write. Returns the merged config()."""
    if not isinstance(updates, dict):
        raise ValueError("invalid config payload")
    with _lock:                                       # serialize the read-modify-write
        cur = _read_dev_preview_settings()
        for k, v in updates.items():
            if k in _CONFIG_READONLY:
                raise ValueError(f"'{k}' is deployment-coupled — change it in Compose + restart, not here")
            if k not in _CONFIG_EDITABLE:
                raise ValueError(f"'{k}' is not an editable config key")
            if k == "enabled":
                cur["enabled"] = bool(v)
            elif k == "package_manager":
                if v not in ("npm",):
                    raise ValueError("only 'npm' is supported (for now)")
                cur["package_manager"] = "npm"
            elif k == "app_allowlist":
                if v in (None, "", []):
                    cur.pop("app_allowlist", None)
                elif isinstance(v, list) and all(isinstance(x, str) and _SAFE_APP_NAME.match(x) for x in v):
                    cur["app_allowlist"] = sorted(set(v))
                else:
                    raise ValueError("app_allowlist must be null or a list of app names")
        _write_dev_preview_settings(cur)
    logger.info("dev_preview: config updated (%s)", ", ".join(sorted(updates)))
    return config()


def vault_map_get(app_id: str) -> dict:
    """Full vault mapping for an app — LOCATORS only, never values. Powers the
    mapping editor UI."""
    _safe_app_dir(app_id)
    return {"app_id": app_id, "map": _vault_map(app_id), "sources": sorted(_VAULT_FETCHERS)}


def vault_map_set(app_id: str, key: str, mapping: dict) -> dict:
    """Add/update one vault mapping entry. Stores ONLY a strictly-validated
    locator (k3s ns/secret/key or a Vaultwarden item_id/field) — never a value."""
    _safe_app_dir(app_id)
    if not key or not _ENV_KEY_RE.match(key):
        raise ValueError("invalid key name")
    if key.upper() in _ENV_KEY_DENY:
        raise ValueError("that key name is not allowed")
    if not isinstance(mapping, dict):
        raise ValueError("invalid mapping")
    source = str(mapping.get("source") or "")
    if source not in _VAULT_FETCHERS:
        raise ValueError("source must be one of: " + ", ".join(sorted(_VAULT_FETCHERS)))
    loc = {"source": source}
    if source == "k3s":
        for f in ("ns", "secret", "key"):
            val = str(mapping.get(f) or "")
            if not _VAULT_TOKEN_RE.match(val):
                raise ValueError(f"k3s locator field '{f}' is empty or has invalid characters")
            loc[f] = val
    else:  # vaultwarden
        item = str(mapping.get("item_id") or "")
        field = str(mapping.get("field") or "password")
        if not _VAULT_TOKEN_RE.match(item) or not _VAULT_TOKEN_RE.match(field):
            raise ValueError("vaultwarden item_id/field is empty or has invalid characters")
        loc["item_id"] = item
        loc["field"] = field
    with _lock:                                       # serialize the read-modify-write
        cur = _read_dev_preview_settings()
        vm = dict(cur.get("vault_map") or {})
        appmap = dict(vm.get(app_id) or {})
        appmap[key] = loc
        vm[app_id] = appmap
        cur["vault_map"] = vm
        _write_dev_preview_settings(cur)
    logger.info("dev_preview: vault_map set %s/%s -> %s", os.path.basename(_safe_app_dir(app_id)), key, source)
    return {"ok": True, "key": key, "source": source}


def vault_map_delete(app_id: str, key: str) -> dict:
    _safe_app_dir(app_id)
    if not key or not _ENV_KEY_RE.match(key):
        raise ValueError("invalid key name")
    with _lock:                                       # serialize the read-modify-write
        cur = _read_dev_preview_settings()
        vm = dict(cur.get("vault_map") or {})
        appmap = dict(vm.get(app_id) or {})
        existed = appmap.pop(key, None) is not None
        if appmap:
            vm[app_id] = appmap
        else:
            vm.pop(app_id, None)
        cur["vault_map"] = vm
        _write_dev_preview_settings(cur)
    return {"ok": True, "key": key, "deleted": existed}


def _port_bind_scope(port: int) -> str:
    """Live: is `port` LISTENing on loopback only, on all interfaces, or not at
    all (inside this container)? Reads /proc/net/tcp(6)."""
    ph = format(port, "04X")
    loop = exposed = False
    for tcp in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(tcp) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if len(p) > 3 and p[3] == "0A":
                        addr, prt = p[1].rsplit(":", 1)
                        if prt.upper() != ph:
                            continue
                        if addr.strip("0") == "":           # 0.0.0.0 / ::
                            exposed = True
                        elif addr.upper() in ("0100007F", "00000000000000000000000001000000"):
                            loop = True
                        else:
                            exposed = True
        except OSError:
            pass
    return "all" if exposed else ("loopback" if loop else "none")


def _service_role_present(app_id) -> Optional[bool]:
    try:
        d = _safe_app_dir(app_id)
    except Exception:
        return None
    envf = os.path.join(d, ".env.local")
    if not os.path.isfile(envf):
        return False
    try:
        for ln in open(envf):
            if ln.startswith("SUPABASE_SERVICE_ROLE_KEY="):
                return len(ln.split("=", 1)[1].strip()) > 0
    except OSError:
        pass
    return False


def security_status() -> dict:
    """Live security posture for the read-only Security Status panel. 'enforced'
    items are FIXED in code (not UI toggles); the bind scope is a live /proc check."""
    cfg = config()
    running = status().get("running")
    app_id = running.get("app_id") if running else None
    bind = _port_bind_scope(cfg["dev_port"]) if running else "none"
    return {
        "dev_server_running": bool(running),
        "dev_port": cfg["dev_port"],
        "proxy_port": cfg["proxy_port"],
        "dev_port_bind_scope": bind,                    # live: loopback | all | none
        "dev_server_loopback_only": bind != "all",      # FAIL only if bound to all ifaces
        "dev_port_host_published": False,               # Compose config (NOT live-verified in-container)
        "service_role_present": _service_role_present(app_id) if app_id else None,
        "proxy_admin_gated": True,                      # enforced: require_admin_cookie + gate
        "proxy_csrf_guard": True,                       # enforced: Origin/Fetch-Metadata on unsafe+WS
        "frame_strip_only": True,                       # enforced: strips only XFO + CSP frame-ancestors
        # How each item is established — so the UI never overstates a guarantee.
        # 'live' = checked now; 'enforced' = code invariant; 'configured' = a
        # deploy-config fact this in-container process CANNOT verify live.
        "_proof": {
            "dev_server_loopback_only": "live",
            "service_role_present": "live",
            "dev_port_host_published": "configured",
            "proxy_admin_gated": "enforced",
            "proxy_csrf_guard": "enforced",
            "frame_strip_only": "enforced",
        },
    }
