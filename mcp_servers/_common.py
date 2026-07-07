"""
_common.py

Shared constants and helpers for built-in MCP servers.
"""

MAX_OUTPUT_CHARS = 10_000
MAX_READ_CHARS = 20_000
SHELL_TIMEOUT = 60
PYTHON_TIMEOUT = 30
SEARCH_TIMEOUT = 30


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate text to *limit* characters with a suffix note."""
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    return text
