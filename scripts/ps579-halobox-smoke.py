#!/usr/bin/env python3
"""HALOBOX_ADAPTER_SMOKE_TEST - the minimum real invocation that proves the path.

NOT a PS-579 benchmark cell and NOT a corpus packet. It uses no canonical task
identity and contributes nothing to corpus results. Its only claim is that the
ADAPTER works end to end through the PRODUCTION path:

    ExecutionPackage -> PS-605 decision over the PERSISTED PS-632 receipt
    -> DispatchDecisionReceipt -> pin check -> llama-server invocation
    -> AttemptReceipt -> deterministic VerificationReceipt -> EvidencePackage
    -> validator verdict, with receipt_ref_matches_store = true.

The semantic check is deliberately thin and deterministic: the model must write
the exact unguessable nonce into one file through a real tool call, and the check
reads that file. Prose, or a tool call that wrote nothing, fails.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "ps635-live"))

import runtime_client                                   # noqa: E402
from src import dispatch_boundary as dbd                # noqa: E402
from src import local_target_routing as ltr             # noqa: E402
from src.local_targets import target_by_id              # noqa: E402
from src.attempt_receipt import (                       # noqa: E402
    ArtifactRef, context_projection_digest, make_attempt_receipt,
    make_verification_receipt)
from src.evidence_package import (                      # noqa: E402
    seal_evidence_package, validate_evidence_package)
from src.execution_package import (                     # noqa: E402
    VerificationPlan, build_execution_package, make_dispatch_receipt,
    default_requirements, seal_verifier_digests)
from src.evidence_contract import (                     # noqa: E402
    INDEPENDENCE_HARNESS_HIDDEN, KIND_CONTEXT_DELIVERY, EvidenceRequirement)
from src.source_snapshot import take_source_snapshot    # noqa: E402
from src.target_capability_store import store_from_env  # noqa: E402

TARGET = "local-framework-halobox"
NONCE = "HBX-" + uuid.uuid4().hex[:12].upper()
#: The SAME tool schema the worker harness offers, so the smoke exercises the real
#: write path rather than a bespoke one.
WRITE_TOOL = [{
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write one file inside the permitted write scope.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}}, "required": ["path", "content"]}}},
]


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def main() -> int:
    run_id = "halobox-adapter-smoke-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ROOT / "docs" / "benchmark-ps579" / "halobox-adapter-smoke" / run_id
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    write_rel = ("docs/benchmark-ps579/halobox-adapter-smoke/" + run_id
                 + "/artifacts/smoke_nonce.txt")

    packet = {
        "packet_id": "HALOBOX-ADAPTER-SMOKE",
        "objective": ("Adapter smoke only: write one file containing the exact "
                      "nonce given in the request. No corpus task."),
        "contract": ("Call write_file once with path '" + write_rel + "' and content "
                     "exactly the nonce from the user message."),
        "target_requirements": ["native_tools"],
        "write_scope": [write_rel],
        "interface": [{"name": "nonce", "required": True, "type_hint": "str",
                       "semantics": "the exact nonce to write"}],
        "test_command": "smoke:nonce-file-content",
        "acceptance_criteria": ["the file exists and holds exactly the nonce"],
        "negative_control": "prose, or a tool call that wrote nothing, must FAIL",
        "stop_conditions": ["any transport error"],
        "role": "local_implementer",
        "base_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    }

    verifier_id = "smoke:nonce-file-content"
    plan = VerificationPlan(
        verifier_id=verifier_id, command="smoke:nonce-file-content",
        verifier_paths=[], verifier_digests=seal_verifier_digests(str(ROOT), []),
        positive_control="the written file holds exactly the nonce",
        negative_control="prose, or a tool call that wrote nothing, must FAIL")
    requirements = default_requirements(
        plan, negative_control=packet["negative_control"]) + (
        EvidenceRequirement(
            requirement_id="context_delivery", kind=KIND_CONTEXT_DELIVERY,
            vantage="adapter smoke workspace",
            expected_verifier="the sealed smoke transcript",
            independence=INDEPENDENCE_HARNESS_HIDDEN),)
    head_sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    packet["base_sha"] = head_sha or packet["base_sha"]
    snapshot = take_source_snapshot(
        str(ROOT), base_sha=packet["base_sha"],
        relevant_paths=[write_rel])
    ep = build_execution_package(
        packet,
        source=snapshot,
        verification=plan, run_id=run_id,
        package_id=packet["packet_id"] + ":" + run_id,
        jira_key="PS-579", execution_role="local_implementer", allowed_tools=("write_file",),
        network_policy="tailnet-loopback (ssh to the target's HaloBox loopback)",
        evidence_requirements=requirements, write_scope=[write_rel])

    # ---- PS-605 decides, over the PERSISTED PS-632 receipt ---------------------
    store = store_from_env()
    persisted = ltr.persisted_routing_inputs(store)
    write_json(run_dir / "capability_inputs.json", {
        "store": store.directory, "store_audit": store.verify(),
        "bound_receipts": dict(persisted.bound_receipts),
        "candidates": [{"target_id": p.target_id, "profile_id": p.profile_id}
                       for p in persisted.profiles],
        "skipped": [dict(s) for s in persisted.skipped]})
    bound = ltr.resolve_persisted_dispatch(
        persisted, packet=packet, role="local_implementer",
        execution_package_hash=ep.package_hash, run_id=run_id,
        preferred_target_id=TARGET, decision_id="dec-smoke-" + run_id)
    dispatch = make_dispatch_receipt(**bound.decision.to_ps638_receipt_kwargs())
    write_json(run_dir / "dispatch_receipt.json", dispatch.to_dict())
    write_json(run_dir / "routing_decision.json", bound.decision.to_dict())

    selected = bound.decision.selected_profile.profile_id
    pin = bound.pin_for(selected)
    spec = target_by_id(dispatch.selected_target_id)
    client = runtime_client.client_for_target(dispatch.selected_target_id, spec=spec)
    try:
        dbd.verify_invocation(bound, profile_id=selected, model=client.model,
                              chat_url=spec.endpoint or "")
    except Exception as exc:                       # pin violation = zero calls
        write_json(run_dir / "result.json", {
            "run_id": run_id, "result": "PIN_VIOLATION", "reason": str(exc),
            "model_calls": 0, "dispatch_receipt_hash": dispatch.receipt_hash})
        print(run_id + ": PIN_VIOLATION - " + str(exc) + " (0 model calls)")
        return 0

    # ---- the invocation -------------------------------------------------------
    from src.worker_context import render_worker_context

    # The SHIPPED renderer builds the worker context, so the declared interface is
    # really delivered and the sealed interface is present in what the model saw.
    context = (render_worker_context(packet)
               + "\n\nWrite the file '" + write_rel + "' using the write_file tool. "
               "Its content must be exactly:\n" + NONCE + "\nNothing else.")
    (run_dir / "artifacts" / "smoke-context.txt").write_text(context)
    started = time.monotonic()
    call = client.api_streaming_chat(
        [{"role": "user", "content": context}], num_ctx=32768, tools=WRITE_TOOL,
        num_predict=256)
    elapsed = round(time.monotonic() - started, 3)
    body = call.body or {}
    message = body.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    model_text = (message.get("content") or "")
    transcript = json.dumps({"content": model_text, "tool_calls": tool_calls}, indent=2)
    (run_dir / "artifacts" / "smoke-model-output.txt").write_text(transcript)

    # Execute the tool call the way the worker dispatcher does: the path must be
    # inside the DECLARED write scope, and only then is it written. A path outside
    # the scope is a policy failure, never a silent write.
    writes = []
    policy_error = ""
    for entry in tool_calls:
        args = (entry.get("function") or {}).get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            policy_error = "tool call arguments were not an object"
            break
        path = str(args.get("path") or "").lstrip("./")
        if path != write_rel:
            policy_error = "out-of-scope path: " + repr(path)
            break
        destination = ROOT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(str(args.get("content") or ""))
        writes.append(path)

    # Deterministic check: the nonce must be IN THE FILE the tool wrote. Reading the
    # artifact is the verification; a tool-call count alone proves nothing.
    nonce_path = ROOT / write_rel
    file_content = nonce_path.read_text().strip() if nonce_path.exists() else ""
    passed = (not policy_error) and file_content == NONCE
    checks = {
        "transport_ok": bool(call.ok), "runtime_error": call.runtime_error,
        "tool_calls": len(tool_calls), "writes_executed": writes,
        "policy_error": policy_error,
        "nonce_file": write_rel, "nonce_expected": NONCE,
        "nonce_file_content": file_content, "nonce_written_through_tool": passed,
        "prompt_tokens": body.get("prompt_eval_count"),
        "completion_tokens": body.get("eval_count"),
        "chunks": body.get("_chunks"), "ttft_s": call.ttft_s,
        "num_ctx_requested": body.get("_num_ctx_requested"),
        "num_ctx_served": body.get("_num_ctx_served"),
    }
    write_json(run_dir / "smoke_check.json", checks)

    def stored(name: str, text: str) -> dict:
        data = text.encode()
        (run_dir / "artifacts" / name).write_bytes(data)
        return {"artifact_id": name, "sha256": hashlib.sha256(data).hexdigest(),
                "media_type": "text/plain", "size": len(data),
                "storage_uri": "file://" + str(run_dir / "artifacts" / name)}

    ctx_ref = stored("smoke-context.txt", context)
    out_ref = stored("smoke-model-output.txt", transcript)
    ended = datetime.now(timezone.utc).isoformat()
    attempt = make_attempt_receipt(
        receipt_id="attempt-" + run_id + "-1", run_id=run_id,
        packet_id=packet["packet_id"], attempt=1, repair_of=0,
        execution_package_hash=ep.package_hash,
        dispatch_receipt_hash=dispatch.receipt_hash,
        target_id=dispatch.selected_target_id,
        host=str(pin.get("host") or ""), model=str(pin.get("model") or ""),
        runtime_kind=str(pin.get("runtime_kind") or "llama-server"),
        runtime_version=str(dispatch.selected_runtime_version or ""),
        model_version=str(dispatch.selected_model_digest or ""),
        context_projection_hash=context_projection_digest(context),
        rendered_context_ref=ArtifactRef(**ctx_ref),
        output_ref=ArtifactRef(**out_ref),
        requested_context=32768, served_context=int(body.get("_num_ctx_served") or 0),
        ended_at=ended, elapsed_s=elapsed,
        finish_reason=str(body.get("finish_reason") or "")[:200],
        prompt_tokens=int(body.get("prompt_eval_count") or 0),
        completion_tokens=int(body.get("eval_count") or 0),
        tool_invocations=tuple(
            {"name": (c.get("function") or {}).get("name", "write_file")}
            for c in tool_calls),
        declared_write_set=(write_rel,),
        actual_write_set=(write_rel,) if passed else (),
        artifact_refs=(ArtifactRef(**ctx_ref), ArtifactRef(**out_ref)),
        failure_class="" if passed else "technical")
    write_json(run_dir / "attempt_receipt-1.json", attempt.to_dict())

    verification = make_verification_receipt(
        receipt_id="verify-" + run_id + "-1", run_id=run_id,
        packet_id=packet["packet_id"], attempt=1,
        execution_package_hash=ep.package_hash, verifier_id=verifier_id,
        verifier_digest="", verifier_paths=(),
        source_snapshot_digest=str(getattr(snapshot, "snapshot_digest", "")) or "smoke",
        normalized_command="smoke:nonce-file-content",
        exit_code=0 if passed else 1,
        stdout_ref=ArtifactRef(**out_ref), stderr_ref=ArtifactRef(**out_ref),
        stdout_complete=True, stderr_complete=True,
        stdout_bytes=out_ref["size"], stderr_bytes=0,
        tests_collected=1, tests_executed=1, tests_passed=1 if passed else 0,
        tests_failed=0 if passed else 1, tests_skipped=0,
        expected="the written file holds exactly the nonce",
        observed="nonce_file_content='" + file_content + "'",
        control_id="smoke:write_file-with-the-nonce",
        control_expected="FAIL if the file is missing or holds anything else",
        control_observed="tool_calls=" + str(len(tool_calls)),
        control_passed=passed,
        worktree=str(ROOT), host=str(pin.get("host") or ""),
        started_at=ended, ended_at=ended, elapsed_s=elapsed,
        requirement_ids=tuple(r.requirement_id for r in ep.evidence_requirements),
        proof_class="HARNESS_HIDDEN", failure_fingerprint="")
    write_json(run_dir / "verification_receipt-1.json", verification.to_dict())

    refs = list(dispatch.capability_receipt_refs)
    chain = {
        "execution_package_hash": ep.package_hash,
        "dispatch_receipt_hash": dispatch.receipt_hash,
        "capability_receipt_refs": refs,
        "stored_receipt_hash": persisted.bound_receipts.get(
            dispatch.selected_target_id, ""),
        "selected_profile_id": selected,
    }
    chain["receipt_ref_matches_store"] = bool(refs) and refs[0] == chain["stored_receipt_hash"]
    chain["attempt_binds_dispatch"] = (
        attempt.dispatch_receipt_hash == dispatch.receipt_hash
        and attempt.execution_package_hash == ep.package_hash)
    chain["ok"] = bool(chain["receipt_ref_matches_store"] and chain["attempt_binds_dispatch"])
    write_json(run_dir / "dispatch_chain.json", chain)

    package = seal_evidence_package(
        evidence_package_id="evpkg-" + run_id, execution_package=ep,
        dispatch_receipts=(dispatch,), attempt_receipts=(attempt,),
        verification_receipts=(verification,))
    write_json(run_dir / "evidence_package.json", package.to_dict())
    validation = validate_evidence_package(package.to_dict())
    write_json(run_dir / "validation.json", validation.to_dict())

    summary = {
        "kind": "HALOBOX_ADAPTER_SMOKE_TEST", "run_id": run_id,
        "corpus_cell": False,
        "corpus_note": ("adapter smoke only: not a PS-579 benchmark cell and excluded "
                        "from every corpus aggregate"),
        "target_id": dispatch.selected_target_id, "profile_id": selected,
        "receipt_hash": chain["stored_receipt_hash"],
        "capability_receipt_refs": refs,
        "decided_by": dispatch.decided_by, "policy_ref": dispatch.policy_ref,
        "endpoint": spec.endpoint, "runtime_kind": spec.runtime_kind,
        "runtime_version": dispatch.selected_runtime_version,
        "model": dispatch.selected_model, "model_digest": dispatch.selected_model_digest,
        "chain": chain, "smoke_check": checks,
        "execution_package_hash": ep.package_hash,
        "attempt_receipt_hash": attempt.receipt_hash,
        "verification_receipt_hash": verification.receipt_hash,
        "evidence_package_hash": package.evidence_package_hash,
        "validator_ok": validation.ok, "validator_codes": list(validation.codes),
        "wall_clock_s": elapsed,
    }
    write_json(run_dir / "run_summary.json", summary)

    print("=== " + run_id + " ===")
    print("target      : " + dispatch.selected_target_id + " (" + str(spec.endpoint) + ")")
    print("profile     : " + selected)
    print("receipt     : " + chain["stored_receipt_hash"][:24]
          + "... refs_match=" + str(chain["receipt_ref_matches_store"]))
    print("decided_by  : " + dispatch.decided_by + "  " + dispatch.policy_ref)
    print("nonce       : " + NONCE + " -> written_via_tool=" + str(passed)
          + "  tool_calls=" + str(len(tool_calls)))
    print("latency     : wall=" + str(elapsed) + "s ttft=" + str(call.ttft_s)
          + " chunks=" + str(body.get("_chunks")))
    print("package     : " + package.evidence_package_hash[:24] + "... validator="
          + ("VERIFIED" if validation.ok else "REJECTED " + str(list(validation.codes))))
    print("artifacts   : " + str(run_dir))
    return 0 if (passed and chain["ok"] and validation.ok) else 1


if __name__ == "__main__":
    sys.exit(main())
