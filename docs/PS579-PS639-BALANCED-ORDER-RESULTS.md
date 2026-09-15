# PS-579 PS-639 balanced-order timing control results

This report covers the preregistered three-set control. The original PS-639
evidence was not overwritten. Set A's first G2 dispatch was preserved as a
zero-call liveness refusal; the valid Set A position-three G2 cell was rerun
once after a fresh receipt gate.

## Identity

| field | value |
| --- | --- |
| branch | `ps-579-rtx-g0g1g2` |
| execution commit | `2a6846bd13c7918750a911b0a678e2dc68a85aa0` |
| task/base | `PS639-compose-drift-rtx` / `56ff059f8b5e24a6ba940ac5a8b90bb964e97a86` |
| preregistration | `docs/PS579-PREREG-PS639-BALANCED-ORDER.md` |
| preregistration SHA-256 | `d4c2f6cc2bc9bd122759c52affc743f8b2b94885f4b4edd4ee10dc8432be3f73` |
| receipt gate | `0e51432086544cb57c01e4d823df907e3fd3acc2d923dfa736c9b03d6bf94f2c` |

All nine valid cells used the same material RTX4500/Ollama 0.32.11/CUDA/Qwen
digest and safe context 32768. Receipt hashes legitimately refreshed between
cells; every selected receipt was present in the canonical store and every
chain reported `receipt_ref_matches_store=true`.

## Complete 3x3 matrix

Values are `total / model-API / TTFT round 1 / TTFT round 2`, in seconds.

| set | position 1 | position 2 | position 3 |
| --- | --- | --- | --- |
| A: G0 -> G1 -> G2 | G0 `182.816 / 180.942 / 2.275 / 85.866` | G1 `201.408 / 199.734 / 1.156 / 106.106` | G2 `183.142 / 181.493 / 2.247 / 86.722` |
| B: G2 -> G0 -> G1 | G2 `182.490 / 180.774 / 2.358 / 86.018` | G0 `182.172 / 180.545 / 2.381 / 86.094` | G1 `180.541 / 178.945 / 2.316 / 85.360` |
| C: G1 -> G2 -> G0 | G1 `183.795 / 182.521 / 2.347 / 87.524` | G2 `184.683 / 183.354 / 2.583 / 87.476` | G0 `184.488 / 183.275 / 2.556 / 87.158` |

All nine cells were first-attempt `ACCEPTED_CANDIDATE`, one model call, zero
repairs, zero replans, zero planner calls, `8 passed`, and validator `VERIFIED`.

## By orchestration arm (n=3)

| arm | total mean / median / range | API mean / median / range | TTFT1 mean | TTFT2 mean | verifier mean | accepted |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| G0 | 183.159 / 182.816 / 182.172–184.488 | 181.587 / 180.942 / 180.545–183.275 | 2.404 | 86.373 | 1.191 | 3/3 |
| G1 | 188.581 / 183.795 / 180.541–201.408 | 187.067 / 182.521 / 178.945–199.734 | 1.940 | 92.997 | 1.138 | 3/3 |
| G2 | 183.438 / 183.142 / 182.490–184.683 | 181.874 / 181.493 / 180.774–183.354 | 2.396 | 86.739 | 1.190 | 3/3 |

G1's mean is elevated by one 201.408s observation. G2 does not remain slower
than G0 across the balanced positions.

## By execution position (n=3)

| position | total values / mean | API values / mean | TTFT1 mean | TTFT2 mean |
| --- | --- | --- | ---: | ---: |
| 1 | 182.816, 182.490, 183.795 / **183.034** | 180.942, 180.774, 182.521 / **181.412** | 2.327 | 86.469 |
| 2 | 201.408, 182.172, 184.683 / **189.421** | 199.734, 180.545, 183.354 / **187.878** | 2.040 | 93.225 |
| 3 | 183.142, 180.541, 184.488 / **182.724** | 181.493, 178.945, 183.275 / **181.238** | 2.373 | 86.413 |

The position-two elevation is caused by the single Set A G1 cell, not by G2:
position-three G2 was 183.142s and position-two G2 was 184.683s.

## Runtime-state observations

Every pre-request snapshot showed the same loaded model digest and context
length 32768, GPU utilization 0%, and VRAM approximately 19,068/32,623 MiB.
Observed first-load averages ranged from 1.56 to 4.61; the highest was Set B G2,
which still completed in 182.490s. No simple host-load or GPU-residency pattern
explains the prior 349.998s G2 result.

## Liveness boundary event

The original Set A G2 dispatch at `20260915T145904Z` was refused before model
execution because the short PS-632 liveness state had expired after the long
G0/G1 sequence. It produced zero model calls and was not counted as a timing
cell. A fresh receipt was resolved and the third-position G2 cell was then run
from a new exact-base worktree. This confirms that receipt freshness is a real
control-plane boundary, but it does not explain the prior model/API latency
anomaly.

## Classification

`RUNTIME_VARIANCE`

The prior sequential anomaly remains correctly classified as runtime/model
request latency observed in that order. The balanced control does not support an
arm effect or position effect: G2 is normal in all three positions, and the
single high cell belongs to G1. The original +150s delay was not reproduced.

No repair or replan occurred. Repair uplift, post-repair PASS,
no-progress-to-replan, manager regression, truncation recovery, and
semantic-review escape remain `UNOBSERVED`.

## Evidence root

Complete independent chains are under:

`docs/benchmark-ps579/rtx-ps639-balanced-order/`

The nine EvidencePackage hashes are listed in the PS-579 Jira update and in the
per-cell `run_summary.json` files under that root.
