# Handoff — PS-605 deterministic routing, from the PS-635 G2 seam

Written after the G2 manager/replanner seam was proven (see
`docs/local-targets/PS635-G2-REPLANNER.md`). This is a **scope boundary**, not a
plan for PS-605: it lists what PS-635 now provides, what PS-605 owns, and what
would be a mistake to bolt onto either.

## What PS-635/PS-638 already provide (do not rebuild these)

| primitive | what it is | where |
| --- | --- | --- |
| `WorkPacket` | immutable, fail-closed validated unit of work (interface required) | `src/work_packet.py` |
| fresh worker context | bounded render of objective/scope/interface/contract/criteria | `src/worker_context.py` |
| compact repair packet | deterministic failure evidence, contract/interface UNCHANGED | `src/repair_packet.py` |
| bounded loop | dispatch → deterministic verify → repair → bounded retry → escalate | `src/local_worker_loop.py` |
| canonical state | append-only, hash-chained run/attempt/verification/repair/decision/**proposal** entries | `src/execution_ledger.py` |
| execution envelope | `ExecutionPackage` + `DispatchDecisionReceipt` + receipts + `EvidencePackage` + validator | `src/execution_package.py`, `src/attempt_receipt.py`, `src/evidence_contract.py`, `src/evidence_package.py` |
| advisory manager | typed, schema-validated, deterministically gated proposal; advisory only | `src/replanner.py` |
| live harness | one command that runs a case, seals the package and validates it | `scripts/ps635-live/live_run.py` |

## The exact hole PS-605 fills

Every live run so far records:

```
decided_by: "explicit_pin"
reason:     "operator pinned local-rtx4500; PS-605 policy routing is not wired for
             this packet, so no policy reference is claimed"
```

That is honest and it is a hole. `DispatchDecisionReceipt` has the fields for a
real decision — policy/config revision and hash, candidates considered with
allow/refuse reasons, capability-receipt versions and freshness, budget/quota
state, the fallback rule if one was used, the granted tool/network/permission
envelope, a deterministic reason code and `observed_at` — and PS-605 is what makes
them true instead of empty.

## What PS-605 should expose to the loop/harness

One deterministic function of canonical inputs, in the shape the harness can call
where it currently pins:

* **inputs**: the `ExecutionPackage` (domain, role, required capabilities, allowed
  tools/network, budgets, privacy class), the current policy/config revision and
  hash, the candidate `ExecutionTarget` profiles with their PS-632 capability
  receipts and freshness, and the observed resource/budget state.
* **output**: either a selected profile plus the granted envelope, or a **typed
  refusal**. There is no third outcome and no silent substitution.
* **side effect**: nothing. Routing decides; it does not dispatch, retry, or
  mutate lifecycle state.

## Constraints these pieces already enforce, and that routing must not undo

1. **No model may select or substitute itself.** A model cannot request routing
   either: in the G2 gate, any `route`/`target`/`provider`/`model`/`runtime`
   attempt in a manager proposal is refused with `routing_refused`, and a
   `target_id` change is refused the same way. Routing stays a control-plane
   decision.
2. **Fallback may not escape the originating domain's policy.** A sensitive packet
   with no allowed local profile must refuse; "no allowed target" is an answer, not
   a reason to relax the domain.
3. **Capability is per PROFILE, not per host.** A runtime/model/backend/
   speculation/parser/approximation change can invalidate capability while the
   physical host is unchanged. Route on PS-632 receipts, never on host names.
4. **MS-R1 is never an inference fallback.** It is deterministic
   governance_ci/verification only.
5. **A refusal is typed.** `packet_invalid`, `context`, `runtime_provider` and
   `technical` are already separate failure classes in the ledger; routing adds its
   own policy-refusal reason codes and must not collapse them into "failed".

## What PS-605 should NOT do

* Do not re-implement lifecycle, retry policy, the ledger, the evidence envelope
  or the manager gate. In particular, the manager's advice is **not** a routing
  input: it cannot name a target, and asking for one is already refused and
  recorded.
* Do not let routing decide whether a run is ACCEPTED or landed. Those remain the
  verifier + a named non-local authority + the mechanical lander.
* Do not broaden the G2 seam to absorb routing. Routing is the next seam in
  sequence; the replay infrastructure for comparing G0/G1/G2 is PS-579's.

## Concrete first step for PS-605 (smallest useful slice)

1. Implement the selector as a pure function with typed refusals and table-driven
   tests, in the PS-605 worktree (`work/ps-605-domain-policy`).
2. Make the harness consume it: replace the pinned target with
   `select_target(...)`, build `DispatchDecisionReceipt` FROM the returned
   decision (policy revision/hash, candidates, capability receipts, reason code),
   and keep `explicit_pin` available as an explicit **policy** rather than the
   default. The existing receipt fields are the acceptance criteria — if a field
   is still empty afterwards, the routing decision is not yet real.
3. Negative control that makes the slice meaningful: a sensitive-domain packet
   whose local profiles are all ineligible must produce a typed refusal with
   **zero model calls and zero dispatches**, recorded in the ledger, and the
   harness must not seal an EvidencePackage about a run that never happened (the
   `neg-no-interface` case already demonstrates that pattern).
4. Reuse the comparator pattern rather than inventing one: `live_run.py
   --verify-only <worktree> <case> --base-sha <sha>` seals an exact-base
   VerificationReceipt with no model call. That is how a routing change can be
   shown not to have moved the deterministic baseline.

## Evidence to read before starting

* `docs/local-targets/PS635-G2-REPLANNER.md` — the manager gate, its refusal
  codes, and two live controls on RTX4500.
* `docs/local-targets/PS638-LIVE-PREREG.md` — the receipt path the routing
  decision has to plug into, including the comparator and validator behaviour.
* `docs/local-targets/PS639-G1-PREREG.md` — the exact-base comparator pattern.
* `data/live/g2-replan-control-20260914T222427Z/` — a sealed package containing a
  manager seam, if you want a worked example of what "the decision is attributable"
  looks like end to end. (Run artifacts live under the gitignored `data/`.)
