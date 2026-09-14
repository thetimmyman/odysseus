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
