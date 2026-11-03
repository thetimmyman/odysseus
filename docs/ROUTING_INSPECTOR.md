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

`apply_operator_override(explanation, target, ...)` operates at the authorization
boundary (`src/override_authorization.py`). It enforces the core invariant:
*The gate must never read an authorization fact from an object the caller can construct.
Evidence must be references the gate resolves — never content the gate believes.*

To steer execution to an alternative candidate, the caller supplies `OverrideEvidenceRefs`
(or compatible canonical references) rather than raw profile or receipt content. An injected
`OverrideEvidenceResolver` resolves authoritative profiles from the capability store,
receipts, live capacity grants from `CapacityRegistry`, request packets, resources, and
authoritative domain policy.

The gate enforces reproduction rather than replacement:
1. **Explanation Authenticity:** The cited `RoutingExplanation` must be hash-sealed and untampered.
2. **Recorded Eligibility:** The requested target must already have been eligible in the cited
   explanation; an override can never convert a policy DENY into an ALLOW.
3. **Reproduction of Facts:** A fresh evaluation of the resolved inputs must reproduce the
   canonical per-row facts (`provider`, `locality`, `endpoint_url`, `receipt_hash`,
   `capacity_receipt_refs`).
4. **Current Eligibility:** A fresh evaluation at the authoritative override instant (`now`,
   bound to server clock within strict skew tolerances) must confirm the requested candidate
   remains eligible right now. The fresh evaluation cannot qualify any candidate that was not
   eligible at decision time.
5. **Bounded Validity & Keyed Authentication:** The resulting `OverrideAuditReceipt` is
   privately minted with a keyed HMAC signature. Its validity lifetime is bounded:
   `valid_until = min(capability_expiry, capacity_expiry, minted_at + 300s)`. A receipt
   cannot outlive its underlying evidence.

`verify_override_receipt(receipt, explanation, ...)` checks the HMAC signature, structural
hash, temporal expiration, and optionally re-derives the full evaluation against the resolver.

This increment authorizes nothing downstream: no dispatch, no worker control, no capability
or policy change, no evidence refresh, and no pinned attempt. OD-19 still does not accept
observed interventions, so an override receipt here must not be reported as measured attention
cost or a completed override, and there is no route, store, migration or default activation for it.
