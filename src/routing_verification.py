"""src/routing_verification.py — Phase 4 mode-aware verification (spec
Section 16). A task's verification_mode decides WHICH invariants a patch must
satisfy before it can be accepted, instead of Phase 3's flat
run-every-test_command loop:

    regression_guard      existing tests must pass; no new regressions
                          (baseline fuzz); generated tests run ADVISORY only
    bug_fix               the original failing case must now pass; no
                          unrelated regressions; target behavior may change
    feature_addition      existing tests pass AND human-authored acceptance
                          criteria pass; new behavior validated
    refactor_equivalence  optional strict behavioral-equivalence layer:
                          outputs + approved critical invariants must match.
                          Strict equivalence is ONLY available in this mode.
    security_fix          security-specific assertions pass; unsafe prior
                          behavior may change (existing tests advisory);
                          approval gates likely
    analysis_only         NO patch acceptance, ever. Verification means
                          evidence completeness / report quality.

Test authority (spec Section 16): generated tests have authority weight 0 by
default — an unpromoted generated test RUNS and REPORTS but can NEVER flip
`passed`. Only human authority blocks: authority
"human_authored_acceptance_test" natively, or a generated test a human/admin
explicitly PROMOTED (persistent + auditable via the routing_generated_tests
table's promoted/promoted_by/promoted_at columns and notes trail).

Baseline fuzzing: fuzz cases run against BOTH the ORIGINAL worktree (task
repo HEAD) and the PATCHED worktree (HEAD + patch); only NEW regressions
(pass-on-original but fail-on-patched) block. A case already failing on the
original base is pre-existing and never blocks — the baseline comparison,
not promotion, is fuzz's fairness mechanism.

Behavioral equivalence (v1): equivalence commands run on both worktrees and
their STDOUT is compared BYTE-WISE — stdout comparison is the v1 equivalence
check (exit codes are recorded but only a stdout mismatch fails the layer).
Outputs are truncated at sandbox.maxOutputBytes before comparison; a
truncated pair that matches within the cap gets a warning note.

Confidence is METADATA ONLY and never a pass/fail condition: a model-reported
confidence is copied into the result with an explicit neverBlocks marker.
An overconfident failure (passed=False with confidence >= the policy's
verification.overconfidenceThreshold) is flagged for the calibration stats
WP6 consumes; a low-confidence pass may deepen human review — neither ever
changes `passed`.

Every layer command goes through routing_sandbox.run_in_sandbox (allowlist +
network-less container + ToolCallRecord audit rows); a DENIED command is a
layer error and fails a blocking layer CLOSED. Nothing here commits, merges,
or pushes — "patch_accepted" means "eligible for human promotion", never an
automatic git action.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src import routing_policy
from src.routing_coordinator import VerificationMode
from src.routing_patch import validate_patch_shape
from src.routing_sandbox import run_in_sandbox
from src.routing_workdir import (
    apply_patch,
    create_worktree,
    data_root,
    remove_worktree,
    revert_worktree,
)


class TestAuthority(str, Enum):
    """Spec Section 16 test-authority levels. Blocking eligibility is NOT the
    enum itself — see is_blocking_eligible(): only human_authored_acceptance_test
    (native human authority) or an explicitly promoted row may block."""
    EXISTING_REPO_TEST = "existing_repo_test"
    HUMAN_AUTHORED_ACCEPTANCE_TEST = "human_authored_acceptance_test"
    GENERATED_AMPLIFICATION_TEST = "generated_amplification_test"
    GENERATED_FUZZ_CASE = "generated_fuzz_case"
    MODEL_SUGGESTED_TEST = "model_suggested_test"


# Mode -> ordered layer specs, the spec Section 16 invariants table verbatim.
# layer: free-form display name; source: where the layer's commands come from
# (see _layer_entries); blocking: whether the layer's outcome counts toward
# the top-level `passed`. Notes on the encoding:
#   - regression_guard's "generated tests advisory" row is a blocking=False
#     layer: generated/acceptance tests run and report but never gate here.
#   - "no (new/unrelated) regressions" rows are the baseline-fuzz layer,
#     blocking ONLY on new regressions (see _run_fuzz_layer).
#   - security_fix: "unsafe prior behavior may change" -> existing tests are
#     advisory (blocking=False); only the security assertions gate.
#   - equivalence appears ONLY under refactor_equivalence — strict behavioral
#     equivalence is not available in any other mode.
#   - analysis_only is [] and never accepts a patch (MODE_ACCEPTS_PATCH).
MODE_INVARIANTS: Dict[str, List[Dict[str, Any]]] = {
    VerificationMode.REGRESSION_GUARD.value: [
        {"layer": "existing_tests", "source": "existing_tests", "blocking": True},
        {"layer": "generated_tests", "source": "acceptance_tests", "blocking": False},
        {"layer": "baseline_fuzz", "source": "fuzz", "blocking": True},
    ],
    VerificationMode.BUG_FIX.value: [
        {"layer": "original_failing_case", "source": "original_failing_case", "blocking": True},
        {"layer": "existing_tests", "source": "existing_tests", "blocking": True},
        {"layer": "baseline_fuzz", "source": "fuzz", "blocking": True},
    ],
    VerificationMode.FEATURE_ADDITION.value: [
        {"layer": "existing_tests", "source": "existing_tests", "blocking": True},
        {"layer": "acceptance_tests", "source": "acceptance_tests", "blocking": True},
    ],
    VerificationMode.REFACTOR_EQUIVALENCE.value: [
        {"layer": "existing_tests", "source": "existing_tests", "blocking": True},
        {"layer": "behavioral_equivalence", "source": "equivalence", "blocking": True},
        {"layer": "baseline_fuzz", "source": "fuzz", "blocking": True},
    ],
    VerificationMode.SECURITY_FIX.value: [
        {"layer": "security_assertions", "source": "security_assertions", "blocking": True},
        {"layer": "existing_tests", "source": "existing_tests", "blocking": False},
    ],
    VerificationMode.ANALYSIS_ONLY.value: [],
}

# analysis_only NEVER accepts a patch; every other mode gates acceptance on
# its blocking layers.
MODE_ACCEPTS_PATCH: Dict[str, bool] = {
    m.value: m is not VerificationMode.ANALYSIS_ONLY for m in VerificationMode
}

# Sources that compare against an unpatched baseline and therefore need the
# second (ORIGINAL) worktree.
_BASELINE_SOURCES = ("fuzz", "equivalence")

# Task-type -> verification-mode inference used when a task carries no
# explicit verification_mode (spec Section 16 defaults).
INFERRED_MODE_BY_TASK_TYPE = {
    "bug_debug": VerificationMode.BUG_FIX.value,
    "implementation": VerificationMode.FEATURE_ADDITION.value,
    "diff_review": VerificationMode.ANALYSIS_ONLY.value,
    "feature_plan": VerificationMode.ANALYSIS_ONLY.value,
}

_SKIP_NOTES = {
    "existing_tests": "no inputs.test_commands configured; existing-tests layer skipped",
    "original_failing_case": ("no inputs.failing_case_commands provided; original-failing-case "
                              "layer skipped (record the reproducer to make bug_fix meaningful)"),
    "acceptance_tests": ("no acceptance tests configured (inputs.acceptance_commands empty and "
                         "no registry entries for this task); layer skipped"),
    "equivalence": ("optional strict-equivalence layer skipped: no inputs.equivalence_commands "
                    "provided"),
    "security_assertions": "no inputs.security_commands provided; security-assertions layer skipped",
    "fuzz": "no generated_fuzz_case registry entries for this task; baseline-fuzz layer skipped",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_mode(task, policy: Optional[dict] = None) -> str:
    """Verification mode inferred from task_type when the task doesn't carry
    one: bug_debug->bug_fix, implementation->feature_addition,
    diff_review/feature_plan->analysis_only, anything else falls back to the
    policy's verification.defaultMode (regression_guard by default)."""
    mapped = INFERRED_MODE_BY_TASK_TYPE.get(task.task_type)
    if mapped:
        return mapped
    if policy is None:
        policy = routing_policy.load_policy()
    default = (policy.get("verification") or {}).get("defaultMode")
    if default in MODE_INVARIANTS:
        return default
    return VerificationMode.REGRESSION_GUARD.value


def is_blocking_eligible(test_row) -> bool:
    """Spec Section 16 blocking eligibility for a registry test row: ONLY
    human authority blocks — authority == human_authored_acceptance_test, or
    promoted == True (the persistent, auditable human grant). Everything else
    is weight-0/advisory: it runs and reports but can never flip `passed`."""
    return (
        test_row.authority == TestAuthority.HUMAN_AUTHORED_ACCEPTANCE_TEST.value
        or bool(test_row.promoted)
    )


def load_patch_text(model_run) -> str:
    """The archived patch.diff located via artifacts.patch_path — the same
    convention odysseus-patch writes/reads (shared here so odysseus-exec and
    this module never diverge). Raises ValueError when no archived patch
    exists; callers (CLI fail(), route 400) decide how to surface it."""
    artifacts = json.loads(model_run.artifacts) if model_run.artifacts else {}
    patch_path = artifacts.get("patch_path")
    if not patch_path or not os.path.isfile(patch_path):
        raise ValueError(
            f"no archived patch found for {model_run.id!r} (artifacts.patch_path "
            "missing or unreadable) -- run `odysseus-patch extract` first"
        )
    with open(patch_path, "r", errors="replace") as f:
        return f.read()


def attempt_dir(model_run) -> Optional[str]:
    """The model run's archive dir (where prompt.md/response.md live), so
    sandbox stdout/stderr land next to the run's other artifacts. None when
    the run predates archiving — run_in_sandbox falls back to its default."""
    artifacts = json.loads(model_run.artifacts) if model_run.artifacts else {}
    for key in ("response_text_path", "patch_path", "prompt_path"):
        p = artifacts.get(key)
        if p:
            return os.path.dirname(p)
    return None


def _registry_tests(db, task_id: str) -> list:
    """All routing_generated_tests rows for the task, oldest first (stable
    layer ordering across re-verifications)."""
    from core.database import GeneratedTest

    return (
        db.query(GeneratedTest)
        .filter(GeneratedTest.task_id == task_id)
        .order_by(GeneratedTest.created_at, GeneratedTest.id)
        .all()
    )


def _layer_entries(db, task, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The commands a layer will run, as entries {cmd, advisory, origin}.
    advisory=True marks a weight-0 command whose failure is reported but can
    never fail the layer (unpromoted generated tests)."""
    inputs = json.loads(task.inputs) if task.inputs else {}
    source = spec["source"]

    def _from_inputs(key: str) -> List[Dict[str, Any]]:
        return [
            {"cmd": c, "advisory": False, "origin": f"task.inputs.{key}"}
            for c in (inputs.get(key) or [])
        ]

    if source == "existing_tests":
        return _from_inputs("test_commands")
    if source == "original_failing_case":
        return _from_inputs("failing_case_commands")
    if source == "equivalence":
        return _from_inputs("equivalence_commands")
    if source == "security_assertions":
        return _from_inputs("security_commands")
    if source == "acceptance_tests":
        # Human-authored by definition — they came from the task author.
        entries = _from_inputs("acceptance_commands")
        fuzz_value = TestAuthority.GENERATED_FUZZ_CASE.value
        for row in _registry_tests(db, task.id):
            if row.authority == fuzz_value:
                continue  # fuzz cases belong to the fuzz layer
            entries.append({
                "cmd": row.command,
                "advisory": not is_blocking_eligible(row),
                "origin": f"generated_test:{row.id}",
                "authority": row.authority,
                "promoted": bool(row.promoted),
            })
        return entries
    if source == "fuzz":
        fuzz_value = TestAuthority.GENERATED_FUZZ_CASE.value
        return [
            {"cmd": row.command, "advisory": False,
             "origin": f"generated_test:{row.id}", "authority": row.authority}
            for row in _registry_tests(db, task.id)
            if row.authority == fuzz_value
        ]
    raise ValueError(f"unknown layer source {source!r}")


def _cmd_ok(sb: dict) -> bool:
    return bool(sb["allowed"]) and sb["error"] is None and sb["exit_code"] == 0


def _base_record(entry: Dict[str, Any], sb: dict) -> Dict[str, Any]:
    rec = {
        "cmd": entry["cmd"],
        "exit_code": sb["exit_code"],
        "tool_call_record_id": sb["tool_call_record_id"],
        "advisory": entry["advisory"],
        "origin": entry["origin"],
    }
    if sb["error"]:
        rec["error"] = sb["error"]
    return rec


def _skipped_layer(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "layer": spec["layer"], "source": spec["source"], "blocking": spec["blocking"],
        "passed": True, "skipped": True, "commands": [],
        "notes": [_SKIP_NOTES[spec["source"]]],
    }


def _new_layer(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "layer": spec["layer"], "source": spec["source"], "blocking": spec["blocking"],
        "passed": True, "skipped": False, "commands": [], "notes": [],
    }


def _run_single_tree_layer(db, spec, entries, worktree, *, policy, run_id,
                           artifacts_dir, sandbox_log) -> Dict[str, Any]:
    """Layers judged on the PATCHED tree alone (existing/acceptance/failing-
    case/security). Layer passes iff every NON-advisory command exits 0; a
    denied or infrastructure-failed command fails the layer closed; advisory
    (weight-0) failures are reported in notes but never fail the layer."""
    layer = _new_layer(spec)
    for entry in entries:
        sb = run_in_sandbox(worktree, entry["cmd"], policy=policy, run_id=run_id,
                            db=db, artifacts_dir=artifacts_dir)
        sandbox_log.append(sb)
        layer["commands"].append(_base_record(entry, sb))
        if _cmd_ok(sb):
            continue
        if entry["advisory"]:
            layer["notes"].append(
                f"advisory (weight-0) test failed without blocking: {entry['cmd']!r} "
                f"({entry['origin']}) — promote it to make it blocking"
            )
            continue
        layer["passed"] = False
        if not sb["allowed"]:
            layer["notes"].append(
                f"command denied by the sandbox allowlist (fail-closed): {entry['cmd']!r}"
            )
        elif sb["error"]:
            layer["notes"].append(f"command error (fail-closed): {entry['cmd']!r}: {sb['error']}")
    return layer


def _run_fuzz_layer(db, spec, entries, original_wt, patched_wt, *, policy,
                    run_id, artifacts_dir, sandbox_log) -> Dict[str, Any]:
    """Baseline fuzzing: each case runs on the ORIGINAL and the PATCHED
    worktree. Blocks ONLY on new regressions (ok-on-original, not-ok-on-
    patched). A case failing on the original base is pre-existing and never
    blocks. Denied/infrastructure errors fail the layer closed."""
    layer = _new_layer(spec)
    for entry in entries:
        orig = run_in_sandbox(original_wt, entry["cmd"], policy=policy, run_id=run_id,
                              db=db, artifacts_dir=artifacts_dir)
        pat = run_in_sandbox(patched_wt, entry["cmd"], policy=policy, run_id=run_id,
                             db=db, artifacts_dir=artifacts_dir)
        sandbox_log.extend([orig, pat])
        rec = _base_record(entry, pat)
        rec["original_exit_code"] = orig["exit_code"]
        rec["original_tool_call_record_id"] = orig["tool_call_record_id"]
        if orig["error"]:
            rec["original_error"] = orig["error"]

        hard_error = (
            not orig["allowed"] or not pat["allowed"]
            or orig["error"] is not None or pat["error"] is not None
        )
        if hard_error:
            rec["new_regression"] = None
            layer["passed"] = False
            layer["notes"].append(
                f"fuzz case could not be adjudicated (denied or errored on one side, "
                f"fail-closed): {entry['cmd']!r}"
            )
            layer["commands"].append(rec)
            continue

        orig_ok = orig["exit_code"] == 0
        pat_ok = pat["exit_code"] == 0
        rec["new_regression"] = orig_ok and not pat_ok
        rec["pre_existing_failure"] = not orig_ok
        layer["commands"].append(rec)
        if rec["new_regression"]:
            layer["passed"] = False
            layer["notes"].append(
                f"NEW regression: {entry['cmd']!r} passes on the original base but fails "
                "on the patched tree"
            )
        elif not orig_ok:
            layer["notes"].append(
                f"pre-existing failure (fails on the original base too): {entry['cmd']!r} "
                "— non-blocking"
            )
    return layer


def _read_bytes(path: Optional[str]) -> bytes:
    if not path:
        return b""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _run_equivalence_layer(db, spec, entries, original_wt, patched_wt, *, policy,
                           run_id, artifacts_dir, sandbox_log) -> Dict[str, Any]:
    """Strict behavioral equivalence (refactor_equivalence ONLY): each command
    runs on both worktrees and stdout must match BYTE-WISE — the v1
    equivalence check. Exit codes are recorded for the audit trail but only a
    stdout mismatch (or a denied/errored command, fail-closed) fails."""
    layer = _new_layer(spec)
    for entry in entries:
        orig = run_in_sandbox(original_wt, entry["cmd"], policy=policy, run_id=run_id,
                              db=db, artifacts_dir=artifacts_dir)
        pat = run_in_sandbox(patched_wt, entry["cmd"], policy=policy, run_id=run_id,
                             db=db, artifacts_dir=artifacts_dir)
        sandbox_log.extend([orig, pat])
        rec = _base_record(entry, pat)
        rec["original_exit_code"] = orig["exit_code"]
        rec["original_tool_call_record_id"] = orig["tool_call_record_id"]

        hard_error = (
            not orig["allowed"] or not pat["allowed"]
            or orig["error"] is not None or pat["error"] is not None
        )
        if hard_error:
            rec["stdout_match"] = None
            layer["passed"] = False
            layer["notes"].append(
                f"equivalence command could not be adjudicated (denied or errored, "
                f"fail-closed): {entry['cmd']!r}"
            )
            layer["commands"].append(rec)
            continue

        match = _read_bytes(orig["stdout_path"]) == _read_bytes(pat["stdout_path"])
        rec["stdout_match"] = match
        layer["commands"].append(rec)
        if not match:
            layer["passed"] = False
            layer["notes"].append(
                f"stdout mismatch between original and patched (v1 byte-wise "
                f"equivalence check): {entry['cmd']!r}"
            )
        if orig["stdout_truncated"] or pat["stdout_truncated"]:
            layer["notes"].append(
                f"stdout truncated at sandbox.maxOutputBytes for {entry['cmd']!r}; "
                "equivalence compared the truncated prefixes only"
            )
    return layer


def _apply_confidence(result: dict, scores: dict, threshold: float) -> None:
    """Confidence is metadata ONLY — copied into the result, never consulted
    for pass/fail. Overconfident failures get a calibration flag for WP6's
    model-calibration stats; a low-confidence pass may deepen human review;
    neither changes `passed`."""
    conf = scores.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return
    conf = float(conf)
    if not (0.0 <= conf <= 1.0):
        return
    result["confidence"] = {
        "value": conf,
        "neverBlocks": True,
        "note": "confidence is metadata only; it never changes passed",
    }
    if not result["passed"] and conf >= threshold:
        result["calibration"] = {"overconfidentFailure": True}


def _finalize(db, model_run, result: dict, run_dir: str, sandbox_log: list,
              threshold: float) -> dict:
    """Stamp confidence/infrastructure metadata, archive verification.json
    next to the run's manifest.json, and persist into model_run.scores under
    "verification" (merged — other score keys are never clobbered) plus the
    tool-call artifact trail."""
    scores = json.loads(model_run.scores) if model_run.scores else {}
    _apply_confidence(result, scores, threshold)
    if any(sb.get("error") == "docker_unavailable" for sb in sandbox_log):
        # Infrastructure failure, not command failure: still fail-closed, but
        # flagged so a docker-equipped re-run isn't scored as a patch break.
        result["infrastructure_error"] = "docker_unavailable"
    result["completed_at"] = _now_iso()

    verification_path = None
    try:
        os.makedirs(run_dir, exist_ok=True)
        verification_path = os.path.join(run_dir, "verification.json")
        with open(verification_path, "w") as f:
            json.dump({"model_run_id": model_run.id, **result}, f, indent=2, default=str)
    except OSError as e:
        verification_path = None
        result["notes"].append(f"could not archive verification.json: {e}")

    scores["verification"] = result
    model_run.scores = json.dumps(scores)

    artifacts = json.loads(model_run.artifacts) if model_run.artifacts else {}
    if sandbox_log:
        calls = artifacts.get("tool_calls") or []
        for sb in sandbox_log:
            calls.append({
                "cmd": sb["cmd"],
                "allowed": sb["allowed"],
                "exit_code": sb["exit_code"],
                "timed_out": sb["timed_out"],
                "stdout_path": sb["stdout_path"],
                "stderr_path": sb["stderr_path"],
                "tool_call_record_id": sb["tool_call_record_id"],
            })
        artifacts["tool_calls"] = calls
    if verification_path:
        artifacts["verification_path"] = verification_path
    model_run.artifacts = json.dumps(artifacts)
    db.commit()
    return result


def _wt_id(model_run_id: str, tag: str) -> str:
    return f"{model_run_id}-{tag}-{uuid.uuid4().hex[:8]}"


def _cleanup_worktree(repo_path: str, worktree: str, notes: List[str]) -> None:
    """Best-effort revert (the documented failed-patch reset) then remove.
    Cleanup failure must never destroy a completed verification verdict — it
    is recorded as a note instead."""
    try:
        revert_worktree(worktree)
    except Exception:
        pass
    try:
        remove_worktree(repo_path, worktree)
    except Exception as e:  # noqa: BLE001 — reported, not raised
        notes.append(f"worktree cleanup failed for {worktree!r}: {e}")


def verify_model_run(db, task, model_run, *, mode: Optional[str] = None,
                     allow_dirty: bool = False, keep_worktree: bool = False,
                     policy: Optional[dict] = None) -> dict:
    """Run spec Section 16 mode-aware verification for one archived model run
    and return the VerificationResult dict:

        {"mode", "passed", "patch_accepted", "patch_applied", "layers":
         [{layer, source, blocking, passed, skipped, commands: [{cmd,
         exit_code, tool_call_record_id, ...}], notes}], "notes",
         "confidence"?, "calibration"?, "infrastructure_error"?,
         "completed_at"}

    Mode resolution: explicit `mode` arg > task.verification_mode >
    infer_mode(task). `passed` = every BLOCKING layer passed; advisory layers
    and weight-0 commands report but never gate. analysis_only runs no
    commands and NEVER accepts a patch — its `passed` is evidence
    completeness (a non-empty archived analysis/response text).

    Worktrees: one temp worktree at the task repo's HEAD with the patch
    applied; a second ORIGINAL (unpatched) worktree is added only when a
    fuzz/equivalence layer actually has commands to baseline against. ALL
    worktrees are reverted+removed at the end — keep_worktree=True keeps the
    PATCHED one only (path reported in the result). Nothing is ever
    committed, merged, or pushed.

    The result is persisted into model_run.scores["verification"] (merged,
    other keys preserved) and archived as verification.json in the run's
    archive dir next to manifest.json. Raises ValueError for an unknown mode
    or a missing archived patch."""
    if policy is None:
        policy = routing_policy.load_policy()
    verification_cfg = policy.get("verification") or {}
    threshold = float(verification_cfg.get("overconfidenceThreshold", 0.8))

    resolved = mode or task.verification_mode or infer_mode(task, policy)
    if resolved not in MODE_INVARIANTS:
        allowed = "|".join(sorted(MODE_INVARIANTS))
        raise ValueError(f"unknown verification mode {resolved!r} (allowed: {allowed})")

    art_dir = attempt_dir(model_run)
    run_dir = (os.path.dirname(art_dir) if art_dir
               else os.path.join(data_root(), "routing", "runs", task.id, model_run.run_id))

    result: Dict[str, Any] = {
        "mode": resolved,
        "passed": False,
        "patch_accepted": False,
        "patch_applied": None,
        "layers": [],
        "notes": [],
    }
    sandbox_log: List[dict] = []

    # --- analysis_only: no patch, no worktrees, no commands, EVER ---
    if resolved == VerificationMode.ANALYSIS_ONLY.value:
        artifacts = json.loads(model_run.artifacts) if model_run.artifacts else {}
        response_path = artifacts.get("response_text_path")
        evidence_complete = bool(
            response_path and os.path.isfile(response_path)
            and os.path.getsize(response_path) > 0
        )
        result["passed"] = evidence_complete
        result["notes"].append(
            "analysis_only NEVER accepts a patch; passed reflects evidence "
            "completeness (v1: a non-empty archived response text)"
        )
        if not evidence_complete:
            result["notes"].append("no archived analysis output found (artifacts.response_text_path)")
        return _finalize(db, model_run, result, run_dir, sandbox_log, threshold)

    # --- patch-accepting modes ---
    patch_text = load_patch_text(model_run)  # ValueError when absent
    validation = validate_patch_shape(patch_text, task.repo_path)
    if not validation["allowed"]:
        result["patch_applied"] = False
        result["error"] = "patch failed shape re-validation"
        result["patch_validation_reasons"] = validation["reasons"]
        return _finalize(db, model_run, result, run_dir, sandbox_log, threshold)

    specs = MODE_INVARIANTS[resolved]
    entries_by_index = [_layer_entries(db, task, spec) for spec in specs]
    needs_baseline = any(
        spec["source"] in _BASELINE_SOURCES and entries_by_index[i]
        for i, spec in enumerate(specs)
    )

    created: List[str] = []
    patched_wt: Optional[str] = None
    try:
        patched_wt = create_worktree(task.repo_path, _wt_id(model_run.id, "vfy"),
                                     base_ref="HEAD", allow_dirty=allow_dirty)
        created.append(patched_wt)
        applied = apply_patch(patched_wt, patch_text)
        if not applied["applied"]:
            result["patch_applied"] = False
            result["error"] = applied["error"]
            return _finalize(db, model_run, result, run_dir, sandbox_log, threshold)
        result["patch_applied"] = True
        result["changed_files"] = applied["changed_files"]

        original_wt: Optional[str] = None
        if needs_baseline:
            original_wt = create_worktree(task.repo_path, _wt_id(model_run.id, "orig"),
                                          base_ref="HEAD", allow_dirty=allow_dirty)
            created.append(original_wt)

        run_kwargs = dict(policy=policy, run_id=model_run.run_id,
                          artifacts_dir=art_dir, sandbox_log=sandbox_log)
        for spec, entries in zip(specs, entries_by_index):
            if not entries:
                result["layers"].append(_skipped_layer(spec))
                continue
            if spec["source"] == "fuzz":
                layer = _run_fuzz_layer(db, spec, entries, original_wt, patched_wt, **run_kwargs)
            elif spec["source"] == "equivalence":
                layer = _run_equivalence_layer(db, spec, entries, original_wt, patched_wt, **run_kwargs)
            else:
                layer = _run_single_tree_layer(db, spec, entries, patched_wt, **run_kwargs)
            result["layers"].append(layer)

        result["passed"] = all(l["passed"] for l in result["layers"] if l["blocking"])
        result["patch_accepted"] = bool(result["passed"] and MODE_ACCEPTS_PATCH[resolved])
        if result["layers"] and all(l.get("skipped") for l in result["layers"]):
            result["notes"].append(
                "every layer was skipped (no verification commands configured) — "
                "passed is vacuous; add test/acceptance commands to make it meaningful"
            )
    finally:
        for wt in created:
            if keep_worktree and wt == patched_wt:
                result["worktree_path"] = patched_wt
                continue
            _cleanup_worktree(task.repo_path, wt, result["notes"])

    return _finalize(db, model_run, result, run_dir, sandbox_log, threshold)
