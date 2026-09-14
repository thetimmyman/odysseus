"""src/local_worker_loop.py — bounded dispatch to verify to repair loop (PS-635).

This is the minimal GVS5H-shaped loop:

    packet -> fresh bounded context -> local worker -> deterministic verification
        -> (FAIL) compact repair packet -> fresh context -> retry (bounded)
        -> (PASS) ACCEPTED_CANDIDATE + evidence
        -> (exhausted / no progress / infra) ESCALATE or BLOCKED

What this module deliberately does NOT do:

* it does not select a target (PS-605 owns routing); the caller pins one and
  passes its identity, which is then recorded on the run;
* it does not enforce privacy or budget policy;
* it cannot produce ``ACCEPTED`` — the ledger refuses that from any write path
  except a named non-local authority, so a worker-driven run tops out at
  ``ACCEPTED_CANDIDATE`` no matter what this loop decides;
* it never replays a worker conversation; every attempt gets a FRESH context
  built from the packet or the repair packet.

Everything that touches a model or a test runner is injected, so the loop's own
logic is testable without a live node.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.execution_ledger import (
    ExecutionLedger,
    FAILURE_CONTEXT,
    FAILURE_PACKET_INVALID,
    FAILURE_RUNTIME_PROVIDER,
    KIND_FAILURE,
    RESULT_ACCEPTED_CANDIDATE,
    RESULT_BLOCKED,
    RESULT_ESCALATE,
    RESULT_IN_PROGRESS,
    RESULT_REJECTED,
)
from src.replanner import BOUNDARY_PASS, BOUNDARY_STALL, CONTINUING_KINDS
from src.repair_packet import (
    VerificationFailure,
    build_repair_packet,
    failure_fingerprint,
    parse_verification_failure,
    render_repair_context,
)

DECISION_NEXT = "next"
DECISION_STOP = "stop"
DECISION_ESCALATE = "escalate"
DECISION_BLOCKED = "blocked"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_REPAIR_CHARS = 6000


def estimate_context_tokens(text: str) -> int:
    """Token estimate for a rendered context string.

    Reuses the repo's own estimator so the loop and the agent path cannot drift
    apart on what "too big" means. Falls back to a CONSERVATIVE over-estimate if
    the import is unavailable, because under-estimating here is what lets a
    prompt be dispatched into a window that cannot hold it.
    """
    try:
        from src.model_context import estimate_tokens

        return int(estimate_tokens([{"role": "user", "content": text}]))
    except Exception:
        return int(len(text or "") * 0.35) + 8


def default_generation_reserve() -> int:
    """Output budget to withhold from the window. Reuses context_safety's value.

    The loop must not invent a second notion of "how much room is left for the
    answer"; ``src/context_safety.py`` already defines it, and it already
    refuses to call a model when ``input + reserve`` exceeds the window.
    """
    try:
        from src.context_safety import DEFAULT_GEN_RESERVE_ABSOLUTE

        return int(DEFAULT_GEN_RESERVE_ABSOLUTE)
    except Exception:
        return 4096


@dataclass(frozen=True)
class PinnedTarget:
    """The execution identity this run is pinned to. Recorded, never inferred."""

    target_id: str
    host: str
    model: str
    runtime_version: str = ""
    worktree: str = ""
    served_context: Optional[int] = None


@dataclass
class DispatchOutcome:
    """What one dispatch produced. Produced by the caller's worker adapter."""

    artifacts: Tuple[str, ...] = ()
    failure_class: str = ""
    status: str = ""
    elapsed_s: float = 0.0
    rounds: int = 0
    timings: Dict[str, object] = field(default_factory=dict)

    @property
    def produced_artifacts(self) -> bool:
        return bool(self.artifacts)


@dataclass
class LoopResult:
    run_id: str
    result: str
    failure_class: str
    decision: str
    reason: str
    attempts: int
    artifact: tuple = ()
    verification: dict = field(default_factory=dict)
    budget_refused: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "result": self.result,
            "failure_class": self.failure_class, "decision": self.decision,
            "reason": self.reason, "attempts": self.attempts,
            "artifact": list(self.artifact),
            "verification": self.verification,
            "budget_refused": self.budget_refused,
        }


class ContextBudgetExceeded(Exception):
    """The packet's fresh context cannot fit the PINNED target's served window.

    Raised instead of dispatching, because the measured failure mode of an
    over-budget prompt is not always an error: on the non-streaming `/api/generate`
    path the runtime truncates the front of the prompt SILENTLY and answers
    anyway (see `docs/` findings F2). A silent truncation can remove the objective
    itself, so the loop refuses before spending the turn.
    """

    def __init__(self, estimated: int, usable: int, served_context: Optional[int]):
        self.estimated = estimated
        self.usable = usable
        self.served_context = served_context
        super().__init__(
            f"fresh context needs ~{estimated} tokens but only {usable} are usable "
            f"(served_context={served_context})")


def usable_input_tokens(served_context: Optional[int], *,
                        generation_reserve: Optional[int] = None) -> Optional[int]:
    """How many tokens of packet context a target can actually accept.

    Mirrors the invariant in ``src/context_safety.py``:

        safe_input + generation_reserve <= effective_context_window

    Returns None when the window is unknown — and unknown must be treated as a
    REFUSAL by the caller, not as "no limit". A target whose serving window has
    not been measured is exactly the case that produced the silent truncation.
    """
    if served_context is None:
        return None
    reserve = (default_generation_reserve() if generation_reserve is None
               else int(generation_reserve))
    return max(0, int(served_context) - reserve)


def validate_dispatchable_packet(packet: Mapping) -> "object":
    """Validate a packet mapping against the WorkPacket primitive's own rules.

    Reuses ``src.work_packet.make_work_packet`` rather than restating its rules,
    so there is ONE definition of a dispatchable packet. The mapping may carry
    extra keys the primitive does not know (``role``, ``contract``, ``base_sha``);
    those are filtered out rather than rejected, because they are manager-side
    annotations that the worker context renders, not packet identity.

    Raises ``WorkPacketError`` when the packet is not dispatchable — which now
    includes the interface rule earned by measurement: a writable packet that does
    not declare the input keys its contract promises cannot be repaired into
    correctness, so it must not be dispatched at all.
    """
    from dataclasses import fields

    from src.work_packet import WorkPacket, make_work_packet

    known = {f.name for f in fields(WorkPacket)}
    subset = {k: v for k, v in dict(packet).items() if k in known}
    return make_work_packet(**subset)


#: The approach note is appended to the fresh repair context under this label.
#: It is a SIBLING of the deterministic repair evidence, never a replacement for
#: it: the manager adds framing, and the failure evidence, contract, interface and
#: acceptance criteria stay exactly as the repair packet rendered them.
APPROACH_LABEL = "\n\nMANAGER_APPROACH (advisory steering only; the contract, "
APPROACH_LABEL += "interface, scope, criteria and verification below are UNCHANGED):\n"


def consult_planner(*, advise, packet: Mapping, validated, ledger: ExecutionLedger,
                    run_id: str, pinned: "PinnedTarget", boundary: str,
                    attempts_used: int, max_attempts: int, replans_used: int,
                    max_replans: int, policy=None) -> dict:
    """Consult the advisory planner at ONE deterministic decision boundary.

    ``advise(planner_input) -> (proposal | None, reason)`` is injected, so the
    loop's own logic stays testable without a model. Nothing here decides what
    the run DOES: it records advice, validates it and reports the verdict. The
    caller decides, and the caller's options are what the verdict allows.

    Never raises. A planner that crashes, or a reply that is not a proposal, is
    recorded as a refusal with a typed code — a fault in the advisor must not be
    able to fail a run, and must not be able to pass for agreement either.
    """
    result = {"consulted": False, "boundary": boundary, "plan_input": None,
              "context": "", "proposal": None, "verdict": None, "ok": False,
              "kind": "", "approach": "", "approach_digest": "", "refusal": "",
              "input_digest": ""}
    if advise is None:
        return result

    from src.replanner import (CODE_PLANNER_FAULT, CODE_SCHEMA_INVALID,
                               build_planner_input, make_manager_proposal,
                               render_planner_context, validate_proposal,
                               ManagerProposal, PlannerPolicy)

    packet_id = str(packet.get("packet_id", ""))
    result["consulted"] = True
    try:
        plan_input = build_planner_input(
            ledger=ledger, run_id=run_id, packet=packet, boundary=boundary,
            max_attempts=max_attempts, max_replans=max_replans,
            replans_used=replans_used, policy=policy or PlannerPolicy(),
            validated=validated)
    except Exception as exc:
        result["refusal"] = f"planner fault: {exc}"
        result["error_code"] = CODE_PLANNER_FAULT
        ledger.record_proposal_refusal(
            run_id=run_id, packet_id=packet_id, proposal={},
            code=CODE_PLANNER_FAULT,
            detail=f"the planner input could not be derived: {exc}")
        return result

    result["plan_input"] = plan_input
    result["input_digest"] = plan_input.input_digest
    result["context"] = render_planner_context(plan_input)

    try:
        offered, reason = advise(plan_input)
    except Exception as exc:
        offered, reason = None, f"the advisor raised {type(exc).__name__}: {exc}"

    if offered is None:
        detail = reason or "the advisor returned no proposal"
        result["refusal"] = detail
        result["error_code"] = CODE_SCHEMA_INVALID
        ledger.record_proposal_refusal(
            run_id=run_id, packet_id=packet_id, proposal={},
            code=CODE_SCHEMA_INVALID, detail=detail)
        return result

    proposal = offered
    if not isinstance(proposal, ManagerProposal):
        try:
            proposal = make_manager_proposal(**dict(proposal))
        except Exception as exc:
            result["refusal"] = f"not a proposal shape: {exc}"
            result["error_code"] = CODE_SCHEMA_INVALID
            ledger.record_proposal_refusal(
                run_id=run_id, packet_id=packet_id,
                proposal=dict(offered) if isinstance(offered, Mapping) else {},
                code=CODE_SCHEMA_INVALID, detail=f"not a proposal shape: {exc}")
            return result

    verdict = validate_proposal(proposal, plan_input=plan_input, packet=packet,
                               ledger=ledger)
    result["proposal"] = proposal.to_dict()
    result["verdict"] = verdict.to_dict()
    result["ok"] = verdict.ok
    result["kind"] = proposal.kind
    result["approach"] = verdict.approach
    result["approach_digest"] = verdict.approach_digest
    if verdict.ok:
        ledger.record_proposal(run_id=run_id, packet_id=packet_id,
                               proposal=proposal.to_dict(),
                               verdict=verdict.to_dict())
    else:
        result["refusal"] = f"{verdict.code}: {verdict.detail}"
        result["error_code"] = verdict.code
        ledger.record_proposal_refusal(
            run_id=run_id, packet_id=packet_id, proposal=proposal.to_dict(),
            code=verdict.code, detail=verdict.detail)
    return result


def run_bounded(packet: Mapping, *, run_id: str, ledger: ExecutionLedger,
                pinned: PinnedTarget,
                dispatch: Callable[[str, int], DispatchOutcome],
                verify: Callable[[], dict],
                base_context: str,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                generation_reserve: Optional[int] = None,
                read_changed_files: Optional[Callable[[Mapping], Mapping]] = None,
                max_context_chars: Optional[int] = None,
                advise: Optional[Callable[[object], tuple]] = None,
                max_replans: int = 0,
                planner_policy=None) -> LoopResult:
    """Run one packet through the bounded loop. Never raises for task outcomes.

    ``dispatch(context, attempt) -> DispatchOutcome`` and ``verify() -> dict``
    are injected; ``verify()`` must return at least
    ``{test_command, returncode, passed}`` and should include the runner output,
    which this loop turns into bounded repair evidence.

    ``advise(planner_input) -> (proposal | None, reason)`` is the G2 seam, and it
    is OPTIONAL and advisory in the strongest sense the design allows:

    * it is consulted only at the deterministic decision boundary where the loop
      was about to stop, and once at a PASS — never mid-attempt;
    * its proposal is validated by ``src.replanner`` before it can affect
      anything, and a refusal is recorded with a typed code;
    * the only thing a validated proposal can change is the APPROACH text of the
      next fresh context. It cannot change scope, interface, source,
      verification, permissions, routing, budget or the run's result;
    * one run accepts at most ``max_replans`` replans, and a replan consumes the
      SAME attempt budget the deterministic loop uses, so advice cannot buy
      attempts;
    * with ``advise=None`` (the default) the loop behaves exactly as G1 did.
    """
    # Validate ONCE, up front, and keep the validated packet: its interface digest is
    # evidence, and computing it here means the digest recorded on the run is the
    # digest of the packet that was actually validated — not a re-derivation.
    validated = None
    packet_error = ""
    try:
        validated = validate_dispatchable_packet(packet)
    except ValueError as exc:
        packet_error = str(exc)

    ledger.record_run(
        run_id=run_id, packet_id=str(packet.get("packet_id", "")),
        objective=str(packet.get("objective", "")),
        role=str(packet.get("role", "")), target_id=pinned.target_id,
        host=pinned.host, model=pinned.model,
        runtime_version=pinned.runtime_version, worktree=pinned.worktree,
        write_scope=list(packet.get("write_scope") or ()),
        base_sha=str(packet.get("base_sha", "")),
        interface_digest=(validated.interface_digest if validated else ""),
        interface=(validated.normalized_interface() if validated else ()))

    # ---- packet gate: refuse a packet that is not dispatchable AT ALL --------
    # Before the budget gate, because "this is not a packet" is a different answer
    # from "this packet does not fit". Measured 2026-09-14: a writable packet whose
    # contract never named its input keys was dispatched, failed, was repaired, and
    # the repair failed the SAME way — the loop correctly escalated rather than
    # converge, but the turns were spent for nothing. An under-specified packet is
    # now refused at the boundary instead of being repaired into place.
    if packet_error:
        reason = f"packet is not dispatchable: {packet_error}"
        ledger.record_decision(run_id=run_id,
                               packet_id=str(packet.get("packet_id", "")),
                               decision=DECISION_BLOCKED, reason=reason,
                               result=RESULT_BLOCKED)
        ledger.append(KIND_FAILURE, run_id=run_id,
                      packet_id=str(packet.get("packet_id", "")),
                      payload={"failure_class": FAILURE_PACKET_INVALID,
                               "detail": packet_error[:300],
                               "result": RESULT_BLOCKED})
        return LoopResult(run_id=run_id, result=RESULT_BLOCKED,
                          failure_class=FAILURE_PACKET_INVALID,
                          decision=DECISION_BLOCKED, reason=reason, attempts=0)

    # ---- budget gate: refuse BEFORE dispatching an over-budget packet --------
    usable = usable_input_tokens(pinned.served_context, generation_reserve=generation_reserve)
    estimate = estimate_context_tokens(base_context)
    if usable is None or estimate > usable:
        reason = ("served_context unmeasured for this target: refusing to dispatch"
                  if usable is None else
                  f"fresh context ~{estimate} tokens exceeds usable {usable}")
        ledger.record_decision(run_id=run_id, packet_id=str(packet.get("packet_id", "")),
                               decision=DECISION_BLOCKED, reason=reason,
                               result=RESULT_BLOCKED)
        ledger.append(KIND_FAILURE, run_id=run_id,
                      packet_id=str(packet.get("packet_id", "")),
                      payload={"failure_class": FAILURE_CONTEXT, "detail": reason,
                               "result": RESULT_BLOCKED})
        return LoopResult(run_id=run_id, result=RESULT_BLOCKED,
                          failure_class=FAILURE_CONTEXT, decision=DECISION_BLOCKED,
                          reason=reason, attempts=0, budget_refused=True)

    read_changed = read_changed_files or (lambda _packet: {})
    context = base_context
    prior_fingerprint = ""
    artifact: Tuple[str, ...] = ()
    last_verification: dict = {}
    replans_used = 0

    for attempt in range(1, max_attempts + 1):
        outcome = dispatch(context, attempt)
        if len(outcome.artifacts):
            artifact = tuple(outcome.artifacts)

        # An infrastructure fault is not a task verdict: stop, do not "repair".
        if outcome.failure_class == FAILURE_RUNTIME_PROVIDER:
            reason = f"runtime/provider failure on attempt {attempt}; escalating as infrastructure"
            ledger.append(KIND_FAILURE, run_id=run_id,
                          packet_id=str(packet.get("packet_id", "")),
                          payload={"failure_class": FAILURE_RUNTIME_PROVIDER,
                                   "detail": outcome.status, "result": RESULT_BLOCKED})
            ledger.record_decision(run_id=run_id,
                                   packet_id=str(packet.get("packet_id", "")),
                                   decision=DECISION_BLOCKED, reason=reason,
                                   result=RESULT_BLOCKED)
            return LoopResult(run_id=run_id, result=RESULT_BLOCKED,
                              failure_class=FAILURE_RUNTIME_PROVIDER,
                              decision=DECISION_BLOCKED, reason=reason,
                              attempts=attempt, artifact=artifact)

        ledger.record_attempt(
            run_id=run_id, packet_id=str(packet.get("packet_id", "")),
            attempt=attempt, target_id=pinned.target_id, host=pinned.host,
            num_ctx=(pinned.served_context or 0),
            served_context=pinned.served_context, elapsed_s=outcome.elapsed_s,
            rounds=outcome.rounds, artifacts=outcome.artifacts,
            failure_class=outcome.failure_class, status=outcome.status,
            timings=outcome.timings)

        result = verify()
        last_verification = {
            "test_command": result.get("test_command", ""),
            "returncode": int(result.get("returncode", 1)),
            "passed": bool(result.get("passed")),
            "summary": list(result.get("summary") or ()),
        }
        ledger.record_verification(
            run_id=run_id, packet_id=str(packet.get("packet_id", "")),
            test_command=last_verification["test_command"],
            passed=last_verification["passed"],
            returncode=last_verification["returncode"],
            summary=last_verification["summary"],
            excerpt=str(result.get("output") or result.get("excerpt") or ""))

        if last_verification["passed"]:
            reason = f"attempt {attempt} passed deterministic verification"
            # G2, PASS boundary: ask the manager once, then ignore its opinion
            # about the outcome. Deterministic verification already decided this
            # run, and at a PASS the only legal kinds are terminal ones — so a
            # "replan" here is REFUSED with a typed code rather than honoured.
            # The consultation exists to be recorded, not to be obeyed.
            advice = consult_planner(
                advise=advise, packet=packet, validated=validated, ledger=ledger,
                run_id=run_id, pinned=pinned, boundary=BOUNDARY_PASS,
                attempts_used=attempt, max_attempts=max_attempts,
                replans_used=replans_used, max_replans=max_replans,
                policy=planner_policy)
            if advice["consulted"]:
                reason += (f"; manager advised {advice['kind'] or 'nothing'}"
                           + (" (validated)" if advice["ok"] else
                              f" (refused: {advice.get('error_code', '')})"))
            ledger.record_decision(run_id=run_id,
                                   packet_id=str(packet.get("packet_id", "")),
                                   decision=DECISION_STOP, reason=reason,
                                   result=RESULT_ACCEPTED_CANDIDATE)
            return LoopResult(run_id=run_id, result=RESULT_ACCEPTED_CANDIDATE,
                              failure_class="", decision=DECISION_STOP,
                              reason=reason, attempts=attempt, artifact=artifact,
                              verification=last_verification)

        # ---- deterministic FAIL: build a compact repair packet ---------------
        failure: VerificationFailure = parse_verification_failure(
            last_verification["test_command"], last_verification["returncode"],
            str(result.get("output") or result.get("excerpt") or ""))
        fingerprint = failure_fingerprint(failure)
        repair = build_repair_packet(packet, failure=failure,
                                     changed_files=read_changed(packet),
                                     attempt=attempt,
                                     budget_remaining=max_attempts - attempt)
        ledger.record_repair(run_id=run_id, packet_id=str(packet.get("packet_id", "")),
                             attempt=attempt,
                             failure_class=(outcome.failure_class or "technical"),
                             fingerprint=fingerprint, repair_packet=repair)
        # Rendered ONCE, so the replanned attempt and the ordinary repair attempt
        # are provably given the same deterministic evidence. The manager adds a
        # labelled approach note; it does not get to rewrite the repair packet.
        rendered_repair = render_repair_context(
            repair, max_chars=(max_context_chars or DEFAULT_MAX_REPAIR_CHARS))

        # No-progress rule: the SAME failure twice means the repair loop is not
        # converging, and another identical round just burns a slow node's turn.
        # In G2 this is the one boundary where the decision is genuinely open, so
        # it is the only place a validated proposal can change what happens next.
        if fingerprint == prior_fingerprint:
            advice = consult_planner(
                advise=advise, packet=packet, validated=validated, ledger=ledger,
                run_id=run_id, pinned=pinned, boundary=BOUNDARY_STALL,
                attempts_used=attempt, max_attempts=max_attempts,
                replans_used=replans_used, max_replans=max_replans,
                policy=planner_policy)
            if (advice["ok"] and advice["kind"] in CONTINUING_KINDS
                    and attempt < max_attempts):
                replans_used += 1
                reason = (
                    f"attempt {attempt} reproduced the same failure ({fingerprint}); "
                    f"manager proposed {advice['kind']} "
                    f"({str((advice['proposal'] or {}).get('proposal_hash', ''))[:16]}) "
                    "and the proposal passed deterministic validation -> bounded "
                    "replan with the same budget")
                ledger.record_decision(
                    run_id=run_id, packet_id=str(packet.get("packet_id", "")),
                    decision=DECISION_NEXT, reason=reason,
                    result=RESULT_IN_PROGRESS)
                # A NEW approach resets the repeat detector: the next attempt is
                # not the same attempt, and if IT fails identically the loop
                # re-enters this branch with the replan allowance now spent.
                # ``attempt < max_attempts`` is belt-and-braces on top of the
                # gate's budget rule: a proposal cannot buy an attempt beyond the
                # budget, so the loop can never be talked past its own bound.
                prior_fingerprint = ""
                context = rendered_repair
                if advice["approach"]:
                    context += APPROACH_LABEL + advice["approach"] + "\n"
                continue

            reason = (f"attempt {attempt} reproduced the same failure "
                      f"({fingerprint}); no progress -> escalate")
            if advice["consulted"] and not advice["ok"]:
                reason += (f" (manager advice refused: "
                           f"{advice.get('error_code', '')})")
            elif advice["consulted"]:
                reason += (f" (manager advised {advice['kind'] or 'nothing'}, "
                           "which is not a replan)")
            ledger.record_decision(run_id=run_id,
                                   packet_id=str(packet.get("packet_id", "")),
                                   decision=DECISION_ESCALATE, reason=reason,
                                   result=RESULT_ESCALATE)
            return LoopResult(run_id=run_id, result=RESULT_ESCALATE,
                              failure_class=(outcome.failure_class or "technical"),
                              decision=DECISION_ESCALATE, reason=reason,
                              attempts=attempt, artifact=artifact,
                              verification=last_verification)
        prior_fingerprint = fingerprint

        if attempt == max_attempts:
            reason = f"retry budget exhausted after {attempt} attempts"
            ledger.record_decision(run_id=run_id,
                                   packet_id=str(packet.get("packet_id", "")),
                                   decision=DECISION_ESCALATE, reason=reason,
                                   result=RESULT_ESCALATE)
            return LoopResult(run_id=run_id, result=RESULT_ESCALATE,
                              failure_class=(outcome.failure_class or "technical"),
                              decision=DECISION_ESCALATE, reason=reason,
                              attempts=attempt, artifact=artifact,
                              verification=last_verification)

        # Fresh context from the repair packet — never the previous conversation.
        context = rendered_repair

    # Unreachable: the loop returns inside the final iteration above.
    return LoopResult(run_id=run_id, result=RESULT_REJECTED, failure_class="technical",
                      decision=DECISION_ESCALATE, reason="loop fell through",
                      attempts=max_attempts)
