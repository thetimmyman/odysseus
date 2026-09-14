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

---

## OUTCOME of G1b (appended after the run)

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 FAILS on a stated rule | **FALSIFIED** — `12 passed` on attempt 1 |
| 2. repair packet built from the failure | did not occur |
| 3. attempt 2 passes | did not occur |

Result: `ACCEPTED_CANDIDATE`, **1 attempt, 0 repairs**, 1 model call, 2320-char context,
interface digest `76247a4b4314c6c0`, chain `(True, None)`. Every one of the nine
interacting rules was implemented correctly first time, including the after-merge
filter, the earliest-start tie-break and the all-filtered collapse.

### Capability finding (recorded because it is the real result here)

`local-rtx4500` one-shots FULLY SPECIFIED bounded pure-function packets at this
scale: 6 rules (G1a) and 9 interacting rules (G1b) both passed on the first attempt
with a single model call and no repair. That is a genuinely useful result for
routing — and it means a single subtle rule is not enough to produce a repair case.

A synthetic defect is NOT acceptable as a substitute: manufacturing a failure would
prove nothing about the repair loop. So G1c uses a task whose difficulty is
inherent — multiple assertion FORMS that must all be parsed — while every rule stays
stated in the contract.

---

## Experiment G1c — pre-registration (written BEFORE its run)

### Why this task

The repair packet is supposed to carry "expected vs actual" (Section 6), and the
current code does not extract them — it quotes the raw excerpt. Extracting them is
real, useful work on a NEW module (so a bad artifact cannot break the loop itself).
It is also inherently multi-form: equality, containment and truthiness each have a
different shape in pytest output.

### The packet

| | |
| --- | --- |
| packet_id | `G1c-verifier-evidence-rtx` |
| write scope | `src/verifier_evidence.py` (new module) |
| verification | `tests/test_verifier_evidence_ps635.py` (harness-owned, never shown) |
| interface | `output` (required, `str`) |

### The contract (stated in full)

`extract_assertion_evidence(output: str) -> dict` returning exactly
`{"kind", "expected", "actual"}`:

1. Find the LAST line in `output` matching `AssertionError: assert <EXPR>`.
2. Strip a trailing comma and surrounding whitespace from `<EXPR>`. If `<EXPR>` is
   fully wrapped in parentheses, remove ONE layer of them.
3. `assert A == B`   -> kind `equality`, `actual = A`, `expected = B`
4. `assert A in B`   -> kind `containment`, `actual = A`, `expected = B`
5. `assert not A`    -> kind `truthiness`, `expected = "False"`, `actual = A`
6. anything else     -> kind `other`, `expected = ""`, `actual = ""`
7. Values are the SOURCE TEXT, trimmed — never evaluated.
8. No match anywhere in `output` -> kind `other`, `expected = ""`, `actual = ""`.

### Pre-registered predictions

1. **Attempt 1 FAILS** on at least one of the containment / truthiness /
   paren-stripping rules, with a visible expected-vs-actual assertion.
2. The repair packet carries the SAME interface digest and contract, and its
   `failing_tests` names the specific test.
3. **Attempt 2 PASSES** -> `ACCEPTED_CANDIDATE`, 2 attempts, 1 repair.
4. Same fingerprint twice -> **ESCALATE**, no third dispatch, reported as negative.

If attempt 1 also passes, the honest conclusion for this session is that
`local-rtx4500` one-shots bounded fully-specified packets at this scale, and G1 is
reported as NOT achieved rather than manufactured.

---

## OUTCOME of G1c (appended after the run)

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 FAILS on a stated rule | **FALSIFIED** — `8 passed` on attempt 1 |
| 2-3. repair built, attempt 2 passes | did not occur |

Result: `ACCEPTED_CANDIDATE`, **1 attempt, 0 repairs**, 1 model call, 2275-char context,
interface digest `0dce35628a445393`, chain `(True, None)`. All eight rules including
last-match-wins, one-layer paren unwrapping, containment and truthiness forms.

### Three falsified predictions in a row is a finding, not noise

| packet | rules | outcome |
| --- | --- | --- |
| G1a interval merge | 6 (one subtle: touching) | **pass on attempt 1** |
| G1b window stats | 9 interacting | **pass on attempt 1** |
| G1c assertion-evidence parser | 8, multi-form | **pass on attempt 1** |

With a complete contract, a declared interface and a bounded fresh context at
2.2-2.3k chars, `local-rtx4500` one-shots bounded pure-function packets at this
scale — roughly 200-900 generated tokens each, one model call, zero repairs. That
is a routing-relevant capability result: **well-specified small packets do not need
the repair path at all.**

It also means "one subtle rule" cannot produce a repair case. G1d therefore uses a
task with genuine algorithmic difficulty and NO standard-library shortcut, where
the failure mode is algorithmic rather than lexical.

---

## Experiment G1d — pre-registration (written BEFORE its run)

| | |
| --- | --- |
| packet_id | `G1d-toposort-rtx` |
| write scope | `src/topo_sort.py` (new module) |
| verification | `tests/test_topo_sort_ps635.py` (harness-owned, never shown) |
| interface | `graph` (required, `dict[str, list[str]]`) |

### The contract (stated in full)

`topological_order(graph: dict) -> list`:

1. `graph` maps a node to the list of its SUCCESSORS (edges node -> successor).
2. Return a list containing EVERY node exactly once — including nodes that appear
   only as a successor and nodes with no edges.
3. Among the nodes that are currently ready (every predecessor already placed),
   always choose the **alphabetically smallest**.
4. Duplicate edges are ignored.
5. A cycle raises `ValueError`.
6. Empty input returns `[]`.
7. The input must not be mutated.

### Why this one can genuinely fail

Rule 3 is the whole task. A correct-looking Kahn implementation that drains a set or
list of ready nodes produces a valid topological order that is usually NOT
alphabetical, and the test asserts the exact list. There is no standard-library
one-liner for "lexicographically smallest topological order".

### Pre-registered predictions

1. **Attempt 1 FAILS** on the exact-order assertions (rule 3) and/or the
   isolated-node rule, with the expected and actual lists visible.
2. The repair packet carries the same interface digest and contract verbatim.
3. **Attempt 2 PASSES** -> `ACCEPTED_CANDIDATE`, 2 attempts, 1 repair.
4. Same fingerprint twice -> **ESCALATE**, no third dispatch, reported as negative.

If attempt 1 also passes, G1 is reported as NOT ACHIEVED: the correct conclusion is
that this worker does not need repairing on bounded fully-specified packets, and
that manufacturing a defect to force a repair would not be evidence.
