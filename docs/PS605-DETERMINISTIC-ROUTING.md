# PS-605 — deterministic execution-target routing (first slice)

What this slice is: the production selection seam between an `ExecutionPackage` and
a pinned attempt. One pure function of canonical inputs produces either a selected
target or a typed refusal, and the decision is written down as a `DispatchDecision`
whose receipt form is PS-638's `DispatchDecisionReceipt` field-for-field.

```
ExecutionPackage -> requested domain/role/capabilities        (RoutingRequest)
  -> deterministic policy  (src.routing_domain_policy: privacy, allow/deny, ceiling)
  -> fresh qualified candidates (ExecutionTargetProfile + PS-632 receipt view)
  -> privacy / locality / inference / role / receipt / capability / exactness /
     tool / network / budget / resource filters  (13 ordered rules, ONE path)
  -> deterministically SELECTED target
  -> immutable DispatchDecision  -> to_ps638_receipt_kwargs()
  -> pinned attempt identity (decision.pin(), decision.attempt_binding())
```

## Why it cannot reroute itself

Nothing here dispatches. `select_target()` performs no I/O, makes no model call and
writes no state; the decision it returns is a frozen dataclass, and the module
exposes no dispatch/execute entry point. A runtime adapter therefore has no API to
call, and the identity it must use arrives as DATA (`pin()`), not as a decision it
makes. The test suite asserts this structurally, not as a comment.

## One filter path, so "fallback" cannot be a weaker standard

Every candidate goes through the same 13 ordered rules; the selected candidate is
the first ELIGIBLE one in a deterministic order (explicit preference, then
local-first when locality is required, then cost rank, then a stable tie-break).
`fallback_used` is therefore a statement about preference rank, and the receipt
records the rule that decided EVERY candidate:

| code | meaning |
| --- | --- |
| `policy_denied` | domain privacy / allow-deny / sensitivity ceiling |
| `privacy_local_only_no_eligible_target` | local-only work, no eligible local profile (fail closed) |
| `not_an_inference_target` | deterministic-only node asked to do inference |
| `exactness_unsatisfied` | approximate profile asked for exact/reference intent |
| `role_not_supported` | role not declared by the profile |
| `capability_receipt_missing` / `_profile_mismatch` / `_stale` / `_unhealthy` | no usable receipt for this exact profile |
| `capability_missing` | the receipt does not evidence a required capability |
| `tool_not_granted` / `network_policy_unsatisfied` | permission envelope |
| `budget_class_exceeded` | budget class or cost rank |
| `resource_unavailable` | a fact about now (GTT held, endpoint down) |

When every candidate fails for the same reason, that reason IS the refusal code;
when they fail differently, the code is `no_eligible_target` and the per-candidate
detail is attached. A refusal is never a substitute for an approved target.

## What the receipt carries (the ticket's list, field by field)

run/packet/ExecutionPackage identity; requested role and RESOLVED capabilities
(role shorthand ∪ explicit); `policy_ref` = `routing_policy@<version>+sha256:<hash>`
over the policy's content; `candidates_considered` with per-candidate eligibility,
rule, reason, receipt identity, receipt freshness and TTL, cost rank and preference
rank; `capability_receipt_refs` (content hashes); the selected target/host/model/
runtime kind+version/model digest/backend; granted tools and scopes and network
policy; `decided_by = ps605_policy`; a deterministic `reason` beginning with the
reason code; `decided_at`; and an optional `authority` block (PS-638 DR-01+DR-09,
added 2026-09-16). The decision-level facts PS-638 has no field for (fallback
rule, fallback_used, budget and resource facts) ride on the SELECTED candidate's
entry, so a sealed package still contains them.

**`authority` (optional, additive, PS-638 DR-01+DR-09).** A mapping recording
who/what asked and on whose credential — `requesting_principal`,
`delegating_principal`, `acting_principal`, `delegation_chain`, `credential_ref`,
`action`, `resource`, `grant_id`, `grant_expires_at`, `consent_id`,
`authority_schema_version`. It **records and audits; it does not enforce
authorization** — no code path reads it to permit or deny anything, its content is
never validated, and it must never be described as an access-control or security
boundary. When absent (the default, `None`), it is omitted from `core()` entirely,
so every decision/receipt sealed before this field existed keeps hashing exactly
as it always did (proven by `tests/test_ps638_authority_hash_stability.py`'s F1
falsification, including a negative control against a fixture frozen from the
landed baseline). When present, its contents are part of `receipt_hash` like any
other field. `policy_ref` and all 13 ordered filters are unaffected.

`receipt_hash` is computed with PS-638's own rule (sha256 over its `core()` fields,
in its order) by `ps638_receipt_hash()`, and `PS638_RECEIPT_FIELDS` /
`PS638_RECEIPT_CORE_FIELDS` are frozen here as the contract. When the PS-635 branch
lands, `make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())` is the
integration, and `decision.attempt_binding()["dispatch_receipt_hash"]` is what an
`AttemptReceipt` records to bind itself to this decision.

## Deliberately NOT in this slice

Cockpit/UI, landing, retry policy, budgets as accounting, the ledger, the evidence
package, verification, and anything about acceptance. PS-605 owns SELECTION; PS-635
owns the loop and PS-638 owns the envelope. Capability receipts are CONSUMED here,
never produced (that is PS-632).
