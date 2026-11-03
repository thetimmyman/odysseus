"""Broker-route probe: reachability and latency for a registered endpoint.

Standalone diagnostic helper (OD-45/OD-47 telemetry lane). It measures one
registered endpoint and normalizes what it saw into a compact telemetry row.

Rules this module enforces:

1. **Observation is not inference.** A probe reports what the socket did. A
   timeout is a timeout; it is never quietly recorded as a latency, and an
   endpoint that was never asked is never reported as ``ok``.
2. **No implicit alias.** The endpoint that answered must be the endpoint the
   spec registers. A distinct hostname is a different endpoint — it does not
   become an alias for the registered one, and it does not get to satisfy the
   registration (OD-47: ``HOSTNAME differs`` is a refusal, not a rounding
   error).
3. **"Couldn't ask" and "didn't like the answer" stay separate.** They collapse
   into one wire value here, but the distinction survives in ``state`` and
   ``reason``, because merging them hides network faults as model quality
   (see ``src/local_targets.py`` on the same point).
4. **A timeout never blocks the loop.** Every attempt is bounded by
   ``asyncio.wait_for``; a dead endpoint costs its timeout and nothing more,
   and concurrent probes finish in parallel.

The probe layer's failure classes are its own (``PROBE_FAILURE_CLASSES``); they
feed the same ``failure_classes`` channel the capability records use.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, Optional
from urllib.parse import urlsplit

from src.endpoint_identity import canonical_endpoint_identity

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Wire statuses. ``ok``/``degraded``/``offline`` are the requested contract;
#: ``unknown`` is an extension this module refuses to fold into ``ok`` — an
#: endpoint that was never asked has no measured status to report, and
#: inventing one is how an absence becomes a healthy zero.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_OFFLINE = "offline"
STATUS_UNKNOWN = "unknown"

#: Internal state, aligned with ``src.local_targets`` so a probe row and a
#: capability record never disagree about the same endpoint.
STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_UNREACHABLE = "unreachable"
STATE_UNKNOWN = "unknown"

#: Probe-layer outcomes. One per way an attempt can actually end.
OUTCOME_RESPONDED = "responded"
OUTCOME_REFUSED = "refused"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_MALFORMED = "malformed"
OUTCOME_NOT_ASKED = "not_asked"

#: Probe failure classes. Snake-case to match the capability-record channel.
FAILURE_CONNECT_REFUSED = "connect_refused"
FAILURE_CONNECT_TIMEOUT = "connect_timeout"
FAILURE_MALFORMED_RESPONSE = "malformed_response"
FAILURE_ENDPOINT_MISMATCH = "endpoint_mismatch"

PROBE_FAILURE_CLASSES = frozenset({
    FAILURE_CONNECT_REFUSED, FAILURE_CONNECT_TIMEOUT,
    FAILURE_MALFORMED_RESPONSE, FAILURE_ENDPOINT_MISMATCH,
})

#: Outcome -> state. ``malformed`` is DEGRADED, not UNREACHABLE: the socket
#: answered, we simply could not read the answer. Recording it as offline would
#: blame the network for what was a protocol fault.
_OUTCOME_TO_STATE = {
    OUTCOME_RESPONDED: STATE_HEALTHY,
    OUTCOME_REFUSED: STATE_UNREACHABLE,
    OUTCOME_TIMEOUT: STATE_UNREACHABLE,
    OUTCOME_MALFORMED: STATE_DEGRADED,
    OUTCOME_NOT_ASKED: STATE_UNKNOWN,
}

#: State -> wire status. ``unknown`` maps to ``unknown``, never to ``ok``.
_STATE_TO_STATUS = {
    STATE_HEALTHY: STATUS_OK,
    STATE_DEGRADED: STATUS_DEGRADED,
    STATE_UNREACHABLE: STATUS_OFFLINE,
    STATE_UNKNOWN: STATUS_UNKNOWN,
}

DEFAULT_TIMEOUT_S = 5.0

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

#: The compact telemetry row, as JSON Schema. Single source of truth: the
#: dataclass below is checked against it by a test, so the two cannot drift.
TELEMETRY_JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "tmos.broker-probe.telemetry-row",
    "title": "Broker route telemetry row",
    "type": "object",
    "additionalProperties": False,
    "required": ["endpoint", "status", "latency_ms", "last_verified"],
    "properties": {
        "endpoint": {"type": "string", "minLength": 1},
        "status": {"enum": [STATUS_OK, STATUS_DEGRADED, STATUS_OFFLINE, STATUS_UNKNOWN]},
        # Null means "nothing was measured". A timeout is not a latency, and
        # writing the timeout bound into this field would poison a percentile.
        "latency_ms": {"type": ["number", "null"], "minimum": 0},
        "last_verified": {"type": "string", "minLength": 1},
        "state": {"enum": [STATE_HEALTHY, STATE_DEGRADED, STATE_UNREACHABLE, STATE_UNKNOWN]},
        "reason": {"type": "string"},
        "failure_classes": {"type": "array", "items": {"enum": sorted(PROBE_FAILURE_CLASSES)}},
    },
}

# --------------------------------------------------------------------------
# Observation and row
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeObservation:
    """What one attempt actually saw, before any normalization.

    ``latency_ms`` is measured on a monotonic clock. It stays ``None`` when no
    round trip completed — a refused or timed-out attempt has no latency, and
    the time spent waiting is not one either.
    """

    target_id: str
    endpoint: str
    outcome: str
    latency_ms: Optional[float] = None
    reason: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOME_TO_STATE:
            raise ValueError(f"unknown probe outcome: {self.outcome!r}")


@dataclass(frozen=True)
class TelemetryRow:
    """The normalized row. Field order matches ``TELEMETRY_JSON_SCHEMA``."""

    endpoint: str
    status: str
    latency_ms: Optional[float]
    last_verified: str
    state: str
    reason: str = ""
    failure_classes: tuple = ()

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "last_verified": self.last_verified,
            "state": self.state,
            "reason": self.reason,
            "failure_classes": list(self.failure_classes),
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(url: str) -> str:
    """The canonical endpoint identity, so this guard cannot disagree with the
    identity comparison the rest of the routing layer performs — no second rule
    engine for the same question."""
    return canonical_endpoint_identity(url)


def normalize(obs: ProbeObservation, *, registered_endpoint: Optional[str] = None) -> TelemetryRow:
    """Observation -> telemetry row. Pure, so a disputed row can be re-derived
    from stored evidence instead of re-run.

    A probe that answered on a different origin than the spec registers is a
    MISMATCH: the state stays whatever the socket said, but the row records the
    mismatch and is not routable. The alias is never taken silently.
    """
    state = _OUTCOME_TO_STATE[obs.outcome]
    failures: list = []
    reason = obs.reason

    if registered_endpoint is not None and _identity(obs.endpoint) != _identity(registered_endpoint):
        # The answer came from somewhere else. Report it, do not adopt it.
        failures.append(FAILURE_ENDPOINT_MISMATCH)
        reason = reason or f"answered on {_identity(obs.endpoint)}, registered {_identity(registered_endpoint)}"

    if obs.outcome == OUTCOME_REFUSED:
        failures.append(FAILURE_CONNECT_REFUSED)
    elif obs.outcome == OUTCOME_TIMEOUT:
        failures.append(FAILURE_CONNECT_TIMEOUT)
    elif obs.outcome == OUTCOME_MALFORMED:
        failures.append(FAILURE_MALFORMED_RESPONSE)

    return TelemetryRow(
        endpoint=obs.endpoint,
        status=_STATE_TO_STATUS[state],
        latency_ms=obs.latency_ms,
        last_verified=obs.observed_at or _utc_iso(),
        state=state,
        reason=reason,
        failure_classes=tuple(dict.fromkeys(failures)),
    )


def is_routable(row: TelemetryRow) -> bool:
    """Only a healthy, matching, measured endpoint may be routed to."""
    return row.status == STATUS_OK and FAILURE_ENDPOINT_MISMATCH not in row.failure_classes


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

#: A transport answers "what did the socket do for this endpoint?" and returns
#: a :class:`ProbeObservation`. The default is the real one below; tests inject
#: a fake, so no test opens a socket.
Transport = Callable[[str, float], Awaitable[ProbeObservation]]


async def _tcp_transport(endpoint: str, timeout_s: float) -> ProbeObservation:
    """Real transport: TCP connect + one HTTP HEAD, timed by ``wait_for``.

    Every failure path returns an observation rather than raising, so one dead
    endpoint cannot take down a sweep.
    """
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
    except asyncio.TimeoutError:
        return ProbeObservation("", endpoint, OUTCOME_TIMEOUT, reason="connect timed out")
    except (ConnectionRefusedError, OSError) as exc:
        return ProbeObservation("", endpoint, OUTCOME_REFUSED, reason=f"connect refused: {type(exc).__name__}")

    try:
        path = parsed.path or "/"
        writer.write(f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout_s - (time.monotonic() - started))
        elapsed = (time.monotonic() - started) * 1000.0
        if not line.startswith(b"HTTP/"):
            return ProbeObservation("", endpoint, OUTCOME_MALFORMED, latency_ms=elapsed,
                                    reason="first line is not an HTTP status")
        return ProbeObservation("", endpoint, OUTCOME_RESPONDED, latency_ms=elapsed)
    except asyncio.TimeoutError:
        # Connected but never answered: the socket worked, the service did not.
        # No latency is recorded — the wait is the deadline we gave up on, not a
        # round trip, and filing it as one would poison a percentile.
        return ProbeObservation("", endpoint, OUTCOME_MALFORMED,
                                reason="connected, no response before deadline")
    except (ConnectionResetError, OSError) as exc:
        return ProbeObservation("", endpoint, OUTCOME_REFUSED,
                                reason=f"connection lost: {type(exc).__name__}")
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 — cleanup must not change the verdict
            pass


async def probe(
    endpoint: str,
    *,
    transport: Optional[Transport] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    registered_endpoint: Optional[str] = None,
) -> TelemetryRow:
    """Probe one endpoint and return its telemetry row. Never raises on a
    network failure; a failure is a value."""
    obs = await (transport or _tcp_transport)(endpoint, timeout_s)
    return normalize(obs, registered_endpoint=registered_endpoint)


async def probe_endpoints(
    endpoints,
    *,
    transport: Optional[Transport] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    registered: Optional[Mapping[str, str]] = None,
) -> list:
    """Probe a set of endpoints concurrently. A dead one costs its timeout, not
    the sum of every dead one — asserted by a test."""
    registered = registered or {}
    rows = await asyncio.gather(*[
        probe(ep, transport=transport, timeout_s=timeout_s,
              registered_endpoint=registered.get(ep, ep))
        for ep in endpoints
    ])
    return list(rows)


def load_fixture(path) -> dict:
    """Read a fixture file. Fixtures are data; this module holds no fixture of
    its own — they live under ``tests/fixtures/``. A missing or unreadable
    fixture is a hard error, never a silent empty result."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"fixture unreadable: {type(exc).__name__}") from exc


def row_from_fixture(entry: dict, *, registered_endpoint: Optional[str] = None) -> TelemetryRow:
    """Build a row from a fixture entry without touching the network."""
    return normalize(
        ProbeObservation(
            target_id=entry.get("target_id", ""),
            endpoint=entry["endpoint"],
            outcome=entry["outcome"],
            latency_ms=entry.get("latency_ms"),
            reason=entry.get("reason", ""),
            observed_at=entry.get("observed_at", ""),
        ),
        registered_endpoint=registered_endpoint,
    )
