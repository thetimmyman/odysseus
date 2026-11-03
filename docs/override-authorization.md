# Operator-Override Authorization Boundary Redesign

## 1. Threat Model & Trust Boundary

### The Boundary
Evaluation of dispatch routing decisions can be pure: given inputs, `RoutingInspectorEvaluator` projects the selector's decision into an immutable, deterministically hashed `RoutingExplanation`.

**Authorization cannot be a pure function of caller-supplied data.**

In previous iterations, the authorization gate (`apply_operator_override`) treated objects passed by the caller (`CanonicalEvidence`, with caller-supplied `ExecutionTargetProfile`, `TargetCapabilityReceipt`, and `ProviderCapacityReceipt` instances) as facts. This architectural defect repeatedly allowed callers to forge profiles (claiming hosted targets were local/ollama), forge receipts (with fabricated TTLs, future expiration dates, or synthetic hashes), and circumvent policy denials.

### Threat Model
- **Untrusted Actor:** The caller of `apply_operator_override` (e.g. an operator API endpoint, worker, or test harness). The caller may supply arbitrary payloads, forged profile definitions, synthetic capability receipts, forged capacity grants, backdated or future timestamps, and manipulated explanation JSON.
- **Trusted Computing Base (TCB):**
  - Canonical stores: `TargetCapabilityStore.current()`, `CapacityRegistry`, `target_by_id`.
  - Authoritative policies: `routing_domain_policy.domain_policy()`.
  - Server-side clock: Monotonic/authoritative UTC instant within configured skew limits.
  - Secret signing key: `HMAC_KEY` held only by the authorization authority.

### The Core Invariant
> **The gate must never read an authorization fact from an object the caller can construct.**
> Evidence must be **references the gate resolves** — never **content the gate believes**.

---

## 2. Architecture & The Resolver Protocol

### Evidence References vs. Content Objects
Callers pass `OverrideEvidenceRefs`, containing only identities and reference handles:
```python
@dataclass(frozen=True)
class OverrideEvidenceRefs:
    run_id: str
    packet_id: str
    capability_receipt_refs: Tuple[str, ...] = ()
    capacity_receipt_refs: Tuple[str, ...] = ()
```
Callers cannot supply profiles, receipts, or capacity structures; caller-constructed content objects are strictly unrepresentable.

### The Resolver Interface
Resolution is decoupled via the runtime-checkable protocol `OverrideEvidenceResolver`:
```python
class OverrideEvidenceResolver(Protocol):
    def resolve_profile(self, target_id: str, profile_id: str) -> dr.ExecutionTargetProfile: ...
    def resolve_capability(self, profile_id: str) -> Optional[Any]: ...
    def resolve_capacity(self, profile: dr.ExecutionTargetProfile) -> Sequence[Any]: ...
    def resolve_request(self, run_id: str, packet_id: str) -> dr.RoutingRequest: ...
    def resolve_resources(self) -> Optional[Mapping[str, Mapping[str, Any]]]: ...
    def resolve_domain_policy(self, domain: str) -> Optional[rdp.DomainPolicy]: ...
```

Two concrete implementations are provided:
1. `CanonicalOverrideResolver`: Production resolver reading from canonical registries (`TargetCapabilityStore`, `CapacityRegistry`, `domain_policy`).
2. `InMemoryOverrideResolver`: In-memory resolver for isolated testing and migration environments.

---

## 3. Enforcement Mechanisms

1. **Reproduce, Don't Replace:**
   - The cited `RoutingExplanation` must be verified and hash-sealed.
   - The target must already have been eligible in the cited explanation (`recorded.eligible is True`). An override can never convert a policy DENY into an ALLOW.
   - The fresh evaluation at the override instant must re-verify that the target remains eligible.
   - The fresh evaluation cannot qualify any target that was not eligible in the cited explanation (`fresh_eligible.issubset(cited_eligible)`).
   - Per-row canonical facts (`provider`, `locality`, `endpoint_url`, `receipt_hash`, `capacity_receipt_refs`) in the fresh evaluation must match the cited explanation.
2. **Server-Governed Instant & Skew Bounds:**
   - Callers cannot backdate or forward-date the authorization instant. If an instant is supplied, it is validated against the server clock (`clock()` or wall-clock UTC) within a 30-second skew window.
   - The instant cannot precede the cited explanation's `observed_at`.
3. **Bounded Evidence Lifetime:**
   - Minted receipts carry `minted_at` and `valid_until`.
   - `valid_until = min(capability_expiry, capacity_expiry, minted_at + 300s)`.
   - A receipt cannot outlive its underlying capability or capacity evidence, and has a hard ceiling of 5 minutes.
4. **Locality Oracle Defense:**
   - In `src/routing_domain_policy.py`, `_PRIVATE_HOST_RE` no longer matches single-label dotless hosts (`|[^.]+$` dropped). Dotless hostnames (`openrouter`, `anthropic`) are never treated as local.
   - In `evaluate_route`, `locality` is an explicit parameter. When `locality == "hosted"`, `local = False` unconditionally, preventing endpoint strings (e.g. `http://127.0.0.1:11434/v1`) from conferring privacy permission.
5. **Keyed HMAC Receipts:**
   - Receipts are minted privately with an HMAC-SHA256 signature over their canonical fields.
   - `verify_override_receipt` re-derives authorization, checks the HMAC signature, structural hash, and validity window.

---

## 4. Bypass Mapping & Exploit Verification Matrix

| Bypass / Exploit | Description / Mechanism | Stopping Control |
|---|---|---|
| **B1 / X7-B1** | Row `locality="local"` on hosted target skips credential/entitlement check. | `evaluate_route` takes authoritative `locality` from profile; `InMemoryOverrideResolver` and `CanonicalOverrideResolver` read true target locality from store; loopback URL on hosted target cannot confer local status. |
| **B2 / X7-B2** | Dotless single-label hosts (`https://openrouter`) treated as local by `_private_host_re`. | `src/routing_domain_policy.py`: `_PRIVATE_HOST_RE` updated to remove `\|[^.]+$`. Dotless hostnames are strictly non-local. |
| **B3 / X7-B3** | Fabricated `eligible=True` and stripped `refusal_reasons` in explanation record. | The gate re-runs selector on resolved inputs; `authoritative.eligible` must be True in fresh evaluation; cited explanation's recorded candidate must also have `eligible=True`. |
| **B4 / X7-B4** | Forged `capacity_expires_at="9999-12-31..."` in record. | Capacity facts are resolved from `CapacityRegistry`, never read from candidate fields; gate parses real timestamps and validates freshness against `now`. |
| **B5 / X7-B5** | Restamped `receipt_observed_at` and `receipt_ttl_s=10**18` in record. | Capability receipts are resolved by ID/hash from registry; `receipt_ttl_s` and `observed_at` are re-verified against store facts. |
| **B6 / X1** | Forged profile in `CanonicalEvidence` claiming `hosted-a` is `ollama/local`. | Evidence is refs only (`OverrideEvidenceRefs`). Profiles are resolved exclusively from store. In `test_x1`, even if caller tries to pass a forged profile, the resolver rejects it or the gate catches the policy denial. |
| **X2** | Backdated instant into expired evidence window. | Server clock skew enforcement: `apply_operator_override` verifies `abs((now - clock()).total_seconds()) <= max_clock_skew_s`. |
| **X3** | Fabricated capacity receipt via `make_capacity_receipt`. | Capacity receipts resolved from `CapacityRegistry`; caller-fabricated receipts are not in registry. |
| **X4** | Fresh evaluation reproduces different eligible set than cited explanation. | Reproduction check: `fresh_eligible.issubset(cited_eligible)` and candidate facts must match. Newly eligible candidates cannot be substituted. |
| **X5** | Hosted profile with loopback URL under local-only sensitive domain. | `src/routing_domain_policy.py` and `dispatch_routing.py`: `locality="hosted"` sets `local = False` unconditionally. |
| **X6** | Hand-built `OverrideAuditReceipt` with plausible fields. | `verify_override_receipt` checks HMAC signature (`hmac.compare_digest`). Unauthenticated or forged receipts fail verification. |
| **X8** | Target refused by `resource_unavailable`. | `resolve_resources()` resolves live resource availability matrix; fresh selector flags `dr.REFUSED_RESOURCE`. |
| **X9** | Positive control: legitimate steer between two eligible candidates. | Deterministic receipt minted, `valid_until` bounded, HMAC signed, passes verification. |

---

## 5. What Remains Out of Reach

The redesigned boundary strictly defends against caller-fabricated facts and unauthorized overrides. However, several failure modes remain fundamentally outside the reach of the authorization gate:

1. **Compromised Resolver / Store Corruption:**
   - If an attacker has write access to the underlying `TargetCapabilityStore` or `CapacityRegistry`, they can inject genuine-looking profiles or validly signed capacity receipts at the source. The gate relies on store integrity as a root of trust.
2. **Revocation TOCTOU Between Authorization and Dispatch:**
   - An override receipt certifies eligibility at instant $t_{\text{minted}}$ with validity until $t_{\text{valid}}$. If an operator revokes credentials or a target crashes at $t \in (t_{\text{minted}}, t_{\text{dispatch}})$, the downstream dispatcher must perform its own pre-flight dispatch check. The override receipt authorizes candidate selection, but does not guarantee execution environment availability at dispatch time.
3. **Secret Key Compromise (HMAC):**
   - A keyed HMAC protects against in-process and serialization-level forgery by unprivileged callers. However, if the HMAC key is compromised or set to an insecure default in production configuration, callers can mint arbitrary receipts. Key management must remain guarded in Vaultwarden (`vault`).
4. **Network-Level DNS Spoofing / Internal Loopback Proxies:**
   - If internal DNS resolves a host like `local-runner.internal` to an external hosted egress, or an iptables/loopback proxy redirects 127.0.0.1 traffic out of the machine, domain routing policy cannot detect the deception at the application layer. OS-level network namespaces and egress filtering remain necessary.
