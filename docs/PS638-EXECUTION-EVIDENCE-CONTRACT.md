# PS-638 — the execution/evidence contract (canonical, consolidated)

**This document is a consolidation, not a redesign** (PACKAGES.md B.7,
OPERATOR_RULINGS.md CHECKPOINT §5). Every statement below is sourced from code
already approved and landed on `origin/main` (`e787725fdf74bd03d7cb1cb48b5e7803fa0613a6`
via PR #38, `7afd55bad7034d789c98be4ee6e9ebcfcc97cdba`), or from the Jira PS-638
description. No statement here is new design. No `DESIGN_NOW` / second-wave
concept (DR-02, DR-06, DR-10, DR-14, DR-17, DR-18, DR-21, DR-24) appears as a
required runtime field — where one is mentioned at all, it is named explicitly
as not-yet-required, deferred, or unassigned.

PS-638 splits the envelope along one question: **what was asked** versus **what
happened**. Everything that follows is either the INTENT half (authored before
dispatch, then hashed) or the EVIDENCE half (produced by attempts and
verification, then sealed) — plus the accepted vocabulary extensions Package B
adds to both.

## 1. ExecutionPackage — the immutable intent

Source: `src/execution_package.py` (573 lines).

> "The immutable INTENT written down before anything runs... PS-638 splits the
> envelope along one question: what was asked versus what happened. This
> module is the first half. Everything here is authored before dispatch and
> then hashed, so afterwards the run can only be compared against it — it
> cannot quietly become it."

Two structural decisions, both from the module docstring:

- **The interface is the packet's, not a copy.** `ExecutionPackage` stores the
  same `InterfaceField` values the `WorkPacket` primitive (PS-635 primitive #1,
  `src/work_packet.py`) validates, and its `interface_digest` delegates to that
  primitive rather than re-deriving a second, package-local notion of "the
  interface" — the PS-635 failure this contract descends from was exactly a
  worker and a verifier disagreeing about key names.
- **Dispatch provenance does not pretend to be routing policy.** PS-605 owns
  routing. A receipt must say `explicit_pin` and carry no `policy_ref` until
  production policy is wired; a receipt claiming `ps605_policy` with no policy
  revision is refused (`execution_package.py:225-232`).

## 2. Source identity — why a SHA is not enough

Source: `src/source_snapshot.py` (416 lines).

> "PS-638's second hardening item is explicit: `base_sha`/`head_sha` are
> insufficient if a worktree has uncommitted or untracked inputs, and a clean
> git SHA must never be allowed to imply a clean execution tree."

A snapshot records, deterministically: the repo/base SHA/head SHA/branch/
worktree identity; the STAGED/UNSTAGED/UNTRACKED disposition as explicit path
lists; a normalized digest of the tracked diff against the recorded base; a
digest over untracked content actually part of the tree; per-path digests for
execution-dependent files; and one `snapshot_digest` over all of it.
Determinism is structural (fixed git invocation flags, sorted/NUL-split
paths), and untracked content is bounded — a path whose content exceeds the
cap is recorded in `truncated_paths` and the snapshot's `is_complete` goes
False, so an incomplete snapshot reads as ambiguity, never as clean.

## 3. AttemptReceipt — what actually happened

Source: `src/attempt_receipt.py` (538 lines).

> "Attempt 2 never overwrites attempt 1. A receipt is frozen and
> content-addressed per attempt. There is no mutable 'current attempt' to
> overwrite... PS-638 hardening item 4 requires that every retry survive."

A pre-existing-failure claim needs a baseline, not an opinion: PS-638 requires
an exact-base receipt with a matching normalized failure identity, and only
`classify_against_baseline` decides — returning SAME / CHANGED / NEW from
fingerprints, never from a sentence.

## 4. VerificationReceipt — a verdict is DERIVED, never asserted

Source: `src/attempt_receipt.py`.

> "`VerificationReceipt` has no `passed` field to set. The outcome comes from
> `exit_code` plus capture completeness, so a nonzero command cannot become
> PASS because the surrounding text looked reassuring, and a truncated log
> cannot become PASS either — an absence inside a truncated capture is not an
> absence."

Both directions are deliberately symmetric: truncation poisons a PASS exactly
as much as it poisons a claim of absence.

## 5. EvidenceRequirement / EvidenceClaim — typed, mechanically closable

Source: `src/evidence_contract.py` (371 lines).

> "`ExecutionPackage` carries an explicit set of EvidenceRequirement IDs, and
> package validation COMPUTES whether each is SATISFIED / FAILED / BLOCKED /
> NOT_APPLICABLE from receipts. 'Evidence looks complete' is not a state this
> module can represent."

Two rules the module enforces mechanically:

- **UNRESOLVED is a real, non-terminal fifth state.** A requirement no receipt
  addresses is none of the four terminal states, and collapsing it into
  NOT_APPLICABLE would let an omission read as a waiver. The validator refuses
  to seal while any mandatory requirement is UNRESOLVED; NOT_APPLICABLE is only
  reachable through an explicit recorded waiver with a reason, never by
  absence.
- **INDEPENDENCE is compared, not trusted.** Every requirement names the
  independence class its proof must have; every receipt declares the class of
  proof it carries; closure refuses to satisfy an independent requirement with
  a WORKER_AUTHORED receipt even when that receipt says PASS.

## 6. EvidencePackage — sealed, fail-closed

Source: `src/evidence_package.py` (940 lines).

> "The last stage of the chain: a package that binds intent, dispatch
> provenance, attempts, deterministic verifications and requirement closure
> into one content-addressed envelope, plus a validator that decides VERIFIED
> or not."

Design rules the module enforces:

- **Fail closed, and say WHY.** The validator returns named reason codes,
  never a bare `False`.
- **Recompute, do not read.** `outcome`, requirement states and hashes are all
  recomputed from the raw fields; a package whose summary disagrees with its
  own receipts is rejected rather than believed.
- **No requirement, no VERIFIED.** Mandatory requirements that are UNRESOLVED,
  FAILED, or BLOCKED-when-not-allowed stop the package.
- **Trends are evidence.** Attempt numbers must be contiguous from 1 and every
  receipt hash must verify — an earlier red cannot be dropped to make a final
  green look cleaner.
- **Provenance is compared.** A PASS carried by worker-authored proof cannot
  close a requirement that demands independence, even when genuine.

## 7. VERIFIED != ACCEPTED

Sources: `src/evidence_package.py` validator semantics + `src/mechanical_landing.py`
(395 lines, PS-578).

`EvidencePackage` validation decides VERIFIED, not ACCEPTED — a distinct,
downstream authority decides whether a verified package actually lands:

> "`ExecutionPackage.source` is input provenance. A writable run's accepted
> candidate is the source snapshot named by its passing VerificationReceipt(s)."

`mechanical_landing.py`'s `LandingRefusalCode` enumerates the ways a VERIFIED
package can still be correctly refused landing: `EVIDENCE_NOT_VERIFIED`,
`EVIDENCE_STALE`, `EVIDENCE_INVALID`, `SEMANTIC_ACCEPTANCE_MISSING`,
`ACCEPTANCE_SOURCE_MISMATCH`, `CANDIDATE_CHANGED`, `GOVERNANCE_NOT_GREEN`,
`WRITE_SCOPE_MISMATCH`, `UNRESOLVED_REWORK`, `LANDING_STRATEGY_NOT_ALLOWED`,
`LANDED_TREE_NOT_EQUIVALENT`, `JIRA_RECONCILIATION_FAILED`. Being VERIFIED is
necessary and never sufficient; landing is its own authority (PS-578) and
semantic acceptance is a further, separate authority still, per
`docs/CANONICAL-EXECUTION-FOUNDATION.md`'s authority table.

## 8. Immutable / hash-bound evidence

The `core()` / `receipt_hash` discipline is uniform across `execution_package.py`,
`attempt_receipt.py`, and `evidence_package.py`: every receipt type exposes a
`core()` that returns exactly its hashed fields, and `receipt_hash` is
`sha256` over that core's canonical JSON (`sort_keys=True`,
`separators=(",", ":")`, `ensure_ascii=False`). `PS638_RECEIPT_CORE_FIELDS`
(`src/dispatch_routing.py`, referenced by `docs/PS605-DETERMINISTIC-ROUTING.md:65-70`)
freezes the exact field set and order `DispatchDecision`'s emitted receipt is
bound to, field-for-field, so a decision published by PS-605 hashes the same
way `DispatchDecisionReceipt` would hash it.

## 9. PS-605 boundary

Sources: `docs/PS605-DETERMINISTIC-ROUTING.md` (routing contract) +
`src/dispatch_boundary.py` (1157 lines, the production seam).

`dispatch_boundary.py`'s docstring is the seam map:

```
RoutingTask + candidate rows  ->  RoutingRequest        (intent, from data)
candidates + endpoint rows    ->  profiles + receipts   (the estate)
profiles/receipts + policy    ->  DispatchDecision      (selection authority)
decision + candidates         ->  execution ORDER       (what may be attempted)
resolved endpoint + decision  ->  pin check             (invocation guard)
invocation + decision         ->  AttemptBinding        (PS-638 identity)
all of the above              ->  sealed evidence       (re-checkable proof)
```

PS-605 owns SELECTION and routing/privacy/policy legality; PS-638 owns the
evidence envelope that a selection's receipt is bound into.

## 10. PS-632 boundary

Source: `docs/ps632-capability-receipts.md` (119 lines).

PS-632 owns **capability evidence**: the `TargetCapabilityReceipt` (two-level
identity — `host_id` + a materially-derived `profile_id`), the three
independent freshness clocks (liveness, semantic qualification, material
identity), and the append-only, hash-verified store. PS-605 *consumes* a
non-authoritative projection of a resolved PS-632 receipt; PS-638 only
*refers* to a capability receipt hash (`DispatchDecisionReceipt.capability_receipt_refs`)
— this is not a second evidence ledger.

## 11. PS-640 / PS-641 / PS-578 boundaries

Source: `docs/CANONICAL-EXECUTION-FOUNDATION.md` authority table (lines 5-15)
and store-ownership lines (44-46); `docs/PS640-ARCHITECTURE.md`; this
package's `docs/PS641-PRODUCTION-COMPOSITION.md` (§B.6, below).

> "Store ownership is explicit: PS-632 owns target capability receipts; PS-640
> owns provider capacity/entitlement receipts; PS-638 owns execution/evidence
> receipts; PS-605 owns routing decisions; PS-641 owns production composition
> within one already-authorized invocation."

(The final clause is this package's DR-16 amendment to line 46 — see
`docs/PS641-PRODUCTION-COMPOSITION.md` for the full boundary.)

## 12. Adjacent primitive — WorkPacket (cite, do not absorb)

Source: `src/work_packet.py` (388 lines, PS-635 primitive #1).

> "A WorkPacket is the atomic unit of dispatchable work. It is immutable
> (frozen) and carries everything a worker needs to execute and verify a
> task."

`ExecutionPackage`'s interface digest delegates to this primitive (§1, above).
This document notes the boundary and does not restate PS-635's own contract.

---

## 13. Accepted delta vocabulary (Package B, DR-01 through DR-16)

Everything below this line is additive vocabulary accepted by
`OPERATOR_RULINGS.md` D2 and applied by Package B. None of it is
`DESIGN_NOW`; none of it makes any existing field required; none of it changes
any existing state's meaning.

### 13.1 DR-01 + DR-09 — optional `authority` block (IMPLEMENTED here)

The only item in this batch with an executable surface. `DispatchDecisionReceipt`
(`src/execution_package.py`) and `DispatchDecision` (`src/dispatch_routing.py`)
each gained one optional field, `authority: Optional[Mapping[str, Any]] = None`,
holding: `requesting_principal`, `delegating_principal`, `acting_principal`,
`delegation_chain`, `credential_ref`, `action`, `resource`, `grant_id`,
`grant_expires_at`, `consent_id`, `authority_schema_version`.

**Mandatory qualifier (D7): this block RECORDS AND AUDITS. It does not enforce
authorization.** No code path reads it to permit or deny an action, its
content is never validated, and it must never be described as an access
control or security boundary — including wherever agent shells currently hold
`system:masters` (D7). Runtime enforcement is a later, separate,
sensitivity-dependent ruling, not in scope here.

Two new evidence-requirement kinds are reserved by name only, with no code
enforcing them yet: `KIND_AUTHORITY_BINDING`, `KIND_CREDENTIAL_USE`. One new
terminal evidence reason is reserved by name only: `grant_expired`.

**Hash discipline (F1, falsified in `tests/test_ps638_authority_hash_stability.py`):**
absent (`None`) → omitted from `core()` and the hash entirely, so every
receipt/decision sealed before this field existed hashes exactly as it always
did (proven against a fixture frozen from the landed baseline,
`7afd55bad7034d789c98be4ee6e9ebcfcc97cdba` — not merely against a same-run
rebuild, and proven again at the `seal_dispatch_evidence` payload level per
review round 2 F-1: the field is omitted from `to_ps638_receipt_kwargs()`'s
own returned dict, not merely from the hashed core, since that dict is
separately content-addressed as `seal.evidence_hash`). Present → hash-bound
like any other field, with sub-field key order and explicit-null-vs-omitted
normalized to the same canonical form (`_normalize_authority`).

**`authority={}` is distinct from absent.** `_normalize_authority` drops
`None`-valued *sub-fields* inside an authority mapping that is already
present, so a sub-field a caller omits and the same sub-field explicitly set
to `None` hash identically. It does **not** treat an explicitly empty mapping
(`authority={}`) as equivalent to `authority=None`: the former is present
(hash-bound, appears in `core()` as `{}`) and the latter is absent (omitted
from `core()` and the kwargs dict entirely). This is a deliberate,
narrower rule than the sub-field one — "the caller said something, even if
that something was empty" is a different fact from "the caller said
nothing" — and is asserted directly by
`tests/test_ps638_authority_hash_stability.py::test_authority_is_never_read_to_permit_or_deny_anything`.

`policy_ref` and all 13 PS-605 ordered filters are untouched.

### 13.2 DR-05 — `AMBIGUOUS` / `RECONCILED` + `IdempotencyKey` + `ReconciliationRecord`

Vocabulary only; no code in this package.

- Two new PS-638 disposition values: `AMBIGUOUS`, `RECONCILED` — additive, no
  existing state renumbered.
- `IdempotencyKey` as an optional field on `AttemptReceipt`/effect records.
- `ReconciliationRecord` as an `EvidenceClaim` type.
- A typed `ActionReceipt` *is-a* PS-638 receipt for non-Dev actions.

**Name-collision, disambiguated here as the amendment requires:** PS-638's
`AttemptReceipt` already carries a generation/fencing token that fences
**attempts** (an in-process retry counter). The durable-action token this
delta contemplates fences **external effects** (an idempotency key for a
side-effecting call outside the process). These are named distinctly —
`attempt_generation` (existing, `AttemptReceipt`) versus a separately-named
effect fence (`IdempotencyKey`, not yet implemented) — precisely so
implementation does not conflate a retry counter with an external-effect
fence. No mechanism is defined here; this section names the vocabulary only.

### 13.3 DR-07 — temporal-truth naming rule

Every sourced fact carries `occurred_at` / `valid_from` / `valid_to` (when it
was true) **and** `observed_at` (when PersonalOS learned it). Corrections
**append** with a `supersedes` edge and never mutate a fact in place.

This is a naming convention with no implementation surface: no new required
field is added to any existing receipt, and no schema changes. `execution_package.py`,
`attempt_receipt.py` and PS-640's `docs/PS640-ARCHITECTURE.md` already carry
`observed_at` + TTL/STALE semantics; this section only names the convention
those modules already follow, and extends it to future fact-bearing records.

### 13.4 DR-08 — `DeletionReceipt` as an `EvidencePackage` subtype

`DeletionReceipt` is named as a PS-638-shaped receipt with typed evidence
requirements: source-erasure proof, derived-invalidation proof, tombstone
ref, retention record, recompute result. Deletion *flavors* (`account_delete`
vs `gdpr_erasure`) are first-class, named values.

**Builds nothing.** No TA or EOT erasure code is touched by this package, and
no GDPR pipeline work is authorized by naming this subtype.

### 13.5 DR-13 — capacity observation != capacity reservation

**Text accepted here, applied in Package C** (against the frozen PS-640
candidate's own architecture doc, during its canonical port — see
`PACKAGES.md` B.5 and C.2). Not applied to any file in this package. The
agreed text: append to `PS640-ARCHITECTURE.md`'s `## Canonical distinction`
chain, `... != capacity reservation`, with the explicit sentence
`ProviderCapacityReceipt is an observation, never a reservation. Reservation
is a separate, fenced, expiring claim owned by the durable-action layer.`

### 13.6 DR-16 — PS-641 scoped to within-invocation composition

See `docs/PS641-PRODUCTION-COMPOSITION.md` (this package, new) and §11 above
for the `CANONICAL-EXECUTION-FOUNDATION.md:46` amendment. Summarized here only
to keep this document's boundary section complete: PS-641 owns composition
*within one already-authorized invocation*; it does not own policy/selection
(PS-605), the evidence envelope (PS-638), a bounded dispatch-verify-repair
loop and retry budget implemented in PS-635's lane (**mechanics, not
authority** — a measurement, not an ownership grant), or durable cross-run
intent/leases/fencing/continuation/scheduling (DR-21 / PS-650).

**Ownership boundary RULED (`OPERATOR_RULINGS.md` D9, 2026-09-17):** PS-605
legality+selection; PS-635 bounded loop + retry budget (mechanics, not
authority); PS-638 canonical evidence/receipts; PS-641 composition and
revalidation within an authorized invocation; DR-21/PS-650 durable intent,
leases, fencing, continuation, scheduling, idempotency, reconciliation.
**The phrase "replan authority" itself remains UNASSIGNED** — that specific
word is distinct from the now-ruled boundary above; this document does not
assign it to PS-635 or to any other lane.

### 13.7 DR-10 — the D3 invariant, ruled as a required integration invariant

Carried here because `docs/PS641-PRODUCTION-COMPOSITION.md` quotes it
verbatim as a requirement PS-641 must respect. **DR-10 is RULED
(`OPERATOR_RULINGS.md` D9, 2026-09-17): `ACCEPT_DELTA_NOW` / `IMPLEMENT_NOW`
in `CONTRACT_DELTA_REGISTER.md`** — a required integration invariant,
currently UNSATISFIED. Semantics: normally revalidate the standing dispatch
decision cheaply (freshness/TTL, policy revision, target identity, effect
envelope); invoke a fresh PS-605 selection only when one of those checks
finds the standing decision invalidated. See
`docs/PS641-PRODUCTION-COMPOSITION.md` for the full text and the measured gap
(`PS635_OWNERSHIP_FINDINGS.md` §4-§5).
