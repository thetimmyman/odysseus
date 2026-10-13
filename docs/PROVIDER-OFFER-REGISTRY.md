# Provider/model offer registry

This extends the existing offer observation contract. Capacity observations stay
with `provider_capacity`; policy and dispatch stay with the existing selector;
verified, accepted and landed outcomes stay with the outcome ledger. Reading a
tariff does not grant permission to use it.

## Reuse assessment and implementation boundary

OD-24's desired result is immutable, source-bound economics for an exact
provider/pool/model/harness/path, suitable for a later outcome join. The existing
implementation at Odysseus `4fb2255b83ea252b6abb3e21c31b6f96e28100ea` supplies
`src/provider_model_offer.py`, `src/provider_model_offer_store.py`,
`src/offer_economics.py` and `src/execution_outcomes.py`. The repository's MIT
license applies. Reuse these contracts and the create-only store; do not create
another catalog, capacity store, routing authority or provider collector.

TMOS AI Usage `90d3cdfaa736659fbd5edc9a15094287c2f1dab0` supplies the existing
provider coverage assessment, `collector/inference_reports.py`,
`collector/offer_refresh.py` and `collector/outcome_ledger.py`. They are reference
inputs, not copied modules. Provider quota, published deals, local activity,
recognized subscription expense and validated tasks remain different facts.

The earlier authenticated reconnaissance is retained in OD-24's September 24
Plane records and the source-pinned `docs/PS679-USAGE-BRIDGE.md`. It established
Cline account/history readings, Command Code billing-period summaries, OpenCode
Go quota/model APIs and the distinct Console service-account export. Consumer
Codex/Claude quota sources and local activity do not establish organization API
billing or complete personal request history. Later OD-24 comments record
OpenRouter provider-reported zero charges and a failed installed-task outcome;
that experiment is not requalified here and does not authorize another inference
experiment. These are dated observations, not fresh quota or promotion claims.

No bespoke collector is admitted here. CodexBar request/parser patterns,
models.dev/LiteLLM catalogs and ccusage local history remain reference donors
behind the existing adapters. No external implementation or price table is
copied, no new dependency is installed, and no unverified donor license is
treated as extraction permission. A future collector needs a fresh authenticated
source census, exact upstream revision/component license and its own read-only
positive/negative proof before implementation.

### Existing source census

This is a source census at the revisions above, not a new authenticated probe.
Response contracts and prior negative observations are reused rather than
making inference calls or scraping another price table. Current account access,
promotion eligibility and missing report coverage remain UNKNOWN unless a fresh
owner-produced observation supplies them.

| Pool/path | Existing read-only source and auth scope | Available dimensions and limits | Source class / fallback |
|---|---|---|---|
| ClinePass | Account API key; `users/me/plan/usage-limits`, paginated `users/{id}/usages` | Rolling/weekly/monthly meters and resets; allowlisted request model/token/credit fields. A field named `costUsd` alone does not prove cash units. Model-specific current tariffs/promotions and complete history remain unknown. | App-facing provider API, partly undocumented (`PROVIDER_API_DISCOVERED`); retained bounded local ledger, never fabricated completeness. |
| Command Code | API key; `provider/v1/models`, `alpha/billing/credits`, `alpha/billing/subscriptions`, `alpha/usage/summary` | Native credit pools, plan eligibility, rolling windows/reset and aggregate tokens/request outcomes. Credits are not USD; request completion is not accepted work. Published deals require fresh exact-path verification. | Existing account API observations; published catalog separately. Unsupported request-level export remains unknown; local sessions are partial fallback. |
| OpenCode Go | Personal Go key; `zen/go/v1/models`, `zen/go/v1/usage` | Model catalog, percentage windows and resets. This key is distinct from the Console service-account CSV export (`api/v1/usage/export`). Workspace charges cannot become Go subscription expense. | Supported provider API; optional separately scoped Console report/import and local transcript fallback. |
| OpenRouter | Existing provider/account request path; historical credits/key/catalog and generation observations are recorded in Plane | Aggregate credits/usage/catalog and provider-reported generation charges have distinct identities. No fresh catalog, account counter, exact free route or current promotion is asserted by this package. | Historical provider API evidence; missing current evidence remains UNKNOWN. No repeat of the closed inference experiment. |
| Codex subscription / separate organization API | Existing subscription quota collector and local rollout reader; organization usage/cost reporting requires distinct administrative scope | Subscription quota/reset and local token/activity coverage are separate from organization API spend. Complete per-model tariff, personal billing and successful-work joins are not established by those meters. | Existing account-scoped client evidence; local activity is explicitly incomplete, not billing authority. |
| Claude subscription / separate organization API | Existing subscription quota collector and local transcript reader; organization Admin Usage/Cost APIs have different credentials/scope | Subscription windows/reset and captured local activity; consumer plan history and realized per-task spend stay unknown when not observed. | Existing account-scoped client evidence; local activity fallback cannot stand in for organization or consumer billing. |
| Local Qwen | Existing capacity observations and source-bound request/attempt usage ledger | Host/resource availability and recorded input/output/cache usage; no invented dollar conversion for time, energy or opportunity cost. Zero marginal cash requires explicit source evidence and does not mean zero resource burn. | Local client evidence and separately owned capacity facts; missing observations are UNKNOWN. |

Every source carries its own observation time/TTL. Successful quota collection
does not refresh a tariff or establish an exact model route's callability. The
minimum collector set added by this package is **none**. A new receipt producer
maps verified existing observations into v2; it may not upgrade an HTML deal
banner, aggregator price, historical source or local estimate into authenticated
provider billing. CodexBar is a collector/parser reference, models.dev and
LiteLLM are catalog cross-check references, and ccusage is a local-history
reference. Their earlier ADAPT/REFERENCE dispositions do not select a new
external component here; extraction still requires its exact revision/license.

## Versioned terms

Version 1 retains its exact serialized shape and hashes. Version 2 adds typed
`economic_terms` to the same `ProviderModelOfferReceipt`, using the same
content-addressed reference and historical store. Legacy scalar price/credit/
allowance fields must remain unknown in version 2. Existing dispatch consumers
therefore cannot silently reinterpret a richer rule as an old scalar quote.

Terms distinguish published tariff, effective tariff, quota burn, marginal cash
and allowance bonus. Each observation has its own source class, immutable source
version/hash, observation time, TTL, confidence and OBSERVED/UNKNOWN/ABSENT
status. Alias/family mappings require provenance and never replace native model
identity for scope matching. Promotions name the dimensions they affect; a quota
bonus cannot rewrite the list tariff. Billing classification cites its source
and remains descriptive; it cannot replace the capacity owner's classification.

The supported `linear-usage-v1` rule is data, never executable code. It supports
complete-request rates, one-time fixed allowance bonuses, explicit disjoint token categories, tool/request fees,
exact service tiers and nonoverlapping contiguous context bands. Usage rules
explicitly price every category, including known zero rates. Missing usage is
unknown. Cached input is separate from uncached input; reasoning already inside
output is not added again. Arithmetic uses exact rational quantities and never
converts native credits or quota percentages into cash.

## Read-only consumer and outcome handoff

The registry reads an explicit set of caller-selected receipt references, checks
exact scope and currentness, and retains all candidates and disagreements.
Fresh authenticated provider sources precede official catalogs, third-party
registries, operator declarations and inference. A fresh higher-authority
unknown cannot be overwritten by a lower-authority advertised zero. Equal
authority/time conflicts are unknown. Expired/future observations remain
historical and cannot become current by reading them again. Unknown validity
dates in version 2 use the observed TTL; a known expiry still bounds it.
An unavailable authenticated endpoint retains its attempted source class with an
UNKNOWN status and reason; `UNAVAILABLE` alone supplies no tariff authority.

Billing classification is inherited only from the existing capacity store's
authoritative current receipt and fresh price provenance. A conflicting offer
classification remains UNKNOWN with a discrepancy. Without that owner join,
the declared offer classification is retained separately and the reconciled
classification stays UNKNOWN. This is not capacity availability, entitlement,
reservation or routing permission.

Outcome handoffs bind an exact immutable offer and rule, exact attempt reference,
execution time and usage. Provider-reported realized charge is a separate
execution-bound observation, never substituted for estimated cash or quota burn.
The outcome owner must verify that attempt/source binding and own all acceptance
and landing decisions. No matched empirical outcome cohort is manufactured:
ranking remains unavailable, with sample size and uncertainty explicitly absent.
Future empirical comparisons must retain N, unknown coverage, uncertainty and
fault classes; a 1/1 sample cannot inherit a large cohort's confidence.
`validate_economic_handoff` recomputes the rules, exact bindings and integrity
hash after serialization. It does not attest that caller-supplied usage or a
provider charge claim is true; the outcome owner still checks the attempt and
retained source capture.

Inspect selected private objects without collecting anything:

```sh
python -m src.offer_registry --store /absolute/private/offers \
  --offer-ref offer:SHA256 --scope /absolute/private/scope.json \
  --usage /absolute/private/disjoint-usage.json \
  --capacity-store /absolute/private/capacity
```

Scope contains exactly `provider`, `pool_id`, `capacity_receipt_ref`, `harness`,
`usage_path`, and `native_model`. Usage contains uncached `input_tokens`,
`output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `requests` and
`tool_calls`, all explicit nonnegative integers. `--service-tier` is exact;
`--context-tokens` is required for tiered-context rules. Missing usage produces
UNKNOWN arithmetic. Bad references, corrupt objects, duplicate input fields or
an unreadable supplied capacity index exit 2 without a successful result.
Omit the capacity store for tariff-only inspection with UNKNOWN classification.

The bounded proof uses synthetic provider sources and usage, including expiry,
source conflict, unknown-versus-zero, shared-pool/harness identity and historical
handoffs. Existing receipt/store/quote/outcome regressions must remain green.
No network, credential or inference call is needed for these tests. Rollout is
an opt-in read-only consumer, without production deployment or global routing
activation. Revert the source change to remove it; existing v1 objects remain
readable and v2 history must be exported/retained before removing its reader.
