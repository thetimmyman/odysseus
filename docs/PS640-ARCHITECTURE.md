# PS-640 provider capacity and entitlement registry

## Reconciliation

PS-632 owns `TargetCapabilityReceipt`: whether an exact execution profile can
perform a requested capability. PS-638 owns the versioned evidence envelope and
the `capacity_receipt_ref` binding point. PS-605 owns deterministic policy,
privacy and permission legality. PS-641 will consume policy-selected candidates
and bind a real dispatch. Existing PS-522 cooldown/reset observations and PS-597
shared API budget are inputs to this facts layer, not routing branches. PS-623
describes model/target catalog identity, which is not a capability or capacity
assertion. PS-627 is an acceptance gate. No Command Code client is implemented.

PS-640 owns one immutable `ProviderCapacityReceipt` per independently consumable
pool, typed quota/cost/entitlement observations, scoped freshness, provenance,
validation and append-only persistence. Entitlement is an observation of what a
provider/account exposes; it is not a PS-605 grant of autonomous legal use.
PS-640 reports ZDR/privacy facts but never accepts a policy requirement or
decides whether ZDR is required. It does not choose a provider, model or
fallback and it does not decide privacy legality.

## Canonical distinction

`catalog availability` != `capability qualification` != `capacity observation`
!= `entitlement observation` != `routing legality` != `dispatch` != `capacity
reservation`.

`ProviderCapacityReceipt` is an observation, never a reservation. Reservation
is a separate, fenced, expiring claim owned by the durable-action layer.

`TargetCapabilityReceipt`, `ProviderCapacityReceipt`, and
`DispatchDecisionReceipt` are separate artifacts. A production target needs
fresh qualified capability evidence **and** fresh permitted capacity evidence;
neither substitutes for the other. `ProviderCapacityReceipt.ref` is the stable
PS-638 reference (`capacity:<sha256>`); PS-641 may include it in a dispatch
receipt without importing provider-specific clients.

`UNKNOWN` is serialized as `{status: "unknown"}`. It is not zero, unlimited,
absent, or permission to route autonomously. Every dynamic state, entitlement,
ZDR, quota and known pricing fact has its own provenance and TTL; a fresh health
observation cannot refresh another fact. Price observations carry source,
version and freshness. Actual billed cost stays unknown unless provider billing
evidence supplies it.

The JSONL history is append-only. `current.json` is authoritative for runtime
state: a missing, torn or contradictory index fails closed instead of deriving
authority from history. Replacement receipts must supersede the current receipt
for the same pool. Historical superseded and invalidated records remain
queryable through explicit history access but are excluded from current facts.
Invalidating a current receipt appends an invalidation event and removes that
pool from `current.json`; it is a historical tombstone, never a usable current
receipt. Mutations use a file lock for the complete append-and-index
transaction. A writer that cannot acquire the lock fails with
`CapacityStoreBusyError`; an unindexed JSONL append after a crash is never
auto-promoted.
When an index is valid, runtime resolves only the indexed receipt and the
predecessor chain needed to validate it; valid or corrupt trailing JSONL rows
are unindexed orphan history and cannot replace that authority. The strict
historical reader still reports corrupt rows, and promoting an orphan requires
an explicit recovery/admin operation that writes a new authoritative index.

Any complete in-memory history passed to `CapacityRegistry` is validated before
exposure. Missing, cross-pool, cyclic, temporally invalid, or competing
supersession links fail closed; a self-validating receipt is not by itself
proof of authoritative current state.

`has_usable_capacity_facts()` is deliberately a structural facts check. It is
not PS-605 permission, privacy legality, provider preference, or dispatch
selection. Identity classes are case-sensitive and are validated without
lowercasing. Numeric quota, concurrency and monetary values are finite typed
numbers; counts are integral. Actual billed cost requires billing-grade
provenance such as a billing endpoint, invoice, usage ledger, or explicit
external billing evidence.

## Reserved terminology (DESIGN_NOW, not implemented)

`account_owner_principal` and `credential_ref` are reserved field names on
`ProviderCapacityReceipt` for a later contract revision, once DR-01's
`Principal` / `CredentialReference` authority value types exist. Both would be
`UNKNOWN`-capable, reusing the existing `{status: "unknown"}` serialization
above. This section reserves the names only: no runtime field, no schema
change, and no store change is made by this package. Implementation is
authorized only once the referenced authority value types exist.

## PS-645 capacity collector (implemented, T3)

Command Code implements the collector PS-645 boundary describes: `src/capacity_collector.py` maps one ChatGPT-subscription auth session (one independently consumable pool: `chatgpt-subscription:session:<auth_id>`) into `ProviderCapacityReceipt` from the provider's own facts — the model list plus the usage endpoint `GET /backend-api/wham/usage` (windows `primary_window` / `secondary_window` as `used_percent` + `reset_at`, `credits`, `spend_control`, `rate_limit` flags).

The collector is a pure observation: it neither picks a target, applies a policy, nor mutates auth state. Fact-shape rules:

* State: `spend_control.reached == true` → `EXHAUSTED`; `limit_reached` / `allowed == false` / 429 `usage_limit_reached` → `RATE_LIMITED` (a 429 is a pool fact, not a transport failure); any known window at remaining 0 → `RATE_LIMITED`; otherwise `AVAILABLE`. Unknown auth, empty model list, absent or unparseable usage → no receipt (fail closed, store untouched).
* Quotas carry explicit provenance and never carry invented numbers; `remaining <= 0` and `RESET_AT` are the provider's words, not estimates. A window the endpoint does not report is all-`UNKNOWN`, which is not zero.
* Entitlement is recorded as `THIRD_PARTY_HARNESS` (the runtime runs on a harness consuming the subscription), authorization class `OAUTH_CLI`. The PS-641 runtime gate for `THIRD_PARTY_HARNESS` receipts stays off by design: this pool type carries capacity facts but never grants autonomous-use legality.
* Price observation: `SUBSCRIPTION_SUNK_COST` on `SUBSCRIPTION_CONTRACT`, `pricing_version` set to the JWT plan claim (`chatgpt_plan_type`); marginal cost stays `UNKNOWN` (a flat subscription has no per-use price the provider reports).
* `TTL` is short and on-demand (`DEF_TTL_SECONDS`); no background poller. Dispatch-time cost is one collect per decision; the store is the durable record.

Hosted seam (the only dispatch read the collector feeds): `collect()` / `sync_capacity_store()` mint and supersede; `fresh_hosted_capacity_receipts()` returns the freshest fresh receipt per pool, which PS-641 hands to `dispatch_routing.classify_capacity_for`. A stale or absent receipt yields no capacity facts, and the gate fails closed by its existing `REFUSED_CAPACITY_*` rules — the collector never selects, and the gate never re-observes.

### Known limitation — per-model tier windows (PS-640 design follow-up)

Flat subscription plans subsidize models unevenly: premium (o3 / gpt-5-class) models draw the shared 5h window at several times the rate of standard models, and some plans add a separate rolling-weekly meter for exactly those models. The current `ProviderCapacityReceipt` shape cannot express that: `PriceObservation` is per-pool (one slot), `exposed_models` carries slugs only, and `QuotaDimension` has no model-tier scoping. Consequence: on a plan with a separate weekly premium meter, a pool can report `AVAILABLE` for a premium model whose weekly meter is exhausted — the collector treats the shared windows as one meter and never asserts per-model coverage (no per-model rate or tier mapping is minted; the price observation stays `UNKNOWN` on marginal cost).

The fix is a PS-640 *type-shape* question (per-tier window dimensions, or a model→window mapping on the receipt), not a collector bug: once the shape lands, the collector maps any extra endpoint windows it reports (each has its own `used_percent` / `reset_at`) without further design work. Tracked as a follow-up against PS-640; deliberately NOT implemented in PS-645/T3 against the frozen facts layer.

(Historical note — the original boundary text: "Command Code can later implement a collector that maps one exact account or subscription pool into this generic type.")
