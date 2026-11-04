"""Interactive plan review and anchored question sealing (OD-22 / Jira PS-646 / Epic TMOS-1).

Bridges the interaction gap between Plane issues and autonomous ExecutionPackages:
Plane intent -> planner draft -> anchored questions/answers -> SEALED revision -> ExecutionPackage.

Enforces:
1. Hard authority invariant: models may draft/revise/propose, but cannot self-seal
   or self-grant execution authority.
2. Unresolved blocking questions prevent plan sealing (fail closed) unless an explicit
   operator ruling is recorded.
3. Content-addressed plan sealing binds exact SHA-256 hash to ExecutionPackages; any
   subsequent modification creates a new revision and supersedes existing bindings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
from enum import Enum
import hashlib
from typing import List, Optional
import uuid


SCHEMA_SEALED_RECEIPT = "sealed-plan-receipt-v1"


class PlanReviewError(Exception):
    """Raised when plan review, modification, or sealing fails validation."""
    pass


class CommentStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class CommentSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class AuthorRole(str, Enum):
    PLANNER = "PLANNER"
    OPERATOR = "OPERATOR"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class AnchoredComment:
    comment_id: str
    anchor_id: str
    content: str
    author_id: str
    author_role: AuthorRole
    severity: CommentSeverity = CommentSeverity.BLOCKING
    status: CommentStatus = CommentStatus.OPEN
    created_at: str = field(default_factory=utc_now)
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_note: Optional[str] = None


@dataclass(frozen=True)
class PlanRevision:
    revision_num: int
    content: str
    content_hash: str
    created_at: str
    created_by: str
    created_by_role: AuthorRole
    diff_from_previous: Optional[str] = None


@dataclass(frozen=True)
class SealedPlanReceipt:
    receipt_id: str
    ticket_key: str
    revision_num: int
    plan_content_hash: str
    sealed_by: str
    sealed_at: str
    override_ruling: Optional[str] = None
    schema: str = SCHEMA_SEALED_RECEIPT


@dataclass
class PlanReviewArtifact:
    ticket_key: str
    title: str
    current_revision_num: int
    revisions: List[PlanRevision] = field(default_factory=list)
    comments: List[AnchoredComment] = field(default_factory=list)
    is_sealed: bool = False
    sealed_at: Optional[str] = None
    sealed_by: Optional[str] = None
    sealed_revision_hash: Optional[str] = None

    @property
    def current_revision(self) -> PlanRevision:
        if not self.revisions:
            raise PlanReviewError("Plan has no revisions.")
        return self.revisions[-1]

    def unresolved_blocking_comments(self) -> List[AnchoredComment]:
        return [
            c for c in self.comments
            if c.severity == CommentSeverity.BLOCKING and c.status == CommentStatus.OPEN
        ]


class PlanReviewService:
    """Manages the lifecycle of interactive execution plan reviews."""

    def create_plan(
        self,
        ticket_key: str,
        title: str,
        initial_content: str,
        author_id: str,
        author_role: AuthorRole,
    ) -> PlanReviewArtifact:
        content_hash = hash_content(initial_content)
        rev1 = PlanRevision(
            revision_num=1,
            content=initial_content,
            content_hash=content_hash,
            created_at=utc_now(),
            created_by=author_id,
            created_by_role=author_role,
            diff_from_previous=None,
        )
        return PlanReviewArtifact(
            ticket_key=ticket_key,
            title=title,
            current_revision_num=1,
            revisions=[rev1],
            comments=[],
            is_sealed=False,
        )

    def add_revision(
        self,
        plan: PlanReviewArtifact,
        new_content: str,
        author_id: str,
        author_role: AuthorRole,
    ) -> PlanRevision:
        prev = plan.current_revision
        diff_lines = list(
            difflib.unified_diff(
                prev.content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"rev_{prev.revision_num}",
                tofile=f"rev_{prev.revision_num + 1}",
            )
        )
        diff_text = "".join(diff_lines) if diff_lines else None

        new_rev_num = prev.revision_num + 1
        content_hash = hash_content(new_content)
        rev = PlanRevision(
            revision_num=new_rev_num,
            content=new_content,
            content_hash=content_hash,
            created_at=utc_now(),
            created_by=author_id,
            created_by_role=author_role,
            diff_from_previous=diff_text,
        )
        plan.revisions.append(rev)
        plan.current_revision_num = new_rev_num

        # New revision unseals the active draft so it can be re-reviewed
        plan.is_sealed = False
        plan.sealed_at = None
        plan.sealed_by = None
        plan.sealed_revision_hash = None

        return rev

    def attach_comment(
        self,
        plan: PlanReviewArtifact,
        anchor_id: str,
        content: str,
        author_id: str,
        author_role: AuthorRole,
        severity: CommentSeverity = CommentSeverity.BLOCKING,
    ) -> AnchoredComment:
        comment_id = f"c-{uuid.uuid4().hex[:8]}"
        comment = AnchoredComment(
            comment_id=comment_id,
            anchor_id=anchor_id,
            content=content,
            author_id=author_id,
            author_role=author_role,
            severity=severity,
            status=CommentStatus.OPEN,
            created_at=utc_now(),
        )
        plan.comments.append(comment)
        return comment

    def resolve_comment(
        self,
        plan: PlanReviewArtifact,
        comment_id: str,
        operator_id: str,
        resolution_note: str,
    ) -> AnchoredComment:
        for c in plan.comments:
            if c.comment_id == comment_id:
                c.status = CommentStatus.RESOLVED
                c.resolved_by = operator_id
                c.resolved_at = utc_now()
                c.resolution_note = resolution_note
                return c
        raise PlanReviewError(f"Comment {comment_id} not found on plan {plan.ticket_key}.")

    def reopen_comment(
        self,
        plan: PlanReviewArtifact,
        comment_id: str,
        operator_id: str,
    ) -> AnchoredComment:
        for c in plan.comments:
            if c.comment_id == comment_id:
                c.status = CommentStatus.OPEN
                c.resolved_by = None
                c.resolved_at = None
                c.resolution_note = None
                return c
        raise PlanReviewError(f"Comment {comment_id} not found on plan {plan.ticket_key}.")

    def seal_plan(
        self,
        plan: PlanReviewArtifact,
        sealed_by: str,
        author_role: AuthorRole,
        override_ruling: Optional[str] = None,
    ) -> SealedPlanReceipt:
        # Negative Control 1: Models cannot self-seal plans or grant execution authority
        if author_role != AuthorRole.OPERATOR:
            raise PlanReviewError("models cannot self-seal or grant execution authority")

        # Negative Control 2: Unresolved blocking questions prevent plan sealing
        open_blocking = plan.unresolved_blocking_comments()
        if open_blocking and not override_ruling:
            raise PlanReviewError(
                f"cannot seal plan with unresolved blocking questions: "
                f"{len(open_blocking)} blocking question(s) remain open."
            )

        now = utc_now()
        current_hash = plan.current_revision.content_hash
        receipt_id = f"seal-{uuid.uuid4().hex[:12]}"

        plan.is_sealed = True
        plan.sealed_at = now
        plan.sealed_by = sealed_by
        plan.sealed_revision_hash = current_hash

        return SealedPlanReceipt(
            receipt_id=receipt_id,
            ticket_key=plan.ticket_key,
            revision_num=plan.current_revision_num,
            plan_content_hash=current_hash,
            sealed_by=sealed_by,
            sealed_at=now,
            override_ruling=override_ruling,
        )

    def validate_binding(
        self,
        receipt: SealedPlanReceipt,
        plan: PlanReviewArtifact,
    ) -> bool:
        """Verifies that an ExecutionPackage binding matches the plan's exact sealed state."""
        if not plan.is_sealed:
            return False
        if plan.sealed_revision_hash != receipt.plan_content_hash:
            return False
        if plan.current_revision.content_hash != receipt.plan_content_hash:
            return False
        return True
