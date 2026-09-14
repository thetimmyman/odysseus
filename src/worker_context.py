"""Fresh bounded worker-context renderer (PS-635 primitive #3).

Renders a bounded worker context string from a packet mapping. The output is
always capped at ``max_chars``; when truncation occurs a visible marker is
appended so truncation is never silent.
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
    """Render a list of items as 2-space-indented bullets, or NONE if empty."""
    if not items:
        return "NONE"
    if len(items) == 1:
        # A single item may be rendered inline after the label.
        return items[0]
    return "\n".join("  - " + item for item in items)


def render_worker_context(packet: dict, *, max_chars: int = 4000) -> str:
    """Render a bounded worker context string from a packet MAPPING.

    INPUT KEYS read from ``packet`` -- these exact names; absent means empty:
        "objective"            -> str
        "write_scope"          -> list/tuple of str
        "acceptance_criteria"  -> list/tuple of str
        "negative_control"     -> str
        "stop_conditions"      -> list/tuple of str

    OUTPUT: exactly these five labelled sections, in this order:
        OBJECTIVE: <objective, or NONE>
        WRITE_SCOPE: <comma-joined scope, or NONE>
        ACCEPTANCE: <one bullet per criterion, each indented 2 spaces>
        NEGATIVE_CONTROL: <text, or NONE>
        STOP_IF: <one bullet per condition, each indented 2 spaces>

    If a list has exactly one item it is rendered inline after the label.
    If a key is absent or its value is empty, that label renders the literal
    NONE.

    The result never exceeds ``max_chars`` characters. If the rendered text is
    longer, it is cut at a character boundary and the exact marker
    ``...[TRUNCATED]`` is appended so truncation is visible rather than silent.
    """
    if not isinstance(packet, dict):
        packet = {}

    objective = _as_str(packet.get("objective"))
    write_scope = _as_list(packet.get("write_scope"))
    acceptance = _as_list(packet.get("acceptance_criteria"))
    negative_control = _as_str(packet.get("negative_control"))
    stop_conditions = _as_list(packet.get("stop_conditions"))

    objective_text = objective if objective else "NONE"
    scope_text = ", ".join(write_scope) if write_scope else "NONE"
    acceptance_text = _render_bullets(acceptance)
    negative_text = negative_control if negative_control else "NONE"
    stop_text = _render_bullets(stop_conditions)

    sections = [
        "OBJECTIVE: " + objective_text,
        "WRITE_SCOPE: " + scope_text,
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
        # Degenerate bound: we cannot even fit the marker; return the marker
        # clipped to the bound so the length contract still holds.
        return _TRUNCATION_MARKER[:max_chars]
    cut = max_chars - marker_len
    return rendered[:cut] + _TRUNCATION_MARKER
