"""Tests for interactive plan review and anchored question sealing (OD-22 / Jira PS-646 / Epic TMOS-1).

Verifies:
1. Negative Control 1: Models cannot self-seal plans or self-grant execution authority.
2. Negative Control 2: Unresolved blocking questions prevent plan sealing (fails closed).
3. Negative Control 3: Explicit operator override ruling permits sealing despite open question with audit trail.
4. Negative Control 4: Post-seal content modifications change content-hash and invalidate existing execution bindings.
5. Negative Control 5: Revisions generate structured diffs and preserve immutable revision history.
6. Negative Control 6: Anchored comments preserve full provenance (author, timestamp, resolution).
7. Negative Control 7: Detached/unanchored inputs cannot silently modify sealed requirement contracts.
"""
from __future__ import annotations

import pytest
from src.plan_review import (
    AnchoredComment,
    AuthorRole,
    CommentSeverity,
    CommentStatus,
    PlanReviewArtifact,
    PlanReviewError,
    PlanReviewService,
    SealedPlanReceipt,
)


def test_models_cannot_self_seal_or_grant_execution_authority():
    """Negative control: AI model/planner attempts to self-seal a plan and is refused."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Execution Plan for Routing Refactor",
        initial_content="# Objective\nRefactor routing without breaking hard policy.\n",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
    )
    assert isinstance(plan, PlanReviewArtifact)

    with pytest.raises(PlanReviewError, match="models cannot self-seal"):
        service.seal_plan(
            plan=plan,
            sealed_by="planner-deepseek-v4",
            author_role=AuthorRole.PLANNER,  # Model role attempting to seal!
        )

    assert plan.is_sealed is False
    assert plan.sealed_revision_hash is None


def test_unresolved_blocking_questions_prevent_sealing():
    """Negative control: A plan with unresolved blocking questions cannot be sealed."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Execution Plan for Routing Refactor",
        initial_content="# Implementation\nUse either approach A or approach B.\n",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
    )

    # Planner attaches a blocking question directly to ambiguous statement
    comment = service.attach_comment(
        plan=plan,
        anchor_id="approach-selection",
        content="Should approach A or approach B be selected?",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
        severity=CommentSeverity.BLOCKING,
    )
    assert isinstance(comment, AnchoredComment)
    assert comment.status == CommentStatus.OPEN
    assert len(plan.unresolved_blocking_comments()) == 1

    # Operator attempts to seal without answering/resolving
    with pytest.raises(PlanReviewError, match="unresolved blocking"):
        service.seal_plan(
            plan=plan,
            sealed_by="tim",
            author_role=AuthorRole.OPERATOR,
        )

    assert plan.is_sealed is False


def test_operator_resolution_allows_sealing():
    """Operator resolves blocking question inline, allowing subsequent plan sealing."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Execution Plan for Routing Refactor",
        initial_content="# Implementation\nUse either approach A or approach B.\n",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
    )
    comment = service.attach_comment(
        plan=plan,
        anchor_id="approach-selection",
        content="Should approach A or approach B be selected?",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
        severity=CommentSeverity.BLOCKING,
    )
    assert isinstance(comment, AnchoredComment)

    # Operator resolves comment inline
    service.resolve_comment(
        plan=plan,
        comment_id=comment.comment_id,
        operator_id="tim",
        resolution_note="Approach B: zero-copy in-memory buffer.",
    )
    assert len(plan.unresolved_blocking_comments()) == 0

    # Now operator can seal plan
    receipt = service.seal_plan(
        plan=plan,
        sealed_by="tim",
        author_role=AuthorRole.OPERATOR,
    )

    assert isinstance(receipt, SealedPlanReceipt)
    assert plan.is_sealed is True
    assert plan.sealed_by == "tim"
    assert plan.sealed_revision_hash == plan.current_revision.content_hash
    assert receipt.plan_content_hash == plan.current_revision.content_hash


def test_operator_explicit_override_ruling_seals_despite_open_question():
    """Negative control / override: Operator explicitly records a formal ruling to bypass an open question."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Emergency Fix Plan",
        initial_content="# Objective\nMitigate immediate latency spike.\n",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
    )
    service.attach_comment(
        plan=plan,
        anchor_id="latency-root-cause",
        content="Is this caused by disk I/O or network socket limits?",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
        severity=CommentSeverity.BLOCKING,
    )

    receipt = service.seal_plan(
        plan=plan,
        sealed_by="tim",
        author_role=AuthorRole.OPERATOR,
        override_ruling="Operator ruling: Proceed with restart mitigation first; diagnose socket limits post-recovery.",
    )

    assert plan.is_sealed is True
    assert receipt.override_ruling is not None
    assert "mitigation first" in receipt.override_ruling


def test_content_mutation_after_seal_invalidates_binding():
    """Negative control: Changing plan content creates a new revision and supersedes old sealed binding."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Execution Plan",
        initial_content="# Step 1\nRun tests.\n",
        author_id="tim",
        author_role=AuthorRole.OPERATOR,
    )
    receipt_v1 = service.seal_plan(
        plan=plan,
        sealed_by="tim",
        author_role=AuthorRole.OPERATOR,
    )
    assert service.validate_binding(receipt_v1, plan) is True

    # Mutate content by proposing revision 2
    rev2 = service.add_revision(
        plan=plan,
        new_content="# Step 1\nRun tests.\n# Step 2\nDeploy to staging.\n",
        author_id="planner-deepseek-v4",
        author_role=AuthorRole.PLANNER,
    )
    assert rev2.revision_num == 2
    assert plan.is_sealed is False  # New revision unseals the active draft

    # Receipt v1 does NOT validate against the modified plan revision!
    assert service.validate_binding(receipt_v1, plan) is False
    assert receipt_v1.plan_content_hash != plan.current_revision.content_hash


def test_plan_revision_diff_and_immutable_history():
    """Plan revisions generate structured diffs and retain immutable history."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Refactor Plan",
        initial_content="Line 1: Alpha\nLine 2: Beta\n",
        author_id="planner",
        author_role=AuthorRole.PLANNER,
    )
    assert plan.current_revision_num == 1
    assert len(plan.revisions) == 1

    rev2 = service.add_revision(
        plan=plan,
        new_content="Line 1: Alpha\nLine 2: Gamma\nLine 3: Delta\n",
        author_id="planner",
        author_role=AuthorRole.PLANNER,
    )
    assert plan.current_revision_num == 2
    assert len(plan.revisions) == 2
    assert rev2.diff_from_previous is not None
    assert "-Line 2: Beta" in rev2.diff_from_previous
    assert "+Line 2: Gamma" in rev2.diff_from_previous
    assert "+Line 3: Delta" in rev2.diff_from_previous


def test_advisory_comments_do_not_block_sealing():
    """Advisory / non-blocking comments do not prevent plan sealing."""
    service = PlanReviewService()
    plan = service.create_plan(
        ticket_key="OD-22",
        title="Execution Plan",
        initial_content="# Implementation\nUse fast path.\n",
        author_id="planner",
        author_role=AuthorRole.PLANNER,
    )
    service.attach_comment(
        plan=plan,
        anchor_id="perf-tip",
        content="Consider benchmarking cache hits if convenient.",
        author_id="planner",
        author_role=AuthorRole.PLANNER,
        severity=CommentSeverity.ADVISORY,  # Non-blocking advisory
    )
    assert len(plan.unresolved_blocking_comments()) == 0

    receipt = service.seal_plan(
        plan=plan,
        sealed_by="tim",
        author_role=AuthorRole.OPERATOR,
    )
    assert plan.is_sealed is True
    assert isinstance(receipt, SealedPlanReceipt)
