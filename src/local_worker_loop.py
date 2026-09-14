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
    FAILURE_RUNTIME_PROVIDER,
    KIND_FAILURE,
    RESULT_ACCEPTED_CANDIDATE,
    RESULT_BLOCKED,
    RESULT_ESCALATE,
    RESULT_REJECTED,
)
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


def run_bounded(packet: Mapping, *, run_id: str, ledger: ExecutionLedger,
                pinned: PinnedTarget,
                dispatch: Callable[[str, int], DispatchOutcome],
                verify: Callable[[], dict],
                base_context: str,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                generation_reserve: Optional[int] = None,
                read_changed_files: Optional[Callable[[Mapping], Mapping]] = None,
                max_context_chars: Optional[int] = None) -> LoopResult:
    """Run one packet through the bounded loop. Never raises for task outcomes.

    ``dispatch(context, attempt) -> DispatchOutcome`` and ``verify() -> dict``
    are injected; ``verify()`` must return at least
    ``{test_command, returncode, passed}`` and should include the runner output,
    which this loop turns into bounded repair evidence.
    """
    ledger.record_run(
        run_id=run_id, packet_id=str(packet.get("packet_id", "")),
        objective=str(packet.get("objective", "")),
        role=str(packet.get("role", "")), target_id=pinned.target_id,
        host=pinned.host, model=pinned.model,
        runtime_version=pinned.runtime_version, worktree=pinned.worktree,
        write_scope=list(packet.get("write_scope") or ()),
        base_sha=str(packet.get("base_sha", "")))

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

        # No-progress rule: the SAME failure twice means the repair loop is not
        # converging, and another identical round just burns a slow node's turn.
        if fingerprint == prior_fingerprint:
            reason = (f"attempt {attempt} reproduced the same failure "
                      f"({fingerprint}); no progress -> escalate")
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
        rendered = render_repair_context(
            repair, max_chars=(max_context_chars or DEFAULT_MAX_REPAIR_CHARS))
        context = rendered

    # Unreachable: the loop returns inside the final iteration above.
    return LoopResult(run_id=run_id, result=RESULT_REJECTED, failure_class="technical",
                      decision=DECISION_ESCALATE, reason="loop fell through",
                      attempts=max_attempts)
