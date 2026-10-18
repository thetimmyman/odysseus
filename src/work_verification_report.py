"""Decode a frozen verifier's report; never qualify its tools or execution.

The execution owner independently binds the command, source, exact container
and cleanup. Additional measured fields are all-or-none. A legacy report's
unreported counters and tool versions remain unavailable.
"""
import re

from src.inspector_errors import InspectorUnavailable
from src.local_measurement_process import decode_json


_BASE = {"schema", "tests_passed", "tests_failed"}
_CONTROLS = {"positive_control": "PASS", "negative_control": "FAIL"}
_MEASURED = {"tests_collected", "tests_executed", "tests_skipped", "tool_versions", "provenance"}
_COUNTERS = ("tests_collected", "tests_executed", "tests_passed", "tests_failed", "tests_skipped")


def _pins(rows):
    if type(rows) not in (list, tuple) or not rows:
        raise ValueError("verifier pins unavailable")
    result = {}
    for row in rows:
        if (type(row) not in (list, tuple) or len(row) != 2
                or type(row[0]) is not str or not row[0] or row[0] in result
                or type(row[1]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[1]) is None):
            raise ValueError("verifier pins unavailable")
        result[row[0]] = row[1]
    return result


def decode_work_verification_report(raw, *, command, verifier_digests):
    """Return a validated report or refuse it without inferring missing facts.

    Provenance must cite the already sealed command and complete verifier pin
    set. This verifies consistency, not the truth of a producer's observations;
    its source, framework, image and controls still need independent acceptance.
    """
    try:
        if type(raw) is not bytes or len(raw) > 65536 or type(command) is not str or not command:
            raise ValueError("report unavailable")
        expected_pins = _pins(verifier_digests)
        report = decode_json(raw)
        if (type(report) is not dict or not _BASE <= set(report)
                or set(report) - (_BASE | set(_CONTROLS) | _MEASURED)
                or report["schema"] != "tmos-work-verification-v1"):
            raise ValueError("report unavailable")
        measured = bool(set(report) & _MEASURED)
        if measured and not (_MEASURED | set(_CONTROLS)) <= set(report):
            raise ValueError("partial measurement")
        for name in _COUNTERS if measured else ("tests_passed", "tests_failed"):
            if type(report[name]) is not int or report[name] < 0:
                raise ValueError("counter unavailable")
        for name, expected in _CONTROLS.items():
            if name not in report:
                continue
            control = report[name]
            if (type(control) is not dict or set(control) != {"expected", "observed"}
                    or control["expected"] != expected
                    or control["observed"] not in ("PASS", "FAIL", "UNKNOWN")):
                raise ValueError("control unavailable")
        if measured:
            if (report["tests_collected"] <= 0 or report["tests_executed"] <= 0
                    or report["tests_collected"] != report["tests_executed"] + report["tests_skipped"]
                    or report["tests_executed"] != report["tests_passed"] + report["tests_failed"]):
                raise ValueError("contradictory counters")
            provenance = report["provenance"]
            if (type(provenance) is not dict or set(provenance) != {"command", "verifier_digests"}
                    or provenance["command"] != command
                    or _pins(provenance["verifier_digests"]) != expected_pins):
                raise ValueError("foreign provenance")
            versions = report["tool_versions"]
            if type(versions) is not list or not 0 < len(versions) <= 16:
                raise ValueError("tool observations unavailable")
            names = set()
            for pair in versions:
                if (type(pair) is not list or len(pair) != 2
                        or type(pair[0]) is not str
                        or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", pair[0]) is None
                        or pair[0] == "container_image" or pair[0] in names
                        or type(pair[1]) is not str or not 0 < len(pair[1]) <= 128
                        or pair[1] != pair[1].strip()
                        or any(ord(char) < 32 or ord(char) > 126 for char in pair[1])):
                    raise ValueError("tool observation unavailable")
                names.add(pair[0])
        return report
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise InspectorUnavailable("inspector unavailable") from None
