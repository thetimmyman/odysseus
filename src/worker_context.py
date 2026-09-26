"""Bounded worker-context renderer; truncation always leaves a visible marker.

The declared interface gets its own section so the worker sees its exact input
keys. Each bullet is the ``normalized()`` string the ``interface_digest`` hashes,
so evidence covers exactly what the worker was shown.
"""

__all__ = ["render_worker_context"]

_TRUNCATION_MARKER = "...[TRUNCATED]"


def _as_str(value):
    """Coerce a scalar value to a stripped string; empty/None -> ''."""
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value):
    """Coerce a value to a list of stripped, non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    result = []
    for item in items:
        text = _as_str(item)
        if text:
            result.append(text)
    return result


def _render_bullets(items):
    if not items:
        return "NONE"
    if len(items) == 1:
        return items[0]
    return "\n".join("  - " + item for item in items)


def _interface_lines(value):
    """Canonical interface lines, or ``[]``, using the validator's own coercion."""
    if value is None or value == "":
        return []
    try:
        from src.work_packet import coerce_interface
    except Exception:
        return []
    try:
        fields = coerce_interface(value)
    except Exception:
        # Surface a malformed interface explicitly rather than hiding it.
        return ["INVALID INTERFACE DECLARATION (rejected by the packet validator)"]
    return [field.normalized() for field in fields]


def _render_interface(value):
    return _render_bullets(_interface_lines(value))


def render_worker_context(packet: dict, *, max_chars: int = 4000) -> str:
    """Render a bounded worker context string from a packet MAPPING.

    INPUT KEYS read from ``packet`` -- these exact names; absent means empty:
        "objective"            -> str
        "write_scope"          -> list/tuple of str
        "interface"            -> the declared input keys (bare names, mappings or
                                  InterfaceField values); rendered verbatim
        "contract"             -> str: the exact public surface to implement
                                  (carried into every repair attempt verbatim)
        "acceptance_criteria"  -> list/tuple of str
        "negative_control"     -> str
        "stop_conditions"      -> list/tuple of str

    OUTPUT: exactly these seven labelled sections, in this order:
        OBJECTIVE: <objective, or NONE>
        WRITE_SCOPE: <comma-joined scope, or NONE>
        INTERFACE: <one bullet per declared input key, each indented 2 spaces>
        CONTRACT: <the exact public surface, or NONE>
        ACCEPTANCE: <one bullet per criterion, each indented 2 spaces>
        NEGATIVE_CONTROL: <text, or NONE>
        STOP_IF: <one bullet per condition, each indented 2 spaces>

    If a list has exactly one item it is rendered inline after the label.
    If a key is absent or its value is empty, that label renders the literal
    NONE.

    The result never exceeds ``max_chars``; longer text is cut and ends with
    ``...[TRUNCATED]``.
    """
    if not isinstance(packet, dict):
        packet = {}

    objective = _as_str(packet.get("objective"))
    write_scope = _as_list(packet.get("write_scope"))
    interface = packet.get("interface")
    acceptance = _as_list(packet.get("acceptance_criteria"))
    negative_control = _as_str(packet.get("negative_control"))
    stop_conditions = _as_list(packet.get("stop_conditions"))

    objective_text = objective if objective else "NONE"
    scope_text = ", ".join(write_scope) if write_scope else "NONE"
    interface_text = _render_interface(interface)
    contract = _as_str(packet.get("contract"))
    contract_text = contract if contract else "NONE"
    acceptance_text = _render_bullets(acceptance)
    negative_text = negative_control if negative_control else "NONE"
    stop_text = _render_bullets(stop_conditions)

    sections = [
        "OBJECTIVE: " + objective_text,
        "WRITE_SCOPE: " + scope_text,
        "INTERFACE: " + interface_text,
        "CONTRACT: " + contract_text,
        "ACCEPTANCE: " + acceptance_text,
        "NEGATIVE_CONTROL: " + negative_text,
        "STOP_IF: " + stop_text,
    ]
    rendered = "\n".join(sections)

    if len(rendered) <= max_chars:
        return rendered

    # Truncate at a character boundary, reserving room for the marker so the
    # final string never exceeds max_chars.
    marker_len = len(_TRUNCATION_MARKER)
    if max_chars <= marker_len:
        # Too small for the marker: clip the marker so the length bound holds.
        return _TRUNCATION_MARKER[:max_chars]
    cut = max_chars - marker_len
    return rendered[:cut] + _TRUNCATION_MARKER
