# Routing-coordinator model research & benchmark protocol (v0.1)

Status: **PROTOCOL** (proposed, not yet executed). Complements `PS605-DETERMINISTIC-ROUTING.md`
and `src/routing_benchmark.py`. This document pins *what we benchmark, which models, how they are
tuned, where they are hosted, and what must be true before a number is trustable* — so a benchmark
run is the LAST step, not the first.

## 0. The actual question (purpose, not just "compare models")

**0.1 — A sharper reframe first (evidence from the fixture analysis): is this even an LLM task?**
Every output field is a LOW-CARDINALITY ENUM over an already-semi-structured input, not open-ended
generation. Field cardinalities (over the 27 fixtures): domain=5, taskType=9, risk=4,
dataSensitivity=5 (public→secret), backend=3, verificationMode=6, approvalRequired=2 (boolean). The
input is `{title, objective, type, repoPath, inputs.files[], inputs.logs[]/prompt}` — short strings
packed with LEXICAL signals (`vault/unseal.py`, `auth/signing_key.py`, `billing/reconcile.py`,
`tacticus`, `k3s/flannel`, `diff`, `release`).

**Consequence: the decision is ~80–90% deterministic**, and the policy-gate field (`dataSensitivity`)
is the field you *least* want an LLM owning (a sensitivity mislabel is a security event; a lexicon +
"uncertain → treat as restricted → local-only" rule is strictly safer and cannot fail). The LLM's real
role shrinks to **feature extraction / catching lexicon misses / resolving genuinely-ambiguous input**,
not emitting the whole decision.

---

The two general-purpose providers are FIXED (qwen3.8 flash on Framework, qwen3.8 27b on RTX 4500)
and they will be **busy on real work**. The benchmark therefore answers ONE question:

> **Does a dedicated micro-classifier on the 2080 Ti (or CPU) clear the HARD-GATE FLOOR on the
> cheap routing tiers — at near-zero cost — so the two fixed providers are never bothered with
> routine triage?**

The micro model does not need to beat qwen3.8. It needs to be *good enough* on the cheap tiers
(triage: domain/taskType/verificationMode + the safe common case) to meet the gates
(`schema_validity ≥ .98`, `policy_gate_compliance ≥ .99`, `uncertainty_handling ≥ .85`) while the
fixed providers stay idle for cheap jobs. If it can: we offload triage to the idle 2080 Ti and keep
the expensive models free. If it cannot: the answer is "route every gap to qwen3.8 flash and accept
the cost" — also a clean, actionable conclusion.

## 1. The task (what we are actually measuring)

The routing coordinator, when deterministic routing cannot decide, must assign metadata to the
*seam* between an application request and its status: `domain`, `taskType`, `risk`,
`dataSensitivity`, `verificationMode`, `backend`, `approvalRequired` — plus obey the policy gate
(`restricted`/`secret` never route remote) and abstain cleanly when underspecified.

This is **one multi-label decision per input, jointly coupled, security-gated**, NOT a set of
independent sub-tasks. The 27 fixtures in `config/routing_coordinator_fixtures/fixtures.json`
cover 12 scored dimensions + failure_retry, with hard gates (`policy_gate_compliance ≥ 0.99`,
`schema_validity ≥ 0.98`, `consistency ≥ 0.90`, `uncertainty_handling ≥ 0.85`, etc.;
`passedAllGates` is the AND).

## 2. Task decomposition — specialists by POLICY TIER, not by dimension

The 12 dimensions do NOT split into per-dimension experts (they are jointly determined; splitting
breaks the `policy_gate` coupling and the `arbitration`/`consistency` requirements). Instead, the
coordination decision has **three policy tiers that legitimately want different model profiles**:

1. **Micro triage (cheap, high-volume, low-stakes).** `domain`, `taskType`, `verificationMode`,
   and the common case. A **0.5–1B** model is appropriate here: latency-sensitive, error tolerated
   *because tier 3 double-checks any dangerous consequence*.
2. **Gate/sensitivity (conservative, catastrophic-to-be-wrong).** `dataSensitivity`,
   `policy_gate`, `approval`. This is the tier where false confidence is a security event. It wants
   a **tuned classifier that prefers to ABSTAIN/under-classify** (`restricted?` → ask) over a
   confidently-wrong label. The most likely target for a *fine-tuned* small specialist.
3. **Fallback/catch-all (arbitration, uncertainty, ambiguous).** One **small generalist (7–8B)**
   that is NOT tuned to any one label space but is good at "these disagreed; which is safer" and at
   abstaining. This is the confidence-bearing catch-all.

So the answer to "many experts vs one big" is: **a small cascade (tier-1 micro + tier-2 gate
specialist + tier-3 generalist), not nine micro-models and not one do-everything model.** Every
tier emits `candidate` (never `declared`) rows behind a confidence layer; an unclassifiable input
must ABSTAIN and leave the gap visible rather than be filled.

## 3. Which models are actually *designed* for this

- **No off-the-shelf model is pre-trained for THIS 12-dimension routing + policy gate.** The
  "fine-tuned expert" must be *created* from our 27 labeled fixtures (they are the fine-tuning seed).
- **Zero-shot candidates** are the only thing measurable *today* (no fine-tuned tier-2 specialist
  yet). The CURRENT Qwen family (Tim-provided, live-verified 2026-09-22) is the natural pick because
  the fixed generalists are qwen3.8 — use the same-family small siblings for a clean escalation path.
- **The deciding capability is JSON-schema/gbnf-constrained decoding**, not size. The harness
  already states: plain sampling on a small model misses `schema_validity ≥ 0.98`; the endpoint
  MUST constrain output to `CoordinatorDecision` (GBNF / `json_schema` response_format).
- **The generalist arm is FIXED and OUT OF SCOPE for selection** (Tim 2026-09-22):
  **qwen3.8 flash** (Framework, via halogen) and **qwen3.8 27b** (RTX 4500). These are the two
  fixed general-purpose providers. The benchmark no longer asks "which generalist" — it asks
  whether a **dedicated micro-classifier on the 2080 Ti (or CPU)** is worth its keep for triage.

### Verified candidate matrix (CURRENT models — live HF, 2026-09-22)

Fixed providers (Tim): **Qwen3.8-Flash-Next** (Framework/halogen) and **Qwen3.8-27B** (RTX 4500).

Micro-tier candidates (small Qwen3.5 siblings — the natural companions to qwen3.8):

| model | Q4_K_M | Q8_0 | fits 2080 Ti (11GB)? |
|---|---|---|---|
| Qwen3.5-0.8B | — | 0.83 GB | ✅ trivially (even CPU is fine) |
| Qwen3.5-2B | 1.28 GB | 2.01 GB | ✅ trivially |
| Qwen3.5-4B | 2.74 GB | 4.48 GB | ✅ comfortably |
| Qwen3.5-9B (borderline generalist) | ~5–6 GB (est) | ~10 GB (est) | ✅ at Q4, tight at Q8 |

(GGUF sources: `ggml-org/Qwen3.5-0.8B-GGUF`, `unsloth/Qwen3.5-{2B,4B}-GGUF` — official quants.)

**Rule added (correcting a stale-catalog mistake): model candidates are selected from the live HF
index at benchmark time, NEVER from the model's training-memory catalog.** Earlier versions of this
doc named Qwen2.5/Gemma-3/Llama-3.1/Phi-3 — those are one-to-two generations stale. The corrected,
current micro-tier is the Qwen3.5 0.8B/2B/4B family.

**Candidate matrix** (to be filled by model research, with exact GGUF quant/context/offload):

| tier | candidate | size | quant (2080 Ti) | context | offload | runtime (schema-constrained) |
|---|---|---|---|---|---|---|
| 1 micro triage | Qwen2.5-0.5B/1.5B, Gemma-3-1B/4B | ≤2B | Q8 (fits) | 4k | full GPU or CPU | llama.cpp gbnf |
| 2 gate specialist | *(tuned later)* | ~1B tuned | TBD | 4k | full GPU | llama.cpp gbnf |
| 3 fallback | Qwen2.5-7B / Gemma-3-8B | 7–8B | Q4 | 8–16k | full GPU (Discord closed) | llama.cpp json_schema |

## 4. Where we home them (hosting map)

| tier | home | rationale |
|---|---|---|
| micro triage (0.5–1B) | **This desktop's 2080 Ti**, or **CPU-only** | ≤2B runs fine on CPU (a 0.5B Q4 ≈ 0.4 GB). Needs no GPU, no dedicated node. Low-latency hot path. |
| gate specialist (1B tuned) | **Serve** anywhere cheap; **train** on RTX 4500 / Framework | Serving 1B is cheap; *fine-tuning* 1B+ wants >11 GB (blanket the training-time shortage). |
| fallback (7–8B) | **2080 Ti now** (Q4, Discord closed) → **RTX 4500** when landed | 7–8B Q4 ≈ 4–5 GB; 2080 Ti is the casual resident, 4500 the proper home. |

**Raspberry Pi — explicitly REJECTED for inference serving.** ARM, 8 GB, no discrete GPU / no CUDA
(so no real llama.cpp CUDA benefit; CPU-only at Pi speed is useless in a routing hot-path), and
they are the 4-of-5 etcd quorum today (loading them with a service is both wrong and unsafe) and
destined for the edge-device lab. The MS-R1 (12c/64 GB aarch64) is the ARM serving/compat home if
we ever want an ARM build of a serving model — NOT the Pi fleet.

## 5. What must be pinned BEFORE a run is trustable (sloppiness guards)

1. **Per-config `InferenceProfile` provenance** — model sha256, quant, runtime + commit, prompt
   template + commit, greedy vs sampled, temperature, context, offload layers. Captured per run so
   two numbers are comparable and a "which model won" is never actually "which prompt won".
2. **Fixed tuning protocol** — temperature ≈ 0 (greedy) for a classification task; JSON-schema/gbnf
   constrained output ON; fixed `max_tokens`; the *same* `CoordinatorClient.decide` task shape to
   every model. `consistency` is scored by the harness, so non-determinism is measured, not assumed.
3. **Stub baseline first** — run the injectable stub through `run_benchmark` to prove scoring is
   correct and to record a "floor" before spending any GPU. Then one live model.
4. **Recorded baseline + negatives** — the 4 `policygate-*` traps (mislabel-and-exfiltrate must
   fail, scored against ground truth), 2 `uncertainty-*` (must yield low confidence/safe fallback),
   and 2 `consistency-*` fixtures are the negative/positive coverage. Keep them frozen.
5. **Named-by-tier reporting** — report per-tier, not just one aggregate `passedAllGates`, so a
   "passing" model that silently fails the gate tier is not hidden inside a green total.

## 6. Honest limitations (state them, don't bury them)

- **Zero-shot today**: no fine-tuned tier-2 specialist exists yet; the "specialists" arm can only be
  measured as zero-shot small models until we fine-tune (needs a labeled set > 27 fixtures).
- **Lost baseline**: the earlier ad-hoc run on this PC left no recorded numbers, so there is no
  "current best" to beat — the stub + first pinned run becomes the baseline.
- **Single card**: results on the 2080 Ti measure "what fits + what's correct there," not the
  RTX 4500/Framework ceiling; a model that does not fit at Q4 here is deferred, not failed.

## 6.5 The three benchmark arms (the experiment this is actually designed to answer)

Given §0.1 (the decision is ~80–90% deterministic), the benchmark compares THREE arms, not
"model A vs model B":

1. **Arm 0 — deterministic only (no LLM).** Rules + a lexicon on `title/objective/type/files`
   feed a trivial classifier (logistic/GBM) for the enumerable fields; the policy gate is a hard
   post-hoc rule; `uncertain` → conservative default (`restricted`→local, abstain). This is the
   null-arm: if it clears the gates, NO model is needed for triage at all.
2. **Arm 1 — rules + a micro-LLM for feature extraction only.** Qwen3.5-0.8B (or 2B) reads the
   text and emits `{domain_hint, taskType_hint, sensitivity_hint}`; the deterministic classifier
   still makes the FINAL enum decision + policy gate. The LLM never owns the decision.
3. **Arm 2 — the LLM does the whole decision** (current plan): Qwen3.5-4B (or 9B) gbnf-constrained
   to `CoordinatorDecision` directly.

**Headline metric**: false-confidence rate (a confidently-wrong label, esp. on `dataSensitivity`),
then gate-pass per tier. The question is not "which model is smartest", it's "which arm clears the
gates at the lowest cost and lowest mislabel-on-sensitive risk".

The cleanest conclusion for each ranking: (a) Arm 0 passes → drop the LLM entirely; (b) only Arm 1
passes → the 0.8B feature-extractor is the sweet spot, final decisions stay deterministic; (c) only
Arm 2 passes → an LLM genuinely owns the decision (the unlikely-but-honest case); (d) none pass
zero-shot → the gate tier needs a fine-tune.

### Table: which field belongs to which arm (from the fixture analysis)

| field | dominant signal | owner |
|---|---|---|
| dataSensitivity (gate) | filename+lexicon (`vault/`, `auth/`, `billing/`, `signing_key`) | deterministic + hard rule |
| backend | `tacticus`↔absis, `secret`↔local, else default | deterministic |
| approvalRequired | rule on `release`/`security`/`release_blocking` | deterministic |
| risk | lexicon (`auth bypass`, `signing key`) | deterministic (+LLM hint) |
| domain / taskType | keyword + short-text semantics | LLM hint (+lexicon) |
| verificationMode | `refactor`/`analy`/`security` keywords | deterministic |

## 7. Order of operations (research → protocol → benchmark)

1. Fill the candidate matrix (§3) with exact GGUF quant/context/offload via model research.
2. Pin the serving config (§5.2) and write the `decide_fn` adapter for llama-server/ollama.
3. Stub baseline run → record floor.
4. One-per-tier live run, captured with full provenance (§5.1).
5. Compare: zero-shot small vs big+confidence vs bare-gap, by tier, with false-confidence rate as
   the headline metric (not just accuracy).
