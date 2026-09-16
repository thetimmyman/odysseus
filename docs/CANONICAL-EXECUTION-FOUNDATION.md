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
