# PS-579 RTX4500 G0/G1/G2 paired baseline preregistration

Sealed before the first comparative model run on this block. This artifact is
the scoring contract; outcomes cannot change its arms, retry rule, or stop rule.

## Source and corpus

- Repository: `pewdiepie-archdaemon/odysseus`
- Evaluation branch base: `ps-632-capability-registry @ 1fc99c2e7857497d0e6bd0400099744076953a99`
- Candidate task: `g1e-bounded-cache` (`g2-replan-control` case wrapper for the
  same packet, used across all three arms)
- Packet identity: `G1e-bounded-cache-rtx`
- Write scope: `src/bounded_cache.py`
- Exact-base source identity: packet `base_sha=1e362f10ff969edefb601962287a8842c1200c5e`; this is the existing PS-579 G2 control's exact source base and keeps G0/G1/G2 paired on one task identity. Run source snapshots are sealed by PS-638.
- Hidden verifier: `scripts/ps635-live/hidden/test_bounded_cache_ps635.py`
- Hidden verifier SHA-256: `9e33cbc2f0cf615edac0d02e9f0f21cee4a2f0ae77af2937943617d084c595b0`

This is the existing fully specified, replayable internal PS-635 candidate. No
defect, failure, context overflow, malformed packet, or repair opportunity is
manufactured. A naturally passing first attempt is valid evidence.

## Execution target and receipt

- Target preference: `local-rtx4500` (PS-605 remains routing authority)
- Runtime: Ollama `0.32.11`, CUDA backend, model `qwen3.8:27b`
- Model digest: `d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28`
- Configured/served/measured-safe context: `32768` / `32768` / `32768`
- PS-632 profile: `local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx32768:d94d964641c7`
- PS-632 receipt hash: `9b180a5e317fbade1d7195f795acb3d2a8ce66533b8597593fe700d5d77b4891` (resolved from the current store at seal time; native tools and liveness re-proven after warming the already-qualified model; supersedes `273fe11d8ea2a60d4a12f167edbe053c9b8aacddbdbe1915ff5ea1760eb999ad`)
- Receipt store: `/home/tdefreest/scratch/ps632-store`; store verification must be green before each run.
- Context projection identity: `src.attempt_receipt.context_projection_digest` over the exact rendered worker context; every AttemptReceipt records the resulting hash.

## Arms and changed variable

The only changed variable is orchestration arm, with the same packet, source
base, verifier, target preference, runtime, model, context, and deterministic
temperature (`0`) for each arm.

- **G0:** existing raw `Dispatcher` path, one worker invocation and one deterministic verifier; no PS-635 repair retry and no replanner.
- **G1:** frozen PS-635 bounded loop: immutable packet, fresh bounded context, worker, deterministic verifier, compact repair evidence, fresh retry only after a genuine technical failure.
- **G2:** frozen G1 plus the existing validated replanner only at an eligible no-progress/repeated-failure boundary; no challenger and no PASS-boundary consultation.

The runner's PS-638 chain is retained for all arms. A failed attempt is never
discarded. G0's one-shot stop is an evaluation-arm rule, not a change to frozen
runtime semantics.

## Hypothesis

G1 will produce at least as many correctly accepted engineering candidates as G0
and may improve acceptance after naturally occurring technical failures. G2 will
match G1 on first-attempt PASS cases and can differ only when frozen no-progress
semantics are naturally reached. Token throughput is diagnostic, not the
objective.

## Scoring, retries, and stopping

- Candidate success is `ACCEPTED_CANDIDATE` with a VERIFIED PS-638 evidence package and deterministic verifier PASS.
- `ACCEPTED_CANDIDATE` is not `DELIVERED` or `ACCEPTED`; no local model auto-approves or lands work.
- Count first-attempt PASS separately from PASS after repair.
- Do not retry G0. G1/G2 use the frozen bounded attempt budget (`3` for this case); only a genuine technical verifier failure can create repair evidence, and only repeated identical failure can open G2 replanning.
- Stop an arm on packet invalidity, context blocking, infrastructure/runtime fault, broken evidence binding, ambiguous verifier identity, or materially changed runtime/profile. Do not classify these as worker repair failures.
- Do not proceed to Framework profiles or external corpora in this block.

## Metrics

For each task × arm: deterministic result, attempts, first-attempt result,
post-repair result when naturally applicable, verifier outcome/failure class,
wall-clock elapsed time, worker/model calls, prompt/completion tokens, runtime
latency/TTFT where available, accepted-candidate status, semantic-review
disposition (`flag-for-human-read`), PS-632 receipt identity, and the complete
PS-638 ExecutionPackage → DispatchDecisionReceipt → AttemptReceipt(s) →
VerificationReceipt(s) → EvidencePackage chain.

Primary comparison: `accepted-candidate work/hour = accepted candidates /
wall-clock hours`, reported alongside paired task outcomes and operational
stability. Missing or naturally unobserved repair/replan cells remain
`UNOBSERVED`; no quantitative repair uplift is claimed without a naturally
occurring fully specified failed-first-attempt case.
