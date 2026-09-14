# Pre-registration — PS-638-routed live runs on RTX4500 (2026-09-14)

Written and committed BEFORE the runs. Outcomes are appended, never rewritten.

## What is new here, and what is not

Not new: PS-635 already ran the interface-delivery control (L1) and the G1a..G1d2
series, and those results stand as committed.

New, and the reason for this document: **those runs emitted ledger entries and a
hand-written Markdown account, not a sealed PS-638 package.** This run drives the
same loop with the PS-638 evidence layer attached, so every claim below is bound
to an ExecutionPackage hash, an AttemptReceipt per attempt, a VerificationReceipt
with a DERIVED verdict, a requirement closure, and a validator verdict — all of it
source-bound and re-runnable from the committed harness at
`scripts/ps635-live/live_run.py`.

## Setup (fixed before the runs)

| | |
| --- | --- |
| base | `c7cce461` (PS-638 v1) plus the harness commit |
| harness | `scripts/ps635-live/live_run.py` (committed) |
| target | `local-rtx4500` = `minipc` = tailnet `tacticusanalytics` |
| runtime / model | ollama 0.32.11 / `qwen3.8:27b` |
| context | `num_ctx` 32768 (measured safe floor), generation reserve default |
| tools | exactly one: `write_file`, path restricted to the packet write scope |
| hidden verifier hashes | sealed into the VerificationPlan BEFORE any model call |
| attempts | `max_attempts=3`; one fresh context per attempt, repair carries the ORIGINAL contract and interface |
| routing | explicit operator pin; the receipt records `explicit_pin` and claims NO policy reference |
| network | probed non-destructively; no host config edited, no daemon restarted |

## Run E1 — the interface-delivery control, through the receipt path

Case `l1-interface` (`src/ledger_note.py`, verifier `tests/test_ledger_note_ps635.py`).
The key names appear ONLY in `WorkPacket.interface`; the harness REFUSES to
proceed if a name leaks into the objective, contract, criteria, file name or
prompt.

Pre-registered predictions:

1. attempt 1 PASSES (`8 passed`), terminal `ACCEPTED_CANDIDATE`, **0 repairs**.
2. The sealed package VALIDATES (`VERIFIED`), with all mandatory requirements
   `SATISFIED`.
3. The sealed interface line is present in the rendered-context artifact that the
   package references, so delivery is checkable from the package alone.

## Run E2 — the first GENUINE repair case

Case `g1e-bounded-cache` (`src/bounded_cache.py`, hidden verifier
`scripts/ps635-live/hidden/test_bounded_cache_ps635.py`).

Why a stateful task: the G1a..G1d2 series one-shotted 6 rules, 9 interacting
rules, multi-form assertion parsing and a 7-rule algorithmic packet. A repair case
needs genuine internal state. This packet states FOURTEEN rules for an LRU cache —
cumulative counters that survive `clear()`, a `purge` that is neither hit nor
miss, `set_capacity` evicting and counting, MRU-first `keys()`, and
equality-not-identity key handling. Nothing is hidden: every rule is written out,
so a first-attempt miss is an IMPLEMENTATION DEFECT.

The hidden verifier was validated against a reference implementation (19 passed)
and against two deliberately wrong ones before the run:
`clear()` resetting the counters fails exactly one test, and MRU-eviction fails
exactly the named control. It is not being trusted because it is green.

Pre-registered predictions:

1. **Attempt 1 FAILS deterministically** on at least one stated rule, with a real
   assertion visible in the verifier output.
2. The repair packet carries the SAME `interface_digest` and the contract
   verbatim.
3. **Attempt 2 PASSES** → `ACCEPTED_CANDIDATE`, 2 attempts, 1 repair; the sealed
   package VALIDATES, and it still contains BOTH attempts (no earlier red erased).
4. If attempt 2 fails with the same fingerprint: **ESCALATE**, no third dispatch.
   The package is still sealed, and it is REJECTED by the validator with named
   reasons. That is a legitimate outcome and will be reported as a negative
   result.

## Run E3 — the missing-interface NEGATIVE control

Case `neg-no-interface`: identically specified to E2, with `interface` removed.

Pre-registered prediction: the package is refused as `PACKET_INVALID` **before any
budget or model call** (`model_calls == 0`), and **no EvidencePackage is sealed** —
because a sealed package about a run that never happened would be a document about
nothing.
