# PS-579 preregistration — PS-639 balanced-order timing control

Sealed before every repeat model call. This controls the unexplained PS-639 G2
wall-time observation without changing task content, verifier, model, routing,
or frozen G0/G1/G2 semantics.

## Fixed identity

| field | value |
| --- | --- |
| task/packet | `ps639-compose-drift` / `PS639-compose-drift-rtx` |
| exact base | `56ff059f8b5e24a6ba940ac5a8b90bb964e97a86` |
| evaluation commit | `46c0c46fe2931b6886d45ea4e3a5bb9f818ac0ad` |
| interface digest | `b1886a2ec92475f5` |
| verifier | `tests/test_gpu_compose_standalone.py` |
| verifier SHA-256 | `670a4152db3f87779c6092c582c108cef9b82676f693c4fe29c9cee06ff955cf` |
| write scope | `docker-compose.gpu-nvidia.yml`, `docker-compose.gpu-amd.yml` only |
| target | `local-rtx4500` / RTX4500 / Ollama `0.32.11` / CUDA / `qwen3.8:27b` |
| model digest | `d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28` |
| measured-safe context | `32768` |
| preregistration receipt gate | `0e51432086544cb57c01e4d823df907e3fd3acc2d923dfa736c9b03d6bf94f2c` |

The receipt gate is fresh from `/home/tdefreest/scratch/ps632-store`. A fresh
receipt may legitimately differ by hash between ordered sets, but material
profile identity must remain unchanged. Any material drift stops the control.

## Changed variable and ordered sets

The only changed variable is execution order / orchestration arm. Exactly three
sets are preregistered, with each arm appearing once in each position:

| set | position 1 | position 2 | position 3 |
| --- | --- | --- | --- |
| A | G0 | G1 | G2 |
| B | G2 | G0 | G1 |
| C | G1 | G2 | G0 |

Every individual run uses a fresh isolated subject worktree whose two target
files contain the exact canonical base content. No run inherits another arm's
implementation. The same packet, worker-visible context, permissions, verifier,
runtime profile class, and context projection apply to all cells.

## Frozen arms and stop rules

* G0 is the existing historically representative raw/local worker baseline.
* G1 is frozen `WorkPacket -> fresh bounded context -> worker -> deterministic
  verifier -> repair only after genuine qualifying technical failure`.
* G2 is frozen G1 plus bounded validated replanning only at its eligible
  no-progress/repeated-failure boundary. PASS stops with zero planner calls.

Do not manufacture failures, repairs, replans, no-progress, context pressure, or
runtime faults. Preserve every attempt and the complete PS-638 chain. Stop a
cell or the experiment for receipt material drift, packet/verifier mismatch,
source contamination, evidence-chain failure, or any need to change frozen
semantics.

## Lightweight timing/state capture

The benchmark-only runner records, immediately before each model API request,
one inexpensive SSH snapshot containing timestamp context, `/proc/loadavg`,
Ollama `/api/ps` loaded-model state, and best-effort `nvidia-smi` name,
utilization, and memory usage. Probe duration is recorded separately and is not
included in model/API duration. No high-frequency profiling is used.

For every cell record total wall, pre-model/routing wall where available,
receipt observation, model/API wall, each model round independently, TTFT,
generation timing where derivable, verifier wall, evidence/validation wall,
residual, runtime snapshots, calls, tokens, attempts, planner calls, selected
receipt, source scope, and validator result.

## Analysis rules

Report descriptive arm-level and position-level n=3 means, medians, ranges,
paired cell values, and complete 3x3 matrix. Accepted-candidate/hour remains a
descriptive rate, not landed work/hour.

Classify the result only as:

* `ORDER_EFFECT_SUPPORTED` if latency tracks position more strongly than arm;
* `ARM_EFFECT_SUPPORTED` only if an arm remains consistently slower across all
  balanced positions and the stage is reproducible;
* `RUNTIME_VARIANCE` if timing varies without consistent arm or position pattern;
* `INSUFFICIENT_EVIDENCE` if n=3 cannot distinguish the possibilities.

Model/runtime latency remains separate from orchestration CPU/control-plane
overhead. No formal significance claim is authorized at n=3. Repair uplift,
post-repair PASS, no-progress-to-replan, manager regression, truncation
recovery, and semantic-review escape remain `UNOBSERVED` unless naturally
triggered.

## Hypothesis

The prior PS-639 G2 anomaly is runtime/model-request latency observed in
sequential order. Balanced position assignment is expected to distinguish an
order effect from arm-specific latency and ordinary runtime variance; it is not
expected to create a G2 planner call or change deterministic outcomes.
