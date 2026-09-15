# PS-579 first Framework execution-profile comparison - BLOCKED before dispatch

Date: 2026-09-15. Status: **`FRAMEWORK_RECEIPT_BLOCKED`**. No model call was made,
no preregistration was sealed, no runtime was tuned, and nothing was changed on the
Framework host.

## 1. What IS resolved: the exact qualified identity

The identity is fully recoverable from the sealed PS-624/PS-632 record (PS-632
comment 13196, PS-624 comments 13195/13197) - it is NOT the blocker.

| field | value |
| --- | --- |
| profile ID | `framework/halobox-same-gguf/vulkan/qwen38-flash-next-ud-iq4_xs@halo-box-29e091e` |
| host ID | `framework-strix-halo-gfx1151-ps636-recovered` |
| runtime | upstream-close `halo-box/llama.cpp` commit `29e091ea5b228ac1735cde369e68e6767a53e510`, **Vulkan** backend |
| artifact | Unsloth Qwen3.8-Flash-Next UD-IQ4_XS split; shard hashes 00001 `5ce89370720f8bf90890f439361282104c1aa1482d4013bb9a50923e758e71a4`, 00002 `577a38a2392b40ca2193cea502e1d92f60b8cd370675d308e0ec21885d9daaa7`, 00003 `d4634e6d84f0ebb0940be15c90d3790bf6464e3dea3a1cddc567dc0e83ad8833` |
| context / slots / batch | configured = served 262144, parallel 4, batch/ubatch 2048/512, FA on, ngl 999 |
| disposition | **QUALIFIED_EXPERIMENTAL** (intended role: conservative/reference profile where standardized prefill/depth evidence matters) |
| qualification controls | normal/strict-JSON, tools, parallel tools, streaming, continuation, no-tool, long-context, cancellation/client-disconnect, post-error health all PASS; malformed JSON returns HTTP 500 while health recovers (recorded API-status limitation) |
| standardized pp/tg (llama-bench r=5, no Ollama contention) | shallow 412.00/27.40; d4096 395.51/26.04; d16384 331.54/23.69; d32768 267.89/20.68 t/s |

**Live host baseline matches the sealed baseline**, verified this session over ssh:
`framework` reachable (tailscale active, `ssh` OK), kernel `6.19.10-300.fc44.x86_64`,
cmdline `iommu=pt amdgpu.gttsize=112640 ttm.pages_limit=28835840
ttm.page_pool_size=28835840`, GPU `Radeon 8060S/gfx1151` with GTT total 118111600640.
No Halogen or stale benchmark process is running; **Ollama is resident**
(`/bin/ollama serve` + a `llama-server -c 262144` child holding ~35.9 GB GTT), which is
the restored PS-624 teardown state.

## 2. The blocker, stated as four concrete gaps

**(G1) No profile-scoped qualified registry entry - routing refuses by design.**
`src/local_target_routing.py::persisted_routing_inputs` skips a host BEFORE reading
the store when its registry spec has an empty `qualification_ref`:

    if not str(spec.qualification_ref or "").strip():
        skipped.append({... "unqualified: no independently qualified profile for this
        host (research/Phase-0 metadata is not qualification)"})

`TARGET_FRAMEWORK` in `src/local_targets.py` is host-scoped with
`endpoint="http://127.0.0.1:11434"`, default `runtime_kind="ollama"` and
`qualification_ref=""`. There is no per-profile spec, and the receipt's own
`qualification_ref` is read from the SPEC, not from the store - so persisting a
receipt cannot make the profile selectable. PS-632 comment 13196 states the intent
explicitly: "No generic routable framework capability is created or retained."
(NOTE: that endpoint is the Framework **Ollama** service, not HaloBox.)

**(G2) No measurement path for a non-Ollama runtime.** `scripts/odysseus-capability
discover` resolves `--target` through `target_by_id(...)` over the registry and probes
with the Ollama inspector. There is no HaloBox target and no llama-server inspector, so
the fields PS-632 requires to be MEASURED (model resident, a proven native tool call,
streaming, served context) cannot be obtained for this profile at all.

**(G3) No runtime client in the benchmark harness.**
`scripts/ps635-live/ollama_client.py` is hardcoded:
`TARGETS = {"local-rtx4500": ("minipc", "qwen3.8:27b"), "local-msr1": ("msr1", "qwen3.8:27b")}`,
and `oc.target("local-framework")` raises `KeyError`. The dispatcher speaks Ollama's
`POST /api/chat` with `tools`; the HaloBox build at
`/mnt/framework-data/repos/halo-box/llama.cpp/build/bin` exposes **no Ollama-compatible
route** (no `/api/chat` or `/api/tags` in `tools/server`), so it needs an OpenAI
`/v1/chat/completions` client (plus a `runtime_state()` source such as `/props`).

**(G4) The sealed HaloBox evidence root is not readable by the operator account.**
`/mnt/framework-data/recovery/ps624-upstream-close-20260914` is `drwxr-x--- root root`;
`ls`/`du` return **Permission denied**. The GGUF shard hashes and binary-bundle identity
in section 1 therefore come from the sealed Jira record and could not be re-verified
from the evidence root itself without privilege elevation, which was not performed.

## 3. Why this is a stop and not something to work around

* Fabricating the receipt from prose would put DECLARED identity into a store that
exists to hold MEASURED identity - the exact failure class PS-632 was built to
eliminate. Explicitly forbidden by the slice instruction ("Do not invent missing
identity fields").
* Injecting a synthetic `specs=` list into `persisted_routing_inputs` to bypass the
qualification gate would be inventing registry policy outside the registry.
* Pointing the Ollama client at a HaloBox port would dispatch Ollama-protocol requests
to a llama-server endpoint; the only honest outcomes are an error or, worse, results
attributed to a profile that did not produce them.
* Creating the profile-scoped registry entry, the inspector and the client is NEW
DEVELOPMENT (a runtime adapter), and the slice boundary says not to start new
Framework qualification or runtime work here.

## 4. Minimal bounded unblocking path for the next slice

1. Add ONE profile-scoped registry entry (e.g. `local-framework-halobox`) with
`roles=(ROLE_INFERENCE,)`, `qualification_ref=` the PS-624 profile id above, the
HaloBox HTTP endpoint, and a non-Ollama `runtime_kind` - registry policy, not a probe.
2. Add a llama-server inspector to the PS-632 probe path so `discover` can MEASURE the
profile (native tool call, streaming, served context, health).
3. Add a llama-server client to the harness exposing the SAME interface the dispatcher
already uses (`api_streaming_chat`, `runtime_state`, `model`, `ssh_host`), leaving
prompts, tools, retry rules, verifier behaviour, G1 repair and G2 replan eligibility
untouched.
4. Launch HaloBox with the sealed configuration and Ollama stopped (no contention),
restore Ollama afterwards, and record teardown/GTT return - the PS-624 method.
5. Obtain readable access to the sealed evidence root (or re-seal the shard/binary
identity) so the receipt's artifact identity is bound to a verifiable object rather
than to a summary.

None of these five is a benchmark run, and none is a qualification rerun.

## 5. Boundary compliance and side observations

* No model task was executed; no preregistration was sealed (the gate is BEFORE the
first model call, so a sealed prereg for a cell that cannot dispatch would be a
document about nothing).
* No host tuning: GTT, IOMMU, Vulkan, memory limits, kernel, firmware and runtime flags
were read-only this session.
* Corpus contracts untouched. This is therefore NOT a `CORPUS_CONTRACT_PROBLEM`.
* RTX context note for future comparison arithmetic:
`RTX4500_REQUIRED_CONTEXT = 131072` (commit `6268074e`, "qualify RTX4500 131K routing
profile"), and the store now holds
`local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx131072:d94d964641c7`
(observed 2026-09-15T16:44:35Z). The sealed historical RTX benchmark used the 32K
profile `bad2e249330fd8c2...` and is NOT rewritten. Any future normalized comparison
would therefore be a DIFFERENT RTX profile identity, which must be declared as a
profile difference rather than folded into the historical numbers.
