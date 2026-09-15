# PS-579 preregistration — PS-639 paired RTX4500 observation

This artifact is sealed before the PS-639 G0/G1/G2 model calls. Outcomes are
recorded separately and do not rewrite these inputs or rules.

## Identity and source boundary

| field | value |
| --- | --- |
| Jira task | `PS-639` / `ps639-compose-drift` |
| packet | `PS639-compose-drift-rtx` |
| evaluation repository | `odysseus-github-ps579` |
| harness/evaluation commit | `cb78d991d90bf6087667029d8be4649d8b3bf3c7` |
| exact task base | `56ff059f8b5e24a6ba940ac5a8b90bb964e97a86` |
| source base state | detached exact-base comparator previously recorded clean; 5 passed / 3 failed |
| write scope | `docker-compose.gpu-nvidia.yml`, `docker-compose.gpu-amd.yml` only |
| read scope | `docker-compose.yml`, `docker/gpu.nvidia.yml`, `docker/gpu.amd.yml`, verifier |
| interface digest | `b1886a2ec92475f5` |
| verifier | `tests/test_gpu_compose_standalone.py` |
| verifier SHA-256 | `670a4152db3f87779c6092c582c108cef9b82676f693c4fe29c9cee06ff955cf` |
| hidden fixture | none separate; verifier is harness-owned and is not worker-visible as an implementation answer |

Each isolated arm starts from the exact base content for the two in-scope files.
The evaluation harness remains at the evaluation commit; relative to that
harness tree, only those two tracked files are restored to the base content in
each subject worktree. No staged, untracked, generated, or out-of-scope source
change is eligible for acceptance.

The canonical exact-base comparator is recorded in
`docs/local-targets/PS639-G1-PREREG.md`: source snapshot
`e3a18609fe197ea299c346d2f4b8d86e10800d14e4d0b2f117cdd52ff52aa905`, failure
fingerprint `b6551c73c3572df8`, VerificationReceipt
`3ef2221466d9210cf407f6d573b31a0499553c28ab00948099ecf50d7cfce41c`, and the
three expected failing nodes. This is expected base state, not a worker
failure, repair, or replan observation.

## Execution profile gate

| field | value |
| --- | --- |
| target/profile | `local-rtx4500` / `local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx32768:d94d964641c7` |
| PS-632 receipt | `4a69ce2d1063f2f9897a9335ee5edf4869a84aa6445da26e3136decd0e3d383b` |
| model digest | `d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28` |
| runtime/backend | Ollama `0.32.11` / CUDA |
| safe context | `32768` |
| context projection | the sealed packet's normal PS-635 worker projection; hash recorded per attempt |

The receipt was freshly resolved from `/home/tdefreest/scratch/ps632-store`
immediately before sealing. Material profile drift stops the block.

## Arms and scoring

The only changed variable is the orchestration arm. The same packet, exact base,
worker-visible context, deterministic verifier, permissions, source scope, and
execution receipt apply to every arm.

* **G0:** existing historically representative raw/local worker baseline, one
  normal worker attempt, then deterministic verification; no repair unless the
  frozen G0 definition already provides it.
* **G1:** frozen `WorkPacket -> fresh bounded context -> worker -> deterministic
  verifier -> compact repair evidence -> fresh retry only after a genuine
  qualifying technical failure`.
* **G2:** frozen G1 plus bounded validated replanning only at its eligible
  no-progress/repeated-failure boundary. A verifier PASS stops immediately with
  zero planner calls.

`ACCEPTED_CANDIDATE` requires the deterministic verifier to pass and the
PS-638 evidence chain to validate. The descriptive rate is
`accepted candidates / wall-clock hour`; it is not landed work/hour.

## Interpretation, retry, and stop rules

The red exact base is expected task state and does not count as a failed worker
attempt, repair, uplift, or G2 activation. First-attempt status is the verifier
result after the worker's first produced change. A repair requires a fully
specified packet, a genuine technical defect in that first worker change,
deterministic rejection, compact verifier-derived repair evidence, and a fresh
subsequent attempt. A replan requires the frozen G2 eligibility rule; ordinary
technical failure does not activate it.

Classify failures as `pre-existing/base defect`, `PACKET_INVALID`,
`CONTEXT_BLOCKED`, infrastructure/runtime, worker technical failure,
deterministic verifier failure, evidence-chain failure, no-progress, or
G2-eligible repeated/no-progress state. Infrastructure, packet, context, and
base defects are not repair observations.

Stop the block if the receipt materially drifts, the packet or verifier cannot
be reconstructed, the source/evidence binding fails, an out-of-scope write is
attempted, or frozen semantics would need changing. Preserve raw output and all
attempts. Do not manufacture failures, repairs, replans, or context pressure.

## Explicit hypotheses

1. A first-attempt PASS produces no G1 repair call.
2. A first-attempt PASS produces no G2 planner/replanner call.
3. G1/G2 happy-path overhead is measured rather than assumed; model-call and
   token neutrality is expected from the frozen semantics, while wall time is
   descriptive.
4. No repair-uplift or replan claim is made unless naturally observed.

## Required measurements

For each arm capture deterministic outcome, base state, first-attempt result,
final result, attempts, repairs, planner calls, model calls, prompt/completion
tokens, total and per-attempt wall time, verifier time, accepted-candidate
status, failure class, raw output, and the complete PS-638 chain:

`ExecutionPackage -> DispatchDecisionReceipt -> PS-632 receipt -> AttemptReceipt(s) -> VerificationReceipt(s) -> EvidencePackage -> validator verdict`.

The selected receipt must resolve with `receipt_ref_matches_store = true`.

After this block, report paired results with the two prior internal tasks and
stop. No Framework or external benchmark arm is part of this preregistration.
