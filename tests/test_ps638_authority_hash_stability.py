"""The optional ``authority`` field must not change hashes when absent.

Covers ``DispatchDecisionReceipt`` (src/execution_package.py) and
``DispatchDecision`` (src/dispatch_routing.py):

  (a) POSITIVE: receipts differing only in ``authority`` hash differently,
      otherwise authority would be unbound, forgeable evidence.
  (b) NEGATIVE CONTROL: with ``authority`` omitted, the hash is BYTE-IDENTICAL
      to a frozen fixture captured before the field existed (not a same-run
      rebuild, which could hide two wrong computations agreeing). This must
      also hold for ``seal.evidence_hash``, since the kwargs dict is sealed
      verbatim by ``seal_dispatch_evidence()``.
  (c) ROUND-TRIP / DETERMINISM: key order and null-vs-absent inside
      ``authority`` are not separate hash identities, and a round-tripped
      receipt compares equal.

The ``authority`` block only records and audits; nothing here implies runtime
enforcement or treats a recorded principal as an access-control decision.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path

from src import dispatch_routing as dr
from src.execution_package import make_dispatch_receipt

FIXTURES = Path(__file__).parent / "fixtures"

def _load(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


RECEIPT_FIXTURE = _load("ps638_receipt_pre_authority.json")
DECISION_FIXTURE = _load("ps605_decision_pre_authority.json")

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.timezone.utc)
PROFILE_ID = "rtx4500-ollama-qwen38-27b"
IMPLEMENTER_CAPS = frozenset({
    dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL, dr.CAP_EXACT_REFERENCE_SEMANTICS,
})


def _decision_inputs():
    """Rebuild the exact deterministic inputs the DispatchDecision fixture used."""
    policy = dr.policy_snapshot(policy={"routingPolicyVersion": "1.2",
                                        "remoteSensitivityCeiling": "confidential"})
    profile = dr.make_target_profile(
        target_id="local-rtx4500", profile_id=PROFILE_ID, provider="ollama",
        host="minipc", runtime_kind="ollama", runtime_version="0.32.11",
        model="qwen3.8:27b", model_digest="d94d9646", backend="cuda",
        endpoint_url="http://127.0.0.1:11434/v1", locality=dr.LOCALITY_LOCAL,
        roles=frozenset({dr.ROLE_IMPLEMENTER, dr.ROLE_REPAIR}),
        tools=frozenset({"write_file"}), network_policy="tailnet-loopback",
        budget_class="dev", cost_rank=0,
    )
    receipt = dr.make_legacy_capability_view(
        receipt_id="cap-rtx-1", profile_id=PROFILE_ID, target_id="local-rtx4500",
        capabilities=IMPLEMENTER_CAPS, exactness=dr.EXACTNESS_EXACT,
        observed_at="2026-09-15T11:00:00+00:00", ttl_s=86400, healthy=True,
        host="minipc", runtime_version="0.32.11", model_digest="d94d9646",
    )
    request = dr.RoutingRequest(
        domain="general_swe", role=dr.ROLE_IMPLEMENTER, run_id="run-fixture-2",
        packet_id="P-fixture-2", execution_package_hash="pkg-hash-fixture-2",
        required_tools=("write_file",), network_policy="tailnet-loopback",
        budget_class="dev", write_scope=("src/thing.py",),
    )
    return request, profile, receipt, policy


def _select(**kwargs):
    request, profile, receipt, policy = _decision_inputs()
    return dr.select_target(request, profiles=[profile], receipts=[receipt],
                            policy=policy, now=NOW, decision_id="dec-fixture-2",
                            **kwargs)


AUTHORITY_BLOCK = {
    "requesting_principal": "tim.defreest@gmail.com",
    "acting_principal": "agent:package-b-worker",
    "action": "dispatch",
    "resource": "src/dispatch_routing.py",
    "grant_id": "grant-ps664-f1",
    "grant_expires_at": "2026-12-31T00:00:00+00:00",
}


# (a) POSITIVE

def test_a_present_authority_changes_the_dispatch_decision_receipt_hash():
    absent = make_dispatch_receipt(**RECEIPT_FIXTURE["kwargs"])
    present = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                        "authority": AUTHORITY_BLOCK})
    assert present.receipt_hash != absent.receipt_hash
    assert present.core()["authority"] == AUTHORITY_BLOCK
    assert "authority" not in absent.core()


def test_a_present_authority_changes_the_dispatch_decision_hash():
    """Same claim, run through DispatchDecision (dispatch_routing.py)."""
    kwargs_absent = dict(DECISION_FIXTURE["receipt_kwargs"])
    kwargs_present = {**kwargs_absent, "authority": AUTHORITY_BLOCK}
    hash_absent = dr.ps638_receipt_hash(kwargs_absent)
    hash_present = dr.ps638_receipt_hash(kwargs_present)
    assert hash_present != hash_absent
    assert dr.ps638_receipt_core(kwargs_present)["authority"] == AUTHORITY_BLOCK
    assert "authority" not in dr.ps638_receipt_core(kwargs_absent)


def test_a_two_decisions_differing_only_in_authority_hash_differently():
    """End-to-end through the real constructor, not just the kwargs dict."""
    decision = _select()
    with_authority = dataclasses.replace(decision, authority=AUTHORITY_BLOCK)
    hash_without = dr.ps638_receipt_hash(decision.to_ps638_receipt_kwargs())
    hash_with = dr.ps638_receipt_hash(with_authority.to_ps638_receipt_kwargs())
    assert hash_with != hash_without


# (b) NEGATIVE CONTROL: compares against fixtures captured before `authority`
# existed, not a same-run rebuild.

def test_b_absent_authority_hashes_identically_to_the_pre_change_fixture():
    assert RECEIPT_FIXTURE["captured_from_sha"] == \
        "7afd55bad7034d789c98be4ee6e9ebcfcc97cdba"
    rebuilt = make_dispatch_receipt(**RECEIPT_FIXTURE["kwargs"])
    assert rebuilt.core() == RECEIPT_FIXTURE["core"]
    assert rebuilt.receipt_hash == RECEIPT_FIXTURE["receipt_hash"]
    assert "authority" not in rebuilt.core()


def test_b_absent_authority_hashes_identically_for_dispatch_decision():
    assert DECISION_FIXTURE["captured_from_sha"] == \
        "7afd55bad7034d789c98be4ee6e9ebcfcc97cdba"
    decision = _select()
    assert decision.receipt_hash == DECISION_FIXTURE["receipt_hash"]
    kwargs = decision.to_ps638_receipt_kwargs()
    assert dr.ps638_receipt_core(kwargs) == DECISION_FIXTURE["ps638_receipt_core"]
    assert "authority" not in dr.ps638_receipt_core(kwargs)


def test_b_negative_control_actually_exercises_the_receipt_serializer(monkeypatch):
    """Prove (b) is not vacuously true.

    Patches the real ``core()`` to serialize an absent ``authority`` as
    ``null`` and asserts the result diverges from the frozen fixture.
    """
    from src import execution_package as ep

    original_core = ep.DispatchDecisionReceipt.core

    def defective_core(self):
        payload = original_core(self)
        if self.authority is None:
            payload["authority"] = None  # the bug this suite exists to catch
        return payload

    monkeypatch.setattr(ep.DispatchDecisionReceipt, "core", defective_core)
    defective_receipt = make_dispatch_receipt(**RECEIPT_FIXTURE["kwargs"])
    assert defective_receipt.core() != RECEIPT_FIXTURE["core"]
    assert defective_receipt.receipt_hash != RECEIPT_FIXTURE["receipt_hash"]


def test_b_negative_control_actually_exercises_the_decision_serializer(monkeypatch):
    """Same non-vacuity check, for DispatchDecision's path."""
    original = dr.ps638_receipt_core

    def defective(kwargs):
        core = original(kwargs)
        if kwargs.get("authority") is None:
            core["authority"] = None  # the bug this suite exists to catch
        return core

    monkeypatch.setattr(dr, "ps638_receipt_core", defective)
    kwargs = dict(DECISION_FIXTURE["receipt_kwargs"])
    defective_hash = dr.ps638_receipt_hash(kwargs)
    assert defective_hash != DECISION_FIXTURE["receipt_hash"]


def test_b_absent_authority_is_omitted_from_the_kwargs_dict_itself():
    """The kwargs dict itself must omit an absent ``authority``.

    It is sealed verbatim as ``seal.evidence_hash`` input, so a ``None`` entry
    would move that hash even though ``ps638_receipt_core`` never sees the key.
    """
    decision = _select()
    kwargs = decision.to_ps638_receipt_kwargs()
    assert "authority" not in kwargs
    with_authority = dataclasses.replace(decision, authority={"grant_id": "g1"})
    assert "authority" in with_authority.to_ps638_receipt_kwargs()


def test_b_seal_evidence_hash_is_stable_for_an_authority_free_dispatch():
    """An authority-free real dispatch seals byte-identically to the fixture.

    Uses the unmodified tests/test_dispatch_boundary.py helpers, which also
    captured the fixture.
    """
    import test_dispatch_boundary as tdb
    from src import dispatch_boundary as dbd

    fixture = _load("ps638_seal_evidence_pre_authority.json")
    assert fixture["captured_from_sha"] == "7afd55bad7034d789c98be4ee6e9ebcfcc97cdba"

    db = tdb._db()
    task = tdb._seed(db)
    bound = tdb._resolve(db, task, "p-rtx", "p-openrouter", "p-msr")
    attempt = dbd.attempt_for(bound, attempt=1, profile_id="p-rtx", run_id="run-1")
    payload = tdb._sealed(bound, attempts=(attempt,), invocations=(
        {"target_id": "profile:p-rtx", "locality": "local"},))

    assert "authority" not in payload["decision_receipt"]
    assert payload["decision"]["receipt_hash"] == fixture["decision_receipt_hash"]
    assert payload["seal"]["evidence_hash"] == fixture["seal_evidence_hash"]


def test_b_seal_evidence_hash_changes_when_authority_is_present():
    """Positive control at the seal level, mirroring test_a_* above.

    Without this, `` test_b_seal_evidence_hash_is_stable...`` above could pass
    vacuously if `authority` were (incorrectly) excluded from the seal
    entirely, the same "unbound, forgeable evidence" failure mode F1's
    top-level docstring already calls out for `receipt_hash`.
    """
    import test_dispatch_boundary as tdb
    from src import dispatch_boundary as dbd

    db = tdb._db()
    task = tdb._seed(db)
    bound = tdb._resolve(db, task, "p-rtx", "p-openrouter", "p-msr")
    attempt = dbd.attempt_for(bound, attempt=1, profile_id="p-rtx", run_id="run-1")
    payload_without = tdb._sealed(bound, attempts=(attempt,), invocations=(
        {"target_id": "profile:p-rtx", "locality": "local"},))

    bound_with_authority = dataclasses.replace(
        bound, decision=dataclasses.replace(
            bound.decision, authority={"grant_id": "g1"}))
    attempt2 = dbd.attempt_for(bound_with_authority, attempt=1, profile_id="p-rtx",
                               run_id="run-1")
    payload_with = tdb._sealed(bound_with_authority, attempts=(attempt2,),
                               invocations=({"target_id": "profile:p-rtx",
                                             "locality": "local"},))

    assert payload_with["decision_receipt"]["authority"] == {"grant_id": "g1"}
    assert payload_with["seal"]["evidence_hash"] != payload_without["seal"]["evidence_hash"]


# (c) ROUND-TRIP / DETERMINISM

def test_c_key_order_inside_authority_does_not_change_the_hash():
    ordered_a = {"grant_id": "g1", "action": "run", "resource": "r1"}
    ordered_b = {"resource": "r1", "action": "run", "grant_id": "g1"}
    receipt_a = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                          "authority": ordered_a})
    receipt_b = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                          "authority": ordered_b})
    assert receipt_a.receipt_hash == receipt_b.receipt_hash


def test_c_explicit_null_subfield_equals_omitted_subfield():
    with_explicit_null = {"grant_id": "g1", "grant_expires_at": None}
    without_the_key = {"grant_id": "g1"}
    receipt_a = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                          "authority": with_explicit_null})
    receipt_b = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                          "authority": without_the_key})
    assert receipt_a.receipt_hash == receipt_b.receipt_hash
    assert receipt_a.core()["authority"] == receipt_b.core()["authority"] == \
        {"grant_id": "g1"}


def test_c_dispatch_decision_authority_is_also_order_and_null_stable():
    ordered_a = {"grant_id": "g1", "action": "run"}
    ordered_b = {"action": "run", "grant_id": "g1"}
    kwargs_base = dict(DECISION_FIXTURE["receipt_kwargs"])
    hash_a = dr.ps638_receipt_hash({**kwargs_base, "authority": ordered_a})
    hash_b = dr.ps638_receipt_hash({**kwargs_base, "authority": ordered_b})
    assert hash_a == hash_b

    with_null = {"grant_id": "g1", "grant_expires_at": None}
    without_key = {"grant_id": "g1"}
    hash_with_null = dr.ps638_receipt_hash({**kwargs_base, "authority": with_null})
    hash_without_key = dr.ps638_receipt_hash({**kwargs_base, "authority": without_key})
    assert hash_with_null == hash_without_key


def test_c_receipt_round_trips_through_dict_reconstruction():
    original = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                         "authority": AUTHORITY_BLOCK})
    payload = original.to_dict()
    rebuilt = make_dispatch_receipt(
        **{k: v for k, v in payload.items() if k != "receipt_hash"})
    assert rebuilt == original
    assert rebuilt.receipt_hash == original.receipt_hash
    assert rebuilt.core() == original.core()


def test_c_receipt_without_authority_round_trips_too():
    original = make_dispatch_receipt(**RECEIPT_FIXTURE["kwargs"])
    payload = original.to_dict()
    assert "authority" not in payload
    rebuilt = make_dispatch_receipt(
        **{k: v for k, v in payload.items() if k != "receipt_hash"})
    assert rebuilt == original
    assert rebuilt.receipt_hash == original.receipt_hash



def test_authority_is_never_read_to_permit_or_deny_anything():
    """No enforcement: constructing a receipt with ANY authority content must
    never raise, and constructing one WITHOUT authority must never raise
    either -- the field is optional, unvalidated, and decorative to the
    constructor's own logic."""
    # Content-free garbage is accepted as-is; schema policing is a later,
    # separate, sensitivity-dependent ruling -- not this one.
    receipt = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                        "authority": {"anything_at_all": 123,
                                                       "unexpected_key": ["x"]}})
    assert receipt.receipt_hash  # constructed successfully, nothing enforced

    receipt_empty = make_dispatch_receipt(**{**RECEIPT_FIXTURE["kwargs"],
                                              "authority": {}})
    assert receipt_empty.core()["authority"] == {}
