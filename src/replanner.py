"""src/replanner.py — the smallest G2 manager/replanner seam (PS-635).

G1 is a LOOP whose manager is code: packet -> fresh bounded context -> worker ->
deterministic verify -> compact repair -> fresh retry -> pass or escalate. Every
decision it can reach was fixed in advance, which is why it is trustworthy and
why it cannot do anything else.

G2 puts a MODEL in an advisory role. That is the whole risk, so this module is
the whole seam and it is deliberately narrow:

    canonical state   ->  PlannerInput        a bounded PROJECTION, not a transcript
    PlannerInput      ->  ManagerProposal     typed, schema-validated, sealed
    ManagerProposal   ->  ProposalVerdict     deterministic, typed, fail-closed
    ProposalVerdict   ->  the loop may act    or it does not, and the record says why

Three properties make an advisory model safe here, and they are structural
rather than promised:

**1. The planner cannot express authority.** There is no ``kind`` for accepting,
landing, routing or mutating Jira, and the schema refuses unknown kinds — so
"mark this ACCEPTED" is not a refusal the gate has to catch, it is a sentence the
schema cannot spell. A proposal may still ASK (``requested_actions``,
``requested_authority``, or a ``packet_delta`` key outside its one permitted
lever), and that is representable on purpose: a refusal with a typed reason is
better evidence than an impossible request.

**2. The planner's only lever is the APPROACH.** ``packet_delta`` accepts exactly
one key, ``approach`` — bounded steering text for the next attempt. Scope,
interface, source, verification, permissions, routing, budget and identity are
each named explicitly and refused by their own code if touched. The gate then
re-validates the EFFECTIVE packet through the WorkPacket primitive, so a proposal
cannot enter dispatch on a packet shape the primitive would reject.

**3. The planner's input is the canonical record, not the conversation.** The
projection is built from the append-only ledger (identity, attempts, verdicts,
repairs, decisions) plus the validated packet and the declared policy. Worker
output, prose transcripts and filesystem state are excluded, because a manager
reasoning over a transcript is a manager reasoning over an unverifiable story.

Everything here is deterministic and hermetic: no model is called, no file is
written, no lifecycle state is changed. The live advisor lives in the harness and
returns a :class:`ManagerProposal`; validation is this module's business.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Any, List, Mapping, Optional, Sequence, Tuple

PROPOSAL_SCHEMA_VERSION = 1
PLANNER_INPUT_SCHEMA_VERSION = 1

# ----------------------------------------------------------------- proposal ---
#: A proposal may advise only these five things. The absence of ``accept``,
#: ``ship``, ``land``, ``route`` and friends is the point: unrepresentable
#: authority cannot be exercised by accident or by a well-argued model.
KIND_NEXT_PACKET = "next_packet"
KIND_REPLAN = "replan"
KIND_APPROACH_SWITCH = "approach_switch"
KIND_STOP = "stop"
KIND_ESCALATE = "escalate"

KNOWN_KINDS = frozenset({
    KIND_NEXT_PACKET, KIND_REPLAN, KIND_APPROACH_SWITCH, KIND_STOP, KIND_ESCALATE,
})

#: Kinds that ask the loop to spend another bounded attempt.
CONTINUING_KINDS = frozenset({KIND_NEXT_PACKET, KIND_REPLAN, KIND_APPROACH_SWITCH})
#: Kinds that must carry new approach text (a "switch" without a new approach is
#: the same attempt again, which is what the no-progress rule exists to stop).
APPROACH_REQUIRED_KINDS = frozenset({KIND_REPLAN, KIND_APPROACH_SWITCH})

MAX_APPROACH_CHARS = 600
MAX_RATIONALE_CHARS = 1200
DEFAULT_MAX_CONTEXT_CHARS = 4000

#: Boundaries at which the loop may consult the planner. The boundary is a
#: deterministic fact about the run, not the manager's opinion about it.
BOUNDARY_PASS = "pass"
BOUNDARY_STALL = "stall"
BOUNDARY_EXHAUSTED = "exhausted"
BOUNDARY_BLOCKED = "blocked"

PASS_KINDS: Tuple[str, ...] = (KIND_STOP,)
STALL_KINDS: Tuple[str, ...] = (KIND_NEXT_PACKET, KIND_REPLAN, KIND_APPROACH_SWITCH,
                                KIND_STOP, KIND_ESCALATE)
TERMINAL_KINDS: Tuple[str, ...] = (KIND_STOP, KIND_ESCALATE)


def allowed_kinds_for(boundary: str, *, budget_remaining: int,
                      replans_remaining: int) -> Tuple[str, ...]:
    """The kinds that are legal for this deterministic state.

    Computed HERE rather than offered to the caller as a parameter, so the
    manager cannot be handed a permission set that its state does not support.
    """
    if boundary == BOUNDARY_PASS:
        # Deterministic verification already won. A model may agree that the run
        # is over; it may not restart, replan or override a PASS.
        return PASS_KINDS
    if boundary == BOUNDARY_BLOCKED:
        return TERMINAL_KINDS
    if int(budget_remaining) <= 0 or int(replans_remaining) <= 0:
        return TERMINAL_KINDS
    if boundary in (BOUNDARY_STALL, BOUNDARY_EXHAUSTED):
        return STALL_KINDS
    return TERMINAL_KINDS


# ----------------------------------------------------------- verdict codes ---
#: The proposal is not a proposal (shape, types, unknown kind, unknown field).
CODE_SCHEMA_INVALID = "schema_invalid"
#: The proposal names a run/packet other than the canonical one it was made for.
CODE_RUN_MISMATCH = "run_mismatch"
#: The proposal's own hash does not match its content.
CODE_PROPOSAL_HASH_MISMATCH = "proposal_hash_mismatch"
#: A field-level rule that is not one of the families below was broken.
CODE_SHAPE_INVALID = "shape_invalid"
#: This kind is not legal for the deterministic state (a replan after the
#: verifier already PASSed, for instance).
CODE_KIND_NOT_ALLOWED = "kind_not_allowed"
#: Asked for acceptance/approval/ship/land or a lifecycle mutation.
CODE_AUTHORITY_REQUESTED = "authority_requested"
#: Wanted a broader tool/network/capability envelope.
CODE_PERMISSION_WIDENING = "permission_widening"
#: Wanted to pick or change the execution target/provider/model.
CODE_ROUTING_REFUSED = "routing_refused"
#: Wanted to write to Jira.
CODE_JIRA_MUTATION_REFUSED = "jira_mutation_refused"
#: Wanted a different write or read scope.
CODE_SCOPE_WIDENING = "scope_widening"
#: Wanted a different or weaker verification (a changed test, a restated
#: criterion, a skipped review). Never legal: the planner is not the verifier.
CODE_VERIFICATION_WEAKENED = "verification_weakened"
#: Wanted to change what this work is.
CODE_IDENTITY_DRIFT = "identity_drift"
#: Wanted to change the sealed interface.
CODE_INTERFACE_DRIFT = "interface_drift"
#: Wanted to change the source the work is applied to.
CODE_SOURCE_DRIFT = "source_drift"
#: Wanted more attempts or time than the canonical budget allows.
CODE_BUDGET_WIDENING = "budget_widening"
#: Requested an action string outside the known vocabulary (fail closed).
CODE_UNKNOWN_ACTION = "unknown_action"
#: The effective packet is not dispatchable under the WorkPacket primitive.
CODE_PACKET_INVALID = "packet_invalid"
#: The proposal points back at work that already ran (an approach or packet
#: cycle).
CODE_CYCLIC_PACKET = "cyclic_packet"
#: Cited evidence that is not in this run's canonical ledger.
CODE_EVIDENCE_UNBOUND = "evidence_unbound"
#: No attempts remain.
CODE_BUDGET_EXHAUSTED = "budget_exhausted"
#: The bounded replan allowance for this run is already spent.
CODE_REPLANS_EXHAUSTED = "replans_exhausted"
#: The approach is not new, so acting on it would repeat a failed attempt.
CODE_APPROACH_REPEATED = "approach_repeated"
#: The advisor itself faulted (its input could not be derived, or it raised).
#: A fault in the planner is not evidence about the work, so it has its own code.
CODE_PLANNER_FAULT = "planner_fault"

KNOWN_CODES = frozenset({
    CODE_SCHEMA_INVALID, CODE_RUN_MISMATCH, CODE_PROPOSAL_HASH_MISMATCH,
    CODE_SHAPE_INVALID, CODE_KIND_NOT_ALLOWED, CODE_AUTHORITY_REQUESTED,
    CODE_PERMISSION_WIDENING, CODE_ROUTING_REFUSED, CODE_JIRA_MUTATION_REFUSED,
    CODE_SCOPE_WIDENING, CODE_VERIFICATION_WEAKENED, CODE_IDENTITY_DRIFT,
    CODE_INTERFACE_DRIFT, CODE_SOURCE_DRIFT, CODE_BUDGET_WIDENING,
    CODE_UNKNOWN_ACTION, CODE_PACKET_INVALID, CODE_CYCLIC_PACKET,
    CODE_EVIDENCE_UNBOUND, CODE_BUDGET_EXHAUSTED, CODE_REPLANS_EXHAUSTED,
    CODE_APPROACH_REPEATED, CODE_PLANNER_FAULT,
})

#: The ONE packet-delta key a manager may set.
DELTA_APPROACH = "approach"

#: Every other delta key, mapped to the family that refuses it. Explicit rather
#: than a denylist-by-prefix, because an unrecognised key must fail closed
#: (CODE_SCHEMA_INVALID) instead of being quietly ignored.
_DELTA_FAMILIES: dict = {
    # identity: what the work IS
    "packet_id": CODE_IDENTITY_DRIFT, "package_id": CODE_IDENTITY_DRIFT,
    "run_id": CODE_IDENTITY_DRIFT, "objective": CODE_IDENTITY_DRIFT,
    "contract": CODE_IDENTITY_DRIFT, "role": CODE_IDENTITY_DRIFT,
    # the sealed interface
    "interface": CODE_INTERFACE_DRIFT, "interface_digest": CODE_INTERFACE_DRIFT,
    "interface_error": CODE_INTERFACE_DRIFT,
    # the source the work applies to
    "base_sha": CODE_SOURCE_DRIFT, "source_snapshot": CODE_SOURCE_DRIFT,
    "worktree": CODE_SOURCE_DRIFT, "branch": CODE_SOURCE_DRIFT,
    # the scopes
    "write_scope": CODE_SCOPE_WIDENING, "read_scope": CODE_SCOPE_WIDENING,
    "allowed_write_scope": CODE_SCOPE_WIDENING, "scope": CODE_SCOPE_WIDENING,
    # verification and review
    "test_command": CODE_VERIFICATION_WEAKENED, "tests": CODE_VERIFICATION_WEAKENED,
    "verification": CODE_VERIFICATION_WEAKENED, "verifier": CODE_VERIFICATION_WEAKENED,
    "verifier_id": CODE_VERIFICATION_WEAKENED,
    "verifier_digest": CODE_VERIFICATION_WEAKENED,
    "acceptance_criteria": CODE_VERIFICATION_WEAKENED,
    "negative_control": CODE_VERIFICATION_WEAKENED,
    "positive_control": CODE_VERIFICATION_WEAKENED,
    "review": CODE_VERIFICATION_WEAKENED, "reviewer": CODE_VERIFICATION_WEAKENED,
    "skip_verification": CODE_VERIFICATION_WEAKENED,
    "bypass_verification": CODE_VERIFICATION_WEAKENED,
    "skip_review": CODE_VERIFICATION_WEAKENED,
    "bypass_review": CODE_VERIFICATION_WEAKENED,
    # permissions / envelope
    "permissions": CODE_PERMISSION_WIDENING, "allowed_tools": CODE_PERMISSION_WIDENING,
    "tools": CODE_PERMISSION_WIDENING, "capabilities": CODE_PERMISSION_WIDENING,
    "network_policy": CODE_PERMISSION_WIDENING, "egress": CODE_PERMISSION_WIDENING,
    # routing
    "route": CODE_ROUTING_REFUSED, "routing": CODE_ROUTING_REFUSED,
    "target": CODE_ROUTING_REFUSED, "target_id": CODE_ROUTING_REFUSED,
    "provider": CODE_ROUTING_REFUSED, "model": CODE_ROUTING_REFUSED,
    "runtime": CODE_ROUTING_REFUSED, "endpoint": CODE_ROUTING_REFUSED,
    # Jira
    "jira": CODE_JIRA_MUTATION_REFUSED, "jira_key": CODE_JIRA_MUTATION_REFUSED,
    "jira_transition": CODE_JIRA_MUTATION_REFUSED,
    "jira_comment": CODE_JIRA_MUTATION_REFUSED, "issue": CODE_JIRA_MUTATION_REFUSED,
    # authority / lifecycle
    "result": CODE_AUTHORITY_REQUESTED, "lifecycle": CODE_AUTHORITY_REQUESTED,
    "state": CODE_AUTHORITY_REQUESTED, "accepted": CODE_AUTHORITY_REQUESTED,
    "accept": CODE_AUTHORITY_REQUESTED, "approve": CODE_AUTHORITY_REQUESTED,
    "approval": CODE_AUTHORITY_REQUESTED, "ship": CODE_AUTHORITY_REQUESTED,
    "land": CODE_AUTHORITY_REQUESTED, "merge": CODE_AUTHORITY_REQUESTED,
    "deploy": CODE_AUTHORITY_REQUESTED, "authority": CODE_AUTHORITY_REQUESTED,
    # budget
    "budget": CODE_BUDGET_WIDENING, "max_attempts": CODE_BUDGET_WIDENING,
    "attempts": CODE_BUDGET_WIDENING, "timeout": CODE_BUDGET_WIDENING,
    "max_replans": CODE_BUDGET_WIDENING,
}

#: Action strings, mapped to the family that refuses them. EVERY requested
#: action is refused — there is no allowed action — but the vocabulary exists so
#: a refusal can name a family, and so an unrecognised action fails closed with
#: its own code rather than being silently tolerated.
_ACTION_FAMILIES: dict = {
    "accept": CODE_AUTHORITY_REQUESTED, "mark_accepted": CODE_AUTHORITY_REQUESTED,
    "approve": CODE_AUTHORITY_REQUESTED, "approval": CODE_AUTHORITY_REQUESTED,
    "ship": CODE_AUTHORITY_REQUESTED, "land": CODE_AUTHORITY_REQUESTED,
    "merge": CODE_AUTHORITY_REQUESTED, "deploy": CODE_AUTHORITY_REQUESTED,
    "release": CODE_AUTHORITY_REQUESTED, "set_lifecycle": CODE_AUTHORITY_REQUESTED,
    "mutate_lifecycle": CODE_AUTHORITY_REQUESTED,
    "transition": CODE_AUTHORITY_REQUESTED,
    "widen_scope": CODE_SCOPE_WIDENING, "widen_write_scope": CODE_SCOPE_WIDENING,
    "widen_permissions": CODE_PERMISSION_WIDENING,
    "grant_permission": CODE_PERMISSION_WIDENING,
    "bypass_verification": CODE_VERIFICATION_WEAKENED,
    "skip_verification": CODE_VERIFICATION_WEAKENED,
    "bypass_review": CODE_VERIFICATION_WEAKENED,
    "skip_review": CODE_VERIFICATION_WEAKENED,
    "weaken_verification": CODE_VERIFICATION_WEAKENED,
    "route": CODE_ROUTING_REFUSED, "select_target": CODE_ROUTING_REFUSED,
    "switch_target": CODE_ROUTING_REFUSED, "select_model": CODE_ROUTING_REFUSED,
    "mutate_jira": CODE_JIRA_MUTATION_REFUSED,
    "update_jira": CODE_JIRA_MUTATION_REFUSED,
    "transition_jira": CODE_JIRA_MUTATION_REFUSED,
    "comment_jira": CODE_JIRA_MUTATION_REFUSED,
    "extend_budget": CODE_BUDGET_WIDENING, "grant_attempts": CODE_BUDGET_WIDENING,
    "change_interface": CODE_INTERFACE_DRIFT,
    "change_source": CODE_SOURCE_DRIFT, "change_objective": CODE_IDENTITY_DRIFT,
}


def _normalize_action(value: Any) -> str:
    """Lowercase, collapse anything non-alphanumeric to ``_``. Deterministic."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value).strip())
    return "_".join(part for part in cleaned.split("_") if part)


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean(value: Any, field_name: str, *, required: bool = True,
           limit: int = 0) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ReplannerError(f"{field_name} must be non-empty")
    if limit and len(text) > limit:
        raise ReplannerError(
            f"{field_name} exceeds its {limit}-character bound ({len(text)})")
    return text


class ReplannerError(ValueError):
    """Raised when a proposal cannot even be CONSTRUCTED (shape, not semantics).

    A malformed proposal is a schema failure and stops here. Whether a
    well-formed proposal may be ACTED on is a different question, answered by
    :func:`validate_proposal` with a typed code — conflating the two would make
    "the manager wrote nonsense" indistinguishable from "the manager asked for
    something it may not have", and those are different findings.
    """


def approach_digest(approach: str) -> str:
    """Stable 16-hex digest of an approach note. Empty note -> empty digest."""
    text = (approach or "").strip()
    if not text:
        return ""
    return _sha256_hex(text.encode("utf-8"))[:16]


@dataclass(frozen=True)
class ManagerProposal:
    """One advisory proposal, sealed and typed.

    Deliberately NOT here: a lifecycle state, an accepted flag, a target, a
    permission set, a scope, a budget, or a verifier. The proposal states what it
    THINKS should happen next; the canonical state decides whether that is
    representable.
    """

    proposal_id: str
    run_id: str
    packet_id: str
    kind: str
    rationale: str
    approach: str = ""
    evidence_refs: Tuple[str, ...] = ()
    requested_actions: Tuple[str, ...] = ()
    requested_authority: Tuple[str, ...] = ()
    packet_delta: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PROPOSAL_SCHEMA_VERSION
    proposal_hash: str = field(default="")

    def __post_init__(self) -> None:
        _clean(self.proposal_id, "proposal_id", limit=200)
        _clean(self.run_id, "run_id", limit=200)
        _clean(self.packet_id, "packet_id", limit=200)
        if self.kind not in KNOWN_KINDS:
            raise ReplannerError(
                f"kind {self.kind!r} is not a manager proposal kind; known: "
                f"{sorted(KNOWN_KINDS)}. Authority (accept/ship/land/route) is "
                "deliberately unrepresentable here.")
        _clean(self.rationale, "rationale", limit=MAX_RATIONALE_CHARS)
        if len(self.approach or "") > MAX_APPROACH_CHARS:
            raise ReplannerError(
                f"approach exceeds its {MAX_APPROACH_CHARS}-character bound "
                f"({len(self.approach)})")
        if not isinstance(self.packet_delta, Mapping):
            raise ReplannerError(
                f"packet_delta must be a mapping, got "
                f"{type(self.packet_delta).__name__}")
        for name in ("evidence_refs", "requested_actions", "requested_authority"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ReplannerError(f"{name} must be a sequence of strings")
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise ReplannerError(f"{name} entries must be non-empty strings")
        if self.schema_version != PROPOSAL_SCHEMA_VERSION:
            raise ReplannerError(
                f"unknown proposal schema_version {self.schema_version!r}; this "
                f"reader implements {PROPOSAL_SCHEMA_VERSION}")

    @property
    def approach_required(self) -> bool:
        return self.kind in APPROACH_REQUIRED_KINDS

    @property
    def continuing(self) -> bool:
        return self.kind in CONTINUING_KINDS

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "kind": self.kind,
            "rationale": self.rationale,
            "approach": self.approach,
            "evidence_refs": list(self.evidence_refs),
            "requested_actions": list(self.requested_actions),
            "requested_authority": list(self.requested_authority),
            "packet_delta": dict(self.packet_delta),
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["proposal_hash"] = self.proposal_hash
        return payload


def compute_proposal_hash(core: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical(dict(core)))


_PROPOSAL_FIELDS = {f.name for f in fields(ManagerProposal)}


def make_manager_proposal(**kwargs: Any) -> ManagerProposal:
    """Validate the SHAPE, then seal. Authority is NOT checked here.

    Unvalidated construction is impossible through this function, which is why
    the loop only accepts proposals that came through it: a raw dict typed in
    from a model's text cannot reach canonical state without passing a schema.
    """
    unknown = set(kwargs) - _PROPOSAL_FIELDS
    if unknown:
        raise ReplannerError(
            f"unknown field(s) for ManagerProposal: {sorted(unknown)}")
    core: dict = {}
    for name in ("proposal_id", "run_id", "packet_id", "kind", "rationale",
                 "approach", "schema_version"):
        if name in kwargs:
            core[name] = kwargs[name]
    for name in ("evidence_refs", "requested_actions", "requested_authority"):
        if name in kwargs:
            value = kwargs[name]
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ReplannerError(f"{name} must be a sequence of strings")
            core[name] = tuple(str(v).strip() for v in value)
    if "packet_delta" in kwargs:
        delta = kwargs["packet_delta"] or {}
        if not isinstance(delta, Mapping):
            raise ReplannerError("packet_delta must be a mapping")
        core["packet_delta"] = {str(k).strip(): v for k, v in delta.items()}
    core.setdefault("schema_version", PROPOSAL_SCHEMA_VERSION)
    core.setdefault("approach", "")
    core.setdefault("packet_delta", {})
    provisional = ManagerProposal(**core)
    return ManagerProposal(proposal_hash=compute_proposal_hash(provisional.core()),
                           **core)


def proposal_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """True when a serialized proposal's hash matches its own fields."""
    if not isinstance(payload, Mapping) or not payload.get("proposal_hash"):
        return False
    core = {k: v for k, v in payload.items() if k != "proposal_hash"}
    return compute_proposal_hash(core) == payload["proposal_hash"]


# ------------------------------------------------------------ planner input ---
@dataclass(frozen=True)
class PlannerPolicy:
    """Facts about the run's envelope that the packet itself does not carry.

    Passed in explicitly so the projection can state what was GRANTED. A manager
    that cannot see the envelope would propose within it by luck.
    """

    allowed_tools: Tuple[str, ...] = ()
    network_policy: str = ""
    capabilities: Tuple[str, ...] = ()
    verifier_id: str = ""
    verifier_digest: str = ""


def _entries(ledger: Any, run_id: str) -> list:
    """Canonical entries for a run, via the ledger's own reader."""
    if ledger is None:
        return []
    return list(ledger.entries_for(run_id))


@dataclass(frozen=True)
class PlannerInput:
    """The bounded projection a manager reasons over.

    Every field is either a fact recorded in the append-only ledger, a field of
    the VALIDATED packet, or part of the declared envelope. Nothing here is read
    from a worker conversation, a worker's model output, or the filesystem: those
    are the things a manager cannot independently check, and a proposal grounded
    in them would carry its author's story as if it were evidence.
    """

    run_id: str
    packet_id: str
    objective: str
    boundary: str
    allowed_kinds: Tuple[str, ...]
    attempts: Tuple[Mapping[str, Any], ...] = ()
    verifications: Tuple[Mapping[str, Any], ...] = ()
    failure_fingerprints: Tuple[str, ...] = ()
    prior_approach_digests: Tuple[str, ...] = ()
    evidence_index: Tuple[str, ...] = ()
    target_id: str = ""
    host: str = ""
    model: str = ""
    served_context: Optional[int] = None
    worktree: str = ""
    base_sha: str = ""
    write_scope: Tuple[str, ...] = ()
    read_scope: Tuple[str, ...] = ()
    interface: Tuple[str, ...] = ()
    interface_digest: str = ""
    test_command: str = ""
    allowed_tools: Tuple[str, ...] = ()
    network_policy: str = ""
    capabilities: Tuple[str, ...] = ()
    verifier_id: str = ""
    verifier_digest: str = ""
    objective_ok: bool = True
    attempts_used: int = 0
    max_attempts: int = 0
    budget_remaining: int = 0
    replans_used: int = 0
    max_replans: int = 0
    last_failure_excerpt: str = ""
    input_digest: str = field(default="")
    schema_version: int = PLANNER_INPUT_SCHEMA_VERSION

    @property
    def replans_remaining(self) -> int:
        return max(0, int(self.max_replans) - int(self.replans_used))

    @property
    def latest_verification(self) -> Mapping[str, Any]:
        return self.verifications[-1] if self.verifications else {}

    def core(self) -> dict:
        """Canonical, JSON-safe form. This is what the input digest covers."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "objective": self.objective,
            "identity": {
                "target_id": self.target_id, "host": self.host,
                "model": self.model, "served_context": self.served_context,
                "worktree": self.worktree, "base_sha": self.base_sha,
            },
            "scope": {"write_scope": list(self.write_scope),
                      "read_scope": list(self.read_scope)},
            "interface": {"digest": self.interface_digest,
                          "fields": list(self.interface)},
            "verification": {"test_command": self.test_command,
                             "verifier_id": self.verifier_id,
                             "verifier_digest": self.verifier_digest},
            "policy": {"allowed_tools": list(self.allowed_tools),
                       "network_policy": self.network_policy,
                       "capabilities": list(self.capabilities)},
            "boundary": {"kind": self.boundary,
                         "allowed_kinds": list(self.allowed_kinds)},
            "budget": {"attempts_used": self.attempts_used,
                       "max_attempts": self.max_attempts,
                       "budget_remaining": self.budget_remaining,
                       "replans_used": self.replans_used,
                       "max_replans": self.max_replans},
            "attempts": [dict(a) for a in self.attempts],
            "verifications": [dict(v) for v in self.verifications],
            "failure_fingerprints": list(self.failure_fingerprints),
            "prior_approach_digests": list(self.prior_approach_digests),
            "last_failure_excerpt": self.last_failure_excerpt,
            "evidence_index": list(self.evidence_index),
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["input_digest"] = self.input_digest
        return payload


def compute_input_digest(core: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical(dict(core)))



#: Bounded excerpt of the failing verifier evidence the manager is shown. The
#: ledger already bounds what it stores; this bounds what the manager's context
#: spends, so a manager turn cannot grow unboundedly with a long test log.
FAILURE_EXCERPT_CHARS = 1200


def build_planner_input(*, ledger: Any, run_id: str, packet: Mapping[str, Any],
                        boundary: str, max_attempts: int, max_replans: int = 1,
                        replans_used: int = 0,
                        policy: Optional[PlannerPolicy] = None,
                        validated: Any = None) -> PlannerInput:
    """Project CANONICAL state into a bounded planner input.

    Reads: the append-only ledger, the packet that was validated at dispatch
    time, and the declared envelope. Does NOT read: the worktree, any worker
    model output, any prior conversation. Those exclusions are what make the
    projection checkable by a third party — everything in it is reconstructible
    from the ledger and the sealed package.
    """
    from src.work_packet import coerce_interface, interface_digest_from_normalized

    policy = policy or PlannerPolicy()
    entries = _entries(ledger, run_id)
    run_entry = next((e for e in entries if e.kind == "run"), None)
    identity = dict(run_entry.payload) if run_entry is not None else {}

    attempts = tuple(dict(e.payload) for e in entries if e.kind == "attempt")
    verifications = tuple(dict(e.payload) for e in entries
                          if e.kind == "verification")
    repairs = tuple(dict(e.payload) for e in entries if e.kind == "repair")
    fingerprints = tuple(str(r.get("fingerprint") or "") for r in repairs)
    proposals = tuple(dict(e.payload) for e in entries if e.kind == "proposal")
    prior_approaches = tuple(sorted({
        str(p.get("approach_digest") or "") for p in proposals
        if p.get("accepted") and p.get("approach_digest")}))

    if validated is not None:
        interface_lines = tuple(validated.normalized_interface())
        interface_digest = validated.interface_digest
    else:
        try:
            interface_lines = tuple(f.normalized() for f in
                                    coerce_interface(packet.get("interface")))
            interface_digest = interface_digest_from_normalized(interface_lines)
        except Exception:
            interface_lines, interface_digest = (), ""

    excerpt = ""
    if verifications:
        excerpt = str(verifications[-1].get("excerpt") or "")[-FAILURE_EXCERPT_CHARS:]

    used = len(attempts)
    budget_remaining = max(0, int(max_attempts) - used)
    replans_used = int(replans_used)
    allowed = allowed_kinds_for(
        boundary, budget_remaining=budget_remaining,
        replans_remaining=max(0, int(max_replans) - replans_used))

    # PROJECTED rows, not the raw ledger payloads. The projection is the point:
    # a field added to an attempt entry later cannot reach the manager without
    # someone editing this list, which is what makes the input auditable.
    attempt_rows = tuple({
        "attempt": a.get("attempt"), "target_id": a.get("target_id"),
        "model": a.get("model"), "rounds": a.get("rounds"),
        "artifacts": a.get("artifacts"), "failure_class": a.get("failure_class"),
        "status": a.get("status"), "elapsed_s": a.get("elapsed_s"),
    } for a in attempts)
    verification_rows = tuple({
        "attempt": v.get("attempt"), "test_command": v.get("test_command"),
        "passed": v.get("passed"), "returncode": v.get("returncode"),
        "summary": v.get("summary"),
    } for v in verifications)


    core = {
        "schema_version": PLANNER_INPUT_SCHEMA_VERSION,
        "run_id": run_id,
        "packet_id": str(packet.get("packet_id", "")),
        "objective": str(packet.get("objective", "")),
        "identity": {
            "target_id": str(identity.get("target_id") or ""),
            "host": str(identity.get("host") or ""),
            "model": str(identity.get("model") or ""),
            "served_context": identity.get("served_context"),
            "worktree": str(identity.get("worktree") or ""),
            "base_sha": str(packet.get("base_sha") or identity.get("base_sha") or ""),
        },
        "scope": {"write_scope": [str(s) for s in packet.get("write_scope") or ()],
                  "read_scope": [str(s) for s in packet.get("read_scope") or ()]},
        "interface": {"digest": interface_digest, "fields": list(interface_lines)},
        "verification": {"test_command": str(packet.get("test_command") or ""),
                         "verifier_id": policy.verifier_id,
                         "verifier_digest": policy.verifier_digest},
        "policy": {"allowed_tools": list(policy.allowed_tools),
                   "network_policy": policy.network_policy,
                   "capabilities": list(policy.capabilities)},
        "boundary": {"kind": boundary, "allowed_kinds": list(allowed)},
        "budget": {"attempts_used": used, "max_attempts": int(max_attempts),
                   "budget_remaining": budget_remaining,
                   "replans_used": replans_used, "max_replans": int(max_replans)},
        "attempts": [dict(a) for a in attempt_rows],
        "verifications": [dict(v) for v in verification_rows],
        "failure_fingerprints": list(fingerprints),
        "prior_approach_digests": list(prior_approaches),
        "last_failure_excerpt": excerpt,
        "evidence_index": [e.entry_hash for e in entries],
    }

    return PlannerInput(
        run_id=run_id, packet_id=core["packet_id"], objective=core["objective"],
        boundary=boundary, allowed_kinds=tuple(allowed),
        attempts=attempt_rows, verifications=verification_rows,
        failure_fingerprints=fingerprints, prior_approach_digests=prior_approaches,
        evidence_index=tuple(core["evidence_index"]),
        target_id=core["identity"]["target_id"], host=core["identity"]["host"],
        model=core["identity"]["model"],
        served_context=core["identity"]["served_context"],
        worktree=core["identity"]["worktree"], base_sha=core["identity"]["base_sha"],
        write_scope=tuple(core["scope"]["write_scope"]),
        read_scope=tuple(core["scope"]["read_scope"]),
        interface=interface_lines, interface_digest=interface_digest,
        test_command=core["verification"]["test_command"],
        allowed_tools=tuple(policy.allowed_tools),
        network_policy=policy.network_policy,
        capabilities=tuple(policy.capabilities),
        verifier_id=policy.verifier_id, verifier_digest=policy.verifier_digest,
        objective_ok=validated is not None,
        attempts_used=used, max_attempts=int(max_attempts),
        budget_remaining=budget_remaining, replans_used=replans_used,
        max_replans=int(max_replans), last_failure_excerpt=excerpt,
        input_digest=compute_input_digest(core))



# --------------------------------------------------------- planner context ---
_PROTOCOL = (
    'REPLY PROTOCOL — reply with ONE JSON object and nothing else:\n'
    '{"kind": "<one of the allowed kinds>", "rationale": "<why>", '
    '"approach": "<short steering note for the next attempt>", '
    '"evidence_refs": ["<ledger entry hash from EVIDENCE INDEX>"], '
    '"requested_actions": [], "requested_authority": [], "packet_delta": {}}\n'
    'You are ADVISING, not deciding. You cannot accept, approve, ship, land, '
    'route, change scope, change the interface, change the verification or '
    'change the budget: those are not yours, and asking for them is refused and '
    'recorded. The only thing you may change is "approach".'
)


def render_planner_context(plan_input: "PlannerInput", *,
                           max_chars: int = DEFAULT_MAX_CONTEXT_CHARS) -> str:
    """Render the projection as a bounded, deterministic manager context."""
    def bullets(items, indent="  - "):
        return "\n".join(indent + str(i) for i in items) if items else indent + "NONE"

    lines: List[str] = []
    lines.append("MANAGER/REPLANNER REQUEST — G2 advisory proposal.")
    lines.append("")
    lines.append(f"RUN: {plan_input.run_id}")
    lines.append(f"PACKET: {plan_input.packet_id}")
    lines.append(f"OBJECTIVE: {plan_input.objective or 'NONE'}")
    lines.append(f"BOUNDARY: {plan_input.boundary}")
    lines.append("ALLOWED_KINDS: " + ", ".join(plan_input.allowed_kinds))
    lines.append(
        f"BUDGET: attempts {plan_input.attempts_used} of {plan_input.max_attempts}; "
        f"remaining {plan_input.budget_remaining}; replans "
        f"{plan_input.replans_used} of {plan_input.max_replans}")
    lines.append("")
    lines.append("EXECUTION IDENTITY (the run is pinned to this; you cannot change it):")
    lines.append(f"  target_id: {plan_input.target_id}")
    lines.append(f"  host: {plan_input.host}   model: {plan_input.model}")
    lines.append(f"  served_context: {plan_input.served_context}")
    lines.append(f"  worktree: {plan_input.worktree or 'NONE'}")
    lines.append(f"  base_sha: {plan_input.base_sha or 'NONE'}")
    lines.append("")
    lines.append("WRITE_SCOPE: " + (", ".join(plan_input.write_scope) or "NONE"))
    lines.append("READ_SCOPE: " + (", ".join(plan_input.read_scope) or "NONE"))
    lines.append(f"INTERFACE_DIGEST: {plan_input.interface_digest or 'NONE'}")
    lines.append("INTERFACE (the sealed input keys; you cannot change them):")
    lines.append(bullets(plan_input.interface))
    lines.append("")
    lines.append("VERIFICATION (the deterministic gate; you cannot change it):")
    lines.append(f"  test_command: {plan_input.test_command or 'NONE'}")
    lines.append(f"  verifier_id: {plan_input.verifier_id or 'NONE'}")
    lines.append(f"  verifier_digest: {plan_input.verifier_digest or 'NONE'}")
    lines.append("ENVELOPE (granted; you cannot widen it):")
    lines.append(f"  allowed_tools: {', '.join(plan_input.allowed_tools) or 'NONE'}")
    lines.append(f"  network_policy: {plan_input.network_policy or 'NONE'}")
    lines.append(f"  capabilities: {', '.join(plan_input.capabilities) or 'NONE'}")
    lines.append("")
    lines.append("CANONICAL HISTORY (from the append-only ledger — the ONLY state "
                 "you may reason from; no transcript is available to you):")
    if plan_input.attempts:
        for attempt in plan_input.attempts:
            verdict = next((v for v in plan_input.verifications
                            if v.get("attempt") == attempt.get("attempt")), {})
            outcome = ("PASS" if verdict.get("passed")
                       else f"FAIL (exit {verdict.get('returncode')})")
            lines.append(
                f"  attempt {attempt.get('attempt')}: {outcome}"
                f"  rounds={attempt.get('rounds')}"
                f"  artifacts={attempt.get('artifacts')}"
                f"  failure_class={attempt.get('failure_class') or '-'}")
    else:
        lines.append("  no attempt recorded")
    lines.append("FAILURE_FINGERPRINTS: "
                 + (", ".join(plan_input.failure_fingerprints) or "NONE"))
    lines.append("PRIOR_APPROACH_DIGESTS: "
                 + (", ".join(plan_input.prior_approach_digests) or "NONE"))
    if plan_input.last_failure_excerpt:
        lines.append("")
        lines.append("LAST FAILURE EXCERPT (deterministic verifier output, bounded):")
        lines.append(plan_input.last_failure_excerpt)
    lines.append("")
    lines.append("EVIDENCE INDEX (cite at least one of these exact hashes in "
                 "evidence_refs):")
    lines.append(bullets(plan_input.evidence_index))
    lines.append("")
    lines.append(_PROTOCOL)

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    marker = "\n...[PLANNER CONTEXT TRUNCATED]"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return text[:max_chars - len(marker)] + marker


# ------------------------------------------------------------------ parsing ---
def extract_json_object(text: str) -> Optional[dict]:
    """First balanced ``{...}`` object in ``text``, or None.

    Bounded and string-aware: a brace inside a JSON string does not close the
    object, and the scan stops at the end of the text. A local model that wraps
    its JSON in prose (or a fenced block) still parses; one that emits nothing
    usable yields None, and None is a recorded outcome rather than a guess.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except ValueError:
                        break
                    return parsed if isinstance(parsed, dict) else None
        start = text.find("{", start + 1)
    return None


def parse_proposal_text(text: str, *, run_id: str,
                        packet_id: str) -> Tuple[Optional[ManagerProposal], str]:
    """Best-effort parse of a model's reply into a sealed proposal.

    Returns ``(proposal, "")`` or ``(None, reason)``. The reasons are worded as
    findings, not excuses: "the reply contained no JSON object", "the JSON
    object is not a proposal shape: ...". A malformed reply is exactly what the
    negative controls exercise, so it must be a first-class outcome.
    """
    payload = extract_json_object(text or "")
    if payload is None:
        return None, "the reply contained no JSON object"
    allowed = {"kind", "rationale", "approach", "evidence_refs",
               "requested_actions", "requested_authority", "packet_delta",
               "proposal_id", "run_id", "packet_id", "schema_version"}
    unknown = set(payload) - allowed
    if unknown:
        return None, f"the JSON object carries unknown field(s): {sorted(unknown)}"
    try:
        proposal = make_manager_proposal(
            proposal_id=str(payload.get("proposal_id")
                            or f"p-{run_id}-{compute_proposal_hash(payload)[:8]}"),
            run_id=str(payload.get("run_id") or run_id),
            packet_id=str(payload.get("packet_id") or packet_id),
            kind=str(payload.get("kind") or ""),
            rationale=str(payload.get("rationale") or ""),
            approach=str(payload.get("approach") or ""),
            evidence_refs=payload.get("evidence_refs") or (),
            requested_actions=payload.get("requested_actions") or (),
            requested_authority=payload.get("requested_authority") or (),
            packet_delta=payload.get("packet_delta") or {},
            **({"schema_version": payload["schema_version"]}
               if isinstance(payload.get("schema_version"), int) else {}))
    except ReplannerError as exc:
        return None, f"the JSON object is not a proposal shape: {exc}"
    return proposal, ""


# ------------------------------------------------------------------- gate ---
@dataclass(frozen=True)
class ProposalVerdict:
    """The deterministic answer to "may this proposal be acted on?"."""

    proposal_id: str
    proposal_hash: str
    kind: str
    ok: bool
    code: str = ""
    detail: str = ""
    approach: str = ""
    approach_digest: str = ""
    checks: Tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id, "proposal_hash": self.proposal_hash,
            "kind": self.kind, "ok": self.ok, "code": self.code,
            "detail": self.detail, "approach_digest": self.approach_digest,
            "checks": [dict(c) for c in self.checks],
        }


def _check(name: str, ok: bool, code: str = "", detail: str = "") -> dict:
    return {"check": name, "ok": bool(ok), "code": code if not ok else "",
            "detail": detail}


def effective_packet(packet: Mapping[str, Any],
                     proposal: ManagerProposal) -> dict:
    """The packet as it would be dispatched with the proposal's approach applied.

    The ONLY key this can change is ``approach``. Everything else is copied from
    the canonical packet, so "the manager changed the scope" is not a bug this
    function could have — it is a sentence the function cannot express.
    """
    effective = dict(packet)
    approach = str(proposal.packet_delta.get(DELTA_APPROACH) or proposal.approach or "")
    if approach:
        effective[DELTA_APPROACH] = approach
    return effective


def _settled_result(ledger: Any, run_id: str) -> str:
    """The run's explicit terminal DECISION, or '' when the run is still open.

    Deliberately NOT ``ledger.terminal_result``: a failed VERIFICATION records
    ``REJECTED`` on its own payload, which is a statement about one attempt, not
    about the run. Reading that as the run's terminal state would make every
    failing attempt look like a settled run and refuse the replan that exists for
    exactly that situation. What settles a run is a DECISION or an ACCEPTANCE.
    """
    if ledger is None:
        return ""
    try:
        entries = list(ledger.entries_for(run_id))
    except Exception:
        return ""
    for entry in reversed(entries):
        if entry.kind in ("decision", "acceptance"):
            result = str(entry.payload.get("result") or "")
            if result and result != "IN_PROGRESS":
                return f"{entry.kind}:{result}"
    return ""


def validate_proposal(proposal: ManagerProposal, *, plan_input: PlannerInput,
                      packet: Mapping[str, Any],
                      ledger: Any = None) -> ProposalVerdict:
    """Decide whether a proposal may enter canonical state or dispatch.

    Every check runs (none short-circuits) so the record shows what was examined
    as well as what failed, and the verdict takes the FIRST failure in a fixed
    order — deterministic, so two readings of the same inputs cannot disagree.

    The packet re-validation is not decoration: it is the WorkPacket primitive
    deciding dispatchability on the packet this proposal would ACTUALLY cause,
    with the interface/scope/source cross-checks comparing that packet against
    the projection the manager was shown. A proposal cannot be made about one
    packet and executed against another.
    """
    approach = str(proposal.packet_delta.get(DELTA_APPROACH)
                   or proposal.approach or "").strip()
    digest = approach_digest(approach)
    checks: list = []

    # ---- 1. the proposal is the proposal that was sealed --------------------
    checks.append(_check(
        "proposal_hash", proposal_hash_is_valid(proposal.to_dict()),
        CODE_PROPOSAL_HASH_MISMATCH,
        "the proposal's hash does not match its own fields"))

    # ---- 2. it is about THIS run and THIS packet ---------------------------
    same = (proposal.run_id == plan_input.run_id
            and proposal.packet_id == plan_input.packet_id)
    checks.append(_check(
        "run_identity", same, CODE_RUN_MISMATCH,
        f"proposal is for {proposal.run_id}/{proposal.packet_id}; canonical state "
        f"is {plan_input.run_id}/{plan_input.packet_id}"))

    # ---- 3. the kind is legal for this deterministic boundary --------------
    legal = proposal.kind in plan_input.allowed_kinds
    checks.append(_check(
        "kind_allowed", legal, CODE_KIND_NOT_ALLOWED,
        f"kind {proposal.kind!r} is not legal at the {plan_input.boundary} "
        f"boundary; allowed here: {list(plan_input.allowed_kinds)}"))

    # ---- 4. no authority is requested --------------------------------------
    checks.append(_check(
        "requested_authority", not proposal.requested_authority,
        CODE_AUTHORITY_REQUESTED,
        "the proposal asked for authority it cannot hold: "
        f"{list(proposal.requested_authority)}"))

    # ---- 5. no action is requested (and unknown actions fail closed) -------
    if not proposal.requested_actions:
        checks.append(_check("requested_actions", True,
                             detail="no action requested"))
    else:
        code = CODE_UNKNOWN_ACTION
        families = []
        for action in proposal.requested_actions:
            family = _ACTION_FAMILIES.get(_normalize_action(action))
            if family and code == CODE_UNKNOWN_ACTION:
                code = family
            families.append(f"{action}->{family or 'unknown'}")
        checks.append(_check(
            "requested_actions", False, code,
            f"a manager may not request an action; asked: {families}"))

    # ---- 6. the delta touches nothing but the approach ---------------------
    offenders = []
    for key in proposal.packet_delta:
        normalized = _normalize_action(key)
        if normalized == DELTA_APPROACH:
            continue
        offenders.append((key, _DELTA_FAMILIES.get(normalized)))
    if not offenders:
        checks.append(_check("packet_delta", True,
                             detail="delta touches only the approach"))
    else:
        unknown = [k for k, family in offenders if family is None]
        code = (CODE_SCHEMA_INVALID if unknown
                else next(family for _k, family in offenders if family))
        checks.append(_check(
            "packet_delta", False, code,
            "the delta tries to change something that is not delegable: "
            + ", ".join(f"{k}->{family or 'unknown key'}"
                        for k, family in offenders)))

    # ---- 7. the proposal is grounded in canonical evidence -----------------
    known = set(plan_input.evidence_index)
    cited = list(dict.fromkeys(proposal.evidence_refs))
    unbound = sorted(set(cited) - known)
    if cited and not unbound:
        checks.append(_check("evidence_binding", True,
                             detail=f"{len(cited)} canonical entry hash(es) cited"))
    else:
        checks.append(_check(
            "evidence_binding", False, CODE_EVIDENCE_UNBOUND,
            ("the proposal cites no canonical evidence" if not cited else
             "the proposal cites evidence that is not in this run's ledger: "
             f"{unbound}")))

    # ---- 8. the effective packet is dispatchable ---------------------------
    effective = effective_packet(packet, proposal)
    packet_error = ""
    validated = None
    try:
        from src.local_worker_loop import validate_dispatchable_packet

        validated = validate_dispatchable_packet(effective)
    except Exception as exc:  # the primitive's ValueError, or an import fault
        packet_error = str(exc)
    checks.append(_check(
        "packet_valid", not packet_error, CODE_PACKET_INVALID,
        "the packet this proposal would dispatch is not dispatchable: "
        f"{packet_error}"))

    # ---- 9-13. the packet agrees with the projection ----------------------
    if validated is None:
        checks.append(_check(
            "canonical_agreement", False, CODE_PACKET_INVALID,
            "the packet could not be validated, so it cannot be compared"))
    else:
        drift = []
        if validated.interface_digest != plan_input.interface_digest:
            drift.append(("interface", validated.interface_digest,
                          plan_input.interface_digest))
        if str(packet.get("base_sha") or "") != str(plan_input.base_sha or ""):
            drift.append(("base_sha", packet.get("base_sha"), plan_input.base_sha))
        if list(validated.write_scope) != list(plan_input.write_scope):
            drift.append(("write_scope", list(validated.write_scope),
                          list(plan_input.write_scope)))
        if list(validated.read_scope) != list(plan_input.read_scope):
            drift.append(("read_scope", list(validated.read_scope),
                          list(plan_input.read_scope)))
        if validated.test_command != plan_input.test_command:
            drift.append(("test_command", validated.test_command,
                          plan_input.test_command))
        if validated.objective != plan_input.objective:
            drift.append(("objective", validated.objective, plan_input.objective))
        if not drift:
            checks.append(_check(
                "canonical_agreement", True,
                detail="packet matches the projection on interface, source, "
                       "scope, verification and objective"))
        else:
            code = {
                "interface": CODE_INTERFACE_DRIFT, "base_sha": CODE_SOURCE_DRIFT,
                "write_scope": CODE_SCOPE_WIDENING,
                "read_scope": CODE_SCOPE_WIDENING,
                "test_command": CODE_VERIFICATION_WEAKENED,
                "objective": CODE_IDENTITY_DRIFT,
            }[drift[0][0]]
            checks.append(_check(
                "canonical_agreement", False, code,
                "the packet does not agree with the canonical state the manager "
                f"was shown: {drift}"))

    # ---- 14. the run has not already terminated ----------------------------
    terminal = _settled_result(ledger, plan_input.run_id)
    if proposal.continuing and terminal:
        checks.append(_check(
            "not_already_terminal", False, CODE_CYCLIC_PACKET,
            f"the run already recorded {terminal}; a continuing proposal would "
            "re-open a settled run"))
    else:
        checks.append(_check(
            "not_already_terminal", True,
            detail=f"run settlement: {terminal or 'none recorded'}"))

    # ---- 15. budget and the replan allowance are consumed, never widened ---
    if not proposal.continuing:
        checks.append(_check("budget", True,
                             detail="terminal kind: no budget requested"))
    elif int(plan_input.budget_remaining) <= 0:
        checks.append(_check("budget", False, CODE_BUDGET_EXHAUSTED,
                             "no attempts remain; the budget is not the manager's"))
    elif plan_input.replans_remaining <= 0:
        checks.append(_check("budget", False, CODE_REPLANS_EXHAUSTED,
                             "the bounded replan allowance for this run is spent"))
    else:
        checks.append(_check(
            "budget", True,
            detail=f"{plan_input.budget_remaining} attempt(s) and "
                   f"{plan_input.replans_remaining} replan(s) remain"))

    # ---- 16. a genuinely new approach, or nothing --------------------------
    if proposal.kind in APPROACH_REQUIRED_KINDS and not approach:
        checks.append(_check(
            "approach", False, CODE_SHAPE_INVALID,
            f"kind {proposal.kind!r} must carry approach text: a switch with no "
            "new approach is the same attempt again"))
    elif (proposal.kind in APPROACH_REQUIRED_KINDS
          and digest in tuple(plan_input.prior_approach_digests)):
        checks.append(_check(
            "approach", False, CODE_APPROACH_REPEATED,
            f"approach digest {digest} has already been tried in this run"))
    elif proposal.kind in (KIND_STOP, KIND_ESCALATE, KIND_NEXT_PACKET) and approach:
        checks.append(_check(
            "approach", False, CODE_SHAPE_INVALID,
            f"kind {proposal.kind!r} must not carry approach text"))
    else:
        checks.append(_check("approach", True,
                             detail=f"approach digest {digest or 'NONE'}"))

    failures = [c for c in checks if not c["ok"]]
    return ProposalVerdict(
        proposal_id=proposal.proposal_id, proposal_hash=proposal.proposal_hash,
        kind=proposal.kind, ok=not failures,
        code=(failures[0]["code"] if failures else ""),
        detail="; ".join(f"{c['check']}: {c['detail']}" for c in failures)[:600],
        approach=approach, approach_digest=digest, checks=tuple(checks))
