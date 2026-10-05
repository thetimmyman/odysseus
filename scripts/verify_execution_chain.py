#!/usr/bin/env python3
"""Run one synthetic writable PS-635/PS-638 canary under an existing RTX lease.

Creates a fresh synthetic Git repository, preregisters its hidden oracle, calls
only the already-resident requested model, then retains the complete canonical
package. Does not deploy, change inference settings, or land repository work.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.attempt_receipt import artifact_ref, context_projection_digest, make_attempt_receipt, make_verification_receipt
from src.dispatch_boundary import BoundDispatch, InvocationIdentity, TargetEstate
from src.dispatch_routing import (
    EXACTNESS_EXACT, LOCALITY_LOCAL, ROLE_IMPLEMENTER, RoutingRequest,
    make_legacy_capability_view, make_target_profile, policy_snapshot, select_target,
)
from src.evidence_io import project_evidence_summary, retain_evidence_bundle
from src.execution_package import Budgets, VerificationPlan, build_execution_package, seal_verifier_digests
from src.local_worker_loop import VerificationExecution, WorkerExecution, _runtime_pin_identity, run_local_worker_loop
from src.source_snapshot import take_source_snapshot


ORACLE = '''import ast, json, pathlib
def safe_function(source):
    tree = ast.parse(source)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    node = tree.body[0]
    assert node.name == "transform" and len(node.args.args) == 1 and node.args.args[0].arg == "value"
    assert not node.decorator_list and not node.args.defaults and not node.args.kw_defaults
    assert not node.args.vararg and not node.args.kwarg and not node.args.kwonlyargs
    assert len(node.body) == 1 and isinstance(node.body[0], ast.Return)
    call = node.body[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert isinstance(call.func.value, ast.Name) and call.func.value.id == "value"
    assert call.func.attr in {"upper", "lower"} and not call.args and not call.keywords
    # Compile only our reconstruction. Worker annotations and all other text stay inert.
    scope = {}
    exec("def transform(value): return value." + call.func.attr + "()", scope)
    return scope["transform"]
def discriminates(source):
    fn = safe_function(source)
    return all(fn(a) == b for a, b in [("Hello", "HELLO"), ("a", "A"), ("", "")])
try:
    text = pathlib.Path("src/thing.py").read_text()
    candidate = discriminates(text)
    positive = discriminates("def transform(value): return value.upper()")
    negative = not discriminates("def transform(value): return value.lower()")
    print(json.dumps({"candidate": candidate, "positive_control": positive, "negative_control": negative}))
    raise SystemExit(0 if candidate and positive and negative else 1)
except (AssertionError, SyntaxError, ValueError):
    print(json.dumps({"candidate": False, "positive_control": False, "negative_control": False}))
    raise SystemExit(1)
'''


def _request(endpoint, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(endpoint.rstrip("/") + path, data,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return response.read()


def run_canary(*, worktree, output_directory, endpoint, model, generate=None):
    """``generate`` is an offline test adapter; the live path requires a broker lease."""
    if generate is None and (not os.getenv("HALOGEN_LEASE") or os.getenv("HALOGEN_POOL") != "rtx4500"):
        raise ValueError("a live RTX canary requires an active rtx4500 broker lease")
    root = Path(worktree)
    root.mkdir(parents=True, exist_ok=False)
    (root / "src").mkdir()
    (root / "src/thing.py").write_text("def transform(value): return value\n")
    (root / "oracle.py").write_text(ORACLE)
    for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Synthetic Fixture"), ("add", "src/thing.py", "oracle.py"),
                 ("commit", "-q", "-m", "synthetic baseline")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    def runtime_observation():
        if generate is not None:
            return "offline-fixture", hashlib.sha256(model.encode()).hexdigest(), 0
        running = json.loads(_request(endpoint, "/api/ps"))["models"]
        resident = next((m for m in running if m.get("name") == model), None)
        if resident is None:
            raise ValueError("requested model is not resident; canary refuses a model reload")
        return (json.loads(_request(endpoint, "/api/version"))["version"],
                resident["digest"], int(resident.get("context_length", 0)))
    runtime_version, model_digest, served_context = runtime_observation()
    command = sys.executable + " oracle.py"
    plan = VerificationPlan(
        verifier_id="oracle.py", command=command, verifier_paths=("oracle.py",),
        verifier_digests=seal_verifier_digests(str(root), ("oracle.py",)),
        positive_control="reference uppercase passes", negative_control="lowercase mutant fails")
    snapshot = lambda: take_source_snapshot(str(root), base_sha=base, relevant_paths=("src/thing.py", "oracle.py")).to_dict()
    packet = {"packet_id": "synthetic-uppercase", "objective": "Implement transform: uppercase every input string",
              "contract": "def transform(value: str) -> str; return value.upper(). Output only Python source, no explanation.",
              "write_scope": ["src/thing.py"], "interface": [{"name": "value", "required": True}],
              "test_command": command, "acceptance_criteria": ["uppercase mixed-case and empty strings"],
              "negative_control": plan.negative_control}
    package = build_execution_package(packet, source=take_source_snapshot(str(root), base_sha=base,
        relevant_paths=("src/thing.py", "oracle.py")), verification=plan,
        run_id="canary-" + base[:12], allowed_tools=("write_file",), network_policy="offline",
        budgets=Budgets(max_attempts=1, context_tokens=4096, output_tokens=512, time_seconds=180))
    now = datetime.now(timezone.utc)
    profile = make_target_profile(target_id="canary-rtx", profile_id="canary-profile", provider="ollama",
        host=endpoint, runtime_kind="ollama", runtime_version=runtime_version, backend="ollama",
        model=model, model_digest=model_digest, endpoint_url=endpoint, locality=LOCALITY_LOCAL,
        roles=frozenset({ROLE_IMPLEMENTER}), tools=frozenset({"write_file"}), network_policy="offline", budget_class="dev",
        configured_served_context=served_context,
        runtime_options={"temperature": 0, "num_predict": 512, "think": False})
    capability = make_legacy_capability_view(receipt_id="canary-declared-capability", profile_id=profile.profile_id,
        target_id=profile.target_id, capabilities=frozenset({"text_generation", "single_tool_call", "exact_reference_semantics"}),
        exactness=EXACTNESS_EXACT, observed_at=now.isoformat(), ttl_s=300,
        host=profile.host, runtime_version=profile.runtime_version, model_digest=profile.model_digest,
        notes="Canary profile declaration; this run does not qualify routing authority")
    policy = policy_snapshot(policy={"routingPolicyVersion": "synthetic-canary-v1"})
    request = RoutingRequest(domain="general_swe", role=ROLE_IMPLEMENTER, run_id=package.run_id,
        packet_id=package.packet_id, execution_package_hash=package.package_hash, required_tools=("write_file",),
        network_policy="offline", budget_class="dev", write_scope=("src/thing.py",))
    decision = select_target(request, profiles=(profile,), receipts=(capability,), policy=policy, now=now)
    bound = BoundDispatch(request=request, estate=TargetEstate(profiles=(profile,), receipts=(capability,)), decision=decision, policy=policy)

    def invocation(dispatch):
        pin = dispatch.pin_for(profile.profile_id)
        names = InvocationIdentity.__dataclass_fields__
        return InvocationIdentity(chat_url=pin["endpoint_url"],
                                  **{key: value for key, value in pin.items() if key in names})

    def facts(dispatch, number):
        pin = dispatch.pin_for(profile.profile_id)
        observed_version, observed_model, observed_context = runtime_observation()
        runtime = _runtime_pin_identity(dispatch)
        runtime.update(runtime_version=observed_version, model_digest=observed_model,
                       configured_served_context=observed_context)
        return {"capability_receipts": {ref: True for ref in decision.capability_receipt_refs}, "capacity_fresh": True,
            "capacity_receipt_refs": (), "policy_ref": policy.policy_ref, "target_id": profile.target_id,
            "profile_id": profile.profile_id, "endpoint_identity": pin["endpoint_identity"],
            "runtime_identity": runtime, "granted_tools": decision.granted_tools,
            "granted_write_scope": decision.granted_write_scope, "granted_read_scope": decision.granted_read_scope,
            "network_policy": decision.network_policy, "execution_package_hash": package.package_hash,
            "source_digest": package.source.snapshot_digest, "interface_digest": package.interface_digest}

    def worker(dispatch, context, number, budget):
        start, tick = datetime.now(timezone.utc).isoformat(), time.monotonic()
        raw = (generate(context) if generate else _request(endpoint, "/api/generate", {
            "model": model, "prompt": context, "stream": False, "think": False,
            "options": {"temperature": 0, "num_predict": budget}}))
        body = json.loads(raw)
        code = body["response"].strip()
        if code.startswith("```"):
            code = code.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        (root / "src/thing.py").write_text(code + "\n")
        rendered = artifact_ref("rendered-context", context, storage_uri="memory://context")
        output = artifact_ref("raw-model-output", raw, media_type="application/json", storage_uri="memory://raw")
        patch = artifact_ref("produced-source", code + "\n", storage_uri="memory://produced-source")
        receipt = make_attempt_receipt(receipt_id="canary-attempt-1", run_id=package.run_id, packet_id=package.packet_id,
            attempt=1, execution_package_hash=package.package_hash, dispatch_receipt_hash=decision.receipt_hash,
            target_id=profile.target_id, host=profile.host, model=model, context_projection_hash=context_projection_digest(context),
            runtime_kind=profile.runtime_kind, runtime_version=runtime_version, model_version=model_digest,
            generation_token="lease-sha256:" + hashlib.sha256(os.getenv("HALOGEN_LEASE", "offline-fixture").encode()).hexdigest(),
            rendered_context_ref=rendered, output_ref=output, artifact_refs=(patch,),
            started_at=start, ended_at=datetime.now(timezone.utc).isoformat(), elapsed_s=time.monotonic()-tick,
            finish_reason=body.get("done_reason", "unknown"), prompt_tokens=body.get("prompt_eval_count", 0),
            completion_tokens=body.get("eval_count", 0), requested_context=0, served_context=served_context,
            declared_write_set=("src/thing.py",), actual_write_set=("src/thing.py",))
        return WorkerExecution(receipt, {rendered.storage_uri: context.encode(), output.storage_uri: raw,
                                        patch.storage_uri: (code + "\n").encode()})

    def verifier(dispatch, attempt, number):
        source = snapshot()
        start, tick = datetime.now(timezone.utc).isoformat(), time.monotonic()
        ran = subprocess.run([sys.executable, "oracle.py"], cwd=root, capture_output=True, timeout=30)
        stdout = artifact_ref("verifier-stdout", ran.stdout, storage_uri="memory://stdout")
        stderr = artifact_ref("verifier-stderr", ran.stderr, storage_uri="memory://stderr")
        observed = json.loads(ran.stdout)
        fields = dict(receipt_id="canary-verification", run_id=package.run_id, packet_id=package.packet_id,
            attempt=number, execution_package_hash=package.package_hash, verifier_id=plan.verifier_id,
            normalized_command=command, source_snapshot_digest=source["snapshot_digest"], exit_code=ran.returncode,
            verifier_digest=seal_verifier_digests(str(root), ("oracle.py",))[0][1], verifier_paths=("oracle.py",),
            worktree=str(root), host=socket.gethostname(), started_at=start, ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_s=time.monotonic()-tick, stdout_ref=stdout, stderr_ref=stderr,
            stdout_bytes=len(ran.stdout), stderr_bytes=len(ran.stderr), timeout_s=30,
            proof_vantage="harness worktree", requirement_ids=("deterministic_verification", "source_binding", "scope_check"))
        receipt = make_verification_receipt(**fields)
        controls = tuple(make_verification_receipt(**{**fields, "receipt_id": control,
            "requirement_ids": ("negative_control",) if control == "negative_control" else (),
            "control_id": control, "control_expected": expected, "control_observed": expected if observed[control] else "WRONG",
            "control_passed": observed[control]}) for control, expected in (("positive_control", "PASS"), ("negative_control", "FAIL")))
        return VerificationExecution(receipt, {stdout.storage_uri: ran.stdout, stderr.storage_uri: ran.stderr}, controls)

    result = run_local_worker_loop(execution_package=package, standing_dispatch=bound, current_facts=facts,
        invocation=invocation, execute_worker=worker, execute_verifier=verifier, current_source=snapshot)
    if result.evidence_package is None:
        raise ValueError(result.refusal_reason)
    bundle = retain_evidence_bundle(output_directory, result.evidence_package, artifacts=result.artifacts)
    summary = project_evidence_summary(result.evidence_package, artifacts=result.artifacts)
    Path(output_directory, "summary.md").write_text(summary)
    return {"status": result.status, "validation": result.validation.to_dict(), "bundle": str(bundle),
            "evidence_package_hash": result.evidence_package["evidence_package_hash"],
            "tokens": sum(a.prompt_tokens + a.completion_tokens for a in result.attempts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    result = run_canary(**vars(args))
    print(json.dumps(result, indent=2))
    return 0 if result["validation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
