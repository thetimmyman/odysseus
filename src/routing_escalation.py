
"""
routing_escalation.py — Escalation policy (Section 11) and Emergency override /
break-glass path (Section 14) for the v0.5 Model Routing Harness.

Premium escalation is allowed ONLY when all five conditions in Section 11 hold.
The emergency override is deliberately narrow, security-admin approved, TTL-
bounded, and fully audited. This module is pure policy logic; persistence and
HTTP concerns live in routes/routing_harness_routes.py and core/database.py.
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
    """Objective signals that indicate unresolved risk (Section 11.2)."""
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
    """Section 11: premium escalation allowed only when ALL conditions hold."""
    reasons: List[str] = []

    cheaper_attempts_exhausted = ctx.cheaper_attempts >= ctx.max_cheaper_attempts

    # Condition 1: high-risk / release-blocking / unresolved after N cheap attempts.
    c1 = (
        ctx.risk in (Risk.HIGH, Risk.RELEASE_BLOCKING)
        or cheaper_attempts_exhausted
    )
    if not c1:
        reasons.append(
            "condition1_unmet: risk not high/blocking and cheaper_attempts"
            f"({ctx.cheaper_attempts}) < {ctx.max_cheaper_attempts}"
        )

    # Condition 2: at least one objective unresolved-risk signal (Section 11.2:
    # tests still fail, safe patching failed, cheap models disagree on root
    # cause, best cheap run below threshold, or reviewer requests escalation).
    # NOTE: exhausting the configured cheaper attempts satisfies Section 11.1's
    # condition 1, but is NOT one of the §11.2 objective signals -- the gates
    # are independent.
    c2 = ctx.signal.any()
    if not c2:
        reasons.append("condition2_unmet: no unresolved-risk signal")

    # Condition 3: estimated premium cost within budget or manually approved.
    c3 = ctx.approval_satisfied
    if ctx.budget_remaining_usd is not None:
        if ctx.est_premium_cost_usd <= ctx.budget_remaining_usd:
            c3 = True
        else:
            c3 = ctx.approval_satisfied
    if not c3:
        reasons.append("condition3_unmet: premium cost over remaining budget and not approved")

    # Condition 4: data policy allows the selected premium provider.
    c4 = ctx.data_policy_allows_premium
    if not c4:
        reasons.append("condition4_unmet: data policy forbids premium provider")

    # Condition 5: required human approval gate satisfied.
    c5 = ctx.approval_satisfied
    if not c5:
        reasons.append("condition5_unmet: approval gate unsatisfied")

    allowed = all([c1, c2, c3, c4, c5])
    return EscalationVerdict(
        allowed=allowed,
        reasons=reasons,
        requires_approval=not ctx.approval_satisfied,
    )


# --- Emergency override / break-glass (Section 14) ---
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
    Build an emergency override. The caller MUST have verified that
    `approved_by` holds the security_admin role before persisting/activating.
    TTL is capped at DEFAULT_EMERGENCY_TTL_MINUTES by policy.
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
