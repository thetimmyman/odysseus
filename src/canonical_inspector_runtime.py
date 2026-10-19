"""Finite app-owned TMOS execution composition, disabled in normal defaults.

Configuration is admitted intent, never runtime qualification. Native schema,
restore, current-reader/adoption and transport acceptance remain separate.
No imports, construction or startup operation creates schema or calls a model.

The private fixed manifest holds schema canonical-tmos-owner-v1, an exact
principal (auth_key/account_epoch), and packages keyed by the two admitted
packet IDs. Each package is sealed through the normal ExecutionPackage authoring
interface before execution. Runtime configuration supplies a source-pinned
normal lease client, identity_commands keyed by registered target ID, optional
read-only SSH configuration projection, and preferred_profiles. None of these
fields qualify evidence or grant routing eligibility. The verifier's frozen
command must emit tmos-work-verification-v1 JSON with actual test counts and
positive/negative control observations; its source digests are checked before
and after execution. Ordinary pytest console text is not that protocol.
"""
from dataclasses import dataclass
import asyncio
from functools import partial
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import threading
import sys
import uuid

from src.canonical_inspector_admission import AdmissionStore, PreparedTmosWork
from src.inspector_errors import InspectorUnavailable
from src.canonical_inspector_workspace import (
    manifest_digest, _manifest_file, workspace_operation as workspace_operation,
)
from src.canonical_inspector_service import InspectorService, same_work_intent
from src.constants import (CANONICAL_INSPECTOR_RETAINED_ROOT, CANONICAL_TMOS_MANIFEST_FILE,
                           CANONICAL_TMOS_PACKET_IDS, CANONICAL_TMOS_DISCLOSURE_GENERATION,
                           LOCAL_MEASUREMENT_COMMAND)
from src.execution_package import package_hash_is_valid, compute_package_hash
from src.local_measurement_process import SystemdProcesses
from src.routing_inspector import project_retained_routing_inspection


class TmosExecutionOwner:
    """One app's finite admitted templates and concrete I/O ownership."""

    def __init__(self, manifest, *, store, auth_manager, retained_root, process_factory=SystemdProcesses,
                 routing_reader=None):
        self._manifest = json.loads(json.dumps(manifest, allow_nan=False))
        if (self._manifest.get("schema") != "canonical-tmos-owner-v1"
                or type(self._manifest.get("packages")) is not dict
                or not self._manifest["packages"]
                or set(self._manifest["packages"]) - set(CANONICAL_TMOS_PACKET_IDS)):
            raise InspectorUnavailable("inspector unavailable")
        for packet_id, package in self._manifest["packages"].items():
            if (type(package) is not dict or package.get("packet_id") != packet_id
                    or not package_hash_is_valid(package) or type(package.get("ticket_key")) is not str
                    or re.fullmatch(r"TMOS-[1-9][0-9]*", package["ticket_key"]) is None):
                raise InspectorUnavailable("inspector unavailable")
        self._store, self._auth = store, auth_manager
        self._root, self._process_factory = Path(retained_root), process_factory
        self._closed = False
        self._active = {}
        self._routing_reader = routing_reader
        self._busy = False
        self._blocked = False
        self._actions = {}

    def submit_alternate(self, service, owner, handle, receipt, original_package, reauthorize):
        self._authorize(owner, reauthorize)
        request_hash = receipt["receipt_hash"]
        rows = self._store.interventions(owner, handle, CANONICAL_TMOS_DISCLOSURE_GENERATION)
        if (receipt not in rows or any(row.get("operator_request_receipt_hash") == request_hash for row in rows)
                or request_hash in self._actions):
            return
        if self._busy or self._blocked:
            raise InspectorUnavailable("inspector unavailable")
        template = self._manifest["packages"].get(receipt["packet_id"])
        if template is None or not same_work_intent(original_package, template):
            raise InspectorUnavailable("inspector unavailable")

        async def run():
            claim = None
            try:
                claim, result = await self.execute(owner, receipt["packet_id"], "alternate:" + request_hash,
                    reauthorize, alternate_of=(handle, receipt))
                service.publish_tmos_alternate(owner, handle, request_hash, claim, result, reauthorize)
            except Exception:
                self._blocked = True
                if claim is not None:
                    self._store.fail_tmos_alternate(claim, owner, stage="publication")
        task = asyncio.create_task(run())
        self._actions[request_hash] = task

        def settled(completed):
            self._actions.pop(request_hash, None)
            if completed.cancelled() or completed.exception() is not None:
                self._blocked = True

        task.add_done_callback(settled)

    def _authorize(self, owner, reauthorize):
        if (self._closed or reauthorize() != owner
                or self._manifest.get("principal") != owner.__dict__
                or self._auth.snapshot_admin_account_epoch(owner.auth_key) != (owner.auth_key, owner.account_epoch)):
            raise InspectorUnavailable("inspector unavailable")

    def _inputs(self):
        from src.local_target_routing import persisted_routing_inputs
        from src.target_capability_store import store_from_env
        metadata = {}
        if self._routing_reader is not None:
            return self._routing_reader(now=datetime.now(timezone.utc), provenance_sink=metadata)
        return persisted_routing_inputs(store_from_env(), now=datetime.now(timezone.utc), provenance_sink=metadata)

    async def execute(self, owner, packet_id, key, reauthorize, *, alternate_of=None):
        """One app-owned invocation; an uncertain job never becomes a retry."""
        if alternate_of is not None:
            original_handle = alternate_of[0]
            self._store.resolve(owner, original_handle, CANONICAL_TMOS_DISCLOSURE_GENERATION)
        self._authorize(owner, reauthorize)
        if (self._busy or self._blocked or packet_id not in self._manifest["packages"]
                or type(key) is not str or not 0 < len(key.encode()) <= 128):
            raise InspectorUnavailable("inspector unavailable")
        template = self._manifest["packages"][packet_id]
        budgets = template["budgets"]
        if (budgets["max_attempts"] != 1 or not 0 < budgets["context_tokens"] <= 4096
                or not 0 < budgets["output_tokens"] <= 512 or not 0 < budgets["time_seconds"] <= 180):
            raise InspectorUnavailable("inspector unavailable")
        self._busy = True
        call = {"processes": None, "exited": threading.Event(), "resolved": False,
                "deadline": time.monotonic() + budgets["time_seconds"], "stage": "boundary"}
        task = asyncio.create_task(asyncio.to_thread(self._execute_sync, call, owner, packet_id,
                                                    key, reauthorize, alternate_of))
        self._active[task] = call
        try:
            done, _ = await asyncio.wait({task}, timeout=budgets["time_seconds"])
            if task not in done:
                self._blocked = True
                raise InspectorUnavailable("inspector unavailable")
            return task.result()
        except asyncio.CancelledError:
            call["cancelled"] = True
            if call["processes"] is not None:
                call["processes"].request_stop()
            raise
        finally:
            if task.done() and call["exited"].is_set() and call["resolved"]:
                self._active.pop(task, None)
                self._busy = False
            else:
                self._blocked = True

    def _execute_sync(self, call, owner, packet_id, key, reauthorize, alternate_of):
        from src.canonical_pilot_adapter import PilotExecution
        from src.dispatch_boundary import TargetEstate, resolve_from_estate
        from src.dispatch_routing import (RoutingRequest, policy_snapshot, ROLE_APPROXIMATE_IMPLEMENTER,
                                          EXACTNESS_APPROXIMATE, EXACTNESS_EXACT, RoutingRefused)
        from src.evidence_io import retain_evidence_bundle
        from src.local_measurement_owner import BrokerLease
        from src.local_measurement_config import ReadOnlyConfigProjection
        from src.local_worker_loop import run_local_worker_loop
        from src.inspector_alternate import select_requested_alternate

        lease, processes, claim = None, None, None
        try:
            runtime = self._manifest["runtime"]
            self._authorize(owner, reauthorize)
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([
                manifest_digest(self._manifest), owner.auth_key, owner.account_epoch, packet_id, key],
                separators=(",", ":"))))
            transport_options = {}
            if runtime.get("execution_transport") is not None:
                from src.local_measurement_transport import SshTransport
                transport_options["transport"] = SshTransport(self._manifest, run_id=run_id,
                    packet_id=packet_id, deadline=call["deadline"])
            projection = (ReadOnlyConfigProjection(runtime["ssh_config_projection"])
                          if not transport_options and runtime.get("ssh_config_projection") is not None else None)
            command = [sys.executable, "-I", LOCAL_MEASUREMENT_COMMAND]
            processes = self._process_factory(worker_command=command + ["--worker-http"],
                broker_worker=command + ["--worker-broker", "--lease-client", runtime["lease_client"],
                    "--lease-client-sha256", runtime["lease_client_sha256"]],
                workspace_worker=command + ["--worker-workspace", "--tmos-manifest", CANONICAL_TMOS_MANIFEST_FILE],
                config_projection=projection, **transport_options)
            call["processes"] = processes

            def guard():
                self._authorize(owner, reauthorize)
                if call.get("cancelled") or time.monotonic() >= call["deadline"]:
                    raise InspectorUnavailable("inspector unavailable")
                return True

            guard()
            if transport_options:
                processes.qualify_no_provider(lease_valid=guard)
            else:
                processes.qualify_no_provider()
            operation = {"schema": "tmos-workspace-operation-v1", "packet_id": packet_id, "run_id": run_id,
                         "manifest_digest": manifest_digest(self._manifest)}
            call["stage"] = "prepare"
            prepared_source = processes.workspace({**operation, "operation": "prepare"}, lease_valid=guard)
            if prepared_source.get("cleanup_confirmed") is not True:
                raise InspectorUnavailable("inspector unavailable")
            package = json.loads(json.dumps(self._manifest["packages"][packet_id]))
            package.update(run_id=run_id, package_id=str(uuid.uuid4()), source=prepared_source["source"])
            package["package_hash"] = compute_package_hash({name: value for name, value in package.items() if name != "package_hash"})
            call["stage"] = "admission"
            prepared = PreparedTmosWork(run_id, packet_id, package["package_hash"], manifest_digest(self._manifest))
            claim = self._store.admit_tmos_work(owner, key, prepared)
            self._store.consume(claim, owner)
            if alternate_of is None:
                self._store.begin_tmos_work(claim, owner)
            else:
                self._store.begin_tmos_alternate(claim, owner, alternate_of[0], alternate_of[1]["receipt_hash"])
            call["stage"] = "routing"
            inputs = self._inputs()
            policy = policy_snapshot()
            role = package["execution_role"]
            request = RoutingRequest(domain="general_swe", role=role, run_id=run_id, packet_id=packet_id,
                execution_package_hash=package["package_hash"], capabilities=tuple(package["required_capabilities"]),
                exactness=EXACTNESS_APPROXIMATE if role == ROLE_APPROXIMATE_IMPLEMENTER else EXACTNESS_EXACT,
                local_only=True, required_tools=tuple(package["allowed_tools"]), network_policy=package["network_policy"],
                read_scope=tuple(package["read_scope"]), write_scope=tuple(package["write_scope"]),
                minimum_context_tokens=package["budgets"]["context_tokens"],
                preferred_profile_ids=tuple(runtime.get("preferred_profiles", ())))
            options = dict(estate=TargetEstate(profiles=inputs.profiles, receipts=inputs.receipts, skipped=inputs.skipped),
                capability_store=inputs.capability_store, network_classes=inputs.network_classes,
                policy=policy, now=datetime.now(timezone.utc))
            bound = (resolve_from_estate(request=request, **options) if alternate_of is None else
                     select_requested_alternate(request, alternate_of[1], actor=owner, **options))
            profile = bound.decision.selected_profile
            if profile.runtime_kind != "ollama" or profile.locality != "local":
                raise InspectorUnavailable("inspector unavailable")
            lease = BrokerLease(processes.post, endpoint=profile.endpoint_url, holder=run_id)
            call["stage"] = "lease"
            guard(); lease.acquire()

            def valid():
                return guard() and lease.valid()

            def workspace(action, **extra):
                guard(); lease.check()
                captured = processes.workspace({**operation, "operation": action, "package": package, **extra},
                                               lease_valid=valid)
                if captured.get("cleanup_confirmed") is not True:
                    raise InspectorUnavailable("inspector unavailable")
                return captured

            def observation():
                guard(); lease.check()
                return self._observation(bound, processes, runtime, valid, inputs.capability_store)

            def facts(dispatch, number):
                current, invocation = observation()
                return self._facts(dispatch, package, current, invocation)

            def worker(dispatch, context, number, output_tokens):
                call["stage"] = "invoke"
                observation()
                return self._worker(dispatch, package, context, number, output_tokens, processes, valid,
                                    workspace, lease)

            def verifier(dispatch, attempt, number):
                call["stage"] = "verify"
                seconds = min(package["verification"]["timeout_s"], int(call["deadline"] - time.monotonic()))
                checked = self._verifier(dispatch, package, number,
                    workspace("verify", policy_hash=policy.policy_hash, timeout_s=seconds))
                call["verifier_completed"] = True
                return checked

            result = run_local_worker_loop(execution_package=package, standing_dispatch=bound,
                current_facts=facts, invocation=lambda _dispatch: observation()[1],
                execute_worker=worker, execute_verifier=verifier,
                current_source=lambda: workspace("snapshot")["source"], advisory_budget=0)
            if (result.evidence_package is None or len(result.attempts) != 1
                    or call.get("verifier_completed") is not True):
                raise InspectorUnavailable("inspector unavailable")
            call["stage"] = "post_identity"
            observation()
            call["stage"] = "cleanup"
            if processes.finish_capture() is not True or not asyncio.run(processes.confirm_stopped()):
                raise InspectorUnavailable("inspector unavailable")
            call["stage"] = "release"
            lease.release_completed()
            call["stage"] = "retention"
            path = retain_evidence_bundle(self._root, result.evidence_package, artifacts=result.artifacts)
            if transport_options and processes.finalize_ownership(allowed=guard) is not True:
                raise InspectorUnavailable("inspector unavailable")
            call["resolved"] = True
            return claim, PilotExecution(path.stem, result.attempts[0].receipt_hash, True)
        except Exception as failure:
            if claim is not None and alternate_of is not None:
                self._store.fail_tmos_alternate(claim, owner, stage=call["stage"],
                    refusal_code=failure.code if isinstance(failure, RoutingRefused) else None)
            raise InspectorUnavailable("inspector unavailable") from None
        finally:
            try:
                if lease is not None and not call["resolved"]:
                    lease.abandon()
            finally:
                call["exited"].set()

    def _observation(self, bound, processes, runtime, valid, store):
        """Bind fresh native material, never fill observations from a desired pin."""
        from src.dispatch_boundary import InvocationIdentity, verify_invocation

        profile = bound.decision.selected_profile
        current = self._inputs()
        fresh = next((value for value in current.profiles if value.profile_id == profile.profile_id), None)
        receipt = store.current(profile.profile_id)
        now = datetime.now(timezone.utc)
        if (fresh is None or fresh.to_dict() != profile.to_dict() or receipt is None
                or receipt.receipt_hash not in bound.decision.capability_receipt_refs
                or receipt.qualification_state(now=now) != "valid" or receipt.health_state(now=now) != "live"
                or receipt.host.ssh_host and runtime.get("ssh_config_projection") is None):
            raise InspectorUnavailable("inspector unavailable")
        command = runtime["identity_commands"][profile.target_id]
        if type(command) is not list or not command or any(type(arg) is not str or not arg for arg in command):
            raise InspectorUnavailable("inspector unavailable")
        observed = processes.identity(command, lease_valid=valid)
        now = datetime.now(timezone.utc)
        checked = datetime.fromisoformat(observed["checked_at"].replace("Z", "+00:00"))
        material = observed["current_material_identity"]
        if (observed.get("profile_id") != profile.profile_id or observed.get("comparison", {}).get("equal") is not True
                or store.current(profile.profile_id) != receipt
                or receipt.qualification_state(now=now) != "valid" or receipt.health_state(now=now) != "live"
                or checked.tzinfo is None or checked > now or (now - checked).total_seconds() > receipt.health_ttl_s
                or json.dumps(material, sort_keys=True, allow_nan=False) != json.dumps(receipt.material_identity(), sort_keys=True, allow_nan=False)):
            raise InspectorUnavailable("inspector unavailable")
        native, model, context = material["runtime"], material["model"], material["context"]
        invocation = InvocationIdentity(profile_id=observed["profile_id"], provider=native["provider"],
            runtime_kind=native["runtime_kind"], runtime_version=native["version"], runtime_commit=native["commit"],
            runtime_image_digest=native["image_digest"], backend=native["backend"], backend_version=native["backend_version"],
            model=model["model_id"], model_digest=model["digest"], chat_url=native["endpoint_url"],
            endpoint_type=native["endpoint_type"], locality="local", runtime_options=context["options"],
            configured_context=context["configured_context"], configured_served_context=context["configured_served_context"])
        verify_invocation(bound, invocation=invocation)
        return receipt, invocation

    @staticmethod
    def _facts(bound, package, receipt, invocation):
        from src.dispatch_routing import policy_snapshot, classify_capacity_for
        from src.endpoint_identity import canonical_endpoint_identity

        if set(bound.decision.capability_receipt_refs) != {receipt.receipt_hash}:
            raise InspectorUnavailable("inspector unavailable")
        capacity_ok, capacity_refs, _rule, _reason = classify_capacity_for(
            bound.decision.selected_profile, bound.capacity_receipts, now=datetime.now(timezone.utc))
        names = ("provider", "runtime_kind", "runtime_version", "runtime_commit", "runtime_image_digest",
                 "backend", "backend_version", "model", "model_digest", "runtime_options",
                 "configured_context", "configured_served_context")
        return {"capability_receipts": {receipt.receipt_hash: True}, "capacity_fresh": capacity_ok,
            "capacity_receipt_refs": capacity_refs, "policy_ref": policy_snapshot().policy_ref,
            "target_id": bound.decision.selected_profile.target_id, "profile_id": invocation.profile_id,
            "endpoint_identity": canonical_endpoint_identity(invocation.chat_url, invocation.endpoint_type),
            "runtime_identity": {name: getattr(invocation, name) for name in names},
            "granted_tools": bound.decision.granted_tools, "granted_write_scope": bound.decision.granted_write_scope,
            "granted_read_scope": bound.decision.granted_read_scope, "network_policy": bound.decision.network_policy,
            "execution_package_hash": package["package_hash"], "source_digest": package["source"]["snapshot_digest"],
            "interface_digest": package["interface_digest"]}

    @staticmethod
    def _worker(bound, package, context, number, output_tokens, processes, valid, workspace, lease):
        from src.attempt_receipt import artifact_ref, context_projection_digest, make_attempt_receipt
        from src.local_worker_loop import WorkerExecution

        profile = bound.decision.selected_profile
        if ("write_file" not in bound.decision.granted_tools or "write_file" not in package["allowed_tools"]
                or not package["write_scope"]):
            raise InspectorUnavailable("inspector unavailable")
        options = dict(profile.runtime_options)
        if (options.get("num_ctx") != profile.configured_context or options.get("num_predict") != output_tokens
                or options.get("temperature") != 0 or options.get("think") is not False):
            raise InspectorUnavailable("inspector unavailable")
        tool = {"type": "function", "function": {"name": "write_file",
            "description": "Replace PENDING_PATCH in candidate.patch with the complete unified diff for the admitted task.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                "search": {"type": "string"}, "replacement": {"type": "string"}},
                "required": ["path", "search", "replacement"], "additionalProperties": False}}}
        body = {"model": profile.model, "messages": [{"role": "user", "content": context}],
            "tools": [tool], "stream": False, "think": False,
            "options": {key: value for key, value in options.items() if key != "think" and not key.endswith("_sha256")}}
        started, tick = datetime.now(timezone.utc).isoformat(), time.monotonic()
        observed = processes.request(profile.endpoint_url.rstrip("/") + "/api/chat", body, lease_valid=valid)
        raw = json.dumps(observed, sort_keys=True, allow_nan=False).encode()
        message = observed.get("message") or {}
        calls = message.get("tool_calls") or []
        if (len(raw) > 262144 or observed.get("model") != profile.model or observed.get("error")
                or observed.get("done") is not True or observed.get("done_reason") not in ("stop", "tool_calls")
                or message.get("content") not in (None, "") or len(calls) != 1
                or calls[0].get("function", {}).get("name") != "write_file"
                or type(observed.get("prompt_eval_count")) is not int
                or not 0 < observed["prompt_eval_count"] <= profile.configured_context
                or type(observed.get("eval_count")) is not int or not 0 < observed["eval_count"] <= output_tokens):
            raise InspectorUnavailable("inspector unavailable")
        arguments = calls[0]["function"]["arguments"]
        if isinstance(arguments, str):
            from src.local_measurement_process import decode_json
            arguments = decode_json(arguments.encode())
        if (type(arguments) is not dict or set(arguments) != {"path", "search", "replacement"}
                or arguments["path"] != "candidate.patch" or arguments["search"] != "PENDING_PATCH"
                or type(arguments["replacement"]) is not str):
            raise InspectorUnavailable("inspector unavailable")
        patch = arguments["replacement"]
        applied = workspace("apply", patch=patch)
        if applied.get("cleanup_confirmed") is not True:
            raise InspectorUnavailable("inspector unavailable")
        rendered = artifact_ref("rendered-context", context, storage_uri="memory://context")
        output = artifact_ref("provider-response", raw, media_type="application/json", storage_uri="memory://response")
        request = artifact_ref("provider-request", json.dumps(body, sort_keys=True, allow_nan=False),
            media_type="application/json", storage_uri="memory://request")
        produced = artifact_ref("candidate-patch", patch, storage_uri="memory://patch")
        token = lease.evidence().get("lease")
        if not token:
            raise InspectorUnavailable("inspector unavailable")
        receipt = make_attempt_receipt(receipt_id=str(uuid.uuid4()), run_id=package["run_id"],
            packet_id=package["packet_id"], attempt=number, execution_package_hash=package["package_hash"],
            dispatch_receipt_hash=bound.decision.receipt_hash, generation_token="lease-sha256:" + hashlib.sha256(token.encode()).hexdigest(),
            target_id=profile.target_id, host=profile.host, model=profile.model, runtime_kind=profile.runtime_kind,
            runtime_version=profile.runtime_version, model_version=profile.model_digest,
            context_projection_hash=context_projection_digest(context), rendered_context_ref=rendered, output_ref=output,
            requested_context=profile.configured_context, served_context=profile.configured_served_context,
            started_at=started, ended_at=datetime.now(timezone.utc).isoformat(), elapsed_s=time.monotonic() - tick,
            finish_reason=observed["done_reason"], prompt_tokens=observed["prompt_eval_count"], completion_tokens=observed["eval_count"],
            tool_invocations=({"name": "write_file", "arguments": arguments},), declared_write_set=tuple(package["write_scope"]),
            actual_write_set=tuple(applied["changed_files"]), artifact_refs=(request, produced))
        return WorkerExecution(receipt, {rendered.storage_uri: context.encode(), output.storage_uri: raw,
            request.storage_uri: json.dumps(body, sort_keys=True, allow_nan=False).encode(), produced.storage_uri: patch.encode()})

    @staticmethod
    def _verifier(bound, package, number, observed):
        from src.attempt_receipt import artifact_ref, make_verification_receipt
        from src.work_verification_report import decode_work_verification_report
        from src.local_worker_loop import VerificationExecution

        plan = package["verification"]
        stdout, stderr = (base64.b64decode(observed[key], validate=True) for key in ("stdout_b64", "stderr_b64"))
        measured = dict(observed["verifier_digests"])
        if (observed.get("cleanup_confirmed") is not True or measured != dict(plan["verifier_digests"])
                or observed["normalized_command"] != plan["command"] or not measured
                or type(observed.get("timeout_s")) is not int or not 0 < observed["timeout_s"] <= plan["timeout_s"]):
            raise InspectorUnavailable("inspector unavailable")
        out = artifact_ref("verifier-stdout", stdout, storage_uri="memory://verifier-stdout")
        err = artifact_ref("verifier-stderr", stderr, storage_uri="memory://verifier-stderr")
        report = decode_work_verification_report(stdout, command=plan["command"],
            verifier_digests=observed["verifier_digests"]) if observed["exit_code"] == 0 else None
        fields = dict(receipt_id=str(uuid.uuid4()), run_id=package["run_id"], packet_id=package["packet_id"], attempt=number,
            execution_package_hash=package["package_hash"], verifier_id=plan["verifier_id"], normalized_command=plan["command"],
            source_snapshot_digest=observed["source"]["snapshot_digest"], verifier_paths=tuple(plan["verifier_paths"]),
            verifier_digest=measured.get(plan["verifier_id"], next(iter(measured.values()))),
            worktree=observed["source"]["worktree"], host=observed["host"], started_at=observed["started_at"],
            container=observed.get('container_id', ''),
            tool_versions=((("container_image", observed['image_id']),) if observed.get('image_id') else ())
                + tuple(tuple(pair) for pair in (report or {}).get("tool_versions", ())),
            ended_at=observed["ended_at"], elapsed_s=observed["elapsed_s"], exit_code=observed["exit_code"],
            stdout_ref=out, stderr_ref=err, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timeout_s=observed["timeout_s"],
            proof_vantage="harness worktree", requirement_ids=("deterministic_verification", "source_binding", "scope_check"),
            tests_collected=(report or {}).get("tests_collected"),
            tests_executed=(report or {}).get("tests_executed"),
            tests_skipped=(report or {}).get("tests_skipped"),
            tests_passed=report["tests_passed"] if report is not None else None,
            tests_failed=report["tests_failed"] if report is not None else None)
        main = make_verification_receipt(**fields)
        controls = []
        if report is not None:
            for control, expected in (("positive_control", "PASS"), ("negative_control", "FAIL")):
                result = report.get(control, {})
                controls.append(make_verification_receipt(**{**fields, "receipt_id": str(uuid.uuid4()),
                    # Main-run counters do not measure the separate control runs.
                    "tests_collected": None, "tests_executed": None, "tests_passed": None,
                    "tests_failed": None, "tests_skipped": None,
                    "control_id": control, "control_expected": expected, "control_observed": result.get("observed", "UNKNOWN"),
                    "control_passed": result.get("expected") == expected and result.get("observed") == expected,
                    "requirement_ids": ("negative_control",) if control == "negative_control" else ()}))
        return VerificationExecution(main, {out.storage_uri: stdout, err.storage_uri: stderr}, tuple(controls))

    async def close(self):
        self._closed = True
        for call in tuple(self._active.values()):
            if call["processes"] is not None:
                call["processes"].request_stop()
        actions = tuple(self._actions.values())
        tasks = tuple(set(self._active) | set(actions))
        if tasks:
            await asyncio.wait(tasks, timeout=12)
        if any(not task.done() for task in actions):
            return False
        if any(not task.done() or not call["exited"].is_set() or not call["resolved"]
               for task, call in self._active.items()):
            return False
        for call in self._active.values():
            if call["processes"] is not None and not await call["processes"].confirm_stopped():
                return False
        return not self._blocked


@dataclass
class InspectorRuntime:
    service: InspectorService
    owner: TmosExecutionOwner


async def start_inspector_runtime(app, *, settings=None, sessions=None, manifest=None,
                                  retained_root=None, process_factory=SystemdProcesses, routing_reader=None):
    """Install only this lifespan's explicitly configured owned composition.

    Missing/unsupported native state leaves the existing 503 interface. This
    never substitutes an in-memory engine or initializes feature metadata.
    Test injection is an inert source seam and carries no operational authority.
    """
    if settings is None:
        from src.settings import load_settings
        settings = load_settings()
    requested = settings.get("canonicalInspector", {})
    if type(requested) is not dict or requested.get("enabled") is not True:
        return None
    if (getattr(app.state, "canonical_inspector_runtime", None) is not None
            or getattr(app.state, "canonical_inspector_service", None) is not None):
        raise InspectorUnavailable("inspector unavailable")
    try:
        if sessions is None:
            from core.database import SessionLocal
            sessions = SessionLocal
        store = AdmissionStore(sessions)
        store.check_schema()
        root = Path(retained_root if retained_root is not None else CANONICAL_INSPECTOR_RETAINED_ROOT)
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise InspectorUnavailable("inspector unavailable")
        if manifest is None:
            manifest = _manifest_file(CANONICAL_TMOS_MANIFEST_FILE)
        owner = TmosExecutionOwner(manifest, store=store,
            auth_manager=app.state.auth_manager, retained_root=root, process_factory=process_factory,
            routing_reader=routing_reader)
        service = InspectorService(store, retained_root=root,
            projector=partial(project_retained_routing_inspection, scoring_version="canonical-inspector-v1"),
            retention_parents=(root.parent,), restored_disclosure_generation=CANONICAL_TMOS_DISCLOSURE_GENERATION,
            work_owner=owner)
        runtime = InspectorRuntime(service, owner)
        app.state.canonical_inspector_runtime = runtime
        app.state.canonical_inspector_service = service
        return runtime
    except (OSError, ValueError, TypeError, KeyError):
        app.state.canonical_inspector_runtime_reason = "Inspector configuration or native state unavailable"
        return None


async def stop_inspector_runtime(app):
    runtime = getattr(app.state, "canonical_inspector_runtime", None)
    if runtime is None:
        return True
    if (type(runtime) is not InspectorRuntime
            or app.state.canonical_inspector_service not in (None, runtime.service)):
        raise InspectorUnavailable("inspector unavailable")
    app.state.canonical_inspector_service = None
    if not await runtime.owner.close():
        return False
    app.state.canonical_inspector_runtime = None
    return True
