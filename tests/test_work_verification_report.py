"""Report interface controls only; literals are synthetic, not measured work."""
import copy
import json

import pytest

from src.inspector_errors import InspectorUnavailable
from src.work_verification_report import decode_work_verification_report
from tests.helpers.docker_cli import block_native_transports


COMMAND = "node benchmark/routing-inspector/verify.mjs"
PINS = [["benchmark/routing-inspector/verify.mjs", "a" * 64],
        ["benchmark/routing-inspector/reason.test.ts", "b" * 64]]


@pytest.fixture(autouse=True)
def no_native_transports(monkeypatch):
    block_native_transports(monkeypatch)


def measured_report():
    return {"schema": "tmos-work-verification-v1", "tests_collected": 23,
        "tests_executed": 20, "tests_passed": 20, "tests_failed": 0, "tests_skipped": 3,
        "tool_versions": [["node", "v24.10.0"], ["vitest", "4.1.11"]],
        "provenance": {"command": COMMAND, "verifier_digests": copy.deepcopy(PINS)},
        "positive_control": {"expected": "PASS", "observed": "PASS"},
        "negative_control": {"expected": "FAIL", "observed": "FAIL"}}


def decode(report):
    return decode_work_verification_report(json.dumps(report).encode(),
        command=COMMAND, verifier_digests=PINS)


def test_measured_report_preserves_counts_tools_and_exact_producer_binding():
    report = measured_report()
    parsed = decode(report)
    assert [parsed[name] for name in ("tests_collected", "tests_executed", "tests_passed",
                                    "tests_failed", "tests_skipped")] == [23, 20, 20, 0, 3]
    assert parsed["tool_versions"] == [["node", "v24.10.0"], ["vitest", "4.1.11"]]
    assert parsed["provenance"] == {"command": COMMAND, "verifier_digests": PINS}
    assert parsed is not report


def test_legacy_report_does_not_invent_unavailable_counters_or_tools():
    parsed = decode({"schema": "tmos-work-verification-v1", "tests_passed": 2, "tests_failed": 0})
    assert parsed["tests_passed"] == 2
    assert all(parsed.get(name) is None for name in (
        "tests_collected", "tests_executed", "tests_skipped", "tool_versions", "provenance"))


@pytest.mark.parametrize("field,value", [
    ("tests_collected", 22), ("tests_executed", 19), ("tests_skipped", 4),
    ("tests_passed", 19), ("tests_failed", 1), ("tests_collected", True),
    ("tests_executed", -1), ("tests_passed", 20.0), ("tests_skipped", None),
    ("schema", "unrecognized"), ("unknown", "not-supported"),
    ("tool_versions", []), ("tool_versions", [["node", "v24"], ["node", "v24"]]),
    ("tool_versions", [["container_image", "sha256:" + "c" * 64]]),
    ("tool_versions", [["node", "v24\nforged"]]), ("tool_versions", [["node"]]),
    ("tool_versions", {"node": "v24"}), ("tool_versions", [["node", ""]]),
    ("positive_control", []),
    ("negative_control", {"expected": "PASS", "observed": "PASS"}),
    ("positive_control", {"expected": "PASS", "observed": "PASS", "override": True}),
    ("negative_control", {"expected": "FAIL", "observed": True}),
])
def test_malformed_or_contradictory_reports_are_refused(field, value):
    report = measured_report()
    report[field] = value
    with pytest.raises(InspectorUnavailable):
        decode(report)


@pytest.mark.parametrize("field", ["tests_collected", "tests_executed", "tests_skipped",
                                  "tool_versions", "provenance", "positive_control", "negative_control"])
def test_partial_measured_reports_are_refused(field):
    report = measured_report()
    del report[field]
    with pytest.raises(InspectorUnavailable):
        decode(report)


@pytest.mark.parametrize("provenance", [
    {"command": "node another.mjs", "verifier_digests": PINS},
    {"command": COMMAND, "verifier_digests": PINS[:1]},
    {"command": COMMAND, "verifier_digests": PINS + PINS[:1]},
    {"command": COMMAND, "verifier_digests": [[PINS[0][0], "c" * 64], PINS[1]]},
    {"command": COMMAND, "verifier_digests": PINS, "source": "claimed-current"},
    {"command": COMMAND, "verifier_digests": [["path", "not-a-digest"]]},
    {"command": COMMAND}, None,
])
def test_foreign_incomplete_duplicate_or_unknown_provenance_is_refused(provenance):
    report = measured_report()
    report["provenance"] = provenance
    with pytest.raises(InspectorUnavailable):
        decode(report)


@pytest.mark.parametrize("raw", [b'{"schema":"tmos-work-verification-v1","tests_passed":1,"tests_passed":2,"tests_failed":0}',
    b'{"schema":"tmos-work-verification-v1","tests_passed":NaN,"tests_failed":0}',
    b'[]', b'{', b'\xff', b' ' * 65537])
def test_duplicate_nonfinite_malformed_and_overbudget_json_is_refused(raw):
    with pytest.raises(InspectorUnavailable):
        decode_work_verification_report(raw, command=COMMAND, verifier_digests=PINS)


def test_zero_test_claim_is_not_a_measured_execution():
    report = measured_report()
    report.update(tests_collected=0, tests_executed=0, tests_passed=0, tests_skipped=0)
    with pytest.raises(InspectorUnavailable):
        decode(report)


def test_unknown_control_is_preserved_as_unknown():
    report = measured_report()
    report["negative_control"]["observed"] = "UNKNOWN"
    assert decode(report)["negative_control"]["observed"] == "UNKNOWN"
