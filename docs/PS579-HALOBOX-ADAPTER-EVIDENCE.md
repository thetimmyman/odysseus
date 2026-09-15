# HaloBox Same-GGUF adapter - evidence (2026-09-15)

Runtime-adapter and capability-registration slice. NO benchmark run: the PS-579
corpus was not executed, and the only model invocations were the bounded
`HALOBOX_ADAPTER_SMOKE_TEST` plus its negative controls.

## 1. Seam (documented before implementation, see PS579-HALOBOX-ADAPTER-SEAM.md)

Three contracts, two implementations each, no routing branch:

| contract | ollama | llama-server |
| --- | --- | --- |
| registry spec (`runtime_kind`) | `ollama` | `llama-server` |
| inspector (`inspect -> raw observation`) | `OllamaInspector` | `LlamaServerInspector` |
| client (dispatcher surface) | `ollama_client.Target` | `LlamaServerTarget` |

`build_capability`, the receipt factory, the store, PS-605 routing and PS-638
evidence are SHARED unchanged. `inspector_for(spec)` and `runtime_client.client_for_target()`
pick the implementation from the registry.

## 2. Registry entry (profile-scoped, no generic Framework capability)

```
target_id         local-framework-halobox
ssh_host          framework
endpoint          http://127.0.0.1:8731     <- from the SEALED launch.sh, not 11434
model             Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf
runtime_kind      llama-server
roles             (inference,)
qualification_ref ps624-qualified:framework/halobox-same-gguf/vulkan/
                  qwen38-flash-next-ud-iq4_xs@halo-box-29e091e
max_concurrency   4
artifact_paths    the three sealed UD-IQ4_XS shards
requires_exact_served_context  True
```

`local-framework` is unchanged: still `qualification_ref=""`, so no generic
Framework inference capability exists, and the client factory refuses to build an
invocation path for it at all.

## 3. Verified live identity

| fact | value | source |
| --- | --- | --- |
| runtime commit | `29e091ea5b228ac1735cde369e68e6767a53e510` | `git -C /mnt/framework-data/repos/halo-box/llama.cpp rev-parse HEAD` (on the target) + sealed launch |
| runtime report | `build_info = b1-29e091e` | `/props` |
| backend | Vulkan | sealed launch + `/props` device |
| model shard 1 | `5ce89370720f8bf90890f439361282104c1aa1482d4013bb9a50923e758e71a4` (10,946,624 B) | sha256 ON THE TARGET |
| model shard 2 | `577a38a2392b40ca2193cea502e1d92f60b8cd370675d308e0ec21885d9daaa7` (49,835,229,856 B) | sha256 ON THE TARGET |
| model shard 3 | `d4634e6d84f0ebb0940be15c90d3790bf6464e3dea3a1cddc567dc0e83ad8833` (43,836,407,744 B) | sha256 ON THE TARGET |
| composite model digest | `8bc229db96a6e574467bf02f5fe17966cd2b41fb2604ce4d701175d3f2ea5fb8` | sha256 over the ordered shard list |
| quantisation | `IQ4_XS` | runtime `ftype = "IQ4_XS - 4.25 bpw"` (filename fallback only) |
| configured / served / safe context | 262144 / 65536 / 32768 | sealed launch + `/props` (n_ctx_slot) + sealed llama-bench depths |
| measured capabilities | `native_tools`, `streaming`, `readonly_analysis` | real tool call (1), real stream (34 chunks), health |

All three shard hashes MATCH the sealed PS-624 `gguf.sha256` for this artifact.

## 4. Persisted PS-632 receipt and dispatch chain

```
profile_id   local-framework-halobox:llama-server-vulkan:b1-29e091e:
             Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf:IQ4_XS:ctx32768:8bc229db96a6
smoke receipt hash        e99b759d406e1cf84213682559fa2b88300f61f24e1f0876e537a9d341322fa8
capability_receipt_refs   [that same hash]      receipt_ref_matches_store = TRUE
ExecutionPackage          7f02b1c1e3664d32 (see the run summary)
EvidencePackage           9411f22448247d22ab4d451db67b50a7ce3e348b6b170f477c702d7cf9b65a90
AttemptReceipt            692d37018fd050baaac7362506789bf5e7881ce16d5dc6e452dfa4e656c687a0
VerificationReceipt       e52d8aae5fed4ba9a0ba4e7f315d68bdd640714544952b88cc1ecec32626c5db
decided_by                ps605_policy   routing_policy@1.2+sha256:4df270ad9543772d
validator                 VERIFIED, no codes
```

Evidence root: `docs/benchmark-ps579/halobox-adapter-smoke/`.

## 5. HALOBOX_ADAPTER_SMOKE_TEST

Not a corpus cell. One invocation of the real write_file tool over the OpenAI SSE
path; the nonce is read back from the file the tool wrote, so prose or an empty
tool call fails.

```
nonce  HBX-A58EAEAC41CB -> written_via_tool=True, tool_calls=1
wall   7.022 s   ttft 6.819 s   chunks 88   tokens 585 prompt / 117 completion
```

## 6. Negative controls (live where the path allows)

| control | result |
| --- | --- |
| missing receipt | typed skip `no persisted capability receipt for this host`, 0 model calls |
| stale/liveness-expired receipt | `liveness not live: liveness_expired`, 0 candidates, 0 calls |
| wrong profile id | `pin_profile_mismatch`, 0 calls |
| generic Framework Ollama cannot satisfy a HaloBox request | client construction REFUSED (unqualified host) |
| **wrong local endpoint (the host's Ollama 11434)** | `pin_endpoint_mismatch`, 0 calls - see 7 |
| **mutated endpoint after the decision (9999)** | `pin_endpoint_mismatch`, 0 calls - see 7 |
| changed model digest / backend / runtime commit | new `profile_id` and/or new `identity_digest` (hermetic tests) |
| adapter cannot reroute itself | no endpoint setter; endpoint comes from the spec only |
| receipt not in the store | `receipt_ref_matches_store` FALSE is a chain failure, asserted |

## 7. Defect found BY the controls, and fixed

The dispatch pin checked LOCALITY only, so `http://127.0.0.1:11434` (the host's
Ollama service) and `http://127.0.0.1:9999` were both accepted as the pinned
endpoint - two loopback ports are equally "local". `pin_for` now carries the
endpoint and `verify_invocation` raises `pin_endpoint_mismatch` before any request.
Both controls now refuse with zero model calls. The endpoint check runs AFTER the
locality check on purpose: locality is the coarser, security-relevant signal ("a
local pin must not resolve to a hosted URL") and keeps its own code, while the exact
endpoint is a second, finer check.

This is a production-dispatch HARDENING, and it changed one existing expectation:
`tests/test_dispatch_boundary.py` used to assert that a different LOCAL address was
acceptable for the same model. That is the same hole in a different coat (two LAN
hosts, or two loopback ports, for one pinned decision), so the test now asserts the
strengthened contract, with the change recorded in the test itself. RTX is unaffected
in practice: the harness already passes the decision's own spec endpoint.

## 8. Heartbeat (a 300 s liveness clock needs one)

`discover --reuse-identity-from-store` reuses the artifact identity already
MEASURED in the store, but only after re-checking every declared shard's SIZE; any
size change falls through to a full re-hash. Measured: a heartbeat costs ~7 s and
still reports `measured` capabilities fresh, versus ~2 minutes for a full hash of
87 GiB. It can refresh liveness; it can never carry a digest onto different bytes.

## 9. Honest operational notes

* One heartbeat hit a TRANSIENT ssh transport failure. The inspector recorded
  `unreachable`, persisted an UNQUALIFIED receipt, and routing refused with
  `health_not_healthy` / served-context 0 and zero model calls. That is the
  fail-closed path working; a later heartbeat restored a qualified receipt.
* The `completion_served` sub-probe reported false: an 8-token `max_tokens` reply
  came back with empty content under this template's default reasoning-preserve
  behaviour. It adds no failure class and the stronger probes (tool call + stream)
  both passed; recorded rather than hidden.
* The declared alias is the shard-1 basename because the sealed launch passes no
  `--alias`; the full served id is kept in the receipt's runtime options.

## 10. Cleanup / restoration

HaloBox stopped with SIGTERM; no llama-server remained; GTT returned to
18,657,280 bytes (the sealed post-teardown value); Ollama restarted and the
Framework health guard passed: `model=qwen3.8:27b context=262144
gpu_resident=true available_kib=123550764`. No kernel/GTT/IOMMU/firmware/runtime
setting was changed at any point.

## 11. Not done (by instruction)

No PS-579 corpus cell, no Halogen, no HaloBox tuning, no PS-624 re-qualification,
no G0/G1/G2 change, no RTX policy change, no generic Framework routing, no PS-578.
