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
