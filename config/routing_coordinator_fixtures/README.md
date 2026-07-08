# Coordinator benchmark fixtures (v0.5 Model Routing Harness, spec Phase 8)

Model-agnostic scenarios replayed against a candidate resident coordinator by
`src/routing_benchmark.py`. Each fixture is `{id, dimension, task, expected}`:

- `task` — an OdysseusTask-shaped input handed verbatim to the coordinator model
  (`CoordinatorClient.decide`). It carries no canned decision; the decision is
  whatever the model produces (or, in tests, a stub).
- `expected` — the correct classification/route for scoring: `domain`,
  `taskType`, `risk`, `dataSensitivity`, `verificationMode`, `backend`,
  `approvalRequired`, `gate_expectation` (`allowed`|`blocked`, documentary),
  optional `acceptableRoles` / `acceptableBackends` (tolerant arbitration match),
  optional `maxConfidenceForUncertain` (marks a fixture as an ambiguity test).

The engine is LLM-free/injectable, so this suite is scored identically whether
the decisions come from a live endpoint or a canned test stub.

## Dimension coverage (>= 2 fixtures each)

| dimension | fixtures |
|---|---|
| schema_validity | schema-01-bugfix, schema-02-docs |
| domain_classification | domain-01-swe, domain-02-tacticus, domain-03-infra |
| task_type_classification | tasktype-01-diffreview, tasktype-02-release |
| risk_classification | risk-01-low, risk-02-releaseblocking |
| data_sensitivity_classification | sensitivity-01-public, sensitivity-02-confidential |
| verification_mode_selection | verifmode-01-refactor, verifmode-02-analysis |
| backend_routing | backend-01-swe-local, backend-02-tacticus-absis |
| policy_gate_compliance | policygate-01-restricted-remote-trap, policygate-02-secret-local-only |
| approval_gate | approval-01-release-admin, approval-02-security-fix |
| arbitration | arbitration-01-tacticus-absis, arbitration-02-implementer-lead |
| uncertainty_handling | uncertainty-01-underspecified, uncertainty-02-ambiguous-domain |
| consistency | consistency-01-stable-bug, consistency-02-stable-review (measured across ALL fixtures) |

## Hard cases deliberately included

- **restricted/secret data must never route remote** — `policygate-*` carry
  `restricted`/`secret` sensitivity; a decision that recommends a remote backend
  must be blocked and diverted (the FINAL route must be legal).
- **ambiguous/underspecified tasks** — `uncertainty-*` are deliberately vague;
  safe behavior is low confidence, a scout-led chain, approval, or a safe_scout
  fallback.
- **tacticus_analytics -> absis backend intent** — `domain-02`, `backend-02`,
  `arbitration-01` expect the `absis_tacticus_job_queue` backend.
