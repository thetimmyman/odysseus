"""Generic versioned JSON config store: archive-before-write, append-only
publish_log.jsonl, and path-jailed rollback.

Imports nothing else from ``src`` (routing_policy imports this, so a back-edge
would be circular). Live files live under the data/ volume so in-app saves
survive redeploys; ``seed_if_missing`` copies the baked ``config/`` default.

Layout for domain ``d``:
  <data_root>/routing/<d>.json            -- live file (read by the app)
  <data_root>/routing/<d>_versions/       -- archived snapshots + publish_log
"""
import copy
import fcntl
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Optional

_log = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Per-domain publish locks so concurrent threadpool publishes can't interleave
# the archive -> write -> log section (lost updates, torn archives).
_locks_guard = threading.Lock()
_domain_locks: dict = {}


def _domain_lock(domain: str) -> threading.Lock:
    with _locks_guard:
        lk = _domain_locks.get(domain)
        if lk is None:
            lk = threading.Lock()
            _domain_locks[domain] = lk
        return lk


@contextmanager
def _publish_guard(domain: str):
    """Serialize publishes for a domain: a threading.Lock plus a best-effort
    fcntl.flock for multi-worker setups (some network mounts lack flock)."""
    tl = _domain_lock(domain)
    tl.acquire()
    lock_fh = None
    try:
        try:
            vdir = versions_dir(domain)
            os.makedirs(vdir, exist_ok=True)
            lock_fh = open(os.path.join(vdir, ".publish.lock"), "w")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            if lock_fh is not None:
                try:
                    lock_fh.close()
                except OSError:
                    pass
                lock_fh = None
        yield
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass
        tl.release()


def _atomic_write_json(path: str, obj: dict) -> None:
    """Atomically write ``obj`` as JSON (temp file, fsync, os.replace) so readers
    never see a truncated file. ``allow_nan=False`` fails loudly before replace
    instead of persisting NaN/Infinity."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; use 0644 so files written as root stay readable
        # by the non-root app user. These configs hold no secrets.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json_raw(path: str, raw: str) -> None:
    """Atomically write an already-serialized JSON string (for archives)."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # see _atomic_write_json — keep archives app-readable
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def data_root() -> str:
    """ODYSSEUS_DATA_DIR (read per call) or ``<repo-root>/data``. Duplicated from
    routing_workdir to avoid its heavy imports."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    if override:
        return os.path.realpath(override)
    return os.path.join(_REPO_ROOT, "data")


def live_path(domain: str) -> str:
    return os.path.join(data_root(), "routing", f"{domain}.json")


def versions_dir(domain: str) -> str:
    return os.path.join(data_root(), "routing", f"{domain}_versions")


def _read_live(domain: str) -> Optional[dict]:
    lp = live_path(domain)
    try:
        with open(lp) as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
        _log.warning("config_store: live file %s did not contain a JSON object; "
                     "caller will use its fallback", lp)
        return None
    except FileNotFoundError:
        return None
    except Exception:
        # Present but unreadable: never silent; callers may need to fail safe
        # in a specific direction (e.g. never raise a budget cap).
        _log.warning("config_store: live file %s exists but is unreadable; "
                     "caller will use its fallback", lp)
        return None


read_live = _read_live


def live_status(domain: str) -> str:
    """'missing' | 'ok' | 'unreadable', so callers can refuse to degrade a corrupt
    file to a permissive default (e.g. silently raising a spend cap)."""
    if not os.path.exists(live_path(domain)):
        return "missing"
    return "ok" if _read_live(domain) is not None else "unreadable"


def seed_if_missing(domain: str, baked_default_path: Optional[str] = None,
                    default_dict: Optional[dict] = None) -> None:
    """If the live file is absent, copy the baked ``config/`` default (or
    ``default_dict``) into place. Never raises."""
    lp = live_path(domain)
    if os.path.exists(lp):
        return
    data: Optional[dict] = None
    if baked_default_path and os.path.isfile(baked_default_path):
        try:
            with open(baked_default_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = None
    if data is None:
        data = dict(default_dict or {})
    try:
        _atomic_write_json(lp, data)
    except (OSError, ValueError):
        # A read-only volume must not crash the read path.
        pass


def read(domain: str, baked_default_path: Optional[str] = None,
         fallback_dict: Optional[dict] = None) -> dict:
    """Seed if missing, then load. A corrupt live file degrades to
    ``fallback_dict``; this read path never raises."""
    seed_if_missing(domain, baked_default_path, fallback_dict)
    d = _read_live(domain)
    if d is None:
        return copy.deepcopy(fallback_dict or {})
    return d


def publish(domain: str, new_dict: dict, actor: str,
            validate_fn: Optional[Callable[[dict], list]] = None) -> dict:
    """Validate, archive the current live file, write the new one, then append
    to publish_log.jsonl.

    ``validate_fn`` returns reasons; any reason raises ``ValueError`` before any
    write, leaving the live file untouched.
    """
    if not isinstance(new_dict, dict):
        raise ValueError(["config must be a JSON object"])
    if validate_fn is not None:
        reasons = validate_fn(new_dict)
        if reasons:
            raise ValueError(reasons)

    vdir = versions_dir(domain)
    lp = live_path(domain)
    os.makedirs(vdir, exist_ok=True)
    os.makedirs(os.path.dirname(lp), exist_ok=True)

    with _publish_guard(domain):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_name: Optional[str] = None
        if os.path.exists(lp):
            try:
                with open(lp) as f:
                    current_raw = f.read()
                try:
                    current_version = json.loads(current_raw).get("version", "unknown")
                except Exception:
                    current_version = "unknown"
                # Version in the filename answers "what was live before"; sanitized
                # so a hostile version can't inject path separators.
                safe_version = re.sub(r"[^A-Za-z0-9._]", "_", str(current_version))[:40]
                archive_name = f"{ts}-{safe_version}.json"
                _atomic_write_json_raw(os.path.join(vdir, archive_name), current_raw)
            except OSError:
                # An unreadable current file must not block publishing a good one.
                archive_name = None

        _atomic_write_json(lp, new_dict)

        log_path = os.path.join(vdir, "publish_log.jsonl")
        with open(log_path, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": actor or "unknown",
                "version": new_dict.get("version"),
                "archive": archive_name,
            }) + "\n")

    written = _read_live(domain)
    return written if written is not None else copy.deepcopy(new_dict)


def list_versions(domain: str) -> list:
    """Archived snapshots newest-first: ``[{archive_name, version, ts, actor}]``.
    ts/actor come from publish_log, else file mtime and None."""
    vdir = versions_dir(domain)
    if not os.path.isdir(vdir):
        return []

    # archive_name -> the publish-log row that created it.
    by_archive = {}
    log_path = os.path.join(vdir, "publish_log.jsonl")
    if os.path.isfile(log_path):
        try:
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    arc = e.get("archive")
                    if arc:
                        by_archive[arc] = e
        except OSError:
            pass

    out = []
    for name in os.listdir(vdir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(vdir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        stem = name[:-len(".json")]
        ts_part, _, version_part = stem.partition("-")
        row = by_archive.get(name, {})
        out.append({
            "archive_name": name,
            "version": version_part or "unknown",
            "ts": row.get("ts") or datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc).isoformat(),
            "actor": row.get("actor"),
        })
    out.sort(key=lambda e: e["archive_name"], reverse=True)
    return out


def rollback(domain: str, archive_name: str, actor: str,
             validate_fn: Optional[Callable[[dict], list]] = None) -> dict:
    """Re-publish an archived snapshot. The name is realpath-jailed inside
    versions_dir. Goes through publish(), so rollback itself is archived and logged."""
    if not archive_name or os.path.basename(archive_name) != archive_name:
        raise ValueError("invalid archive name")
    versions_root = os.path.realpath(versions_dir(domain))
    candidate = os.path.realpath(os.path.join(versions_root, archive_name))
    if candidate == versions_root or os.path.commonpath(
            [versions_root, candidate]) != versions_root:
        raise ValueError("invalid archive name")
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"no archived config named {archive_name!r}")
    with open(candidate) as f:
        archived = json.load(f)
    return publish(domain, archived, actor, validate_fn=validate_fn)
