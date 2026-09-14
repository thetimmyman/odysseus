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
