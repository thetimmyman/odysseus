"""Bounded automation usefulness scorecard attachment and fail-closed lifecycle control.

This module is strictly Odysseus-owned and performs no cross-product imports or
implicit I/O. Scorecard attachment enforces Cockpit's 256-entry / 64 KiB cap
and strict schema allowlist, safely omitting entries when unconfigured or when
an upstream source fails. Lifecycle control requests fail closed (503) unless
an explicit server-side control authority/store is configured, and never trust
caller-supplied actor, principal, grant, or authority fields.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import copy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

from src.constants import (
    AUTOMATION_CONTROL_ID_BYTES,
    AUTOMATION_CONTROL_KEY_BYTES,
    AUTOMATION_CONTROL_REASON_BYTES,
    AUTOMATION_CONTROL_VERSION_BYTES,
    AUTOMATION_USEFULNESS_EXPORT_MAX_BYTES,
    AUTOMATION_USEFULNESS_MAX_BYTES,
    AUTOMATION_USEFULNESS_MAX_ENTRIES,
    CANONICAL_INSPECTOR_RESPONSE_BYTES,
)

AUTOMATION_LIFECYCLE_STATES = frozenset({"active", "simplified", "paused", "retired"})
AUTOMATION_CONTROL_ACTIONS = frozenset({"simplify", "pause", "retire", "restore"})
AUTOMATION_CONTROL_ALLOWED_KEYS = frozenset({
    "automation_id",
    "action",
    "expected_version",
    "reason",
    "handle",
    "target_version",
    "idempotency_key",
})

_AUTOMATION_ID_RE = re.compile(r"^[^\s*/\\?#]{1,256}$")
_OPAQUE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_OUTCOME_NUMERIC_KEYS = (
    "total_n",
    "succeeded_n",
    "failed_n",
    "false_mismatch_n",
    "unknown_n",
    "verified_n",
    "accepted_n",
)
_FEEDBACK_NUMERIC_KEYS = ("useful", "wrong", "noisy", "unnecessary")
_EXTERNAL_CONTROL_RECEIPT_ACTIONS = {
    "simplify": "automation.simplified",
    "pause": "automation.paused",
    "retire": "automation.retired",
    "restore": "automation.restored",
}


class AutomationControlError(ValueError):
    """Structured refusal raised during automation control validation or execution."""

    status_code: int = 400
    code: str = "invalid_request"

    def __init__(self, detail: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_refusal(self, *, automation_id: str | None = None, action: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "status": "REFUSED",
            "code": self.code,
            "detail": self.detail,
        }
        if automation_id is not None:
            payload["automation_id"] = automation_id
        if action is not None:
            payload["action"] = action
        return payload


class AutomationControlValidationError(AutomationControlError):
    status_code = 400
    code = "invalid_request"


class AutomationControlUnauthorizedError(AutomationControlError):
    status_code = 403
    code = "unauthorized"


class AutomationControlNotFoundError(AutomationControlError):
    status_code = 404
    code = "not_found"


class AutomationControlConflictError(AutomationControlError):
    status_code = 409
    code = "version_conflict"


class AutomationControlUnavailableError(AutomationControlError):
    status_code = 503
    code = "unavailable"


@dataclass(frozen=True)
class ValidatedAutomationControlRequest:
    automation_id: str
    action: str
    expected_version: str
    reason: str
    idempotency_key: str
    handle: str | None = None
    target_version: str | None = None


def _is_finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_nonfinite(_token: str) -> Any:
    raise ValueError("non-finite JSON constant")


def validate_automation_id(raw: Any) -> str:
    if (
        type(raw) is not str
        or not raw.strip()
        or raw in (".", "..")
        or _AUTOMATION_ID_RE.fullmatch(raw) is None
        or len(raw.encode("utf-8")) > AUTOMATION_CONTROL_ID_BYTES
    ):
        raise AutomationControlValidationError(
            "A valid automation_id is required.",
            code="invalid_request",
        )
    return raw


def validate_automation_control_request(
    path_automation_id: Any,
    body: Any,
) -> ValidatedAutomationControlRequest:
    """Strictly validate an automation control request and reject forged authority fields."""
    automation_id = validate_automation_id(path_automation_id)
    if type(body) is not dict:
        raise AutomationControlValidationError(
            "Invalid automation control request body.",
            code="invalid_request",
        )

    unknown_keys = sorted(set(body) - AUTOMATION_CONTROL_ALLOWED_KEYS)
    if unknown_keys:
        raise AutomationControlValidationError(
            f"Unsupported or disallowed fields in automation control request: {', '.join(unknown_keys)}.",
            code="invalid_request",
        )

    if "automation_id" in body:
        if type(body["automation_id"]) is not str or body["automation_id"] != automation_id:
            raise AutomationControlValidationError(
                "Body automation_id must match path automation_id.",
                code="invalid_request",
            )

    action = body.get("action")
    if type(action) is not str or action not in AUTOMATION_CONTROL_ACTIONS:
        raise AutomationControlValidationError(
            "Control action must be one of simplify, pause, retire, or restore.",
            code="invalid_action",
        )

    expected_version = body.get("expected_version")
    if (
        type(expected_version) is not str
        or not expected_version.strip()
        or len(expected_version.encode("utf-8")) > AUTOMATION_CONTROL_VERSION_BYTES
    ):
        raise AutomationControlValidationError(
            "A non-empty expected_version (<= 512 bytes) is required.",
            code="invalid_expected_version",
        )

    reason = body.get("reason")
    if (
        type(reason) is not str
        or not reason.strip()
        or len(reason.encode("utf-8")) > AUTOMATION_CONTROL_REASON_BYTES
    ):
        raise AutomationControlValidationError(
            "A non-empty reason (<= 1024 bytes) is required.",
            code="invalid_reason",
        )

    handle: str | None = None
    if "handle" in body:
        raw_handle = body["handle"]
        if type(raw_handle) is not str or _OPAQUE_UUID_RE.fullmatch(raw_handle) is None:
            raise AutomationControlValidationError(
                "Attempt handle must be an opaque UUID.",
                code="invalid_request",
            )
        handle = raw_handle

    target_version: str | None = None
    if "target_version" in body:
        raw_target = body["target_version"]
        if (
            type(raw_target) is not str
            or not raw_target.strip()
            or len(raw_target.encode("utf-8")) > AUTOMATION_CONTROL_VERSION_BYTES
        ):
            raise AutomationControlValidationError(
                "target_version must be a non-empty string (<= 512 bytes) when provided.",
                code="invalid_request",
            )
        target_version = raw_target

    if "idempotency_key" in body:
        raw_key = body["idempotency_key"]
        if (
            type(raw_key) is not str
            or not raw_key.strip()
            or any(ord(ch) < 32 for ch in raw_key)
            or len(raw_key.encode("utf-8")) > AUTOMATION_CONTROL_KEY_BYTES
        ):
            raise AutomationControlValidationError(
                "idempotency_key must be a non-empty string (<= 128 bytes) when provided.",
                code="invalid_request",
            )
        idempotency_key = raw_key
    else:
        idempotency_key = str(uuid.uuid4())

    return ValidatedAutomationControlRequest(
        automation_id=automation_id,
        action=action,
        expected_version=expected_version,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        handle=handle,
        target_version=target_version,
    )


def sanitize_automation_usefulness_entry(raw: Any) -> tuple[dict[str, Any], list[str]] | None:
    """Validate and project one entry onto Cockpit's validAutomationUsefulness allowlist.

    Returns (sanitized_entry, valid_source_record_hashes) or None if invalid.
    """
    if not isinstance(raw, Mapping):
        return None

    automation_id = raw.get("automation_id")
    if (
        type(automation_id) is not str
        or not automation_id.strip()
        or len(automation_id.encode("utf-8")) > AUTOMATION_CONTROL_ID_BYTES
    ):
        return None

    lifecycle_state = raw.get("lifecycle_state")
    if type(lifecycle_state) is not str or lifecycle_state not in AUTOMATION_LIFECYCLE_STATES:
        return None

    reversible = raw.get("reversible")
    if type(reversible) is not bool:
        return None

    current_version_ref = raw.get("current_version_ref")
    if current_version_ref is not None and type(current_version_ref) is not str:
        return None

    prior_version_ref = raw.get("prior_version_ref")
    if prior_version_ref is not None and type(prior_version_ref) is not str:
        return None

    outcomes_raw = raw.get("outcomes")
    if not isinstance(outcomes_raw, Mapping) or not all(
        _is_finite_number(outcomes_raw.get(k)) for k in _OUTCOME_NUMERIC_KEYS
    ):
        return None
    outcomes = {k: outcomes_raw[k] for k in _OUTCOME_NUMERIC_KEYS}

    effort_raw = raw.get("effort")
    if (
        not isinstance(effort_raw, Mapping)
        or not _is_finite_number(effort_raw.get("interruptions_total"))
        or type(effort_raw.get("correction_minutes_status")) is not str
        or (
            effort_raw.get("correction_minutes_total") is not None
            and not _is_finite_number(effort_raw.get("correction_minutes_total"))
        )
        or not _is_finite_number(effort_raw.get("notification_useful_n"))
        or not _is_finite_number(effort_raw.get("notification_noisy_n"))
    ):
        return None
    effort = {
        "interruptions_total": effort_raw["interruptions_total"],
        "correction_minutes_status": effort_raw["correction_minutes_status"],
        "correction_minutes_total": effort_raw["correction_minutes_total"],
        "notification_useful_n": effort_raw["notification_useful_n"],
        "notification_noisy_n": effort_raw["notification_noisy_n"],
    }

    fb_raw = raw.get("feedback_counts")
    if not isinstance(fb_raw, Mapping) or not all(
        _is_finite_number(fb_raw.get(k)) for k in _FEEDBACK_NUMERIC_KEYS
    ):
        return None
    feedback_counts = {k: fb_raw[k] for k in _FEEDBACK_NUMERIC_KEYS}

    baseline_raw = raw.get("baseline_and_uncertainty")
    if (
        not isinstance(baseline_raw, Mapping)
        or not _is_string_list(baseline_raw.get("baseline_kinds"))
        or (
            baseline_raw.get("minutes_saved") is not None
            and not _is_finite_number(baseline_raw.get("minutes_saved"))
        )
        or not _is_string_list(baseline_raw.get("uncertainties"))
    ):
        return None
    baseline_and_uncertainty = {
        "baseline_kinds": list(baseline_raw["baseline_kinds"]),
        "minutes_saved": baseline_raw["minutes_saved"],
        "uncertainties": list(baseline_raw["uncertainties"]),
    }

    quota_raw = raw.get("subscription_quota")
    if (
        not isinstance(quota_raw, Mapping)
        or type(quota_raw.get("status")) is not str
        or not _is_string_list(quota_raw.get("pools"))
        or not _is_string_list(quota_raw.get("unit_names"))
        or not _is_finite_number(quota_raw.get("observed_n"))
        or not _is_finite_number(quota_raw.get("unknown_n"))
        or (
            quota_raw.get("total_units") is not None
            and not _is_finite_number(quota_raw.get("total_units"))
        )
    ):
        return None
    subscription_quota = {
        "status": quota_raw["status"],
        "pools": list(quota_raw["pools"]),
        "observed_n": quota_raw["observed_n"],
        "unknown_n": quota_raw["unknown_n"],
        "total_units": quota_raw["total_units"],
        "unit_names": list(quota_raw["unit_names"]),
    }

    api_cost_raw = raw.get("api_cost")
    if (
        not isinstance(api_cost_raw, Mapping)
        or type(api_cost_raw.get("status")) is not str
        or not _is_string_list(api_cost_raw.get("currencies"))
        or not _is_finite_number(api_cost_raw.get("observed_n"))
        or not _is_finite_number(api_cost_raw.get("unknown_n"))
        or (
            api_cost_raw.get("total_amount") is not None
            and not _is_finite_number(api_cost_raw.get("total_amount"))
        )
    ):
        return None
    api_cost = {
        "status": api_cost_raw["status"],
        "currencies": list(api_cost_raw["currencies"]),
        "observed_n": api_cost_raw["observed_n"],
        "unknown_n": api_cost_raw["unknown_n"],
        "total_amount": api_cost_raw["total_amount"],
    }

    effects_raw = raw.get("dispatched_effects")
    if not isinstance(effects_raw, list):
        return None
    dispatched_effects: list[dict[str, Any]] = []
    for eff in effects_raw:
        if (
            not isinstance(eff, Mapping)
            or type(eff.get("effect_id")) is not str
            or type(eff.get("target")) is not str
            or type(eff.get("native_status")) is not str
            or type(eff.get("dispatched_at")) is not str
        ):
            return None
        clean_eff: dict[str, Any] = {
            "effect_id": eff["effect_id"],
            "target": eff["target"],
            "native_status": eff["native_status"],
            "dispatched_at": eff["dispatched_at"],
        }
        if "receipt_ref" in eff:
            ref = eff["receipt_ref"]
            if ref is not None and type(ref) is not str:
                return None
            clean_eff["receipt_ref"] = ref
        dispatched_effects.append(clean_eff)

    raw_hashes = raw.get("source_record_hashes")
    source_hashes: list[str] = []
    if isinstance(raw_hashes, list):
        source_hashes = sorted({h for h in raw_hashes if _is_sha256_hex(h)})

    sanitized: dict[str, Any] = {
        "automation_id": automation_id,
        "lifecycle_state": lifecycle_state,
        "current_version_ref": current_version_ref,
        "prior_version_ref": prior_version_ref,
        "reversible": reversible,
        "outcomes": outcomes,
        "effort": effort,
        "feedback_counts": feedback_counts,
        "baseline_and_uncertainty": baseline_and_uncertainty,
        "subscription_quota": subscription_quota,
        "api_cost": api_cost,
        "dispatched_effects": dispatched_effects,
    }
    if isinstance(raw.get("dispatched_effects_total_n"), int) and not isinstance(
        raw.get("dispatched_effects_total_n"), bool
    ):
        sanitized["dispatched_effects_total_n"] = max(
            raw["dispatched_effects_total_n"], len(dispatched_effects)
        )
    return sanitized, source_hashes


def _preview_sanitized(
    items: list[tuple[dict[str, Any], list[str]]],
    limit: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    preview: list[dict[str, Any]] = []
    hashes: set[str] = set()
    for entry, entry_hashes in items[:limit]:
        card = dict(entry)
        effects = entry["dispatched_effects"]
        card["dispatched_effects_total_n"] = max(
            entry.get("dispatched_effects_total_n", len(effects)),
            len(effects),
        )
        card["dispatched_effects"] = effects[:limit]
        card["baseline_and_uncertainty"] = dict(
            entry["baseline_and_uncertainty"],
            uncertainties=entry["baseline_and_uncertainty"]["uncertainties"][:limit],
        )
        card["subscription_quota"] = dict(
            entry["subscription_quota"],
            pools=entry["subscription_quota"]["pools"][:limit],
            unit_names=entry["subscription_quota"]["unit_names"][:limit],
        )
        card["api_cost"] = dict(
            entry["api_cost"],
            currencies=entry["api_cost"]["currencies"][:limit],
        )
        preview.append(card)
        hashes.update(entry_hashes)
    return preview, hashes


class JsonExportAutomationUsefulnessSource:
    """Bounded read-only JSON export reader for automation usefulness entries."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = AUTOMATION_USEFULNESS_EXPORT_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    def project_scorecard(self) -> list[dict[str, Any]]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > self.max_bytes:
                raise ValueError("automation usefulness export is missing, non-regular, or oversized")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(fd, min(65536, self.max_bytes - total + 1))
                if not block:
                    break
                total += len(block)
                if total > self.max_bytes:
                    raise ValueError("automation usefulness export exceeds size limit")
                chunks.append(block)
        finally:
            os.close(fd)
        decoded = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_reject_nonfinite)
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, dict) and isinstance(decoded.get("automation_usefulness"), list):
            return decoded["automation_usefulness"]
        raise ValueError("automation usefulness export must decode to a list or object with automation_usefulness")


def load_automation_usefulness_entries(
    source: Any,
    *,
    base_scorecard: Mapping[str, Any] | None = None,
) -> list[tuple[dict[str, Any], list[str]]] | None:
    """Safely load and validate entries from a configured store, export file, callable, or sequence.

    Returns None on unconfigured or failing/malformed input so the scorecard never crashes.
    """
    if source is None:
        return None
    try:
        raw_entries: Any
        if isinstance(source, (str, os.PathLike)):
            raw_entries = JsonExportAutomationUsefulnessSource(source).project_scorecard()
        elif hasattr(source, "project_scorecard") and callable(source.project_scorecard):
            raw_entries = source.project_scorecard()
        elif (
            base_scorecard is not None
            and hasattr(source, "attach_to_od19_scorecard")
            and callable(source.attach_to_od19_scorecard)
        ):
            attached = source.attach_to_od19_scorecard(copy.deepcopy(dict(base_scorecard)))
            if not isinstance(attached, Mapping):
                return None
            raw_entries = attached.get("automation_usefulness")
        elif callable(source):
            raw_entries = source()
        elif isinstance(source, (list, tuple)):
            raw_entries = list(source)
        else:
            return None

        if not isinstance(raw_entries, list):
            return None

        sanitized: list[tuple[dict[str, Any], list[str]]] = []
        for item in raw_entries:
            cleaned = sanitize_automation_usefulness_entry(item)
            if cleaned is None:
                return None
            sanitized.append(cleaned)
        return sanitized
    except Exception:
        return None


def attach_automation_usefulness(
    base_scorecard: Mapping[str, Any],
    entries: Iterable[Mapping[str, Any]] | list[tuple[dict[str, Any], list[str]]],
    *,
    max_entries: int = AUTOMATION_USEFULNESS_MAX_ENTRIES,
    max_bytes: int = AUTOMATION_USEFULNESS_MAX_BYTES,
) -> dict[str, Any]:
    """Attach bounded, Cockpit-valid automation_usefulness entries onto an OD-19 scorecard.

    Preserves ranking_allowed=False, caps to max_entries (256) and max_bytes (64 KiB),
    and fails closed by returning an untouched copy of base_scorecard if inputs are malformed.
    """
    if (
        not isinstance(base_scorecard, Mapping)
        or base_scorecard.get("schema") != "execution-outcome-scorecard-v1"
        or base_scorecard.get("ranking_allowed") is not False
    ):
        return copy.deepcopy(dict(base_scorecard)) if isinstance(base_scorecard, Mapping) else {}

    sanitized: list[tuple[dict[str, Any], list[str]]] = []
    try:
        for item in entries:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], dict)
                and isinstance(item[1], list)
            ):
                cleaned = sanitize_automation_usefulness_entry(item[0])
                if cleaned is None:
                    return copy.deepcopy(dict(base_scorecard))
                merged_hashes = sorted(
                    set(cleaned[1]) | {h for h in item[1] if _is_sha256_hex(h)}
                )
                sanitized.append((cleaned[0], merged_hashes))
            else:
                cleaned = sanitize_automation_usefulness_entry(item)
                if cleaned is None:
                    return copy.deepcopy(dict(base_scorecard))
                sanitized.append(cleaned)
    except Exception:
        return copy.deepcopy(dict(base_scorecard))

    base_copy = copy.deepcopy(dict(base_scorecard))
    base_hashes = (
        [h for h in base_copy.get("source_record_hashes", []) if isinstance(h, str)]
        if isinstance(base_copy.get("source_record_hashes"), list)
        else []
    )
    cap = max(0, min(int(max_entries), AUTOMATION_USEFULNESS_MAX_ENTRIES))
    limit = min(len(sanitized), cap) if sanitized else 0

    while True:
        preview, extra_hashes = _preview_sanitized(sanitized, limit)
        candidate = copy.deepcopy(base_copy)
        candidate["automation_usefulness_total_n"] = len(sanitized)
        candidate["automation_usefulness"] = preview
        if extra_hashes:
            candidate["source_record_hashes"] = sorted(set(base_hashes) | extra_hashes)
        try:
            size = len(_canonical_json_bytes(candidate))
        except (TypeError, ValueError):
            return base_copy
        if size <= max_bytes:
            return candidate
        if limit == 0:
            return base_copy
        limit //= 2


def attach_automation_usefulness_from_source(
    base_scorecard: Mapping[str, Any],
    source: Any,
    *,
    max_entries: int = AUTOMATION_USEFULNESS_MAX_ENTRIES,
    max_bytes: int = AUTOMATION_USEFULNESS_MAX_BYTES,
) -> dict[str, Any]:
    """Safely attach automation_usefulness from a configured source onto an OD-19 scorecard."""
    if source is None:
        return copy.deepcopy(dict(base_scorecard)) if isinstance(base_scorecard, Mapping) else {}
    loaded = load_automation_usefulness_entries(source, base_scorecard=base_scorecard)
    if loaded is None:
        return copy.deepcopy(dict(base_scorecard)) if isinstance(base_scorecard, Mapping) else {}
    return attach_automation_usefulness(
        base_scorecard,
        loaded,
        max_entries=max_entries,
        max_bytes=max_bytes,
    )


def fit_response_with_automation_usefulness(
    result: dict[str, Any],
    *,
    max_bytes: int = CANONICAL_INSPECTOR_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Shrink or drop automation_usefulness if needed so attaching it never overflows the response cap."""
    try:
        raw = json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return result
    if len(raw) <= max_bytes:
        return result

    routing = result.get("routing")
    outcomes = routing.get("outcomes") if isinstance(routing, dict) else None
    scorecard = outcomes.get("scorecard") if isinstance(outcomes, dict) else None
    if not isinstance(scorecard, dict) or "automation_usefulness" not in scorecard:
        return result

    entries = scorecard.get("automation_usefulness")
    if isinstance(entries, list) and entries:
        sanitized = []
        for entry in entries:
            cleaned = sanitize_automation_usefulness_entry(entry)
            if cleaned is None:
                sanitized = []
                break
            sanitized.append(cleaned)
        limit = len(sanitized) // 2
        while True:
            preview, _ = _preview_sanitized(sanitized, limit)
            scorecard["automation_usefulness"] = preview
            if len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")) <= max_bytes:
                return result
            if limit == 0:
                break
            limit //= 2

    scorecard.pop("automation_usefulness", None)
    scorecard.pop("automation_usefulness_total_n", None)
    return result


def _map_external_control_error(exc: Exception) -> AutomationControlError:
    if isinstance(exc, AutomationControlError):
        return exc
    name = type(exc).__name__
    if name == "UnauthorizedAutomationControlError" or isinstance(exc, PermissionError):
        return AutomationControlUnauthorizedError("Automation control authorization refused.")
    if name in ("ReversibilityError", "DispatchedEffectAccountingError"):
        return AutomationControlConflictError(
            "Automation lifecycle precondition or version conflict; re-read before retrying."
        )
    if isinstance(exc, LookupError):
        return AutomationControlNotFoundError("Automation not found.")
    if name == "AutomationUsefulnessError" or isinstance(exc, ValueError):
        return AutomationControlValidationError("Invalid automation control request.")
    return AutomationControlUnavailableError("Automation control backend is unavailable.")


def _normalize_control_ack(
    raw: Any,
    req: ValidatedAutomationControlRequest,
) -> dict[str, Any]:
    """Verify and normalize a delegated control result to match Cockpit's automationControlAck."""
    evidence_uri: str | None = None
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], Mapping):
        receipt_obj: Mapping[str, Any] = raw[0]
        if isinstance(raw[1], str):
            evidence_uri = raw[1]
        status = "APPLIED"
        receipt_hash = receipt_obj.get("receipt_hash")
        receipt_id = receipt_obj.get("receipt_id")
        observed_at = receipt_obj.get("observed_at")
        raw_action = receipt_obj.get("action")
        if raw_action is not None and raw_action not in (
            req.action,
            _EXTERNAL_CONTROL_RECEIPT_ACTIONS[req.action],
        ):
            raise AutomationControlUnavailableError("Automation control acknowledgement mismatch.")
    elif isinstance(raw, Mapping):
        if raw.get("ok") is False:
            raise AutomationControlUnavailableError("Automation control did not succeed.")
        status = raw.get("status")
        if status not in ("APPLIED", "REQUESTED"):
            raise AutomationControlUnavailableError("Automation control acknowledgement status is invalid.")
        nested = raw.get("receipt") if isinstance(raw.get("receipt"), Mapping) else None
        receipt_hash = raw.get("receipt_hash") or (nested.get("receipt_hash") if nested else None)
        receipt_id = nested.get("receipt_id") if nested else raw.get("receipt_id")
        observed_at = nested.get("observed_at") if nested else raw.get("observed_at")
        evidence_uri = (
            nested.get("evidence_uri") if nested and isinstance(nested.get("evidence_uri"), str)
            else raw.get("evidence_uri") if isinstance(raw.get("evidence_uri"), str)
            else None
        )
        if nested is not None:
            if nested.get("automation_id") is not None and nested.get("automation_id") != req.automation_id:
                raise AutomationControlUnavailableError("Automation control acknowledgement mismatch.")
            raw_action = nested.get("action")
            if raw_action is not None and raw_action not in (
                req.action,
                _EXTERNAL_CONTROL_RECEIPT_ACTIONS[req.action],
            ):
                raise AutomationControlUnavailableError("Automation control acknowledgement mismatch.")
        else:
            raw_action = None
    else:
        raise AutomationControlUnavailableError("Automation control acknowledgement is malformed.")

    if not _is_sha256_hex(receipt_hash):
        raise AutomationControlUnavailableError("Automation control acknowledgement lacks a valid receipt hash.")

    receipt_out: dict[str, Any] = {
        "automation_id": req.automation_id,
        "action": req.action,
        "receipt_hash": receipt_hash,
    }
    if isinstance(receipt_id, str) and receipt_id:
        receipt_out["receipt_id"] = receipt_id
    if isinstance(observed_at, str) and observed_at:
        receipt_out["observed_at"] = observed_at
    if isinstance(evidence_uri, str) and evidence_uri:
        receipt_out["evidence_uri"] = evidence_uri
    if isinstance(raw_action, str) and raw_action and raw_action != req.action:
        receipt_out["work_receipt_action"] = raw_action

    ack = {
        "ok": True,
        "status": status,
        "receipt_hash": receipt_hash,
        "receipt": receipt_out,
    }
    if len(_canonical_json_bytes(ack)) > CANONICAL_INSPECTOR_RESPONSE_BYTES:
        raise AutomationControlUnavailableError("Automation control acknowledgement exceeds response limit.")
    return ack


def delegate_automation_control(
    authority: Any,
    owner: Any,
    req: ValidatedAutomationControlRequest,
    *,
    work_item_key: str = "ODYSSEUS-INSPECTOR",
) -> dict[str, Any]:
    """Delegate a validated control request to a configured authority/store with fail-closed semantics."""
    if authority is None:
        raise AutomationControlUnavailableError(
            "Automation control authority is not configured; lifecycle controls fail closed."
        )

    try:
        if hasattr(authority, "apply_automation_control") and callable(authority.apply_automation_control):
            raw = authority.apply_automation_control(owner, req)
        elif hasattr(authority, "apply_control") and callable(authority.apply_control):
            auth_credential = None
            credential_for = getattr(authority, "credential_for", None)
            if callable(credential_for):
                auth_credential = credential_for(owner, req)
            raw = authority.apply_control(
                automation_id=req.automation_id,
                work_item_key=work_item_key,
                run_id=req.idempotency_key,
                action=req.action,
                reason=req.reason,
                auth_credential=auth_credential,
                target_version_ref=req.target_version,
                expected_version_ref=req.expected_version,
            )
        elif callable(authority):
            raw = authority(owner, req)
        else:
            raise AutomationControlUnavailableError(
                "Automation control authority is not configured; lifecycle controls fail closed."
            )
    except Exception as exc:
        raise _map_external_control_error(exc) from None

    return _normalize_control_ack(raw, req)
