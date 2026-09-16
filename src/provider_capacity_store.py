"""Authoritative append-only persistence for PS-640 capacity receipts."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from typing import Dict, Iterable, Mapping, Tuple

from src.provider_capacity import (
    CapacityError, CapacityState, EvidenceProvenance, ProviderCapacityReceipt,
    _sha256, capacity_receipt_from_dict, capacity_receipt_hash_is_valid,
    make_capacity_receipt,
)


class CapacityStoreError(RuntimeError):
    """The persisted capacity authority cannot be trusted."""


def _json_no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class ProviderCapacityStore:
    """Append-only history plus an authoritative, validated current index."""

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self.receipts_path = os.path.join(self.directory, "receipts.jsonl")
        self.index_path = os.path.join(self.directory, "current.json")

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
            return None
        index = self._read_index(required=True)
        entries = self.entries()
        active = self._authoritative_current(entries, index)
        return active.get(pool_id)

    def append(self, receipt: ProviderCapacityReceipt, *, supersedes: str = "") -> ProviderCapacityReceipt:
        existing = self.entries() if os.path.exists(self.receipts_path) else ()
        index = self._read_index(required=True) if existing else {}
        if any(r.receipt_hash == receipt.receipt_hash for r in existing):
            raise CapacityStoreError("duplicate receipt hash")
        active = self._authoritative_current(existing, index) if existing else {}
        if supersedes:
            prior = next((r for r in existing if r.receipt_hash == supersedes), None)
            if prior is None:
                raise CapacityStoreError("supersedes references a missing receipt")
            if prior.pool_id != receipt.pool_id:
                raise CapacityStoreError("cross-pool supersession is forbidden")
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
                   provenance: EvidenceProvenance) -> ProviderCapacityReceipt:
        """Append a pool-scoped invalidation record without deleting history."""
        entries = self.entries()
        target = next((r for r in entries if r.receipt_hash == receipt_hash), None)
        if target is None:
            raise CapacityStoreError("cannot invalidate a missing receipt")
        if not reason.strip():
            raise CapacityStoreError("invalidation reason must be non-empty")
        current = self.current(target.pool_id)
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
               "supersedes": ""})
        return self.append(invalidated, supersedes=receipt_hash)

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
        by_hash = {r.receipt_hash: r for r in entries}
        if len(by_hash) != len(entries):
            raise CapacityStoreError("duplicate receipt hash in history")
        superseded = {r.supersedes for r in entries if r.supersedes}
        active = {}
        for receipt in entries:
            if receipt.receipt_hash in superseded:
                continue
            if receipt.pool_id in active:
                raise CapacityStoreError(f"conflicting current receipts for {receipt.pool_id}")
            active[receipt.pool_id] = receipt
        if set(index) != set(active):
            raise CapacityStoreError("current index does not exactly cover active pools")
        for pool_id, item in index.items():
            if not isinstance(item, Mapping) or item.get("pool_id") != pool_id:
                raise CapacityStoreError("current index pool identity mismatch")
            wanted = str(item.get("receipt_hash") or "")
            if wanted not in by_hash or active[pool_id].receipt_hash != wanted:
                raise CapacityStoreError("current index does not point to authoritative receipt")
            if active[pool_id].invalidation_reason and active[pool_id].state != CapacityState.UNAVAILABLE:
                raise CapacityStoreError("invalidated receipt has inconsistent state")
        return active

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
        except BaseException:
            handle.close()
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise
