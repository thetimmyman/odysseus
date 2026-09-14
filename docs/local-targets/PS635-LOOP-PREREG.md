# Pre-registration — first live run of the PS-635 bounded repair loop

Written BEFORE the run. Not edited afterwards except to append the outcome.

## The question

Slice A produced a real deterministic failure: on `local-msr1`, the worker wrote
`src/worker_context.py` reading packet keys `acceptance` / `stop_if`, while the
harness test used the real field names `acceptance_criteria` / `stop_conditions`.
Four of five tests passed; only the deterministic test caught the mismatch.

> Can the bounded loop, using ONLY a compact repair packet built from the
> deterministic failure, get a local worker to fix that mismatch — **without ever
> being told the input key names**?

## Setup (fixed before the run)

- Target: **`local-rtx4500`** (minipc, ollama 0.32.11, qwen3.8:27b) — chosen
  because a turn costs ~45 s, so the loop completes in minutes rather than hours.
  The model is the same family as the Slice A P2 run.
- Packet: **verbatim** the PS-632 P2 packet, including its UNDER-SPECIFIED contract
  (output labels only, input keys never named). That defect is the experiment.
- Verification: `tests/test_worker_context_ps632.py` — the harness-owned test,
  **byte-identical** to the one used in Slice A, and never shown to the worker.
- `max_attempts = 3`, one `write_file` tool, write scope `src/worker_context.py`.
- Every attempt gets a FRESH context: attempt 1 = the packet; later attempts = the
  compact repair packet only (no conversation replay).
- Base: `3cb7b7c1`. One worktree, `/home/tdefreest/worktrees/s7-loop`.

## Pre-registered predictions

1. **Attempt 1 FAILS**, reproducing the Slice A mismatch (the same assertion should
   fail: `first criterion` absent from the rendered output).
2. **Attempt 2 PASSES.** Prediction: the repair packet's failing-test id plus the
   assertion excerpt is ENOUGH for the worker to infer that it read the wrong keys.
3. **If attempt 2 fails**, the loop **escalates** rather than continuing when the
   failure fingerprint repeats, and never dispatches a third identical attempt.

## What would falsify the claim

- Attempt 2 fails and the loop escalates ⇒ the honest conclusion is *"a compact
  repair packet is NOT sufficient to recover an interface mismatch"*, recorded as a
  negative result. That is a legitimate outcome, not a failure of the experiment.
- Attempt 2 passes but the artifact was written outside the write scope, or the
  test file was modified, or the harness wrote the file ⇒ **not** a success.

## What this run does and does not prove

- It tests the LOOP MECHANISM (fresh context → deterministic verify → compact repair
  → retry) on one real failure, on one target.
- It does **not** establish any reviewer authority, any auto-approve class, or any
  general claim that local workers self-correct. The ledger's ceiling for a
  worker-driven run remains `ACCEPTED_CANDIDATE`.

---

## OUTCOME of experiment 1 (appended after the run)

Run `ps635-loop-demo-1`, target `local-rtx4500`, base `3cb7b7c1`.

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 FAILS | **CONFIRMED** — `1 failed, 4 passed`, same assertion |
| 2. attempt 2 PASSES | **FALSIFIED** — attempt 2 also failed |
| 3. on a repeated fingerprint the loop escalates, no third dispatch | **CONFIRMED** |

Actual result: `ESCALATE`, 2 attempts, 2 repairs recorded, no third dispatch.
Reason: `attempt 2 reproduced the same failure (7a0d195bae79f737); no progress`.
Ledger chain verified `(True, None)`; the verification test file was untouched.

Contexts handed to the worker: **2 135 chars (attempt 1) and 3 570 chars (attempt 2)**
— i.e. the repair attempt used a fresh, compact, repair-shaped context, never a
conversation replay. Attempt timings 31.5 s and 23.4 s.

### What attempt 2 actually did — and why it matters

The repair attempt produced *better-engineered* code than attempt 1 (docstrings,
`__all__`, a `_SECTIONS` table, `_as_text` / `_render_bullets` helpers) and still
failed the same assertion, because it guessed the input keys **again and
differently**: it kept `"acceptance"` and additionally invented a `"criteria"`
key, emitting a `CRITERIA: NONE` section the test does not want.

### Conclusion (per the pre-registered falsification clause)

> **A compact repair packet is NOT sufficient to recover an interface mismatch.**

And the diagnosis is precise, not vague: the repair packet faithfully carried the
SYMPTOM — the failing assertion shows the rendered output contained
`ACCEPTANCE: NONE` — but the worker cannot infer the ROOT CAUSE, because the root
cause is *which key the unseen test reads*, and that is exactly the information the
packet never contained. Two attempts, two different wrong guesses, same failure.

**Architectural consequence for PS-635:** the repair loop cannot substitute for
packet quality. A bounded repair loop over an under-specified packet converges on
**escalation**, not on success — which is the correct, safe behaviour (it stopped
instead of burning turns), but it means packet contracts MUST name their
interfaces, and a packet without a specified interface should be refused at
authoring time rather than dispatched and repaired.

---

## Experiment 2 — pre-registration (written BEFORE the run)

Hypothesis derived from experiment 1: the failure was caused by the under-specified
PACKET, not by the loop, the target or the repair packet.

**Change: exactly one thing** — the contract now names the packet's INPUT keys
explicitly (`objective`, `write_scope`, `acceptance_criteria`, `negative_control`,
`stop_conditions`, `max_chars`). Everything else is identical: same target, same
tools, same `max_attempts=3`, same harness-owned test the worker never sees, same
budget gate.

Predictions:

1. **Attempt 1 PASSES** (`1 passed`), reaching `ACCEPTED_CANDIDATE`.
2. **The loop stops after one attempt** — no repair packet is needed, and
   `repairs` is empty for the run.

Falsification: if attempt 1 still fails, the cause is NOT the packet's interface
specification and the diagnosis above is wrong.

## Still not claimed

Neither experiment creates reviewer authority, an auto-approve class, or a general
claim that local workers self-correct. The ledger ceiling for a worker-driven run
remains `ACCEPTED_CANDIDATE`; acceptance still requires a named non-local
authority.

---

## OUTCOME of experiment 2 (appended after the run)

Run `ps635-loop-demo-2`, target `local-rtx4500`, base `3cb7b7c1`, contract = the
EXPLICIT-interface variant.

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 PASSES | **CONFIRMED** — `5 passed` |
| 2. loop stops after one attempt, `repairs` empty | **CONFIRMED** — 1 attempt, 0 repairs |

Result: `ACCEPTED_CANDIDATE`, decision `stop`, reason `attempt 1 passed
deterministic verification`, one context handed to the worker (2 511 chars), ledger
chain `(True, None)`, verification test file untouched.

## The two experiments together

| | experiment 1 (contract under-specified) | experiment 2 (contract names its interfaces) |
| --- | --- | --- |
| attempt 1 | fail (`1 failed, 4 passed`) | **pass (`5 passed`)** |
| attempt 2 (repair) | fail — guessed again, differently | not needed |
| repairs recorded | 2 | **0** |
| terminal result | `ESCALATE` | `ACCEPTED_CANDIDATE` |
| worker contexts | 2 135 / 3 570 chars | 2 511 chars |

**One variable changed** between the two runs: whether the packet contract names
its input keys. Same target, same model, same runtime, same single `write_file`
tool, same harness-owned test the worker never sees, same budget gate, same
`max_attempts`.

### Causal conclusion

The Slice A failure was caused by the **under-specified packet**, not by the loop,
the target, or the repair mechanism. And the repair loop does **not** compensate for
a missing interface: over an under-specified packet it converges on **escalation**,
which is the safe outcome (it stopped rather than burning turns) but is not a fix.

### Architectural consequence for PS-635 (actionable)

1. **Packet contracts must name their interfaces.** A packet whose interface is
   unspecified should be **refused at authoring time**, not dispatched and
   repaired. That check belongs in the packet constructor, next to the existing
   fail-closed checks for empty `write_scope` / empty `test_command`.
2. **`failure_fingerprint` earned its place.** It converted "the retry failed" into
   "the retry failed the SAME way, so stop" — and it did so on the first pair of
   real attempts, without a human reading the logs.
3. **A negative result with a control is worth more than a green run.** Experiment 1
alone would have read as "local workers cannot repair"; experiment 2 shows the
   loop is sound and the *input* was defective.

### Limits of this evidence

- Two runs, one target, one model family, one task. This establishes a causal
  relationship for THIS defect class (missing interface specification); it does not
  generalise to arbitrary repair loops or to semantic (non-deterministic) failures.
- Nothing here creates reviewer authority or an auto-approve class. The ceiling for
  a worker-driven run remains `ACCEPTED_CANDIDATE`; acceptance still requires a
  named non-local authority.
