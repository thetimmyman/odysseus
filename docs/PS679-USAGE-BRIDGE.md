# AI Usage outcome bridge and free-offer preflight

These integrations are opt-in. Neither enables inference, changes default
routing, nor claims that historical CLI sessions were verified tasks.

## Committed outcome events

Set `ODYSSEUS_USAGE_EXPORT_CONFIG` to a private JSON file:

```json
{
  "directory": "/absolute/private/path/usage-export",
  "cohort": "coding-repository-tasks",
  "profiles": {"your-exact-profile-id": "command-code"}
}
```

Supported subscription IDs are `codex`, `claude-code`, `command-code`,
`clinepass`, and `opencode-go`. Bind actual subscription-backed profiles;
never infer subscription identity from the model name. The exporter writes
0600 files in a 0700 directory. `events.jsonl` is consumable by TMOS AI Usage's
`collector/outcome_ledger.py`. Export failure does not retry paid inference;
reconcile committed rows using:

```sh
python -m src.routing_outcomes reconcile --task-id TASK_ID
```

New executor attempts carry explicit inference-attempt markers; older records
without markers are excluded rather than guessed. Completed responses remain
pending. Actual upstream errors and meaningful failed checks are recorded;
infrastructure verification failures are not attributed to model quality.
The exporter uses one stable task identity across retries, reopens prior
terminal outcomes on new attempts or changed verification, and deduplicates
attempt events. Mixed or unmapped provider tasks are excluded permanently;
any prior credit is reopened and abandoned to prevent double counting.

After independently reviewing the candidate and its acceptance criteria:

```sh
python -m src.routing_outcomes accept --task-id TASK_ID \
  --model-run-id MODEL_RUN_ID --reviewer REVIEWER --reason 'Acceptance rationale'
```

This explicit attestation requires a non-analysis verification with executed,
nonadvisory, successful blocking checks, committed successful tool records,
and the exact unchanged verified patch. No-tests verification no longer marks
a patch accepted. The private metadata evidence excludes prompts, responses,
commands, and rationale text. Its hash proves integrity, not independent truth;
the named local reviewer remains responsible for semantic acceptance.
Observed exports remain partial coverage; these events alone cannot assert
complete provider utilization or recognized billing spend.

## Free-only request gate

`ODYSSEUS_FREE_OFFER_CONFIG` activates an additional fail-closed gate for every
scout network request and canonical `verify_invocation()` call. All profiles
must be explicitly bound; an unlisted fallback is refused. Example structure:

```json
{
  "capacity_store": "/absolute/provider-capacity-store",
  "profiles": {
    "your-exact-profile-id": {
      "provider": "command-code",
      "pool_id": "your-authoritative-pool-id",
      "harness": "odysseus-scout",
        "usage_path": "your-exact-authorized-usage-path",
        "account_identity": "verified-account-identity",
        "credential_sha256": "SHA256_OF_CANONICAL_RESOLVED_REQUEST_HEADERS",
        "endpoint_id": "EXACT_ENDPOINT_ID",
        "transport_provider": "ACTUAL_RESOLVER_TRANSPORT_PROVIDER",
      "chat_url": "https://provider.example/exact/chat/path",
      "offer_path": "/absolute/verified-offer-receipt.json",
      "source_path": "/absolute/preserved-provider-terms"
    }
  }
}
```

The gate verifies immutable offer hashes, preserved source bytes, authoritative
current capacity, and exact model/provider/pool/usage-path/harness/endpoint
binding. It checks freshness and expiration immediately before each request.
Only an explicitly evidenced zero USD **per complete request** tariff passes;
unknown prices and free input tokens alone do not. Source collection and
verification are separate responsibilities: this gate does not scrape deals,
assert legal eligibility, reserve concurrency, or substitute paid offers.
Existing routing/privacy/budget checks still apply. Canonical callers must
invoke `verify_invocation()` before every external request; the gate cannot
protect requests issued by unrelated native CLIs.

Do not enable this gate from a screenshot or stale promotion. No live account
configuration or deployed harness was changed by this patch.

To construct the receipt and retain source bytes, prepare a verified terms JSON
with `verified_by`, `provider`, `pool_id`, `native_model`, `harness`, `usage_path`,
`tariff_id`, `tariff_version`, `source_url`, `source_sha256`, `observed_at`,
`ttl_seconds`, `valid_from`, `valid_until`, `unit: "request"`, and
`offered_rate_usd: 0`. Dates must be explicit timezone-aware observations and
effective dates from the provider; indefinite/uncertain dates cannot enable
free-only routing. The helper refuses mismatched, stale, or exhausted capacity:

```sh
python -m src.promotional_dispatch --terms-path /private/verified-terms.json \
  --source-path /private/provider-terms --capacity-store /private/capacity \
  --profile-id EXACT_PROFILE --chat-url https://provider.example/exact/path \
  --directory /private/new-offer
```

For a scoped invocation, set `ODYSSEUS_FREE_OFFER_CONFIG` to the emitted config
path before running `odysseus-run`. All candidates in that invocation must pass
the free-only gate; it does not silently enable a paid fallback. No global
environment, saved route, or daemon is changed by the construction helper.

Optionally add `"prefer_verified_free": true` and an explicit `quality_tiers`
profile-to-tier mapping to that scoped config. Scout execution then moves
currently verified free candidates ahead within each contiguous equivalent
tier of the already policy-routed list. Unmapped profiles and tier boundaries
retain their ordering; no profiles are added. Routing scores mix cost and
capability, so they are not silently reused as quality tiers. The actual call
still repeats preflight. This is a verified-free preference, not a general
discount optimizer or comparison of nominal provider credits with cash.

## Bounded cash discounts and quote-only inspection

`ODYSSEUS_OFFER_CONFIG` enables bounded cash quotes instead of free-only mode;
setting both variables is rejected. The config must contain an explicit
`maximum_predicted_request_usd`. `prefer_discounted: true` enables stable cash ordering
inside explicit `quality_tiers` on the already eligible candidate list. It
does not add candidates, cross tiers, authorize paid work, or bypass existing
task/budget checks. Missing comparative evidence leaves a tier's order unchanged.

Each profile may reference one `offer_path`/`source_path`, or an `offer_paths`
array with `source_paths` mapping each preserved source SHA to its file. Cash
tariffs must be USD and either one complete `request` rate or disjoint
`million_input_tokens`, `million_output_tokens`, `million_cache_read_tokens`,
and `million_cache_write_tokens` component rates. Every predicted nonzero
component needs a rate. Mixed whole-request/component tariffs, duplicate
units, and provider credit face values are refused as cash comparisons.

Workload JSON supplies four nonnegative integer counts: `input_tokens`
(uncached only), `output_tokens`, `cache_read_tokens`, and `cache_write_tokens`.
Quotes are predictions, not actual bills. Scout uses the full prompt token
estimate, output limit and zero assumed cache benefit for request preflight.
The explicit maximum limits the predicted request price; it cannot guarantee
a provider's final invoice when tokenizer/usage estimates differ. Existing
budget checks still apply, and paid quotes require the task's paid permission.

```sh
python -m src.offer_economics --config /private/offer-config.json \
  --profile-id EXACT_PROFILE --model EXACT_NATIVE_MODEL \
  --chat-url https://provider.example/exact/path --harness odysseus-scout \
  --workload /private/workload.json
```

This command only reads local receipts, source bytes and capacity. Its JSON
shows source validity/observation data, predicted cash, published-list savings
when known, and separate native credit/allowance effects. For an explicit
`per_request_debit` credit effect, matching current native quota can produce
an estimated requests-from-that-dimension count. It never becomes cash or
claims unlimited capacity for a zero debit.

To construct a bounded-cash config with the verified-terms helper, add
`--maximum-predicted-request-usd AMOUNT` and include `workload` in the terms JSON. The
default helper remains free-only. Multi-component bundles can reference the
separately verified immutable receipts in `offer_paths`.

Canonical dispatch now includes optional `offer_receipt_refs` in both its
decision hash and PS-638 receipt hash. The sealed evidence contains immutable
offer receipts and the scoped quote; verification recomputes price, scope and
reference binding. The invocation repeats freshness/capacity/source checks and
rejects changed offer/ceiling/workload bindings. Dispatches without offers keep
their prior receipt bytes. Native CLI requests outside these call paths remain
outside the harness's control.

The current account binding additionally requires `credential_sha256` in the
authoritative capacity receipt, matching the exact resolved request-header
fingerprint, plus matching `account_identity`, `endpoint_id`, and
`transport_provider` in profile scope. Raw headers are never exported. Full
headers are fingerprinted so cookies or vendor-specific authentication cannot
silently change billed accounts at the same URL. Credential rotation or benign
header changes require fresh verified capacity/binding evidence; legacy
capacity receipts without this evidence cannot activate offers. The verified
terms builder requires that same fingerprint and endpoint/provider identity.

Canonical `resolve_dispatch` / `resolve_from_estate` callers must supply the
actual per-request `workload` and measured `offer_identity`; invocation adapters
must pass the same measured fields through `InvocationIdentity`. A workload
saved in a config is only an inspection example and is never a dispatch
fallback. Scout prepares each complete rendered prompt once and uses its same
workload for ranking, preflight, and the actual output-token limit. Budget
arithmetic retains decimal precision until database/API serialization.

`offer_quote_digests` in the PS-638 receipt now anchors the entire quote,
including workload, ceiling, credential binding and observation time. The
validator requires quote observation within five seconds before decision time.
Recomputing the outer evidence hash cannot change the quote while retaining
the original dispatch receipt hash.

Once an external request begins, its predicted cost is reserved. A successful
usage report adjusts that estimate; malformed usage or post-inference archive/
processing errors retain the reserve or computed cost in the failed attempt.
When billing becomes uncertain, scoped-offer execution stops instead of
falling through to another paid request. These conservative reserves are
explicit estimates, not claimed provider invoices.


Collect real capacity for an existing, enabled, authenticated Odysseus endpoint:

```sh
python -m src.capacity_collector --endpoint-id ENDPOINT_ID --model EXACT_MODEL --store-dir /private/capacity
```

This read-only command resolves the actual endpoint credentials before querying
provider models and quota. ChatGPT uses its authenticated model and usage APIs;
Command Code uses `/provider/v1/models`, `/alpha/billing/credits`, and
`/alpha/billing/subscriptions` (requiring a recognized active API-eligible plan); OpenCode Go uses
`/zen/go/v1/models` and `/zen/go/v1/usage`. The latter adapters allow only their
fixed HTTPS provider origins, refuse redirects, require every expected usage
window, and retain a response-content SHA-256 in receipt provenance. API errors,
missing models, missing meters, or changed credentials leave the store untouched.
They perform no inference. Their account identifier is explicitly a pseudonymous
credential identity, not a provider-verified billing account number. Different
keys belonging to one account are not pooled automatically. Raw credentials and
response bodies are not persisted.

Command Code amounts remain **provider credits**, with no USD face-value
conversion or invented monthly allowance. OpenCode percentages remain percentage
windows. No price is inferred by either capacity collector. Aggregate quota and
a model catalogue do not establish model-specific free capacity or successful
inference access: a catalogue-visible model that is blocked by the provider must
not be activated as a verified promotion. Such cases require separately verified
route/offer evidence; the collector cannot cure a 403 or authorize paid fallback.
These adapters support measurement but do not enable an offer or edit routing.

Canonical callers supply workload and measured request identity at a trusted
adapter boundary; the receipt binds those assertions but cannot independently
prove that an external adapter measured them honestly. The Scout adapter measures
them directly in its executable preflight. An omitted cash-offer profile fails
closed; should a quote source return no quote, its original catalog task-budget
check remains mandatory before any inference.


To connect the exporter to TMOS AI Usage automatically, set the exporter JSON
`directory` to the **expanded absolute path** of
`<TMOS_USAGE_STATE_DIR>/outcome-events`; with the default state directory, use
`/home/YOUR_USER/.local/state/tmos-ai-usage/outcome-events`. The plugin's collector
reads `events.jsonl` there and ingests it idempotently into its outcome ledger.
Replace `YOUR_USER` and any custom state directory before saving; JSON does not
expand shell variables or `~`. Keep this directory private (0700). Merely enabling
the bridge does not manufacture validation evidence or historical completed tasks.

Supported subscription namespaces normalize their provider identity and complete
invocation URL consistently across DB estate discovery, capability projection,
capacity collection, quote validation, and invocation pins. A friendly database
endpoint name does not become a provider ID. An API base and its supported chat
endpoint are equivalent only under fixed provider namespaces; arbitrary hosts and
paths are never rewritten into a subscription identity.


The Scout executor (`routing_executor.execute_candidates`) and canonical
`dispatch_boundary.resolve_dispatch`/`resolve_from_estate` are separate call
paths in this branch: Scout does not invoke the canonical boundary. Scout scopes
offers to `harness: "odysseus-scout"`; a canonical runtime adapter scopes to its
qualified `runtime_kind` (for example `"openai_compatible"`). Prepare separate
explicitly verified configs for these distinct processes/paths. A config for one
harness is intentionally refused by the other, even for the same provider/model.
Both supported paths have offer-enabled regression tests; no claim is made that
Scout automatically produces a canonical sealed dispatch receipt.
