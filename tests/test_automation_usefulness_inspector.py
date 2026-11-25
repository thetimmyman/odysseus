"""Tests for TMOS-411: bounded automation_usefulness scorecard attachment and fail-closed control."""
from __future__ import annotations

import hashlib
import json
import math
import uuid

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from core.auth import AuthManager
from src.automation_usefulness import (
    AutomationControlConflictError,
    AutomationControlUnavailableError,
    JsonExportAutomationUsefulnessSource,
    attach_automation_usefulness,
    attach_automation_usefulness_from_source,
)
from src.canonical_inspector_service import InspectorService
from src.constants import (
    AUTOMATION_USEFULNESS_MAX_BYTES,
    AUTOMATION_USEFULNESS_MAX_ENTRIES,
    CANONICAL_INSPECTOR_RESPONSE_BYTES,
)
from tests.test_canonical_inspector_admission import (
    native_store as native_store,
    prepared,
    principal,
    publication,
)
from tests.test_canonical_inspector_routes import (
    client_for,
    retained_payload,
    retained_projection,
)

_LIFECYCLE_STATES = {"active", "simplified", "paused", "retired"}


def _assert_cockpit_valid_automation_usefulness(raw):
    """Exact Python mirror of Cockpit's validAutomationUsefulness() in routing-inspector.ts."""
    if raw is None:
        return
    assert isinstance(raw, list) and len(raw) <= 256

    def finite_num(v):
        return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)

    def str_list(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    for entry in raw:
        assert isinstance(entry, dict)
        assert isinstance(entry.get("automation_id"), str) and entry["automation_id"].strip()
        assert entry.get("lifecycle_state") in _LIFECYCLE_STATES
        assert type(entry.get("reversible")) is bool

        outcomes = entry.get("outcomes")
        assert isinstance(outcomes, dict)
        for key in (
            "total_n", "succeeded_n", "failed_n", "false_mismatch_n",
            "unknown_n", "verified_n", "accepted_n",
        ):
            assert finite_num(outcomes.get(key))

        effort = entry.get("effort")
        assert isinstance(effort, dict)
        assert finite_num(effort.get("interruptions_total"))
        assert isinstance(effort.get("correction_minutes_status"), str)
        assert effort.get("correction_minutes_total") is None or finite_num(effort.get("correction_minutes_total"))
        assert finite_num(effort.get("notification_useful_n"))
        assert finite_num(effort.get("notification_noisy_n"))

        fb = entry.get("feedback_counts")
        assert isinstance(fb, dict)
        for key in ("useful", "wrong", "noisy", "unnecessary"):
            assert finite_num(fb.get(key))

        baseline = entry.get("baseline_and_uncertainty")
        assert isinstance(baseline, dict)
        assert str_list(baseline.get("baseline_kinds"))
        assert baseline.get("minutes_saved") is None or finite_num(baseline.get("minutes_saved"))
        assert str_list(baseline.get("uncertainties"))

        quota = entry.get("subscription_quota")
        assert isinstance(quota, dict)
        assert isinstance(quota.get("status"), str)
        assert str_list(quota.get("pools"))
        assert str_list(quota.get("unit_names"))
        assert finite_num(quota.get("observed_n"))
        assert finite_num(quota.get("unknown_n"))
        assert quota.get("total_units") is None or finite_num(quota.get("total_units"))

        api_cost = entry.get("api_cost")
        assert isinstance(api_cost, dict)
        assert isinstance(api_cost.get("status"), str)
        assert str_list(api_cost.get("currencies"))
        assert finite_num(api_cost.get("observed_n"))
        assert finite_num(api_cost.get("unknown_n"))
        assert api_cost.get("total_amount") is None or finite_num(api_cost.get("total_amount"))

        effects = entry.get("dispatched_effects")
        assert isinstance(effects, list) and len(effects) <= 256
        for eff in effects:
            assert isinstance(eff, dict)
            assert isinstance(eff.get("effect_id"), str)
            assert isinstance(eff.get("target"), str)
            assert isinstance(eff.get("native_status"), str)
            assert isinstance(eff.get("dispatched_at"), str)


def make_usefulness_entry(
    automation_id: str = "auto-email-triage",
    *,
    lifecycle_state: str = "active",
    current_version_ref: str | None = "v1",
    prior_version_ref: str | None = None,
    reversible: bool = False,
    dispatched_effects: list[dict] | None = None,
    uncertainties: list[str] | None = None,
    source_record_hashes: list[str] | None = None,
) -> dict:
    return {
        "automation_id": automation_id,
        "lifecycle_state": lifecycle_state,
        "current_version_ref": current_version_ref,
        "prior_version_ref": prior_version_ref,
        "reversible": reversible,
        "outcomes": {
            "total_n": 4,
            "succeeded_n": 3,
            "failed_n": 1,
            "false_mismatch_n": 0,
            "unknown_n": 0,
            "verified_n": 3,
            "accepted_n": 2,
        },
        "effort": {
            "interruptions_total": 1,
            "correction_minutes_status": "OBSERVED",
            "correction_minutes_total": 3.5,
            "notification_useful_n": 2,
            "notification_noisy_n": 1,
        },
        "feedback_counts": {
            "useful": 2,
            "wrong": 0,
            "noisy": 1,
            "unnecessary": 0,
        },
        "baseline_and_uncertainty": {
            "baseline_kinds": ["manual_sample"],
            "minutes_saved": 12.5,
            "uncertainties": uncertainties if uncertainties is not None else ["small sample"],
        },
        "subscription_quota": {
            "status": "OBSERVED",
            "pools": ["local-pool"],
            "observed_n": 4,
            "unknown_n": 0,
            "total_units": 120.0,
            "unit_names": ["tokens"],
        },
        "api_cost": {
            "status": "ABSENT",
            "currencies": [],
            "observed_n": 0,
            "unknown_n": 0,
            "total_amount": None,
        },
        "dispatched_effects": dispatched_effects
        if dispatched_effects is not None
        else [
            {
                "effect_id": "eff-1",
                "target": "calendar.draft",
                "native_status": "dispatched",
                "dispatched_at": "2026-10-04T12:00:00Z",
                "receipt_ref": "urn:sha256:" + "c" * 64,
            }
        ],
        "source_record_hashes": source_record_hashes if source_record_hashes is not None else ["f" * 64],
    }


def make_base_scorecard() -> dict:
    return {
        "schema": "execution-outcome-scorecard-v1",
        "scoring_version": "canonical-inspector-v1",
        "confidence_status": "INSUFFICIENT",
        "ranking_allowed": False,
        "capacity_pool_coverage": {"status": "ABSENT", "distinct_pool_identity_n": 0},
        "capacity_pool_identities": [],
        "source_record_hashes": ["a" * 64],
        "arms": [],
    }


class FakeReversibilityError(Exception):
    pass


FakeReversibilityError.__name__ = "ReversibilityError"


class FakeUnauthorizedAutomationControlError(Exception):
    pass


FakeUnauthorizedAutomationControlError.__name__ = "UnauthorizedAutomationControlError"


class FakeWorkReceiptAutomationStore:
    """In-memory duck-typed store matching TMOS AutomationUsefulnessStore's scorecard + apply_control API."""

    _RECEIPT_ACTIONS = {
        "simplify": "automation.simplified",
        "pause": "automation.paused",
        "retire": "automation.retired",
        "restore": "automation.restored",
    }

    def __init__(self, initial_state: dict[str, dict] | None = None, *, deny_auth: bool = False):
        self.states: dict[str, dict] = initial_state or {
            "auto-email-triage": {
                "lifecycle_state": "active",
                "current_version_ref": "v1",
                "rollback_stack": [],
            }
        }
        self.deny_auth = deny_auth
        self.calls: list[dict] = []

    def credential_for(self, owner, req):
        return {"auth_key": owner.auth_key, "account_epoch": owner.account_epoch, "action": req.action}

    def project_scorecard(self) -> list[dict]:
        entries = []
        for aid, st in sorted(self.states.items()):
            prior = st["rollback_stack"][-1][1] if st["rollback_stack"] else None
            entry = make_usefulness_entry(
                aid,
                lifecycle_state=st["lifecycle_state"],
                current_version_ref=st["current_version_ref"],
                prior_version_ref=prior,
                reversible=bool(st["rollback_stack"]),
            )
            entry["private_internal_note"] = "SHOULD_BE_STRIPPED"
            entries.append(entry)
        return entries

    def apply_control(
        self,
        *,
        automation_id: str,
        work_item_key: str,
        run_id: str,
        action: str,
        reason: str,
        auth_credential=None,
        target_version_ref=None,
        expected_version_ref=None,
    ):
        self.calls.append({
            "automation_id": automation_id,
            "work_item_key": work_item_key,
            "run_id": run_id,
            "action": action,
            "reason": reason,
            "auth_credential": auth_credential,
            "target_version_ref": target_version_ref,
            "expected_version_ref": expected_version_ref,
        })
        if self.deny_auth or auth_credential is None:
            raise FakeUnauthorizedAutomationControlError("unauthorized")
        if automation_id not in self.states:
            raise LookupError("unknown automation")
        st = self.states[automation_id]
        if expected_version_ref is not None and st["current_version_ref"] != expected_version_ref:
            raise FakeReversibilityError("version mismatch")

        if action == "pause":
            if st["lifecycle_state"] == "paused":
                raise FakeReversibilityError("already paused")
            st["rollback_stack"].append((st["lifecycle_state"], st["current_version_ref"]))
            st["lifecycle_state"] = "paused"
        elif action == "retire":
            if st["lifecycle_state"] == "retired":
                raise FakeReversibilityError("already retired")
            st["rollback_stack"].append((st["lifecycle_state"], st["current_version_ref"]))
            st["lifecycle_state"] = "retired"
        elif action == "simplify":
            if not target_version_ref or target_version_ref == st["current_version_ref"]:
                raise FakeReversibilityError("invalid simplify target")
            st["rollback_stack"].append((st["lifecycle_state"], st["current_version_ref"]))
            st["lifecycle_state"] = "simplified"
            st["current_version_ref"] = target_version_ref
        elif action == "restore":
            if not st["rollback_stack"]:
                raise FakeReversibilityError("empty rollback stack")
            prev_state, prev_ver = st["rollback_stack"].pop()
            st["lifecycle_state"] = prev_state
            st["current_version_ref"] = prev_ver

        digest = hashlib.sha256(f"{automation_id}:{action}:{run_id}:{reason}".encode()).hexdigest()
        receipt = {
            "receipt_id": f"receipt.work.automation-usefulness.{uuid.uuid4()}",
            "action": self._RECEIPT_ACTIONS[action],
            "receipt_hash": digest,
            "observed_at": "2026-10-04T12:30:00Z",
        }
        return receipt, f"urn:sha256:{digest}"


def _valid_control_body(**overrides) -> dict:
    body = {
        "automation_id": "auto-email-triage",
        "action": "pause",
        "expected_version": "v1",
        "reason": "Paused due to noisy notifications",
        "idempotency_key": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Auth refusal (401 / 403) and CSRF / origin protections
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {},
    {"x-test-user": "nonadmin"},
    {"x-test-user": "internal-tool"},
    {"x-test-user": "api"},
    {"x-test-user": "admin", "x-test-bearer": "yes"},
])
def test_control_auth_refusal_403_precedes_validation_and_delegate(tmp_path, monkeypatch, headers):
    store = FakeWorkReceiptAutomationStore()
    app, manager, client = client_for(tmp_path, monkeypatch)
    manager._config["users"]["nonadmin"] = {"is_admin": False, "created": 3.0}
    app.state.automation_control_authority = store

    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers=headers,
        json={"action": "invalid-action"},
    )
    assert res.status_code == 403
    assert store.calls == []


def test_control_unauthenticated_401_with_auth_middleware(tmp_path, monkeypatch):
    from routes.routing_harness_routes import setup_routing_harness_routes

    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = AuthManager(str(tmp_path / "auth.json"))
    manager._config = {"users": {"admin": {"is_admin": True, "created": 1.5}}}
    store = FakeWorkReceiptAutomationStore()

    app = FastAPI()
    app.state.auth_manager = manager
    app.state.automation_control_authority = store

    @app.middleware("http")
    async def require_session_middleware(request, call_next):
        user = request.headers.get("x-test-user")
        if not user:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        request.state.current_user = user
        return await call_next(request)

    app.include_router(setup_routing_harness_routes())
    client = TestClient(app)

    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        json=_valid_control_body(),
    )
    assert res.status_code == 401
    assert store.calls == []


@pytest.mark.parametrize("bad_headers", [
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},
    {"origin": "https://evil.example"},
    {"origin": "http://evil.example"},
    {"origin": "null"},
    {"origin": ""},
    {"referer": "https://evil.example/csrf.html"},
    {"content-type": "application/x-www-form-urlencoded"},
    {"content-type": "text/plain"},
])
def test_control_csrf_and_origin_refusal_403(tmp_path, monkeypatch, bad_headers):
    store = FakeWorkReceiptAutomationStore()
    app, _, client = client_for(tmp_path, monkeypatch)
    app.state.automation_control_authority = store

    headers = {"x-test-user": "admin", **bad_headers}
    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers=headers,
        content=json.dumps(_valid_control_body()).encode("utf-8"),
    )
    assert res.status_code == 403
    assert store.calls == []


def test_control_same_origin_headers_allowed(tmp_path, monkeypatch):
    store = FakeWorkReceiptAutomationStore()
    app, _, client = client_for(tmp_path, monkeypatch)
    app.state.automation_control_authority = store

    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={
            "x-test-user": "admin",
            "origin": "http://testserver",
            "sec-fetch-site": "same-origin",
        },
        json=_valid_control_body(),
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True


# ---------------------------------------------------------------------------
# 2. Invalid payload & forged authority field refusal (400)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected_code", [
    (_valid_control_body(action="delete"), "invalid_action"),
    (_valid_control_body(action=""), "invalid_action"),
    (_valid_control_body(action=123), "invalid_action"),
    ({k: v for k, v in _valid_control_body().items() if k != "action"}, "invalid_action"),
    (_valid_control_body(expected_version=""), "invalid_expected_version"),
    (_valid_control_body(expected_version="   "), "invalid_expected_version"),
    (_valid_control_body(expected_version="v" * 513), "invalid_expected_version"),
    ({k: v for k, v in _valid_control_body().items() if k != "expected_version"}, "invalid_expected_version"),
    (_valid_control_body(reason=""), "invalid_reason"),
    (_valid_control_body(reason="   \t\n "), "invalid_reason"),
    (_valid_control_body(reason="r" * 1025), "invalid_reason"),
    # 342 x 3-byte UTF-8 char = 1026 bytes (> 1024 bytes even though len < 1024)
    (_valid_control_body(reason="\u2603" * 342), "invalid_reason"),
    ({k: v for k, v in _valid_control_body().items() if k != "reason"}, "invalid_reason"),
    (_valid_control_body(automation_id="different-automation"), "invalid_request"),
    (_valid_control_body(handle="not-a-uuid"), "invalid_request"),
    (_valid_control_body(handle=None), "invalid_request"),
    (_valid_control_body(target_version="   "), "invalid_request"),
    (_valid_control_body(target_version=None), "invalid_request"),
    (_valid_control_body(idempotency_key=""), "invalid_request"),
    (_valid_control_body(idempotency_key="bad\nkey"), "invalid_request"),
])
def test_control_invalid_payload_refusal_400(tmp_path, monkeypatch, payload, expected_code):
    store = FakeWorkReceiptAutomationStore()
    app, _, client = client_for(tmp_path, monkeypatch)
    app.state.automation_control_authority = store

    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=payload,
    )
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert body["status"] == "REFUSED"
    assert body["code"] == expected_code
    assert store.calls == []


@pytest.mark.parametrize("forged_field,forged_value", [
    ("actor", "root"),
    ("actor_kind", "kernel"),
    ("principal", {"auth_key": "root"}),
    ("principal_id", "principal:root"),
    ("principal_kind", "system"),
    ("auth_method", "bypass"),
    ("auth_credential", "forged-token"),
    ("grant_ref", "grant:allow-all"),
    ("grant_set", ["*"]),
    ("decision_id", "dec-forged"),
    ("authority", "override"),
    ("is_admin", True),
    ("work_item_key", "TMOS-999"),
    ("unknown_extra_field", "nope"),
])
def test_control_rejects_forged_authority_and_unknown_fields(tmp_path, monkeypatch, forged_field, forged_value):
    store = FakeWorkReceiptAutomationStore()
    app, _, client = client_for(tmp_path, monkeypatch)
    app.state.automation_control_authority = store

    payload = _valid_control_body(**{forged_field: forged_value})
    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=payload,
    )
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert body["code"] == "invalid_request"
    assert forged_field in body["detail"]
    assert store.calls == []


def test_control_rejects_duplicate_json_keys_and_oversized_body(tmp_path, monkeypatch):
    store = FakeWorkReceiptAutomationStore()
    app, _, client = client_for(tmp_path, monkeypatch)
    app.state.automation_control_authority = store

    dup_json = b'{"action":"pause","action":"retire","expected_version":"v1","reason":"dup"}'
    res_dup = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin", "content-type": "application/json"},
        content=dup_json,
    )
    assert res_dup.status_code == 400
    assert res_dup.json()["ok"] is False

    huge_json = json.dumps({"action": "pause", "expected_version": "v1", "reason": "x" * 5000}).encode("utf-8")
    res_big = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin", "content-type": "application/json"},
        content=huge_json,
    )
    assert res_big.status_code == 400
    assert res_big.json()["ok"] is False
    assert store.calls == []


# ---------------------------------------------------------------------------
# 3. Unconfigured fail-closed refusal (503)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["simplify", "pause", "retire", "restore"])
def test_control_unconfigured_fails_closed_503(tmp_path, monkeypatch, action):
    _, _, client = client_for(tmp_path, monkeypatch)
    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(action=action),
    )
    assert res.status_code == 503
    body = res.json()
    assert body["ok"] is False
    assert body["status"] == "REFUSED"
    assert body["code"] == "unavailable"
    assert body["automation_id"] == "auto-email-triage"
    assert body["action"] == action
    assert "detail" in body and body["detail"]


def test_control_readonly_usefulness_source_still_fails_closed_503(native_store, tmp_path, monkeypatch):
    store, _ = native_store
    export_path = tmp_path / "usefulness.json"
    export_path.write_text(json.dumps([make_usefulness_entry()]), encoding="utf-8")

    service = InspectorService(
        store,
        retained_root=tmp_path,
        projector=lambda *_a, **_k: retained_projection(),
        automation_usefulness_source=JsonExportAutomationUsefulnessSource(export_path),
        automation_control_authority=None,
    )
    _, _, client = client_for(tmp_path, monkeypatch, service)
    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(),
    )
    assert res.status_code == 503
    assert res.json()["ok"] is False
    assert res.json()["code"] == "unavailable"


# ---------------------------------------------------------------------------
# 4. Bounded scorecard attachment (256-entry / 64 KiB cap, safe fallback)
# ---------------------------------------------------------------------------

def test_attach_automation_usefulness_bounds_256_entries_and_64kib_cap():
    base = make_base_scorecard()

    # 300 entries with 300 dispatched effects on the first entry -> capped <= 256 entries & <= 64 KiB.
    many_effects = [
        {
            "effect_id": f"eff-{i}",
            "target": "mail.send",
            "native_status": "dispatched",
            "dispatched_at": "2026-10-04T12:00:00Z",
        }
        for i in range(300)
    ]
    entries = [
        make_usefulness_entry(
            f"auto-{i:03d}",
            dispatched_effects=many_effects if i == 0 else [],
            source_record_hashes=[f"{i:064x}"],
        )
        for i in range(300)
    ]
    attached = attach_automation_usefulness(base, entries)
    assert attached["ranking_allowed"] is False
    assert attached["automation_usefulness_total_n"] == 300
    assert len(attached["automation_usefulness"]) <= AUTOMATION_USEFULNESS_MAX_ENTRIES
    assert len(attached["automation_usefulness"]) > 0
    assert attached["automation_usefulness"][0]["dispatched_effects_total_n"] == 300
    assert len(attached["automation_usefulness"][0]["dispatched_effects"]) <= len(attached["automation_usefulness"])
    encoded = json.dumps(attached, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert len(encoded) <= AUTOMATION_USEFULNESS_MAX_BYTES
    _assert_cockpit_valid_automation_usefulness(attached["automation_usefulness"])


def test_attach_automation_usefulness_never_crashes_on_malformed_or_raising_source(tmp_path):
    base = make_base_scorecard()

    def broken_source():
        raise RuntimeError("boom")

    assert "automation_usefulness" not in attach_automation_usefulness_from_source(base, broken_source)
    assert "automation_usefulness" not in attach_automation_usefulness_from_source(base, None)

    bad_entry = make_usefulness_entry()
    bad_entry["outcomes"]["total_n"] = True  # bool is not a valid finite JS number
    assert "automation_usefulness" not in attach_automation_usefulness(base, [bad_entry])

    bad_state = make_usefulness_entry(lifecycle_state="deleted")
    assert "automation_usefulness" not in attach_automation_usefulness(base, [bad_state])

    corrupt_export = tmp_path / "corrupt.json"
    corrupt_export.write_text("{not-json", encoding="utf-8")
    assert "automation_usefulness" not in attach_automation_usefulness_from_source(
        base, JsonExportAutomationUsefulnessSource(corrupt_export)
    )

    real_export = tmp_path / "real.json"
    real_export.write_text(json.dumps([make_usefulness_entry()]), encoding="utf-8")
    symlink_export = tmp_path / "link.json"
    symlink_export.symlink_to(real_export)
    # Symlinks are rejected fail-closed.
    assert "automation_usefulness" not in attach_automation_usefulness_from_source(
        base, JsonExportAutomationUsefulnessSource(symlink_export)
    )


def test_inspector_response_shrinks_usefulness_rather_than_failing_64kib_cap(native_store, tmp_path, monkeypatch):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())

    # Large entries that collectively exceed 64 KiB when combined with the rest of the inspection response.
    large_entries = [
        make_usefulness_entry(
            f"auto-big-{i:03d}",
            uncertainties=["u" * 600 for _ in range(4)],
        )
        for i in range(40)
    ]
    proj = retained_projection()
    proj["outcomes"] = {
        "status": "OBSERVED",
        "associated_source_hashes": ["a" * 64],
        "scorecard": make_base_scorecard(),
    }
    service = InspectorService(
        store,
        retained_root=tmp_path,
        projector=lambda *_a, **_k: proj,
        automation_usefulness_source=lambda: large_entries,
    )
    monkeypatch.setattr(service, "_read", lambda *_a, **_k: (retained_payload(), {}))
    _, _, client = client_for(tmp_path, monkeypatch, service)

    res = client.get("/api/harness/inspector/attempts/" + handle, headers={"x-test-user": "admin"})
    assert res.status_code == 200
    assert len(res.content) <= CANONICAL_INSPECTOR_RESPONSE_BYTES
    scorecard = res.json()["routing"]["outcomes"]["scorecard"]
    _assert_cockpit_valid_automation_usefulness(scorecard.get("automation_usefulness"))


# ---------------------------------------------------------------------------
# 5. Configured scorecard attachment + control round-trip
# ---------------------------------------------------------------------------

def test_configured_scorecard_attachment_and_control_roundtrip(native_store, tmp_path, monkeypatch):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())

    auto_store = FakeWorkReceiptAutomationStore()

    def build_proj(*_a, **_k):
        proj = retained_projection()
        proj["outcomes"] = {
            "status": "OBSERVED",
            "associated_source_hashes": ["a" * 64],
            "scorecard": make_base_scorecard(),
        }
        return proj

    service = InspectorService(
        store,
        retained_root=tmp_path,
        projector=build_proj,
        automation_usefulness_source=auto_store,
        automation_control_authority=auto_store,
    )
    monkeypatch.setattr(service, "_read", lambda *_a, **_k: (retained_payload(), {}))
    _, _, client = client_for(tmp_path, monkeypatch, service)

    inspect_url = "/api/harness/inspector/attempts/" + handle
    control_url = "/api/harness/inspector/automations/auto-email-triage/control"
    admin_headers = {"x-test-user": "admin"}

    # Step 1: Inspect shows active automation with private fields stripped.
    before = client.get(inspect_url, headers=admin_headers)
    assert before.status_code == 200
    assert "SHOULD_BE_STRIPPED" not in before.text
    scorecard = before.json()["routing"]["outcomes"]["scorecard"]
    assert scorecard["ranking_allowed"] is False
    _assert_cockpit_valid_automation_usefulness(scorecard["automation_usefulness"])
    assert len(scorecard["automation_usefulness"]) == 1
    assert scorecard["automation_usefulness"][0]["lifecycle_state"] == "active"
    assert scorecard["automation_usefulness"][0]["reversible"] is False

    # Step 2: Pause via POST control endpoint with owned handle.
    pause_res = client.post(
        control_url,
        headers=admin_headers,
        json=_valid_control_body(action="pause", expected_version="v1", handle=handle),
    )
    assert pause_res.status_code == 200
    pause_body = pause_res.json()
    assert pause_body["ok"] is True
    assert pause_body["status"] == "APPLIED"
    assert len(pause_body["receipt_hash"]) == 64
    assert pause_body["receipt"]["automation_id"] == "auto-email-triage"
    assert pause_body["receipt"]["action"] == "pause"
    assert pause_body["receipt"]["work_receipt_action"] == "automation.paused"
    # Credential was derived server-side from admin Principal, never from request body.
    assert auto_store.calls[-1]["auth_credential"]["auth_key"] == "admin"

    # Step 3: Re-inspect reflects paused + reversible state.
    after_pause = client.get(inspect_url, headers=admin_headers).json()
    entry = after_pause["routing"]["outcomes"]["scorecard"]["automation_usefulness"][0]
    assert entry["lifecycle_state"] == "paused"
    assert entry["reversible"] is True

    # Step 4: Version conflict returns 409.
    conflict = client.post(
        control_url,
        headers=admin_headers,
        json=_valid_control_body(action="retire", expected_version="stale-version"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["ok"] is False
    assert conflict.json()["code"] == "version_conflict"

    # Step 5: Restore returns to active.
    restore_res = client.post(
        control_url,
        headers=admin_headers,
        json=_valid_control_body(action="restore", expected_version="v1", reason="Restoring after tuning"),
    )
    assert restore_res.status_code == 200
    assert restore_res.json()["receipt"]["action"] == "restore"
    after_restore = client.get(inspect_url, headers=admin_headers).json()
    assert after_restore["routing"]["outcomes"]["scorecard"]["automation_usefulness"][0]["lifecycle_state"] == "active"

    # Step 6: Simplify updates version ref.
    simplify_res = client.post(
        control_url,
        headers=admin_headers,
        json=_valid_control_body(
            action="simplify",
            expected_version="v1",
            target_version="v2-simple",
            reason="Drop unnecessary notifications",
        ),
    )
    assert simplify_res.status_code == 200
    assert simplify_res.json()["receipt"]["action"] == "simplify"
    after_simplify = client.get(inspect_url, headers=admin_headers).json()
    sim_entry = after_simplify["routing"]["outcomes"]["scorecard"]["automation_usefulness"][0]
    assert sim_entry["lifecycle_state"] == "simplified"
    assert sim_entry["current_version_ref"] == "v2-simple"
    assert sim_entry["prior_version_ref"] == "v1"

    # Step 7: Foreign admin cannot bind control to another admin's handle (404).
    foreign = client.post(
        control_url,
        headers={"x-test-user": "other"},
        json=_valid_control_body(action="retire", expected_version="v2-simple", handle=handle),
    )
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"


def test_control_store_unauthorized_and_malformed_ack_refusals(tmp_path, monkeypatch):
    denied_store = FakeWorkReceiptAutomationStore(deny_auth=True)
    app, _, client = client_for(tmp_path, monkeypatch)
    app.state.automation_control_authority = denied_store

    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(),
    )
    assert res.status_code == 403
    assert res.json()["code"] == "unauthorized"

    # Malformed acknowledgement (missing valid 64-hex receipt_hash) must fail closed with 503.
    app.state.automation_control_authority = lambda _owner, _req: {
        "ok": True,
        "status": "APPLIED",
        "receipt_hash": "not-a-sha256",
    }
    bad_ack = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(),
    )
    assert bad_ack.status_code == 503
    assert bad_ack.json()["ok"] is False
    assert bad_ack.json()["code"] == "unavailable"


def test_control_error_mapping_direct_conflict_and_unavailable(tmp_path, monkeypatch):
    app, _, client = client_for(tmp_path, monkeypatch)

    def raise_conflict(_owner, _req):
        raise AutomationControlConflictError("Version changed")

    app.state.automation_control_authority = raise_conflict
    res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(),
    )
    assert res.status_code == 409
    assert res.json()["code"] == "version_conflict"

    def raise_unavail(_owner, _req):
        raise AutomationControlUnavailableError("Authority offline")

    app.state.automation_control_authority = raise_unavail
    res2 = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(),
    )
    assert res2.status_code == 503
    assert res2.json()["code"] == "unavailable"


@pytest.mark.asyncio
async def test_runtime_wires_export_and_control_authority_and_cleans_up_on_stop(native_store, tmp_path, monkeypatch):
    from src.canonical_inspector_runtime import start_inspector_runtime, stop_inspector_runtime
    from tests.test_canonical_inspector_runtime import InertProcesses
    from tests.test_canonical_tmos_publication import retained_work

    store, engine = native_store
    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)

    export_file = tmp_path / "automation_usefulness.json"
    export_file.write_text(json.dumps({"automation_usefulness": [make_usefulness_entry()]}), encoding="utf-8")
    control_store = FakeWorkReceiptAutomationStore()

    app, _, client = client_for(tmp_path, monkeypatch)
    await start_inspector_runtime(
        app,
        settings={
            "canonicalInspector": {
                "enabled": True,
                "automationUsefulnessExport": str(export_file),
            }
        },
        sessions=sessionmaker(bind=engine, expire_on_commit=False),
        manifest={"schema": "canonical-tmos-owner-v1", "packages": {work.package.packet_id: work.package.to_dict()}},
        retained_root=work.path.parent,
        process_factory=lambda **cfg: InertProcesses(**cfg),
        automation_control_authority=control_store,
    )

    inspect_res = client.get(
        "/api/harness/inspector/attempts/" + published["handle"],
        headers={"x-test-user": "admin"},
    )
    assert inspect_res.status_code == 200
    scorecard = inspect_res.json()["routing"]["outcomes"]["scorecard"]
    _assert_cockpit_valid_automation_usefulness(scorecard["automation_usefulness"])
    assert scorecard["automation_usefulness"][0]["automation_id"] == "auto-email-triage"

    ctrl_res = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(action="pause", expected_version="v1"),
    )
    assert ctrl_res.status_code == 200

    await stop_inspector_runtime(app)
    after_stop = client.post(
        "/api/harness/inspector/automations/auto-email-triage/control",
        headers={"x-test-user": "admin"},
        json=_valid_control_body(action="restore", expected_version="v1"),
    )
    assert after_stop.status_code == 503
