"""Fresh bounded worker-context renderer (PS-635 primitive #3).

Renders a bounded worker context string from a packet mapping. The output is
always bounded to ``max_chars`` characters; if the rendered text would exceed
that bound it is cut at a character boundary and the exact marker
``...[TRUNCATED]`` is appended so truncation is visible rather than silent.
"""

__all__ = ["render_worker_context"]

_TRUNCATION_MARKER = "...[TRUNCATED]"

# Required section order and labels.
_SECTIONS = [
    ("OBJECTIVE", "objective"),
    ("WRITE_SCOPE", "write_scope"),
    ("ACCEPTANCE", "acceptance"),
    ("CRITERIA", "criteria"),
    ("NEGATIVE_CONTROL", "negative_control"),
    ("STOP_IF", "stop_if"),
]


def _as_text(value):
    """Coerce a value to a single-line string, or '' when absent/empty."""
    if value is None:
        return ""
    text = str(value)
    # Collapse any embedded newlines so each labelled section stays on one line.
    text = " ".join(part.strip() for part in text.splitlines())
    return text.strip()


def _render_bullets(items):
    """Render a list of criteria/conditions as 2-space-indented bullets.

    Returns '' when there are no items so the caller can substitute NONE.
    """
    lines = []
    for item in items:
        text = _as_text(item)
        if not text:
            continue
        lines.append("  - " + text)
    return "\n".join(lines)


def _render_value(value):
    """Render a single section value.

    Lists/tuples are rendered as bullets; scalars as a single line. Returns ''
    when the value is absent/empty so the caller can substitute NONE.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _render_bullets(value)
    return _as_text(value)


def render_worker_context(packet, max_chars=2000):
    """Render a bounded worker-context string from ``packet``.

    Sections appear in the required order with the required labels. Absent or
    empty keys render ``NONE`` rather than raising. The output never exceeds
    ``max_chars``; when it would, it is cut at a character boundary and the
    exact marker ``...[TRUNCATED]`` is appended.
    """
    if packet is None:
        packet = {}

    lines = []
    for label, key in _SECTIONS:
        rendered = _render_value(packet.get(key))
        if not rendered:
            rendered = "NONE"
        lines.append(label + ": " + rendered)

    text = "\n".join(lines)

    if len(text) <= max_chars:
        return text

    # Reserve room for the marker so the final output never exceeds max_chars.
    budget = max_chars - len(_TRUNCATION_MARKER)
    if budget < 0:
        budget = 0
    return text[:budget] + _TRUNCATION_MARKER
