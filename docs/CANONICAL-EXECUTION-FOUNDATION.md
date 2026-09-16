# Canonical execution foundation

Odysseus keeps these authorities separate:

| Concern | Authority |
| --- | --- |
| catalog presence | registry/discovery |
| exact capability qualification | PS-632 TargetCapabilityReceipt |
| capacity and entitlement | PS-640 ProviderCapacityReceipt |
| routing, privacy, and policy legality | PS-605 |
| execution intent and provenance | PS-638 ExecutionPackage |
| dispatch decision | PS-605 DispatchDecisionReceipt |
| attempts, verification, and evidence | PS-638 |
| semantic acceptance | acceptance/review authority |
| landing | mechanical landing authority |

Catalog presence is not capability qualification. Capability qualification is
not capacity entitlement, policy legality, execution, verification, semantic
acceptance, or landing.

PS-605 selects only independently eligible targets. Runtime adapters execute
the already-authorized decision and have no routing or fallback authority.
PS-632 receipts are exact, content-addressed, freshness-bound, append-only
records with supersession and invalidation. PS-638 binds source, package,
dispatch, attempt, verifier, and artifact identity into deterministic evidence.
# Authority boundaries

Target discovery is not target qualification. PS-632 `TargetCapabilityReceipt`
and `TargetCapabilityStore` are the only capability authority; PS-605 consumes a
non-authoritative projection of a resolved PS-632 receipt to apply policy and
select a target. The legacy selector view is translation/test data only and is
never loaded from a store or treated as qualification.

Execution profile identity answers what would run and excludes observation time.
Receipt content identity additionally binds what was measured, the qualification
reference, freshness, and probe results. Configured, served, demonstrated,
semantic-verified, and measured-safe context remain separate fields.

Store ownership is explicit: PS-632 owns target capability receipts; PS-640 owns
provider capacity/entitlement receipts; PS-638 owns execution/evidence receipts;
PS-605 owns routing decisions; PS-641 owns production composition.
