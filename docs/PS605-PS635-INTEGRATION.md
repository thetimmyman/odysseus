# PS-605 × PS-635 — the integration slice

Branch: **`work/ps635-ps605-integration`**, created from the PS-635 lineage
(`f96ebc20`) with PS-605 (`aef0e0d9`) merged in. Both source worktrees are untouched.

## 1. Reconciliation (measured)

| | |
| --- | --- |
| PS-635 lineage | `work/ps-635-loop-demo @ f96ebc20` (49 files changed vs the base) |
| PS-605 | `work/ps-605-domain-policy @ aef0e0d9` (12 files) |
| common ancestor | `def0119b` |
| file overlap | **0 files** — the merge is textually clean, which is why the work below is semantic |
| dirty state | both clean before the merge |
| PS-638 types on both sides | PS-635: `ExecutionPackage`, `DispatchDecisionReceipt`, `AttemptReceipt`, `VerificationReceipt`, `EvidencePackage`; PS-605: the receipt kwargs + hash contract it must feed |

Merge commit: `Merge branch 'work/ps-605-domain-policy' into
work/ps635-ps605-integration` — no squashing, so the PS-605 evidence commits keep
their provenance.

## 2. What the integration changed

| Change | Why |
| --- | --- |
| `make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())` is now the ONLY way a dispatch receipt is built in the harness | PS-605 selects; PS-638 owns the receipt |
| `scripts/ps635-live/live_run.py` routes through `local_target_routing.resolve_fleet_dispatch` | the harness no longer hand-pins; `--target` is a preference submitted through the boundary |
| `dispatch_boundary.resolve_from_estate` | a registry-backed caller does not fabricate a `RoutingTask` row to satisfy the DB path |
| `dispatch_boundary` canonicalization aligned to `ensure_ascii=False` | PS-605's hash and PS-638's `receipt_hash` must be byte-identical; the cross-lineage test found the difference |
| `routing_engine.{endpoint_is_local,sensitivity_requires_local_only,roles_for_task_type}` | the boundary's private cross-package imports become a narrow public contract — one owner per rule |
| `CAP_STREAMING` adopts the registry's string | one vocabulary for one property instead of three near-synonyms |

Harness guards, all fail-closed before any model call:

* `preference_violation` — PS-605 selected a node the operator did not name → REFUSAL;
* `pin_matches_client` — the runtime client's host/model must equal the decision's pin;
* `verify_invocation` — the resolved endpoint's locality must match the pinned locality;
* `dispatch_chain.json` — package hash → dispatch receipt hash → every attempt's
  `dispatch_receipt_hash` → the target that actually ran, checked before sealing.

## 3. The cross-lineage contract (`tests/test_ps638_dispatch_contract.py`)

`make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())` — asserted, not assumed:

* the kwargs are EXACTLY PS-638's `DispatchDecisionReceipt` fields (that builder
  rejects unknown fields, so a drifting contract fails loudly);
* `ps638_receipt_hash(kwargs) == receipt.receipt_hash`, and the same value is
  re-derived from the SEALED dict;
* `decided_by == "ps605_policy"` with a non-empty `policy_ref`, and the receipt binds
  the REAL `ExecutionPackage` hash, packet id and run id;
* the attempt chain: `attempt.dispatch_receipt_hash == dispatch.receipt_hash`,
  `dispatch.execution_package_hash == ep.package_hash`, and the attempt's host/model
  equal the decision's pin;
* a REBOUND attempt is detectably unbound even though both records are individually
  valid — the binding is checked, not assumed;
* changing the selected target, model, host, policy revision or network policy after
  sealing changes the hash.

## 4. Refusal evidence (disposition)

**The frozen contract cannot represent a refusal.** `DispatchDecisionReceipt` requires
non-empty `selected_target_id`, `selected_host` and `selected_model`
(`__post_init__`), and a refusal has none; `test_ps638_dispatch_contract.py` asserts
that builder refuses empty selections, so the gap is pinned rather than papered over.
Filling those fields with placeholders would be a lie dressed as a receipt.

A refusal is therefore recorded in the two authorities that already exist:

1. **the ledger** (`src/execution_ledger.py`, the canonical run-state record): a `run`
   entry with an EMPTY execution identity (naming a target would misattribute the
   refusal) plus a `decision=blocked` entry carrying the typed refusal code;
2. **`dispatch_refusal.json`** (harness, sealed by `seal_dispatch_refusal`): the PS-605
   refusal verbatim — code, reason, per-candidate rules — bound to the SAME
   `execution_package_hash`, content-addressed over PS-638's own canonicalization, and
   with `attempt_receipts: []` because **no AttemptReceipt exists when no attempt
   happened**. No EvidencePackage is sealed, matching the harness's existing rule
   ("a package with nothing to verify is not sealed, and saying so IS the evidence").

Mutation controls: changing the package hash, packet id, run id or the refusal object,
or adding an attempted receipt, all break `refusal_hash`
(`tests/test_ps605_ps632_seam.py`).

**Contract gap to close in PS-638 (not invented here):** a canonical
`DispatchRefusalReceipt` — or a nullable-selected variant of
`DispatchDecisionReceipt` — with the same `core()`-sealed shape, so a refusal lives
inside the PS-638 evidence envelope instead of beside it. Until that exists the ledger
plus the sealed refusal file are the canonical pair, and neither is a second
authority: they record, they do not decide.

## 5. PS-632 (measured capability) status

**A canonical implementation already exists on this lineage: `src/local_targets.py`** —
the registry (`LocalTargetSpec`, `registered_targets()`), the measured record
(`LocalTargetCapability` with `proven_capabilities()`, `health`, `last_probe`,
`served_context`, `native_tools ∈ {True, False, None}`), and `fleet_snapshot()` as
"the artifact PS-632 asks for". There is **no** separate capability-receipt registry on
`work/ps-632-local-target-registry`, and none was invented here.

The registry is now CONSUMED rather than duplicated: `src/local_target_routing.py`
translates records into PS-605 profiles + receipts (`provenance="measured"`,
`observed_at=last_probe`, profile-specific), maps the packet's requirement names
(`native_tools`, `readonly_analysis`, `streaming`) onto PS-605 capabilities
fail-closed, and derives roles from PROVEN capability only — so a node that merely
*declares* tools cannot be handed implementer work, and a tool-less healthy node is
still usable for read-only analysis.

Three things the integrated path needs from PS-632 that PS-632 does not yet expose:

1. **the model digest on the record.** It exists only inside
   `LocalTargetCapability.evidence["model"]["digest"]`; the receipt carries it today by
   reaching into `evidence`, which is a coupling PS-632 should own.
2. **a freshness policy, not just a timestamp.** `last_probe` exists, but the TTL ("how
   old is too old") is currently supplied by the consumer
   (`local_target_routing.DEFAULT_RECEIPT_TTL_S = 3600`). PS-632 should name the TTL
   per target class, because it is a property of the measurement.
3. **a receipt store keyed by (target, model).** The PS-605 boundary can consume
   measured receipts from a store (`PS605_RECEIPT_STORE`), but nothing in the
   repository persists them, so each dispatch re-probes.

Those three are the concrete PS-632 implementation gap. None of them is a parallel
registry.

## 6. `dispatch_boundary.py` ownership check

`src/dispatch_boundary.py` is a **boundary / sealing / invocation-integrity** layer:
profile+receipt projection, decision binding, the execution ORDER, the pre-network pin
guard, the invocation recorder, attempt bindings, and evidence sealing/validation.

It is deliberately NOT:

* a routing policy engine — the 13 ordered filters and the policy snapshot live in
  `src/dispatch_routing.py` + `src/routing_domain_policy.py`;
* a scheduler — no queue, no concurrency, no timing, no retry policy;
* a capability registry — it consumes receipts and never probes; measuring is
  `src/local_targets.py`;
* an evidence ledger — `src/execution_ledger.py` and the PS-638 envelope own run state
  and evidence;
* a lifecycle authority — it never accepts, lands or escalates; `fallback` is
  selection among independently eligible candidates, which the decision records.

Its cross-package surface is now the narrow public contract in §2: no private
`routing_engine` names remain in the boundary.

## 7. The controlled real local execution

One run, `l1-interface`, on the qualified RTX4500 Qwen profile through the integrated
production path (`--target local-rtx4500` as a PREFERENCE; `num_ctx 32768`):

| Link | Value |
| --- | --- |
| measured fleet | `local-rtx4500`: healthy, `native_tools=True` (proven by a real tool call), `served_context=32768`, `last_probe=2026-09-15T04:05…` |
| PS-605 decision | selected `local-rtx4500`, reason `selected_local_only_candidate`, provenance `measured`, policy `routing_policy@1.2+sha256:4df270ad9543772d` |
| DispatchDecisionReceipt | `decided_by=ps605_policy` (NOT `explicit_pin`), host `minipc`, model `qwen3.8:27b`, runtime `0.32.11`, digest `d94d964641c7…`, `receipt_hash=6e387097cae05802…` |
| AttemptReceipt | target `local-rtx4500`, host `minipc`, model `qwen3.8:27b`, runtime `0.32.11`, `dispatch_receipt_hash=6e387097cae05802…`, `execution_package_hash=eedc5ee2885376d2…` |
| chain check | `dispatch_chain.json`: `ok=true`, `attempts_bound=true`, `attempts_on_the_pinned_target=true`, `receipt_target_matches_pin=true` |
| ExecutionPackage | `eedc5ee2885376d2b8856d0d62a08114e5ea0b953ab704e99dec828c2dcc1ebc` |
| EvidencePackage | `6423f109b6e66f2e…`, validator **VERIFIED**, 0 reasons |
| verification | attempt 1: exit 0 → **PASS** (8 passed, 0 failed) |
| terminal result | `ACCEPTED_CANDIDATE`, 1 attempt, **1 model call**, no advisor records |
| ledger | `run` → `attempt` → `verification` → `decision`; chain valid; no `acceptance` entry |

G1/G2 invariants, re-checked on this run and by the suites (328 focused tests +
3924 in the full suite):

* verifier PASS → `ACCEPTED_CANDIDATE` (never `ACCEPTED`; the ledger has no
  `acceptance` entry, so no acceptance authority was exercised);
* PASS → zero planner/advisor calls (`manager: null`, one model call, which is the
  worker's);
* `advise=None` preserves G1 behaviour (the l1-interface case runs with no advisor);
* at most one bounded proposal on eligible no-progress/repeated failure, and no
  manager/replanner acceptance or routing authority
  (`tests/test_replanner_ps635.py`, the ledger's `_reject_unrepresentable`);
* no landing authority and no manufactured repair case: this run is one-shot, the
  repair path was never entered, and repair uplift stays **UNOBSERVED**.

**A hosted invocation remains UNOBSERVED** — there is no hosted leg in this path and
none was manufactured.



