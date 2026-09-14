"""PS-635 G1c — harness-owned acceptance test (never shown to the worker).

Every rule below is stated in the packet contract. A failure is an implementation
defect: the case exists because assertion evidence has several FORMS, not because
the contract was vague.
"""
from src.verifier_evidence import extract_assertion_evidence

EQ = """
=========================== short test summary info ============================
E       AssertionError: assert 3 == 4
FAILED tests/x.py::test_a - AssertionError
"""

CONTAINMENT = "E       AssertionError: assert 'first' in 'SUBJECT: NONE'\n"
TRUTHINESS = "E       AssertionError: assert not True\n"
PARENS = "E       AssertionError: assert (3 == 4),\n"
OTHER = "E       AssertionError: assert foo.bar()\n"
NOTHING = "all good, 4 passed\n"


def test_equality_form():
    out = extract_assertion_evidence(EQ)
    assert out["kind"] == "equality"
    assert out["actual"] == "3"
    assert out["expected"] == "4"


def test_containment_form():
    out = extract_assertion_evidence(CONTAINMENT)
    assert out["kind"] == "containment"
    assert out["actual"] == "'first'"
    assert out["expected"] == "'SUBJECT: NONE'"


def test_truthiness_form():
    out = extract_assertion_evidence(TRUTHINESS)
    assert out["kind"] == "truthiness"
    assert out["actual"] == "True"
    assert out["expected"] == "False"


def test_fully_wrapped_parens_are_unwrapped_once():
    out = extract_assertion_evidence(PARENS)
    assert out["kind"] == "equality"
    assert out["actual"] == "3"
    assert out["expected"] == "4"


def test_unrecognised_expression_is_other():
    out = extract_assertion_evidence(OTHER)
    assert out["kind"] == "other"
    assert out["expected"] == "" and out["actual"] == ""


def test_no_assertion_at_all_is_other():
    out = extract_assertion_evidence(NOTHING)
    assert out == {"kind": "other", "expected": "", "actual": ""}


def test_last_match_wins():
    out = extract_assertion_evidence("AssertionError: assert 1 == 2\nAssertionError: assert 5 == 6\n")
    assert out["actual"] == "5" and out["expected"] == "6"


def test_exact_key_set():
    assert set(extract_assertion_evidence(EQ)) == {"kind", "expected", "actual"}
