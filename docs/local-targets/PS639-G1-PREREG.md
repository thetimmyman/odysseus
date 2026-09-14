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

---

## OUTCOME of PS-639 (appended after the run)

Run `ps639-compose-drift-20260914T180006Z`, target `local-rtx4500`, base `56ff059f`.

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 FAILS on at least one baseline-red test | **FALSIFIED** — attempt 1 passed `8 passed` |
| 2. repair packet carries the same interface digest | did not occur |
| 3. attempt 2 passes → 2 attempts, 1 repair | did not occur |
| 4. repeated fingerprint → escalate | did not occur |

Result: `ACCEPTED_CANDIDATE`, **1 attempt, 0 repairs**, 1 model call, **236.6 s**,
two `write_file` calls (one per file), 9 849 prompt / 2 953 completion tokens,
`8 passed`, control `True`, package **VERIFIED** with all five requirements
`SATISFIED`.

```
package_hash          ebe4b76b481a3a20aaf559449085e789e99a9f7cc9f624945d437c72fd564606
dispatch_receipt_hash 259ffaf0a72ce0115922cc1375e4106aa6409967688f02aa24159797811b5fb4
evidence_package_hash 214ce2440cb60084cdf3041b7acb023de5d692cfb37bc99f674504799ceb331a
validator             VERIFIED, no named reasons
attempt               writes ['docker-compose.gpu-nvidia.yml', 'docker-compose.gpu-amd.yml']
interface_digest      b1886a2ec92475f5   (unchanged across the run)
verifier_digest       670a4152db3f87779c6092c582c108cef9b82676f693c4fe29c9cee06ff955cf
```

### The defect is genuinely fixed (checked independently of the verifier)

* `git status --porcelain` lists **only** the two in-scope files; the verifier file's
  digest is byte-identical to the one sealed in the plan, so it was not touched.
* The diff is **20 insertions and 0 deletions** — three variables and their carried
  comments, inserted in both files **at the position `docker-compose.yml` puts them**
  (after `ODYSSEUS_SCRIPT_HOST`, before `ODYSSEUS_CHAT_UPLOAD_MAX_BYTES`). The
  predicted shortcut — appending at the end — did not happen.
* Re-derived with an independent merge written for this check (not the test): both
  standalone files now parse to **exactly** `base + overlay`, and each file's
  environment begins with the base list in base order.

`tests/test_gpu_compose_standalone.py` at this head: **8 passed** (was 3 failed /
5 passed at base).

### G1 is STILL not satisfied, and this document says so

Prediction 1 was falsified, so no repair cycle occurred and **no G2 work may start**.
This is now the **sixth** fully-specified packet to be fixed on the first attempt —
and the first on the axis that was supposed to be harder: existing multi-file source
rather than a fresh self-contained module. The earlier task-shape conclusion
("difficulty inside a self-contained bounded packet is not what produces a repair
case") does not survive contact with this result as stated; the honest revision is
narrower and less convenient:

> On this target, packet SIZE and FILE COUNT also did not produce a first-attempt
> defect. A 31 k-character context, two 8.5 KB artifacts, and a stated ordering trap
> were all handled in one pass.

What has NOT been tested is the remaining axis: a packet that exceeds what one fresh
bounded context can hold, or a task carrying genuine ambiguity about *which* change
is wanted. Those are the shapes worth trying next — not another harder rule set.
Manufacturing a failure remains off the table.

### Side effect worth recording

The three failures this lane had been reporting as "known, not caused by this branch"
are now **fixed by this run** — and the fix came from the worker, verified by the
repo's own pre-existing test.

Full repository suite at this head: **3797 passed, 3 skipped, 0 failed** (272.5 s).
At `83556dfa` it was 3794 passed / 3 failed / 3 skipped, and the 3 were exactly these.

The earlier PS-638 write-up deliberately refused to call those failures "pre-existing"
without an exact-base receipt, and offered only a name-diff argument instead. That
argument is now moot in the strongest way available: the exact-base comparator WAS
produced for this ticket (`b6551c73c3572df8` at `56ff059f`), and the failures no
longer exist to classify.


