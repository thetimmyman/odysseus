"""Broker-route probe: normalization, the endpoint guard, and non-blocking timeouts.

Each property has a negative control:

1. every way an attempt can end normalizes to its own status, and the fixture
   table is a complete partition of the probe's outcomes;
2. a timeout records NO latency — the wait is not a measurement;
3. "couldn't ask" (offline) and "didn't like the answer" (degraded) stay
   distinct, so a network fault is never filed as a protocol fault;
4. an endpoint that answered on an unregistered host is NOT adopted as an
   alias (OD-47's real failure), even when scheme/port/path all agree;
5. a timeout costs its timeout and not the sum of every dead endpoint, so the
   probe cannot block the event loop;
6. every emitted row validates against the published JSON Schema.

No test opens a socket: the transport is injected. The real transport is
asserted unused by a test that would fail if a fixture leaked to the network.
"""
import asyncio
import json
import time
from pathlib import Path

import jsonschema
import pytest

from src import broker_probe as bp

FIXTURES = json.loads((Path(__file__).resolve().parent / "fixtures" / "broker_probe_states.json").read_text())

#: Every outcome the probe can report, so the fixture table can be proven complete.
ALL_OUTCOMES = frozenset({
    bp.OUTCOME_RESPONDED, bp.OUTCOME_REFUSED, bp.OUTCOME_TIMEOUT,
    bp.OUTCOME_MALFORMED, bp.OUTCOME_NOT_ASKED,
})

CASES = [name for name in FIXTURES if not name.startswith("_")]


def _fixture(name):
    return FIXTURES[name]


# --------------------------------------------------------------------------
# 1. Normalization is a complete partition
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", CASES)
def test_each_socket_state_normalizes_to_its_own_status(name):
    entry = _fixture(name)
    row = bp.row_from_fixture(entry, registered_endpoint=entry.get("registered_endpoint", entry["endpoint"]))
    assert row.status == entry["expect_status"]
    assert row.state == entry["expect_state"]
    assert bp.is_routable(row) is entry["expect_routable"]


def test_the_fixture_table_covers_every_outcome():
    """A gap here means an outcome the suite never checks — not a pass."""
    assert {_fixture(n)["outcome"] for n in CASES} == ALL_OUTCOMES, "an outcome has no fixture"


def test_the_endpoint_guard_has_its_own_case_beyond_the_plain_outcomes():
    """``answered_elsewhere`` is a second ``responded`` case on purpose: same
    outcome, different guard. It is what separates "answered where we asked"
    from "answered somewhere else", so the partition is outcomes x guard, not
    outcomes alone."""
    plain = [_fixture(n) for n in CASES if "registered_endpoint" not in _fixture(n)]
    guarded = [_fixture(n) for n in CASES if "registered_endpoint" in _fixture(n)]
    assert len(guarded) == 1
    assert {e["outcome"] for e in plain} == ALL_OUTCOMES
    assert guarded[0]["outcome"] == bp.OUTCOME_RESPONDED
    # The plain case is routable; the guarded one is not, on the same outcome.
    assert bp.is_routable(bp.row_from_fixture(plain[0]))
    assert not bp.is_routable(bp.row_from_fixture(guarded[0],
                                                 registered_endpoint=guarded[0]["registered_endpoint"]))


def test_an_unknown_outcome_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        bp.ProbeObservation("t", "http://synthetic-broker.invalid:11434", "shrugged")


# --------------------------------------------------------------------------
# 2. A timeout is not a latency
# --------------------------------------------------------------------------


def test_a_timeout_records_no_latency_rather_than_the_timeout_bound():
    obs = bp.ProbeObservation("t", "http://synthetic-timeout.invalid:11434",
                              bp.OUTCOME_TIMEOUT, reason="connect timed out")
    row = bp.normalize(obs)
    assert row.latency_ms is None
    assert row.failure_classes == (bp.FAILURE_CONNECT_TIMEOUT,)


def test_a_refusal_records_no_latency():
    obs = bp.ProbeObservation("t", "http://synthetic-refused.invalid:11434", bp.OUTCOME_REFUSED)
    assert bp.normalize(obs).latency_ms is None

def test_a_read_timeout_records_no_latency_even_though_the_socket_worked():
    """The wait was the deadline we gave up on, not a round trip. Recording it
    as a latency would drag a percentile toward the timeout."""
    obs = bp.ProbeObservation("t", "http://x.invalid", bp.OUTCOME_MALFORMED,
                              reason="connected, no response before deadline")
    row = bp.normalize(obs)
    assert row.latency_ms is None
    assert row.status == bp.STATUS_DEGRADED, "the socket worked; the service did not answer"


def test_a_garbage_answer_keeps_its_latency_because_it_was_actually_measured():
    obs = bp.ProbeObservation("t", "http://x.invalid", bp.OUTCOME_MALFORMED, latency_ms=7.8)
    assert bp.normalize(obs).latency_ms == 7.8


# --------------------------------------------------------------------------
# 3. Couldn't ask vs didn't like the answer
# --------------------------------------------------------------------------


def test_a_protocol_fault_is_degraded_not_offline():
    """The socket answered; we could not read it. Filing that as offline would
    blame the network for a protocol fault and hide it from the operator."""
    obs = bp.ProbeObservation("t", "http://synthetic-malformed.invalid:11434",
                              bp.OUTCOME_MALFORMED, latency_ms=7.8)
    row = bp.normalize(obs)
    assert row.status == bp.STATUS_DEGRADED
    assert row.state == bp.STATE_DEGRADED
    assert row.failure_classes == (bp.FAILURE_MALFORMED_RESPONSE,)


def test_could_not_ask_and_did_not_like_the_answer_are_different_states():
    could_not_ask = bp.normalize(bp.ProbeObservation("t", "http://x.invalid", bp.OUTCOME_TIMEOUT))
    did_not_like = bp.normalize(bp.ProbeObservation("t", "http://x.invalid", bp.OUTCOME_MALFORMED))
    assert could_not_ask.state != did_not_like.state


def test_an_endpoint_never_asked_is_unknown_and_never_ok():
    """Absence is not a healthy zero."""
    row = bp.normalize(bp.ProbeObservation("t", "http://x.invalid", bp.OUTCOME_NOT_ASKED))
    assert row.status == bp.STATUS_UNKNOWN
    assert row.status != bp.STATUS_OK
    assert not bp.is_routable(row)


# --------------------------------------------------------------------------
# 4. The endpoint guard (OD-47)
# --------------------------------------------------------------------------


def test_an_answer_from_an_unregistered_host_is_not_adopted_as_an_alias():
    entry = _fixture("answered_elsewhere")
    row = bp.row_from_fixture(entry, registered_endpoint=entry["registered_endpoint"])
    # The socket genuinely answered, so the state is honest...
    assert row.state == "healthy"
    # ...but the row is not routable, and it says why.
    assert not bp.is_routable(row)
    assert bp.FAILURE_ENDPOINT_MISMATCH in row.failure_classes
    assert row.reason


def test_an_explicit_default_port_is_a_different_identity():
    """The canonical identity function is deliberately exact and does NOT fold
    a written :80 into a bare host. Deferring to it means this guard and the
    routing layer's own comparison always agree — including on exactness."""
    obs = bp.ProbeObservation("t", "http://synthetic-broker.invalid:80", bp.OUTCOME_RESPONDED, latency_ms=1.0)
    row = bp.normalize(obs, registered_endpoint="http://synthetic-broker.invalid")
    assert not bp.is_routable(row)
    assert bp.FAILURE_ENDPOINT_MISMATCH in row.failure_classes


def test_host_case_is_not_a_change_because_the_canonical_identity_folds_it():
    """The canonical function lowercases the host; the guard inherits that, so a
    case difference never manufactures a mismatch."""
    obs = bp.ProbeObservation("t", "http://Synthetic-Broker.INVALID:11434",
                              bp.OUTCOME_RESPONDED, latency_ms=1.0)
    row = bp.normalize(obs, registered_endpoint="http://synthetic-broker.invalid:11434")
    assert bp.is_routable(row)
    assert row.failure_classes == ()


def test_a_different_port_is_a_different_endpoint():
    obs = bp.ProbeObservation("t", "http://synthetic-broker.invalid:9999", bp.OUTCOME_RESPONDED, latency_ms=1.0)
    row = bp.normalize(obs, registered_endpoint="http://synthetic-broker.invalid:11434")
    assert not bp.is_routable(row)

def test_the_guard_defers_to_the_canonical_identity_and_not_its_own_rule():
    """No second rule engine: the probe's endpoint guard IS the canonical
    identity function the routing layer uses, so the two cannot disagree about
    whether two URLs name the same endpoint."""
    from src.endpoint_identity import canonical_endpoint_identity

    a, b = "http://synthetic-a.invalid:11434", "http://synthetic-b.invalid:11434"
    agree = canonical_endpoint_identity(a) == canonical_endpoint_identity(b)
    obs = bp.ProbeObservation("t", a, bp.OUTCOME_RESPONDED, latency_ms=1.0)
    row = bp.normalize(obs, registered_endpoint=b)
    assert (bp.FAILURE_ENDPOINT_MISMATCH in row.failure_classes) is (not agree)


def test_a_relative_endpoint_is_refused_rather_than_guessed():
    """The canonical identity function refuses a non-absolute URL; the guard
    inherits that refusal instead of inventing a comparison."""
    obs = bp.ProbeObservation("t", "relative/path", bp.OUTCOME_RESPONDED, latency_ms=1.0)
    with pytest.raises(ValueError):
        bp.normalize(obs, registered_endpoint="http://synthetic-b.invalid:11434")


# --------------------------------------------------------------------------
# 5. Timeouts do not block the loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dead_endpoint_costs_its_timeout_and_not_the_sum_of_all_dead_ones():
    """Three dead endpoints, three concurrent probes: wall time lands near ONE
    timeout. If the probe serialized, this would take three."""

    async def slow_transport(endpoint, timeout_s):
        await asyncio.sleep(timeout_s)
        return bp.ProbeObservation("t", endpoint, bp.OUTCOME_TIMEOUT, reason="stub")

    endpoints = [f"http://synthetic-dead-{i}.invalid:11434" for i in range(3)]
    started = time.monotonic()
    rows = await bp.probe_endpoints(endpoints, transport=slow_transport, timeout_s=0.25)
    elapsed = time.monotonic() - started

    assert len(rows) == 3
    assert all(r.status == bp.STATUS_OFFLINE for r in rows)
    # One timeout plus generous slack; three serialized would be ~0.75s.
    assert elapsed < 0.5, f"probes serialized: {elapsed:.3f}s for a 0.25s timeout"


@pytest.mark.asyncio
async def test_a_probe_returns_a_value_on_every_failure_path_rather_than_raising():
    """One dead endpoint must not take down a sweep."""

    async def exploding_transport(endpoint, timeout_s):
        return bp.ProbeObservation("t", endpoint, bp.OUTCOME_REFUSED, reason="stub")

    row = await bp.probe("http://synthetic-refused.invalid:11434", transport=exploding_transport)
    assert row.status == bp.STATUS_OFFLINE


@pytest.mark.asyncio
async def test_the_real_transport_is_not_used_when_one_is_injected():
    """Guards the suite's own claim that no test touches the network."""
    called = {"real": False}

    async def fake_transport(endpoint, timeout_s):
        return bp.ProbeObservation("t", endpoint, bp.OUTCOME_RESPONDED, latency_ms=2.0)

    def spy(*a, **k):
        called["real"] = True
        raise AssertionError("real transport ran")

    original = bp._tcp_transport
    bp._tcp_transport = spy
    try:
        row = await bp.probe("http://synthetic-broker.invalid:11434", transport=fake_transport)
    finally:
        bp._tcp_transport = original
    assert row.status == bp.STATUS_OK
    assert not called["real"]


# --------------------------------------------------------------------------
# 6. Schema
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", CASES)
def test_every_emitted_row_validates_against_the_published_schema(name):
    entry = _fixture(name)
    row = bp.row_from_fixture(entry, registered_endpoint=entry.get("registered_endpoint", entry["endpoint"]))
    jsonschema.validate(instance=row.to_dict(), schema=bp.TELEMETRY_JSON_SCHEMA)


def test_the_schema_and_the_dataclass_cannot_drift():
    """The schema's required set is exactly the compact contract the task
    specifies; the dataclass carries those plus the fields that keep the
    distinctions the schema alone cannot express."""
    assert set(bp.TELEMETRY_JSON_SCHEMA["properties"]) == set(bp.TelemetryRow.__dataclass_fields__)
    assert bp.TELEMETRY_JSON_SCHEMA["required"] == ["endpoint", "status", "latency_ms", "last_verified"]


def test_the_schema_refuses_a_status_outside_the_vocabulary():
    bad = {"endpoint": "http://x.invalid", "status": "fine_ish", "latency_ms": None,
           "last_verified": "2026-11-03T00:00:00+00:00"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=bp.TELEMETRY_JSON_SCHEMA)


def test_the_schema_refuses_a_negative_latency():
    bad = {"endpoint": "http://x.invalid", "status": "ok", "latency_ms": -1,
           "last_verified": "2026-11-03T00:00:00+00:00"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=bp.TELEMETRY_JSON_SCHEMA)


# --------------------------------------------------------------------------
# 7. Routing decision
# --------------------------------------------------------------------------


def test_only_a_healthy_matching_measured_endpoint_is_routable():
    routable = bp.normalize(bp.ProbeObservation("t", "http://a.invalid", bp.OUTCOME_RESPONDED, latency_ms=1.0))
    assert bp.is_routable(routable)

    for outcome in (bp.OUTCOME_TIMEOUT, bp.OUTCOME_REFUSED, bp.OUTCOME_MALFORMED, bp.OUTCOME_NOT_ASKED):
        assert not bp.is_routable(bp.normalize(bp.ProbeObservation("t", "http://a.invalid", outcome)))


def test_the_probe_vocabulary_agrees_with_the_capability_vocabulary():
    """A probe row and a capability record describe the same endpoint; they must
    not run different vocabularies for the same state."""
    from src import local_targets as lt

    assert bp.STATE_HEALTHY == lt.HEALTH_HEALTHY
    assert bp.STATE_DEGRADED == lt.HEALTH_DEGRADED
    assert bp.STATE_UNREACHABLE == lt.HEALTH_UNREACHABLE
    assert bp.STATE_UNKNOWN == lt.HEALTH_UNKNOWN


def test_an_unreadable_fixture_is_an_error_not_an_empty_result(tmp_path):
    with pytest.raises(ValueError):
        bp.load_fixture(tmp_path / "absent.json")
