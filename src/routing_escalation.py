
"""
Premium escalation policy and the emergency break-glass override (pure logic).

Escalation needs all five conditions. The override is narrow, security-admin
approved, TTL-bounded and audited.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.routing_coordinator import (
    ExecutionBackend,
    Risk,
    SCHEMA_VERSION,
)

logger = logging.getLogger("odysseus.routing.escalation")

DEFAULT_EMERGENCY_TTL_MINUTES = 60


@dataclass
class EscalationSignal:
    """Objective signals that indicate unresolved risk."""
    tests_still_fail: bool = False
    safe_patching_failed: bool = False
    cheap_models_disagree: bool = False
    best_cheap_run_below_threshold: bool = False
    reviewer_requested_escalation: bool = False

    def any(self) -> bool:
        return any(
            [
                self.tests_still_fail,
                self.safe_patching_failed,
                self.cheap_models_disagree,
                self.best_cheap_run_below_threshold,
                self.reviewer_requested_escalation,
            ]
        )


@dataclass
class EscalationContext:
    task_id: str
    risk: Risk
    cheaper_attempts: int
    max_cheaper_attempts: int = 2
    signal: EscalationSignal = field(default_factory=EscalationSignal)
    est_premium_cost_usd: float = 0.0
    budget_remaining_usd: Optional[float] = None
    data_policy_allows_premium: bool = True
    approval_satisfied: bool = False


@dataclass
class EscalationVerdict:
    allowed: bool
    reasons: List[str]
    requires_approval: bool


def evaluate_escalation(ctx: EscalationContext) -> EscalationVerdict:
    """Premium escalation is allowed only when all conditions hold."""
    reasons: List[str] = []

    cheaper_attempts_exhausted = ctx.cheaper_attempts >= ctx.max_cheaper_attempts

    # High-risk, release-blocking, or unresolved after N cheap attempts.
    c1 = (
        ctx.risk in (Risk.HIGH, Risk.RELEASE_BLOCKING)
        or cheaper_attempts_exhausted
    )
    if not c1:
        reasons.append(
            "condition1_unmet: risk not high/blocking and cheaper_attempts"
            f"({ctx.cheaper_attempts}) < {ctx.max_cheaper_attempts}"
        )

    # At least one objective risk signal; exhausted cheap attempts don't count here.
    c2 = ctx.signal.any()
    if not c2:
        reasons.append("condition2_unmet: no unresolved-risk signal")

    # Cost within budget or manually approved.
    c3 = ctx.approval_satisfied
    if ctx.budget_remaining_usd is not None:
        if ctx.est_premium_cost_usd <= ctx.budget_remaining_usd:
            c3 = True
        else:
            c3 = ctx.approval_satisfied
    if not c3:
        reasons.append("condition3_unmet: premium cost over remaining budget and not approved")

    # Data policy allows the premium provider.
    c4 = ctx.data_policy_allows_premium
    if not c4:
        reasons.append("condition4_unmet: data policy forbids premium provider")

    # Required human approval satisfied.
    c5 = ctx.approval_satisfied
    if not c5:
        reasons.append("condition5_unmet: approval gate unsatisfied")

    allowed = all([c1, c2, c3, c4, c5])
    return EscalationVerdict(
        allowed=allowed,
        reasons=reasons,
        requires_approval=not ctx.approval_satisfied,
    )


@dataclass
class EmergencyOverride:
    requested_by: str
    approved_by: str
    reason: str
    expires_at: datetime
    forced_backend: ExecutionBackend = ExecutionBackend.HUMAN_ONLY_EMERGENCY
    post_mortem_required: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active: bool = True

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def expired_or_inactive(self, now: Optional[datetime] = None) -> bool:
        return (not self.active) or self.is_expired(now)

    def to_dict(self) -> dict:
        return {
            "emergencyOverride": True,
            "requestedBy": self.requested_by,
            "approvedBy": self.approved_by,
            "reason": self.reason,
            "expiresAt": self.expires_at.isoformat(),
            "forcedBackend": self.forced_backend.value,
            "postMortemRequired": self.post_mortem_required,
            "createdAt": self.created_at.isoformat(),
            "active": self.active,
        }


def build_emergency_override(
    requested_by: str,
    approved_by: str,
    reason: str,
    *,
    ttl_minutes: int = DEFAULT_EMERGENCY_TTL_MINUTES,
    now: Optional[datetime] = None,
    forced_backend: ExecutionBackend = ExecutionBackend.HUMAN_ONLY_EMERGENCY,
) -> EmergencyOverride:
    """
    Build an emergency override; the caller must verify `approved_by` is a
    security_admin. TTL is capped at DEFAULT_EMERGENCY_TTL_MINUTES.
    """
    now = now or datetime.now(timezone.utc)
    ttl = max(1, min(int(ttl_minutes), DEFAULT_EMERGENCY_TTL_MINUTES))
    expires_at = now + timedelta(minutes=ttl)
    return EmergencyOverride(
        requested_by=requested_by,
        approved_by=approved_by,
        reason=reason,
        expires_at=expires_at,
        forced_backend=forced_backend,
        post_mortem_required=True,
    )
