"""Assertion-evidence extractor.

Turns a verifier failure (captured pytest output) into expected-vs-actual
repair evidence by scanning for ``AssertionError: assert <EXPR>`` lines.
"""

import re

_ASSERT_RE = re.compile(r"AssertionError:\s*assert\s+(.*)")


def _unwrap(expr: str) -> str:
    """Strip a single trailing comma and one layer of fully-wrapping parens."""
    expr = expr.strip()
    if expr.endswith(","):
        expr = expr[:-1].rstrip()
    if len(expr) >= 2 and expr[0] == "(" and expr[-1] == ")":
        # Only remove if the outermost parentheses are a single matching pair
        # that wraps the whole expression.
        depth = 0
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    break
        else:
            if depth == 0:
                expr = expr[1:-1].strip()
    return expr


def extract_assertion_evidence(output: str) -> dict:
    """Extract expected-vs-actual evidence from captured pytest output.

    Returns a dict with exactly the keys ``"kind"``, ``"expected"``,
    ``"actual"``.
    """
    matches = _ASSERT_RE.findall(output)
    if not matches:
        return {"kind": "other", "expected": "", "actual": ""}

    expr = _unwrap(matches[-1])

    if " == " in expr:
        left, right = expr.split(" == ", 1)
        return {
            "kind": "equality",
            "expected": right.strip(),
            "actual": left.strip(),
        }

    if " in " in expr:
        left, right = expr.split(" in ", 1)
        return {
            "kind": "containment",
            "expected": right.strip(),
            "actual": left.strip(),
        }

    if expr.startswith("not "):
        return {
            "kind": "truthiness",
            "expected": "False",
            "actual": expr[4:].strip(),
        }

    return {"kind": "other", "expected": "", "actual": ""}
