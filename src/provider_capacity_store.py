"""Authoritative append-only persistence for PS-640 capacity receipts."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
import fcntl
from dataclasses import replace
from typing import Dict, Iterable, Mapping, Tuple

from src.provider_capacity import (
    CapacityError, CapacityState, EvidenceProvenance, ProviderCapacityReceipt,
    _sha256, capacity_receipt_from_dict, capacity_receipt_hash_is_valid,
    make_capacity_receipt, validated_current_receipts,
)


class CapacityStoreError(RuntimeError):
    """The persisted capacity authority cannot be trusted."""


class CapacityStoreBusyError(CapacityStoreError):
    """The single-writer authority lock could not be acquired."""


def _json_no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class ProviderCapacityStore:
    """Append-only history plus an authoritative, validated current index."""

    def __init__(self, directory: str, *, lock_timeout_seconds: float = 5.0):
        self.directory = os.path.abspath(directory)
        self.receipts_path = os.path.join(self.directory, "receipts.jsonl")
        self.index_path = os.path.join(self.directory, "current.json")
        self.lock_path = os.path.join(self.directory, "capacity.lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def entries(self) -> Tuple[ProviderCapacityReceipt, ...]:
        if not os.path.exists(self.receipts_path):
            return ()
        out = []
        with open(self.receipts_path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line, object_pairs_hook=_json_no_duplicate_pairs)
                    if not isinstance(payload, dict) or not capacity_receipt_hash_is_valid(payload):
                        raise ValueError("receipt hash mismatch")
                    out.append(capacity_receipt_from_dict(payload))
                except (ValueError, KeyError, TypeError, json.JSONDecodeError, CapacityError) as exc:
                    raise CapacityStoreError(f"{self.receipts_path}:{number}: {exc}") from exc
        return tuple(out)

    def history(self, pool_id: str = "") -> Tuple[ProviderCapacityReceipt, ...]:
        """Explicit historical access; this never supplies runtime authority."""
        entries = self.entries()
        return tuple(r for r in entries if not pool_id or r.pool_id == pool_id)

    def current(self, pool_id: str) -> ProviderCapacityReceipt | None:
        if not os.path.exists(self.receipts_path):
            if os.path.exists(self.index_path):
                raise CapacityStoreError("current index exists without receipt history")
            return None
        index = self._read_index(required=True)
        entries = self._entries_for_index(index)
        active = self._authoritative_current(entries, index)
        return active.get(pool_id)

    def append(self, receipt: ProviderCapacityReceipt, *, supersedes: str = "") -> ProviderCapacityReceipt:
        with self._mutation_lock():
            return self._append_locked(receipt, supersedes=supersedes)

    def _append_locked(self, receipt: ProviderCapacityReceipt, *, supersedes: str = "") -> ProviderCapacityReceipt:
        existing = self.entries() if os.path.exists(self.receipts_path) else ()
        index = self._read_index(required=True) if existing else self._read_index(required=False)
        if any(r.receipt_hash == receipt.receipt_hash for r in existing):
            raise CapacityStoreError("duplicate receipt hash")
        active = self._authoritative_current(existing, index) if existing else {}
        if supersedes:
            prior = next((r for r in existing if r.receipt_hash == supersedes), None)
            if prior is None:
                raise CapacityStoreError("supersedes references a missing receipt")
            if prior.pool_id != receipt.pool_id:
                raise CapacityStoreError("cross-pool supersession is forbidden")
            if prior.invalidation_reason:
                raise CapacityStoreError("an invalidated receipt cannot be superseded")
            if prior.receipt_hash == receipt.receipt_hash:
                raise CapacityStoreError("self-supersession is forbidden")
            if active.get(receipt.pool_id) is None or active[receipt.pool_id].receipt_hash != prior.receipt_hash:
                raise CapacityStoreError("only the authoritative current receipt may be superseded")
        elif receipt.pool_id in active:
            raise CapacityStoreError("replacement receipt must name supersedes")
        if receipt.supersedes and receipt.supersedes != supersedes:
            raise CapacityStoreError("receipt supersedes field does not match append request")
        if supersedes:
            core = {**receipt.core(), "supersedes": supersedes}
            receipt = replace(receipt, supersedes=supersedes, receipt_hash=_sha256(core))
        self._append_line(receipt)
        index = dict(index)
        index[receipt.pool_id] = {"pool_id": receipt.pool_id,
                                  "receipt_hash": receipt.receipt_hash,
                                  "observed_at": receipt.observed_at}
        self._write_index(index)
        return receipt

    def invalidate(self, receipt_hash: str, reason: str,
                   provenance: EvidenceProvenance, *, pool_id: str | None = None) -> ProviderCapacityReceipt:
        """Append a pool-scoped invalidation record without deleting history."""
        with self._mutation_lock():
            entries = self.entries()
            target = next((r for r in entries if r.receipt_hash == receipt_hash), None)
            if target is None:
                raise CapacityStoreError("cannot invalidate a missing receipt")
            if pool_id is not None and target.pool_id != pool_id:
                raise CapacityStoreError("cross-pool invalidation is forbidden")
            if not isinstance(reason, str) or not reason.strip():
                raise CapacityStoreError("invalidation reason must be non-empty")
            index = self._read_index(required=True)
            active = self._authoritative_current(entries, index)
            current = active.get(target.pool_id)
            if current is None or current.receipt_hash != receipt_hash:
                raise CapacityStoreError("only the authoritative current receipt may be invalidated")
            invalidated = make_capacity_receipt(
                **{**target.core(), "state": CapacityState.UNAVAILABLE,
                   "state_provenance": provenance,
                   "invalidation_reason": reason,
                   "observed_at": provenance.observed_at,
                   "ttl_seconds": provenance.ttl_seconds,
                   "evidence_source": provenance.source,
                   "evidence_reference": provenance.reference,
                   "collector_id": provenance.collector_id,
                   "supersedes": receipt_hash})
            self._append_line(invalidated)
            next_index = dict(index)
            next_index.pop(target.pool_id, None)
            self._write_index(next_index)
            return invalidated

    def verify(self) -> bool:
        try:
            entries = self.entries()
            index = self._read_index(required=bool(entries))
            self._authoritative_current(entries, index)
            return True
        except (CapacityStoreError, CapacityError, ValueError, KeyError, TypeError):
            return False

    def _authoritative_current(self, entries: Iterable[ProviderCapacityReceipt],
                               index: Mapping[str, Mapping[str, str]]) -> Dict[str, ProviderCapacityReceipt]:
        entries = tuple(entries)
        by_hash = {}
        counts = {}
        for receipt in entries:
            counts[receipt.receipt_hash] = counts.get(receipt.receipt_hash, 0) + 1
            by_hash[receipt.receipt_hash] = receipt
        active = {}
        for pool_id, item in index.items():
            if not isinstance(item, Mapping) or item.get("pool_id") != pool_id:
                raise CapacityStoreError("current index pool identity mismatch")
            wanted = item.get("receipt_hash")
            if not isinstance(wanted, str) or not wanted or wanted not in by_hash or counts[wanted] != 1:
                raise CapacityStoreError("current index does not point to authoritative receipt")
            receipt = by_hash[wanted]
            if receipt.pool_id != pool_id or receipt.invalidation_reason:
                raise CapacityStoreError("current index does not point to usable receipt")
            chain = []
            seen = set()
            cursor = receipt
            while True:
                if cursor.receipt_hash in seen:
                    raise CapacityStoreError("cycle in supersession history")
                seen.add(cursor.receipt_hash)
                chain.append(cursor)
                if not cursor.supersedes:
                    break
                target = by_hash.get(cursor.supersedes)
                if target is None:
                    raise CapacityStoreError("supersession target is missing")
                cursor = target
            validated = validated_current_receipts(chain)
            if len(validated) != 1 or validated[0].receipt_hash != wanted:
                raise CapacityStoreError("current index does not point to authoritative receipt")
            active[pool_id] = receipt
        return active

    def _entries_for_index(self, index: Mapping[str, Mapping[str, str]]) -> Tuple[ProviderCapacityReceipt, ...]:
        """Read valid rows for indexed resolution; trailing orphan rows are not authority."""
        if not os.path.exists(self.receipts_path):
            return ()
        out = []
        with open(self.receipts_path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line, object_pairs_hook=_json_no_duplicate_pairs)
                    if not isinstance(payload, dict) or not capacity_receipt_hash_is_valid(payload):
                        continue
                    out.append(capacity_receipt_from_dict(payload))
                except (ValueError, KeyError, TypeError, json.JSONDecodeError, CapacityError):
                    continue
        return tuple(out)

    @contextmanager
    def _mutation_lock(self):
        os.makedirs(self.directory, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.lock_timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CapacityStoreBusyError("capacity store writer lock is busy")
                    time.sleep(0.01)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _read_index(self, *, required: bool) -> Dict[str, dict]:
        if not os.path.exists(self.index_path):
            if required:
                raise CapacityStoreError("current index is missing")
            return {}
        try:
            with open(self.index_path, encoding="utf-8") as handle:
                data = json.load(handle, object_pairs_hook=_json_no_duplicate_pairs)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CapacityStoreError(f"current index is unreadable: {exc}") from exc
        if not isinstance(data, dict) or any(not isinstance(v, Mapping) for v in data.values()):
            raise CapacityStoreError("current index is not a map of pool entries")
        return {str(k): dict(v) for k, v in data.items()}

    def _append_line(self, receipt: ProviderCapacityReceipt) -> None:
        os.makedirs(self.directory, exist_ok=True)
        with open(self.receipts_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _write_index(self, index: Mapping[str, Mapping[str, str]]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.directory,
                                             prefix=".current-", delete=False)
        try:
            json.dump({k: dict(index[k]) for k in sorted(index)}, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, self.index_path)
            directory_fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            handle.close()
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise
