"""Bounded collection uses only fake asynchronous streams and owned boundaries."""
import asyncio
import json

import pytest

from tests.test_canonical_inspector_admission import native_store as native_store, principal

from src.canonical_pilot_adapter import (
    CaptureBudget, PilotLimit, ProviderBudget, collect_bytes, collect_process,
    read_idempotency_key,
)


async def chunks(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_streamed_request_exact_shape_and_utf8_budget():
    assert await read_idempotency_key(chunks(b'{"idempotency_', b'key":"A"}')) == "A"
    for body in (b'{"idempotency_key":"a","idempotency_key":"b"}',
                 b'{"idempotency_key":"a","owner":"admin"}',
                 b'{"idempotency_key":NaN}', b'\xff', b' ' * 513,
                 json.dumps({"idempotency_key": "\u00e9" * 65}, ensure_ascii=False).encode()):
        with pytest.raises(PilotLimit):
            await read_idempotency_key(chunks(body))


@pytest.mark.asyncio
async def test_collect_checks_limit_before_append_and_deadline():
    assert await collect_bytes(chunks(b"ab", b"cd"), limit=4, seconds=1) == b"abcd"
    with pytest.raises(PilotLimit):
        await collect_bytes(chunks(b"ab", b"cde"), limit=4, seconds=1)

    async def slow():
        await asyncio.sleep(0.05)
        yield b"a"

    with pytest.raises(PilotLimit):
        await collect_bytes(slow(), limit=4, seconds=0.001)


class Boundary:
    qualified = True

    def __init__(self, stdout, stderr, *, stopped=True, lease=True):
        self.stdout, self.stderr = chunks(*stdout), chunks(*stderr)
        self.terminated = 0
        self.stopped = stopped
        self.lease = lease

    async def wait(self):
        return 0

    async def terminate_all(self):
        self.terminated += 1

    async def confirm_stopped(self):
        return self.stopped

    def lease_valid(self):
        return self.lease


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout,stderr", [([b"abcde"], []), ([], [b"abcde"]),
                                           ([b"abcd"], [b"abcd"])])
async def test_process_flood_terminates_whole_boundary(stdout, stderr):
    boundary = Boundary(stdout, stderr)
    with pytest.raises(PilotLimit):
        await collect_process(boundary, CaptureBudget(4, 4, 6, 1))
    assert boundary.terminated == 1


@pytest.mark.asyncio
async def test_missing_boundary_or_lease_never_uses_weaker_fallback():
    boundary = Boundary([b"ok"], [])
    boundary.qualified = False
    with pytest.raises(PilotLimit):
        await collect_process(boundary, CaptureBudget(4, 4, 6, 1))
    boundary.qualified = True
    boundary.lease = False
    with pytest.raises(PilotLimit):
        await collect_process(boundary, CaptureBudget(4, 4, 6, 1))
    assert boundary.terminated == 1


@pytest.mark.asyncio
async def test_process_success_drains_both_streams_and_confirms_exit():
    boundary = Boundary([b"ab"], [b"cd"])
    assert await collect_process(boundary, CaptureBudget(4, 4, 6, 1)) == (b"ab", b"cd", 0)
    assert boundary.terminated == 0
    boundary = Boundary([b"ab"], [], stopped=False)
    with pytest.raises(PilotLimit):
        await collect_process(boundary, CaptureBudget(4, 4, 6, 1))


@pytest.mark.asyncio
async def test_every_provider_response_obeys_shared_call_and_byte_budget():
    budget = ProviderBudget(observation_calls=1, generation_calls=1, total_bytes=5,
                            observation_bytes=4, generation_bytes=4,
                            observation_seconds=1, generation_seconds=1)
    assert await budget.collect("observation", chunks(b"abc")) == b"abc"
    with pytest.raises(PilotLimit):
        await budget.collect("observation", chunks(b"a"))
    with pytest.raises(PilotLimit):
        await budget.collect("generation", chunks(b"def"))


@pytest.mark.asyncio
async def test_cleanup_precedes_task_harvest_for_cancellation_resistant_stream():
    stopped = asyncio.Event()

    class StickyBoundary(Boundary):
        def __init__(self):
            super().__init__([], [b"overflow"])
            self.stdout = self.sticky()

        async def sticky(self):
            while not stopped.is_set():
                try:
                    await stopped.wait()
                except asyncio.CancelledError:
                    if not stopped.is_set():
                        continue
            yield b""

        async def terminate_all(self):
            self.terminated += 1
            stopped.set()

    boundary = StickyBoundary()
    with pytest.raises(PilotLimit):
        await asyncio.wait_for(collect_process(boundary, CaptureBudget(4, 4, 6, 1)), timeout=1)
    assert boundary.terminated == 1


@pytest.mark.asyncio
async def test_setup_aggregate_budget_is_not_reset_between_commands():
    budget = CaptureBudget(4, 4, 6, 1, total_seconds=2)
    assert await collect_process(Boundary([b"abc"], []), budget) == (b"abc", b"", 0)
    boundary = Boundary([b"abcd"], [])
    with pytest.raises(PilotLimit):
        await collect_process(boundary, budget)
    assert boundary.terminated == 1


@pytest.mark.asyncio
async def test_dishonest_length_header_does_not_bypass_body_cap():
    with pytest.raises(PilotLimit):
        await read_idempotency_key(chunks(b" " * 513), content_length="1")


@pytest.mark.asyncio
async def test_quiet_boundary_lease_loss_requests_prompt_whole_boundary_cleanup():
    stopped = asyncio.Event()

    class QuietBoundary(Boundary):
        def __init__(self):
            super().__init__([], [])
            self.stdout = self.quiet()

        async def quiet(self):
            await stopped.wait()
            yield b""

        async def wait(self):
            await stopped.wait()
            return 0

        async def terminate_all(self):
            self.terminated += 1
            stopped.set()

    boundary = QuietBoundary()

    async def lose_lease():
        await asyncio.sleep(0.01)
        boundary.lease = False

    loss = asyncio.create_task(lose_lease())
    with pytest.raises(PilotLimit):
        await asyncio.wait_for(collect_process(boundary, CaptureBudget(4, 4, 6, 10)), timeout=0.5)
    await loss
    assert boundary.terminated == 1


def test_fixed_pilot_descriptor_and_digest_are_not_caller_replaceable():
    from src.canonical_pilot_adapter import FixedSyntheticPilot, fixed_pilot_files, fixed_pilot_packet

    packet = fixed_pilot_packet()
    assert packet["packet_id"] == "synthetic-uppercase"
    assert packet["write_scope"] == ["src/thing.py"]
    files = fixed_pilot_files()
    assert files["src/thing.py"] == b"def transform(value): return value\n"
    assert b"ast.parse" in files["oracle.py"]
    packet["write_scope"].append("private.py")
    assert fixed_pilot_packet()["write_scope"] == ["src/thing.py"]
    with pytest.raises(TypeError):
        files["oracle.py"] = b"pass"
    assert FixedSyntheticPilot is not None


def synthetic_pilot(tmp_path):
    """Canonical source records and bounded fake owners, without host/model I/O."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    import hashlib
    from src import dispatch_routing as routing
    from src.attempt_receipt import artifact_ref, make_attempt_receipt, make_verification_receipt
    from src.canonical_pilot_adapter import FixedSyntheticPilot, PilotOwners, PilotObservations
    from src.dispatch_boundary import TargetEstate
    from src.local_targets import (CapabilityEvidence, ContextProfile, ModelIdentity,
                                   RuntimeIdentity, make_target_capability_receipt)
    from src.local_worker_loop import VerificationExecution
    from src.source_snapshot import _finalize
    from tests.test_local_worker_loop import _invocation, _worker, _verification

    now = datetime.now(timezone.utc)
    canonical = make_target_capability_receipt(
        host_id="synthetic-host", profile_id="recomputed", observed_at=now.isoformat(),
        runtime=RuntimeIdentity(runtime_kind="ollama", provider="ollama", version="fixture-v1",
                                backend="fixture-backend", endpoint_url="http://127.0.0.1:9999",
                                endpoint_type="ollama"),
        model=ModelIdentity(model_id="fixture-model", alias="fixture-model", digest="d" * 64),
        context=ContextProfile(configured_context=4096, configured_served_context=4096,
                               safe_working_context=4096, safe_context_source="synthetic source qualification"),
        capabilities=CapabilityEvidence(measured=(routing.CAP_TEXT_GENERATION,
            routing.CAP_SINGLE_TOOL_CALL, routing.CAP_EXACT_REFERENCE_SEMANTICS)),
        health="healthy", health_checked_at=now.isoformat(), qualification_ref="synthetic-only")
    profile = routing.make_target_profile(
        target_id=canonical.host_id, profile_id=canonical.profile_id, provider="ollama", host=canonical.host_id,
        runtime_kind="ollama", runtime_version="fixture-v1", backend="fixture-backend",
        model="fixture-model", model_digest="d" * 64, endpoint_url="http://127.0.0.1:9999",
        endpoint_type="ollama", locality=routing.LOCALITY_LOCAL, roles=frozenset({routing.ROLE_IMPLEMENTER}),
        tools=frozenset({"write_file"}), configured_context=4096, configured_served_context=4096,
        network_policy="offline", budget_class="dev")
    policy = routing.policy_snapshot(policy={"routingPolicyVersion": "fixed-synthetic-v1"})
    state = {"sources": {}, "workers": 0, "lease": True, "observe_boolean": False,
             "cleanup": True, "canonical": canonical}

    class Capabilities:
        def current(self, profile_id):
            return state["canonical"] if profile_id == canonical.profile_id else None

    async def setup(root, files, _capture_budget):
        digests = [(path, hashlib.sha256(raw).hexdigest()) for path, raw in files.items()]
        source = _finalize(dict(repo_root=str(root), repo_identity="synthetic-fixture", base_sha="a" * 40,
                                head_sha="a" * 40, branch="fixture", worktree=str(root), detached=False,
                                staged_paths=[], unstaged_paths=[], untracked_paths=[],
                                tracked_diff_digest=hashlib.sha256(b"").hexdigest(),
                                untracked_digest=hashlib.sha256(b"").hexdigest(),
                                relevant_digests=digests, truncated_paths=[], diff_truncated=False,
                                schema_version=1))
        state["sources"][root.name] = source.to_dict()
        return source

    def observe(bound):
        if state["observe_boolean"]:
            return True
        return PilotObservations(None if bound is None else _invocation(bound),
                                 (state["canonical"],), (), policy.policy_ref)

    class Lease:
        def valid(self):
            return state["lease"]

        async def release(self):
            state["released"] = state.get("released", 0) + 1
            return True

    async def lease():
        return Lease()

    class Cleanup:
        async def terminate_all(self):
            state["terminated"] = state.get("terminated", 0) + 1

        async def confirm_stopped(self):
            return state["cleanup"]

    def worker(bound, package, context, number, _budget, _provider_budget):
        state["workers"] += 1
        source = dict(package.source.core())
        source["unstaged_paths"] = ["src/thing.py"]
        source["tracked_diff_digest"] = "b" * 64
        source["relevant_digests"] = [(path, hashlib.sha256(b"def transform(value): return value.upper()\n").hexdigest()
                                       if path == "src/thing.py" else digest)
                                      for path, digest in package.source.relevant_digests]
        state["sources"][package.run_id] = _finalize(source).to_dict()
        result = _worker(bound, package, context, number)
        receipt = make_attempt_receipt(**{**result.receipt.__dict__,
            "model_version": bound.decision.selected_profile.model_digest,
            "generation_token": "synthetic-generation", "started_at": now.isoformat(),
            "ended_at": (now + timedelta(seconds=1)).isoformat(), "elapsed_s": 1.0})
        return replace(result, receipt=receipt)

    def verifier(bound, package, attempt, number, _collector):
        result = _verification(bound, package, attempt, number)
        stderr = artifact_ref("stderr", b"", storage_uri="memory://stderr")
        receipt = make_verification_receipt(**{**result.receipt.__dict__,
            "source_snapshot_digest": state["sources"][package.run_id]["snapshot_digest"],
            "stderr_ref": stderr, "started_at": now.isoformat(), "ended_at": now.isoformat(),
            "proof_vantage": "harness worktree", "control_id": "", "control_passed": None,
            "requirement_ids": tuple(requirement.requirement_id for requirement in package.evidence_requirements
                                     if requirement.requirement_id != "negative_control")})
        controls = tuple(make_verification_receipt(**{**receipt.__dict__, "receipt_id": control,
            "control_id": control, "control_passed": True, "control_expected": expected,
            "control_observed": expected,
            "requirement_ids": ("negative_control",) if control == "negative_control" else ()})
            for control, expected in (("positive_control", "PASS"), ("negative_control", "FAIL")))
        return VerificationExecution(receipt, {**result.artifacts, stderr.storage_uri: b""}, controls)

    owners = PilotOwners(TargetEstate(profiles=(profile,)), Capabilities(), policy, setup,
                         lambda run: state["sources"][run], observe, lease, worker, verifier,
                         Cleanup(), lambda: now)
    return FixedSyntheticPilot(owners, work_root=tmp_path / "work", retained_root=tmp_path / "retained"), state


@pytest.mark.asyncio
async def test_fixed_pilot_uses_real_canonical_worker_and_retention_with_fake_owners(tmp_path, native_store):
    import uuid
    from src.evidence_io import read_retained_evidence_bundle
    from src.canonical_inspector_service import READ_LIMITS

    pilot, state = synthetic_pilot(tmp_path)
    prepared = await pilot.prepare(str(uuid.uuid4()))
    store, _ = native_store
    claim = store.admit(principal(), "one", prepared)
    store.consume(claim, principal())
    with pytest.raises(PilotLimit):
        await pilot.execute(prepared)
    result = await pilot.execute(claim)
    payload, _ = read_retained_evidence_bundle(tmp_path / "retained", result.bundle_hash, **READ_LIMITS)
    assert payload["schema_version"] == 2
    assert payload["execution_package"]["packet_id"] == "synthetic-uppercase"
    assert payload["execution_package"]["run_id"] == prepared.run_id
    assert len(payload["attempt_receipts"]) == state["workers"] == 1
    assert result.attempt_hash == payload["attempt_receipts"][0]["receipt_hash"]
    with pytest.raises(PilotLimit):
        await pilot.execute(claim)


@pytest.mark.asyncio
async def test_canned_boolean_observation_and_lost_lease_refuse_before_worker(tmp_path, native_store):
    import uuid

    store, _ = native_store
    for index, key in enumerate(("observe_boolean", "lease")):
        pilot, state = synthetic_pilot(tmp_path)
        state[key] = key == "observe_boolean"
        prepared = await pilot.prepare(str(uuid.uuid4()))
        claim = store.admit(principal(), str(index), prepared)
        store.consume(claim, principal())
        with pytest.raises(PilotLimit):
            await pilot.execute(claim)
        assert state["workers"] == 0
        store.release_terminal(claim, cleanup_confirmed=True)


@pytest.mark.asyncio
async def test_cancelled_wrapper_is_not_worker_thread_exit_proof(tmp_path, native_store, monkeypatch):
    from dataclasses import replace
    import threading
    import uuid
    import src.canonical_pilot_adapter as adapter

    pilot, state = synthetic_pilot(tmp_path)
    started, released = threading.Event(), threading.Event()
    original_worker = pilot._owners.worker

    def blocked_worker(*args):
        started.set()
        if not released.wait(timeout=5):
            raise RuntimeError("isolated test worker timeout")
        return original_worker(*args)

    pilot._owners = replace(pilot._owners, worker=blocked_worker)
    original_to_thread = asyncio.to_thread
    actual_threads = []

    async def cancelled_wrapper(function, **kwargs):
        actual = asyncio.create_task(original_to_thread(function, **kwargs))
        actual_threads.append(actual)
        while not started.is_set():
            await asyncio.sleep(0.001)
        raise asyncio.CancelledError

    monkeypatch.setattr(adapter.asyncio, "to_thread", cancelled_wrapper)
    monkeypatch.setattr(adapter, "CANONICAL_PILOT_TERMINATION_CONFIRM_SECONDS", 0.01)
    prepared = await pilot.prepare(str(uuid.uuid4()))
    store, _ = native_store
    claim = store.admit(principal(), "one", prepared)
    store.consume(claim, principal())
    try:
        with pytest.raises(asyncio.CancelledError):
            await pilot.execute(claim)
        assert started.is_set() and not released.is_set()
        assert state.get("released", 0) == 0
    finally:
        released.set()
        await asyncio.wait_for(asyncio.gather(*actual_threads, return_exceptions=True), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["attempt", "verifier", "control"])
async def test_missing_returned_artifact_bytes_never_invoke_uri_loader(tmp_path, native_store, monkeypatch, missing):
    from dataclasses import replace
    import uuid
    from src.attempt_receipt import artifact_ref, make_attempt_receipt, make_verification_receipt
    import src.evidence_package as evidence
    import src.evidence_io as retained

    pilot, state = synthetic_pilot(tmp_path)
    worker, verifier = pilot._owners.worker, pilot._owners.verifier
    forbidden = artifact_ref("missing", b"private", storage_uri="file:///synthetic-protected")
    calls = []

    def forbid_loader(*_args, **_kwargs):
        calls.append("fallback")
        raise AssertionError("URI fallback must never run")

    monkeypatch.setattr(evidence, "default_artifact_loader", forbid_loader)
    monkeypatch.setattr(retained, "default_artifact_loader", forbid_loader)

    def malformed_worker(*args):
        result = worker(*args)
        row = make_attempt_receipt(**{**result.receipt.__dict__, "artifact_refs": (forbidden,)})
        return replace(result, receipt=row)

    def malformed_verifier(*args):
        result = verifier(*args)
        if missing == "verifier":
            row = make_verification_receipt(**{**result.receipt.__dict__, "stdout_ref": forbidden})
            return replace(result, receipt=row)
        control = make_verification_receipt(**{**result.controls[0].__dict__, "stdout_ref": forbidden})
        return replace(result, controls=(control, *result.controls[1:]))

    pilot._owners = replace(pilot._owners,
                            worker=malformed_worker if missing == "attempt" else worker,
                            verifier=malformed_verifier if missing != "attempt" else verifier)
    prepared = await pilot.prepare(str(uuid.uuid4()))
    store, _ = native_store
    claim = store.admit(principal(), "one", prepared)
    store.consume(claim, principal())
    try:
        result = await pilot.execute(claim)
    except PilotLimit:
        result = None
    if result is not None:
        # The unchanged coordinator may retain a bounded failed attempt after
        # refusing malformed verifier output. It cannot invent verification.
        from src.canonical_inspector_service import READ_LIMITS
        payload, artifacts = retained.read_retained_evidence_bundle(
            tmp_path / "retained", result.bundle_hash, **READ_LIMITS)
        validation = evidence.validate_evidence_package(payload, artifact_extensions=artifacts)
        assert validation.state != "VERIFIED"
    assert calls == []
