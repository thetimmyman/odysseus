# PS-579 RTX4500 131K reconciliation

Date: 2026-09-15

## Scope

This is a qualification and local-Pi usability slice. It does not run the
Framework comparison and does not change frozen G0/G1/G2 behavior.

## Canonical lineage

- branch: `ps-579-rtx-g0g1g2`
- dispatch fixture fix: `b5632c715e6face9b178a2fb795529d544b34419`
- independent source fix inspected: `de260ae825b2b2a0539c75aeae06cb0396538e1e`
- full suite after the fixture fix: `3956 passed, 3 skipped, 0 failed`

The 17 dispatch-boundary failures were caused by a test-only receipt seeded
from wall clock (`utcnow() - 30 minutes`) while the decision clock was pinned
to `2026-09-15 12:00 UTC`. After wall clock passed 12:30 UTC, the receipt was
future-dated relative to the decision clock and correctly refused as stale.
The fixture now derives `SEEDED_AT` from the pinned `NOW`. The regression test
passed in three separated invocations; production routing was not changed by
that fix.

## RTX profile

The live host is `tacticusanalytics-1` (`tacticusanalytics`) with Ollama
`0.32.11`, CUDA, model `qwen3.8:27b`, digest
`d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28`, and
`OLLAMA_NUM_PARALLEL=1`. The resident server was started with `-c 131072 -np
1`; `/api/ps` reported `context_length=131072`, and the resident model's VRAM
size was `22647154932` bytes. The host showed no OOM or restart and 100% GPU
residency was retained.

The distinct PS-632 profile is:

`local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx131072:d94d964641c7`

Receipt hash:

`e2c195a3c791d1da7b315768a0c75b381492762477220b90385e9d4022ff0daf`

Identity digest:

`1c232429429fefb28d403d826cd6c0d33454042c6bf7552b7c19f3110559f8f6`

The store verifies cleanly with 42 receipts and preserves both the historical
`ctx32768` profile and the new `ctx131072` profile. Persisted routing now
requires configured, served, and measured-safe RTX context to equal exactly
131072; a lower receipt is refused rather than used as a fallback.

## Context-integrity ladder

The fixture placed retrieval markers at the beginning, middle, and end and
required the exact three-marker response. Results below used `think=false`,
`num_ctx=131072`, and deterministic decoding. The 32K control produced 13,905
measured prompt tokens (a conservative control, not a claim of 32K depth).

| nominal level | measured prompt tokens | result |
| --- | ---: | --- |
| 32K control | 13,905 | PASS; all markers retained |
| 64K | 64,470 | PASS; all markers retained |
| 96K | 96,670 | PASS; all markers retained |
| 128K | 128,875 | PASS; all markers retained |

Measured safe context for this qualification is `131072` (the 128,875-token
integrity probe is the deepest completed marker test). No CPU offload, OOM, or
restart was observed.

## Pi configuration and proof

Changed file: `/home/tdefreest/.pi/agent/models.json`.

The RTX entry is provider `local-qwen-rtx4500`, model `qwen3.8:27b`, base URL
`http://tacticusanalytics-1:11434/v1`, display name
`Qwen3.8 27B — RTX4500 — 131K`, `contextWindow: 131072`, and `maxTokens:
32768`. The Framework entry remains separate and unchanged.

In Pi 0.85.1, `/models` is the model-configuration screen that exposes both
provider-qualified entries; the actual session switch is then performed by
the model selector. The live TUI displayed the RTX name and switched the
footer to `0.0%/131k (auto) (local-qwen-rtx4500) qwen3.8:27b`.

A fresh RTX `hi` request returned successfully with `33179` input and `210`
output tokens before optional-package cleanup. After removing unrelated
external-service, web, browser, cache, task, and background packages from the
default settings, retaining diagnostics, permission, edit-safety, and
Heimdall safeguards, the fresh structured probe measured `11378` input and
`75` output tokens. A read-only coding analysis using Pi's `read` tool also
completed successfully against the RTX provider and reported the exact
131K routing invariant.

The prior 101K-input interactive observation was from a reused session with
high-thinking state and is not treated as the fresh-start baseline.

## Comparison taxonomy

Future results must keep these dimensions separate:

1. orchestration comparison: G0/G1/G2 on the same execution profile;
2. execution-profile comparison: RTX 131K versus each qualified Framework
   profile;
3. runtime-isolation comparison: only where model artifact and relevant
   runtime inputs are held materially constant;
4. model/artifact differences: RTX Ollama Qwen3.8:27B versus Framework
   HaloBox/Halogen Flash-Next artifacts.

HaloBox Same-GGUF and Halogen Same-GGUF are comparable to each other as
same-GGUF Framework arms. That label does not make either Framework artifact
identical to the RTX Ollama artifact. No Framework benchmark was run in this
slice.
