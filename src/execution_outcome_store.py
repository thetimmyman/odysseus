"""Create-only storage and explicit replay for canonical execution outcomes.

This module never discovers a corpus or chooses a latest record. Callers name
the private directory, byte bound, and exact historical hashes to replay.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from typing import Any

from src.execution_outcomes import (
    ExecutionOutcomeRecord,
    OutcomeError,
    validate_outcome_record,
)
from src.outcome_scorecard import aggregate_outcomes

WRITE_LOCK_TIMEOUT_SECONDS = 5.0
WRITE_LOCK_POLL_INTERVAL_SECONDS = 0.05
_HASH_LENGTH = 64
_HASH_CHARS = frozenset("0123456789abcdef")
_ANCHORED_LINK_SUPPORTED = (
    os.link in os.supports_dir_fd and os.link in os.supports_follow_symlinks
)


class ExecutionOutcomeStoreError(RuntimeError):
    """Base class for outcome-store refusal without exposing record contents."""


class OutcomeStorePathError(ExecutionOutcomeStoreError):
    """Caller path, hash, or byte-bound contract is invalid."""


class OutcomeStorePayloadError(ExecutionOutcomeStoreError):
    """Input bytes do not pass the canonical outcome validator."""


class OutcomeStoreNotFoundError(ExecutionOutcomeStoreError):
    """The exact requested object does not exist."""


class OutcomeStoreCorruptError(ExecutionOutcomeStoreError):
    """Stored bytes or filesystem metadata are invalid."""


class OutcomeStoreConflictError(ExecutionOutcomeStoreError):
    """An existing content-addressed name conflicts with requested bytes."""


class OutcomeStoreBusyError(ExecutionOutcomeStoreError):
    """The bounded writer lock deadline expired."""


class OutcomeStoreUnstableError(ExecutionOutcomeStoreError):
    """Object metadata changed during a read; its contents are unknown."""


class OutcomeStoreDurabilityError(ExecutionOutcomeStoreError):
    """A required fsync was not confirmed; persistence is not acknowledged."""


class OutcomeStoreIOError(ExecutionOutcomeStoreError):
    """A filesystem operation failed without a more specific classification."""


class OutcomeStorePartialReplayError(ExecutionOutcomeStoreError):
    """At least one explicitly selected record failed validation; no scorecard was returned."""


def _valid_hash(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == _HASH_LENGTH
            and all(char in _HASH_CHARS for char in value))


def _byte_bound(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= sys.maxsize:
        raise OutcomeStorePathError("max_record_bytes must be an explicit positive bounded integer")
    return value


def _validated_input(record: ExecutionOutcomeRecord | bytes, max_bytes: int) -> ExecutionOutcomeRecord:
    if not isinstance(record, (ExecutionOutcomeRecord, bytes)):
        raise OutcomeStorePayloadError("record must be a validated outcome record or canonical bytes")
    try:
        validated = validate_outcome_record(record)
    except (OutcomeError, TypeError, ValueError, OverflowError):
        raise OutcomeStorePayloadError("record failed canonical outcome validation") from None
    if len(validated.to_bytes()) > max_bytes:
        raise OutcomeStorePayloadError("canonical record exceeds the caller byte bound")
    return validated


def _open_directory(directory: os.PathLike[str] | str) -> int:
    if (not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_NONBLOCK", 0) or os.open not in os.supports_dir_fd
            or not _ANCHORED_LINK_SUPPORTED):
        raise OutcomeStorePathError("platform lacks required anchored no-follow filesystem operations")
    try:
        path = os.fspath(directory)
    except (TypeError, ValueError):
        raise OutcomeStorePathError("directory must be an explicit existing private directory") from None
    if not isinstance(path, str) or not path or "\x00" in path:
        raise OutcomeStorePathError("directory must be an explicit existing private directory")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise OutcomeStorePathError("cannot open caller-selected directory without following links") from None
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise OutcomeStoreIOError("cannot inspect caller-selected directory") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OutcomeStorePathError("store must be an owned real directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise OutcomeStorePathError("store directory must have private mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _lock(fd: int) -> None:
    deadline = time.monotonic() + WRITE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                raise OutcomeStoreIOError("cannot acquire bounded store lock") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OutcomeStoreBusyError("store remained busy beyond the bounded deadline") from None
            time.sleep(min(WRITE_LOCK_POLL_INTERVAL_SECONDS, remaining))


def _sync(fd: int, what: str) -> None:
    try:
        os.fsync(fd)
    except OSError:
        raise OutcomeStoreDurabilityError(f"required {what} synchronization failed") from None


def _write_all(fd: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        try:
            count = os.write(fd, remaining)
        except OSError:
            raise OutcomeStoreIOError("staging write failed") from None
        if count <= 0:
            raise OutcomeStoreIOError("staging write was incomplete")
        remaining = remaining[count:]


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON value")


def _metadata(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_bytes(fd: int, max_bytes: int) -> bytes:
    try:
        before = os.fstat(fd)
    except OSError:
        raise OutcomeStoreIOError("cannot inspect requested record") from None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
        raise OutcomeStoreCorruptError("stored object is not an owned single-link regular file")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise OutcomeStoreCorruptError("stored object must have private mode 0600")
    if before.st_size < 0 or before.st_size > max_bytes:
        raise OutcomeStoreCorruptError("stored object exceeds the caller byte bound")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            limit = 1 if total >= max_bytes else min(64 * 1024, max_bytes - total)
            block = os.read(fd, limit)
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > max_bytes:
                raise OutcomeStoreCorruptError("stored object exceeds the caller byte bound")
        after = os.fstat(fd)
    except OutcomeStoreCorruptError:
        raise
    except OSError:
        raise OutcomeStoreIOError("cannot read requested record") from None
    if _metadata(before) != _metadata(after) or total != after.st_size:
        raise OutcomeStoreUnstableError("record metadata changed during bounded read")
    return b"".join(chunks)


def _read_record_fd(directory_fd: int, raw_hash: str, max_bytes: int,
                    expected: bytes | None = None, *, sync_file: bool = False) -> ExecutionOutcomeRecord:
    name = f"{raw_hash}.json"
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        raise OutcomeStoreNotFoundError("requested historical record is absent") from None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise OutcomeStoreCorruptError("requested object is a symlink or directory") from None
        raise OutcomeStoreIOError("cannot open requested historical record") from None
    try:
        raw = _read_bytes(fd, max_bytes)
        try:
            decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_keys,
                                 parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise OutcomeStoreCorruptError("stored bytes are not strict JSON") from None
        if not isinstance(decoded, dict):
            raise OutcomeStoreCorruptError("stored record must be a JSON object")
        try:
            record = validate_outcome_record(raw)
        except (OutcomeError, TypeError, ValueError, OverflowError):
            raise OutcomeStoreCorruptError("stored record failed canonical outcome validation") from None
        if record.to_bytes() != raw:
            raise OutcomeStoreCorruptError("stored record is not canonical")
        if hashlib.sha256(raw).hexdigest() != raw_hash:
            raise OutcomeStoreCorruptError("stored bytes do not match requested content hash")
        if expected is not None and raw != expected:
            raise OutcomeStoreConflictError("content-addressed object differs from requested bytes")
        if sync_file:
            _sync(fd, "record file")
        return record
    finally:
        try:
            os.close(fd)
        except OSError:
            raise OutcomeStoreIOError("cannot close requested record") from None


def _sync_directory(fd: int) -> None:
    _sync(fd, "store directory")


def _try_cleanup(directory_fd: int, staging_name: str | None) -> bool:
    if staging_name is None:
        return True
    try:
        os.unlink(staging_name, dir_fd=directory_fd)
        _sync_directory(directory_fd)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def put_record(directory: os.PathLike[str] | str,
               record: ExecutionOutcomeRecord | bytes, *,
               max_record_bytes: int) -> ExecutionOutcomeRecord:
    """Durably add one canonical record, acknowledging only after all syncs."""
    bound = _byte_bound(max_record_bytes)
    validated = _validated_input(record, bound)
    raw = validated.to_bytes()
    raw_hash = hashlib.sha256(raw).hexdigest()
    directory_fd = _open_directory(directory)
    try:
        _lock(directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise

    stage_name: str | None = None
    stage_fd: int | None = None
    result: ExecutionOutcomeRecord | None = None
    failure: BaseException | None = None
    published = False
    try:
        try:
            try:
                result = _read_record_fd(directory_fd, raw_hash, bound, expected=raw,
                                         sync_file=True)
            except OutcomeStoreNotFoundError:
                pass
            if result is not None:
                _sync_directory(directory_fd)
            else:
                stage_name = f".{raw_hash}.{secrets.token_hex(16)}.tmp"
                try:
                    stage_fd = os.open(stage_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                       | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                       dir_fd=directory_fd)
                except OSError:
                    raise OutcomeStoreIOError("cannot create private staging object") from None
                try:
                    os.fchmod(stage_fd, 0o600)
                    stage_info = os.fstat(stage_fd)
                except OSError:
                    raise OutcomeStoreIOError("cannot secure private staging object") from None
                if (not stat.S_ISREG(stage_info.st_mode) or stage_info.st_uid != os.getuid()
                        or stat.S_IMODE(stage_info.st_mode) != 0o600 or stage_info.st_nlink != 1):
                    raise OutcomeStoreIOError("staging object failed private-file checks")
                _write_all(stage_fd, raw)
                _sync(stage_fd, "staged record file")
                try:
                    os.close(stage_fd)
                except OSError:
                    raise OutcomeStoreIOError("cannot close staged record") from None
                stage_fd = None
                object_name = f"{raw_hash}.json"
                try:
                    os.link(stage_name, object_name, src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd, follow_symlinks=False)
                    published = True
                except FileExistsError:
                    result = _read_record_fd(directory_fd, raw_hash, bound,
                                             expected=raw, sync_file=True)
                    # A competing equal-byte publisher may have linked the
                    # destination after our first lookup. Sync this directory
                    # entry before we can acknowledge the verified object.
                    _sync_directory(directory_fd)
                except OSError:
                    raise OutcomeStoreIOError("create-only object publication failed") from None
                if published:
                    _sync_directory(directory_fd)
                    try:
                        os.unlink(stage_name, dir_fd=directory_fd)
                        stage_name = None
                    except OSError:
                        raise OutcomeStoreIOError("cannot remove owned staging name") from None
                    _sync_directory(directory_fd)
                    result = _read_record_fd(directory_fd, raw_hash, bound,
                                             expected=raw, sync_file=True)
                    _sync_directory(directory_fd)
        except BaseException as exc:
            failure = exc
    finally:
        if stage_fd is not None:
            try:
                os.close(stage_fd)
            except OSError:
                if failure is None:
                    failure = OutcomeStoreIOError("cannot close private staging object")
        try:
            if not _try_cleanup(directory_fd, stage_name) and failure is None:
                failure = OutcomeStoreIOError("cannot clean private staging name")
        except OutcomeStoreDurabilityError as exc:
            if failure is None:
                failure = exc
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        except OSError:
            if failure is None:
                failure = OutcomeStoreIOError("cannot release bounded store lock")
        try:
            os.close(directory_fd)
        except OSError:
            if failure is None:
                failure = OutcomeStoreIOError("cannot close store directory")
    if failure is not None:
        raise failure
    if result is None:
        raise OutcomeStoreIOError("store operation ended without a validated record")
    return result


def get_record(directory: os.PathLike[str] | str, raw_record_hash: str, *,
               max_record_bytes: int) -> ExecutionOutcomeRecord:
    """Read exactly one named record; corruption is never repaired or retried."""
    bound = _byte_bound(max_record_bytes)
    if not _valid_hash(raw_record_hash):
        raise OutcomeStorePathError("raw_record_hash must be 64 lowercase hexadecimal characters")
    directory_fd = _open_directory(directory)
    try:
        return _read_record_fd(directory_fd, raw_record_hash, bound)
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            raise OutcomeStoreIOError("cannot close caller-selected directory") from None


def aggregate_stored_outcomes(directory: os.PathLike[str] | str,
                              record_hashes: list[str] | tuple[str, ...], *,
                              scoring_version: str, max_record_bytes: int) -> dict[str, Any]:
    """Replay only the explicit finite selection through the existing scorecard."""
    bound = _byte_bound(max_record_bytes)
    if not isinstance(record_hashes, (list, tuple)) or not record_hashes:
        raise OutcomeStorePathError("replay requires a nonempty explicit list or tuple of hashes")
    if any(not _valid_hash(value) for value in record_hashes):
        raise OutcomeStorePathError("replay contains a malformed record hash")
    if not isinstance(scoring_version, str) or not scoring_version.strip():
        raise OutcomeStorePathError("scoring_version must be explicit and nonempty")
    directory_fd = _open_directory(directory)
    records: list[ExecutionOutcomeRecord] = []
    try:
        for raw_hash in record_hashes:
            try:
                records.append(_read_record_fd(directory_fd, raw_hash, bound))
            except ExecutionOutcomeStoreError:
                raise OutcomeStorePartialReplayError(
                    "one or more explicitly selected records could not be validated") from None
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            raise OutcomeStoreIOError("cannot close caller-selected directory") from None
    try:
        return aggregate_outcomes(records, scoring_version=scoring_version)
    except OutcomeError:
        raise OutcomeStorePartialReplayError(
            "selected records conflict under the existing scorecard rules") from None
