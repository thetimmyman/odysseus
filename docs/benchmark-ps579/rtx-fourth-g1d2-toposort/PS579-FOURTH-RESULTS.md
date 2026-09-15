# PS-579 fourth distinct internal task: G1d2 topo sort

Run date: 2026-09-15. Branch: `ps-579-rtx-g0g1g2`, harness commit:
`1c0df6c1e3330dea3c9f1ed514ed0e1aa76ab9f0`.

Task: `G1d2-toposort-rtx`; exact base:
`2d88004c8d25712086cdfcac77cad9cc9e6f2671`; source SHA-256
`9d329bec8498492b4fc5f5d6767c1715da6222a18ce5418ecde027adef741d12`;
interface digest `fac6d37529c6b3ce`; verifier SHA-256
`3e8f84b3dbbbf9838d8abd5b18262887740aa535046ce331208139e4e6de7f5b`.
The exact base independently passed 11/11 tests. Worker-visible context hash
was `553b7778e9ecdaa6194c5e624f9f300bf3ffd8080fcb350efa5e09683a700939`
for all arms.

Profile: `local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx32768:d94d964641c7`;
PS-632 receipt ID `bad2e249330fd8c28bce3d50585ccecc95d98e9f959c85e7a25aeab8afe047a0`;
receipt hash bound by dispatch `c71c7a767d9a4737e70c8201b6224537baaff8bea7651003e2e55aeb84941538`.

| Arm | Result | Wall s | API s | TTFT s | Verifier s | Attempts | Calls | Prompt / completion | Accepted/hour |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| G0 | ACCEPTED_CANDIDATE | 17.269 | 15.716 | 15.715 | 1.157 | 1 | 1 | 655 / 470 | 208.45 |
| G1 | ACCEPTED_CANDIDATE | 15.888 | 13.830 | 13.828 | 1.199 | 1 | 1 | 655 / 470 | 226.59 |
| G2 | ACCEPTED_CANDIDATE | 15.449 | 13.857 | 13.856 | 1.163 | 1 | 1 | 655 / 470 | 233.18 |

All arms passed first attempt, 11/11 deterministic tests, with zero repairs,
replans, and planner calls. Receipt binding and PS-638 validation were true
for every arm; no typed refusal occurred during the block.

Evidence package SHA-256 and run directory:

- G0 `5c95ae1cfcdeec6799bd5ca846b95703f6d19cca8d50759febc37fd2c712b462`, `g0/g1d2-toposort-20260915T155735Z`
- G1 `81692d09a2eb7aadd72a9bb35b6d16cb659a8db86a3ee435ed708b70d60e3d35`, `g1/g1d2-toposort-20260915T155801Z`
- G2 `4acd89c78d9b104a1b1fbd241da4c807f2c97351965c7c4e89a1eeae70a12a6f`, `g2/g1d2-toposort-20260915T155826Z`

## Cumulative distinct-task result

Balanced PS-639 timing repetitions are excluded as controls, not tasks.
Across four distinct internal tasks, each arm has 4/4 accepted, 4/4 first
attempt passes, 4 attempts, 4 model calls, 12,455 prompt tokens, and 4,365
completion tokens. Total wall and descriptive accepted-candidate work/hour:

| Arm | Total wall s | Accepted-candidate/hour |
| --- | ---: | ---: |
| G0 | 249.758 | 57.66 |
| G1 | 251.695 | 57.21 |
| G2 | 395.354 | 36.42 |

Task-level wall (G0/G1/G2 seconds): G1e bounded cache (21.128/19.376/19.629);
l1-interface (11.847/10.432/10.278); PS-639 Compose drift
(199.514/205.999/349.998); G1d2 topo sort (17.269/15.888/15.449).

No repair or replan observation occurred. Repair uplift, post-repair PASS,
no-progress to validated replan, manager regression, truncation recovery, and
semantic-review escape remain UNOBSERVED. Local Qwen semantic review remains
flag-for-human-read only.

The four tasks provide materially different shapes: cache state machine,
ledger/interface rendering, multi-file Compose configuration, and deterministic
graph ordering. Contracts and evidence are sound. Decision:
`FREEZE_INTERNAL_CORPUS_FOR_RUNTIME_COMPARISON`.
