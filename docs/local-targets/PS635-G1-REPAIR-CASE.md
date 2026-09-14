# Pre-registration — PS-635 G1: first GENUINE repair case

Written and committed BEFORE the run. Not edited afterwards except to append the
outcome.

## Why this is a different experiment from L1

L1 proved the interface is delivered. The earlier slice-A case proved a compact
repair packet cannot recover an interface that was never stated. Neither is
evidence about the repair loop's actual purpose.

G1 needs a failure that the contract DID specify and the implementation still got
wrong — so the repair packet gets genuine defect evidence, not a missing contract.

## The packet

| | |
| --- | --- |
| packet_id | `G1-interval-merge-rtx` |
| write scope | `src/interval_merge.py` |
| target | `local-rtx4500` / `minipc`, ollama 0.32.11, qwen3.8:27b, `num_ctx` 32768 |
| verification | `tests/test_interval_merge_ps635.py` (harness-owned, never shown) |
| interface | `intervals` — required, `list[list[int]]`, half-open `[start, end)` pairs |
| attempts | `max_attempts=3` |
| base context | the SHIPPED `render_worker_context` output |

### The contract states every rule, including the subtle one

`merge_intervals(intervals: list) -> list`:

- returns a NEW sorted list; MUST NOT mutate the input
- intervals are half-open: `[start, end)`
- intervals that OVERLAP **or TOUCH** (`end == next start`) merge into one
- output sorted ascending by start; nested intervals collapse into the outer one
- empty input -> `[]`
- `start > end` -> raise `ValueError`

The touching rule is written out explicitly. An implementation that merges only
strict overlaps (`start < current_end`) therefore contains a genuine DEFECT, not an
ambiguity — which is exactly the case the repair loop is supposed to handle.

## Pre-registered predictions

1. **Attempt 1 FAILS deterministically.** Most likely on the touching case
   (`test_touching_intervals_merge` / `test_touching_chain_merges_into_one`), though
   any specified rule it misses counts. The failure must be a real assertion with
   expected-vs-actual visible in the excerpt.
2. **The repair packet is built from that failure**, carries the SAME interface
   digest as the run entry, and fits the bound (~6 000 chars).
3. **Attempt 2 PASSES** and the run reaches `ACCEPTED_CANDIDATE` with exactly 2
   attempts and 1 recorded repair.
4. If attempt 2 fails with the same failure fingerprint, the loop **ESCALATES** and
   dispatches no third attempt. That is a legitimate outcome and will be reported
   as a negative result — the gate will not be relaxed to manufacture a pass.

## Interpretation

| outcome | means |
| --- | --- |
| attempt 1 pass | no repair case existed; record honestly, build a harder specified packet |
| attempt 1 fail, attempt 2 pass | **G1 success** — a genuine implementation failure corrected by a fresh bounded repair attempt without changing the contract |
| attempt 2 fail, escalate | the repair packet is insufficient for this defect class; record as a negative result |

## Not claimed

No reviewer authority, no auto-acceptance. The ceiling remains
`ACCEPTED_CANDIDATE`; acceptance still requires a named non-local authority.

---

## OUTCOME of G1a (appended after the run)

Run `g1`, target `local-rtx4500`, base `e98c0877`.

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 FAILS deterministically | **FALSIFIED** — attempt 1 passed `10 passed` |
| 2. a repair packet is built from the failure | did not occur |
| 3. attempt 2 passes | did not occur |
| 4. escalate on repeated fingerprint | did not occur |

Result: `ACCEPTED_CANDIDATE`, **1 attempt, 0 repairs**, 1 model call, 1731-char context,
7.977 s, 205 generated tokens, ledger chain `(True, None)`.

The implementation got every stated rule right on the first attempt — including the
touching-merge rule the packet was built around. **No repair case existed.**

Per this document's own interpretation table, that is recorded as *"attempt 1 pass →
no repair case existed"*. The gate is not relaxed, and a defect is not manufactured to
produce a result: a synthetic failure would prove nothing about the repair loop.

The honest reading is also the useful one: `local-rtx4500` one-shots a six-rule pure
function from a complete contract. That is a capability result in its own right, and
it means a repair case needs a task with genuine internal complexity rather than a
single subtle rule.

---

## Experiment G1b — pre-registration (written BEFORE its run)

Same target, same loop, same shape of evidence rules; a strictly harder and still
FULLY specified packet, because one-shotted rules cannot produce a repair case.

### The packet

| | |
| --- | --- |
| packet_id | `G1b-window-stats-rtx` |
| write scope | `src/window_stats.py` |
| verification | `tests/test_window_stats_ps635.py` (harness-owned, never shown) |
| interface | `windows` (required, `list[list[int]]`), `min_seconds` (required, `int`) |

### The contract (nine interacting rules)

`summarize_windows(windows: list, *, min_seconds: int = 0) -> dict`

1. windows are half-open `[start, end)` integer seconds
2. first merge overlapping OR touching windows (as in G1a)
3. then DROP every merged window whose duration (`end - start`) is **< min_seconds**
4. return `{"count", "total_seconds", "longest", "coverage"}` where `coverage` is the
   post-filter merged list, sorted ascending
5. `longest` is the merged window with the greatest duration; **ties go to the
   EARLIEST start**
6. if nothing survives the filter, `count` is 0, `total_seconds` is 0, `longest` is
   `None`, `coverage` is `[]`
7. the input must not be mutated
8. `start > end` raises `ValueError`
9. `min_seconds < 0` raises `ValueError`

Rules 3, 5 and 6 interact: the filter runs AFTER merging, the tie-break is specified
rather than natural, and the all-filtered case must collapse cleanly. All three are
stated, so a miss is an implementation DEFECT, not an ambiguity.

### Pre-registered predictions

1. **Attempt 1 FAILS** on at least one of rules 3 / 5 / 6 (or another stated rule),
   with an expected-vs-actual assertion visible in the excerpt.
2. The repair packet carries the SAME `interface_digest` as the run entry and the
   SAME contract verbatim.
3. **Attempt 2 PASSES** → `ACCEPTED_CANDIDATE`, 2 attempts, 1 recorded repair.
4. If attempt 2 fails with the same fingerprint: **ESCALATE**, no third dispatch, and
   it is reported as a negative result.
