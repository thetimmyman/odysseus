# PS-579 preregistration: fourth internal task, RTX4500 G0/G1/G2

Status: sealed before any model call
Date: 2026-09-15
Branch at sealing: `ps-579-rtx-g0g1g2`
Harness commit at sealing: `bc71ecedc15c7d20f8c24e012f69d7381ef96dff`

## Candidate inventory and selection

| Packet/task | Source/base | Shape | Scope | Verifier / size | Historical state |
| --- | --- | --- | --- | --- | --- |
| `G1-interval-merge-rtx` | `e98c0877` | interval algorithm | `src/interval_merge.py` | `tests/test_interval_merge_ps635.py`, 10 tests, ~1 file | historical PASS and patch |
| `G1b-window-stats-rtx` | `90b50ec0` | windowed data transformation | `src/window_stats.py` | `tests/test_window_stats_ps635.py`, 11 tests, ~1 file | historical PASS and patch |
| `G1c-verifier-evidence-rtx` | `0a8a7af2` | parser/evidence transformation | `src/verifier_evidence.py` | `tests/test_verifier_evidence_ps635.py`, 8 tests, ~1 file | historical PASS and patch |
| `G1d2-toposort-rtx` (selected) | `2d88004c8d25712086cdfcac77cad9cc9e6f2671` | deterministic graph algorithm | `src/topo_sort.py` | `tests/test_topo_sort_ps635.py`, 11 tests, ~1 file | historical corrected verifier; source-bound PASS |

The selected packet adds graph/data-structure diversity to the existing cache
state-machine, ledger rendering, and Compose configuration tasks. It has a
fully specified interface, deterministic independent verification, one-file
write scope, and fits the measured-safe context. Historical output is not part
of the worker-visible context.

## Canonical task and source contract

- task and packet: `G1d2-toposort-rtx`
- repository: `odysseus-github-ps579`
- exact base: `2d88004c8d25712086cdfcac77cad9cc9e6f2671`
- base source state: clean detached worktree; `src/topo_sort.py` SHA-256
  `9d329bec8498492b4fc5f5d6767c1715da6222a18ce5418ecde027adef741d12`
- intended write scope: `src/topo_sort.py` only
- read scope: none
- interface digest: `fac6d37529c6b3ce`
- verifier: `tests/test_topo_sort_ps635.py`
- verifier SHA-256: `3e8f84b3dbbbf9838d8abd5b18262887740aa535046ce331208139e4e6de7f5b`
- hidden fixture: none separate; the verifier remains harness/operator-owned
- baseline contract: exact-base deterministic verification `11 passed`
- expected score: all verifier tests pass, source remains within scope, and the
  PS-638 evidence chain validates; otherwise classify the exact failure.

The worker sees only the normal packet contract and interface projection. The
historical implementation, diff, prior model output, and verifier source are
excluded from worker-visible context. The exact worker-visible projection hash
is recorded in each run evidence.

## Runtime and receipt gate

Execution target: `local-rtx4500` / RTX4500 / Ollama `0.32.11` / CUDA /
Qwen3.8:27B; model digest
`d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28`;
configured, served, and measured-safe context `32768`.

Receipt resolved immediately before execution:

- profile: `local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx32768:d94d964641c7`
- receipt hash: `bad2e249330fd8c28bce3d50585ccecc95d98e9f959c85e7a25aeab8afe047a0`
- store: `/home/tdefreest/scratch/ps632-store`
- observed: `2026-09-15T15:56:05.722994+00:00`

Before each arm, liveness is checked. If the short liveness window has
expired, dispatch is refused and the existing legitimate PS-632 refresh path
is used. The refusal and refresh remain separate evidence and are not counted
as model or orchestration failure. A refreshed receipt must retain the same
material identity before dispatch.

## Arms and hypotheses

Changed variable: orchestration arm only. Source/base, packet, interface,
permissions, verifier, model/runtime identity, context projection, and write
scope are held materially fixed.

- G0: existing historically representative raw/local worker baseline.
- G1: frozen WorkPacket -> fresh bounded worker context -> worker attempt ->
  deterministic verifier -> compact repair only after a genuine qualifying
  technical failure; PASS-first-attempt stops immediately.
- G2: frozen G1 plus bounded validated replanning only at its eligible
  no-progress/repeated-failure boundary; PASS-first-attempt has zero planner
  calls.

Hypotheses:

1. A fully specified implementation should be accepted when its first attempt
   passes; a red or wrong implementation is classified by deterministic
   verification, not by task selection.
2. G1 and G2 add no repair/replanner calls on a PASS-first-attempt path.
3. Correctness and evidence validity are primary; accepted-candidate/hour is
   descriptive and is not landed-work/hour.
4. No repair-uplift or replan claim is made unless naturally observed.

## Scoring, retry, and taxonomy

For each arm record first-attempt result, final result, attempts, repairs,
replans/planner calls, model calls, prompt/completion tokens, total wall,
model/API wall, TTFT where available, verifier wall, residual, and evidence
validity. `ACCEPTED_CANDIDATE` requires deterministic acceptance, in-scope
source state, and a valid PS-638 chain with `receipt_ref_matches_store=true`.
Accepted-candidate/hour is `accepted candidates / wall hours`, reported only
descriptively.

G0 uses its preregistered baseline behavior. G1 repairs only after a genuine
fully specified technical first-attempt failure. G2 replans only after the
frozen eligibility rule is met. No failure, repair opportunity, no-progress,
context failure, or malformed packet is manufactured. Every attempt is
preserved; a later PASS never overwrites an earlier failure.

Classify outcomes as: pre-existing/base defect (expected task state),
`PACKET_INVALID`, `CONTEXT_BLOCKED`, infrastructure/runtime failure, worker
technical failure, deterministic verifier failure, evidence-chain failure,
no-progress, or G2-eligible repeated/no-progress state. A pre-existing red
base is not a worker failure or repair observation.

Stop if material runtime identity drifts, the packet cannot be reconstructed,
the verifier or receipt is ambiguous, the evidence chain cannot bind, or the
task semantics would need changing. Do not alter frozen G0/G1/G2 semantics.

## Scope boundary

This is exactly one fourth distinct internal task. Do not run a fifth task,
Framework comparison, external benchmark, PS-578 landing, challenger,
context-window tuning, runtime tuning, or new G1/G2 behavior in this slice.
