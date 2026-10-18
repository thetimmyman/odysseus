"""Shared Inspector refusal type; importing it has no application side effects."""


class InspectorUnavailable(ValueError):
    """No acknowledged operation; never implies safe redispatch."""
