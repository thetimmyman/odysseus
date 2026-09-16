"""src/target_capability_store.py — durable persistence for PS-632 receipts.

This is the STORE for `src.local_targets.TargetCapabilityReceipt`, not a second
registry and not a second evidence ledger:

* the registry (`src/local_targets.py`) MEASURES and defines what a receipt is;
* this module only keeps those receipts and answers one question for routing:
  *which receipt does this profile currently have, and is it still valid?*
* PS-638's evidence layer REFERS to a receipt hash; it never reads these files, and
  nothing here writes execution evidence.

Layout, under `<data_root>/target_capabilities/`:

    receipts.jsonl   append-only, one canonical JSON receipt per line
    current.json     deterministic index: profile_id -> {receipt_hash, ...}

Both files are written atomically (temp file + ``os.replace`` for the index, append
+ ``fsync`` for the ledger) and every line carries its own hash, so:

* **history is never overwritten** — a superseded receipt stays readable;
* **the current receipt is deterministic** — newest ``observed_at`` wins, ties go to
  the later line, and the choice is recorded in the index rather than inferred from
  file order by each reader;
* **corruption fails closed** — an unparseable line, a hash that does not cover its
  own content, or an index entry whose receipt is absent makes ``current()`` raise
  instead of quietly returning nothing. A store that silently skips a bad line is how
  "the capability was not measured" becomes "route anyway".
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.local_targets import (
    TargetCapabilityReceipt, _canonical_bytes, make_target_capability_receipt,
    target_capability_receipt_hash_is_valid)
from src.routing_workdir import data_root

STORE_DIRNAME = "target_capabilities"
RECEIPTS_FILENAME = "receipts.jsonl"
INDEX_FILENAME = "current.json"
#: An explicit override, so a test or a harness can point at a throwaway store
#: without touching the operator's real capability history.
STORE_ENV = "PS632_CAPABILITY_STORE"


class CapabilityStoreError(RuntimeError):
    """A store that cannot be trusted. Raised instead of returning a partial view."""


def default_store_dir() -> str:
    return os.environ.get(STORE_ENV) or os.path.join(data_root(), STORE_DIRNAME)


class TargetCapabilityStore:
    """Append-only, hash-verified capability receipts for one registry."""

    def __init__(self, directory: str = ""):
        self.directory = os.path.abspath(directory or default_store_dir())
        self.receipts_path = os.path.join(self.directory, RECEIPTS_FILENAME)
        self.index_path = os.path.join(self.directory, INDEX_FILENAME)

    # ------------------------------------------------------------- reading ---
    def entries(self) -> Tuple[TargetCapabilityReceipt, ...]:
        """Every stored receipt, oldest first. Corrupt content raises."""
        if not os.path.exists(self.receipts_path):
            return ()
        receipts: List[TargetCapabilityReceipt] = []
        with open(self.receipts_path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except ValueError as exc:
                    raise CapabilityStoreError(
                        f"{self.receipts_path}:{number} is not valid JSON: {exc}")
                if not isinstance(payload, Mapping):
                    raise CapabilityStoreError(
                        f"{self.receipts_path}:{number} is not a receipt object")
                if not str(payload.get("receipt_hash") or ""):
                    raise CapabilityStoreError(
                        f"{self.receipts_path}:{number} has no receipt_hash")
                if not target_capability_receipt_hash_is_valid(payload):
                    raise CapabilityStoreError(
                        f"{self.receipts_path}:{number} receipt_hash does not cover "
                        "its own content")
                try:
                    receipt = make_target_capability_receipt(**dict(payload))
                except (TypeError, ValueError, KeyError) as exc:
                    raise CapabilityStoreError(
                        f"{self.receipts_path}:{number} is not a valid receipt: {exc}")
                if receipt.receipt_hash != str(payload.get("receipt_hash") or ""):
                    raise CapabilityStoreError(
                        f"{self.receipts_path}:{number} does not round-trip: "
                        f"stored {str(payload.get('receipt_hash'))[:16]} but rebuilt "
                        f"{receipt.receipt_hash[:16]}")
                receipts.append(receipt)
        return tuple(receipts)

    def entries_for_host(self, host_id: str) -> Tuple[TargetCapabilityReceipt, ...]:
        return tuple(r for r in self.entries() if r.host_id == host_id)

    def current(self, profile_id: str) -> Optional[TargetCapabilityReceipt]:
        """The newest receipt for a profile, verified against the index.

        The index is how a reader learns WHICH receipt routing used without
        re-deriving "newest" from a file that may have grown since. An index entry
        whose receipt is missing from the ledger is corruption, not absence.
        """
        index = self._read_index()
        wanted = str(index.get(profile_id, {}).get("receipt_hash") or "")
        receipts = self.entries()
        if wanted:
            for receipt in receipts:
                if receipt.receipt_hash == wanted:
                    return receipt
            raise CapabilityStoreError(
                f"index points at {wanted} for {profile_id!r}, which is not in "
                f"{self.receipts_path}")
        candidates = [r for r in receipts if r.profile_id == profile_id]
        if not candidates:
            return None
        return max(candidates, key=lambda r: (r.observed_at, r.receipt_hash))

    def current_all(self) -> Dict[str, TargetCapabilityReceipt]:
        """Every profile's current receipt, keyed by profile id."""
        profiles = {r.profile_id for r in self.entries()}
        out: Dict[str, TargetCapabilityReceipt] = {}
        for profile_id in sorted(profiles):
            receipt = self.current(profile_id)
            if receipt is not None:
                out[profile_id] = receipt
        return out

    def current_for_host(self, host_id: str) -> Optional[TargetCapabilityReceipt]:
        """The host's current INFERENCE profile, if it has one."""
        from src.local_targets import ROLE_INFERENCE

        candidates = [r for r in self.entries()
                      if r.host_id == host_id and ROLE_INFERENCE in (r.roles or ())]
        if not candidates:
            return None
        newest = max(candidates, key=lambda r: (r.observed_at, r.receipt_hash))
        return self.current(newest.profile_id)

    def verify(self) -> Dict[str, Any]:
        """A full audit: every line hashes, and every index entry resolves."""
        report: Dict[str, Any] = {"ok": True, "receipts": 0, "profiles": [],
                                  "problems": []}
        try:
            receipts = self.entries()
        except CapabilityStoreError as exc:
            return {"ok": False, "receipts": 0, "profiles": [], "problems": [str(exc)]}
        report["receipts"] = len(receipts)
        report["profiles"] = sorted({r.profile_id for r in receipts})
        known = {r.receipt_hash for r in receipts}
        for profile_id, entry in sorted(self._read_index().items()):
            wanted = str(entry.get("receipt_hash") or "")
            if wanted and wanted not in known:
                report["ok"] = False
                report["problems"].append(
                    f"index {profile_id!r} -> {wanted} is missing from the ledger")
        return report


    # ------------------------------------------------------------- writing ---
    def append(self, receipt: TargetCapabilityReceipt,
               *, supersedes: str = "") -> Dict[str, Any]:
        """Store a receipt and make it current for its profile. Never overwrites.

        ``supersedes`` is recorded for audit: a reader can see that a profile's
        qualification was REPLACED (and by what) instead of merely observing that
        the newest line changed.
        """
        if supersedes:
            receipt = make_target_capability_receipt(
                **{**receipt.to_dict(), "supersedes": supersedes})
        os.makedirs(self.directory, exist_ok=True)
        if not str(receipt.receipt_hash or ""):
            raise CapabilityStoreError("refusing to store an unsealed receipt")
        line = json.dumps(json.loads(_canonical_bytes(receipt.to_dict())),
                          sort_keys=True, ensure_ascii=False)
        with open(self.receipts_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        index = self._read_index()
        entry: Dict[str, Any] = {
            "receipt_hash": receipt.receipt_hash, "profile_id": receipt.profile_id,
            "host_id": receipt.host_id, "observed_at": receipt.observed_at,
            "identity_digest": receipt.identity_digest(),
            "model_digest": receipt.model.digest}
        previous = index.get(receipt.profile_id) or {}
        if previous and previous.get("receipt_hash") != receipt.receipt_hash:
            entry["previous_receipt_hash"] = previous.get("receipt_hash", "")
        index[receipt.profile_id] = entry
        self._write_index(index)
        return entry

    def mark_invalidated(self, profile_id: str, reason: str,
                         *, now_iso: str = "") -> TargetCapabilityReceipt:
        """Record that a profile's current qualification no longer applies.

        Nothing is deleted and no new capability is invented: the store appends a
        receipt carrying the typed invalidation reason, so routing refuses on
        EVIDENCE rather than on the absence of a file.
        """
        current = self.current(profile_id)
        if current is None:
            raise CapabilityStoreError(f"no receipt for profile {profile_id!r}")
        invalidated = make_target_capability_receipt(
            **{**current.to_dict(), "invalidation_reason": str(reason),
               "observed_at": now_iso or current.observed_at,
               "supersedes": current.receipt_hash})
        self.append(invalidated, supersedes=current.receipt_hash)
        return invalidated

    # ------------------------------------------------------------ internals ---
    def _read_index(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.index_path):
            return {}
        try:
            with open(self.index_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except ValueError as exc:
            raise CapabilityStoreError(f"{self.index_path} is not valid JSON: {exc}")
        if not isinstance(payload, Mapping):
            raise CapabilityStoreError(f"{self.index_path} is not an object")
        return {str(k): dict(v) for k, v in payload.items() if isinstance(v, Mapping)}

    def _write_index(self, index: Mapping[str, Mapping[str, Any]]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        payload = json.dumps({k: dict(v) for k, v in sorted(index.items())},
                             indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, delete=False,
            prefix=".current-", suffix=".json")
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, self.index_path)
        except BaseException:
            handle.close()
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise


def store_from_env(directory: str = "") -> TargetCapabilityStore:
    return TargetCapabilityStore(directory or default_store_dir())
