# PS-632 — the capability registry: receipts, freshness, persistence

PS-632 owns **capability evidence**. PS-605 consumes it; PS-638 refers to it. This
document is the contract for the three pieces that make that real: the
`TargetCapabilityReceipt`, the freshness rules, and the store.

## 1. One canonical receipt

`src/local_targets.py::TargetCapabilityReceipt` (schema version 1), sealed by
`make_target_capability_receipt` over its own `core()` with PS-638's canonicalization.

Identity is **two-level**, and that is not cosmetic:

* `host_id` — the stable registered machine (`local-rtx4500`);
* `profile_id` — DERIVED from the material inputs, so it identifies one exact
  execution profile:
  `local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx32768:d94d964641c7`.
  A different backend, artifact digest, quantization, runtime build or context is a
  **different profile**, and it inherits nothing. The factory RECOMPUTES the id from
  the receipt's own runtime/model/context, so an edited identity cannot keep an old
  profile id.

Sub-records, each hashed and each with its own evidence meaning:

| Record | Carries |
| --- | --- |
| `RuntimeIdentity` | runtime kind/provider, endpoint type+url, repository, version, commit, image digest, backend + version |
| `ModelIdentity` | exact `model_id`, alias, family, **artifact digest**, size, quantization, auxiliary (draft/MTP/sidecar) artifacts, declared context + declared capability list |
| `ContextProfile` | configured / served / **safe_working_context** + how it was established, plus observed runtime options |
| `HostBaseline` | kernel, boot-cmdline digest, firmware, mesa, ROCm, libhsakmt, GPU, arch — empty means NOT COLLECTED, never "stable" |
| `CapabilityEvidence` | `measured` / `detected` / `declared` sets, tool semantics as OBSERVED, streaming/cancellation/error behaviour |
| `CapabilityLimits` | measured concurrency, queue depth, VRAM residency, ttft/prefill/decode/cold-load |

Three context numbers are deliberately distinct: the model **declares** 262144, the
runtime **serves** 32768, and a measurement established **32768 safe**. Routing uses
the last one, and a receipt with no measured safe context is stored UNQUALIFIED
rather than handed the declared number.

## 2. Provenance classes

`measured` (this probe observed it on this exact profile) > `detected` (the runtime
reports a fact about its own state now) > `declared` (the artifact/config advertises
it). Declared metadata never satisfies a requirement that demands proof:

* a runtime advertising `tools` with no proven call is `declared_but_unproven`;
* that node keeps `readonly_analysis` (read-only work still routes to it) and loses
  `native_tools` — and therefore the implementer/repair roles.

## 3. Freshness: three clocks, and they do not leak

| Clock | Field(s) | Meaning | Effect |
| --- | --- | --- | --- |
| liveness | `health`, `health_checked_at`, `health_ttl_s` (default 300s) | is the node answering NOW | a dispatch may not start on a non-live receipt |
| semantic qualification | `observed_at`, `ttl_s` (default 7 days) | how long the measured capability claim stands | expired ⇒ ineligible |
| material identity | `identity_digest()` over runtime/model/context/host fields | what was qualified | any drift ⇒ `material_identity_changed` |

A heartbeat refresh can NOT resurrect or extend a semantic qualification, and it
cannot carry one onto a materially different profile. A future-dated `observed_at`
is invalid (`observed_at_in_the_future`), not "very fresh".

## 4. Persistence: `src/target_capability_store.py`

Under `<data_root>/target_capabilities/` (override: `PS632_CAPABILITY_STORE`):

```
receipts.jsonl   append-only; one canonical JSON receipt per line, fsync'd
current.json     deterministic index: profile_id -> receipt_hash (+ previous)
```

* **append-only**: a superseding receipt carries `supersedes`, and history stays
  readable — nothing is rewritten;
* **deterministic**: the index records which receipt is current, so readers do not
  re-derive "newest" from a file that may have grown since;
* **hash-verified**: every line's `receipt_hash` must cover its own content;
* **fails closed**: an unparseable line, a tampered receipt, or an index entry whose
  receipt is missing makes `entries()`/`current()` raise. Routing converts that into
  a recorded skip (`capability store unusable: …`) — a refusal, never a silent "no
  capability, route anyway";
* `mark_invalidated(profile_id, reason)` appends typed invalidation evidence instead
  of deleting anything.

This is not a second evidence ledger: PS-638 owns execution evidence and only REFERS
to a receipt hash.

## 5. The registry topology (as the ticket states it)

| Host | roles | qualification_ref | inference? |
| --- | --- | --- | --- |
| `local-rtx4500` (minipc) | inference, deterministic_verifier | `ps632-measured:local-rtx4500` | yes — its own measured receipt |
| `local-msr1` | deterministic_verifier, governance_ci, arm64_ci | `ps637-verifier-role` | **no** — PS-637 retired its Qwen role; no latent inference fallback exists |
| `local-framework` | inference | `""` | **no** — only an independently qualified profile may fill this in; research/Phase-0 metadata is not qualification, and there is no generic `framework` capability |

The gate lives in the routing seam and applies to BOTH the persisted and the
in-memory record paths, so a healthy probe answer cannot make MS-R1 or an unqualified
Framework routable.

## 6. Consumption

`src.local_target_routing.persisted_routing_inputs(store)` reads only: for each
registered host it requires the inference role, a qualification reference, a current
receipt, `qualification_state() == "valid"` and `health_state() == "live"`, recording
a typed reason for every failure. Each candidate's PS-605 profile carries the **exact
`profile_id`**, the receipt's `observed_at`/`ttl_s`, and `source_receipt_hash`, so
`DispatchDecisionReceipt.capability_receipt_refs` names the **PS-632 receipt hash** —
the identity a later reader resolves back to the capability evidence.

`scripts/odysseus-capability discover` is the only writer: it probes the registered
host and persists the receipt. Routing never measures, so a router cannot make a
capability appear by wanting it. A missing, expired or corrupt receipt is a REFUSAL
with the store's own reason recorded in the sealed refusal.

## 7. Tests

`tests/test_ps632_capability_receipt.py` — the schema, the three clocks, the store's
audit behaviour, and one test per negative control (digest mutation, runtime/profile
identity mutation, expiry, future-dated, missing receipt, corrupt receipt, measured
capability removed, declared-only vs measured requirement, MS-R1 inference, and a
receipt that is not the one bound into the evidence).
`tests/test_ps605_ps632_seam.py` — the translation layer and the harness guards.

