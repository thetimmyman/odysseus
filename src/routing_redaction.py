"""Best-effort credential redaction for audit rows and outbound harness text.

Stdlib-only because it runs on every /coordinator/wrap call. It complements
routing_context's secret-file denylist by scrubbing secrets that slipped through.
"""
import re
from typing import List, Pattern, Tuple

REDACTED = "[REDACTED]"

# Whole-match redaction: the entire matched token is the secret.
SECRET_PATTERNS: List[Pattern[str]] = [
    # OpenAI/Anthropic-style secret keys
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    # GitHub tokens (classic + fine-grained prefixes: ghp/gho/ghu/ghs/ghr)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    # AWS access key ids
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # JWTs (three base64url segments; the payload/signature alone can leak claims)
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # PEM private key headers — the header is enough to flag; redacting it
    # breaks reassembly of the key material that follows.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # HTTP bearer auth header values
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]

# Generic key/value assignments: keep the key name (group 1+2) so the audit
# trail still shows WHICH credential appeared, mask only the value (group 3).
_ASSIGNMENT_PATTERN = re.compile(
    r"""(?i)\b(api[_-]?key|secret|token|password)(["' ]*[:=]["' ]*)(\S{8,})"""
)


def redact_text(text: str) -> Tuple[str, bool]:
    """Replace credential-shaped substrings with [REDACTED]; returns (text, applied_any)."""
    if not text:
        return text or "", False
    applied = 0

    def _count(n: int) -> None:
        nonlocal applied
        applied += n

    # Specific shapes first, or the generic assignment pattern swallows them.
    for pattern in SECRET_PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        _count(n)

    def _mask_value(m: "re.Match[str]") -> str:
        # Already masked by a specific pattern above — don't re-mask (it would
        # eat the value's closing quote and double-count).
        if m.group(3).startswith(REDACTED):
            return m.group(0)
        _count(1)
        return m.group(1) + m.group(2) + REDACTED

    text = _ASSIGNMENT_PATTERN.sub(_mask_value, text)
    return text, applied > 0
