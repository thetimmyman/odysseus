"""PS-605 — the PRODUCTION dispatcher consumes the decision (wiring proof).

These tests call ``routing_executor.execute_candidates`` itself, with the network
and the manifest patched out, and assert the things that make the router production
rather than a sidecar:

  1. only the decision's eligible candidates are attempted, in the decision's order;
  2. a PS-605 refusal stops the run with ZERO model calls;
  3. an invocation whose resolved model contradicts the pin never reaches the
     network, and every attempt carries the receipt hash that authorised it.

The estate is the real SQLite one from the boundary tests, so the decision these
tests obey is resolved over real rows rather than fixtures made up for the wiring.
"""
from __future__ import annotations

import json

from src import routing_executor as rex
from tests.test_dispatch_boundary import _db, _seed


def _candidates(*profile_ids):
    return [{"profile_id": pid, "model": "", "roles": [], "score": 1.0,
             "estimated_cost_usd": 0.0, "reasons": []} for pid in profile_ids]


class _Harness:
    """Patched-out world: no network, no budget DB, no on-disk archive."""

    def __init__(self, monkeypatch, tmp_path, *, override_model=None):
        self.llm_calls: list = []
        self.manifests: list = []
        #: When set, the resolver deliberately returns a model the decision did NOT
        #: pin, which is the pin-violation control.
        self.override_model = override_model
        monkeypatch.setattr(rex, "archive_root", lambda: str(tmp_path))
        monkeypatch.setattr(rex, "_write_run_manifest",
                            lambda db, task, run_id, run_dir, bundle:
                            self.manifests.append(bundle))
        monkeypatch.setattr(rex, "build_context_bundle",
                            lambda task: {"prompt": "p", "sources": [],
                                          "metadata": {}})
        monkeypatch.setattr(rex, "build_prompt",
                            lambda role, task, bundle: f"PROMPT::{role}")
        monkeypatch.setattr(rex, "resolve_endpoint_by_id", self._resolve)
        monkeypatch.setattr(rex, "llm_call_with_usage", self._llm)
        monkeypatch.setattr(rex, "check_general_budget",
                            lambda db: {"allowed": True, "reason": ""})
        monkeypatch.setattr(rex, "check_premium_budget",
                            lambda db: {"allowed": True, "reason": ""})
        monkeypatch.setattr(rex, "check_task_budget",
                            lambda db, task, spent, cost: {"allowed": True,
                                                           "reason": ""})
        monkeypatch.setattr(rex, "estimate_cost_usd", lambda p, i, o: 0.0)

    def _resolve(self, endpoint_id, model=None, owner=None):
        urls = {"ep-rtx": "http://192.168.1.130:11434/v1",
                "ep-or": "https://openrouter.ai/api/v1",
                "ep-msr": "http://192.168.1.131:8080/v1"}
        if endpoint_id not in urls:
            return None
        return (urls[endpoint_id], self.override_model or model or "", {})

    def _llm(self, url, model, messages, **kwargs):
        self.llm_calls.append({"url": url, "model": model})
        return ("ok", {"input_tokens": 5, "output_tokens": 5})

    def urls(self):
        return [c["url"] for c in self.llm_calls]


def test_the_dispatcher_attempts_only_the_decisions_eligible_candidates(monkeypatch,
                                                                       tmp_path):
    db = _db()
    task = _seed(db, allow_paid=True)
    harness = _Harness(monkeypatch, tmp_path)
    result = rex.execute_candidates(db, task, _candidates("p-msr", "p-rtx"), 3)

    # MS-R1 is not an inference target for an implementation task, so it was never
    # offered to the loop: exactly one model call, to the LOCAL endpoint.
    assert harness.urls() == ["http://192.168.1.130:11434/v1"]
    assert result["status"] == "succeeded"
    assert result["selected_target_id"] == "profile:p-rtx"
    assert [a["target_id"] for a in result["attempts"]] == ["profile:p-rtx"]
    assert (result["attempts"][0]["dispatch_receipt_hash"]
            == result["dispatch_receipt_hash"])
    assert [i["locality"] for i in result["invocations"]] == ["local"]
    # The decision reached the run manifest, so it is on disk before any call.
    assert harness.manifests
    assert harness.manifests[0]["ps605_dispatch"]["decision"]["reason_code"]


def test_the_dispatcher_follows_the_decisions_order(monkeypatch, tmp_path):
    db = _db()
    task = _seed(db, allow_paid=True)
    task.task_type = "diff_review"       # role = reviewer: both profiles are eligible
    db.commit()
    harness = _Harness(monkeypatch, tmp_path)
    result = rex.execute_candidates(db, task, _candidates("p-openrouter", "p-rtx"), 2)

    # Decision order is deterministic (cost rank, then id) and INDEPENDENT of the
    # order the caller passed the candidates in.
    assert harness.urls() == ["http://192.168.1.130:11434/v1",
                              "https://openrouter.ai/api/v1"]
    assert [a["profile_id"] for a in result["attempts"]] == ["p-rtx", "p-openrouter"]
    assert [i["locality"] for i in result["invocations"]] == ["local", "hosted"]


def test_a_ps605_refusal_stops_the_run_with_zero_model_calls(monkeypatch, tmp_path):
    db = _db()
    task = _seed(db, sensitivity="restricted")
    harness = _Harness(monkeypatch, tmp_path)
    result = rex.execute_candidates(db, task, _candidates("p-openrouter"), 3)

    assert result["status"] == "failed"
    assert result["refused"]["code"] == "privacy_local_only_no_eligible_target"
    assert result["attempted"] == 0
    assert harness.llm_calls == []          # NOTHING was called
    assert result["invocations"] == []
    refusals = list(tmp_path.glob(f"{task.id}/*/dispatch_refusal.json"))
    assert len(refusals) == 1
    payload = json.loads(refusals[0].read_text())
    assert payload["code"] == "privacy_local_only_no_eligible_target"
    assert payload["candidates"][0]["rule"] == "policy_denied"


def test_an_invocation_that_contradicts_the_pin_never_reaches_the_network(
        monkeypatch, tmp_path):
    db = _db()
    task = _seed(db)
    # The endpoint resolver returns a DIFFERENT model than the decision pinned.
    harness = _Harness(monkeypatch, tmp_path, override_model="some-other-model")
    result = rex.execute_candidates(db, task, _candidates("p-rtx"), 3)

    assert harness.llm_calls == []          # refused BEFORE the call
    assert result["attempts"] == []
    assert result["status"] == "failed"
    assert [m["status"] for m in result["model_runs"]] == ["policy_refused"]
    assert "pin_model_mismatch" in result["model_runs"][0]["reason"]
    assert result["invocations"][0]["ok"] is False
    assert result["invocations"][0]["detail"] == "pin_model_mismatch"


def test_the_dispatch_record_binds_every_attempt_to_the_receipt(monkeypatch,
                                                               tmp_path):
    db = _db()
    task = _seed(db)
    harness = _Harness(monkeypatch, tmp_path)
    result = rex.execute_candidates(db, task, _candidates("p-rtx"), 1)

    from src.dispatch_routing import ps638_receipt_hash

    with open(result["dispatch_receipt_path"]) as handle:
        record = json.load(handle)
    assert record["dispatch_receipt_hash"] == ps638_receipt_hash(
        record["dispatch_receipt"])
    assert record["dispatch_receipt_hash"] == result["dispatch_receipt_hash"]
    assert [a["dispatch_receipt_hash"] for a in record["attempts"]] == [
        record["dispatch_receipt_hash"]]
    assert record["dispatch_receipt"]["selected_target_id"] == "profile:p-rtx"
    assert record["dispatch_receipt"]["decided_by"] == "ps605_policy"
    assert record["policy"]["policy_ref"].startswith("routing_policy@")
    assert record["invocations"][0]["locality"] == "local"
    assert record["attempts"][0]["selected"] is True

    # And the PRODUCTION record seals and validates with the same validator the
    # evidence layer uses — no re-derivation, no second code path.
    from src import dispatch_boundary as dbd

    sealed = dbd.seal_recorded_dispatch(
        record=record, budget_snapshot={"max_cost_rank": 0},
        resource_snapshot={"available": True}, sealed_at="T")
    ok, codes = dbd.validate_dispatch_evidence(sealed)
    assert ok is True and codes == ()
    sealed["decision_receipt"]["selected_target_id"] = "profile:p-openrouter"
    ok, codes = dbd.validate_dispatch_evidence(sealed)
    assert ok is False and dbd.EVIDENCE_PIN_CHANGED in codes


