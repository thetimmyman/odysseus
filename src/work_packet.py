"""Immutable WorkPacket primitive (PS-635 primitive #1) with fail-closed validation.

A WorkPacket is the atomic unit of dispatchable work. It is immutable (frozen)
and carries everything a worker needs to execute and verify a task: an
objective, explicit scopes, acceptance criteria, a deterministic test command,
a negative control, and stop conditions.

Construction goes through :func:`make_work_packet`, which validates the inputs
and raises :class:`WorkPacketError` (a ``ValueError``) on any violation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any, Iterable, Tuple


class WorkPacketError(ValueError):
    """Raised when a WorkPacket fails fail-closed validation."""


@dataclass(frozen=True)
class InterfaceField:
    """One declared input the packet's contract promises the worker.

    This is the smallest typed representation that answers the four questions a
    worker actually needs answered — and nothing else:

      * ``name``      the EXACT key, never renamed, abbreviated or inferred
      * ``required``  whether the worker must read it, or may treat it as optional
      * ``type_hint`` the expected shape, e.g. ``list[str]``
      * ``semantics`` what it MEANS, in one line

    Deliberately NOT here: implementation hints, acceptance criteria, or anything
    that belongs in ``objective`` / ``acceptance_criteria``. A second task
    description language is exactly what this is meant to avoid.

    Why the type exists at all (measured 2026-09-14, PS-635): a packet whose
    contract never named its keys made a local worker guess ``acceptance`` where
    the verifier read ``acceptance_criteria``. A compact repair packet could not
    recover that — the information was simply absent — and the loop correctly
    escalated after two attempts. Names must therefore be *declared*, not inferred.
    """

    name: str
    required: bool = True
    type_hint: str = ""
    semantics: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "required": self.required,
                "type_hint": self.type_hint, "semantics": self.semantics}

    def normalized(self) -> str:
        """Canonical single-line form: stable, order-independent of formatting.

        Used for BOTH rendering and the interface digest, so what the worker is
        shown and what the evidence hashes are provably the same string. If those
        were derived separately they could drift, and a digest over a projection
        the worker never saw would prove nothing.
        """
        parts = [self.name, "required" if self.required else "optional"]
        if self.type_hint:
            parts.append(self.type_hint)
        text = " | ".join(parts)
        if self.semantics:
            text += " -- " + self.semantics
        return text


# Fields that must be coerced to a tuple of str.
_TUPLE_FIELDS = (
    "target_requirements",
    "write_scope",
    "read_scope",
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
    interface: Tuple[InterfaceField, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()
    test_command: str = ""
    negative_control: str = ""
    evidence_required: Tuple[str, ...] = ()
    stop_conditions: Tuple[str, ...] = ()

    def normalized_interface(self) -> Tuple[str, ...]:
        """The declared interface in canonical, order-preserving form.

        One canonical string per field, in DECLARATION order. Declaration order is
        preserved rather than sorted because the author controls the reading order
        of the packet; the digest below covers exactly this sequence, so an
        interface that is reordered is a DIFFERENT interface and cannot be
        silently substituted mid-run.
        """
        return tuple(field.normalized() for field in self.interface)

    @property
    def interface_digest(self) -> str:
        """Stable digest of the declared interface, for evidence integrity.

        Cheap, and it buys a real guarantee: two attempts can be shown to have been
        given the SAME interface, and an interface that changed between an attempt
        and its repair is detectable rather than assumed not to happen. Recorded on
        every run and repair entry.
        """
        return interface_digest_of(self.interface)

    def to_dict(self) -> dict:
        """Return a plain-JSON-serializable dict of all fields.

        Tuples are emitted as lists so the result round-trips through
        ``json.dumps`` / ``json.loads``; nested :class:`InterfaceField` values are
        emitted as their own dicts for the same reason.
        """
        result: dict = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, tuple):
                result[field.name] = [
                    v.to_dict() if isinstance(v, InterfaceField) else v for v in value
                ]
            else:
                result[field.name] = value
        return result

    def to_json(self) -> str:
        """Return a JSON string of the packet (convenience wrapper)."""
        return json.dumps(self.to_dict(), sort_keys=True)


def _coerce_interface(value: Any) -> Tuple[InterfaceField, ...]:
    """Coerce the declared interface to a tuple of :class:`InterfaceField`.

    Accepts, and normalises, the three shapes an author might reasonably write:

      * ``"acceptance_criteria"``               -> required, unnamed semantics
      * ``{"name": ..., "required": ..., ...}`` -> full form
      * ``InterfaceField(...)``                 -> passthrough

    The shorthand exists so declaring an interface is never the harder path — a
    rule that is annoying to satisfy is a rule that gets worked around.
    """
    if value is None:
        return ()
    items: Iterable
    if isinstance(value, (str, bytes, InterfaceField, dict)):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise WorkPacketError(
            f"interface must be a sequence of names or mappings, got "
            f"{type(value).__name__}"
        )

    out = []
    for item in items:
        if isinstance(item, InterfaceField):
            field = item
        elif isinstance(item, str):
            field = InterfaceField(name=item.strip())
        elif isinstance(item, dict):
            unknown = set(item) - {"name", "required", "type_hint", "semantics"}
            if unknown:
                raise WorkPacketError(
                    f"unknown interface field key(s): {sorted(unknown)}"
                )
            field = InterfaceField(
                name=str(item.get("name", "")).strip(),
                required=bool(item.get("required", True)),
                type_hint=str(item.get("type_hint", "")).strip(),
                semantics=str(item.get("semantics", "")).strip(),
            )
        else:
            raise WorkPacketError(
                f"interface entry must be a str, mapping or InterfaceField, got "
                f"{type(item).__name__}"
            )

        if not field.name:
            raise WorkPacketError("interface entries must have a non-empty name")
        if any(ch.isspace() for ch in field.name):
            # A key with whitespace cannot be the exact key a mapping is read
            # with, so it would reintroduce exactly the ambiguity this prevents.
            raise WorkPacketError(
                f"interface name {field.name!r} must not contain whitespace: it "
                "must be the exact key the worker reads"
            )
        out.append(field)

    names = [f.name for f in out]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise WorkPacketError(f"duplicate interface name(s): {duplicates}")
    return tuple(out)


def coerce_interface(value: Any) -> Tuple[InterfaceField, ...]:
    """Public form of :func:`_coerce_interface`, for the context renderer.

    The renderer must interpret an interface EXACTLY as the validator does, so it
    calls this rather than re-implementing the rules. Exposed publicly so callers
    do not reach for a private name across a module boundary.
    """
    return _coerce_interface(value)


def interface_digest_of(value: Any) -> str:
    """Digest of a RAW interface value, not of a built packet.

    One definition of "the same interface", usable before a packet exists — which
    is what lets a repair packet prove it carries the interface the ORIGINAL packet
    declared, rather than asserting it in prose.
    """
    fields = _coerce_interface(value)
    canonical = json.dumps([f.normalized() for f in fields],
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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
    interface = _coerce_interface(kwargs.get("interface"))
    if len(interface) == 0:
        raise WorkPacketError(
            "interface must be non-empty: a writable packet must declare the "
            "input keys its contract promises the worker"
        )
    coerced["interface"] = interface

    test_command = coerced.get("test_command", "")
    if not _has_content(test_command):
        raise WorkPacketError(
            "test_command must be non-empty: no deterministic verification "
            "means the packet is not dispatchable"
        )

    return WorkPacket(**coerced)
