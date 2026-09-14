"""Immutable WorkPacket primitive (PS-635 primitive #1) with fail-closed validation.

A WorkPacket is the atomic unit of dispatchable work. It is immutable (frozen)
and carries everything a worker needs to execute and verify a task: an
objective, explicit scopes, acceptance criteria, a deterministic test command,
a negative control, and stop conditions.

Construction goes through :func:`make_work_packet`, which validates the inputs
and raises :class:`WorkPacketError` (a ``ValueError``) on any violation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any, Iterable, Tuple


class WorkPacketError(ValueError):
    """Raised when a WorkPacket fails fail-closed validation."""


# Fields that must be coerced to a tuple of str.
_TUPLE_FIELDS = (
    "target_requirements",
    "write_scope",
    "read_scope",
    "interface",
    "acceptance_criteria",
    "evidence_required",
    "stop_conditions",
)

# Fields that must be plain str.
_STR_FIELDS = (
    "packet_id",
    "objective",
    "test_command",
    "negative_control",
)


def _coerce_str(value: Any, field_name: str) -> str:
    """Coerce a scalar value to ``str``; reject non-scalar inputs."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    raise WorkPacketError(
        f"field {field_name!r} must be a str, got {type(value).__name__}"
    )


def _coerce_tuple(value: Any, field_name: str) -> Tuple[str, ...]:
    """Coerce an iterable of scalars to a tuple of ``str``.

    ``None`` is treated as an empty tuple. A bare scalar is wrapped in a
    single-element tuple. Each element must be coercible to ``str``.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        # A bare string is a single element, not a sequence of characters.
        return (value if isinstance(value, str) else value.decode("utf-8"),)
    if isinstance(value, (int, float, bool)):
        return (str(value),)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_coerce_str(item, field_name) for item in value)
    if isinstance(value, Iterable):
        return tuple(_coerce_str(item, field_name) for item in value)
    raise WorkPacketError(
        f"field {field_name!r} must be an iterable of str, "
        f"got {type(value).__name__}"
    )


def _has_content(value: str) -> bool:
    """True if the string contains at least one non-whitespace character."""
    return any(not ch.isspace() for ch in value)


@dataclass(frozen=True)
class WorkPacket:
    """An immutable, dispatchable unit of work.

    All scope/criteria fields are tuples of ``str``. The packet is frozen so
    it can be shared safely across workers and serialized deterministically.
    """

    packet_id: str
    objective: str
    target_requirements: Tuple[str, ...] = ()
    write_scope: Tuple[str, ...] = ()
    read_scope: Tuple[str, ...] = ()
    #: The INPUT KEYS this packet's contract promises the worker, named exactly
    #: as the worker must read them. Required for a writable packet -- see the
    #: fail-closed check in :func:`make_work_packet` for the measured reason.
    interface: Tuple[str, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()
    test_command: str = ""
    negative_control: str = ""
    evidence_required: Tuple[str, ...] = ()
    stop_conditions: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """Return a plain-JSON-serializable dict of all fields.

        Tuples are emitted as lists so the result round-trips through
        ``json.dumps`` / ``json.loads``.
        """
        result: dict = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, tuple):
                result[field.name] = list(value)
            else:
                result[field.name] = value
        return result

    def to_json(self) -> str:
        """Return a JSON string of the packet (convenience wrapper)."""
        return json.dumps(self.to_dict(), sort_keys=True)


def make_work_packet(**kwargs: Any) -> WorkPacket:
    """Validate then construct a :class:`WorkPacket`.

    Raises :class:`WorkPacketError` if:

    - ``packet_id`` is missing, empty, or has no non-whitespace character
    - ``objective`` is missing, empty, or has no non-whitespace character
    - ``write_scope`` is empty (a writable packet must own a scope)
    - ``interface`` is empty (a writable packet must declare the input keys its
      contract promises -- see the measured reason at the check itself)
    - ``test_command`` is empty (no deterministic verification => not
      dispatchable)

    Every scope/criteria field is coerced to a tuple of ``str``.
    """
    known = {f.name for f in fields(WorkPacket)}
    unknown = set(kwargs) - known
    if unknown:
        raise WorkPacketError(
            f"unknown field(s) for WorkPacket: {sorted(unknown)}"
        )

    # Coerce string fields.
    coerced: dict = {}
    for name in _STR_FIELDS:
        if name in kwargs:
            coerced[name] = _coerce_str(kwargs[name], name)

    # Coerce tuple fields.
    for name in _TUPLE_FIELDS:
        if name in kwargs:
            coerced[name] = _coerce_tuple(kwargs[name], name)

    # Fail-closed validation.
    packet_id = coerced.get("packet_id", "")
    if not _has_content(packet_id):
        raise WorkPacketError(
            "packet_id must be non-empty and contain non-whitespace"
        )

    objective = coerced.get("objective", "")
    if not _has_content(objective):
        raise WorkPacketError(
            "objective must be non-empty and contain non-whitespace"
        )

    write_scope = coerced.get("write_scope", ())
    if len(write_scope) == 0:
        raise WorkPacketError(
            "write_scope must be non-empty: a writable packet must own a scope"
        )

    # MEASURED REASON (PS-635, 2026-09-14). A writable packet whose contract
    # never names the keys the worker must read produced this, twice on the same
    # target: attempt 1 returned `ACCEPTANCE: NONE`, a compact repair packet was
    # built from the failing assertion, attempt 2 guessed the keys AGAIN and
    # DIFFERENTLY, the failure fingerprint repeated, and the loop correctly
    # escalated. Declaring the interface in the contract instead made the same
    # target pass on attempt 1 with zero repairs. A repair loop cannot recover an
    # interface that was never specified, so the packet is refused here rather
    # than dispatched and repaired.
    interface = coerced.get("interface", ())
    if len(interface) == 0:
        raise WorkPacketError(
            "interface must be non-empty: a writable packet must declare the "
            "input keys its contract promises the worker"
        )

    test_command = coerced.get("test_command", "")
    if not _has_content(test_command):
        raise WorkPacketError(
            "test_command must be non-empty: no deterministic verification "
            "means the packet is not dispatchable"
        )

    return WorkPacket(**coerced)
