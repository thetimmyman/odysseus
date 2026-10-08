"""Durable store for `src.local_targets.TargetCapabilityReceipt`.

It only answers which receipt a profile currently has and whether it is valid.

Layout, under `<data_root>/target_capabilities/`:

    receipts.jsonl   append-only, one canonical JSON receipt per line
    current.json     deterministic index: profile_id -> {receipt_hash, ...}

Writes are atomic and every line carries its own hash. History is never
overwritten, the current receipt is recorded in the index, and any corruption
makes ``current()`` raise rather than silently skip a line.
"""
from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.constants import TARGET_CAPABILITY_ACTIVE_FILENAME, TARGET_CAPABILITY_LOCK_FILENAME
from src.local_targets import (
    TargetCapabilityReceipt, LocalTargetUnavailable, DEFAULT_HEALTH_TTL_S, _canonical_bytes,
    make_target_capability_receipt,
    target_capability_receipt_hash_is_valid)
from src.routing_workdir import data_root

STORE_DIRNAME = "target_capabilities"
RECEIPTS_FILENAME = "receipts.jsonl"
INDEX_FILENAME = "current.json"
ACTIVE_FILENAME = TARGET_CAPABILITY_ACTIVE_FILENAME
LOCK_FILENAME = TARGET_CAPABILITY_LOCK_FILENAME
#: Override so tests can use a throwaway store.
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
        self.active_path = os.path.join(self.directory, ACTIVE_FILENAME)

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
                except (TypeError, ValueError, KeyError, LocalTargetUnavailable) as exc:
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
        """The indexed receipt for a profile; an entry missing from the ledger is corruption."""
        index = self._read_index()
        wanted = str(index.get(profile_id, {}).get("receipt_hash") or "")
        receipts = self.entries()
        if wanted:
            for receipt in receipts:
                if receipt.receipt_hash == wanted:
                    if receipt.profile_id != profile_id:
                        raise CapabilityStoreError("index receipt belongs to another profile")
                    return receipt
            raise CapabilityStoreError(
                f"index points at {wanted} for {profile_id!r}, which is not in "
                f"{self.receipts_path} (missing from the ledger)")
        if any(r.profile_id == profile_id for r in receipts):
            raise CapabilityStoreError("profile has evidence but no current index authority")
        return None

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

        profile_id = str(self._read_active().get(host_id) or "")
        if not profile_id:
            if any(r.host_id == host_id and ROLE_INFERENCE in r.roles for r in self.entries()):
                raise CapabilityStoreError("host has evidence but no active profile authority")
            return None
        receipt = self.current(profile_id)
        if receipt is None or receipt.host_id != host_id or ROLE_INFERENCE not in receipt.roles:
            raise CapabilityStoreError("active profile does not resolve to this inference host")
        return receipt

    @contextmanager
    def _writer_lock(self):
        os.makedirs(self.directory, exist_ok=True)
        with open(os.path.join(self.directory, LOCK_FILENAME), "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_active(self) -> Dict[str, str]:
        if not os.path.exists(self.active_path):
            return {}
        try:
            with open(self.active_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (ValueError, OSError) as exc:
            raise CapabilityStoreError(f"active profile index unusable: {exc}") from exc
        if not isinstance(payload, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or not v
                for k, v in payload.items()):
            raise CapabilityStoreError("active profile index is malformed")
        return payload

    def activate_profile(self, host_id: str, profile_id: str, *,
                         expected_profile_id: str, current_identity_digest: str) -> None:
        """Explicit compare-and-swap activation, never newest-observation selection."""
        from src.local_targets import ROLE_INFERENCE
        with self._writer_lock():
            active = self._read_active()
            if str(active.get(host_id) or "") != expected_profile_id:
                raise CapabilityStoreError("active profile changed before activation")
            receipt = self.current(profile_id)
            if receipt is None or receipt.host_id != host_id or ROLE_INFERENCE not in receipt.roles:
                raise CapabilityStoreError("activation requires this host's inference receipt")
            if not current_identity_digest or receipt.qualification_state(
                    current_identity_digest=current_identity_digest) != "valid":
                raise CapabilityStoreError("activation requires fresh matching measured identity")
            active[host_id] = profile_id
            self._write_json(self.active_path, active)

    def deactivate_profile(self, host_id: str, *, expected_profile_id: str) -> None:
        """Revoke active authority without deleting measured receipt history.

        The explicit expected profile protects a concurrent replacement from
        rollback. A host with retained evidence and no active pointer remains
        fail-closed in current_for_host(), until explicitly reactivated.
        """
        if not host_id or not expected_profile_id:
            raise CapabilityStoreError("deactivation requires a host and expected active profile")
        with self._writer_lock():
            active = self._read_active()
            if active.get(host_id) != expected_profile_id:
                raise CapabilityStoreError("active profile changed before deactivation")
            receipt = self.current(expected_profile_id)
            if receipt is None or receipt.host_id != host_id:
                raise CapabilityStoreError("active profile does not resolve to this host")
            del active[host_id]
            self._write_json(self.active_path, active)

    def refresh_observation(self, profile_id: str, *, current_identity_digest: str,
                            health: str, checked_at: str) -> TargetCapabilityReceipt:
        """Refresh liveness without moving semantic observation or TTL; drift invalidates."""
        with self._writer_lock():
            receipt = self.current(profile_id)
            if receipt is None or not current_identity_digest:
                raise CapabilityStoreError("refresh requires a receipt and observed identity")
            state = receipt.qualification_state(current_identity_digest=current_identity_digest)
            payload = {**receipt.to_dict(), "health": health,
                       "health_checked_at": checked_at, "supersedes": receipt.receipt_hash}
            if state != "valid":
                payload["invalidation_reason"] = state
            refreshed = make_target_capability_receipt(**payload)
            self._append(refreshed, supersedes=receipt.receipt_hash)
            return self.current(profile_id)

    def renew_profile(self, receipt: TargetCapabilityReceipt, *, expected_profile_id: str,
                      expected_receipt_hash: str, current_identity_digest: str,
                      identity_checked_at: str) -> TargetCapabilityReceipt:
        """Apply newly measured qualification to the same active execution profile.

        The caller supplies independently collected material identity. Both active
        authority and current evidence are compared under the existing writer lock;
        renewal never selects a replacement profile or enlarges its qualified scope.
        """
        from src.local_targets import _parse_utc

        now = datetime.now(timezone.utc)
        checked = _parse_utc(identity_checked_at)
        if (not current_identity_digest or checked is None or checked > now
                or (now - checked).total_seconds() > DEFAULT_HEALTH_TTL_S):
            raise CapabilityStoreError("renewal requires freshly observed material identity")
        with self._writer_lock():
            now = datetime.now(timezone.utc)
            if checked > now or (now - checked).total_seconds() > DEFAULT_HEALTH_TTL_S:
                raise CapabilityStoreError("renewal requires freshly observed material identity")
            active = self._read_active()
            if not expected_profile_id or active.get(receipt.host_id) != expected_profile_id:
                raise CapabilityStoreError("active profile changed before renewal")
            current = self.current(expected_profile_id)
            if current is None or not expected_receipt_hash or current.receipt_hash != expected_receipt_hash:
                raise CapabilityStoreError("current receipt changed before renewal")
            if (receipt.profile_id != current.profile_id or receipt.host_id != current.host_id
                    or receipt.identity_digest() != current.identity_digest()
                    or current_identity_digest != receipt.identity_digest()):
                raise CapabilityStoreError("renewal cannot change the active material identity")
            if (receipt.qualification_ref != current.qualification_ref
                    or receipt.schema_version != current.schema_version
                    or receipt.qualification_disposition != current.qualification_disposition
                    or receipt.roles != current.roles
                    or not set(receipt.capabilities.measured).issubset(current.capabilities.measured)
                    or receipt.context.safe_working_context > current.context.safe_working_context
                    or receipt.context.semantic_verified_context > current.context.semantic_verified_context
                    or receipt.ttl_s > current.ttl_s or receipt.health_ttl_s > current.health_ttl_s):
                raise CapabilityStoreError("renewal cannot change the qualification anchor or enlarge scope")
            if (receipt.qualification_state(now=now, current_identity_digest=current_identity_digest) != "valid"
                    or receipt.health_state(now=now) != "live"):
                raise CapabilityStoreError("renewal requires a valid and live newly measured receipt")
            observed, previous = _parse_utc(receipt.observed_at), _parse_utc(current.observed_at)
            if (observed is None or previous is None or observed <= previous
                    or not receipt.context.safe_context_source
                    or receipt.context.safe_context_source == current.context.safe_context_source):
                raise CapabilityStoreError("renewal requires new semantic measurement evidence")
            self._append(receipt, supersedes=current.receipt_hash)
            return self.current(expected_profile_id)

    def status(self, *, host_id: str = "", specs=None, now=None) -> Dict[str, Any]:
        """Read stored clocks and refusal reasons without probing or creating files.

        Passing stored checks is distinct from dispatch authorization: current
        material identity and task-specific checks remain unobserved here.
        """
        from src.local_targets import (registered_targets, _parse_utc, ROLE_INFERENCE,
                                       CAP_READONLY_ANALYSIS, CAP_NATIVE_TOOLS)

        moment = now or datetime.now(timezone.utc)
        pool = tuple(registered_targets() if specs is None else specs)
        audit = self.verify()
        records = self.entries() if audit["ok"] else ()
        hosts = sorted({s.target_id for s in pool} | {r.host_id for r in records})
        if host_id:
            if host_id not in hosts:
                raise CapabilityStoreError("host is not registered and has no stored evidence")
            hosts = [host_id]
        by_host = {s.target_id: s for s in pool}
        rows = []
        for host in hosts:
            spec = by_host.get(host)
            receipt = self.current_for_host(host) if audit["ok"] else None
            qualification = receipt.qualification_state(now=moment) if receipt else "NO_RECEIPT"
            health = receipt.health_state(now=moment) if receipt else "UNKNOWN"
            non_inference = spec is not None and ROLE_INFERENCE not in spec.roles
            reason = "; ".join(audit["problems"]) if not audit["ok"] else ""
            if not reason and spec is None:
                reason = "host has no registered execution target"
            elif not reason and non_inference:
                reason = "registry does not give this host the inference role"
            elif not reason and not spec.qualification_ref:
                reason = "unqualified: no independently qualified profile for this host"
            if not reason and receipt is None:
                reason = "no persisted capability receipt"
            if not reason and receipt.schema_version == 2 and (
                    receipt.host_id != host or receipt.model.model_id != spec.model
                    or receipt.qualification_ref != spec.qualification_ref
                    or ROLE_INFERENCE not in receipt.roles
                    or receipt.runtime.endpoint_url != spec.endpoint):
                reason = "registered_profile_binding_mismatch"
            if not reason and qualification != "valid":
                reason = "receipt not qualified: " + qualification
            if not reason and health != "live":
                reason = "liveness not live: " + health
            def expires(value, ttl):
                parsed = _parse_utc(value)
                return (parsed + timedelta(seconds=ttl)).isoformat() if parsed else None
            # Informational role labels only. The evidence registry must not
            # depend on or invoke the routing/dispatch layer it supplies.
            measured = set(receipt.capabilities.measured) if receipt else set()
            analysis = bool(measured & {CAP_READONLY_ANALYSIS, "text_generation"})
            roles = ["approximate_analyst"] if analysis else []
            if analysis and CAP_NATIVE_TOOLS in measured:
                roles.append("approximate_implementer")
            rows.append({
                "host_id": host, "profile_id": receipt.profile_id if receipt else None,
                "receipt_hash": receipt.receipt_hash if receipt else None,
                "qualification": qualification, "health": health,
                "qualification_expires_at": expires(receipt.observed_at, receipt.ttl_s) if receipt else None,
                "health_expires_at": expires(receipt.health_checked_at or receipt.observed_at,
                                               receipt.health_ttl_s) if receipt else None,
                "safe_context_tokens": receipt.context.safe_working_context if receipt else None,
                "roles": list(roles), "refusal_reason": reason or "current material identity not observed",
                "status": ("NOT_AN_INFERENCE_TARGET" if non_inference else "REFUSED" if reason
                           else "READY_FOR_IDENTITY_CHECK"),
                "next_action": ("retain deterministic role" if non_inference else
                                "repair registry evidence" if not audit["ok"] else
                                "reconcile the registered profile binding" if reason in {
                                    "registered_profile_binding_mismatch", "host has no registered execution target"} else
                                "measure and explicitly register a profile" if receipt is None else
                                "requalify the installed profile" if qualification != "valid" else
                                "observe identity and refresh liveness" if health != "live" else
                                "observe current identity and check the requested task")})
        return {"ok": audit["ok"], "checked_at": moment.isoformat(), "audit": audit,
                "profiles": rows, "read_only": True, "provider_calls": 0,
                "current_material_identity_observed": False}

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
        try:
            index = self._read_index()
            active = self._read_active()
            from src.local_targets import ROLE_INFERENCE
            for host_id in set(active) | {r.host_id for r in receipts if ROLE_INFERENCE in r.roles}:
                self.current_for_host(host_id)
            for receipt in receipts:
                self.current(receipt.profile_id)
        except CapabilityStoreError as exc:
            report["ok"] = False
            report["problems"].append(str(exc))
            return report
        for profile_id, entry in sorted(index.items()):
            wanted = str(entry.get("receipt_hash") or "")
            if wanted and wanted not in known:
                report["ok"] = False
                report["problems"].append(
                    f"index {profile_id!r} -> {wanted} is missing from the ledger")
        return report


    def append(self, receipt: TargetCapabilityReceipt,
               *, supersedes: str = "") -> Dict[str, Any]:
        """Append a receipt and make it current; ``supersedes`` records what it replaced."""
        with self._writer_lock():
            return self._append(receipt, supersedes=supersedes)

    def _append(self, receipt: TargetCapabilityReceipt, *, supersedes: str = "") -> Dict[str, Any]:
        if not target_capability_receipt_hash_is_valid(receipt.to_dict()):
            raise CapabilityStoreError("refusing to store an invalid receipt hash")
        index = self._read_index()
        active = self._read_active()
        history = self.entries()
        existing = self.current(receipt.profile_id) if history else None
        if existing is not None and existing.receipt_hash != receipt.receipt_hash and not supersedes:
            raise CapabilityStoreError(
                "a new receipt for an existing profile must explicitly supersede "
                "the current receipt")
        if supersedes:
            predecessor = next((r for r in history
                                 if r.receipt_hash == str(supersedes)), None)
            if predecessor is None:
                raise CapabilityStoreError(
                    f"supersedes references missing receipt {supersedes}")
            if predecessor.profile_id != receipt.profile_id:
                raise CapabilityStoreError(
                    "a receipt may supersede only a receipt for the same profile")
            current = self.current(receipt.profile_id)
            if current is None or current.receipt_hash != predecessor.receipt_hash:
                raise CapabilityStoreError(
                    "supersedes must reference the profile's current receipt")
            if not receipt.invalidation_reason and receipt.qualification_state(
                    now=datetime.now(timezone.utc)) != "valid":
                raise CapabilityStoreError(
                    "a stale or invalid superseding receipt is not routable")
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
        if existing is not None and existing.receipt_hash != receipt.receipt_hash:
            # Only an explicit, validated supersession may advance authority.
            # This guard also protects stores created by older implementations.
            if not supersedes or existing.receipt_hash != str(supersedes):
                raise CapabilityStoreError(
                    "refusing to advance current authority without supersession")
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
        from src.local_targets import ROLE_INFERENCE
        if receipt.host_id not in active and ROLE_INFERENCE in receipt.roles:
            # Only the first recorded profile initializes authority. Later profiles
            # require explicit activation, even if newer or higher capability.
            if not any(r.host_id == receipt.host_id for r in history):
                active[receipt.host_id] = receipt.profile_id
                self._write_json(self.active_path, active)
        return entry

    def mark_invalidated(self, profile_id: str, reason: str,
                         *, now_iso: str = "") -> TargetCapabilityReceipt:
        """Append an invalidation receipt, so routing refuses on evidence, not a missing file."""
        current = self.current(profile_id)
        if current is None:
            raise CapabilityStoreError(f"no receipt for profile {profile_id!r}")
        invalidated = make_target_capability_receipt(
            **{**current.to_dict(), "invalidation_reason": str(reason),
               "observed_at": now_iso or current.observed_at,
               "supersedes": current.receipt_hash})
        self.append(invalidated, supersedes=current.receipt_hash)
        return invalidated

    def _read_index(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.index_path):
            return {}
        try:
            with open(self.index_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (ValueError, OSError) as exc:
            raise CapabilityStoreError(f"{self.index_path} is not valid JSON: {exc}")
        if not isinstance(payload, Mapping):
            raise CapabilityStoreError(f"{self.index_path} is not an object")
        if any(not isinstance(v, Mapping) or not v.get("receipt_hash")
               or v.get("profile_id") != k for k, v in payload.items()):
            raise CapabilityStoreError("current profile index contains malformed authority")
        return {str(k): dict(v) for k, v in payload.items()}

    def _write_index(self, index: Mapping[str, Mapping[str, Any]]) -> None:
        self._write_json(self.index_path, {k: dict(v) for k, v in sorted(index.items())})

    def _write_json(self, path: str, value: Mapping[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        payload = json.dumps(value,
                             indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, delete=False,
            prefix=".current-", suffix=".json")
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, path)
        except BaseException:
            handle.close()
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise


def store_from_env(directory: str = "") -> TargetCapabilityStore:
    return TargetCapabilityStore(directory or default_store_dir())
