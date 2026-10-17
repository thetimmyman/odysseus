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
