# PS-605 — production dispatch consumes the routing authority

The selector was already deterministic. This slice is about the other half: the
**real dispatcher** (`src.routing_executor.execute_candidates`, the function
`scripts/odysseus-run` calls) now takes its order and its membership from a PS-605
decision, refuses to call any model when PS-605 refuses, and refuses an invocation
whose resolved model or endpoint contradicts the decision's pin **before** the
network call. The evidence below is from real dispatches against the RTX4500 node,
not fixtures.

## Code

| Piece | What it does |
| --- | --- |
| `src/dispatch_boundary.py` | projects DB rows into PS-605 profiles + capability receipts (with provenance), resolves the decision, returns the execution ORDER, verifies the pin, records invocations, seals/validates evidence |
| `src/routing_executor.py` | resolves the decision before the run manifest; iterates the decision's eligible candidates only; pin-checks each resolved endpoint before `llm_call_with_usage`; writes `dispatch_receipt.json` (decision + PS-638 receipt fields + attempt bindings + invocation log) |
| `src/dispatch_routing.py` | gained `reviewer`/`debugger`/`escalation`/`implementer`/`scout` roles, receipt `provenance`, and the receipt-exactness rule |
| `scripts/ps605-domain-controls.py` | the two live domain controls: probe → seed → real dispatch → seal → validate → mutate (`--verify-only` re-checks the sealed files) |

**No post-decision chooser.** The loop receives `bound.execution_order(candidates)`;
a candidate PS-605 refused is never offered to it. Runtime adapters report facts and
failures and cannot reroute themselves: the only identity they receive is the pin.

**Provenance is labelled, never assumed.** A receipt is `measured` (the probe in this
experiment), `detected` (an endpoint fact the system actually probed, e.g.
`ModelEndpoint.supports_tools`), or `declared` (what the row says about itself).
`NULL supports_tools` grants no tool capability, and a request may require
capabilities no declared receipt can satisfy (context integrity) — which refuses
instead of proceeding.

**Fail-closed refusals.** A refusal stops the run before any model call and writes
`dispatch_refusal.json` with per-candidate reasons; the run result keeps the same
shape as a run that dispatched so a caller never has to guess.

## Live controls (single run, no errors in the log)

Target: `http://127.0.1.1:11434` (ssh tunnel to the minipc node; loopback on this
host, so it is a LOCAL target) — ollama `0.32.11`, model `qwen3.8:27b`, digest
`d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28`.

Measured receipt (provenance `measured`): `/api/version` + `/api/tags` + `/api/ps`
(served context **131072** at probe time) + a plain completion (`"ready"`) + a tool
call (`write_file` returned) → `{text_generation, single_tool_call,
exact_reference_semantics, context_integrity}`, receipt hash
`72052d90c0b4898490c16140e5fdaff44d870a470d94d78f4d52fa3867da06eb`. Note the
capability set is whatever the probe OBSERVED: an earlier probe on the same node read
`served_context: 0` (nothing resident) and therefore claimed no context integrity at
all — silence is not a capability.

| Control | Decision | Real dispatch | Evidence |
| --- | --- | --- | --- |
| **A1** dev/internal, `preferred_profile_ids=(p-rtx,)` | `p-rtx` eligible, `p-hosted` eligible, `p-msr1` `not_an_inference_target`; selected `profile:p-rtx` | **executed**: 1 attempt, 1 invocation (local, `ok`), status `succeeded` | seal `b7957994…`, receipt `25408b19…`, decision `24b9ec23…`, validated, 0 codes |
| **A2** same estate, local receipt STALE (2 days old, ttl 60s) | `p-rtx` `capability_receipt_stale`, `p-hosted` eligible → **selected `profile:p-hosted`**, `reason_code=selected_fallback_candidate`, `fallback_used=true` | **not performed**: no credentialed hosted endpoint is available to this session | seal `c3491a5a…`, receipt `6b694928…`, validated, 0 codes, `invocations: []` |
| **B1** sensitive/local-only (synthetic fixture, `policy_domain=health`, `data_sensitivity=restricted`) | `p-hosted` **`policy_denied`**, `p-msr1` `not_an_inference_target`, `p-rtx` eligible → selected `profile:p-rtx` | **executed**: 1 attempt, 1 invocation (local), **0 hosted invocations**, status `succeeded` | seal `6b683225…`, receipt `7aad9293…`, validated, 0 codes |
| **B2** same task, hosted candidate only | refusal `privacy_local_only_no_eligible_target` (`p-hosted` `policy_denied`) | **none**: zero invocations, no dispatch evidence sealed | `B2-sensitive-refusal.json`, seal `3e400b5a…` |

Policy revision in every artifact: `routing_policy@1.2+sha256:4df270ad9543772d`.

### Mutation controls, run against the sealed FILES (`--verify-only`, exit 0)

`all_valid: true`, `failures: []`. Per artifact, `applicable` mutations are rejected
**and** the semantic check fires (not just the seal hash):

| Mutation | A1 | A2 (no attempt) | B1 |
| --- | --- | --- | --- |
| selected target changed | `pin_changed_after_sealing`, `decision_receipt_hash_mismatch`, `attempt_target_mismatch`, `evidence_hash_mismatch` | `pin_changed…`, `decision_receipt_hash_mismatch` | same as A1 |
| decision pin changed | `pin_changed_after_sealing` | `pin_changed_after_sealing` | `pin_changed_after_sealing` |
| capability receipt changed | `capability_receipt_changed` | `capability_receipt_changed` | `capability_receipt_changed` |
| policy revision changed | `policy_revision_changed` | `policy_revision_changed` | `policy_revision_changed` |
| attempt rebound to another receipt | `attempt_receipt_unbound` | *not applicable* (no attempt) | `attempt_receipt_unbound` |
| hosted invocation added | `invocation_outside_decision` | `invocation_outside_decision` | `invocation_outside_decision` **+ `hosted_invocation_for_local_only`** |

Pin guards, executed live against a real bound decision: a different model →
`pin_model_mismatch`; a hosted URL for a local pin → `pin_locality_mismatch`. Both
refused before dispatch.

## Test + suite state

* New: `tests/test_dispatch_boundary.py` (22), `tests/test_dispatch_wiring.py` (5 —
  the production entrypoint: eligible-only iteration, decision order, zero-call
  refusal, pin refusal before the network, receipt binding, seal round-trip).
* Existing PS-605: `tests/test_dispatch_routing.py` (21),
  `tests/test_routing_domain_policy.py` (12).
* Full suite on this head: **3569 passed, 3 skipped, 0 failed**
  (`tests/test_gpu_compose_standalone.py` excluded: its 3 failures are exact-base
  proven at `def0119b` and fixed on the PS-635 branch, not re-fixed here).

## Honest limits

1. **The hosted leg was never invoked.** This session cannot read or use hosted
   credentials, so A2's fallback is proven at the decision level (selection, order,
   per-candidate reasons) and the artifact records `invocations: []` with the reason.
   Nothing here should be read as a hosted dispatch having happened.
2. **Capability sets are probe-dependent, and that is the point.** The clean run's
   probe read `served_context: 131072` (model resident) and therefore claimed context
   integrity; an earlier probe on the same node read `0` and claimed nothing. A
   request requiring a capability no receipt evidences refuses rather than proceeding
   (tested hermetically).
3. **Declared receipts still gate non-tool work.** Production profiles without a
   measured-store entry route on `declared`/`detected` provenance: no weaker than the
   previous metadata-only filter, but not PS-632's measured estate.
4. **The harness does not consume the selector yet.** `scripts/ps635-live/live_run.py`
   still pins by hand; wiring it is the follow-up slice, deliberately outside this one.
5. A stated preference (`preferred_profile_ids`) is available as an explicit policy
   input — not the default; A1 uses it deliberately. `fallback_used` is true only
   when a stated preference was unavailable (A2).

## Reproduce

```bash
cd <ps-605 worktree>
DATABASE_URL=sqlite:///$(pwd)/data/ps605-live/app.db \
  python3 scripts/ps605-domain-controls.py --out data/ps605-live \
    --base-url http://127.0.1.1:11434
python3 scripts/ps605-domain-controls.py --out data/ps605-live --verify-only
```

