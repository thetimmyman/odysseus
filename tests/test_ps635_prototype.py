"""PS-635 minimal prototype — ledger, repair packet, bounded loop.

These are HERMETIC: no live node is contacted. The model and the test runner are
injected, so the loop's own decisions (budget refusal, repair, no-progress,
escalation) are tested directly rather than inferred from a slow remote run.

Every invariant that matters has a NEGATIVE control:
  * a worker authority must NOT be able to commit an acceptance;
  * a tampered ledger must NOT verify;
  * an unmeasured or over-budget window must NOT be dispatched into;
  * a runtime fault must NOT be turned into a repair attempt;
  * the same failure twice must NOT produce an identical retry.
"""
import hashlib
import json
import os
import tempfile

import pytest

from src.execution_ledger import (
    ExecutionLedger,
    LedgerError,
    KIND_ACCEPTANCE,
    RESULT_ACCEPTED,
    RESULT_ACCEPTED_CANDIDATE,
    RESULT_BLOCKED,
    RESULT_ESCALATE,
)
from src.repair_packet import (
    build_repair_packet,
    failure_fingerprint,
    parse_verification_failure,
    render_repair_context,
)
from src.local_worker_loop import (
    DispatchOutcome,
    PinnedTarget,
    run_bounded,
)


@pytest.fixture()
def ledger():
    return ExecutionLedger(os.path.join(tempfile.mkdtemp(), "ledger.jsonl"))


PACKET = {
    "packet_id": "P-T",
    "objective": "implement the thing",
    "role": "local_microtask",
    "write_scope": ["src/thing.py"],
    "interface": ["objective", "write_scope"],
    "test_command": "python3 -m pytest tests/test_thing.py -q",
    "acceptance_criteria": ["criterion one", "criterion two"],
    "negative_control": "must fail closed",
    "stop_conditions": ["ambiguous contract"],
}

PINNED = PinnedTarget(target_id="local-msr1", host="msr1", model="qwen3.8:27b",
                      runtime_version="0.33.3", worktree="/tmp/wt",
                      served_context=4096)

FAIL_OUTPUT = """
=========================== short test summary info ============================
FAILED tests/test_thing.py::test_renders_content - AssertionError: assert False
1 failed, 4 passed, 1 warning in 0.16s
"""


class _Worker:
    """A scripted dispatcher that records the context it was handed."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.contexts = []

    def __call__(self, context, attempt):
        self.contexts.append(context)
        return self.outcomes[min(attempt - 1, len(self.outcomes) - 1)]


def _verifier(results):
    state = {"n": 0}

    def _fn():
        r = results[min(state["n"], len(results) - 1)]
        state["n"] += 1
        return r

    return _fn


def _pass():
    return {"test_command": "pytest -q", "returncode": 0, "passed": True,
            "summary": ["5 passed"], "output": "5 passed"}


def _fail(reason="AssertionError: assert False"):
    return {"test_command": "pytest -q", "returncode": 1, "passed": False,
            "summary": ["1 failed, 4 passed"],
            "output": FAIL_OUTPUT.replace("AssertionError: assert False", reason)}


def _fail_named(test_name, reason="AssertionError: assert False"):
    return {"test_command": "pytest -q", "returncode": 1, "passed": False,
            "summary": ["1 failed"],
            "output": ("=========================== short test summary info ====================\n"
                       f"FAILED tests/test_thing.py::{test_name} - {reason}\n"
                       "1 failed, 4 passed, 1 warning in 0.16s\n")}


# ------------------------------------------------------------------ ledger ---

def test_ledger_is_append_only_and_hash_chained(ledger):
    ledger.record_run(run_id="r", packet_id="P", objective="o", role="x",
                      target_id="t", host="h", model="m", runtime_version="v",
                      worktree="w", write_scope=["a"])
    ledger.record_verification(run_id="r", packet_id="P", test_command="pytest",
                               passed=True, returncode=0)
    ok, broken = ledger.verify_chain()
    assert ok and broken is None


def test_ledger_detects_a_rewritten_entry(ledger):
    """NEGATIVE CONTROL: an append-only ledger that cannot detect a rewrite is a log."""
    ledger.record_run(run_id="r", packet_id="P", objective="o", role="x",
                      target_id="t", host="h", model="m", runtime_version="v",
                      worktree="w", write_scope=["a"])
    ledger.record_attempt(run_id="r", packet_id="P", attempt=1, target_id="t",
                          host="h", num_ctx=4096, served_context=4096,
                          elapsed_s=99.0, rounds=1, artifacts=["src/a.py"])
    lines = open(ledger.path).read().splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["elapsed_s"] = 0.001
    lines[1] = json.dumps(tampered, sort_keys=True)
    open(ledger.path, "w").write("\n".join(lines) + "\n")
    ok, broken = ExecutionLedger(ledger.path).verify_chain()
    assert ok is False and broken == 2


def test_a_local_worker_cannot_commit_an_acceptance(ledger):
    """NEGATIVE CONTROL, and the single most important invariant here.

    PS-579 measured that this model has no reviewer authority, so the ledger
    refuses the write rather than trusting callers to remember.
    """
    ledger.record_run(run_id="r", packet_id="P", objective="o", role="x",
                      target_id="t", host="h", model="m", runtime_version="v",
                      worktree="w", write_scope=["a"])
    for authority in ("local_worker", "local_qwen", "local_target"):
        with pytest.raises(LedgerError):
            ledger.record_strong_model_acceptance(run_id="r", packet_id="P",
                                                  authority=authority)


def test_acceptance_requires_a_named_stronger_authority(ledger):
    ledger.record_run(run_id="r", packet_id="P", objective="o", role="x",
                      target_id="t", host="h", model="m", runtime_version="v",
                      worktree="w", write_scope=["a"])
    ledger.record_verification(run_id="r", packet_id="P", test_command="pytest",
                               passed=True, returncode=0)
    assert ledger.terminal_result("r") == RESULT_ACCEPTED_CANDIDATE
    with pytest.raises(LedgerError):
        ledger.record_strong_model_acceptance(run_id="r", packet_id="P",
                                              authority="some_random_model")
    ledger.record_strong_model_acceptance(run_id="r", packet_id="P",
                                          authority="operator", notes="read it")
    assert ledger.terminal_result("r") == RESULT_ACCEPTED


def test_ACCEPTED_cannot_be_written_by_any_other_entry_kind(ledger):
    with pytest.raises(LedgerError):
        ledger.append("attempt", run_id="r", payload={"result": RESULT_ACCEPTED})
    with pytest.raises(LedgerError):
        ledger.append("verification", run_id="r", payload={"result": RESULT_ACCEPTED})


def test_unknown_failure_class_is_refused(ledger):
    with pytest.raises(LedgerError):
        ledger.append("failure", run_id="r", payload={"failure_class": "vibes"})


def test_provenance_excludes_nothing_it_needs_and_names_the_identity(ledger):
    ledger.record_run(run_id="r", packet_id="P", objective="o", role="local_microtask",
                      target_id="local-msr1", host="msr1", model="qwen3.8:27b",
                      runtime_version="0.33.3", worktree="w", write_scope=["a"],
                      base_sha="da4c4357")
    p = ledger.provenance("r")
    assert p["identity"]["host"] == "msr1"
    assert p["identity"]["runtime_version"] == "0.33.3"
    assert p["chain_ok"] is True


# ----------------------------------------------------------- repair packet ---

def test_repair_packet_keeps_acceptance_criteria_unchanged():
    """A repair may not move the goalposts."""
    failure = parse_verification_failure("pytest -q", 1, FAIL_OUTPUT)
    repair = build_repair_packet(PACKET, failure=failure, changed_files={},
                                 attempt=1, budget_remaining=2)
    assert repair["acceptance_criteria"] == PACKET["acceptance_criteria"]
    assert repair["objective"] == PACKET["objective"]


def test_repair_packet_records_what_it_withheld():
    failure = parse_verification_failure("pytest -q", 1, FAIL_OUTPUT)
    repair = build_repair_packet(PACKET, failure=failure, changed_files={},
                                 attempt=1, budget_remaining=2)
    assert "prior conversation" in repair["excluded"]
    assert "prior repair packets" in repair["excluded"]


def test_repair_packet_carries_the_exact_failing_test_and_command():
    failure = parse_verification_failure("python3 -m pytest tests/x.py -q", 1, FAIL_OUTPUT)
    repair = build_repair_packet(PACKET, failure=failure, changed_files={},
                                 attempt=1, budget_remaining=2)
    assert repair["failing_command"] == "python3 -m pytest tests/x.py -q"
    assert "tests/test_thing.py::test_renders_content" in repair["failing_tests"]


def test_repair_packet_is_bounded_even_with_a_huge_log():
    huge = FAIL_OUTPUT + ("x" * 200000)
    failure = parse_verification_failure("pytest", 1, huge)
    repair = build_repair_packet(PACKET, failure=failure, changed_files={},
                                 attempt=1, budget_remaining=2)
    rendered = render_repair_context(repair, max_chars=1500)
    assert len(rendered) <= 1500
    assert "CONTEXT TRUNCATED" in rendered


def test_repair_packet_diff_truncation_keeps_the_head_and_says_so():
    big_diff = "diff --git a/x b/x\n" + "\n".join(f"+line {i}" for i in range(500))
    failure = parse_verification_failure("pytest", 1, FAIL_OUTPUT)
    repair = build_repair_packet(PACKET, failure=failure,
                                 changed_files={"src/thing.py": big_diff},
                                 attempt=1, budget_remaining=1)
    diff = repair["changed_files"]["src/thing.py"]
    assert diff.startswith("diff --git a/x b/x")
    assert "DIFF TRUNCATED" in diff


def test_fingerprint_ignores_line_numbers_but_separates_real_changes():
    a = parse_verification_failure("pytest", 1, FAIL_OUTPUT)
    b = parse_verification_failure("pytest", 1, FAIL_OUTPUT.replace("0.16s", "9.99s"))
    assert failure_fingerprint(a) == failure_fingerprint(b)
    c = parse_verification_failure("pytest", 1,
                                  FAIL_OUTPUT.replace("test_renders_content",
                                                      "test_other_thing"))
    assert failure_fingerprint(a) != failure_fingerprint(c)


def test_collection_error_is_flagged_distinctly():
    out = "!!!! Interrupted: 1 error during collection !!!!\nModuleNotFoundError: No module named 'src.thing'\n"
    failure = parse_verification_failure("pytest", 2, out)
    assert failure.collection_error is True


# -------------------------------------------------------------------- loop ---

def _run(ledger, outcomes, verifications, *, base_context="x" * 400,
         pinned=None, max_attempts=3, packet=None, **kw):
    worker = _Worker(outcomes)
    # generation_reserve is passed explicitly here: the repo default is 4096,
    # which is LARGER than this fixture's 4096 window and therefore refuses
    # everything (see test_default_generation_reserve_refuses_a_4096_window).
    kw.setdefault("generation_reserve", 512)
    res = run_bounded(packet or PACKET, run_id="r1", ledger=ledger,
                      pinned=(pinned or PINNED), dispatch=worker,
                      verify=_verifier(verifications), base_context=base_context,
                      max_attempts=max_attempts, **kw)
    return res, worker


def test_default_generation_reserve_refuses_a_4096_window(ledger):
    """MEASURED CONSEQUENCE, not a bug: the repo's default reserve is 4096.

    `src/context_safety.DEFAULT_GEN_RESERVE_ABSOLUTE = 4096`, so on a target
    serving a 4096 window the invariant `input + reserve <= window` leaves ZERO
    tokens for input. Under the platform's own context-safety rule the MS-R1
    node at its default window cannot accept any packet at all — which is an
    argument about the WINDOW, not about the model, and it is why the router
    must request a larger `num_ctx` (per-request, verified possible in Slice B)
    before routing work there.
    """
    from src.local_worker_loop import default_generation_reserve, usable_input_tokens

    assert default_generation_reserve() == 4096
    assert usable_input_tokens(4096) == 0
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))],
                       [_pass()], generation_reserve=None, base_context="x" * 4000)
    assert res.result == RESULT_BLOCKED and res.budget_refused is True
    assert len(worker.contexts) == 0


def test_loop_accepts_a_candidate_when_verification_passes(ledger):
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",), rounds=1)],
                       [_pass()])
    assert res.result == RESULT_ACCEPTED_CANDIDATE
    assert res.decision == "stop" and res.attempts == 1
    assert len(worker.contexts) == 1


def test_loop_never_reaches_plain_ACCEPTED(ledger):
    """The loop's ceiling is a candidate; acceptance needs a named authority."""
    res, _ = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))], [_pass()])
    assert res.result != RESULT_ACCEPTED
    assert ledger.terminal_result("r1") == RESULT_ACCEPTED_CANDIDATE


def test_loop_repairs_once_then_accepts_and_uses_a_FRESH_context(ledger):
    res, worker = _run(ledger,
                       [DispatchOutcome(artifacts=("src/thing.py",), rounds=1),
                        DispatchOutcome(artifacts=("src/thing.py",), rounds=1)],
                       [_fail(), _pass()])
    assert res.result == RESULT_ACCEPTED_CANDIDATE and res.attempts == 2
    assert len(worker.contexts) == 2
    first, second = worker.contexts
    assert "REPAIR REQUEST" not in first
    assert "REPAIR REQUEST" in second            # fresh repair context
    assert first not in second                   # NOT a replay of the conversation
    assert "criterion one" in second             # acceptance still present
    assert len(ledger.entries_for("r1")) >= 6    # run, attempt, verif, repair, ...


def test_loop_escalates_when_the_same_failure_repeats(ledger):
    """NEGATIVE CONTROL: an identical failure twice means no progress.

    Continuing would spend a slow node's turn to reproduce the same result, which
    is the failure mode a bounded loop exists to stop.
    """
    res, worker = _run(ledger,
                       [DispatchOutcome(artifacts=("src/thing.py",)),
                        DispatchOutcome(artifacts=("src/thing.py",)),
                        DispatchOutcome(artifacts=("src/thing.py",))],
                       [_fail(), _fail(), _fail()], max_attempts=4)
    assert res.result == RESULT_ESCALATE
    assert "same failure" in res.reason
    assert res.attempts == 2
    assert len(worker.contexts) == 2      # the third attempt never happened


def test_loop_escalates_when_the_retry_budget_is_exhausted(ledger):
    res, worker = _run(ledger,
                       [DispatchOutcome(artifacts=("src/thing.py",))] * 3,
                       [_fail_named("t_a"), _fail_named("t_b"), _fail_named("t_c")],
                       max_attempts=3)
    assert res.result == RESULT_ESCALATE
    assert "budget exhausted" in res.reason
    assert res.attempts == 3


def test_a_runtime_failure_is_BLOCKED_not_repaired(ledger):
    """NEGATIVE CONTROL: an outage is not a task failure.

    A provider/runtime fault must never be turned into a repair attempt, because
    the repair would blame the model for the network.
    """
    res, worker = _run(ledger,
                       [DispatchOutcome(failure_class="runtime_provider",
                                        status="ssh timeout")],
                       [_pass()], max_attempts=3)
    assert res.result == RESULT_BLOCKED
    assert res.failure_class == "runtime_provider"
    assert res.attempts == 1
    assert len(worker.contexts) == 1          # no repair round happened
    assert res.reason.find("infrastructure") >= 0


def test_loop_refuses_to_dispatch_when_the_served_window_is_unmeasured(ledger):
    """NEGATIVE CONTROL: unknown window is a REFUSAL, not 'no limit'.

    The measured silent-truncation behaviour means dispatching into an unmeasured
    window can lose the objective without any error at all.
    """
    unknown = PinnedTarget(target_id="local-x", host="h", model="m",
                           served_context=None)
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))],
                       [_pass()], pinned=unknown)
    assert res.result == RESULT_BLOCKED and res.budget_refused is True
    assert len(worker.contexts) == 0          # nothing was dispatched
    assert ledger.failure_class("r1") == "context"


def test_loop_refuses_an_over_budget_packet_before_dispatching(ledger):
    tiny = PinnedTarget(target_id="local-msr1", host="msr1", model="qwen3.8:27b",
                        served_context=512)
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))],
                       [_pass()], base_context="x" * 200000, pinned=tiny)
    assert res.result == RESULT_BLOCKED and res.budget_refused is True
    assert len(worker.contexts) == 0


def test_usable_input_tokens_keeps_room_for_the_answer():
    from src.local_worker_loop import usable_input_tokens
    assert usable_input_tokens(4096, generation_reserve=2048) == 2048
    assert usable_input_tokens(None) is None
    assert usable_input_tokens(1024, generation_reserve=4096) == 0


def test_loop_records_target_identity_on_every_attempt(ledger):
    res, _ = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))], [_pass()])
    attempt = ledger.attempts("r1")[0]
    assert attempt.payload["target_id"] == "local-msr1"
    assert attempt.payload["host"] == "msr1"
    assert attempt.payload["served_context"] == 4096
    prov = ledger.provenance("r1")
    assert prov["identity"]["target_id"] == "local-msr1"


def test_loop_records_a_decision_with_a_reason_for_every_terminal_state(ledger):
    _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))], [_pass()])
    decisions = [e for e in ledger.entries_for("r1") if e.kind == "decision"]
    assert decisions and all(e.payload["reason"] for e in decisions)
    assert decisions[-1].payload["decision"] == "stop"



# ------------------------------------------------- the packet gate (PS-635) ---

def test_loop_refuses_a_packet_without_an_interface(ledger):
    """NEGATIVE CONTROL earned by measurement (2026-09-14).

    A writable packet that does not declare the input keys its contract promises
    must be refused BEFORE dispatch. Measured: with an undeclared interface, a
    local worker guessed the keys, the compact repair packet did NOT recover the
    mismatch, and the loop escalated after two turns. Refusing at the boundary
    costs nothing; repairing an unspecified interface costs turns and still fails.
    """
    bad = dict(PACKET)
    bad.pop("interface")
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))],
                       [_pass()], packet=bad)
    assert res.result == RESULT_BLOCKED
    assert res.failure_class == "packet_invalid"
    assert len(worker.contexts) == 0          # nothing was dispatched
    assert ledger.failure_class("r1") == "packet_invalid"


def test_loop_refuses_a_packet_without_a_test_command(ledger):
    bad = dict(PACKET)
    bad.pop("test_command")
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))],
                       [_pass()], packet=bad)
    assert res.result == RESULT_BLOCKED and res.failure_class == "packet_invalid"
    assert len(worker.contexts) == 0


def test_packet_gate_runs_before_the_budget_gate(ledger):
    """"this is not a packet" is a different answer from "this does not fit"."""
    bad = dict(PACKET)
    bad.pop("interface")
    unknown = PinnedTarget(target_id="local-x", host="h", model="m", served_context=None)
    res, worker = _run(ledger, [DispatchOutcome(artifacts=("src/thing.py",))],
                       [_pass()], pinned=unknown, packet=bad)
    assert res.failure_class == "packet_invalid"
    assert res.budget_refused is False


def test_validate_dispatchable_packet_filters_manager_side_keys():
    """role/contract/base_sha are manager annotations, not packet identity."""
    from src.local_worker_loop import validate_dispatchable_packet
    ok = validate_dispatchable_packet(PACKET)   # carries role + extras
    assert ok.packet_id == "P-T"
    assert "role" not in ok.to_dict()
