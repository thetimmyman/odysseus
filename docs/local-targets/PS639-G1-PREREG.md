# Pre-registration — PS-639: the first real G1 candidate

Written and committed BEFORE the live run. Outcomes are appended, never rewritten.

## Why this is the G1 case

Every previous G1 attempt (G1a/G1b/G1c/G1d2/G1e) was one-shotted, and the recorded
conclusion was that difficulty *inside a self-contained bounded packet* is not what
produces a repair case. PS-639 is a different axis:

* the defect is **naturally occurring**, not injected — it is already red on the
  authoritative base and was found by the project's own suite;
* it edits **existing multi-file source** rather than creating a fresh module;
* it has a **pre-existing authoritative verifier** (`tests/test_gpu_compose_standalone.py`)
  that nobody in this lane wrote or may weaken.

The defect: after `docker-compose.yml` gained three Pi execution-plane variables,
the two standalone GPU Compose files — which explicitly claim equivalence to
base+overlay — were not regenerated.

## Exact-base comparator (sealed BEFORE any model call)

Run on a detached worktree at `56ff059f`, not inferred from "our files did not
change". Artifacts: `/tmp/ps639-base-baseline/` (receipt + probe + captured
stdout/stderr).

| | |
| --- | --- |
| tree | `/tmp/ps639-base`, detached HEAD `56ff059f8b5e…` |
| source snapshot digest | `e3a18609fe197ea299c346d2f4b8d86e10800d14e4d0b2f117cdd52ff52aa905` (disposition `clean`) |
| verifier | `tests/test_gpu_compose_standalone.py`, digest `670a4152db3f8777…` |
| result | exit **1**, `FAIL`, **5 passed / 3 failed** |
| failure fingerprint | `b6551c73c3572df8` |
| VerificationReceipt hash | `3ef2221466d9210cf407f6d573b31a0499553c28ab00948099ecf50d7cfce41c` |
| failing nodes | `test_nvidia_standalone_equals_base_plus_overlay`, `test_amd_standalone_equals_base_plus_overlay`, `test_amd_odysseus_adds_only_overlay` |

No EvidencePackage was sealed for the comparator, deliberately: a package whose
closure has verification receipts and no attempt is rejected by this project's own
validator (`no_attempts`), and correctly so — a comparator is not a run.

## Packet

| | |
| --- | --- |
| packet_id | `PS639-compose-drift-rtx` |
| jira | PS-639 |
| base | `56ff059f` |
| write scope | `docker-compose.gpu-nvidia.yml`, `docker-compose.gpu-amd.yml` — exactly two files, both existing |
| read scope | `docker-compose.yml`, `docker/gpu.nvidia.yml`, `docker/gpu.amd.yml`, the verifier |
| interface | the two exact target paths (required, `path`) — digest `b1886a2ec92475f5` |
| verifier | `pytest -q tests/test_gpu_compose_standalone.py` |
| target | `local-rtx4500` / `minipc`, ollama 0.32.11, `qwen3.8:27b`, `num_ctx` 32768 |
| tools | one `write_file`, path restricted to the write scope; **multi-file** (the dispatcher now keeps calling until both files are written) |
| output budget | 7000 tokens/round — each file is ~8.5 KB, and the default cap would truncate a faithful rewrite |
| attempts | `max_attempts=3`, fresh context per attempt |

### What the worker is shown, and what it is not

The write scope is EXISTING files and the tool can only write, so the worker would
never see what it is editing. The context therefore carries both current files plus
the three read-scope references, all inside the context projection (so the
projection hash covers exactly what was shown, and the validator checks the sealed
interface appears in it).

It does NOT carry the answer. `preflight` refuses the run if the merged environment
list — base entries immediately followed by the overlay additions — appears
contiguously anywhere in the context. The contract describes the merge; it must not
perform it.

### The trap, stated in the contract

`environment` is a **list**, and the verifier compares lists, so the missing
variables must land at the position `docker-compose.yml` puts them. Appending them
to the end of the environment block produces a mapping with the same members and
still FAILS. That is stated in the contract, so a miss is a defect, not an
ambiguity.

## Pre-registered predictions

1. **Attempt 1 FAILS deterministically** on at least one of the three baseline red
   tests — most likely `test_nvidia_standalone_equals_base_plus_overlay` /
   `test_amd_standalone_equals_base_plus_overlay`, with the environment list
   differing by order or membership.
2. If it fails, the repair packet carries the SAME `interface_digest`
   (`b1886a2ec92475f5`) and the SAME contract verbatim, plus bounded deterministic
   failure evidence.
3. **Attempt 2 PASSES** → `ACCEPTED_CANDIDATE`, 2 attempts, 1 recorded repair, the
   sealed package VALIDATES, and it still contains BOTH attempts with the order
   change described.
4. If attempt 2 fails with the same fingerprint → **ESCALATE**, no third dispatch,
   and the outcome is reported as a negative result.

Also recorded either way: the verifier digest must be the same at verification time
as the one sealed in the plan (`670a4152db3f8777…`), so the test file cannot have
been touched.

## Interpretation

| outcome | meaning |
| --- | --- |
| attempt 1 passes | another one-shot, and the honest reading is that this defect class does not produce a repair case either |
| attempt 1 fails, attempt 2 passes | **G1 SUCCESS** — a genuine implementation defect corrected from compact evidence without changing the contract, interface or acceptance |
| attempt 2 fails, escalate | negative result: the repair packet is insufficient for this defect class |

## Explicitly not in scope

No G2 work. No Framework, MS-R1, runtime tuning, UI, or unrelated Jira. The base or
overlay files must not be edited: if the fix appears to require that, the packet's
stop condition fires instead.
