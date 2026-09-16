import pytest

import src.mechanical_landing as ml


class Validation:
    def __init__(self, ok=True, issues=()):
        self.ok, self.issues = ok, tuple(issues)

    def explain(self):
        return "VERIFIED" if self.ok else "REJECTED"


def fixture(monkeypatch, *, head="abc", snapshot="snap", diff="diff"):
    source = {"head_sha": head, "snapshot_digest": snapshot,
              "tracked_diff_digest": diff}
    package = {"evidence_package_hash": "evidence-1",
               "execution_package": {"source": dict(source),
                                      "write_scope": ["src/x.py"],
                                      "actual_write_set": []}}
    monkeypatch.setattr(ml, "validate_evidence_package", lambda *a, **k: Validation())
    monkeypatch.setattr(ml, "snapshot_digest_is_valid", lambda p: True)
    acceptance = ml.make_semantic_acceptance(
        acceptance_id="a-1", reviewer_id="reviewer", evidence_package_hash="evidence-1",
        candidate_source_digest=snapshot, candidate_head_sha=head,
        candidate_diff_digest=diff, observed_at="t", accepted_at="t")
    governance = (ml.GovernanceResult("required", "GREEN"),)
    policy = ml.LandingPolicy((ml.LandingStrategy.SQUASH,), ("required",))
    return package, source, acceptance, governance, policy


def eligible(monkeypatch, **changes):
    package, source, acceptance, governance, policy = fixture(monkeypatch)
    package.update(changes.pop("package", {}))
    return ml.evaluate_landing_eligibility(
        package, changes.pop("acceptance", acceptance),
        current_source=changes.pop("current_source", source),
        governance=changes.pop("governance", governance), policy=changes.pop("policy", policy),
        strategy=changes.pop("strategy", ml.LandingStrategy.SQUASH),
        unresolved_dispositions=changes.pop("unresolved", ()))


def test_matching_verified_inputs_are_eligible(monkeypatch):
    assert eligible(monkeypatch).eligible


def test_evidence_failures_are_distinguishable(monkeypatch):
    package, source, acceptance, governance, policy = fixture(monkeypatch)
    monkeypatch.setattr(ml, "validate_evidence_package",
                        lambda *a, **k: Validation(False, [type("I", (), {"code": "other"})()]))
    result = ml.evaluate_landing_eligibility(package, acceptance, current_source=source,
        governance=governance, policy=policy, strategy=ml.LandingStrategy.SQUASH)
    assert "EVIDENCE_NOT_VERIFIED" in result.codes


def test_stale_and_invalidated_evidence_refuse(monkeypatch):
    assert "EVIDENCE_STALE" in eligible(monkeypatch, package={"stale": True}).codes
    assert "EVIDENCE_INVALID" in eligible(monkeypatch, package={"invalidated": True}).codes


@pytest.mark.parametrize("mutation,code", [
    (lambda p, s, a: (None, None, None), "SEMANTIC_ACCEPTANCE_MISSING"),
    (lambda p, s, a: (a.__class__(**{**a.__dict__, "evidence_package_hash": "other"}), s,
                      a.__class__(**{**a.__dict__, "evidence_package_hash": "other"})), "ACCEPTANCE_SOURCE_MISMATCH"),
    (lambda p, s, a: (a, {**s, "head_sha": "changed"}, a), "CANDIDATE_CHANGED"),
])
def test_binding_negative_controls(monkeypatch, mutation, code):
    package, source, acceptance, governance, policy = fixture(monkeypatch)
    new_acceptance, current, supplied = mutation(package, source, acceptance)
    result = ml.evaluate_landing_eligibility(package, supplied if supplied is not None else None,
        current_source=current or source, governance=governance, policy=policy,
        strategy=ml.LandingStrategy.SQUASH)
    assert code in result.codes


def test_stale_invalid_governance_scope_and_rework_refuse(monkeypatch):
    package, source, acceptance, _, policy = fixture(monkeypatch)
    result = eligible(monkeypatch, governance=(ml.GovernanceResult("required", "RED"),),
                      unresolved=("BLOCKER",))
    assert "GOVERNANCE_NOT_GREEN" in result.codes
    assert "UNRESOLVED_REWORK" in result.codes
    package["execution_package"]["actual_write_set"] = ["secret.txt"]
    result = ml.evaluate_landing_eligibility(package, acceptance, current_source=source,
        governance=(ml.GovernanceResult("required", "GREEN"),), policy=policy,
        strategy=ml.LandingStrategy.SQUASH)
    assert "WRITE_SCOPE_MISMATCH" in result.codes


def test_strategy_must_be_explicitly_allowed(monkeypatch):
    result = eligible(monkeypatch, strategy=ml.LandingStrategy.MERGE)
    assert result.codes == ("LANDING_STRATEGY_NOT_ALLOWED",)


def test_equivalence_allows_sha_change_only_for_matching_material_identity(monkeypatch):
    _, _, acceptance, _, _ = fixture(monkeypatch)
    same = ml.prove_landed_equivalence(acceptance,
        {"head_sha": "squashed", "diff_digest": "diff"}, ml.LandingStrategy.SQUASH)
    different = ml.prove_landed_equivalence(acceptance,
        {"head_sha": "squashed", "diff_digest": "extra"}, ml.LandingStrategy.SQUASH)
    assert same.equivalent
    assert not different.equivalent


class Repo:
    def __init__(self, source, landed):
        self.source, self.landed, self.calls = source, landed, 0

    def current_source(self):
        return self.source

    def land(self, strategy):
        self.calls += 1
        return self.landed


class Jira:
    def __init__(self, fail=False): self.fail, self.receipts = fail, []
    def reconcile(self, receipt):
        self.receipts.append(receipt)
        if self.fail: raise RuntimeError("jira unavailable")
        return {"ok": True, "issue": "PS-578"}


def test_success_receipt_is_bound_and_never_implies_deployment(monkeypatch):
    package, source, acceptance, governance, policy = fixture(monkeypatch)
    jira = Jira()
    receipt = ml.land_exact_candidate(
        evidence_package=package, acceptance=acceptance,
        repository=Repo(source, {"head_sha": "landed", "tree_sha": "", "diff_digest": "diff",
                                 "repository": "repo", "destination_branch": "main"}),
        jira=jira, current_source=source, governance=governance, policy=policy,
        strategy=ml.LandingStrategy.SQUASH)
    assert receipt.receipt_hash
    assert receipt.evidence_package_hash == "evidence-1"
    assert receipt.semantic_acceptance_hash == acceptance.acceptance_hash
    assert receipt.deployment_implication == "none"
    assert receipt.reconciliation_state == "RECONCILED"


def test_jira_failure_reports_partial_repository_landing(monkeypatch):
    package, source, acceptance, governance, policy = fixture(monkeypatch)
    receipt = ml.land_exact_candidate(
        evidence_package=package, acceptance=acceptance,
        repository=Repo(source, {"head_sha": "landed", "diff_digest": "diff"}),
        jira=Jira(fail=True), current_source=source, governance=governance,
        policy=policy, strategy=ml.LandingStrategy.SQUASH)
    assert receipt.reconciliation_state == "REPOSITORY_LANDED_JIRA_FAILED"
    assert receipt.reconciliation_error_code == "JIRA_RECONCILIATION_FAILED"
    assert receipt.landed_head == "landed"
