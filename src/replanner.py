"""Pure, bounded advisory proposal and gate for the local worker stall seam.

The advisor sees a deterministic projection and may suggest only an approach
for the next already-authorized attempt. This module has no provider, ledger,
filesystem, routing, or lifecycle authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.repair_packet import failure_fingerprint

PROPOSAL_SCHEMA_VERSION = 1
PLANNER_INPUT_SCHEMA_VERSION = 1
MAX_APPROACH_CHARS = 600
MAX_RATIONALE_CHARS = 1200
MAX_CONTEXT_CHARS = 4000
MAX_ADVISORY_RAW_BYTES = 65536
KINDS = frozenset({"next_packet", "replan", "approach_switch", "stop", "escalate"})
CONTINUING_KINDS = frozenset({"next_packet", "replan", "approach_switch"})
PROPOSAL_SCHEMAS = {
    "next_packet": {"required": ("rationale", "evidence_refs"), "forbidden": ("approach",)},
    "replan": {"required": ("rationale", "approach", "evidence_refs"), "forbidden": ()},
    "approach_switch": {"required": ("rationale", "approach", "evidence_refs"), "forbidden": ()},
    "stop": {"required": ("rationale", "evidence_refs"), "forbidden": ("approach",)},
    "escalate": {"required": ("rationale", "evidence_refs"), "forbidden": ("approach",)},
}
_DELTA_FAMILIES = {
    **dict.fromkeys(("packet_id", "package_id", "run_id", "objective", "contract", "role"), "identity_drift"),
    **dict.fromkeys(("interface", "interface_digest", "interface_error"), "interface_drift"),
    **dict.fromkeys(("base_sha", "source_snapshot", "source_digest", "source_identity", "worktree", "branch"), "source_drift"),
    **dict.fromkeys(("write_scope", "read_scope", "allowed_write_scope", "allowed_read_scope", "scope"), "scope_widening"),
    **dict.fromkeys(("test_command", "tests", "verification", "verifier", "verifier_id", "verifier_digest",
                     "acceptance_criteria", "negative_control", "positive_control", "verifier_paths",
                     "verifier_digests", "review", "reviewer",
                     "skip_verification", "bypass_verification", "skip_review", "bypass_review"), "verification_weakened"),
    **dict.fromkeys(("permissions", "allowed_tools", "tools", "capabilities", "network_policy", "egress",
                     "policy_ref", "capability_receipt_refs", "capacity_receipt_refs"), "permission_widening"),
    **dict.fromkeys(("route", "routing", "target", "target_id", "profile_id", "provider", "model",
                     "runtime", "runtime_identity", "endpoint", "endpoint_identity", "host", "locality"), "routing_refused"),
    **dict.fromkeys(("jira", "jira_key", "jira_transition", "jira_comment", "issue"), "tracker_mutation_refused"),
    **dict.fromkeys(("result", "lifecycle", "state", "accepted", "accept", "approve", "approval", "ship", "land", "merge", "deploy", "authority"), "authority_requested"),
    **dict.fromkeys(("budget", "max_attempts", "attempts", "timeout", "max_replans"), "budget_widening"),
}
_ACTION_FAMILIES = {
    **dict.fromkeys(("accept", "mark_accepted", "approve", "approval", "ship", "land", "merge", "deploy", "release", "set_lifecycle", "mutate_lifecycle", "transition"), "authority_requested"),
    **dict.fromkeys(("widen_scope", "widen_write_scope"), "scope_widening"),
    **dict.fromkeys(("widen_permissions", "grant_permission"), "permission_widening"),
    **dict.fromkeys(("bypass_verification", "skip_verification", "bypass_review", "skip_review", "weaken_verification"), "verification_weakened"),
    **dict.fromkeys(("route", "select_target", "switch_target", "select_model"), "routing_refused"),
    **dict.fromkeys(("mutate_jira", "update_jira", "transition_jira", "comment_jira", "jira_comment", "update_tracker"), "tracker_mutation_refused"),
    **dict.fromkeys(("extend_budget", "grant_attempts"), "budget_widening"),
    "change_interface": "interface_drift", "change_source": "source_drift", "change_objective": "identity_drift",
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def approach_digest(approach: str) -> str:
    text = (approach or "").strip()
    return _sha(text.encode("utf-8"))[:16] if text else ""


def envelope_digest(plan_input: "PlannerInput") -> str:
    return _sha(_canonical({
        "dispatch_hash": plan_input.dispatch_hash,
        "target_id": plan_input.target_id, "profile_id": plan_input.profile_id,
        "endpoint_identity": plan_input.endpoint_identity,
        "runtime_identity": list(plan_input.runtime_identity), "host": plan_input.host,
        "locality": plan_input.locality,
        "model": plan_input.model, "allowed_tools": list(plan_input.allowed_tools),
        "network_policy": plan_input.network_policy,
        "capabilities": list(plan_input.capabilities),
        "policy_ref": plan_input.policy_ref,
        "capability_receipt_refs": list(plan_input.capability_receipt_refs),
        "capacity_receipt_refs": list(plan_input.capacity_receipt_refs),
        "write_scope": list(plan_input.write_scope), "read_scope": list(plan_input.read_scope),
    }))


@dataclass(frozen=True)
class PlannerInput:
    """Small allowlisted projection of sealed package and canonical receipts."""
    run_id: str
    packet_id: str
    package_hash: str
    dispatch_hash: str
    boundary: str
    allowed_kinds: tuple[str, ...]
    attempts: tuple[str, ...]
    verifications: tuple[str, ...]
    evidence_index: tuple[str, ...]
    failure_fingerprint: str
    failure_excerpt: str
    interface: tuple[str, ...]
    interface_digest: str
    objective: str
    contract: str
    acceptance_criteria: tuple[str, ...]
    write_scope: tuple[str, ...]
    read_scope: tuple[str, ...]
    source_digest: str
    execution_role: str
    profile_id: str
    endpoint_identity: str
    runtime_identity: tuple[tuple[str, str], ...]
    target_id: str
    locality: str
    host: str
    model: str
    allowed_tools: tuple[str, ...]
    network_policy: str
    capabilities: tuple[str, ...]
    policy_ref: str
    capability_receipt_refs: tuple[str, ...]
    capacity_receipt_refs: tuple[str, ...]
    verifier_id: str
    verifier_digests: tuple[tuple[str, str], ...]
    attempts_used: int
    max_attempts: int
    budget_remaining: int
    prior_approach_digests: tuple[str, ...] = ()
    schema_version: int = PLANNER_INPUT_SCHEMA_VERSION

    def core(self) -> dict:
        return {"schema_version": self.schema_version, **{
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.__dict__.items() if key != "schema_version"}}

    @property
    def input_digest(self) -> str:
        return _sha(_canonical(self.core()))


@dataclass(frozen=True)
class ManagerProposal:
    proposal_id: str
    run_id: str
    packet_id: str
    kind: str
    rationale: str
    approach: str = ""
    evidence_refs: tuple[str, ...] = ()
    requested_actions: tuple[str, ...] = ()
    requested_authority: tuple[str, ...] = ()
    packet_delta: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PROPOSAL_SCHEMA_VERSION
    proposal_hash: str = ""

    def core(self) -> dict:
        return {"schema_version": self.schema_version, "proposal_id": self.proposal_id,
                "run_id": self.run_id, "packet_id": self.packet_id, "kind": self.kind,
                "rationale": self.rationale, "approach": self.approach,
                "evidence_refs": list(self.evidence_refs),
                "requested_actions": list(self.requested_actions),
                "requested_authority": list(self.requested_authority),
                "packet_delta": dict(self.packet_delta)}

    def to_dict(self) -> dict:
        return {**self.core(), "proposal_hash": self.proposal_hash}


@dataclass(frozen=True)
class ProposalVerdict:
    proposal_id: str = ""
    proposal_hash: str = ""
    ok: bool = False
    code: str = "schema_invalid"
    detail: str = ""
    approach: str = ""
    approach_digest: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def make_manager_proposal(**kwargs: Any) -> ManagerProposal:
    allowed = {"proposal_id", "run_id", "packet_id", "kind", "rationale", "approach",
               "evidence_refs", "requested_actions", "requested_authority", "packet_delta",
               "schema_version", "proposal_hash"}
    if set(kwargs) - allowed:
        raise ValueError("unknown proposal field")
    if any(not isinstance(kwargs.get(k), str) or not kwargs[k].strip()
           for k in ("proposal_id", "run_id", "packet_id", "kind", "rationale")):
        raise ValueError("proposal identity, kind, and rationale must be non-empty strings")
    if kwargs["kind"] not in KINDS:
        raise ValueError("unknown proposal kind")
    if len(kwargs["rationale"]) > MAX_RATIONALE_CHARS:
        raise ValueError("rationale exceeds bound")
    approach = kwargs.get("approach", "")
    if not isinstance(approach, str) or len(approach) > MAX_APPROACH_CHARS:
        raise ValueError("approach exceeds bound or is not text")
    seqs = {}
    for key in ("evidence_refs", "requested_actions", "requested_authority"):
        raw = kwargs.get(key, ())
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or any(
                not isinstance(item, str) or not item.strip() for item in raw):
            raise ValueError(f"{key} must be a sequence of non-empty strings")
        seqs[key] = tuple(raw)
    delta = kwargs.get("packet_delta", {})
    if not isinstance(delta, Mapping):
        raise ValueError("packet_delta must be an object")
    if type(kwargs.get("schema_version", 1)) is not int or kwargs.get("schema_version", 1) != 1:
        raise ValueError("unknown proposal schema version")
    core = {"proposal_id": kwargs["proposal_id"].strip(), "run_id": kwargs["run_id"].strip(),
            "packet_id": kwargs["packet_id"].strip(), "kind": kwargs["kind"],
            "rationale": kwargs["rationale"].strip(), "approach": approach.strip(),
            **seqs, "packet_delta": dict(delta), "schema_version": 1}
    provisional = ManagerProposal(**core)
    return ManagerProposal(**core, proposal_hash=_sha(_canonical(provisional.core())))


def parse_proposal_text(raw: str | bytes) -> ManagerProposal:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise ValueError("advisor response must be text")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("advisor response must be one JSON object")
    supplied_hash = value.get("proposal_hash")
    proposal = make_manager_proposal(**value)
    if supplied_hash is not None and supplied_hash != proposal.proposal_hash:
        raise ValueError("proposal hash does not match its fields")
    return proposal


def proposal_hash_is_valid(proposal: ManagerProposal) -> bool:
    try:
        return proposal.proposal_hash == _sha(_canonical(proposal.core()))
    except (AttributeError, TypeError, ValueError):
        return False


def _proposal_shape_error(proposal: ManagerProposal) -> str:
    for name in ("proposal_id", "run_id", "packet_id", "kind", "rationale"):
        value = getattr(proposal, name, None)
        if not isinstance(value, str) or not value.strip():
            return f"{name} must be non-empty text"
    if len(proposal.rationale) > MAX_RATIONALE_CHARS:
        return "rationale exceeds its bound"
    if not isinstance(proposal.approach, str) or len(proposal.approach) > MAX_APPROACH_CHARS:
        return "approach exceeds its bound or is not text"
    if type(proposal.schema_version) is not int or proposal.schema_version != PROPOSAL_SCHEMA_VERSION:
        return "unknown proposal schema version"
    for name in ("evidence_refs", "requested_actions", "requested_authority"):
        value = getattr(proposal, name, None)
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or any(
                not isinstance(item, str) or not item.strip() for item in value):
            return f"{name} must be a sequence of non-empty strings"
    if not isinstance(proposal.packet_delta, Mapping) or any(
            not isinstance(key, str) for key in proposal.packet_delta):
        return "packet_delta must be an object with string keys"
    return ""


def validate_proposal(proposal: ManagerProposal, *, plan_input: PlannerInput) -> ProposalVerdict:
    shape_error = _proposal_shape_error(proposal)
    if shape_error:
        return ProposalVerdict(getattr(proposal, "proposal_id", ""),
                               getattr(proposal, "proposal_hash", ""), False,
                               "schema_invalid", shape_error)
    if not proposal_hash_is_valid(proposal):
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "proposal_hash_mismatch", "proposal hash does not match")
    if (proposal.run_id, proposal.packet_id) != (plan_input.run_id, plan_input.packet_id):
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "run_mismatch", "proposal identity differs from the frozen run")
    if proposal.kind not in KINDS or proposal.kind not in plan_input.allowed_kinds:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "kind_not_allowed", "proposal kind is not recognized")
    if proposal.requested_authority:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "authority_requested", "proposal requested authority")
    for action in proposal.requested_actions:
        normalized = re.sub(r"_+", "_", "".join(
            c.lower() if c.isalnum() else "_" for c in action.strip()).strip("_"))
        code = _ACTION_FAMILIES.get(normalized, "unknown_action")
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               code, "proposal requested a disallowed action")
    for key in proposal.packet_delta:
        if key != "approach":
            return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                                   _DELTA_FAMILIES.get(key, "schema_invalid"),
                                   f"packet delta field {key!r} is not permitted")
    if "approach" in proposal.packet_delta and not isinstance(proposal.packet_delta["approach"], str):
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "schema_invalid", "approach delta must be text")
    approach = str(proposal.packet_delta.get("approach") or proposal.approach or "").strip()
    if len(approach) > MAX_APPROACH_CHARS:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "shape_invalid", "approach exceeds its bound")
    schema = PROPOSAL_SCHEMAS.get(proposal.kind)
    if schema is None:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "schema_invalid", "proposal kind has no schema")
    if "approach" in schema["required"] and not approach:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "shape_invalid", "continuing proposal requires a bounded approach")
    if "approach" in schema["forbidden"] and approach:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "shape_invalid", "next_packet cannot carry an approach")
    if not proposal.evidence_refs or any(ref not in plan_input.evidence_index for ref in proposal.evidence_refs):
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "evidence_unbound", "proposal evidence is not in this run")
    if proposal.kind not in CONTINUING_KINDS:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, True,
                               "", "terminal advisory", "", "")
    digest = approach_digest(approach)
    if digest and digest in plan_input.prior_approach_digests:
        return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, False,
                               "approach_repeated", "approach was already used")
    return ProposalVerdict(proposal.proposal_id, proposal.proposal_hash, True,
                           "", "bounded advisory accepted", approach, digest)


def derive_planner_input(package: Mapping[str, Any], bound: Any, *, attempts: Sequence[Any],
                         verifications: Sequence[Any], failure: Any, failure_excerpt: str,
                         budget_remaining: int, prior_approach_digests: Sequence[str] = ()) -> PlannerInput:
    """Derive and assert the advisor envelope from the exact frozen inputs."""
    d = bound.decision
    profile = d.selected_profile
    if (tuple(d.granted_tools) != tuple(package.get("allowed_tools") or ())
            or tuple(d.granted_write_scope) != tuple(package.get("write_scope") or ())
            or tuple(d.granted_read_scope) != tuple(package.get("read_scope") or ())
            or d.network_policy != package.get("network_policy")):
        raise ValueError("standing dispatch envelope differs from frozen package")
    receipt = bound.estate.receipt_for(profile.profile_id)
    decision_receipt = getattr(d, "selected_receipt", None)
    refs = tuple(d.capability_receipt_refs)
    if (receipt is None or decision_receipt is None
            or receipt.profile_id != profile.profile_id
            or decision_receipt.profile_id != profile.profile_id or not refs):
        raise ValueError("selected capability receipt is missing or mismatched")
    estate_ref = str(getattr(receipt, "source_receipt_hash", "") or receipt.receipt_hash)
    decision_ref = str(getattr(decision_receipt, "source_receipt_hash", "")
                       or decision_receipt.receipt_hash)
    if estate_ref != decision_ref or refs != (estate_ref,):
        raise ValueError("selected capability receipt differs from the decision evidence")
    if (getattr(getattr(bound, "policy", None), "policy_ref", "")
            != getattr(getattr(d, "policy", None), "policy_ref", "")):
        raise ValueError("bound policy differs from the dispatch decision")
    measured = tuple(getattr(receipt, "capabilities", ()) or ()) if receipt else ()
    required = tuple(package.get("required_capabilities") or ())
    if any(cap not in measured for cap in required):
        raise ValueError("selected measured capability receipt does not prove package requirements")
    verifier = package.get("verification") or {}
    source = package.get("source") or {}
    pin = bound.pin_for(profile.profile_id) or {}
    runtime_keys = ("provider", "runtime_kind", "runtime_version", "runtime_commit",
                    "runtime_image_digest", "backend", "backend_version", "model",
                    "model_digest", "runtime_options", "configured_context",
                    "configured_served_context")
    runtime_identity = tuple((key, json.dumps(pin.get(key), sort_keys=True,
                                               separators=(",", ":"), ensure_ascii=False,
                                               allow_nan=False)) for key in runtime_keys)
    refs_for_attempts = tuple(a.receipt_hash for a in attempts) + tuple(
        v.receipt_hash for v in verifications)
    return PlannerInput(
        run_id=str(package["run_id"]), packet_id=str(package["packet_id"]),
        package_hash=str(package["package_hash"]), dispatch_hash=str(d.receipt_hash),
        boundary="stall", allowed_kinds=tuple(sorted(KINDS)),
        attempts=tuple(json.dumps({"attempt": a.attempt,
            "receipt_hash": a.receipt_hash, "failure_class": a.failure_class,
            "repair_of": a.repair_of}, sort_keys=True, separators=(",", ":")) for a in attempts),
        verifications=tuple(json.dumps({"attempt": v.attempt, "receipt_hash": v.receipt_hash,
            "outcome": v.outcome, "exit_code": v.exit_code}, sort_keys=True,
            separators=(",", ":")) for v in verifications),
        evidence_index=refs_for_attempts, failure_fingerprint=str(failure_fingerprint(failure)),
        failure_excerpt=(failure_excerpt or "")[-MAX_CONTEXT_CHARS:],
        interface=tuple(package.get("interface") or ()), interface_digest=str(package.get("interface_digest") or ""),
        objective=str(package.get("objective") or ""), contract=str(package.get("contract") or ""),
        acceptance_criteria=tuple(package.get("acceptance_criteria") or ()),
        write_scope=tuple(package.get("write_scope") or ()), read_scope=tuple(package.get("read_scope") or ()),
        source_digest=str(source.get("snapshot_digest") or ""),
        execution_role=str(package.get("execution_role") or ""),
        profile_id=str(profile.profile_id),
        endpoint_identity=str(pin.get("endpoint_identity") or ""),
        runtime_identity=runtime_identity, target_id=str(profile.target_id),
        locality=str(profile.locality), host=str(profile.host), model=str(profile.model),
        allowed_tools=tuple(d.granted_tools),
        network_policy=str(d.network_policy), capabilities=tuple(c for c in required if c in measured),
        policy_ref=str(getattr(getattr(d, "policy", None), "policy_ref", "") or ""),
        capability_receipt_refs=refs, capacity_receipt_refs=tuple(getattr(d, "capacity_receipt_refs", ()) or ()),
        verifier_id=str(verifier.get("verifier_id") or ""),
        verifier_digests=tuple(tuple(x) for x in verifier.get("verifier_digests") or ()),
        attempts_used=len(attempts), max_attempts=int((package.get("budgets") or {}).get("max_attempts", 0)),
        budget_remaining=int(budget_remaining), prior_approach_digests=tuple(prior_approach_digests))


def validate_advisory_artifacts(attempt: Any, artifacts: Mapping[str, bytes], *,
                                planner_input: PlannerInput, proposal: ManagerProposal | None,
                                verdict: ProposalVerdict, journal_ref: Any, raw_ref: Any,
                                fault_ref: Any = None, verification: Any = None) -> bool:
    """Independently check the stored bytes and recompute input/gate binding."""
    try:
        journal_bytes = artifacts[journal_ref.storage_uri]
        if raw_ref is not None:
            raw = artifacts[raw_ref.storage_uri]
            if (len(raw) > MAX_ADVISORY_RAW_BYTES
                    or hashlib.sha256(raw).hexdigest() != raw_ref.sha256
                    or len(raw) != raw_ref.size):
                return False
        elif fault_ref is not None:
            fault = artifacts[fault_ref.storage_uri]
            if hashlib.sha256(fault).hexdigest() != fault_ref.sha256 or len(fault) != fault_ref.size:
                return False
        else:
            return False
        if hashlib.sha256(journal_bytes).hexdigest() != journal_ref.sha256 or len(journal_bytes) != journal_ref.size:
            return False
        journal = json.loads(journal_bytes.decode("utf-8"))
        if journal.get("schema") != "advisory-gate.v1" or journal.get("attempt") != attempt.attempt:
            return False
        if (journal.get("run_id") != planner_input.run_id
                or journal.get("packet_id") != planner_input.packet_id
                or attempt.run_id != planner_input.run_id
                or attempt.packet_id != planner_input.packet_id):
            return False
        if journal.get("package_hash") != attempt.execution_package_hash or journal.get("dispatch_hash") != attempt.dispatch_receipt_hash:
            return False
        if verification is not None and journal.get("verifier_hash") != verification.receipt_hash:
            return False
        if journal.get("planner_input_digest") != planner_input.input_digest:
            return False
        if journal.get("envelope_digest") != envelope_digest(planner_input):
            return False
        if _canonical(journal.get("planner_input")) != _canonical(planner_input.core()):
            return False
        if journal.get("raw_sha256") != (raw_ref.sha256 if raw_ref else "") or journal.get("fault_sha256") != (fault_ref.sha256 if fault_ref else "") or journal.get("proposal") != (proposal.to_dict() if proposal else None):
            return False
        if journal.get("proposal_hash") != (proposal.proposal_hash if proposal else ""):
            return False
        if journal.get("raw_ref") != (raw_ref.to_dict() if raw_ref else None) or journal.get("fault_ref") != (fault_ref.to_dict() if fault_ref else None):
            return False
        expected_action = ("continue" if verdict.ok and proposal is not None
                           and proposal.kind in CONTINUING_KINDS else "escalate")
        if journal.get("controller_action") != expected_action:
            return False
        if journal.get("verdict") != verdict.to_dict():
            return False
        if proposal is not None and validate_proposal(proposal, plan_input=planner_input).to_dict() != verdict.to_dict():
            return False
        refs = {ref.sha256 for ref in attempt.artifact_refs}
        if journal_ref.sha256 not in refs or raw_ref is not None and raw_ref.sha256 not in refs or fault_ref is not None and fault_ref.sha256 not in refs:
            return False
        if journal.get("failure_fingerprint") != planner_input.failure_fingerprint:
            return False
        if (not isinstance(journal.get("shared_budget_before"), int)
                or not isinstance(journal.get("shared_budget_after"), int)
                or journal["shared_budget_before"] - journal["shared_budget_after"] != 1
                or journal["shared_budget_after"] != planner_input.budget_remaining):
            return False
        if journal.get("advisory_budget_before") != 1 or journal.get("advisory_budget_after") != 0:
            return False
        if raw_ref is not None:
            if proposal is None:
                if verdict.code != "schema_invalid":
                    return False
                try:
                    parse_proposal_text(raw)
                except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                    pass
                else:
                    return False
            elif parse_proposal_text(raw).to_dict() != proposal.to_dict():
                return False
        elif fault_ref is not None and verdict.code != "planner_fault":
            return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
        return False
