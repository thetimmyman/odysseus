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
!= `entitlement observation` != `routing legality` != `dispatch`.

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

## PS-645 boundary

Command Code can later implement a collector that maps one exact account or
subscription pool into this generic type. It must supply its own entitlement,
privacy facts, model catalog and pricing provenance. Authentication, model
discovery and network calls remain PS-645 work.
