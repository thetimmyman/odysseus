"""Bounded source seams for one public synthetic pilot, with no live adapter.

The supplied asynchronous streams and whole-descendant supervisor are trusted
in-process dependencies. A qualified real cgroup, broker transport, resident
runtime observations and provider cancellation are deliberately not supplied.
No subprocess, HTTP client, model loading or environment lease shortcut lives
here. A true fake-boundary flag in a test is not operational qualification.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import time
import threading
from types import MappingProxyType
import uuid

from src.constants import (
    CANONICAL_PILOT_BODY_BYTES, CANONICAL_PILOT_BODY_SECONDS,
    CANONICAL_PILOT_GENERATION_BYTES, CANONICAL_PILOT_GENERATION_CALLS,
    CANONICAL_PILOT_KEY_BYTES, CANONICAL_PILOT_OBSERVATION_BYTES,
    CANONICAL_PILOT_LEASE_POLL_SECONDS,
    CANONICAL_PILOT_OBSERVATION_CALLS, CANONICAL_PILOT_OBSERVATION_SECONDS,
    CANONICAL_PILOT_PROVIDER_TOTAL_BYTES, CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS,
    CANONICAL_PILOT_TERMINATION_GRACE_SECONDS, CANONICAL_PILOT_WORKER_SECONDS,
    CANONICAL_PILOT_CONTEXT_TOKENS, CANONICAL_PILOT_ID, CANONICAL_PILOT_OUTPUT_TOKENS,
    CANONICAL_PILOT_SETUP_STDOUT_BYTES, CANONICAL_PILOT_SETUP_STDERR_BYTES,
    CANONICAL_PILOT_SETUP_TOTAL_BYTES, CANONICAL_PILOT_SETUP_SECONDS,
    CANONICAL_PILOT_SETUP_TOTAL_SECONDS, CANONICAL_PILOT_SCRATCH_BYTES,
    CANONICAL_PILOT_BROKER_WAIT_SECONDS,
)


class PilotLimit(ValueError):
    """Bounded generic refusal; never includes captured content."""


def _positive(value):
    if type(value) not in (int, float) or not 0 < value < float("inf"):
        raise PilotLimit("pilot unavailable")
    return value


async def collect_bytes(stream, *, limit, seconds):
    """Incremental cap before buffering/decoding, independent of length headers."""
    if type(limit) is not int or limit <= 0:
        raise PilotLimit("pilot unavailable")
    _positive(seconds)
    output = bytearray()
    try:
        async with asyncio.timeout(seconds):
            async for chunk in stream:
                if type(chunk) is not bytes or len(chunk) > limit - len(output):
                    raise PilotLimit("pilot unavailable")
                output.extend(chunk)
    except (TimeoutError, OSError):
        raise PilotLimit("pilot unavailable") from None
    return bytes(output)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotLimit("pilot unavailable")
        result[key] = value
    return result


def _nonfinite(_):
    raise PilotLimit("pilot unavailable")


async def read_idempotency_key(stream, *, content_length=None):
    if content_length is not None:
        try:
            if not content_length.isdecimal() or int(content_length) > CANONICAL_PILOT_BODY_BYTES:
                raise PilotLimit("pilot unavailable")
        except (AttributeError, ValueError):
            raise PilotLimit("pilot unavailable") from None
    raw = await collect_bytes(stream, limit=CANONICAL_PILOT_BODY_BYTES,
                              seconds=CANONICAL_PILOT_BODY_SECONDS)
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if (type(body) is not dict or set(body) != {"idempotency_key"}
                or type(body["idempotency_key"]) is not str or not body["idempotency_key"]
                or len(body["idempotency_key"].encode("utf-8")) > CANONICAL_PILOT_KEY_BYTES):
            raise PilotLimit("pilot unavailable")
        return body["idempotency_key"]
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PilotLimit("pilot unavailable") from None


@dataclass
class CaptureBudget:
    stdout_bytes: int
    stderr_bytes: int
    total_bytes: int
    seconds: float
    total_seconds: float | None = None
    termination_grace_seconds: float = CANONICAL_PILOT_TERMINATION_GRACE_SECONDS
    termination_confirm_seconds: float = CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS
    used_bytes: int = field(default=0, init=False)
    _deadline: float | None = field(default=None, init=False)
    _failed: bool = field(default=False, init=False)

    def __post_init__(self):
        for value in (self.stdout_bytes, self.stderr_bytes, self.total_bytes):
            if type(value) is not int or value <= 0:
                raise PilotLimit("pilot unavailable")
        _positive(self.seconds)
        if self.total_seconds is not None:
            _positive(self.total_seconds)
        if (_positive(self.termination_grace_seconds) > 3
                or _positive(self.termination_confirm_seconds) > 5):
            raise PilotLimit("pilot unavailable")


async def _bounded_wait(awaitable, seconds):
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task not in done:
        task.cancel()
        raise TimeoutError
    return task.result()


async def _stop(boundary, *, grace_seconds=CANONICAL_PILOT_TERMINATION_GRACE_SECONDS,
                confirm_seconds=CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS):
    try:
        await _bounded_wait(boundary.terminate_all(), grace_seconds)
        return await _bounded_wait(boundary.confirm_stopped(), confirm_seconds) is True
    except Exception:
        return False


async def collect_process(boundary, budget: CaptureBudget):
    """Drain both owned streams concurrently; terminate all on any uncertainty.

    A result requires confirmed whole-boundary exit. Failure carries no result
    or terminal proof; the caller must keep an unproven dispatch slot reserved.
    The shared budget charges setup aggregate bytes across multiple calls.
    """
    if getattr(boundary, "qualified", False) is not True or budget._failed:
        raise PilotLimit("pilot unavailable")
    moment = time.monotonic()
    if budget._deadline is None:
        budget._deadline = moment + (budget.total_seconds or budget.seconds)
    remaining = min(budget.seconds, budget._deadline - moment)
    tasks = []

    async def drain(stream, limit):
        output = bytearray()
        async for chunk in stream:
            if (boundary.lease_valid() is not True or type(chunk) is not bytes
                    or len(chunk) > limit - len(output)
                    or len(chunk) > budget.total_bytes - budget.used_bytes):
                raise PilotLimit("pilot unavailable")
            budget.used_bytes += len(chunk)
            output.extend(chunk)
        return bytes(output)

    async def watch_lease():
        while True:
            if boundary.lease_valid() is not True:
                raise PilotLimit("pilot unavailable")
            await asyncio.sleep(CANONICAL_PILOT_LEASE_POLL_SECONDS)

    try:
        if remaining <= 0 or boundary.lease_valid() is not True:
            raise PilotLimit("pilot unavailable")
        tasks = [asyncio.create_task(drain(boundary.stdout, budget.stdout_bytes)),
                 asyncio.create_task(drain(boundary.stderr, budget.stderr_bytes)),
                 asyncio.create_task(boundary.wait())]
        collected = asyncio.ensure_future(asyncio.gather(*tasks))
        watcher = asyncio.create_task(watch_lease())
        tasks.extend([collected, watcher])
        done, _ = await asyncio.wait({collected, watcher}, timeout=remaining,
                                     return_when=asyncio.FIRST_COMPLETED)
        if watcher in done or collected not in done or collected.exception() is not None:
            raise PilotLimit("pilot unavailable")
        stdout, stderr, exit_code = collected.result()
        watcher.cancel()
        await asyncio.wait({watcher}, timeout=CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS)
        if (type(exit_code) is not int or boundary.lease_valid() is not True
                or await _bounded_wait(boundary.confirm_stopped(), min(
                    budget.termination_confirm_seconds,
                    max(0, budget._deadline - time.monotonic()))) is not True):
            raise PilotLimit("pilot unavailable")
        return stdout, stderr, exit_code
    except BaseException as failure:
        budget._failed = True
        # Stop the owning boundary FIRST: draining/cancellation can itself be
        # uncooperative. Never wait indefinitely for task cancellation to finish.
        await _stop(boundary, grace_seconds=budget.termination_grace_seconds,
                    confirm_seconds=budget.termination_confirm_seconds)
        for task in tasks:
            task.cancel()
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS)
            for task in done:
                if not task.cancelled():
                    task.exception()
        if isinstance(failure, asyncio.CancelledError):
            raise
        raise PilotLimit("pilot unavailable") from None


class ProviderBudget:
    """Every response is bounded before JSON decoding; no endpoint selection."""

    def __init__(self, *, observation_calls=CANONICAL_PILOT_OBSERVATION_CALLS,
                 generation_calls=CANONICAL_PILOT_GENERATION_CALLS,
                 total_bytes=CANONICAL_PILOT_PROVIDER_TOTAL_BYTES,
                 observation_bytes=CANONICAL_PILOT_OBSERVATION_BYTES,
                 generation_bytes=CANONICAL_PILOT_GENERATION_BYTES,
                 observation_seconds=CANONICAL_PILOT_OBSERVATION_SECONDS,
                 generation_seconds=CANONICAL_PILOT_WORKER_SECONDS):
        values = (observation_calls, generation_calls, total_bytes, observation_bytes, generation_bytes)
        if any(type(value) is not int or value <= 0 for value in values):
            raise PilotLimit("pilot unavailable")
        self._calls = {"observation": observation_calls, "generation": generation_calls}
        self._bytes = {"observation": observation_bytes, "generation": generation_bytes}
        self._seconds = {"observation": _positive(observation_seconds),
                         "generation": _positive(generation_seconds)}
        self._remaining_bytes = total_bytes
        self._deadline = time.monotonic() + CANONICAL_PILOT_WORKER_SECONDS
        self._failed = False

    async def collect(self, kind, stream):
        if self._failed or kind not in self._calls or self._calls[kind] <= 0:
            self._failed = True
            raise PilotLimit("pilot unavailable")
        self._calls[kind] -= 1
        output = bytearray()
        try:
            remaining = min(self._seconds[kind], self._deadline - time.monotonic())
            if remaining <= 0:
                raise PilotLimit("pilot unavailable")
            async with asyncio.timeout(remaining):
                async for chunk in stream:
                    if (type(chunk) is not bytes or len(chunk) > self._bytes[kind] - len(output)
                            or len(chunk) > self._remaining_bytes):
                        raise PilotLimit("pilot unavailable")
                    self._remaining_bytes -= len(chunk)
                    output.extend(chunk)
            return bytes(output)
        except (TimeoutError, OSError, PilotLimit):
            self._failed = True
            raise PilotLimit("pilot unavailable") from None


_BASELINE = b"def transform(value): return value\n"
_ORACLE = b'''import ast, json, pathlib
def discriminates(source):
    tree = ast.parse(source)
    if not (len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)):
        raise ValueError("unsupported source shape")
    node = tree.body[0]
    if not (node.name == "transform" and not node.args.posonlyargs
            and len(node.args.args) == 1 and node.args.args[0].arg == "value"):
        raise ValueError("unsupported source shape")
    if not (not node.decorator_list and not node.args.defaults and not node.args.kw_defaults):
        raise ValueError("unsupported source shape")
    if not (not node.args.vararg and not node.args.kwarg and not node.args.kwonlyargs):
        raise ValueError("unsupported source shape")
    if not (len(node.body) == 1 and isinstance(node.body[0], ast.Return)):
        raise ValueError("unsupported source shape")
    call = node.body[0].value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
        raise ValueError("unsupported source shape")
    if not (isinstance(call.func.value, ast.Name) and call.func.value.id == "value"):
        raise ValueError("unsupported source shape")
    if not (call.func.attr in {"upper", "lower"} and not call.args and not call.keywords):
        raise ValueError("unsupported source shape")
    # Worker text is inert; execute only this fixed reconstruction.
    namespace = {}
    exec("def transform(value): return value." + call.func.attr + "()", namespace)
    return all(namespace["transform"](a) == b for a,b in [("Hello","HELLO"),("a","A"),("","")])
try:
    result = {"candidate": discriminates(pathlib.Path("src/thing.py").read_text()),
              "positive_control": discriminates("def transform(value): return value.upper()"),
              "negative_control": not discriminates("def transform(value): return value.lower()")}
    print(json.dumps(result))
    raise SystemExit(0 if all(result.values()) else 1)
except (AssertionError, SyntaxError, ValueError):
    print(json.dumps({"candidate":False,"positive_control":False,"negative_control":False}))
    raise SystemExit(1)
'''


def fixed_pilot_files():
    return MappingProxyType({"src/thing.py": _BASELINE, "oracle.py": _ORACLE})


def fixed_pilot_packet():
    return {"packet_id": CANONICAL_PILOT_ID,
            "objective": "Implement transform: uppercase every input string",
            "contract": "def transform(value: str) -> str; return value.upper(). Output only Python source.",
            "write_scope": ["src/thing.py"],
            "interface": [{"name": "value", "required": True, "type_hint": "str",
                           "semantics": "return uppercase of the input"}],
            "test_command": "python oracle.py",
            "acceptance_criteria": ["uppercase mixed-case and empty strings"],
            "negative_control": "lowercase mutant fails",
            "stop_conditions": ["current identity, lease or bounded capture unavailable"]}


@dataclass(frozen=True)
class PilotObservations:
    """Owner-supplied canonical records and measured invocation, never booleans."""
    invocation: object
    capability_receipts: tuple
    capacity_receipts: tuple
    policy_ref: str


@dataclass(frozen=True)
class PilotOwners:
    """Internal fake dependencies; no installed/live implementation is provided.

    Source setup/snapshot callbacks must enforce the fixed files/private root
    and bounded git capture through their independently owned hard boundary.
    Worker/verifier callbacks receive the collectors and frozen budgets. This
    source interface does not qualify their capture, cancellation or durability.
    """
    estate: object
    capability_store: object
    policy: object
    setup_source: object
    current_source: object
    observe: object
    lease: object
    worker: object
    verifier: object
    cleanup_boundary: object
    now: object = lambda: datetime.now(timezone.utc)


@dataclass(frozen=True)
class PilotExecution:
    bundle_hash: str
    attempt_hash: str
    cleanup_confirmed: bool


class FixedSyntheticPilot:
    """Concrete fixed canonical callgraph with fake owner adapters only.

    This class is never constructed by application setup. It has no discovery,
    provider/broker client, credential, listener, systemd runner or own-slot
    fallback. Missing owners refuse; their source-test injection is not a live
    capability or proof of operational qualification.
    """

    def __init__(self, owners: PilotOwners, *, work_root, retained_root):
        from src.dispatch_boundary import TargetEstate
        if (type(owners) is not PilotOwners or not isinstance(owners.estate, TargetEstate)
                or len(owners.estate.profiles) != 1
                or any(not callable(getattr(owners, key)) for key in (
                    "setup_source", "current_source", "observe", "lease", "worker",
                    "verifier", "now"))
                or any(not callable(getattr(owners.cleanup_boundary, key, None))
                       for key in ("terminate_all", "confirm_stopped"))):
            raise PilotLimit("pilot unavailable")
        self._owners = owners
        self._work_root, self._retained_root = Path(work_root), Path(retained_root)
        self._prepared = {}
        frozen = {"packet": fixed_pilot_packet(),
                  "files": {path: hashlib.sha256(raw).hexdigest() for path, raw in fixed_pilot_files().items()},
                  "profile_id": owners.estate.profiles[0].profile_id,
                  "policy_ref": owners.policy.policy_ref,
                  "max_attempts": 1, "context_tokens": CANONICAL_PILOT_CONTEXT_TOKENS,
                  "output_tokens": CANONICAL_PILOT_OUTPUT_TOKENS,
                  "time_seconds": CANONICAL_PILOT_WORKER_SECONDS}
        self._config_digest = hashlib.sha256(json.dumps(
            frozen, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @property
    def config_digest(self):
        return self._config_digest

    async def prepare(self, run_id):
        from src.canonical_inspector_admission import PreparedPilot
        from src.execution_package import Budgets, VerificationPlan, build_execution_package
        from src.source_snapshot import SourceSnapshotIdentity, snapshot_digest_is_valid
        try:
            if str(uuid.UUID(run_id)) != run_id:
                raise PilotLimit("pilot unavailable")
        except (ValueError, TypeError, AttributeError):
            raise PilotLimit("pilot unavailable") from None
        setup_budget = CaptureBudget(CANONICAL_PILOT_SETUP_STDOUT_BYTES,
                                     CANONICAL_PILOT_SETUP_STDERR_BYTES,
                                     CANONICAL_PILOT_SETUP_TOTAL_BYTES,
                                     CANONICAL_PILOT_SETUP_SECONDS,
                                     total_seconds=CANONICAL_PILOT_SETUP_TOTAL_SECONDS)
        try:
            source = await _bounded_wait(self._owners.setup_source(
                self._work_root / run_id, fixed_pilot_files(), setup_budget), CANONICAL_PILOT_SETUP_TOTAL_SECONDS)
        except BaseException:
            await _stop(self._owners.cleanup_boundary)
            raise PilotLimit("pilot unavailable") from None
        if (not isinstance(source, SourceSnapshotIdentity) or not source.is_clean or not source.is_complete
                or not snapshot_digest_is_valid(source.to_dict())
                or Path(source.repo_root) != self._work_root / run_id
                or any(source.relevant_digest(path) != hashlib.sha256(raw).hexdigest()
                       for path, raw in fixed_pilot_files().items())):
            raise PilotLimit("pilot unavailable")
        plan = VerificationPlan(verifier_id="oracle.py", command="python oracle.py",
                                verifier_paths=("oracle.py",),
                                verifier_digests=(("oracle.py", hashlib.sha256(_ORACLE).hexdigest()),),
                                positive_control="reference uppercase passes",
                                negative_control="lowercase mutant fails")
        package = build_execution_package(
            fixed_pilot_packet(), source=source, verification=plan, run_id=run_id,
            allowed_tools=("write_file",), network_policy="offline",
            budgets=Budgets(max_attempts=1, context_tokens=CANONICAL_PILOT_CONTEXT_TOKENS,
                            output_tokens=CANONICAL_PILOT_OUTPUT_TOKENS,
                            time_seconds=CANONICAL_PILOT_WORKER_SECONDS))
        prepared = PreparedPilot(run_id, CANONICAL_PILOT_ID, package.package_hash, self.config_digest)
        self._prepared[run_id] = (prepared, package)
        return prepared

    def _observations(self, bound, package):
        from src.dispatch_boundary import InvocationIdentity, verify_invocation
        from src.local_targets import TargetCapabilityReceipt, target_capability_receipt_hash_is_valid
        from src.provider_capacity import ProviderCapacityReceipt, capacity_receipt_hash_is_valid
        observed = self._owners.observe(bound)
        if (type(observed) is not PilotObservations
                or not isinstance(observed.invocation, InvocationIdentity)
                or type(observed.capability_receipts) is not tuple
                or type(observed.capacity_receipts) is not tuple
                or observed.policy_ref != bound.policy.policy_ref):
            raise PilotLimit("pilot unavailable")
        now = self._owners.now()
        verify_invocation(bound, invocation=observed.invocation)
        caps = {}
        for receipt in observed.capability_receipts:
            if (not isinstance(receipt, TargetCapabilityReceipt)
                    or not target_capability_receipt_hash_is_valid(receipt.to_dict())
                    or receipt.qualification_state(now=now) != "valid"
                    or receipt.receipt_hash in caps):
                raise PilotLimit("pilot unavailable")
            current = self._owners.capability_store.current(receipt.profile_id)
            if current is None or current.receipt_hash != receipt.receipt_hash:
                raise PilotLimit("pilot unavailable")
            caps[receipt.receipt_hash] = True  # Derived from records checked above.
        if set(caps) != set(bound.decision.capability_receipt_refs):
            raise PilotLimit("pilot unavailable")
        capacities = {}
        for receipt in observed.capacity_receipts:
            if (not isinstance(receipt, ProviderCapacityReceipt)
                    or not capacity_receipt_hash_is_valid(receipt.to_dict())
                    or not receipt.is_operationally_available(now=now)
                    or receipt.ref in capacities):
                raise PilotLimit("pilot unavailable")
            capacities[receipt.ref] = receipt
        expected = set(bound.decision.capacity_receipt_refs)
        if set(capacities) != expected:
            raise PilotLimit("pilot unavailable")
        pin = bound.pin_for(bound.decision.selected_profile.profile_id)
        # Runtime values come from the observed invocation after exact pin
        # validation, rather than replacing observations with the desired pin.
        runtime_names = ("provider", "runtime_kind", "runtime_version", "runtime_commit",
                         "runtime_image_digest", "backend", "backend_version", "model",
                         "model_digest", "runtime_options", "configured_context", "configured_served_context")
        facts = dict(capability_receipts=caps, capacity_fresh=bool(capacities),
                     capacity_receipt_refs=tuple(capacities), policy_ref=observed.policy_ref,
                     target_id=pin["target_id"], profile_id=observed.invocation.profile_id,
                     endpoint_identity=pin["endpoint_identity"],
                     runtime_identity={key: getattr(observed.invocation, key) for key in runtime_names},
                     granted_tools=tuple(bound.decision.granted_tools),
                     granted_write_scope=tuple(bound.decision.granted_write_scope),
                     granted_read_scope=tuple(bound.decision.granted_read_scope),
                     network_policy=bound.decision.network_policy,
                     execution_package_hash=package.package_hash,
                     source_digest=package.source.snapshot_digest, interface_digest=package.interface_digest)
        return observed, facts

    def discard(self, prepared):
        pair = self._prepared.get(prepared.run_id)
        if pair is not None and pair[0] is prepared:
            del self._prepared[prepared.run_id]

    async def execute(self, claim):
        from src.canonical_inspector_admission import _DispatchClaim, InspectorUnavailable
        from src.dispatch_boundary import resolve_from_estate, verify_invocation
        from src.dispatch_routing import ROLE_IMPLEMENTER, RoutingRequest
        from src.evidence_io import retain_evidence_bundle
        from src.local_worker_loop import run_local_worker_loop
        if type(claim) is not _DispatchClaim:
            raise PilotLimit("pilot unavailable")
        try:
            prepared = claim._store._begin_execution(claim)
        except InspectorUnavailable:
            raise PilotLimit("pilot unavailable") from None
        pair = self._prepared.pop(prepared.run_id, None)
        if pair is None or pair[0] is not prepared or prepared.config_digest != self.config_digest:
            raise PilotLimit("pilot unavailable")
        package = pair[1]
        request = RoutingRequest(domain="general_swe", role=ROLE_IMPLEMENTER,
                                 run_id=package.run_id, packet_id=package.packet_id,
                                 execution_package_hash=package.package_hash,
                                 required_tools=("write_file",), network_policy="offline",
                                 write_scope=("src/thing.py",))
        # No context-manager auto-release: uncertain owned work retains its
        # capacity reservation. The only transport here is an injected fake.
        lease, runner, result, failure = None, None, None, None
        stopped = [False]
        thread_exited = threading.Event()
        cleanup_confirmed = False
        try:
            lease = await _bounded_wait(self._owners.lease(), CANONICAL_PILOT_BROKER_WAIT_SECONDS)
            if lease.valid() is not True:
                raise PilotLimit("pilot unavailable")
            initial = self._owners.observe(None)
            if type(initial) is not PilotObservations:
                raise PilotLimit("pilot unavailable")
            bound = resolve_from_estate(
                self._owners.estate, request, capability_store=self._owners.capability_store,
                capacity_receipts=initial.capacity_receipts, policy=self._owners.policy, now=self._owners.now())
            observed, _ = self._observations(bound, package)
            verify_invocation(bound, invocation=observed.invocation)
            provider_budget = ProviderBudget()

            def guard():
                if stopped[0] or lease.valid() is not True:
                    raise PilotLimit("pilot unavailable")

            def facts(dispatch, _number):
                guard()
                return self._observations(dispatch, package)[1]

            def worker(dispatch, context, number, output_tokens):
                guard()
                value = self._owners.worker(dispatch, package, context, number,
                                             output_tokens, provider_budget)
                guard()
                self._bounded_artifacts(value.artifacts)
                self._preflight_receipts((value.receipt,), value.artifacts, worker=True)
                return value

            def verifier(dispatch, attempt, number):
                guard()
                value = self._owners.verifier(dispatch, package, attempt, number, collect_process)
                guard()
                self._bounded_artifacts(value.artifacts)
                self._preflight_receipts((value.receipt, *value.controls), value.artifacts, worker=False)
                return value

            def current_source():
                guard()
                return self._owners.current_source(package.run_id)

            def run_canonical():
                try:
                    return run_local_worker_loop(
                        execution_package=package, standing_dispatch=bound,
                        current_facts=facts,
                        invocation=lambda dispatch: self._observations(dispatch, package)[0].invocation,
                        execute_worker=worker, execute_verifier=verifier,
                        current_source=current_source, advisory_budget=0)
                finally:
                    # A cancelled asyncio wrapper is not evidence that its
                    # underlying synchronous callback thread has exited.
                    thread_exited.set()

            runner = asyncio.create_task(asyncio.to_thread(run_canonical))
            done, _ = await asyncio.wait({runner}, timeout=CANONICAL_PILOT_WORKER_SECONDS)
            if runner not in done:
                raise PilotLimit("pilot unavailable")
            result = runner.result()
            if result.evidence_package is None or len(result.attempts) != 1 or lease.valid() is not True:
                raise PilotLimit("pilot unavailable")
        except BaseException as error:
            failure = error
        finally:
            stopped[0] = True
            cleanup_confirmed = await _stop(self._owners.cleanup_boundary)
            if runner is not None:
                done, _ = await asyncio.wait({runner}, timeout=CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS)
                cleanup_confirmed = (cleanup_confirmed and runner in done
                                     and not runner.cancelled() and thread_exited.is_set())
                if runner in done and not runner.cancelled():
                    runner.exception()
            if cleanup_confirmed and lease is not None:
                try:
                    cleanup_confirmed = await _bounded_wait(
                        lease.release(), CANONICAL_PILOT_TERMINATION_GRACE_SECONDS) is True
                except Exception:
                    cleanup_confirmed = False
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        if failure is not None or not cleanup_confirmed or result is None:
            raise PilotLimit("pilot unavailable")
        self._bounded_artifacts(result.artifacts)
        from src.evidence_package import _iter_artifact_refs
        self._preflight_refs([ref for _label, ref in _iter_artifact_refs(result.evidence_package)],
                             result.artifacts)
        if len(json.dumps(result.evidence_package, allow_nan=False).encode()) > CANONICAL_PILOT_SCRATCH_BYTES:
            raise PilotLimit("pilot unavailable")
        path = retain_evidence_bundle(self._retained_root, result.evidence_package, artifacts=result.artifacts)
        return PilotExecution(path.stem, result.attempts[0].receipt_hash, True)

    @staticmethod
    def _bounded_artifacts(artifacts):
        if (not isinstance(artifacts, dict) or len(artifacts) > 64
                or any(type(raw) is not bytes for raw in artifacts.values())
                or sum(len(raw) for raw in artifacts.values()) > CANONICAL_PILOT_SCRATCH_BYTES):
            raise PilotLimit("pilot unavailable")

    @staticmethod
    def _preflight_receipts(receipts, artifacts, *, worker):
        from src.attempt_receipt import AttemptReceipt, VerificationReceipt
        refs = []
        for receipt in receipts:
            expected = AttemptReceipt if worker else VerificationReceipt
            if not isinstance(receipt, expected):
                raise PilotLimit("pilot unavailable")
            if worker:
                values = [receipt.rendered_context_ref, receipt.output_ref, *receipt.artifact_refs]
            else:
                values = [receipt.stdout_ref, receipt.stderr_ref]
            refs.extend(ref.to_dict() for ref in values if ref is not None)
        FixedSyntheticPilot._preflight_refs(refs, artifacts)

    @staticmethod
    def _preflight_refs(refs, artifacts):
        """No URI loader can run in the unchanged canonical loop or retainer."""
        FixedSyntheticPilot._bounded_artifacts(artifacts)
        expected = {}
        for ref in refs:
            uri, digest, size = ref.get("storage_uri"), ref.get("sha256"), ref.get("size")
            if (type(uri) is not str or not uri or len(uri.encode()) > 4096
                    or type(digest) is not str or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                    or type(size) is not int or not 0 <= size <= CANONICAL_PILOT_SCRATCH_BYTES
                    or uri not in artifacts or len(artifacts[uri]) != size
                    or hashlib.sha256(artifacts[uri]).hexdigest() != digest):
                raise PilotLimit("pilot unavailable")
            if expected.setdefault(uri, (digest, size)) != (digest, size):
                raise PilotLimit("pilot unavailable")
        if set(expected) != set(artifacts):
            raise PilotLimit("pilot unavailable")
