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

`scripts/odysseus-capability import` persists already sealed measurement evidence;
`refresh` updates observed identity/liveness and `activate` changes active authority. Routing never measures, so a router cannot make a
capability appear by wanting it. A missing, expired or corrupt receipt is a REFUSAL
with the store's own reason recorded in the sealed refusal.

## 7. Tests

`tests/test_ps632_capability_receipt.py` — the schema, the three clocks, the store's
audit behaviour, and one test per negative control (digest mutation, runtime/profile
identity mutation, expiry, future-dated, missing receipt, corrupt receipt, measured
capability removed, declared-only vs measured requirement, MS-R1 inference, and a
receipt that is not the one bound into the evidence). The PS-605→PS-632 seam
(`persisted_routing_inputs`) is exercised through the same file.

## Installed-profile closeout

New measured receipts use schema 2 with an explicit `qualification_disposition`:
`QUALIFIED`, `ADOPT_ROLE_SPECIFIC`, `QUALIFIED_EXPERIMENTAL`, `REJECTED`, or
`UNQUALIFIED`. Only the first two are eligible. Schema 1 receipt bytes and hashes
remain readable. Schema 2 profile IDs additionally cover full material host and
auxiliary artifact identity. A healthy host or an exactly identified artifact does
not prove reference semantics: `exact_reference_semantics` requires separate
measured evidence. Role-specific work must explicitly request `approximate_implementer` or
`approximate_analyst` plus approximate intent. Existing role requirements stay
unchanged; default requests retain reference intent, and stronger capabilities remain
required even with approximate intent. `minimum_context_tokens` is checked against
the measured safe context, including every fallback.

`active.json` records the explicitly selected profile per physical host. Appending
another profile never activates it. Missing or corrupt authority fails closed;
readers never choose the newest ledger observation. Writers serialize append and
activation with a file lock. The first profile initializes the host index; replacing
it requires compare-and-swap activation plus current material identity. Importing a
historical store requires explicit activation, rather than silently reconstructing
its authority.

`PS632_PROFILE_REGISTRY` can point at a JSON list of `LocalTargetSpec` records to
replace retired endpoint/model configuration with explicit installed profiles.
The canonical persisted seam checks schema 2 endpoint, host, model, qualification
reference and inference-role bindings. MS-R1 cannot acquire an inference role
through this override. Generic Framework metadata remains unqualified.

`scripts/odysseus-capability --store DIRECTORY verify` audits evidence and indexes.
`import RECEIPT [--supersedes HASH]` requires an already sealed receipt;
`activate HOST PROFILE --expected-profile OLD --identity-digest OBSERVED` explicitly
changes the active profile. `refresh OBSERVATION` consumes a current material
identity/health snapshot. It only refreshes liveness: observation time and semantic
TTL remain unchanged. Material drift or expired qualification appends invalidation
instead of resurrecting evidence. Callers can supply `current_identity_digests` to
`persisted_routing_inputs`; a missing observation then refuses that host.

Live measurements and endpoint configuration remain external private artifacts.
A registry import is separate from deployment and from the PS-641 production
composition acceptance run. Short synthetic context tests establish a bounded
working floor, never the advertised maximum or multi-session capacity. Client
stream closure alone never qualifies server-side cancellation.

## Revoking an installed profile

`odysseus-capability deactivate HOST --expected-profile PROFILE` removes only the active authority pointer under the writer lock. It retains all measured receipts and their current indexes. The expected profile must still be active, so a stale rollback cannot revoke a concurrently activated replacement. A host with evidence but no active pointer remains ineligible; reactivation requires the existing fresh qualification and matching observed material identity through `activate`. Revocation does not refresh health or qualification.

## Keeping the installed approximate workers ready

Use `odysseus-capability --store DIRECTORY status [HOST] --json` first. This
reads existing evidence without changing files or calling a model. It explains
qualification expiry separately from health expiry, shows the measured context
and roles, and gives the next action. `READY_FOR_IDENTITY_CHECK` means the stored
clocks pass; dispatch still needs current identity and the task's normal checks.

An expired qualification needs new measurements. A heartbeat cannot renew it.
For an unchanged, previously measured approximate 4K profile, obtain its normal
background capacity lease, then run:

```sh
scripts/odysseus-qualify --receipt current-receipt.json --spec installed-spec.json \
  --identity-command collector-argv.json --output-dir new-private-evidence
```

The private `collector-argv.json` is an operator-controlled JSON argv list for a
trusted collector. It must independently observe the served artifact, runtime,
options, endpoint binding and host baseline, and emit `checked_at`, `profile_id`
and `current_material_identity`. Missing or stale observations refuse the run.
The included collector is `python -m src.local_target_identity --receipt
current-receipt.json --config private-collector.json`. Put that argv in
`collector-argv.json`, using absolute paths and the intended Python interpreter.
Its private config supplies `kind` (`ollama` or `halogen-flash`), `engine_argv`,
`container`, loopback `health_url`, installed `endpoint_url`, `request_options`
and `gpu_query_argv`; optional `ssh_argv` selects the inspected host. Ollama also
requires `model`, `model_manifest` and `blob_dir`. Flash requires `checkpoint`,
`checkpoint_env_key` and `template`. The collector verifies published-port and
native-address ownership, matching endpoint responses, full artifact hashes,
served template/options, host baseline, and stable container identity. It makes
no inference calls. Artifact/image-bound catalog facts and uncollected host
libraries remain explicitly labelled. Proxies and redirects are refused.

Collection brackets two sequential context/tool trials. Both trials must recall
the exact first, middle and last values, return the expected single tool call,
finish completely, and report at least 4096 prompt tokens. The returned tool is
examined as data; no file write is executed. Failed trials or material drift
issue no receipt. Each attempt uses a new private evidence directory.

This operation produces evidence only. It retains the registered
`qualification_ref` as the profile's original qualification anchor; the fresh
report hash is recorded in `context.safe_context_source` and receipt notes.
The registration therefore stays bound while each renewal remains traceable.
It preserves the execution profile, limits qualification to approximate 4096
tokens and one tested tool call, and makes no reference, 32K, maximum-context,
streaming, cancellation or parallel-tool claim.

Review the measured bundle and collect identity again immediately before apply:

```sh
scripts/odysseus-capability --store DIRECTORY renew new-private-evidence/receipt.json \
  fresh-observation.json --expected-profile ACTIVE_PROFILE --expected-receipt CURRENT_HASH
```

Renewal compares the active profile and current receipt under the existing
writer lock. A stale expectation, different material identity or enlarged scope
is refused. Success appends evidence and advances its current index; it preserves
the active pointer and all previous receipts. A changed model/runtime/profile
requires separate qualification and explicit registration/activation. Generic
Framework remains unqualified. MS-R1 remains a deterministic verifier.

After renewal, a small read-only task should use the canonical persisted routing
and invocation guard, pin the selected local profile, grant no tools or writes,
and seal its actual result and dispatch evidence. Local targets retain the native
subscription-capacity exemption; the runtime's background lease still applies.
Successful synthetic qualification alone is not evidence that a real task ran.
