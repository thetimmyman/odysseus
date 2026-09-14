# Pre-registration — PS-635 interface-delivery live control (experiment L1)

Written and committed BEFORE the run. Not edited afterwards except to append the
outcome.

## The question this isolates

The declared interface is now validated, rendered into the fresh worker context and
carried into the repair packet. **Does the formal interface path actually deliver
the key names to the worker?**

This is deliberately NOT another hidden-contract failure. The contract is COMPLETE:
the worker is told the module path, the callables, the output labels, the section
order and the bounding rule. The ONLY thing the contract does not say is the input
key names — those are supplied exclusively through the formal `interface` field.

## Setup (fixed before the run)

| | |
| --- | --- |
| base | `03f136f3` (plus this pre-registration commit) |
| target | `local-rtx4500` / host `minipc` |
| runtime / model | ollama 0.32.11 / qwen3.8:27b |
| context | `num_ctx` 32768 (measured safe floor), loop generation reserve default |
| tools | exactly one: `write_file`, path restricted to the packet's write scope |
| write scope | `src/ledger_note.py` |
| verification | `tests/test_ledger_note_ps635.py` — harness-owned, never shown to the worker |
| attempts | `max_attempts=3` |
| context builder | the SHIPPED `src.worker_context.render_worker_context` |
| interface digest | recorded on the run; must equal the digest on any repair entry |

### The packet

- objective: implement the note renderer for the execution ledger
- interface (the ONLY source of key names):
  - `subject_name` — required, `str`
  - `verbatim_lines` — required, `list[str]`
  - `block_reason` — optional, `str`

### Key-name isolation (the point of the control)

The names `subject_name`, `verbatim_lines` and `block_reason` are supplied ONLY via
`WorkPacket.interface`. They do not appear in the objective, the contract text, the
acceptance criteria, the negative control, the stop conditions, the test file name,
the output path, the tool schema or any comment. A unit control asserts this on the
rendered context (`test_rendered_interface_names_are_not_available_anywhere_else`).

## Pre-registered predictions

1. **Positive control:** attempt 1 PASSES (`8 passed`), terminal
   `ACCEPTED_CANDIDATE`, **zero repairs**, run-entry `interface_digest` non-empty.
2. **Negative control:** the same writable packet with `interface` REMOVED fails as
   `packet_invalid` with **zero model calls** and zero dispatches.

## Falsification / interpretation

- If attempt 1 fails with `SUBJECT: NONE` (or any NONE where a value was supplied),
  the interface did NOT reach the worker — or reached it and was ignored. That is a
  delivery failure and the loop must NOT be credited: escalate and diagnose.
- If attempt 1 fails on truncation only, the interface still delivered; record it as
  a partial delivery and a genuine (possibly repairable) implementation defect.
- A pass proves the names came through the interface path, because
  `test_invented_keys_are_ignored` fails if the module reads synonym guesses.

## What this does NOT claim

No reviewer authority, no auto-approve class. The ceiling for a worker-driven run
remains `ACCEPTED_CANDIDATE`; acceptance needs a named non-local authority.

---

## OUTCOME of experiment L1 (appended after the run)

Run `l1-positive` / `l1-negative`, target `local-rtx4500`, base `9efb53bc`.

| pre-registered prediction | outcome |
| --- | --- |
| 1. positive passes attempt 1, `ACCEPTED_CANDIDATE`, zero repairs | **CONFIRMED** — `8 passed`, 1 attempt, **0 repairs**, 1 model call |
| 2. negative refused as `packet_invalid` with zero model calls | **CONFIRMED** — `BLOCKED`, `packet_invalid`, 0 attempts, **0 model calls** |

Evidence:

- base context **1 818 chars**, rendered by the SHIPPED `render_worker_context`
- run-entry `interface_digest = 1d6eda6c43ddca1a`, chain verified `True`
- decision `stop` — `attempt 1 passed deterministic verification`
- negative detail: `interface must be non-empty: a writable packet must declare the
  input keys its contract promises the worker`

The worker's module reads exactly `subject_name`, `verbatim_lines`, `block_reason`.
None of those names appears anywhere in the context outside the INTERFACE section,
so the only way it could have read them is through the formal interface path — and
`test_invented_keys_are_ignored` fails if the module reads synonym guesses instead.

**The interface is no longer decorative.** It is validated, delivered, carried into
repairs, and digest-bound to the run evidence.

### Honest observation: one edge the contract stated and the worker still missed

For `max_chars` smaller than the truncation marker (14 chars), the produced module
computes `cut = max_chars - len(marker)`, clamps it to 0, and returns the FULL
marker — so the result exceeds `max_chars`. The acceptance test bounded at 200 and
did not cover it, and the contract's wording ("cut it at a character boundary and
append the exact marker") does not state what to do when the marker itself cannot
fit.

That is recorded rather than glossed: it is a real, bounded, unstated edge, not a
delivery failure. It is also the shape of a good G1 repair case IF the contract is
made explicit about the degenerate bound — a stated requirement the first attempt
gets wrong for an implementation reason, repairable from the failure evidence.
