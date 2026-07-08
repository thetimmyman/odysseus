"""src/config_store.py — generic versioned JSON config store.

Extracted from routing_policy.py's proven pattern (archive-before-write +
append-only publish_log.jsonl + realpath/commonpath-jailed rollback) as a
reusable, dependency-light module. Deliberately imports nothing from the rest
of ``src`` (in particular NOT routing_policy — that module imports the config
layer, and a back-edge would be circular) so any config domain can persist
through it without dragging in the routing/DB stack.

The LIVE file for every domain lives under the data/ volume (data_root(),
ODYSSEUS_DATA_DIR-aware), NOT the baked ``config/`` dir — so an in-app save
survives a redeploy. ``seed_if_missing`` copies the baked default on first
read, so behavior is unchanged on a fresh deploy.

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

# Per-domain in-process publish locks (a domain -> threading.Lock registry,
# itself guarded). Serializes concurrent publishes on the SAME domain so the
# read-current -> archive -> write-live -> log critical section can't interleave
# (two FastAPI threadpool threads racing left lost updates + torn archives). A
# cross-process fcntl.flock in _publish_guard covers the (future) >1-worker case.
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
    """Serialize publishes for a domain: an in-process threading.Lock plus a
    best-effort cross-process fcntl.flock on a lockfile in the versions dir (in
    case the app is ever run with >1 uvicorn worker). A filesystem without
    flock support (some network mounts) must not break publishing, so the flock
    is best-effort; the threading.Lock is the load-bearing one today."""
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
    """Write ``obj`` as pretty JSON to ``path`` ATOMICALLY: a same-dir temp file
    is fsync'd then ``os.replace``'d over the target. os.replace is atomic on
    POSIX, so a concurrent reader always opens either the whole old file or the
    whole new one — never the truncated file that ``open(path,'w')`` exposes
    mid-write (the fail-open race where a reader saw defaults). ``allow_nan``
    is False so a non-finite value fails LOUDLY here (before os.replace, so the
    live file is untouched) instead of persisting a bare NaN/Infinity token
    that only Python's json can read back."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json_raw(path: str, raw: str) -> None:
    """Atomically write an already-serialized JSON string (used to copy the
    outgoing live file into the archive). Same temp+fsync+os.replace as
    _atomic_write_json, so a concurrent reader (list_versions / rollback) can
    never open a half-written archive."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def data_root() -> str:
    """The harness data directory: ODYSSEUS_DATA_DIR when set (resolved per
    call so a monkeypatched/exported env is honored without re-import), else
    ``<repo-root>/data`` — the same convention as routing_workdir.data_root().
    Duplicated locally rather than imported to keep this module dependency
    light (routing_workdir drags in subprocess/git plumbing)."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    if override:
        return os.path.realpath(override)
    return os.path.join(_REPO_ROOT, "data")


def live_path(domain: str) -> str:
    """The live config file for a domain, under the persisted data/ volume."""
    return os.path.join(data_root(), "routing", f"{domain}.json")


def versions_dir(domain: str) -> str:
    """The append-only archive dir for a domain (snapshots + publish log)."""
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
        # Exists but unreadable/corrupt — never silent (a caller may need to
        # fail-safe in a specific direction, e.g. budget must not raise its cap).
        _log.warning("config_store: live file %s exists but is unreadable; "
                     "caller will use its fallback", lp)
        return None


# Public alias — same read, exposed so a caller can act on the distinction
# between a truly-absent file and a present-but-corrupt one (via live_status).
read_live = _read_live


def live_status(domain: str) -> str:
    """'missing' | 'ok' | 'unreadable'. Lets a caller that must fail-safe in a
    particular direction tell a truly-absent live file (fine to seed/default)
    from a present-but-corrupt one (where degrading to a permissive default may
    be the WRONG direction — e.g. silently raising a spend cap)."""
    if not os.path.exists(live_path(domain)):
        return "missing"
    return "ok" if _read_live(domain) is not None else "unreadable"


def seed_if_missing(domain: str, baked_default_path: Optional[str] = None,
                    default_dict: Optional[dict] = None) -> None:
    """On first read, if the live file is absent, copy the baked ``config/``
    default (or the passed default dict) into place so a fresh deploy behaves
    exactly as the baked config did. A corrupt/unreadable baked file falls
    through to ``default_dict`` (never raises)."""
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
        # A read-only volume must not crash the read path; the fallback in
        # read() still returns sane defaults. (ValueError only if a default
        # somehow carried a non-finite value — not our shipped configs.)
        pass


def read(domain: str, baked_default_path: Optional[str] = None,
         fallback_dict: Optional[dict] = None) -> dict:
    """Seed (if missing) then load the live config. A corrupt/unreadable live
    file degrades to ``fallback_dict`` — this is a read path that must never
    raise (mirrors routing_policy.load_policy's degrade-to-default contract)."""
    seed_if_missing(domain, baked_default_path, fallback_dict)
    d = _read_live(domain)
    if d is None:
        return copy.deepcopy(fallback_dict or {})
    return d


def publish(domain: str, new_dict: dict, actor: str,
            validate_fn: Optional[Callable[[dict], list]] = None) -> dict:
    """Validate → archive the current live file → write the new one → append
    to publish_log.jsonl.

    ``validate_fn(new_dict)`` returns a list of reasons; a non-empty list means
    invalid and this raises ``ValueError(reasons)`` BEFORE any write, so a
    rejected publish never touches the live file (fail-safe). Archive-before-
    overwrite means every publish is recoverable via rollback().
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

    # Serialize the whole archive -> write-live -> log sequence: concurrent
    # publishes on the same domain must not interleave (that lost updates and
    # left torn archives). The live write is atomic (temp + os.replace) so a
    # concurrent READER never snapshots a truncated file either.
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
                # Version in the filename so `ls` alone answers "what was live
                # before this publish"; sanitized so a hostile version string
                # can't smuggle path separators into the archive name.
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
    """Archived snapshots newest-first as
    ``[{archive_name, version, ts, actor}]``. ``actor``/``ts`` are joined from
    publish_log.jsonl (the publish that archived that file); when no log row
    matches, ``ts`` falls back to the file mtime and ``actor`` is None."""
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
    """Re-publish an archived snapshot. The archive name is realpath+commonpath
    jailed INSIDE versions_dir (symlinks included) so a traversal name can
    never read outside the archive dir. Rollback goes THROUGH publish(), so it
    itself archives the (bad) current file and lands in the publish log — a
    rollback is never an invisible state change. Mirrors
    routing_policy.rollback_policy exactly."""
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
