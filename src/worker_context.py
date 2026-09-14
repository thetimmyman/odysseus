"""Fresh bounded worker-context renderer (PS-635 primitive #3).

Renders a bounded worker context string from a packet mapping. The output is
always capped at ``max_chars``; when truncation occurs a visible marker is
appended so truncation is never silent.

The declared INTERFACE is a first-class section, not a line bolted onto another
one. Reason (measured 2026-09-14, PS-635): a worker that was never told its input
keys guessed ``acceptance`` where the verifier read ``acceptance_criteria``; the
compact repair packet could not reconstruct the missing name and the loop
correctly escalated. An interface that is declared on the packet but never
rendered here would be decorative — validated, stored, and invisible to the worker.

Each interface bullet prints the field's canonical ``normalized()`` string, which
is the SAME string the packet's ``interface_digest`` hashes. So the evidence proves
something about exactly what the worker was shown, rather than about a parallel
projection that could drift from it.
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


def _interface_lines(value):
    """Canonical one-line strings for a declared interface, or ``[]``.

    Reuses the packet primitive's own coercion so there is ONE definition of what
    an interface entry is; a second, looser parser here would let the renderer and
    the validator disagree about the same packet, which is the class of bug this
    whole slice exists to remove.

    Accepts whatever an author plausibly wrote: bare names, mappings, or
    ``InterfaceField`` instances, or a single one of any of those.
    """
    if value is None or value == "":
        return []
    try:
        from src.work_packet import coerce_interface
    except Exception:
        return []
    try:
        fields = coerce_interface(value)
    except Exception:
        # A malformed interface is the VALIDATOR's business to refuse, not the
        # renderer's to half-render. Rendering nothing here would hide the defect,
        # so surface it explicitly in the context instead.
        return ["INVALID INTERFACE DECLARATION (rejected by the packet validator)"]
    return [field.normalized() for field in fields]


def _render_interface(value):
    """Render the declared interface as a bounded section body."""
    return _render_bullets(_interface_lines(value))


def render_worker_context(packet: dict, *, max_chars: int = 4000) -> str:
    """Render a bounded worker context string from a packet MAPPING.

    INPUT KEYS read from ``packet`` -- these exact names; absent means empty:
        "objective"            -> str
        "write_scope"          -> list/tuple of str
        "interface"            -> the declared input keys (bare names, mappings or
                                  InterfaceField values); rendered verbatim
        "acceptance_criteria"  -> list/tuple of str
        "negative_control"     -> str
        "stop_conditions"      -> list/tuple of str

    OUTPUT: exactly these six labelled sections, in this order:
        OBJECTIVE: <objective, or NONE>
        WRITE_SCOPE: <comma-joined scope, or NONE>
        INTERFACE: <one bullet per declared input key, each indented 2 spaces>
        ACCEPTANCE: <one bullet per criterion, each indented 2 spaces>
        NEGATIVE_CONTROL: <text, or NONE>
        STOP_IF: <one bullet per condition, each indented 2 spaces>

    INTERFACE sits next to WRITE_SCOPE on purpose: the scope says what the worker
    may write, the interface says what it must read, and they are the two halves of
    the same boundary. It is its own section, never appended to another one, so a
    missing interface is visible as a missing section rather than quietly absent.

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
    interface = packet.get("interface")
    acceptance = _as_list(packet.get("acceptance_criteria"))
    negative_control = _as_str(packet.get("negative_control"))
    stop_conditions = _as_list(packet.get("stop_conditions"))

    objective_text = objective if objective else "NONE"
    scope_text = ", ".join(write_scope) if write_scope else "NONE"
    interface_text = _render_interface(interface)
    acceptance_text = _render_bullets(acceptance)
    negative_text = negative_control if negative_control else "NONE"
    stop_text = _render_bullets(stop_conditions)

    sections = [
        "OBJECTIVE: " + objective_text,
        "WRITE_SCOPE: " + scope_text,
        "INTERFACE: " + interface_text,
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
