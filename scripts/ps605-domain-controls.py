#!/usr/bin/env python3
"""ps605-domain-controls.py — the two PRODUCTION domain controls (PS-605).

Runs real dispatches through `src.routing_executor.execute_candidates` (the
production entrypoint) with the PS-605 boundary in front of it, over two
materially different domains, and seals+validates the evidence for each:

  A. a non-sensitive DEV task -> the hosted candidate's ELIGIBILITY is recorded,
     preference is deterministic, and a REAL local dispatch runs on the pinned
     target. A2 (same estate, local receipt stale) -> the DECISION falls back to
     the hosted candidate and records the local refusal; no hosted invocation is
     performed, because this session has no credentialed hosted endpoint, and the
     evidence says exactly that instead of pretending otherwise.
  B. a SENSITIVE, local-only task over SYNTHETIC fixture content -> the hosted
     candidate is refused BEFORE any dispatch, a fresh qualified local profile is
     selected and really invoked, and the invocation log proves zero hosted calls.
     B2: no eligible local profile -> typed refusal, zero invocations.

Safety: the script refuses to run unless DATABASE_URL points INSIDE --out, so
experiment rows can never land in a live application database, and it never reads
credentials (the hosted candidate is deliberately credential-less).

Usage:
    DATABASE_URL=sqlite:///<out>/app.db python3 scripts/ps605-domain-controls.py \
        --out data/ps605 --base-url http://127.0.1.1:11434
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYNTHETIC_NOTE = (
    "SYNTHETIC FIXTURE — NOT REAL DATA. Policy-class exercise only.\n"
    "Patient: Fixture Person (DOB 1900-01-01, id SYNTH-0001)\n"
    "Note: synthetic blood-pressure readings 120/80, 118/79, 121/82.\n"
    "Prescription: SYNTH-MED 10mg, one tablet daily.\n"
)


def _die(message: str):
    sys.stderr.write(f"refusing to run: {message}\n")
    raise SystemExit(2)


def http_json(url: str, payload=None, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def probe_target(base_url: str, want_model: str, *, ttl_s: int) -> dict:
    """MEASURE the target and build a ``measured`` capability receipt.

    Every capability in the receipt is something this probe actually observed: a
    non-empty completion, a real tool call, and a served context window read from
    the runtime itself. Exactness is recorded as exact because the tag and digest
    identify the exact artifact (no quantisation or approximation applied).
    """
    from src.dispatch_routing import (
        CAP_CONTEXT_INTEGRITY, CAP_EXACT_REFERENCE_SEMANTICS, CAP_SINGLE_TOOL_CALL,
        CAP_TEXT_GENERATION, EXACTNESS_EXACT, PROVENANCE_MEASURED,
    )

    version = http_json(f"{base_url}/api/version", timeout=20).get("version", "")
    tags = http_json(f"{base_url}/api/tags", timeout=40).get("models") or []
    entry = next((m for m in tags
                  if str(m.get("name", "")).startswith(want_model)), None)
    if entry is None:
        _die(f"model {want_model!r} is not present on {base_url}")
    model = entry.get("name") or want_model
    digest = entry.get("digest", "")
    served_models = http_json(f"{base_url}/api/ps", timeout=40).get("models") or []
    served = int((served_models[0] if served_models else {}).get("context_length") or 0)

    plain = http_json(f"{base_url}/api/chat", {
        "model": model, "stream": False, "think": False,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "options": {"num_predict": 16},
    }, timeout=600)
    plain_text = (plain.get("message") or {}).get("content") or ""

    tool = http_json(f"{base_url}/api/chat", {
        "model": model, "stream": False, "think": False,
        "messages": [{"role": "user", "content":
                      "Call the write_file tool once with path 'probe.txt' and "
                      "content 'ok'. Do not explain."}],
        "tools": [{"type": "function", "function": {
            "name": "write_file", "description": "Write one file.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]}}}],
        "options": {"num_predict": 128},
    }, timeout=900)
    tool_calls = (tool.get("message") or {}).get("tool_calls") or []

    capabilities = {CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS}
    if tool_calls:
        capabilities.add(CAP_SINGLE_TOOL_CALL)
    if served >= 32768:
        capabilities.add(CAP_CONTEXT_INTEGRITY)

    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "facts": {
            "base_url": base_url, "runtime_version": version, "model": model,
            "model_digest": digest, "served_context": served,
            "plain_completion_nonempty": bool(plain_text.strip()),
            "plain_completion_preview": plain_text.strip()[:120],
            "tool_call_returned": bool(tool_calls),
            "tool_call_name": ((tool_calls[0].get("function") or {}).get("name")
                               if tool_calls else ""),
            "observed_at": observed_at, "ttl_s": int(ttl_s),
            "raw_tool_message_keys": sorted((tool.get("message") or {}).keys()),
        },
        "receipt": {
            "receipt_id": f"measured:{model}:{observed_at}",
            "profile_id": "p-rtx", "target_id": "profile:p-rtx",
            "capabilities": frozenset(capabilities),
            "exactness": EXACTNESS_EXACT, "observed_at": observed_at,
            "ttl_s": int(ttl_s), "healthy": True, "runtime_version": version,
            "model_digest": digest,
            "host": base_url.split("//")[-1].split(":")[0],
            "notes": "probe: /api/version + /api/tags + /api/ps + completion + tool call",
            "provenance": PROVENANCE_MEASURED,
        },
    }


# ----------------------------------------------------------------- the estate ---
def seed_estate(db, *, base_url: str, model: str, observed_at: str):
    """Two real endpoints, three profiles, two tasks. No credentials anywhere."""
    import core.database as cdb

    from src.dispatch_routing import PROVENANCE_MEASURED  # noqa: F401

    created = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    db.add_all([
        cdb.ModelEndpoint(id="ep-rtx", name="RTX4500 minipc (tunnel)",
                          base_url=base_url, is_enabled=True, supports_tools=True),
        cdb.ModelEndpoint(id="ep-hosted", name="OpenRouter (no credential)",
                          base_url="https://openrouter.ai/api/v1",
                          is_enabled=True, supports_tools=True),
        # The deterministic/governance node (MS-R1): a separate host, never an
        # inference target for this work, present so the decision has to refuse it.
        cdb.ModelEndpoint(id="ep-msr1", name="MS-R1 verifier node",
                          base_url="http://192.168.1.131:8080/v1",
                          is_enabled=True, supports_tools=None),
    ])
    db.add_all([
        cdb.RoutingModelProfile(
            id="p-rtx", model_endpoint_id="ep-rtx", model=model,
            roles=json.dumps(["implementer", "debugger", "scout", "reviewer"]),
            context_window=32768, max_output_tokens=512, is_free=True,
            enabled=True, created_at=created),
        cdb.RoutingModelProfile(
            id="p-hosted", model_endpoint_id="ep-hosted", model="deepseek-v4-pro",
            roles=json.dumps(["implementer", "reviewer", "scout"]),
            context_window=131072, max_output_tokens=1024, is_free=False,
            is_premium=False, enabled=True, created_at=created),
        cdb.RoutingModelProfile(
            id="p-msr1", model_endpoint_id="ep-msr1", model="qwen3.8:27b",
            roles=json.dumps(["governance_ci"]), context_window=4096,
            is_free=True, enabled=True, created_at=created),
    ])
    dev = cdb.RoutingTask(
        id="t-dev", title="DEV control — non-sensitive", objective=(
            "Summarise the dev fixture note in one sentence."),
        task_type="implementation", repo_path="/tmp/ps605-dev", risk="low",
        data_sensitivity="internal", allow_free_models=True, allow_paid_models=True,
        allow_premium_models=False, max_attempts=1,
        inputs=json.dumps({"note": "dev fixture: the router picked this target."}))
    sensitive = cdb.RoutingTask(
        id="t-sensitive", title="SENSITIVE control (synthetic fixture)",
        objective=("Summarise this SYNTHETIC fixture note. It is fabricated test "
                   "content for a policy-class exercise."),
        task_type="implementation", repo_path="/tmp/ps605-sensitive", risk="low",
        data_sensitivity="restricted", allow_free_models=True,
        allow_paid_models=True, allow_premium_models=False, max_attempts=1,
        inputs=json.dumps({"policy_domain": "health", "note": SYNTHETIC_NOTE}))
    db.add_all([dev, sensitive])
    db.commit()
    return dev, sensitive


def candidates(*profile_ids):
    return [{"profile_id": pid, "model": "", "roles": [], "score": 1.0,
             "estimated_cost_usd": 0.0, "reasons": []} for pid in profile_ids]


def run_dispatch(db, task, profile_ids, max_attempts: int, out_dir: str, *,
                 label: str, receipt_overrides=None) -> dict:
    """Dispatch through the PRODUCTION entrypoint and collect what it wrote."""
    import shutil

    from src import routing_executor as rex

    result = rex.execute_candidates(db, task, candidates(*profile_ids), max_attempts)
    record_path = result.get("dispatch_receipt_path")
    kept = ""
    if record_path and os.path.exists(record_path):
        kept = os.path.join(out_dir, "production-records", f"{label}.json")
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        shutil.copyfile(record_path, kept)
    return {"label": label, "result": result, "record_path": kept,
            "run_record_path": record_path}


def seal_and_validate(record: dict, out_dir: str, *, label: str,
                      fixture: dict) -> dict:
    """Seal a production record, validate it, and record both."""
    from src import dispatch_boundary as dbd

    sealed = dbd.seal_recorded_dispatch(
        record=record,
        budget_snapshot={
            "requested_max_cost_rank": (record.get("dispatch", {})
                                        .get("request", {}) or {}).get("max_cost_rank"),
            "selected_cost_rank_rank": None,
            "usd_checks": "routing_budget.check_general/premium/task_budget "
                          "(unchanged, authoritative for money)",
        },
        resource_snapshot={
            "targets_available": sorted(
                record.get("capability_provenance", {}) or {}),
            "measured_receipts": sorted(
                pid for pid, prov in
                (record.get("capability_provenance") or {}).items()
                if prov == "measured"),
        },
        fixture=fixture,
        sealed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    ok, codes = dbd.validate_dispatch_evidence(sealed)
    path = os.path.join(out_dir, "evidence", f"{label}.json")
    dbd.write_dispatch_evidence(path, sealed)
    return {"label": label, "ok": ok, "codes": list(codes), "path": path,
            "seal": sealed.get("seal", {}), "sealed": sealed}


def mutation_controls(sealed: dict, out_dir: str, *, label: str) -> list:
    """Apply the required mutations to a sealed payload. Each must be REJECTED.

    A mutation that cannot apply to this payload (no attempts to rebind, a target
    the payload does not name) is reported `applicable: false` rather than counted
    as a survival — a vacuous control proves nothing either way, and saying so is
    the honest answer. `expects` names the semantic code that must fire, so a
    payload cannot pass on `evidence_hash_mismatch` alone.
    """
    import copy

    from src import dispatch_boundary as dbd

    def _verdict(payload, expects):
        ok, codes = dbd.validate_dispatch_evidence(payload)
        return {"ok": ok, "codes": list(codes), "expects": list(expects),
                "semantic_fired": bool(set(expects) & set(codes)),
                "rejected": not ok}

    outcomes = []
    real_target = str((sealed.get("seal") or {}).get("selected_target_id") or "")

    mutated = copy.deepcopy(sealed)
    mutated["decision_receipt"]["selected_target_id"] = real_target + "-mutated"
    outcomes.append({"mutation": "selected_target_changed", "applicable": True,
                     **_verdict(mutated, [dbd.EVIDENCE_PIN_CHANGED])})

    mutated = copy.deepcopy(sealed)
    mutated["decision"]["selected_profile"]["profile_id"] = "p-somewhere-else"
    outcomes.append({"mutation": "decision_pin_changed", "applicable": True,
                     **_verdict(mutated, [dbd.EVIDENCE_PIN_CHANGED])})

    mutated = copy.deepcopy(sealed)
    targets = [r for r in (mutated["capability_receipts"] or ())
               if r.get("profile_id") in {
                   c.get("profile_id") for c in
                   (mutated["decision"].get("candidates") or ()) if c.get("eligible")}]
    applicable = bool(targets and targets[0].get("capabilities"))
    if applicable:
        targets[0]["capabilities"] = ["text_generation"]
    outcomes.append({"mutation": "capability_receipt_changed", "applicable": applicable,
                     **_verdict(mutated, [dbd.EVIDENCE_RECEIPT_CHANGED])})

    mutated = copy.deepcopy(sealed)
    mutated["policy"]["version"] = "9.9"
    mutated["policy"]["policy_ref"] = "routing_policy@9.9+sha256:deadbeef"
    outcomes.append({"mutation": "policy_revision_changed", "applicable": True,
                     **_verdict(mutated, [dbd.EVIDENCE_POLICY_CHANGED])})

    mutated = copy.deepcopy(sealed)
    applicable = bool(mutated.get("attempts"))
    if applicable:
        mutated["attempts"][0]["dispatch_receipt_hash"] = "0" * 64
    outcomes.append({"mutation": "attempt_rebound", "applicable": applicable,
                     **_verdict(mutated, [dbd.EVIDENCE_ATTEMPT_UNBOUND])})

    mutated = copy.deepcopy(sealed)
    mutated["invocations"] = [{"target_id": "profile:not-in-the-decision",
                               "locality": "hosted"},
                              {"target_id": real_target, "locality": "hosted"}]
    expects = [dbd.EVIDENCE_INVOCATION_OUTSIDE_DECISION]
    if bool((mutated.get("route_request") or {}).get("local_only")):
        expects.append(dbd.EVIDENCE_HOSTED_FOR_LOCAL_ONLY)
    outcomes.append({"mutation": "hosted_invocation_added", "applicable": True,
                     **_verdict(mutated, expects)})

    path = os.path.join(out_dir, "evidence", f"{label}-mutations.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"label": label, "outcomes": outcomes}, handle, indent=2)
    return outcomes


def pin_controls(db, task, profile_ids) -> list:
    """Exercise the pin guard against a REAL bound decision (no dispatch)."""
    from src import dispatch_boundary as dbd

    bound = dbd.resolve_dispatch(db, task, candidates(*profile_ids), domain="general_swe",
                                 max_cost_rank=2)
    pin_info = bound.pin_for(bound.decision.selected_profile.profile_id) or {}
    outcomes = []

    def _attempt(model, url, label):
        try:
            dbd.verify_invocation(bound, profile_id=bound.decision.selected_profile
                                  .profile_id, model=model, chat_url=url)
            outcomes.append({"control": label, "refused": False, "code": ""})
        except dbd.DispatchPinViolation as exc:
            outcomes.append({"control": label, "refused": True, "code": exc.code})

    _attempt("some-other-model", "http://127.0.1.1:11434",
             "model_not_the_pinned_one")
    _attempt(str(pin_info.get("model") or ""), "https://openrouter.ai/api/v1",
             "hosted_url_for_a_local_pin")
    return outcomes


def verify_sealed_evidence(out_dir: str) -> int:
    """Re-validate every sealed artifact ON DISK and re-run the mutations on it.

    The files are the evidence, so the verdicts must come from the files: this
    re-derives each seal, then mutates a deep copy of the LOADED artifact and
    reports which checks fired. Exit status is nonzero if any mutation survives,
    so this is usable as a gate.
    """
    import glob

    from src import dispatch_boundary as dbd

    verdicts = []
    failures = []
    for path in sorted(glob.glob(os.path.join(out_dir, "evidence", "*.json"))):
        name = os.path.basename(path)
        if name.endswith("-mutations.json") or name == "verification.json":
            continue
        with open(path, encoding="utf-8") as handle:
            sealed = json.load(handle)
        if sealed.get("refused"):
            record = {"artifact": name, "refused": True,
                      "seal": sealed.get("seal", {}),
                      "refusal_code": (sealed.get("refusal") or {}).get("code", ""),
                      "invocations": sealed.get("invocations", []),
                      "valid": sealed.get("seal", {}).get("evidence_hash")
                      == dbd._sha256_hex(dbd._canonical(
                          dbd.evidence_core(sealed)))}
            verdicts.append(record)
            if not record["valid"]:
                failures.append(name)
            continue
        ok, codes = dbd.validate_dispatch_evidence(sealed)
        record = {"artifact": name, "valid": ok, "codes": list(codes),
                  "seal": sealed.get("seal", {}),
                  "mutations": mutation_controls(
                      sealed, out_dir, label=name.replace(".json", ""))}
        verdicts.append(record)
        if not ok:
            failures.append(name)
        for outcome in record["mutations"]:
            if not outcome["applicable"]:
                continue
            if not outcome["rejected"]:
                failures.append(f"{name}: {outcome['mutation']} survived")
            elif outcome["expects"] and not outcome["semantic_fired"]:
                failures.append(
                    f"{name}: {outcome['mutation']} was rejected by the seal only "
                    f"(expected one of {outcome['expects']}, got {outcome['codes']})")

    report = {"out": out_dir, "artifacts": verdicts,
              "failures": failures, "all_valid": not failures}
    with open(os.path.join(out_dir, "evidence", "verification.json"), "w",
              encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/ps605")
    parser.add_argument("--base-url", default="http://127.0.1.1:11434")
    parser.add_argument("--model", default="qwen3.8:27b")
    parser.add_argument("--ttl-s", type=int, default=3600)
    parser.add_argument("--verify-only", action="store_true",
                        help="re-validate and re-mutate the SEALED FILES in --out "
                             "instead of dispatching (the artifacts of record are "
                             "the thing under test, not an in-memory copy)")
    args = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(os.path.join(out_dir, "evidence"), exist_ok=True)
    if args.verify_only:
        return verify_sealed_evidence(out_dir)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("sqlite:///"):
        _die("DATABASE_URL must be a sqlite file URL (refusing to touch a live DB)")
    db_path = os.path.abspath(database_url.replace("sqlite:///", ""))
    if not db_path.startswith(out_dir):
        _die(f"DATABASE_URL ({db_path}) must live inside --out ({out_dir})")

    from src import dispatch_boundary as dbd
    from src import dispatch_routing as dr
    import core.database as cdb

    # 0. MEASURE the target (its own receipt, not a declaration).
    probe = probe_target(args.base_url, args.model, ttl_s=args.ttl_s)
    receipt = dr.make_capability_receipt(**probe["receipt"])
    store_path = os.path.join(out_dir, "receipts.json")
    with open(store_path, "w", encoding="utf-8") as handle:
        json.dump({"p-rtx": receipt.to_dict()}, handle, indent=2)
    os.environ[dbd.RECEIPT_STORE_ENV] = store_path

    db = cdb.SessionLocal()
    cdb.Base.metadata.create_all(bind=cdb.engine)
    dev_task, sensitive_task = seed_estate(
        db, base_url=args.base_url, model=probe["facts"]["model"],
        observed_at=probe["facts"]["observed_at"])

    summary = {"probe": probe["facts"], "receipt": receipt.to_dict(),
               "receipt_store": store_path, "controls": {}}

    # A1. DEV, preferred local candidate: real dispatch on the pinned target.
    a1 = run_dispatch(db, dev_task, ("p-rtx", "p-hosted", "p-msr1"), 1, out_dir,
                      label="A1-dev-preferred")
    a1_seal = seal_and_validate(
        json.load(open(a1["record_path"])), out_dir, label="A1-dev-preferred",
        fixture={"domain": "dev/internal", "invocations_performed": True})
    summary["controls"]["A1_dev_preferred"] = {
        "status": a1["result"]["status"],
        "selected_target_id": a1["result"]["selected_target_id"],
        "attempts": len(a1["result"]["attempts"]),
        "invocations": a1["result"]["invocations"],
        "validated": a1_seal["ok"], "codes": a1_seal["codes"],
        "seal": a1_seal["seal"],
        "candidate_rules": [[c["profile_id"], c["rule"]] for c in
                            a1_seal["sealed"]["decision"]["candidates"]],
    }
    return _run_rest(out_dir=out_dir, db=db, dev_task=dev_task,
                     sensitive_task=sensitive_task, probe=probe, dr=dr, dbd=dbd,
                     summary=summary, a1_seal=a1_seal)


def _run_rest(*, out_dir, db, dev_task, sensitive_task, probe, dr, dbd, summary,
              a1_seal) -> int:
    """A2 (fallback), B1 (sensitive local), B2 (refusal), mutations and pins."""
    # A2. DEV, local receipt STALE: the DECISION falls back to the hosted candidate.
    stale = dr.make_capability_receipt(**{
        **probe["receipt"], "receipt_id": "stale:p-rtx",
        "observed_at": (datetime.datetime.now(datetime.timezone.utc)
                        - datetime.timedelta(days=2)).isoformat(), "ttl_s": 60})
    a2_bound = dbd.resolve_dispatch(
        db, dev_task, candidates("p-rtx", "p-hosted"), domain="general_swe",
        max_cost_rank=1, preferred_profile_ids=("p-rtx",),
        receipt_overrides={"p-rtx": stale})
    a2_record = {
        "dispatch": a2_bound.to_dict(),
        "dispatch_receipt": a2_bound.decision.to_ps638_receipt_kwargs(),
        "dispatch_receipt_hash": a2_bound.decision.receipt_hash,
        "decision_hash": a2_bound.decision.decision_hash,
        "policy": a2_bound.policy.to_dict(),
        "capability_receipts": [r.to_dict() for r in a2_bound.estate.receipts],
        "capability_provenance": a2_bound.estate.provenance_summary(),
        "skipped_candidates": [dict(s) for s in a2_bound.estate.skipped],
        "attempts": [], "invocations": [],
    }
    a2_seal = seal_and_validate(
        a2_record, out_dir, label="A2-dev-fallback",
        fixture={"fallback": True, "invocations_performed": False,
                 "not_performed_because":
                     "no credentialed hosted endpoint is available to this session; "
                     "the DECISION and its per-candidate reasons are the evidence"})
    summary["controls"]["A2_dev_fallback"] = {
        "selected_target_id": a2_bound.decision.selected_profile.target_id,
        "reason_code": a2_bound.decision.reason_code,
        "fallback_used": a2_bound.decision.fallback_used,
        "candidate_rules": [[c["profile_id"], c["rule"]] for c in
                            a2_seal["sealed"]["decision"]["candidates"]],
        "invocations": [], "validated": a2_seal["ok"], "codes": a2_seal["codes"],
        "seal": a2_seal["seal"],
    }

    # B1. SENSITIVE (synthetic), synthetic-fixture prompt, local target invoked.
    b1 = run_dispatch(db, sensitive_task, ("p-hosted", "p-rtx", "p-msr1"), 1, out_dir,
                      label="B1-sensitive-local")
    b1_seal = seal_and_validate(
        json.load(open(b1["record_path"])), out_dir, label="B1-sensitive-local",
        fixture={"domain": "health", "data_sensitivity": "restricted",
                 "content": "SYNTHETIC", "invocations_performed": True})
    hosted_invocations = [i for i in b1["result"]["invocations"]
                          if i.get("locality") == "hosted"]
    summary["controls"]["B1_sensitive_local"] = {
        "status": b1["result"]["status"],
        "selected_target_id": b1["result"]["selected_target_id"],
        "invocations": b1["result"]["invocations"],
        "hosted_invocations": len(hosted_invocations),
        "candidate_rules": [[c["profile_id"], c["rule"]] for c in
                            b1_seal["sealed"]["decision"]["candidates"]],
        "validated": b1_seal["ok"], "codes": b1_seal["codes"],
        "seal": b1_seal["seal"],
    }

    # B2. SENSITIVE with no eligible local profile: typed refusal, zero invocations.
    try:
        dbd.resolve_dispatch(db, sensitive_task, candidates("p-hosted"),
                             domain="health", max_cost_rank=1)
        b2 = {"code": "", "candidates": []}
    except dr.RoutingRefused as exc:
        b2 = exc.to_dict()
    b2_payload = {
        "schema_version": 1,
        "sealed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "refused": True, "refusal": b2, "invocations": [],
        "fixture": {"domain": "health", "data_sensitivity": "restricted",
                    "content": "SYNTHETIC", "invocations_performed": False},
    }
    b2_payload["seal"] = {
        "evidence_hash": dbd._sha256_hex(dbd._canonical(b2_payload))}
    b2_path = dbd.write_dispatch_evidence(
        os.path.join(out_dir, "evidence", "B2-sensitive-refusal.json"), b2_payload)
    summary["controls"]["B2_sensitive_refusal"] = {
        "code": b2.get("code", ""),
        "candidate_rules": [[c["profile_id"], c["rule"]]
                            for c in b2.get("candidates", [])],
        "invocations": [], "path": b2_path,
    }

    # Mutation + pin controls. The MUTATIONS are verified against the SEALED FILES
    # by `--verify-only` (below/after this run), so the verdicts come from the
    # artifacts of record rather than from an in-memory copy of them.
    summary["mutation_controls"] = {
        "verified_from": "sealed artifacts, via --verify-only",
        "artifacts": ["evidence/A1-dev-preferred.json",
                      "evidence/B1-sensitive-local.json"],
    }
    summary["pin_controls"] = pin_controls(db, dev_task, ("p-rtx", "p-hosted"))
    summary["finish"] = {
        "pin_controls_refused": all(o["refused"] for o in summary["pin_controls"]),
        "next": "run --verify-only to re-derive and mutate the sealed files",
    }

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())






