# PS-707 finite provider-gateway harvest completion

This document completes the finite protocol-normalization/no-fallback proof and
reconciles it with the existing source harvest. It does not activate a gateway,
transport, provider, credential path, retry system, or second selector.

## Evidence and source pins

The original 19-row comparison remains in the immutable TMOS harvest
`docs/research-2026-09-19/PS-707-provider-gateway-harvest.md` (admission-pinned
SHA-256 `3f5589a7273addaf1f87f2a9a1cf5f6366b3984a8567e5db6ae897da7db9e4da`).
It reports 0 ADOPT, 10 ADAPT, 4 REFERENCE, and 5 REJECT. Primary donor source
files are captured in `work/director/research-ps707-primary/pinned-source-manifest.json`
(SHA-256 `8a245e4c1c5c06e3026d32e80334850a3fe35858504631f5d4a37d038da1ad89`):
9Router commit `a8c9d3802c5933500fba95416f5bf0c130581396` and OmniRoute commit
`7a921299c5b4c28dcf837f56a1c312b61414a646`; both captured license files identify
MIT. No donor code is vendored.

The canonical contract was checked against Odysseus base
`963d91f360885f1f5308891187937c38d1c1fb6b`. The finite proof uses the actual
`src.dispatch_boundary.BoundDispatch`, `InvocationIdentity`, and
`verify_invocation`; PS-605's already-selected `DispatchDecision` is the only
selection authority. The normalizer verifies its sealed receipt hash, requires
the selected profile (even where other candidates are eligible), and delegates
the provider/runtime/model/endpoint identity check to `verify_invocation`.
No routing selector is called again.

The canonical decision contract has a content hash but no decision-age TTL. This
normalizer proves that the supplied decision is internally hash-consistent at
the call boundary; it does not invent a freshness window. Callers must use the
current bound decision in the existing dispatch sequence.

## Reconciled KEEP / BORROW / REJECT decisions

| Concern | Ruling | Current contract and disposition |
|---|---|---|
| Canonical protocol pivot and tool name conversion | BORROW shape | Borrow the pure deterministic alias-map pattern from 9Router `open-sse/translator/index.js` and OmniRoute `open-sse/translator/helpers/toolCallHelper.ts`. The admitted implementation is a local immutable request copy with deterministic bounded aliases and exact response restoration. It preserves messages, schemas, argument text, and call IDs. |
| Target/provider/model/endpoint selection | KEEP; donor fallback REJECT | PS-605 selects once; PS-641 `verify_invocation` guards the pin. Alternate eligible profiles are still refused by normalization. No default combo expansion, fallback chain, retry, candidate ordering, or provider/model substitution is added. |
| Provider catalogs and aliases | REJECT as authority | Donor catalogs can discover/expand choices. This proof consumes only the selected canonical identity; unknown aliases and case variants fail closed. |
| Quota dimensions and pool/window vocabulary | KEEP canonical; BORROW vocabulary | PS-640 `ProviderCapacityReceipt` and `QuotaDimension` remain authoritative for typed unit, limit, remaining, reset, provenance, and pool scope. This change adds no provider-specific branch or second quota schema. |
| Quota reset timers/cooldowns | REJECT reset authority; BORROW error classification as reference | A reset timestamp is evidence, not a quota renewal event. Missing/stale reset stays UNKNOWN; no timer clears exhaustion and no routing branch is created. |
| Cost, tokens, and billing | KEEP PS-679 economics | Canonical offer/economics projections stay advisory. Tokens/requests/percent are distinct units; no conversion is inferred. Unknown billing stays UNKNOWN; actual billed cash requires billing-grade evidence. No token count creates cash, tariff, or entitlement. |
| Credential refresh, blobs, and storage | REJECT donor custody | The normalizer accepts no credentials, performs no secret lookup, does not log secrets, and has no provider or network access. Canonical secret-broker reference/session authority remains unchanged. |
| Streaming, SSE deltas, discovery, health, and runtime | REFERENCE only | The bounded proof is pure request/response name normalization. It does not implement SSE, callbacks, socket/process launch, provider health, discovery, or deployed behavior. |

This reconciles the prior 19-row inventory: translator algorithms are the
bounded borrow; canonical PS-605/640/679/641 contracts are kept; selection,
fallback authority, credential custody, reset clearing, and transport adoption
are rejected; the remaining operational donor behaviors are references only.

## Normalization contract

`src/provider_protocol_normalization.py::normalize_request` requires a live
`BoundDispatch` value with a valid canonical receipt hash and an invocation
whose identity passes actual `verify_invocation`. The invocation profile must
equal `decision.selected_profile.profile_id`; mere membership in the eligible
set is insufficient. Request `model` must exactly equal the pinned model.

The function deep-copies the mapping and changes only tool function names that
do not satisfy the explicit bounded ASCII-name constraints. Names are mapped
deterministically to `t_` plus a SHA-256-derived token. It rejects malformed
tool shapes, duplicate inputs, generated-name collisions (including
case-folded collision with an existing safe name), and impossible constraints before returning a
result. The same exact map is applied to supported assistant
`messages[].tool_calls[].function.name` and explicit function `tool_choice`
references; unknown or malformed references are typed refusals. The ordinary
`none`, `auto`, and `required` choice strings remain unchanged. Invalid
UTF-8-encodable names (including lone surrogates) are also typed refusals.
The alias map is returned with the selected profile and decision hash.
`restore_response` requires that same context, exact provider/model claims, and
an exact alias match. It refuses unknown aliases and case-only guesses. It
restores only the function name in a copied response; arguments and call IDs
remain untouched. Request references are validated before a result is returned;
the caller's payload is never modified. Neither function calls transport or
user callbacks.

The proof does not claim that this pure name adapter implements every gateway
protocol feature. Endpoint identity is established by the canonical invocation
guard; this layer cannot change it. There is no fallback result on any refusal.

## Quota and economics examples

`QuotaDimension` remains the canonical shape. Its `unit` is carried alongside
`limit`, `remaining`, `reset_at`, and evidence provenance. `UNKNOWN` is a typed
value, not zero. A missing or unknown reset does not become an elapsed timer.
`project_offer_quota` in `src/offer_economics.py` only projects exact same-unit
per-request debits from bound, fresh typed offer and capacity receipts. It is
advisory arithmetic; it does not reserve capacity or claim USD value, eligibility,
or measured consumption. Percent-to-token, token-to-cash, and subscription-to-API
conversions remain UNKNOWN absent an explicit authoritative mapping. The normalizer
does not modify any of these receipts or projections.

Credentials remain reference/session-only under the existing secret-broker
authority. No donor base64 credential blob, refresh token, provider key, logging
or database custody pattern is adopted. This task makes no secret-broker or
PS-641 production-composition claim.

## Executed finite controls and limits

`tests/test_provider_protocol_normalization.py` uses the actual canonical
resolver and verifier over synthetic SQLite fixtures. It exercises two eligible
profiles while refusing the unselected one, a selected-target round trip,
argument/schema/message/input immutability, deterministic repeated output,
request/provider/model/endpoint substitution, altered or absent decision hash,
generated alias collision with a pre-existing safe name, malformed alias
constraints, consistent declaration/history/forced-choice aliasing and exact
response restoration, unchanged tool-role call IDs, unchanged ordinary choice
strings, malformed or unknown references without input mutation, lone-surrogate
typed refusal, unknown alias, and case-variant alias refusal. A separate native
control constructs the canonical typed quota dimension with explicit UNKNOWN
limit, remaining, and reset, preserving its unit and evidence. No real provider,
network, process, socket, secret, or external database is accessed.

The bounded proof establishes only hermetic normalization behind a canonical
selected target and no-fallback refusal. It does not establish SSE integrity,
deployed RLS, live provider behavior, production composition, credentials,
real quota state, reset correctness at a provider, actual billing, or operational
health. PS-707's other acceptance areas remain bounded by the pinned harvest and
canonical source; this implementation is not permission to launch a gateway.
