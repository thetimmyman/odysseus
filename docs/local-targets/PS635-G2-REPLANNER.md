# PS-635 G2 — the manager/replanner seam, and the live control for it

Written and committed BEFORE the run. Not edited afterwards except to append the
outcome.

## What is new here

G1 is a loop whose manager is code. G2 adds a MODEL in an **advisory** role, and
the whole seam is `src/replanner.py`:

| stage | what it is |
| --- | --- |
| planner input | a bounded projection of CANONICAL state: ledger + validated packet + declared envelope. No transcript, no worker prose, no filesystem. |
| proposal | typed, schema-validated, content-addressed. Five kinds only: `next_packet`, `replan`, `approach_switch`, `stop`, `escalate`. |
| gate | deterministic, typed, fail-closed: 16 named checks, each with its own refusal code. |
| effect | at most ONE bounded replan per run, spending the SAME attempt budget; the only field it may change is `approach`. |

Two properties are structural rather than promised:

1. **Authority is unrepresentable.** There is no `kind` for accept/approve/ship/
   land/route, and the schema refuses unknown kinds, so "mark this ACCEPTED" is
   not a refusal the gate must catch — it is a sentence the proposal schema cannot
   spell. A proposal may still ASK (`requested_actions`, `requested_authority`, or
   a `packet_delta` key outside its one permitted lever) and that is representable
   on purpose: a refusal with a typed code is better evidence than an impossible
   request. The ledger refuses to store an accepted proposal that requests an
   action or authority at all.
2. **A PASS is not negotiable.** At the PASS boundary the deterministic verifier
   has already decided the run, and the ONLY legal kind is `stop`. A proposal to
   replan there is refused with `kind_not_allowed` and recorded.

## What is proven hermetically, before any model call

`tests/test_replanner_ps635.py` (39 tests) plus the loop's own suite:

* the projection is a FIXED SCHEMA — a worker transcript cannot reach the manager,
  and the projection digest moves when canonical state moves;
* one positive control (a valid `approach_switch` is accepted) without which the
  negative controls would be vacuous;
* a refusal, with its own code, for each of: malformed proposal, scope widening,
  permission widening, approval/ship/lifecycle request, failed-gate bypass
  (weakened verification, restated criteria, skipped review), routing, Jira
  mutation, interface drift, source drift, identity drift, budget widening,
  unknown delta key, unbound evidence, invalid packet, repeated approach
  (cycle), a proposal for an already-settled run, a proposal for another run, a
  tampered proposal, a replan with no approach, a `stop` carrying approach text,
  budget/replan-allowance exhaustion;
* the loop: with no advisor it behaves EXACTLY as G1 did; a validated
  `approach_switch` produces one extra bounded attempt whose context carries the
  approach note AND the unchanged repair evidence/contract/interface; a refused
  proposal leaves the deterministic escalation intact; the manager is consulted
  once at a PASS and is not obeyed; an advisor that returns nothing, or raises,
  cannot fail a run; advice cannot buy an attempt past the budget, and a second
  stall escalates once the replan allowance is spent.

## The live control

| | |
| --- | --- |
| case | `g2-replan-control` (`scripts/ps635-live/cases.py`) |
| packet | **verbatim the `g1e-bounded-cache` packet** (14 stated rules, hidden verifier, write scope `src/bounded_cache.py`) |
| base | `1e362f10` plus the G2 commits |
| target | `local-rtx4500` / `minipc`, ollama 0.32.11, `qwen3.8:27b`, `num_ctx` 32768 |
| worker | one `write_file` tool, restricted to the packet write scope |
| manager | the SAME pinned target, no tools, `num_predict` 800, one turn per consultation |
| replans | `max_replans = 1` |
| evidence | PS-638 package with a `manager_seam` seal per consultation (planner input, manager context, raw model output, parsed proposal, verdict, ledger entry hash, execution identities) |

Why the same packet as E2: E2 already ran this exact packet through G1 on this
exact target and one-shotted it (`19 passed`, 1 attempt, 0 repairs). Comparing G2
against a measured G1 arm on the same corpus is what PS-579 needs; a new task
would add a confound instead of a comparison.

### Pre-registered predictions

1. **The worker leg one-shots it** (`19 passed` on attempt 1), as E2 measured.
2. **The manager is consulted exactly once, live, at the PASS boundary**, with
   `allowed_kinds = ["stop"]` in the projection it is shown.
3. **The run's terminal result and attempt count are identical to the G1 arm** —
   `ACCEPTED_CANDIDATE`, 1 attempt, 0 repairs, `19 passed`. Advice cannot move a
   deterministically verified outcome.
4. **The manager's proposal is schema-validated and deterministically gated**, and
   `kind_not_allowed` is the expected verdict IF the model proposes anything other
   than `stop`. Either way the verdict, the proposal hash and the ledger entry are
   recorded.
5. **An advisor that replies with unusable text is a recorded `schema_invalid`
   refusal** with the model's own words preserved as an artifact. Both outcomes
   are legitimate and both will be reported as measured.

### Predictions that will be reported as falsified, honestly

* The **replan branch** (a stall becomes a bounded replan) is expected to stay
  **UNOBSERVED live** — the same reason the live repair branch is unobserved: six
  complete packets have now been one-shotted on attempt 1. It is proven by the
  hermetic suite. **No failure will be manufactured to reach it**, and no packet
  will be weakened, under-specified or oversized to force a red.
* If the live manager's proposal influences anything beyond the recorded advice at
  a PASS, that is a **seam failure** and will be reported as one.

### Not claimed

No reviewer authority, no acceptance authority, no routing. The ceiling for a
worker-driven run remains `ACCEPTED_CANDIDATE`. The fresh challenger (PS-635's
last deliverable) is deliberately NOT added here: the basic replanner path must be
proven first. Deterministic routing remains PS-605's, and this run claims no policy
reference (`explicit_pin`, as recorded on the dispatch receipt).
