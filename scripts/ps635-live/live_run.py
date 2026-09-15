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

* it does not choose a target: PS-605 owns routing. ``--target`` is a PREFERENCE
  submitted through the routing boundary, the measured fleet is read from PS-632's
  registry (``src.local_targets``), and the DispatchDecisionReceipt records
  ``ps605_policy`` with its policy reference. A preferred target that is not
  independently eligible produces a REFUSAL, never a quiet re-point;
* it does not accept work. The ceiling stays ACCEPTED_CANDIDATE;
* it does not soften the validator to make a run look good. A rejected package
  is written out as rejected, with its named reasons.

Usage:
    live_run.py <worktree> <case> [--target local-rtx4500] [--num-ctx 32768]

``--target`` is the target the operator PREFERS, not the target that runs: PS-605
selects from the measured fleet and the receipt records what it did.
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
from typing import Mapping, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

#: The repository this HARNESS lives in. The PS-638 evidence layer is the tool;
#: a tree under measurement is only ever the subject. Keeping them distinct is
#: what lets the harness seal a comparator for a BASE commit that predates the
#: evidence layer entirely — which is exactly what PS-639 needs, since 56ff059f
#: has no src.attempt_receipt at all.
HARNESS_ROOT = HERE.parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

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
    """A write_file tool over the packet's write scope, fully recorded.

    MULTI-FILE (added for PS-639): a packet may own more than one file, so the
    loop keeps calling the tool until every file in the scope has been written or
    the round budget runs out. Only the first tool call of each round is acted on,
    and a round that returns prose instead of a call is a PROTOCOL outcome with
    its own bounded retry — the model is asked for the files still outstanding, by
    path, rather than being told "call the tool" and left to guess which one.

    Every round and every write is recorded, so extra rounds are visible in the
    evidence instead of being invisible background magic.
    """

    def __init__(self, worktree: Path, write_scope, client, store: RunStore,
                 *, num_ctx: int, max_output_tokens: int = MAX_OUTPUT_TOKENS):
        self.worktree = worktree
        self.scope = [s.lstrip("./") for s in write_scope]
        self.client = client
        self.store = store
        self.num_ctx = num_ctx
        self.max_output_tokens = max_output_tokens
        self.max_rounds = max(MAX_EXTRA_ROUNDS + 2, len(self.scope) + 2)
        self.calls: list = []

    def __call__(self, context: str, attempt: int):
        import src.local_worker_loop as lo

        started = time.monotonic()
        rounds = 0
        failure_class = ""
        status = ""
        timings: dict = {}
        pending = list(self.scope)
        writes: list = []
        model_text = ""
        tool_calls: list = []
        prompt_tokens = 0
        completion_tokens = 0
        messages = [{"role": "user", "content": context}]

        for rnd in range(1, self.max_rounds + 1):
            rounds = rnd
            res = self.client.api_streaming_chat(
                messages, num_ctx=self.num_ctx, tools=WRITE_TOOL,
                num_predict=self.max_output_tokens)
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
                writes.append(path)
                if path in pending:
                    pending.remove(path)
                status = f"wrote {path} ({len(content)} chars)"
                if not pending:
                    break
                messages.append({"role": "assistant", "content": model_text[:400]})
                messages.append({"role": "user", "content": (
                    f'Now call write_file for path "{pending[0]}" with its complete '
                    "file content. No prose.")})
                continue

            failure_class = "protocol" if model_text else "tool_channel"
            status = f"no tool call (round {rnd})"
            messages.append({"role": "assistant", "content": model_text[:600]})
            messages.append({"role": "user", "content": (
                f'Call write_file NOW with path "{pending[0]}" and the complete '
                "file content. No prose.")})

        if pending and not failure_class:
            failure_class = "technical"
            status = f"incomplete write set; never wrote {pending}"

        self.calls.append({
            "attempt": attempt, "context": context, "rounds": rounds,
            "writes": writes, "written": writes[-1] if writes else "",
            "pending": list(pending), "model_text": model_text,
            "tool_calls": tool_calls, "timings": timings,
            "failure_class": failure_class, "status": status,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "elapsed_s": round(time.monotonic() - started, 3)})

        return lo.DispatchOutcome(
            artifacts=tuple(writes), failure_class=failure_class, status=status,
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


# ----------------------------------------------------------------- advisor ---
class LiveAdvisor:
    """The G2 manager/replanner, live, on the target model, fully recorded.

    It is the SAME runtime target as the worker (an explicit pin). That is
    deliberate: G2's question is what a local model can contribute in an ADVISORY
    role, and the answer is only useful if it is measured on the model the estate
    actually has. Being the same model as the worker cannot buy this role any
    authority — ``src.replanner`` refuses scope, interface, source, verification,
    permission, routing, budget and lifecycle changes, and the loop only consults
    it at a boundary where it was going to decide deterministically anyway.

    Every turn is recorded (context, raw reply, timing, token counts) so the
    proposal's provenance is the run's evidence, not a reconstruction.
    """

    def __init__(self, client, store: RunStore, *, num_ctx: int,
                 max_output_tokens: int = 800, preflight=None):
        self.client = client
        self.store = store
        self.num_ctx = num_ctx
        self.max_output_tokens = max_output_tokens
        self.preflight = preflight
        self.records: list = []

    def __call__(self, plan_input):
        import src.replanner as rp

        if self.preflight is not None:
            self.preflight(plan_input)
        context = rp.render_planner_context(plan_input)
        started = time.monotonic()
        res = self.client.api_streaming_chat(
            [{"role": "user", "content": context}], num_ctx=self.num_ctx,
            tools=None, num_predict=self.max_output_tokens, think=False)
        elapsed = round(time.monotonic() - started, 3)
        body = res.body or {}
        record = {
            "index": len(self.records) + 1,
            "boundary": plan_input.boundary,
            "input_digest": plan_input.input_digest,
            "plan_input": plan_input.to_dict(),
            "context": context,
            "ok": bool(res.ok) and not res.runtime_error,
            "status": (res.error or res.runtime_error or "")[:200],
            "elapsed_s": elapsed,
            "prompt_tokens": int(body.get("prompt_eval_count") or 0),
            "completion_tokens": int(body.get("eval_count") or 0),
            "model_text": "",
            "proposal": None,
            "proposal_reason": "",
        }
        if not record["ok"]:
            self.records.append(record)
            return None, (f"the manager's turn failed: "
                          f"{res.error or res.runtime_error}")
        record["model_text"] = ((body.get("message") or {}).get("content") or "").strip()
        proposal, reason = rp.parse_proposal_text(
            record["model_text"], run_id=plan_input.run_id,
            packet_id=plan_input.packet_id)
        record["proposal_reason"] = reason
        record["proposal"] = proposal.to_dict() if proposal is not None else None
        self.records.append(record)
        return proposal, reason

    def summary(self) -> dict:
        """Counters for the run summary; derived from the records, never asserted."""
        return {
            "manager_calls": len(self.records),
            "manager_boundaries": [r["boundary"] for r in self.records],
            "manager_parsed": [r["proposal"] is not None for r in self.records],
            "manager_elapsed_s": [r["elapsed_s"] for r in self.records],
            "manager_prompt_tokens": [r["prompt_tokens"] for r in self.records],
            "manager_completion_tokens": [r["completion_tokens"] for r in self.records],
            "manager_kinds": [(r["proposal"] or {}).get("kind", "") for r in self.records],
        }


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


def manager_seals(*, advisor, ledger, run_id: str, packet: Mapping, store, run_dir,
                  execution_package_hash: str, dispatch_receipt_hash: str,
                  target_id: str, model: str) -> tuple:
    """Seal the manager seam into the package, one entry per consultation.

    Each entry binds the planner INPUT (content-addressed, with its digest), the
    exact manager CONTEXT, the model's raw OUTPUT, the parsed proposal, the
    deterministic VERDICT and the canonical LEDGER entry it produced — plus the
    execution identities (package, dispatch receipt, run, packet, target, model)
    that make the whole chain attributable. The artifact references are checked by
    the package validator like every other reference in the package, so a seal
    that cannot produce its bytes is rejected rather than believed.
    """
    if advisor is None:
        return ()
    entries = [e for e in ledger.entries_for(run_id)
               if e.kind in ("proposal", "proposal_refused")]
    seals = []
    for index, record in enumerate(advisor.records, start=1):
        ctx_ref = store.store(f"manager{index}-context.txt",
                              record["context"].encode("utf-8"))
        input_ref = store.store(
            f"manager{index}-planner-input.json",
            (json.dumps(record["plan_input"], indent=2, sort_keys=True,
                        default=str) + "\n").encode("utf-8"),
            media_type="application/json")
        output_ref = store.store(f"manager{index}-model-output.txt",
                                 (record["model_text"] or "").encode("utf-8"))
        proposal_ref = store.store(
            f"manager{index}-proposal.json",
            (json.dumps({"proposal": record["proposal"],
                         "parse_reason": record["proposal_reason"]},
                        indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"),
            media_type="application/json")
        entry = entries[index - 1] if index - 1 < len(entries) else None
        proposal = record["proposal"] or {}
        seals.append({
            "seal_id": f"manager_seam-{index}",
            "run_id": run_id, "packet_id": str(packet.get("packet_id", "")),
            "execution_package_hash": execution_package_hash,
            "dispatch_receipt_hash": dispatch_receipt_hash,
            "manager_target_id": target_id, "manager_model": model,
            "boundary": record["boundary"],
            "planner_input_digest": record["input_digest"],
            "planner_context_ref": ctx_ref,
            "input_ref": input_ref,
            "output_ref": output_ref,
            "proposal_ref": proposal_ref,
            "proposal_hash": str(proposal.get("proposal_hash", "")),
            "proposal_kind": str(proposal.get("kind", "")),
            "manager_turn_ok": bool(record["ok"]),
            "planner_context_ref_hash": ctx_ref["sha256"],
            "ledger_entry_hash": (entry.entry_hash if entry is not None else ""),
            "ledger_entry_kind": (entry.kind if entry is not None else ""),
            "ledger_verdict_code": (
                str(entry.payload.get("verdict_code", "")) if entry is not None else ""),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })
    write_json(run_dir / "manager_seals.json", {"seals": seals})
    return tuple(seals)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False,
                               default=str) + "\n")


def route_packet(worktree: Path, packet: Mapping, ep, run_id: str, run_dir: Path,
                 *, preferred_target_id: str, ledger, role: str = "") -> dict:
    """Ask PS-605 which target may run this packet. The harness does NOT choose.

    Returns ``{"refused": True, ...}`` when PS-605 refuses (nothing is dispatched and
    the refusal is sealed), or ``{"refused": False, "bound": ..., "dispatch": ...}``
    where ``dispatch`` is PS-638's own ``DispatchDecisionReceipt``, built from
    PS-605's decision — the only thing the caller may pin.

    ``preferred_target_id`` travels as a PREFERENCE through PS-605, never applied
    directly: if the preferred target is not independently eligible, PS-605 records
    why and the run is refused rather than quietly re-pointed.
    """
    import src.local_target_routing as ltr
    from src.dispatch_routing import RoutingRefused
    from src.execution_package import make_dispatch_receipt
    from src.local_targets import probe_fleet, registered_targets, target_by_id

    spec = target_by_id(preferred_target_id)
    if spec is None:
        raise SystemExit(f"unknown target {preferred_target_id!r}; registered: "
                         f"{[s.target_id for s in registered_targets()]}")
    records = probe_fleet([spec])
    write_json(run_dir / "fleet_probe.json", {
        "probed": [r.to_dict() for r in records],
        "scope": ("this slice probes the qualified target; the registry itself is "
                  "untouched, and multi-candidate refusal/fallback is proven in the "
                  "PS-605 live controls and the integration tests")})

    try:
        bound, inputs = ltr.resolve_fleet_dispatch(
            records, packet=packet,
            role=role or str(packet.get("role") or "local_implementer"),
            execution_package_hash=ep.package_hash, run_id=run_id,
            preferred_target_id=preferred_target_id, decision_id=f"dec-{run_id}")
    except (RoutingRefused, ltr.FleetRoutingError) as exc:
        refusal = exc.to_dict() if hasattr(exc, "to_dict") else {"reason": str(exc)}
        sealed = seal_dispatch_refusal(
            refusal, packet=packet, execution_package_hash=ep.package_hash,
            run_id=run_id, records=records, run_dir=run_dir)
        # The canonical record of "this run stopped before dispatch" is the ledger,
        # which already owns run state; the refusal file above carries the routing
        # reasons. The run is opened with an EMPTY execution identity on purpose: no
        # target was selected, and naming one would misattribute the refusal.
        ledger.record_run(run_id=run_id, packet_id=packet["packet_id"],
                          objective=str(packet.get("objective") or ""),
                          role=str(packet.get("role") or "local_implementer"),
                          target_id="", host="", model="", runtime_version="",
                          worktree=str(worktree),
                          write_scope=packet.get("write_scope") or (),
                          base_sha=str(packet.get("base_sha") or ""),
                          interface_digest=getattr(ep, "interface_digest", ""))
        # No AttemptReceipt exists because no attempt happened.
        ledger.record_decision(run_id=run_id, packet_id=packet["packet_id"],
                               decision="blocked",
                               reason=f"ps605 refusal: {refusal.get('code')}",
                               result="BLOCKED")
        return {"refused": True, "refusal": refusal, "refusal_path": sealed["path"],
                "refusal_hash": sealed["refusal_hash"],
                "ledger_chain": ledger.verify_chain()}

    dispatch = make_dispatch_receipt(**bound.decision.to_ps638_receipt_kwargs())
    code = preference_violation(preferred_target_id, dispatch.selected_target_id)
    if code:
        # PS-605 chose a different node than the operator named. Refuse instead of
        # dispatching: the preference is a constraint, not a hint to be overridden.
        refusal = {"code": code, "refused": True,
                   "reason": (f"the operator preferred {preferred_target_id!r}; PS-605 "
                              f"selected {dispatch.selected_target_id!r}, and "
                              "executing on another node is not this run's decision "
                              "to make"),
                   "candidates": [a.to_dict() for a in bound.decision.candidates]}
        sealed = seal_dispatch_refusal(
            refusal, packet=packet, execution_package_hash=ep.package_hash,
            run_id=run_id, records=records, run_dir=run_dir)
        ledger.record_run(run_id=run_id, packet_id=packet["packet_id"],
                          objective=str(packet.get("objective") or ""),
                          role=str(packet.get("role") or "local_implementer"),
                          target_id="", host="", model="", runtime_version="",
                          worktree=str(worktree),
                          write_scope=packet.get("write_scope") or (),
                          base_sha=str(packet.get("base_sha") or ""),
                          interface_digest=getattr(ep, "interface_digest", ""))
        ledger.record_decision(run_id=run_id, packet_id=packet["packet_id"],
                               decision="blocked", reason=f"ps605 refusal: {code}",
                               result="BLOCKED")
        return {"refused": True, "refusal": refusal, "refusal_path": sealed["path"],
                "refusal_hash": sealed["refusal_hash"],
                "ledger_chain": ledger.verify_chain()}
    write_json(run_dir / "dispatch_receipt.json", dispatch.to_dict())
    write_json(run_dir / "routing_decision.json", {
        **bound.to_dict(),
        "selected_target_id": dispatch.selected_target_id,
        "dispatch_receipt_hash": dispatch.receipt_hash,
        "fleet_skipped": [dict(s) for s in inputs.skipped],
        "candidate_order": [c["target_id"] for c in
                            bound.decision.to_dict()["candidates"]
                            if c.get("eligible")]})
    return {"refused": False, "bound": bound, "dispatch": dispatch,
            "records": records, "spec": spec,
            "dispatch_receipt_hash": dispatch.receipt_hash}


def seal_dispatch_refusal(refusal: Mapping, *, packet: Mapping,
                          execution_package_hash: str, run_id: str,
                          records, run_dir: Path) -> dict:
    """Seal a PS-605 refusal: auditable, package-bound, immutable, no AttemptReceipt.

    PS-638's ``DispatchDecisionReceipt`` CANNOT represent this: it requires a
    selected target, host and model, and a refusal has none. Inventing empty strings
    there would be a lie dressed as a receipt — and a test asserts that builder
    refuses empty selections, so the gap is pinned rather than papered over. The
    canonical record that a run stopped before dispatch is the ExecutionLedger; this
    file adds the routing REASONS, bound to the same package hash and
    content-addressed, so a later reader can re-derive it without a second authority.
    """
    from src.execution_package import _canonical, _sha256_hex, utc_now

    payload = {
        "schema_version": 1,
        "kind": "dispatch_refusal",
        "sealed_at": utc_now(),
        "run_id": run_id,
        "packet_id": str(packet.get("packet_id") or ""),
        "execution_package_hash": execution_package_hash,
        "refusal": dict(refusal),
        "fleet": [{"target_id": r.target_id, "health": r.health,
                   "proven": list(r.proven_capabilities())} for r in records],
        "attempt_receipts": [],
        "note": ("no attempt occurred, so there is no AttemptReceipt and no "
                 "EvidencePackage: a sealed package about a run that did not happen "
                 "would be a document about nothing"),
    }
    payload["refusal_hash"] = _sha256_hex(_canonical(payload))
    path = run_dir / "dispatch_refusal.json"
    write_json(path, payload)
    return {"path": str(path), "refusal_hash": payload["refusal_hash"],
            "payload": payload}


def dispatch_refusal_is_intact(payload: Mapping) -> bool:
    """True when a sealed refusal still hashes to its own fields."""
    from src.execution_package import _canonical, _sha256_hex

    body = {k: v for k, v in dict(payload).items() if k != "refusal_hash"}
    return payload.get("refusal_hash") == _sha256_hex(_canonical(body))


def preference_violation(preferred_target_id: str,
                         selected_target_id: str) -> str:
    """A stated preference that was not selected is a REFUSAL, not a re-point.

    The operator's ``--target`` expresses which node this run is allowed to use; if
    PS-605 selects a different one (because the preferred node is unhealthy, stale or
    ineligible), executing anyway would be exactly the silent substitution the
    integrity rules forbid. Returns the refusal code, or "" when they agree.
    """
    if not preferred_target_id or preferred_target_id == selected_target_id:
        return ""
    return "preferred_target_not_selected"


def pin_matches_client(bound, spec, client) -> Tuple[bool, str]:
    """The runtime client must BE the pinned target: host and model must agree.

    The last line of defence after the decision: if the client that will actually
    run the packet is not the host/model the receipt names, the run must stop rather
    than execute on a target the evidence cannot describe.
    """
    pin = bound.pin_for(bound.decision.selected_profile.profile_id)
    if pin is None:
        return False, "the selected profile is not in the decision's eligible set"
    for name, pinned, actual in (("host", str(pin.get("host") or ""),
                                  str(getattr(client, "ssh_host", "") or "")),
                                 ("model", str(pin.get("model") or ""),
                                  str(getattr(client, "model", "") or ""))):
        if pinned and actual and pinned != actual:
            return False, (f"{name}: the decision pinned {pinned!r}, the runtime "
                           f"client is {actual!r}")
    return True, ""


def run_case(worktree: Path, case_name: str, target_id: str,
             num_ctx: int) -> int:
    sys.path.insert(0, str(worktree))
    import cases as case_mod
    import src.local_worker_loop as lo
    from src import dispatch_boundary as dbd
    from src.attempt_receipt import (
        ArtifactRef, context_projection_digest, make_attempt_receipt,
        make_verification_receipt)
    from src.evidence_contract import (
        INDEPENDENCE_HARNESS_HIDDEN, KIND_CONTEXT_DELIVERY, EvidenceRequirement)
    from src.evidence_package import (
        find_secret_shaped, seal_evidence_package, validate_evidence_package)
    from src.execution_ledger import ExecutionLedger
    # NOTE: the operator-pin receipt builder is deliberately not imported any more:
    # PS-605 selects the target and the receipt records that it did.
    from src.execution_package import (
        VerificationPlan, build_execution_package, default_requirements,
        make_dispatch_receipt, seal_verifier_digests)
    from src.repair_packet import failure_fingerprint, parse_verification_failure
    from src.source_snapshot import take_source_snapshot
    from src.worker_context import render_worker_context

    case = case_mod.CASES[case_name]()
    case.worktree = worktree
    run_id = f"{case_name}-{utc_stamp()}"
    run_dir = worktree / "data" / "live" / run_id
    store = RunStore(run_dir)
    ledger = ExecutionLedger(str(run_dir / "ledger.jsonl"))
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

    # ---- PS-605 is the routing authority. The harness does not choose. ---------
    routing = route_packet(worktree, packet, ep, run_id, run_dir,
                           preferred_target_id=target_id, ledger=ledger)
    if routing["refused"]:
        write_json(run_dir / "result.json", {
            "run_id": run_id, "result": "REFUSED", "model_calls": 0,
            "package_sealed": False, "ps605_refusal": routing["refusal"],
            "dispatch_refusal_path": routing["refusal_path"],
            "dispatch_refusal_hash": routing["refusal_hash"],
            "reason": ("PS-605 refused this packet against the measured fleet: no "
                       "target may be selected, so no model was called and no "
                       "package was sealed")})
        print(f"{run_id}: REFUSED by PS-605 "
              f"({routing['refusal'].get('code')}) — 0 model calls, no dispatch")
        return 0

    dispatch = routing["dispatch"]
    target_id = dispatch.selected_target_id
    pin = routing["bound"].pin_for(
        routing["bound"].decision.selected_profile.profile_id)
    # Runtime identity comes from the RECEIPT (one source): the pin carries the
    # target/host/model, the receipt carries what the runtime reported.
    version = str(dispatch.selected_runtime_version or "")
    model_digest = str(dispatch.selected_model_digest or "")

    # The runtime client is built FROM the decision, then checked against it. A
    # mismatch stops the run: this is the point where a hand-pin would have to
    # happen, and it cannot.
    client = oc.target(target_id)
    agreed, why = pin_matches_client(routing["bound"], routing["spec"], client)
    if not agreed:
        write_json(run_dir / "pin_violation.json", {
            "run_id": run_id, "result": "PIN_VIOLATION", "reason": why,
            "model_calls": 0, "dispatch_receipt_hash": dispatch.receipt_hash,
            "selected_target_id": dispatch.selected_target_id})
        print(f"{run_id}: PIN_VIOLATION — {why} (0 model calls)")
        return 0
    try:
        dbd.verify_invocation(routing["bound"],
                              profile_id=dispatch.selected_target_id,
                              model=client.model,
                              chat_url=routing["spec"].endpoint or "")
    except Exception as exc:
        write_json(run_dir / "pin_violation.json", {
            "run_id": run_id, "result": "PIN_VIOLATION", "reason": str(exc),
            "model_calls": 0, "dispatch_receipt_hash": dispatch.receipt_hash})
        print(f"{run_id}: PIN_VIOLATION — {exc} (0 model calls)")
        return 0

    requirement_ids = tuple(r.requirement_id for r in ep.evidence_requirements)
    advisor = None
    planner_policy = None
    if getattr(case, "advises", False):
        # G2: the advisory manager runs on the SAME pinned target as the worker.
        from src.replanner import PlannerPolicy

        advisor = LiveAdvisor(
            client, store, num_ctx=num_ctx,
            max_output_tokens=getattr(case, "manager_max_output_tokens", 800),
            preflight=getattr(case, "planner_preflight", None))
        planner_policy = PlannerPolicy(
            allowed_tools=("write_file",), network_policy="tailnet-loopback",
            capabilities=tuple(packet.get("target_requirements") or ()),
            verifier_id=test_rel, verifier_digest=plan.digest_of(test_rel))
    return _execute(case, packet, test_rel, run_id, run_dir, store, ledger, client,
                    target_id, num_ctx, version, model_digest, plan, ep,
                    requirement_ids, relevant, snapshot, source_before,
                    advisor=advisor, planner_policy=planner_policy, routing=routing)


def _execute(case, packet, test_rel, run_id, run_dir, store, ledger, client,
             target_id, num_ctx, version, model_digest, plan, ep, requirement_ids,
             relevant, snapshot, source_before, *, advisor=None,
             planner_policy=None, routing=None) -> int:
    import src.local_worker_loop as lo
    from src.attempt_receipt import (
        ArtifactRef, context_projection_digest, make_attempt_receipt,
        make_verification_receipt)
    from src.evidence_package import (
        seal_evidence_package, validate_evidence_package)
    from src.execution_package import make_dispatch_receipt
    from src.repair_packet import failure_fingerprint, parse_verification_failure
    from src.worker_context import render_worker_context

    # PS-605's decision, sealed as PS-638's OWN receipt type. The harness does not
    # construct a receipt from an operator pin any more: there is no code path here
    # that can name a target PS-605 did not select.
    dispatch = routing["dispatch"]
    pin = routing["bound"].pin_for(
        routing["bound"].decision.selected_profile.profile_id)
    if dispatch.selected_target_id != target_id or str(pin.get("target_id")) != target_id:
        raise SystemExit(
            f"routing/dispatch disagree about the target: {target_id!r}")
    write_json(run_dir / "dispatch_receipt.json", dispatch.to_dict())

    base_context = render_worker_context(packet) + case.source_material(
        case.worktree) + case.tool_instruction
    case.preflight(base_context)
    (run_dir / "base_context.txt").write_text(base_context)

    # The pinned target is the DECISION's identity, field for field.
    pinned = lo.PinnedTarget(target_id=target_id, host=str(pin.get("host") or ""),
                             model=str(pin.get("model") or ""),
                             runtime_version=version,
                             worktree=str(case.worktree), served_context=num_ctx)
    dispatcher = Dispatcher(case.worktree, packet["write_scope"], client, store,
                            num_ctx=num_ctx,
                            max_output_tokens=case.max_output_tokens)
    verifier = Verifier(case.worktree, test_rel, store,
                        lambda: snapshot()["snapshot_digest"])

    started = time.monotonic()
    result = lo.run_bounded(
        packet, run_id=run_id, ledger=ledger, pinned=pinned, dispatch=dispatcher,
        verify=verifier, base_context=base_context, max_attempts=case.max_attempts,
        read_changed_files=changed_files(case.worktree, packet["write_scope"]),
        advise=advisor, max_replans=int(getattr(case, "max_replans", 0)),
        planner_policy=planner_policy)
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
        written_paths = tuple(call.get("writes") or ())
        refs = []
        for written in written_paths:
            written_path = case.worktree / written
            if written_path.exists():
                file_ref = store.store(
                    f"attempt{number}-artifact-{Path(written).name}",
                    written_path.read_bytes())
                refs.append(ArtifactRef(**file_ref))
        artifact_refs = tuple(refs)
        attempts.append(make_attempt_receipt(
            receipt_id=f"attempt-{run_id}-{number}", run_id=run_id,
            packet_id=packet["packet_id"], attempt=number,
            repair_of=0 if number == 1 else number - 1,
            execution_package_hash=ep.package_hash,
            dispatch_receipt_hash=dispatch.receipt_hash,
            target_id=target_id, host=str(pin.get("host") or ""),
            model=str(pin.get("model") or ""),
            runtime_kind=str(pin.get("runtime_kind") or "ollama"),
            runtime_version=version, model_version=model_digest,
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
            actual_write_set=written_paths,
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

    # The chain, checked rather than assumed, before anything is sealed: package
    # hash -> dispatch receipt hash -> every attempt's dispatch_receipt_hash -> the
    # target that actually ran. A break here means the evidence would describe a
    # different run than the one that happened.
    chain = {
        "execution_package_hash": ep.package_hash,
        "dispatch_receipt_hash": dispatch.receipt_hash,
        "attempts_bound": all(a.dispatch_receipt_hash == dispatch.receipt_hash
                              and a.execution_package_hash == ep.package_hash
                              for a in attempts),
        "attempt_targets": sorted({a.target_id for a in attempts}),
        "pinned_target_id": dispatch.selected_target_id,
        "run_target_id": target_id,
        "receipt_target_matches_pin": dispatch.selected_target_id == target_id,
        "attempts_on_the_pinned_target": all(a.target_id == dispatch.selected_target_id
                                             for a in attempts),
    }
    chain["ok"] = bool(chain["attempts_bound"]
                       and chain["receipt_target_matches_pin"]
                       and chain["attempts_on_the_pinned_target"])
    write_json(run_dir / "dispatch_chain.json", chain)
    if not chain["ok"]:
        write_json(run_dir / "result.json", {
            "run_id": run_id, "result": "CHAIN_BROKEN", "model_calls":
            len(dispatcher.calls), "package_sealed": False,
            "reason": "an attempt does not bind the dispatch receipt that "
                      "authorised it, so no package was sealed"})
        print(f"{run_id}: CHAIN_BROKEN — refusing to seal evidence")
        return 0

    package = seal_evidence_package(
        evidence_package_id=f"evpkg-{run_id}", execution_package=ep,
        dispatch_receipts=(dispatch,), attempt_receipts=tuple(attempts),
        verification_receipts=tuple(verifications),
        seals=manager_seals(
            advisor=advisor, ledger=ledger, run_id=run_id, packet=packet,
            store=store, run_dir=run_dir, execution_package_hash=ep.package_hash,
            dispatch_receipt_hash=dispatch.receipt_hash, target_id=target_id,
            model=client.model))
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
        "manager": (None if advisor is None else {
            **advisor.summary(),
            "records": [{"index": r["index"], "boundary": r["boundary"],
                         "input_digest": r["input_digest"],
                         "proposal": r["proposal"],
                         "parse_reason": r["proposal_reason"],
                         "status": r["status"]} for r in advisor.records],
            "ledger_proposals": [e.payload for e in ledger.proposals(run_id)],
            "ledger_refusals": [e.payload for e in ledger.proposal_refusals(run_id)],
            "seal_count": len(package.seals),
        }),
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


def verify_only(worktree: Path, verifier: str, out_dir: Path, *,
                base_sha: str = "", label: str = "baseline") -> int:
    """Run a deterministic verifier on a tree and SEAL a PS-638 VerificationReceipt.

    This is the exact-base comparator PS-638 requires before a failure may be
    called pre-existing. It is bound to the tree it measured and carries the
    normalized failure fingerprint, so a later claim can CITE it rather than infer
    it from "the files did not change".

    No EvidencePackage is sealed, deliberately: a package whose requirement
    closure has verification receipts and no attempt would be rejected by this
    project's own validator (``no_attempts``), and correctly so — a comparator is
    not a run. Sealing one anyway would be a document about nothing.
    """
    # Imported from the HARNESS tree, not the tree under measurement: the evidence
    # layer is the tool, and a base commit that predates it can still be sealed.
    from src.attempt_receipt import ArtifactRef, make_verification_receipt
    from src.execution_package import seal_verifier_digests
    from src.repair_packet import failure_fingerprint, parse_verification_failure
    from src.source_snapshot import take_source_snapshot

    out_dir = Path(out_dir)
    store = RunStore(out_dir)
    source = take_source_snapshot(str(worktree), base_sha=base_sha,
                                  relevant_paths=[verifier])
    digests = dict(seal_verifier_digests(str(worktree), [verifier]))
    command = f"python3 -m pytest {verifier} -q -p no:cacheprovider"

    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "pytest", verifier, "-q",
                           "-p", "no:cacheprovider"],
                          cwd=str(worktree), capture_output=True, text=True,
                          timeout=900)
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    elapsed = round(time.monotonic() - started, 3)
    output = stdout + stderr
    counts = pytest_counts(stdout)
    failure = parse_verification_failure(command, proc.returncode, output)

    receipt = make_verification_receipt(
        receipt_id=f"{label}-{worktree.name}", run_id=label, packet_id=label,
        attempt=1, execution_package_hash=f"{label}:no-package",
        verifier_id=verifier, verifier_digest=digests.get(verifier, ""),
        verifier_paths=(verifier,), source_snapshot_digest=source.snapshot_digest,
        normalized_command=command, exit_code=proc.returncode,
        stdout_ref=ArtifactRef(**store.store(f"{label}-stdout.txt",
                                            stdout.encode("utf-8"))),
        stderr_ref=ArtifactRef(**store.store(f"{label}-stderr.txt",
                                            stderr.encode("utf-8"))),
        worktree=str(worktree), host="local control plane",
        tests_collected=counts["collected"], tests_executed=counts["executed"],
        tests_passed=counts["passed"], tests_failed=counts["failed"],
        tests_skipped=counts["skipped"],
        requirement_ids=(),          # a comparator closes nothing in a closure
        proof_class="EXISTING_AUTHORITATIVE",
        failure_fingerprint=failure_fingerprint(failure),
        elapsed_s=elapsed)

    write_json(out_dir / "baseline_receipt.json", receipt.to_dict())
    write_json(out_dir / "baseline_probe.json", {
        "label": label, "worktree": str(worktree),
        "head_sha": source.head_sha, "base_sha": source.base_sha,
        "source_snapshot_digest": source.snapshot_digest,
        "source_disposition": source.disposition(),
        "verifier": verifier, "verifier_digest": digests.get(verifier, ""),
        "exit_code": receipt.exit_code, "outcome": receipt.outcome,
        "tests_passed": receipt.tests_passed, "tests_failed": receipt.tests_failed,
        "failure_fingerprint": receipt.failure_fingerprint,
        "receipt_hash": receipt.receipt_hash,
        "failing_tests": list(failure.failing_tests),
        "note": ("exact-base comparator; no model was called and no EvidencePackage "
                 "was sealed, because a comparator is not a run"),
    })
    print(f"{label}: exit {receipt.exit_code} -> {receipt.outcome} "
          f"({receipt.tests_passed} passed, {receipt.tests_failed} failed) "
          f"| fingerprint {receipt.failure_fingerprint[:16]}")
    print(f"  source snapshot {source.snapshot_digest[:16]} @ {source.head_sha[:12]}"
          f" | receipt {receipt.receipt_hash[:16]}")
    print(f"  written to {out_dir}")
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
    parser.add_argument("--verify-only", metavar="WORKTREE",
                        help="seal an exact-base VerificationReceipt; no model call")
    parser.add_argument("--out", metavar="DIR",
                        help="output directory for --verify-only")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()

    if args.summarise:
        return summarise_run(Path(args.summarise))

    if args.verify_only:
        import cases as case_mod
        if not args.case or args.case not in case_mod.CASES:
            print(f"a case is required for --verify-only: {sorted(case_mod.CASES)}",
                  file=sys.stderr)
            return 2
        verifier = case_mod.CASES[args.case]().verifier
        out = Path(args.out) if args.out else \
            Path(args.verify_only) / "data" / "live" / f"{args.label}-{utc_stamp()}"
        return verify_only(Path(args.verify_only).resolve(), verifier, out,
                           base_sha=args.base_sha, label=args.label)

    if not args.worktree:
        parser.error("worktree is required (or use --summarise RUN_DIR)")
    worktree = Path(args.worktree).resolve()
    if not (worktree / "src").is_dir():
        print(f"not an odysseus worktree: {worktree}", file=sys.stderr)
        return 2
    return run_case(worktree, args.case, args.target, args.num_ctx)


if __name__ == "__main__":
    sys.exit(main())





