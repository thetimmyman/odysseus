# HaloBox Same-GGUF adapter - architecture seam (written BEFORE implementation)

Goal: let PS-579 dispatch to the already-qualified HaloBox profile through the
existing PS-605 / PS-632 / PS-638 path, with the smallest first-class non-Ollama
runtime seam. This is an adapter and capability-registration slice: no benchmark
run, no runtime qualification, no tuning.

## What already exists, and therefore what does NOT need inventing

| layer | existing abstraction |
| --- | --- |
| registry | `LocalTargetSpec` (target_id, ssh_host, endpoint, model, `runtime_kind`, roles, `qualification_ref`) |
| capability | `LocalTargetCapability` + `build_capability(spec, raw)`, a PURE derivation from a raw observation dict |
| probe | `probe_target` / `probe_fleet(specs, inspector=...)` - already inspector-injectable |
| receipt | `receipt_from_capability` -> `TargetCapabilityReceipt` (identity, provenance, TTLs) |
| store | `target_capability_store` (append-only + index) |
| routing | `persisted_routing_inputs(store)` -> PS-605 `resolve_persisted_dispatch` |
| dispatch | `dispatch_boundary.resolve_from_estate`, `verify_invocation` (pin + locality) |
| evidence | PS-638 ExecutionPackage / DispatchDecisionReceipt / AttemptReceipt / Verification / EvidencePackage |
| client | `scripts/ps635-live/ollama_client.Target` (ollama `/api/chat` NDJSON) |

`runtime_kind` is ALREADY a first-class field on both the spec and the receipt
(`RuntimeIdentity.runtime_kind`), so the runtime dimension needs no new concept -
it needs a second implementation behind the same three contracts.

## The four seams (and nothing else)

**S1 - one profile-scoped registry entry.** A `LocalTargetSpec` for
`local-framework-halobox` with its OWN endpoint (HaloBox's port, not Ollama's
11434), `runtime_kind="llama-server"`, `roles=(inference,)` and a non-empty
`qualification_ref` naming the sealed PS-624 profile. `local-framework` stays
exactly as it is: unqualified, so no generic Framework inference capability
exists. Routing gains NO HaloBox branch - it continues to read specs and receipts.

**S2 - a second inspector behind the SAME raw-observation contract.**
`LlamaServerInspector.inspect(spec)` returns the same keys
`build_capability` already consumes (`reachable`, `version`, `model`, `ps`,
`tool_proof`, `failure_classes`, `auxiliary_artifacts`, `runtime_options`), so the
receipt path is shared, not duplicated. Inspector selection becomes
`inspector_for(spec)` keyed on `runtime_kind`, and `probe_target`/`probe_fleet`
default to it. Provenance stays honest: live queries are MEASURED/DETECTED, and
sealed PS-624 qualification values are supplied explicitly as qualification
inputs (never relabelled as measurements).

**S3 - a second client behind the SAME duck-typed surface the dispatcher uses.**
`LlamaServerTarget` implements `api_streaming_chat`, `api`, `runtime_state`,
`model`, `ssh_host`, `target_id`, and normalises OpenAI SSE
(`/v1/chat/completions`) into the Ollama-shaped body the loop already reads
(`message.content`, `message.tool_calls`, `prompt_eval_count`, `eval_count`).
A tiny factory picks the client from the registry's `runtime_kind`. Prompts, tools,
retry rules, verifier behaviour, G1 repair and G2 replan eligibility are untouched.
The client reports facts; it never chooses an endpoint, so it cannot reroute.

**S4 - endpoint and launch.** HaloBox gets its own port on the Framework host
(8731 from the sealed PS-624 launch; 11434 is Ollama). Because PS-624 showed Ollama
contention is unsafe, Ollama is stopped deliberately for the adapter/smoke window
and restored afterwards, with the sealed launch configuration
(262144 context, parallel 4, batch/ubatch 2048/512, FA on, ngl 999).

## Fail-closed rules this seam must preserve

* No receipt -> no candidate (the gate skips the host with a typed reason).
* Expired qualification, drifted identity or stale liveness -> skipped, zero calls.
* `identity_digest` covers runtime commit, backend, model digest, quantisation,
auxiliary artifacts, contexts and host baseline, so drift yields a NEW profile_id
and the old qualification cannot carry over.
* The dispatch pin (`verify_invocation`) still binds profile + model + endpoint
locality before any request; the client's endpoint comes from the spec only.
* No second router, scheduler, ledger or acceptance authority is introduced.

## Explicit non-goals

No generic Framework routing; no Halogen; no HaloBox tuning; no host kernel/GTT/
IOMMU changes; no PS-624 re-qualification; no G0/G1/G2 change; no RTX policy
change; no PS-579 corpus run; no PS-578.

## CORRECTION 2026-09-16 — identity facts reconciled against the sealed chronology

This document once described the HaloBox control as running with **`ngl 999`**. The sealed
`halobox-control/launch.sh` (07:33:46) that started the qualified server records
**`-ngl all`**, with `launch.started`/`health.ready.json`/`server.log`/`stop.finished` all
inside that window; `999` is the **llama-bench-only** flag (`vendor-bench.command`,
`run-depths.sh`, both after the server stop; `vendor-bench.invalid-ngl.raw` shows llama-bench
rejects `all`). `FINAL-DISPOSITION.md`'s prose copied the bench value into the server
summary. Functional semantics coincide (both offload every layer); the recorded identity is
the executed one: `all`.

Likewise, ContextProfile in PS-632 now separates `configured_pool_context` (262144),
`served_per_request_context` (65536, measured by the sealed `server.log`: `n_slots = 4,
n_ctx_slot = 65536`), `engine_demonstrated_context` (32768, llama-bench ladder, throughput
only) and `semantic_verified_context` (19760, deepest sealed semantic probe). The routing
bound `measured_safe_context = 32768` is engine-demonstrated, not semantically verified.
