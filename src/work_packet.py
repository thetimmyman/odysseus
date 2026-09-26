"""Immutable WorkPacket, the atomic unit of dispatchable work.

Build packets with :func:`make_work_packet`, which raises
:class:`WorkPacketError` on any violation.
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

    ``name`` is the exact key. Keys must be declared: workers that had to guess
    them guessed wrong, and repair could not recover the missing name.
    """

    name: str
    required: bool = True
    type_hint: str = ""
    semantics: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "required": self.required,
                "type_hint": self.type_hint, "semantics": self.semantics}

    def normalized(self) -> str:
        """Canonical line used for both rendering and the digest, so they can't drift."""
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
    "contract",
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
    """Coerce to a tuple of ``str``; ``None`` is empty and a bare scalar is wrapped."""
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
    return any(not ch.isspace() for ch in value)


@dataclass(frozen=True)
class WorkPacket:
    """Frozen so it can be shared across workers and serialized deterministically."""

    packet_id: str
    objective: str
    #: The exact public surface to implement; carried verbatim into every repair,
    #: since failure evidence describes a defect, not the interface.
    contract: str = ""
    target_requirements: Tuple[str, ...] = ()
    write_scope: Tuple[str, ...] = ()
    read_scope: Tuple[str, ...] = ()
    #: Input keys named exactly as the worker reads them; required when writable.
    interface: Tuple[InterfaceField, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()
    test_command: str = ""
    negative_control: str = ""
    evidence_required: Tuple[str, ...] = ()
    stop_conditions: Tuple[str, ...] = ()

    def normalized_interface(self) -> Tuple[str, ...]:
        """Canonical lines in declaration order; a reordered interface is a different one."""
        return tuple(field.normalized() for field in self.interface)

    @property
    def interface_digest(self) -> str:
        """Digest proving attempts and repairs were given the same interface."""
        return interface_digest_of(self.interface)

    def to_dict(self) -> dict:
        """JSON-serializable dict; tuples become lists so it round-trips."""
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
        return json.dumps(self.to_dict(), sort_keys=True)


def _coerce_interface(value: Any) -> Tuple[InterfaceField, ...]:
    """Coerce a bare name, a mapping or an :class:`InterfaceField` to InterfaceFields."""
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
            # A key with whitespace can't be the exact key a mapping is read with.
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
    """Public :func:`_coerce_interface`, so the renderer interprets interfaces exactly as validated."""
    return _coerce_interface(value)


def interface_digest_of(value: Any) -> str:
    """Digest of a raw interface, so a repair can prove it carries the original one."""
    return interface_digest_from_normalized(
        [f.normalized() for f in _coerce_interface(value)])


def interface_digest_from_normalized(lines: Iterable[str]) -> str:
    """Same digest as :func:`interface_digest_of`, over already-normalized lines.

    Normalized lines can't be re-coerced into fields, so evidence validation
    re-hashes them here.
    """
    canonical = json.dumps(list(lines), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def make_work_packet(**kwargs: Any) -> WorkPacket:
    """Validate then construct a :class:`WorkPacket`.

    Raises :class:`WorkPacketError` if:

    - ``packet_id`` is missing, empty, or has no non-whitespace character
    - ``objective`` is missing, empty, or has no non-whitespace character
    - ``write_scope`` is empty (a writable packet must own a scope)
    - ``interface`` is empty (a writable packet must declare its input keys)
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

    coerced: dict = {}
    for name in _STR_FIELDS:
        if name in kwargs:
            coerced[name] = _coerce_str(kwargs[name], name)

    for name in _TUPLE_FIELDS:
        if name in kwargs:
            coerced[name] = _coerce_tuple(kwargs[name], name)

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

    # Workers guess undeclared keys differently each attempt, and repair cannot
    # recover an unspecified interface, so refuse rather than dispatch.
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
