# PS-641 — production composition, scoped (canonical boundary document)

**Status: DESIGN_READY / BLOCKED_BY_DELTA_AND_PS640_INTEGRATION. Do not start
PS-641 implementation from this document** — it materializes the *scope*
ruling (DR-16, `OPERATOR_RULINGS.md` D2 + CHECKPOINT §4), because before this
package PS-641's entire written contract was one line at
`docs/CANONICAL-EXECUTION-FOUNDATION.md:46` plus the Jira description. With no
design doc, that one line **was** the contract, so amending it here is a real
contract edit, not a clarification (CHECKPOINT §4).

PS-641's five prerequisites (`OPERATOR_RULINGS.md` CORRECTIONS), none of which
this document satisfies on its own:

1. `e787725f` lands onto `origin/main` (Package A — **done**,
   `7afd55bad7034d789c98be4ee6e9ebcfcc97cdba`);
2. accepted deltas applied (Package B — this document is part of that);
3. PS-640 `9762dd32` integrated onto that lineage (Package C);
4. PS-640 integration independently reviewed (Package C's review gate);
5. D3 retry/replan language bounded after the PS-635 inspection — the
   inspection is done (`PS635_OWNERSHIP_FINDINGS.md`); the operator's bounding
   decision is what remains.

## PS-641 OWNS (verbatim from the ruling)

1. composition within one already-authorized invocation;
2. immutable preflight;
3. exact target/profile binding;
4. reservation/freshness validation;
5. exact invocation;
6. revalidation before subsequent external requests;
7. verification orchestration.

## PS-641 DOES NOT OWN (verbatim from the ruling)

1. policy/privacy legality;
2. provider selection;
3. retry/replan authority;
4. durable cross-run workflow state;
5. scheduling authority;
6. durable external-effect fencing;
7. semantic acceptance;
8. mechanical landing;
9. deployment.

## The D3 invariant (retained verbatim)

> "Every re-dispatch requires a new PS-605 selection. A change to effect
> envelope, target identity, scope/permissions, or policy invalidates the
> previous selection/pin."

**This is stated as a requirement, not as a property the estate currently
has.** Measured against `work/ps635-ps605-integration` @ `acc0ebda`
(`PS635_OWNERSHIP_FINDINGS.md` §4): selection and capability-receipt
freshness evaluation (`resolve_dispatch` / `verify_invocation`) run **once per
run** (`scripts/ps635-live/live_run.py:797-848`, `:819`;
`src/dispatch_routing.py:789-795`), while attempts 2..N re-dispatch against
that same frozen pin (`src/local_worker_loop.py:409`) with no selection or
re-validation call inside the loop body. The invariant's clause 2 ("every
re-dispatch requires a new selection") therefore **fails today**, and its
invalidation clause holds only vacuously and in one direction — inside-out
envelope changes are unrepresentable, outside-in changes (TTL expiry, policy
revision, endpoint identity after a restart) go undetected. Writing this as a
description of the current estate would be false; it is written here, and
must be implemented, as a requirement on the PS-605 x PS-635/PS-641
integration.

**DR-10 (proposed, not yet ruled).** `PS635_OWNERSHIP_FINDINGS.md` §5.2
proposes that satisfying the invariant is cheap and would not move retry
policy into PS-605: a re-dispatch could reuse the standing decision only when,
at re-dispatch time, its capability receipts are still within TTL, the policy
revision is unchanged, and the resolved endpoint identity still matches the
pin — a freshness/identity check against an existing decision, never a re-run
of policy evaluation and never a new selection call per attempt; otherwise,
re-select or fail closed. **This is DR-10's proposed satisfaction rule, not a
ruled requirement** — DR-10 remains `DESIGN_NOW` / `IMPLEMENT_LATER`
(`CONTRACT_DELTA_REGISTER.md`), it visibly relaxes the verbatim invariant
above (which requires a new selection on "every re-dispatch"), and this
document's own prerequisite #5 (above) states the operator's bounding
decision "is what remains." No implementation may rely on this paragraph
until DR-10 is ruled.

## The measured PS-641 clause: no attempt loop, no retry authority

PS-641 may **not** implement an attempt loop, a retry budget, a no-progress
rule, or a re-dispatch path. Where a composed invocation fails, PS-641 returns
a typed failure to its caller; the decision to attempt again is made outside
PS-641, under the D3 invariant above.

**This is a measurement, not an ownership assignment.** A bounded
dispatch-verify-repair loop and a retry budget are implemented TODAY in
PS-635's lane (`work/ps-635-loop-demo`, `src/local_worker_loop.py:306-568` on
that branch; `PS635_OWNERSHIP_FINDINGS.md` §2.3). **The ownership word for
retry/replan authority remains UNASSIGNED** — not PS-605, not PS-635, not
PS-650, not a durable-action layer — pending the operator's D3 bounding
decision (`OPERATOR_RULINGS.md` D3; `CONTRACT_DELTA_REGISTER.md` item 8: "that
argument is withdrawn"). PS-641 does not acquire retry/replan authority by
growing its own copy of the loop that PS-635 happens to implement today; that
is a statement about what PS-641 must not do, not a statement about who owns
the authority PS-641 is refusing to acquire.

## No named durable-action layer as the referent

**This scope clause names no built layer.** A census of `work/ps-635-loop-demo`
@ `f96ebc20` (`PS635_OWNERSHIP_FINDINGS.md` §2.5-§2.7) finds **zero symbols**
for lease, fencing token, or continuation/checkpoint mechanism — despite Jira
PS-635 listing "claims/leases/fencing" in its authority boundary and carrying
a `lease-fencing` label. PS-650's own report §15 called this "DECLARED-only,
behaviour UNKNOWN"; that is upgraded here to **DECLARED-only, behaviour
ABSENT**.

So: durable cross-run intent, leases, effect fencing, continuation and
scheduling are **durable cross-run authority — owner not yet assigned; these
mechanisms exist nowhere in the estate today.** This is deliberate — even the
*naming* of a durable-action contract waits on the PS-635 ownership inspection
(`OPERATOR_RULINGS.md` CHECKPOINT §6: "Do NOT implement DR-21"), so this
document does not cite DR-21 or any other named layer as the referent for
these concerns. (Open question **Q-N2** asks whether "owner not yet assigned"
is acceptable as a permanent wording; if a placeholder name is preferred
later, this is the one clause in this document that changes.)

## Amendment to `docs/CANONICAL-EXECUTION-FOUNDATION.md:46`

Line 46 previously read, in full:

> "Store ownership is explicit: PS-632 owns target capability receipts; PS-640
> owns provider capacity/entitlement receipts; PS-638 owns execution/evidence
> receipts; PS-605 owns routing decisions; PS-641 owns production
> composition."

It now reads (this package's edit, applied alongside this document):

> "Store ownership is explicit: PS-632 owns target capability receipts; PS-640
> owns provider capacity/entitlement receipts; PS-638 owns execution/evidence
> receipts; PS-605 owns routing decisions; PS-641 owns production composition
> **within one already-authorized invocation** — see
> `docs/PS641-PRODUCTION-COMPOSITION.md` for the full OWNS/DOES-NOT-OWN
> boundary."

## Acceptance (PACKAGES.md B.6)

Both files exist on the landed lineage; the OWNS/DOES-NOT-OWN lists match the
ruling item-for-item; the D3 invariant appears verbatim; no ownership word for
retry/replan authority appears anywhere in this document; no implementation is
authorized or included by this document.
