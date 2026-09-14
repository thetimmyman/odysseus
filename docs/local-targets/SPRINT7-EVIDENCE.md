# Sprint 7 evidence — two-target concurrency (Slice A) and context calibration (Slice B)

Base: `da4c4357` (PS-632 slice 1, off `56ff059f`). Branch
`work/ps-632-local-target-registry`, worktree `/home/tdefreest/worktrees/ps632`.

Harness (evidence tooling, NOT shipped): `~/scratch/sprint7/`
(`ollama_client.py`, `slice_a.py`, `slice_b.py`, `probe_tools.py`,
`probe_overflow.py`). Raw reports, ledgers and artifacts under
`~/scratch/sprint7/{slice-a,slice-b,evidence}/`.

## Why the harness had to be fixed mid-flight

The first Slice A run reported `P2 FAIL` after 1 003.9 s with the worker "never
calling the tool", while the runtime reported 874 and 963 generated tokens. A
second, independent probe (`probe_tools.py`) on the same host, same tool schema,
same prompt showed the tool call WAS returned — in the FIRST stream chunk, with
the terminal chunk carrying an empty message. The harness read only the final
chunk.

So the first result was a FALSE NEGATIVE ABOUT THE MODEL CAUSED BY THE READER. It
was discarded, the reader was fixed to accumulate `content`/`tool_calls` across
chunks, and the run was repeated. Both the failed run and its ledger are kept in
`~/scratch/sprint7/slice-a-run1-parserbug/`.

Runtime-specific streaming shapes (measured, both targets):

| target | runtime | streaming tool call arrives in |
| --- | --- | --- |
| `local-rtx4500` (minipc) | ollama 0.32.11 | the single `done:true` chunk |
| `local-msr1` (msr1) | ollama 0.33.3 | the FIRST chunk (`done:false`); the terminal chunk's message is empty |

Corollary: on both targets the stream was **not incremental** for a tool call
(`_incremental: false`), so TTFT there equals the whole turn, not a first-token
latency. Recorded so the two are never compared as the same measurement.

## Slice A — two-target concurrent dispatch

Two real, non-overlapping engineering packets, one worktree each, one tool each
(`write_file`), harness-owned verification the worker never saw.

| | P1 | P2 |
| --- | --- | --- |
| packet | `P1-workpacket-rtx` | `P2-workercontext-msr1` |
| objective | `src/work_packet.py` (PS-635 primitive #1) | `src/worker_context.py` (PS-635 primitive #3) |
| role | `local_implementer` | `local_microtask` |
| target / host | `local-rtx4500` / minipc | `local-msr1` / msr1 |
| runtime / model | ollama 0.32.11 / qwen3.8:27b | ollama 0.33.3 / qwen3.8:27b |
| worktree / branch | `/home/tdefreest/worktrees/s7-p1` / `work/ps-632-worker-p1` | `/home/tdefreest/worktrees/s7-p2` / `work/ps-632-worker-p2` |
| num_ctx requested / served | 32768 / 32768 | 4096 / 4096 |
| prompt tokens / chars | 841 / 2270 | 812 / 2133 |
| rounds | 1 | 1 |
| ttft / elapsed | 44.85 s / **44.85 s** | 526.21 s / **526.21 s** |
| artifact | `src/work_packet.py` (5 786 B) | `src/worker_context.py` (4 341 B) |
| deterministic verdict | `8 passed` — **PASS** | `1 failed, 4 passed` — **FAIL** |
| failure class | none | technical (deterministic test failure) |

### Concurrency is proven by overlap, not asserted

```
P1  started 05:10:56.204Z  ended 05:11:41.331Z   44.85 s
P2  started 05:10:56.776Z  ended 05:20:02.986Z  526.21 s
overlap 05:10:56.776Z -> 05:11:41.331Z  (both in flight for 44.56 s)
serial estimate 571.06 s   concurrent wall clock 527.09 s   saved 43.98 s
```

Both nodes were genuinely working at the same time. The wall-clock saving is
small **because the two packets were wildly unequal** (11.7x), and that is the
honest reading: concurrency works, but it does not make a 2.83 tok/s node match a
37 tok/s node. Reconciliation was trivial — 2 files produced, 0 shared files, no
conflicts to merge.

### Ownership controls

| control | result |
| --- | --- |
| identical write scope across two packets | **REFUSED** pre-dispatch (`write_scope_overlap`) |
| one scope nested inside another (`src/` vs `src/shared.py`) | **REFUSED** pre-dispatch |
| real packets' scopes disjoint | OK |
| worker writing outside its scope | refused before touching disk (harness) |
| MS-R1 receiving an over-window packet | avoided by sizing the packet for its 4 096 window |

### Why P2's artifact failed — and it is NOT a model failure

MS-R1's artifact is good code: docstrings, a coercion helper, explicit truncation
handling with a visible marker. Four of five tests pass. The one failure is an
interface mismatch:

```
actual  : ACCEPTANCE: NONE ... STOP_IF: NONE
required: ACCEPTANCE: <one bullet per criterion> ... STOP_IF: <one bullet per condition>
```

The worker read keys `acceptance` / `stop_if`; the harness test used the real
packet field names `acceptance_criteria` / `stop_conditions`. **The packet contract
never named its input keys** — it listed only output labels — so the worker
guessed, and the guess was reasonable. This is a defect in the packet, not in the
target.

That is worth stating plainly because the opposite conclusion would have been easy
and wrong: `4 passed, 1 failed` against a contract that under-specifies its
interface measures the contract, not the model. It is also the strongest argument
for the repair loop — the artifact looked excellent and only a deterministic test
caught the mismatch.

## Negative controls and mutation testing

| mutation of the shipped code | tests that failed |
| --- | --- |
| `ACCEPTED` writable from any entry kind | 1 — `test_ACCEPTED_cannot_be_written_by_any_other_entry_kind` |
| acceptance validation removed entirely | 2 — the worker-authority pair |
| budget gate removed | 3 — the unmeasured-window and over-budget pair (+1) |
| no-progress rule removed | 1 — `test_loop_escalates_when_the_same_failure_repeats` |
| repair context replays the base context | 1 — `test_loop_repairs_once_then_accepts_and_uses_a_FRESH_context` |
| remove ONLY the local-worker-authority branch | **0 — not caught** |

The last row is reported deliberately: the invariant is defended by TWO
independent checks (a named-worker refusal and an allow-list), so removing one
still refuses and the suite stays green. It is defense in depth, not a weak test —
removing both is caught by 2 tests.

## Slice B — safe working context calibration

### How 4 096 was established as MS-R1's DEFAULT (not a config, not a ceiling)

Read off the live hosts, not from documentation:

- msr1 runs `/bin/ollama serve` **outside systemd** (no unit, no drop-in, no
  `OLLAMA_CONTEXT_LENGTH` in the environment), and its `llama-server` was started
  with `-c 4096`. That is the ollama **default**.
- minipc by contrast runs `-c 32768` with `--cache-type-k q8_0 --cache-type-v q8_0`.
- A **per-request** `options.num_ctx` re-provisions `llama-server`: requesting
  `num_ctx: 8192` on msr1 produced a new `llama-server … -c 8192` and `/api/ps`
  reported `context_length: 8192`. So calibration needs **no host config edit and
  no daemon restart**, and the knob is per-request rather than per-host.

### Workload (fixed, so every window is comparable)

A deterministic long-context tool task: a seeded pseudo-repo document with a
build code planted at 75% depth (a fact at the very end would be found by
recency, not by context), requiring a `record_answer` tool call carrying the code
and the section that contains it, followed by a SECOND round after a synthetic
tool result. Pass requires tool-call correctness AND recall AND the second round.

### `local-rtx4500` — ladder complete, all windows pass

| window served | prompt tokens | prefill tok/s | decode tok/s | ttft (= whole turn) | tool | recall | round 2 | task |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8 192 | 6 379 | 1 262.44 | 35.74 | 58.49 s | ok | ok | ok | **PASS** |
| 16 384 | 12 100 | 1 382.60 | 34.81 | 63.65 s | ok | ok | ok | **PASS** |
| 24 576 | 17 862 | 1 394.83 | 34.27 | 67.89 s | ok | ok | ok | **PASS** |
| 32 768 | 23 582 | 1 367.83 | 33.66 | 71.91 s | ok | ok | ok | **PASS** |

Observations, all measured:

- **Prefill is ~1 260-1 395 tok/s and decode degrades only mildly** (35.74 → 33.66
  tok/s) as the window grows 4x. There is no cliff at these sizes.
- Turn latency grows sub-linearly (58.5 s → 71.9 s for a 3.7x larger prompt),
  because prefill dominates and prefill is fast.
- On this target `ttft == elapsed` for every window: a tool call arrives in one
  chunk, so this is a whole-turn number and must NOT be compared with a
  first-token latency from a different runtime.

### `local-msr1` — origin established; the long-context ladder is latency-bound

Measured, in order:

1. **4 096 is ollama's default**, not a configured value and not a model ceiling
   (see above). No host mutation was performed at any point.
2. **The window IS configurable per request**, verified by re-provisioning
   `llama-server` at `-c 8192` and reading `context_length: 8192` back from
   `/api/ps`.
3. **A 4 096 window does serve a bounded engineering packet correctly.** Slice A's
   P2 ran at `num_ctx: 4096`, took an 812-token packet context, returned a correct
   `write_file` tool call carrying a 4 341-byte module, and passed 4 of 5
   harness-owned tests. That turn cost 526.2 s.
4. **The Slice B long-context recall test at 4 096 did not complete inside the
   measurement window** (>17 minutes for a ~3 200-token prompt plus a 120-token
   answer, two rounds). No failure was recorded and no error was returned — it is
   pure latency, and it is consistent with MS-R1's measured prefill of roughly
   **9 tok/s** (derived from Slice A: 812-token prompt, 1 235 generated tokens,
   526 s total) and ~2.83 tok/s decode.

That arithmetic is the routing conclusion, and it is a measurement rather than a
preference:

| window | prompt at fill=0.70 | prefill at ~9 tok/s | verdict |
| --- | --- | --- | --- |
| 4 096 | ~3 200 tok | ~6 min | usable for microtasks, but a single turn is minutes |
| 8 192 | ~6 400 tok | ~12 min | marginal |
| 16 384 | ~12 800 tok | ~24 min | not usable as a worker-pool member |

So MS-R1 is characterised as a **microtask / background worker with a bounded
packet budget of roughly `served_context - output_reserve` ≈ 2 700 tokens at its
default window**, which is exactly Slice A's finding. Raising its window makes
each turn slower, not faster; the node's contribution to accepted work per hour is
bounded by prefill, not by correctness.

## `safe_working_context` — the values, and what they do NOT claim

Definition used (conservative, and deliberately NOT "the request did not crash"):

> the largest tested window at which the target completed the representative
task with a CORRECT tool call, correct long-context RECALL, and a successful
SECOND round, with no context error.

| target | declared | served (measured) | `safe_working_context` | bounded? |
| --- | --- | --- | --- | --- |
| `local-rtx4500` | 262144 | 32768 | **32768** | no — a FLOOR |
| `local-msr1` | 262144 | 4096 | **4096** | yes — its default served window |

What each value does NOT claim:

- **32768 is not the model's limit.** No tested window at that target failed
  (tool call, recall and round 2 were correct at every size up to 32 768), so the
  true limit is above it and remains **unbounded by measurement**. It is recorded
  as the largest window verified end-to-end, not as a discovered ceiling. The
  declared 262144 is explicitly NOT used.
- **4096 is not a capability estimate.** It is the default serving window, and the
  largest size at which the target has been verified to complete a bounded packet
  (Slice A). The ladder above it is latency-bound rather than correctness-bound.
- Neither value should be read as "safe for any prompt of that size". The packet's
  own budget is `served_context - generation_reserve`, which is what
  `src/local_worker_loop.usable_input_tokens()` enforces before dispatch.

### Overflow negative control

Run as a separate probe (`probe_overflow.py`) against `local-rtx4500` because the
result was too important to bury in the ladder: an oversized prompt behaves
differently per endpoint.

| endpoint | oversized request (20 012 tokens into a 4 096 window) |
| --- | --- |
| `POST /api/chat`, `stream:true` | **hard error** — `exceed_context_size_error`, `n_prompt_tokens: 20012`, `n_ctx: 4096` |
| `POST /api/generate`, `stream:false` | **SILENT TRUNCATION** — HTTP 200, `done_reason: "length"`, a confident but useless reply, and the returned `context` array shows the prompt cut to the window. **No error.** |

This is the negative control for the whole calibration: it proves that a window
boundary is not always observable from the response, which is why the loop refuses
to dispatch an over-budget packet instead of trusting a successful-looking reply.

## Repair-packet code validated on REAL failure evidence, not a fixture

The PS-635 repair path was exercised against the actual captured pytest output
from Slice A's P2 failure (a real test run on `local-msr1`), not a hand-written
sample:

```
failing tests : ('tests/test_worker_context_ps632.py::test_content_is_rendered',)
collection err: False
fingerprint   : 7a0d195bae79f737
acceptance unchanged: True
withheld      : ['prior conversation', 'prior repair packets', 'manager reasoning']
rendered context: 2000 chars (bounded)
```

The rendered packet contains the objective, the UNCHANGED acceptance criteria, the
write scope, the exact failing command, the failing test id, a bounded diff of the
changed file and a bounded error excerpt. Full text:
`~/scratch/sprint7/evidence/real-repair-packet.txt`.

**Limitation this exposed, recorded rather than hidden:** the extracted
`failure_reasons` came out as `('Asserti...',)` — because the harness had stored
only the last 900 characters of the pytest output, and the short-summary line was
itself cut. A repair packet is only as good as the failure evidence handed to it.
Consequence for the loop's caller: `verify()` must return enough of the runner's
output for `parse_verification_failure` to read the summary line, and
`record_verification` bounds what is PERSISTED (4 000 chars) rather than what is
parsed. This is a caller-contract note, not a defect in the bounding.
