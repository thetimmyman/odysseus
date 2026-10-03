"""Tests for src/routing_coordinator_decide.py — server-side coordinator
decision generation (spec Phase 8 / Section 9 data-locality).

Verifies the fail-closed boundaries with synthetic tasks and stub clients:
  * the sensitivity-vs-remote-ceiling locality gate (payload never transmitted
    to a non-local coordinator for over-ceiling data),
  * redact-before-store / redact-before-return of every persisted field,
  * endpoint failures degrade to the deterministic tier instead of raising,
  * unknown schemaVersion fails closed without a repair call.

No network: every client/db/policy dependency is a stub or monkeypatch.
"""
import json
from types import SimpleNamespace

import pytest

import src.routing_coordinator_decide as rcd
from src.routing_coordinator import SCHEMA_VERSION

_SECRET_KEY = "sk-abcdefghijklmnopqrst0"  # synthetic, matches the sk- shape


class FakeDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass


class CapturingAudit:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def patched_persist(monkeypatch):
    """Neutralize audit signing / policy versions / task payload extraction."""
    import core.database as cd
    import src.secret_storage as ss
    import src.routing_policy as rp
    import src.routing_task_io as rtio

    monkeypatch.setattr(cd, "CoordinatorAudit", CapturingAudit)
    monkeypatch.setattr(ss, "hmac_sign", lambda s: "test-sig")
    monkeypatch.setattr(rp, "policy_versions", lambda: {"test": "1"})
    monkeypatch.setattr(rtio, "task_payload_from_row",
                       lambda task: {"id": task.id, "title": "synthetic task"})


def _task(sensitivity="internal"):
    return SimpleNamespace(id="task-1", data_sensitivity=sensitivity,
                          verification_mode="analysis_only", task_type="bug_debug")


class Client:
    def __init__(self, chat_url, reply=None, error=None, repair=None):
        self._chat_url = chat_url
        self._reply = reply
        self._error = error
        self.repair_fn = repair
        self.decide_calls = []
        self.repair_calls = []

    def is_llm_backed(self):
        return True

    def decide(self, payload):
        self.decide_calls.append(payload)
        if self._error:
            raise RuntimeError(self._error)
        return self._reply


def _patch_ceiling(monkeypatch, rank):
    import src.routing_engine as re_mod
    monkeypatch.setattr(re_mod, "_remote_ceiling_rank", lambda: rank)


SAFE_ROUTE = {
    "backend": "odysseus_general_swe",
    "modelRoleChain": [{"role": "scout", "reason": "deterministic"}],
    "allowPremium": False,
    "verificationMode": "analysis_only",
    "dataSensitivity": "internal",
    "approvalRequired": False,
    "approved": False,
    "rationale": ["stub deterministic route"],
    "schemaVersion": SCHEMA_VERSION,
}

VALID_DECISION = {
    "schemaVersion": SCHEMA_VERSION,
    "taskId": "task-1",
    "classification": {
        "domain": "general_swe", "taskType": "bug_debug", "risk": "low",
        "dataSensitivity": "internal", "verificationMode": "analysis_only",
    },
    "routeRecommendation": {
        "backend": "odysseus_general_swe",
        "modelRoleChain": [{"role": "scout", "reason": "cheap look"}],
        "allowPremium": False,
    },
}


# ------------------------------------------------- Section 9 locality gate

def test_locality_permits_under_ceiling(monkeypatch):
    _patch_ceiling(monkeypatch, 2)  # confidential ceiling
    ok = rcd.coordinator_endpoint_permits_sensitivity(
        _task("internal"), SimpleNamespace(_chat_url="https://remote.example"))
    assert ok is True


def test_locality_blocks_over_ceiling_remote(monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    blocked = rcd.coordinator_endpoint_permits_sensitivity(
        _task("restricted"), SimpleNamespace(_chat_url="https://remote.example"))
    assert blocked is False


def test_locality_allows_over_ceiling_local(monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    ok = rcd.coordinator_endpoint_permits_sensitivity(
        _task("secret"), SimpleNamespace(_chat_url="http://127.0.0.1:11434/v1"))
    assert ok is True


def test_locality_blocks_unresolvable_endpoint(monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    blocked = rcd.coordinator_endpoint_permits_sensitivity(
        _task("restricted"), SimpleNamespace(_chat_url=None))
    assert blocked is False


# ------------------------------------------------------- redact_obj (deep)

def test_redact_obj_masks_nested_strings_preserves_shape():
    obj = {"title": f"fix { _SECRET_KEY } here",
           "inputs": ["clean", f"token { _SECRET_KEY }"], "n": 7}
    out = rcd._redact_obj(obj)
    assert _SECRET_KEY not in json.dumps(out)
    assert "[REDACTED]" in out["title"]
    assert out["inputs"][0] == "clean"
    assert out["n"] == 7


# ------------------------------------------- generate_and_wrap_decision

def test_external_provider_raises():
    client = SimpleNamespace(is_llm_backed=lambda: False)
    with pytest.raises(rcd.ExternalProviderError):
        rcd.generate_and_wrap_decision(FakeDB(), _task(), client)


def test_valid_decision_passes_through(patched_persist, monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    monkeypatch.setattr(rcd, "build_deterministic_route", lambda db, t: SAFE_ROUTE)
    client = Client("https://coord.example", reply=json.dumps(VALID_DECISION))
    result = rcd.generate_and_wrap_decision(FakeDB(), _task(), client)
    assert result["ok"] is True
    assert result["fallbackPath"] == "none"
    assert result["decideError"] is None
    assert result["route"]["backend"] == "odysseus_general_swe"


def test_locality_blocked_never_transmits_payload(patched_persist, monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    monkeypatch.setattr(rcd, "build_deterministic_route", lambda db, t: SAFE_ROUTE)
    repair_calls = []
    client = Client("https://remote.example", reply=json.dumps(VALID_DECISION),
                   repair=lambda raw, errs: repair_calls.append(1) or None)
    result = rcd.generate_and_wrap_decision(
        FakeDB(), _task("restricted"), client)
    assert client.decide_calls == []          # payload never sent
    assert repair_calls == []                # repair tier not wired to blocked endpoint
    assert "coordinator_remote_blocked" in result["decideError"]
    assert result["appliedFallback"] is True
    assert result["fallbackPath"] == "deterministic"


def test_endpoint_failure_degrades_and_redacts_error(patched_persist, monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    monkeypatch.setattr(rcd, "build_deterministic_route", lambda db, t: SAFE_ROUTE)
    client = Client("https://coord.example",
                   error=f"upstream 500 leaked { _SECRET_KEY } in body")
    db = FakeDB()
    result = rcd.generate_and_wrap_decision(db, _task(), client)
    assert result["decideError"] is not None
    assert _SECRET_KEY not in result["decideError"]
    assert any(n.startswith("decide_failed:") for n in result["auditNotes"])
    assert all(_SECRET_KEY not in n for n in result["auditNotes"])
    assert result["appliedFallback"] is True
    # audit row persisted with a signature over the redacted raw
    audit = db.added[-1]
    assert audit.hmac == "test-sig"
    assert audit.parsed_ok is False


def test_unknown_schema_fails_closed_without_repair(patched_persist, monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    monkeypatch.setattr(rcd, "build_deterministic_route", lambda db, t: SAFE_ROUTE)
    repair_calls = []
    bad = dict(VALID_DECISION, schemaVersion="9.9")
    client = Client("https://coord.example", reply=json.dumps(bad),
                   repair=lambda raw, errs: repair_calls.append(1) or None)
    result = rcd.generate_and_wrap_decision(FakeDB(), _task(), client)
    assert repair_calls == []  # unknown version: fail closed, no repair retry
    assert any("schema_version_error" in e for e in result["validationErrors"])
    assert result["appliedFallback"] is True


def test_audit_stores_redacted_raw(patched_persist, monkeypatch):
    _patch_ceiling(monkeypatch, 2)
    monkeypatch.setattr(rcd, "build_deterministic_route", lambda db, t: SAFE_ROUTE)
    raw = json.dumps(dict(VALID_DECISION,
                        rationale=[f"note { _SECRET_KEY } trailing"]))
    client = Client("https://coord.example", reply=raw)
    db = FakeDB()
    result = rcd.generate_and_wrap_decision(db, _task(), client)
    assert result["ok"] is True
    audit = db.added[-1]
    assert _SECRET_KEY not in audit.raw_output
    assert audit.redaction_applied is True
    assert _SECRET_KEY not in result["generatedRaw"]


def test_route_rationale_redacted_before_return(patched_persist, monkeypatch):
    """route.rationale carries model-controlled text and must be redacted
    before return, same bar as validationErrors/auditNotes/generatedRaw."""
    _patch_ceiling(monkeypatch, 2)
    monkeypatch.setattr(rcd, "build_deterministic_route", lambda db, t: SAFE_ROUTE)
    raw = json.dumps(dict(VALID_DECISION,
                        rationale=[f"echo { _SECRET_KEY }"]))
    client = Client("https://coord.example", reply=raw)
    result = rcd.generate_and_wrap_decision(FakeDB(), _task(), client)
    assert _SECRET_KEY not in json.dumps(result["route"])
