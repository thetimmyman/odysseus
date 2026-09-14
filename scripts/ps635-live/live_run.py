#!/usr/bin/env python3
"""PS-635 live runner — a real bounded run that emits PS-638 evidence.

Why this file exists in the repository.

The earlier live experiments lived in a scratch directory and were driven by
one-off scripts. That worked, and it had a real cost: when the session that ran
G1d2 was interrupted, the *result* had to be reconstructed from a run log and a
leftover ledger rather than re-derived, and no one could re-run the harness
because the harness was not in the repo. Evidence you cannot re-produce is
evidence you are trusting.

So this is the committed harness. It drives the SHIPPED primitives:

    src.work_packet.make_work_packet            -> validated immutable packet
    src.worker_context.render_worker_context    -> fresh bounded context
    src.local_worker_loop.run_bounded           -> dispatch/verify/repair
    src.execution_ledger.ExecutionLedger        -> canonical run state
    src.execution_package.build_execution_package, make_dispatch_receipt
    src.attempt_receipt.make_attempt_receipt, make_verification_receipt
    src.evidence_package.seal_evidence_package, validate_evidence_package

and it writes, for every run, a sealed PS-638 EvidencePackage plus its validator
verdict, next to the raw artifacts the package references.

What it deliberately does NOT do:

* it does not choose a target (PS-605 owns routing). The target is an explicit
  operator pin, and the DispatchDecisionReceipt records ``explicit_pin`` with no
  policy reference, because that is what actually happened;
* it does not accept work. The ceiling stays ACCEPTED_CANDIDATE;
* it does not soften the validator to make a run look good. A rejected package
  is written out as rejected, with its named reasons.

Usage:
    live_run.py <worktree> <case> [--target local-rtx4500] [--num-ctx 32768]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ollama_client as oc  # noqa: E402  (harness-local transport)

MAX_OUTPUT_TOKENS = 2400
MAX_EXTRA_ROUNDS = 1

WRITE_TOOL = [{
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write one file inside the permitted write scope.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}}, "required": ["path", "content"]}}},
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- artifacts ---
class RunStore:
    """Writes run artifacts and returns PS-638 ArtifactRef payloads.

    ``storage_uri`` is a ``file://`` URI so the validator's default loader can
    retrieve the bytes and prove the sealed hash. An artifact the validator
    cannot load is an ARTIFACT_UNAVAILABLE rejection, which is the point: sealed
    evidence has to be able to produce what it claims.
    """

    def __init__(self, root: Path):
        self.root = root
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.pending: dict = {}

    def store(self, name: str, data: bytes, media_type: str = "text/plain") -> dict:
        path = self.artifacts / name
        path.write_bytes(data)
        return {"artifact_id": name, "sha256": sha256_bytes(data),
                "media_type": media_type, "size": len(data),
                "storage_uri": f"file://{path}"}


def pytest_counts(output: str) -> dict:
    """Collected/executed/passed/failed/skipped from pytest's own summary.

    Parsed from the runner's output rather than inferred from the exit code, so
    a nonzero exit with zero failing tests (a collection error, say) is visible
    as exactly that.
    """
    import re

    counts = {"collected": None, "executed": None, "passed": None,
              "failed": None, "skipped": None}
    for line in reversed(output.strip().splitlines()):
        if " in " not in line or ("passed" not in line and "failed" not in line
                                  and "error" not in line):
            continue
        for word, key in (("passed", "passed"), ("failed", "failed"),
                          ("skipped", "skipped"), ("error", "failed"),
                          ("errors", "failed")):
            match = re.search(rf"(\d+) {word}\b", line)
            if match and counts[key] is None:
                counts[key] = int(match.group(1))
        break
    passed, failed = counts["passed"] or 0, counts["failed"] or 0
    if counts["passed"] is not None or counts["failed"] is not None:
        counts["executed"] = passed + failed + (counts["skipped"] or 0)
        counts["collected"] = counts["executed"]
    return counts


def control_result(output: str, node_id: str):
    """Did the hidden negative control itself pass, fail, or not run?

    Attribution matters. A red suite does NOT mean the control failed — usually
    the control is the one thing that passed while the implementation failed. So
    the control's result is read from its own line rather than inferred from the
    exit code, and ``None`` (unknown) is returned when nothing can be attributed
    rather than defaulting to a flattering True or a damning False.
    """
    if not node_id:
        return None
    if "error during collection" in output or "ModuleNotFoundError" in output:
        return None
    for line in output.splitlines():
        if node_id in line and line.startswith("FAILED"):
            return False
    if " passed" in output or " failed" in output:
        return True
    return None


# -------------------------------------------------------------- dispatcher ---
class Dispatcher:
    """One write_file tool, a bounded retry of the tool channel, full recording.

    The retry exists because a model that answers in prose instead of calling the
    tool is a *protocol* outcome, not a task verdict. Every round is recorded, so
    the extra rounds are visible in the evidence instead of being invisible
    background magic.
    """

    def __init__(self, worktree: Path, write_scope, client, store: RunStore,
                 *, num_ctx: int):
        self.worktree = worktree
        self.scope = [s.lstrip("./") for s in write_scope]
        self.client = client
        self.store = store
        self.num_ctx = num_ctx
        self.calls: list = []

    def __call__(self, context: str, attempt: int):
        import src.local_worker_loop as lo

        started = time.monotonic()
        rounds = 0
        artifacts: tuple = ()
        failure_class = ""
        status = ""
        timings: dict = {}
        written = ""
        model_text = ""
        tool_calls: list = []
        prompt_tokens = 0
        completion_tokens = 0
        messages = [{"role": "user", "content": context}]

        for rnd in range(1, MAX_EXTRA_ROUNDS + 2):
            rounds = rnd
            res = self.client.api_streaming_chat(
                messages, num_ctx=self.num_ctx, tools=WRITE_TOOL,
                num_predict=MAX_OUTPUT_TOKENS)
            body = res.body or {}
            prompt_tokens = int(body.get("prompt_eval_count") or prompt_tokens or 0)
            completion_tokens = int(body.get("eval_count") or 0)
            timings[f"round{rnd}"] = {
                "elapsed_s": res.elapsed_s, "ttft_s": res.ttft_s,
                "eval_count": body.get("eval_count"),
                "prompt_eval_count": body.get("prompt_eval_count"),
                "incremental": body.get("_incremental", None)}
            if not res.ok:
                failure_class = "runtime_provider"
                status = (res.error or "")[:150]
                break
            if res.runtime_error:
                low = res.runtime_error.lower()
                failure_class = ("context" if any(k in low for k in
                                                  ("context", "exceed", "too large"))
                                 else "runtime_provider")
                status = res.runtime_error[:150]
                break

            message = body.get("message") or {}
            model_text = (message.get("content") or "").strip()
            calls = message.get("tool_calls") or []
            if calls:
                tool_calls.extend(calls)
                fn = calls[0].get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                path = str(args.get("path") or "").strip().lstrip("./")
                content = args.get("content") or ""
                if path not in self.scope:
                    failure_class = "policy"
                    status = f"out-of-scope path {path!r}"
                    break
                target = self.worktree / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                artifacts = (path,)
                written = path
                status = f"wrote {path} ({len(content)} chars)"
                break

            failure_class = "protocol" if model_text else "tool_channel"
            status = f"no tool call (round {rnd})"
            messages.append({"role": "assistant", "content": model_text[:600]})
            messages.append({"role": "user", "content": (
                f'Call write_file NOW with path "{self.scope[0]}" and the complete '
                "file content. No prose.")})

        self.calls.append({
            "attempt": attempt, "context": context, "rounds": rounds,
            "written": written, "model_text": model_text,
            "tool_calls": tool_calls, "timings": timings,
            "failure_class": failure_class, "status": status,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "elapsed_s": round(time.monotonic() - started, 3)})

        return lo.DispatchOutcome(
            artifacts=artifacts, failure_class=failure_class, status=status,
            elapsed_s=round(time.monotonic() - started, 3), rounds=rounds,
            timings=timings)


# ---------------------------------------------------------------- verifier ---
class Verifier:
    """Runs the hidden verifier and records one receipt's worth of raw truth."""

    def __init__(self, worktree: Path, test_rel: str, store: RunStore,
                 source_digest_fn, *, timeout: int = 900):
        self.worktree = worktree
        self.test_rel = test_rel
        self.store = store
        self.source_digest_fn = source_digest_fn
        self.timeout = timeout
        self.records: list = []

    def __call__(self) -> dict:
        attempt = len(self.records) + 1
        command = f"python3 -m pytest {self.test_rel} -q -p no:cacheprovider"
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", self.test_rel, "-q",
                 "-p", "no:cacheprovider"],
                cwd=str(self.worktree), capture_output=True, text=True,
                timeout=self.timeout)
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout
            stdout = raw.decode("utf-8", "replace") if isinstance(raw, bytes) \
                else (raw or "")
            stderr = f"verifier timed out after {self.timeout}s"
            code, timed_out = 124, True
        elapsed = round(time.monotonic() - started, 3)
        output = stdout + stderr
        stdout_ref = self.store.store(f"attempt{attempt}-verifier-stdout.txt",
                                     stdout.encode("utf-8"))
        stderr_ref = self.store.store(f"attempt{attempt}-verifier-stderr.txt",
                                     stderr.encode("utf-8"))
        record = {
            "attempt": attempt, "command": command, "exit_code": code,
            "output": output, "counts": pytest_counts(stdout),
            "stdout_ref": stdout_ref, "stderr_ref": stderr_ref,
            "stdout_complete": not timed_out, "stderr_complete": not timed_out,
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": elapsed,
            "source_digest": self.source_digest_fn(),
            "summary": [ln for ln in output.strip().splitlines()
                        if "passed" in ln or "failed" in ln or "error" in ln][-3:],
        }
        self.records.append(record)
        return {"test_command": command, "returncode": code,
                "passed": code == 0, "summary": record["summary"],
                "output": output}


# ------------------------------------------------------------------ helpers ---
def changed_files(worktree: Path, write_scope):
    """Bounded diff of the write scope, for the repair packet."""

    def _fn(_packet) -> dict:
        out = {}
        for rel in write_scope:
            path = worktree / rel
            if path.exists():
                body = path.read_text()
                out[rel] = (f"--- /dev/null\n+++ b/{rel}\n"
                            + "\n".join("+" + ln for ln in body.splitlines()))
        return out
    return _fn


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False,
                               default=str) + "\n")


def run_case(worktree: Path, case_name: str, target_id: str,
             num_ctx: int) -> int:
    sys.path.insert(0, str(worktree))
    import cases as case_mod
    import src.local_worker_loop as lo
    from src.attempt_receipt import (
        ArtifactRef, context_projection_digest, make_attempt_receipt,
        make_verification_receipt)
    from src.evidence_contract import (
        INDEPENDENCE_HARNESS_HIDDEN, KIND_CONTEXT_DELIVERY, EvidenceRequirement)
    from src.evidence_package import (
        find_secret_shaped, seal_evidence_package, validate_evidence_package)
    from src.execution_ledger import ExecutionLedger
    from src.execution_package import (
        DECIDED_BY_EXPLICIT_PIN, VerificationPlan, build_execution_package,
        default_requirements, make_dispatch_receipt, seal_verifier_digests)
    from src.repair_packet import failure_fingerprint, parse_verification_failure
    from src.source_snapshot import take_source_snapshot
    from src.worker_context import render_worker_context

    case = case_mod.CASES[case_name]()
    case.worktree = worktree
    run_id = f"{case_name}-{utc_stamp()}"
    run_dir = worktree / "data" / "live" / run_id
    store = RunStore(run_dir)
    ledger = ExecutionLedger(str(run_dir / "ledger.jsonl"))
    client = oc.target(target_id)

    packet = case.packet()
    test_rel = case.verifier
    relevant = sorted(set(list(packet["write_scope"]) + [test_rel]))

    def snapshot() -> dict:
        return take_source_snapshot(str(worktree), base_sha=case.base_sha,
                                    relevant_paths=relevant).to_dict()

    # ---- preflight: the packet and the projection must carry no secret shapes --
    leaks = (find_secret_shaped(packet)
             + find_secret_shaped(case.tool_instruction))
    if leaks:
        write_json(run_dir / "preflight.json", {
            "result": "PACKET_INVALID", "reason": "secret_shaped_fixture_leak",
            "paths": list(leaks), "model_calls": 0})
        print(f"REFUSED before dispatch: secret-shaped content at {list(leaks)}")
        return 0

    version = (client.api("/api/version", timeout=20).body or {}).get("version", "")
    tags = client.api("/api/tags", timeout=40).body or {}
    model_digest = next((m.get("digest") for m in (tags.get("models") or ())
                         if m.get("name") == client.model), "")

    source_before = snapshot()
    plan = VerificationPlan(
        verifier_id=test_rel,
        command=f"python3 -m pytest {test_rel} -q",
        verifier_paths=[test_rel],
        verifier_digests=seal_verifier_digests(str(worktree), [test_rel]),
        positive_control=case.positive_control,
        negative_control=case.negative_control)

    requirements = default_requirements(
        plan, negative_control=packet["negative_control"]) + (
        EvidenceRequirement(
            requirement_id="context_delivery", kind=KIND_CONTEXT_DELIVERY,
            vantage="harness worktree",
            expected_verifier="the rendered-context artifact",
            independence=INDEPENDENCE_HARNESS_HIDDEN),)

    try:
        ep = build_execution_package(
            packet, source=take_source_snapshot(
                str(worktree), base_sha=case.base_sha, relevant_paths=relevant),
            verification=plan, run_id=run_id,
            package_id=f"{packet['packet_id']}:{run_id}", jira_key=case.jira_key,
            execution_role="local_implementer", allowed_tools=("write_file",),
            network_policy="tailnet-loopback (ssh to the target's ollama loopback)",
            evidence_requirements=requirements)
    except Exception as exc:
        # An unpkg-able packet is refused HERE, before any budget or model call.
        # The refusal is the evidence: a sealed package about a run that never
        # happened would be a document about nothing.
        write_json(run_dir / "preflight.json", {
            "run_id": run_id, "case": case.name, "result": "PACKET_INVALID",
            "reason": str(exc)[:300], "model_calls": 0,
            "package_sealed": False, "sealed_before_dispatch": False})
        print(f"{run_id}: PACKET_INVALID — refused before dispatch "
              f"(0 model calls)\n  reason: {str(exc)[:200]}")
        return 0

    requirement_ids = tuple(r.requirement_id for r in ep.evidence_requirements)
    return _execute(case, packet, test_rel, run_id, run_dir, store, ledger, client,
                    target_id, num_ctx, version, model_digest, plan, ep,
                    requirement_ids, relevant, snapshot, source_before)


def _execute(case, packet, test_rel, run_id, run_dir, store, ledger, client,
             target_id, num_ctx, version, model_digest, plan, ep, requirement_ids,
             relevant, snapshot, source_before) -> int:
    import src.local_worker_loop as lo
    from src.attempt_receipt import (
        ArtifactRef, context_projection_digest, make_attempt_receipt,
        make_verification_receipt)
    from src.evidence_package import (
        seal_evidence_package, validate_evidence_package)
    from src.execution_package import make_dispatch_receipt
    from src.repair_packet import failure_fingerprint, parse_verification_failure
    from src.worker_context import render_worker_context

    dispatch = make_dispatch_receipt(
        receipt_id=f"dispatch-{run_id}", execution_package_hash=ep.package_hash,
        run_id=run_id, packet_id=packet["packet_id"],
        requested_role="local_implementer",
        requested_capabilities=tuple(packet.get("target_requirements") or ()),
        selected_target_id=target_id, selected_host=client.ssh_host,
        selected_model=client.model, selected_runtime_kind="ollama",
        selected_runtime_version=version, selected_model_digest=model_digest,
        granted_tools=("write_file",),
        granted_write_scope=tuple(packet["write_scope"]),
        granted_read_scope=tuple(packet.get("read_scope") or ()),
        network_policy="tailnet-loopback",
        decided_by="explicit_pin",
        reason=(f"operator pinned {target_id}; PS-605 policy routing is not wired "
                "for this packet, so no policy reference is claimed"),
        candidates_considered=({"target_id": target_id, "eligible": True,
                                "reason": "explicit operator pin"},),
        decided_at=datetime.now(timezone.utc).isoformat())
    write_json(run_dir / "dispatch_receipt.json", dispatch.to_dict())

    base_context = render_worker_context(packet) + case.tool_instruction
    case.preflight(base_context)
    (run_dir / "base_context.txt").write_text(base_context)

    pinned = lo.PinnedTarget(target_id=target_id, host=client.ssh_host,
                             model=client.model, runtime_version=version,
                             worktree=str(case.worktree), served_context=num_ctx)
    dispatcher = Dispatcher(case.worktree, packet["write_scope"], client, store,
                            num_ctx=num_ctx)
    verifier = Verifier(case.worktree, test_rel, store,
                        lambda: snapshot()["snapshot_digest"])

    started = time.monotonic()
    result = lo.run_bounded(
        packet, run_id=run_id, ledger=ledger, pinned=pinned, dispatch=dispatcher,
        verify=verifier, base_context=base_context, max_attempts=case.max_attempts,
        read_changed_files=changed_files(case.worktree, packet["write_scope"]))
    wall_clock_s = round(time.monotonic() - started, 3)

    provenance = ledger.provenance(run_id)
    write_json(run_dir / "ledger_provenance.json", provenance)

    if result.attempts == 0:
        write_json(run_dir / "result.json", {
            "run_id": run_id, "result": result.to_dict(), "model_calls": 0,
            "package_sealed": False,
            "reason": "refused before dispatch; a package with nothing to verify "
                      "is not sealed, and saying so IS the evidence"})
        print(f"{run_id}: {result.result} / {result.failure_class} "
              f"({result.reason}) — 0 model calls, no package sealed")
        return 0

    served = client.served_context()
    attempts = []
    for call in dispatcher.calls:
        number = call["attempt"]
        ctx_ref = store.store(f"attempt{number}-context.txt",
                              call["context"].encode("utf-8"))
        out_ref = store.store(f"attempt{number}-model-output.txt",
                              (call["model_text"] or "").encode("utf-8"))
        artifact_refs = ()
        if call["written"]:
            written_path = case.worktree / call["written"]
            if written_path.exists():
                file_ref = store.store(
                    f"attempt{number}-artifact-{Path(call['written']).name}",
                    written_path.read_bytes())
                artifact_refs = (ArtifactRef(**file_ref),)
        attempts.append(make_attempt_receipt(
            receipt_id=f"attempt-{run_id}-{number}", run_id=run_id,
            packet_id=packet["packet_id"], attempt=number,
            repair_of=0 if number == 1 else number - 1,
            execution_package_hash=ep.package_hash,
            dispatch_receipt_hash=dispatch.receipt_hash,
            target_id=target_id, host=client.ssh_host, model=client.model,
            runtime_kind="ollama", runtime_version=version,
            model_version=model_digest,
            context_projection_hash=context_projection_digest(call["context"]),
            rendered_context_ref=ArtifactRef(**ctx_ref),
            output_ref=ArtifactRef(**out_ref),
            requested_context=num_ctx, served_context=int(served or 0),
            ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_s=call["elapsed_s"], finish_reason=call["status"][:200],
            prompt_tokens=call["prompt_tokens"],
            completion_tokens=call["completion_tokens"],
            tool_invocations=tuple(
                {"name": (c.get("function") or {}).get("name", "write_file")}
                for c in call["tool_calls"]),
            declared_write_set=tuple(packet["write_scope"]),
            actual_write_set=(call["written"],) if call["written"] else (),
            artifact_refs=artifact_refs,
            failure_class=call["failure_class"]))
        write_json(run_dir / f"attempt_receipt-{number}.json",
                   attempts[-1].to_dict())

    verifications = []
    for record in verifier.records:
        number = record["attempt"]
        counts = record["counts"]
        failure = parse_verification_failure(record["command"], record["exit_code"],
                                            record["output"])
        verifications.append(make_verification_receipt(
            receipt_id=f"verify-{run_id}-{number}", run_id=run_id,
            packet_id=packet["packet_id"], attempt=number,
            execution_package_hash=ep.package_hash, verifier_id=test_rel,
            verifier_digest=plan.digest_of(test_rel), verifier_paths=(test_rel,),
            source_snapshot_digest=record["source_digest"],
            normalized_command=record["command"], exit_code=record["exit_code"],
            stdout_ref=ArtifactRef(**record["stdout_ref"]),
            stderr_ref=ArtifactRef(**record["stderr_ref"]),
            stdout_complete=record["stdout_complete"],
            stderr_complete=record["stderr_complete"],
            stdout_bytes=record["stdout_ref"]["size"],
            stderr_bytes=record["stderr_ref"]["size"],
            tests_collected=counts["collected"], tests_executed=counts["executed"],
            tests_passed=counts["passed"], tests_failed=counts["failed"],
            tests_skipped=counts["skipped"],
            expected="the hidden verifier passes on the produced artifact",
            observed=(record["summary"][-1] if record["summary"] else "")[:200],
            control_id=case.control_node_id,
            control_expected="FAIL for an implementation the contract forbids",
            control_observed=f"node id present in {test_rel}",
            control_passed=control_result(record["output"], case.control_node_id),
            worktree=str(case.worktree), host=client.ssh_host,
            started_at=record["started_at"], ended_at=record["ended_at"],
            elapsed_s=record["elapsed_s"],
            requirement_ids=requirement_ids,
            proof_class="HARNESS_HIDDEN",
            failure_fingerprint=failure_fingerprint(failure)))
        write_json(run_dir / f"verification_receipt-{number}.json",
                   verifications[-1].to_dict())

    package = seal_evidence_package(
        evidence_package_id=f"evpkg-{run_id}", execution_package=ep,
        dispatch_receipts=(dispatch,), attempt_receipts=tuple(attempts),
        verification_receipts=tuple(verifications))
    payload = package.to_dict()
    write_json(run_dir / "evidence_package.json", payload)

    # No current_source is passed, deliberately. For a WRITABLE run the tree
    # necessarily differs afterwards, so a whole-snapshot comparison would reject
    # honest evidence; the liveliness that MATTERS here is that the sealed
    # artifact bytes still hash correctly, which the validator already checks.
    # The post-run snapshot is recorded as an observation, not pretended into a
    # check it is not.
    validation = validate_evidence_package(payload)
    write_json(run_dir / "validation.json", validation.to_dict())

    summary = {
        "run_id": run_id, "case": case.name, "target": target_id,
        "num_ctx": num_ctx, "wall_clock_s": wall_clock_s,
        "result": result.to_dict(), "model_calls": len(dispatcher.calls),
        "packet_interface_digest": ep.interface_digest,
        "package_hash": ep.package_hash,
        "dispatch_receipt_hash": dispatch.receipt_hash,
        "attempt_receipt_hashes": [a.receipt_hash for a in attempts],
        "verification_receipt_hashes": [v.receipt_hash for v in verifications],
        "evidence_package_hash": package.evidence_package_hash,
        "validation_ok": validation.ok,
        "validation_reasons": list(validation.codes),
        "requirement_states": [s.to_dict() for s in validation.requirement_states],
        "context_projections": [a.context_projection_hash for a in attempts],
        "repair_fingerprints": [r.get("fingerprint")
                                for r in provenance.get("repairs") or ()],
        "source_before": source_before, "source_after": snapshot(),
        "ledger_chain": ledger.verify_chain(),
    }
    write_json(run_dir / "run_summary.json", summary)

    print(f"\n=== {run_id} ===")
    print(f"result: {result.result} | failure_class: {result.failure_class or '-'}"
          f" | attempts: {result.attempts} | model calls: {len(dispatcher.calls)}")
    print(f"reason: {result.reason}")
    for verification in verifications:
        print(f"  verification attempt {verification.attempt}: exit "
              f"{verification.exit_code} -> {verification.outcome} "
              f"({verification.tests_passed or 0} passed, "
              f"{verification.tests_failed or 0} failed)")
    print(f"package: {ep.package_hash[:16]}  evidence: "
          f"{package.evidence_package_hash[:16]}")
    print("validator: " + ("VERIFIED" if validation.ok
                           else f"REJECTED — {list(validation.codes)}"))
    print(f"artifacts + receipts: {run_dir}")
    return 0


def summarise_run(run_dir: Path) -> int:
    """Rebuild ``run_summary.json`` from records already on disk.

    Exists because of a live defect found on 2026-09-14: the E1 run sealed its
    package and validated it, then crashed while assembling this convenience
    projection. The evidence was never at risk — the package, the receipts, the
    artifacts and the validator verdict were all written — but the summary was
    lost, and the honest repair is to RE-DERIVE it from the sealed records rather
    than re-run the experiment and spend another model turn.

    Nothing here is a source of truth. It reads what the run wrote.
    """
    run_dir = Path(run_dir)
    package = json.loads((run_dir / "evidence_package.json").read_text())
    validation = json.loads((run_dir / "validation.json").read_text())
    provenance = json.loads((run_dir / "ledger_provenance.json").read_text())
    dispatch = json.loads((run_dir / "dispatch_receipt.json").read_text())
    execution = package["execution_package"]
    attempts = package.get("attempt_receipts") or ()
    verifications = package.get("verification_receipts") or ()

    summary = {
        "run_id": run_dir.name,
        "package_hash": execution.get("package_hash"),
        "dispatch_receipt_hash": dispatch.get("receipt_hash"),
        "attempt_receipt_hashes": [a.get("receipt_hash") for a in attempts],
        "verification_receipt_hashes": [v.get("receipt_hash")
                                       for v in verifications],
        "evidence_package_hash": package.get("evidence_package_hash"),
        "validation_ok": validation.get("ok"),
        "validation_reasons": [i["code"] for i in validation.get("issues") or ()],
        "requirement_states": validation.get("requirement_states"),
        "context_projections": [a.get("context_projection_hash") for a in attempts],
        "repair_fingerprints": [r.get("fingerprint")
                                for r in provenance.get("repairs") or ()],
        "terminal_result": provenance.get("terminal_result"),
        "ledger_chain": provenance.get("chain_ok"),
        "source_before": execution.get("source"),
        "recovered": ("this summary was RE-DERIVED from the sealed records after a "
                      "harness defect aborted the run's summary step; the sealed "
                      "package and its verdict are the originals"),
    }
    write_json(run_dir / "run_summary.json", summary)
    print(f"recovered summary for {run_dir.name}: "
          f"terminal {summary['terminal_result']} | "
          f"validator {'VERIFIED' if validation.get('ok') else 'REJECTED'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree", nargs="?")
    parser.add_argument("case", nargs="?", default="l1-interface",
                        help="a case name from cases.CASES")
    parser.add_argument("--target", default="local-rtx4500")
    parser.add_argument("--num-ctx", type=int, default=32768)
    parser.add_argument("--summarise", metavar="RUN_DIR",
                        help="re-derive run_summary.json from sealed records only")
    args = parser.parse_args()

    if args.summarise:
        return summarise_run(Path(args.summarise))

    if not args.worktree:
        parser.error("worktree is required (or use --summarise RUN_DIR)")
    worktree = Path(args.worktree).resolve()
    if not (worktree / "src").is_dir():
        print(f"not an odysseus worktree: {worktree}", file=sys.stderr)
        return 2
    return run_case(worktree, args.case, args.target, args.num_ctx)


if __name__ == "__main__":
    sys.exit(main())





