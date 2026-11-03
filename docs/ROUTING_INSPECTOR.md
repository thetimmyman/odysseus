# Retained routing inspection and operator requests

The existing owner-cookie route loads an exact retained attempt, joins immutable
package/dispatch/supplement hashes, and calls `project_retained_routing_inspection`.
The expanded response discloses the recorded selected model/runtime/profile,
selection reason, policy and tool/network/scope envelope. Alternative refusal
codes are retained. Capability observations and capacity/quota/reset/price facts
keep their canonical times; account identity and collector/storage provenance
remain excluded. A successful fetch never refreshes a historical observation.

`POST /api/harness/inspector/attempts/{handle}/escalation` accepts only an exact
`dispatch_receipt_hash`, bounded `reason`, and `idempotency_key`. A real owner
cookie, a current unrevoked association, and a successful retained inspection are
required. Unknown/cross-owner/revoked handles share the existing refusal. The
owning store appends a hash-bound `operator-intervention-request-v1` receipt with
authenticated actor/epoch, original attempt/package/dispatch, reason and time.
Retries of the same intent return the same receipt; conflicting reuse refuses.
SQLite update/delete/replace triggers preserve the record. This is an immutable
**REQUESTED** escalation, with no resulting dispatch. It does not stop a worker,
grant capability, change policy, refresh evidence, or claim semantic acceptance.

This slice reuses the existing feature metadata and SQLite store. A new
`canonical_inspector_intervention` table is declared in that separate metadata;
imports do not create it. There is no production migration or default service
activation. Existing installations must retain unavailable behavior until the
execution owner qualifies admission, storage/schema backup/restore and the live
adapter. The fixed synthetic pilot remains fixed; it is not a real TMOS worker.
There is no arbitrary receipt registration, path import, backfill or default owner.

An eligible alternate still requires fresh selector evaluation, an immutable
operator-to-new-dispatch association and an actual pinned attempt. That control
is unavailable in this increment. OD-19 currently does not accept observed
interventions; pending requests here must not be reported as measured attention
cost or completed overrides. No new routing, billing or scheduling authority is
introduced.

Tests use isolated file SQLite, real owner checks and synthetic retained evidence.
They establish source behavior, including restart readback, tamper refusal,
request idempotency and unchanged stale-capacity facts. They do not qualify a
production filesystem, live provider, installed transport or the TMOS readiness
pilot. Rollback disables the entry point/service while retaining every canonical
receipt and association; do not delete the audit table or repoint handles.

## Deterministic explanation and audited eligible-target steering

`src/routing_explanation.py` supplies the fresh selector evaluation the section
above reserved, as a pure module with no route and no store. It adds no rules:
`RoutingInspectorEvaluator` calls `dispatch_routing.select_target` — the single
ordered filter path — and projects that decision into a sealed
`RoutingExplanation`. Each alternative carries the selector's own deciding rule
code, its own prose, the canonical capability evidence identity, the entitlement
outcome, and a deterministic `required_change_to_qualify`. The rationale is
therefore a projection of canonical receipts: the module imports no model client
and no network library, so a post-hoc rationale has nowhere to come from. Given
the same evidence and instant, two evaluations hash identically. Passing
`decision_id` pins an explanation to a decision actually taken, so its
`dispatch_receipt_hash` is comparable with the retained receipt.

`apply_operator_override(explanation, target)` steers only between candidates
that are already eligible. It re-derives the domain policy (provider allow and
deny lists, sensitivity ceiling, local-only), evidence presence, health,
capability freshness, credential binding, and entitlement including the
expiry of the capacity facts behind it, against the override instant
rather than trusting the recorded `eligible` flag, so a hash-sealed receipt that
claims eligibility for a target failing a hard rule is still refused. The
resolved `DomainPolicy` is recorded in the explanation as data and re-run
through the canonical `evaluate_route`, so a provider the domain denies stays
denied even when the request itself does not set local-only. Every
ineligibility raises `PolicyViolationException` with its typed refusal codes and
mints nothing; expired capability evidence is never restamped into freshness
and a policy DENY is never converted. An accepted steering mints an immutable
`OverrideAuditReceipt` (schema 1) recording operator, justification, instant,
original and authorized target/profile, explanation/decision/dispatch hashes,
policy ref, request fingerprint, the closed eligible set, `original_still_eligible`
and evidence refs. `authorizes` is a fixed single-element vocabulary and `scope`
is fixed, so the receipt cannot be read as a dispatch, a capability grant, a
policy change or a refresh. Re-issuing the same intent yields the same id and
hash.

This increment still authorizes nothing downstream: no dispatch, no worker
control, no capability or policy change, no evidence refresh and no pinned
attempt. OD-19 still does not accept observed interventions, so an override
receipt here must not be reported as measured attention cost or a completed
override, and there is no route, store, migration or default activation for it.
Tests cover the positive control, determinism, refusal codes for filtered
alternates, and the negative controls above at both the evaluator and the
override boundary, including resealed tampered explanations. Capacity
freshness is re-checked from the recorded fact expiry, so a grant whose evidence
has lapsed (or whose expiry was never recorded) cannot be overridden; detecting
a revocation that leaves the facts themselves fresh still needs a new capacity
collection and remains a route-layer concern. Because the
hard/soft refusal taxonomy is descriptive, a further control proves a
curably-classified refusal still blocks an override, so the DENY guarantee rests
on the deciding rule the gate re-derives rather than on the classification.
