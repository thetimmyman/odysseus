"""Create-only, content-addressed storage for historical offer receipts.

This store preserves immutable offer observations. It does not select offers,
rank providers, expose a current pointer, or grant routing authority.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Mapping
from typing import Any

from src.provider_model_offer import (
    OfferReceiptError,
    ProviderModelOfferReceipt,
    provider_model_offer_from_dict,
)

MAX_RECEIPT_BYTES = 1024 * 1024
WRITE_LOCK_TIMEOUT_SECONDS = 5.0
WRITE_LOCK_POLL_INTERVAL_SECONDS = 0.05
_HASH_LENGTH = 64
_HASH_CHARS = frozenset("0123456789abcdef")
_ANCHORED_LINK_SUPPORTED = (
    os.link in os.supports_dir_fd and os.link in os.supports_follow_symlinks
)


class ProviderModelOfferStoreError(RuntimeError):
    """Base error for untrusted or unavailable historical offer storage."""


class OfferStorePathError(ProviderModelOfferStoreError):
    """The caller-supplied directory or requested object name is not accepted."""


class OfferStorePayloadError(ProviderModelOfferStoreError):
    """The input is not an existing valid immutable offer receipt."""


class OfferStoreNotFoundError(ProviderModelOfferStoreError):
    """The requested receipt object does not exist."""


class OfferStoreCorruptError(ProviderModelOfferStoreError):
    """The stored object is unsafe, malformed, or not canonically sealed."""


class OfferStoreConflictError(ProviderModelOfferStoreError):
    """An existing content-addressed name contains different or invalid bytes."""


class OfferStoreDurabilityError(ProviderModelOfferStoreError):
    """Required file or directory synchronization failed; no success is acknowledged."""


class OfferStoreIOError(ProviderModelOfferStoreError):
    """A filesystem operation failed before the requested operation was acknowledged."""


class OfferStoreBusyError(ProviderModelOfferStoreError):
    """The caller-supplied store remained busy beyond the bounded wait."""


class OfferStoreReadUnstableError(ProviderModelOfferStoreError):
    """The observed receipt changed during a read, so its contents are unknown."""


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise OfferStorePayloadError("offer receipt cannot be encoded as canonical JSON") from exc


def _input_receipt(payload: Any) -> tuple[ProviderModelOfferReceipt, bytes]:
    if isinstance(payload, ProviderModelOfferReceipt):
        serialized: Mapping[str, Any] = payload.to_dict()
    elif isinstance(payload, Mapping):
        serialized = dict(payload)
    else:
        raise OfferStorePayloadError("payload must be a typed offer receipt or serialized mapping")
    try:
        receipt = provider_model_offer_from_dict(serialized)
    except (OfferReceiptError, KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise OfferStorePayloadError(f"invalid provider/model offer receipt: {exc}") from exc
    encoded = _canonical_bytes(receipt.to_dict())
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise OfferStorePayloadError("canonical offer receipt exceeds the store size limit")
    return receipt, encoded


def _valid_hash(receipt_hash: Any) -> bool:
    return (
        isinstance(receipt_hash, str)
        and len(receipt_hash) == _HASH_LENGTH
        and all(character in _HASH_CHARS for character in receipt_hash)
    )


def _open_directory(directory: os.PathLike[str] | str) -> int:
    if (not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_NONBLOCK", 0) or os.open not in os.supports_dir_fd
            or not _ANCHORED_LINK_SUPPORTED):
        raise OfferStorePathError("platform lacks required anchored no-follow filesystem operations")
    try:
        path = os.fspath(directory)
    except TypeError as exc:
        raise OfferStorePathError("directory must be an explicit path to an existing private directory") from exc
    if not isinstance(path, str) or not path:
        raise OfferStorePathError("directory must be an explicit path to an existing private directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path, flags)
    except OSError as exc:
        raise OfferStorePathError(f"cannot open caller-supplied store directory: {exc}") from exc
    try:
        try:
            metadata = os.fstat(directory_fd)
        except OSError as exc:
            raise OfferStoreIOError(f"cannot inspect caller-supplied store directory: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise OfferStorePathError("caller-supplied store path is not a directory")
        if metadata.st_mode & 0o077:
            raise OfferStorePathError("store directory permissions must be private")
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short write while staging offer receipt")
        remaining = remaining[written:]


def _fsync_fd(fd: int, description: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise OfferStoreDurabilityError(f"fsync failed for {description}: {exc}") from exc


def _lock_directory(fd: int) -> None:
    """Serialize store operations so our own hard-link churn cannot mimic edits."""
    deadline = time.monotonic() + WRITE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                raise OfferStoreIOError(f"cannot lock caller-supplied store directory: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfferStoreBusyError("caller-supplied store remained busy past the lock deadline") from exc
            time.sleep(min(WRITE_LOCK_POLL_INTERVAL_SECONDS, remaining))


def _unlock_directory(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        raise OfferStoreIOError(f"cannot unlock caller-supplied store directory: {exc}") from exc


def _open_locked_directory(directory: os.PathLike[str] | str) -> int:
    fd = _open_directory(directory)
    try:
        _lock_directory(fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _read_bounded(fd: int) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise OfferStoreCorruptError("receipt object is not a regular file")
    if before.st_mode & 0o077:
        raise OfferStoreCorruptError("receipt object permissions are not private")
    if before.st_size < 0 or before.st_size > MAX_RECEIPT_BYTES:
        raise OfferStoreCorruptError("receipt object exceeds the store size limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        block = os.read(fd, min(64 * 1024, MAX_RECEIPT_BYTES + 1 - total))
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if total > MAX_RECEIPT_BYTES:
            raise OfferStoreCorruptError("receipt object exceeds the store size limit")
    after = os.fstat(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise OfferStoreReadUnstableError("receipt object changed while being read; contents are unknown")
    if total != after.st_size:
        raise OfferStoreReadUnstableError("receipt object size changed while being read; contents are unknown")
    return b"".join(chunks)


def _read_object(
    directory_fd: int,
    receipt_hash: str,
    *,
    expected_bytes: bytes | None = None,
    sync_file: bool = False,
) -> ProviderModelOfferReceipt:
    name = f"{receipt_hash}.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise OfferStoreNotFoundError(f"offer receipt {receipt_hash} is not stored") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise OfferStoreCorruptError("receipt object is a symlink or non-regular path") from exc
        raise OfferStoreIOError(f"cannot open offer receipt {receipt_hash}: {exc}") from exc

    try:
        try:
            raw = _read_bounded(fd)
        except OfferStoreCorruptError:
            raise
        except OSError as exc:
            raise OfferStoreIOError(f"cannot read offer receipt {receipt_hash}: {exc}") from exc
        try:
            decoded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise OfferStoreCorruptError(f"receipt object is not strict JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise OfferStoreCorruptError("receipt object must be a JSON object")
        try:
            receipt = provider_model_offer_from_dict(decoded)
        except (OfferReceiptError, KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
            raise OfferStoreCorruptError(f"stored offer receipt failed typed validation: {exc}") from exc
        canonical = _canonical_bytes(receipt.to_dict())
        if raw != canonical:
            raise OfferStoreCorruptError("stored offer receipt bytes are not canonical JSON")
        if receipt.receipt_hash != receipt_hash:
            raise OfferStoreCorruptError("stored offer receipt hash does not match its requested filename")
        if expected_bytes is not None and raw != expected_bytes:
            raise OfferStoreConflictError("existing receipt hash contains different canonical bytes")
        if sync_file:
            _fsync_fd(fd, "stored receipt file")
        return receipt
    finally:
        os.close(fd)


def _cleanup_staging(directory_fd: int, staging_name: str | None) -> None:
    if staging_name is None:
        return
    try:
        os.unlink(staging_name, dir_fd=directory_fd)
    except OSError:
        # Failure paths may retain only this call's private staging name.
        pass


def put_receipt(
    directory: os.PathLike[str] | str,
    payload: ProviderModelOfferReceipt | Mapping[str, Any],
) -> ProviderModelOfferReceipt:
    """Atomically add one immutable receipt, returning it only after syncs succeed.

    `directory` must name an existing caller-selected private directory. The
    store never creates directories, chooses a default, or overwrites history.
    """
    receipt, canonical = _input_receipt(payload)
    directory_fd = _open_locked_directory(directory)
    staging_name: str | None = None
    stage_fd: int | None = None
    try:
        staging_name = f".{receipt.receipt_hash}.{secrets.token_hex(16)}.tmp"
        assert staging_name is not None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            stage_fd = os.open(staging_name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            raise OfferStoreIOError(f"cannot create private receipt staging file: {exc}") from exc
        try:
            _write_all(stage_fd, canonical)
            _fsync_fd(stage_fd, "staged receipt file")
        finally:
            os.close(stage_fd)
            stage_fd = None

        object_name = f"{receipt.receipt_hash}.json"
        try:
            os.link(
                staging_name,
                object_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            stored = _read_object(
                directory_fd, receipt.receipt_hash, expected_bytes=canonical, sync_file=True
            )
        except FileExistsError:
            stored = _read_object(
                directory_fd, receipt.receipt_hash, expected_bytes=canonical, sync_file=True
            )
        except OSError as exc:
            raise OfferStoreIOError(f"create-only receipt publication failed: {exc}") from exc

        _fsync_fd(directory_fd, "store directory after publication")
        try:
            os.unlink(staging_name, dir_fd=directory_fd)
            staging_name = ""
        except OSError as exc:
            raise OfferStoreIOError(f"cannot remove private staging file: {exc}") from exc
        _fsync_fd(directory_fd, "store directory after staging cleanup")
        return stored
    except ProviderModelOfferStoreError:
        _cleanup_staging(directory_fd, staging_name or None)
        raise
    except OSError as exc:
        _cleanup_staging(directory_fd, staging_name or None)
        raise OfferStoreIOError(f"offer receipt store operation failed: {exc}") from exc
    finally:
        try:
            _unlock_directory(directory_fd)
        finally:
            try:
                if stage_fd is not None:
                    os.close(stage_fd)
            finally:
                os.close(directory_fd)


def get_receipt(
    directory: os.PathLike[str] | str,
    receipt_hash: str,
) -> ProviderModelOfferReceipt:
    """Read and verify one exact receipt hash without following a leaf symlink."""
    if not _valid_hash(receipt_hash):
        raise OfferStorePathError("receipt_hash must be exactly 64 lowercase hexadecimal characters")
    directory_fd = _open_directory(directory)
    try:
        return _read_object(directory_fd, receipt_hash)
    finally:
        os.close(directory_fd)
