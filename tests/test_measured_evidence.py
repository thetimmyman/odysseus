"""Measured-evidence provider and vendor-vs-measurement conflict reporting.

Off by default; when enabled it finds our own files under evidence/** and
benchmark/<name>/results/**, ranks them first, and reports (never resolves) a
vendor claim that disagrees with a measurement.
"""
import hashlib
import json
from pathlib import Path

import pytest

from src import measured_evidence as me

BENCH_PATH = "evidence/run1/bench.json"


def _source(path, text):
    data = text.encode("utf-8")
    return me.MeasuredSource(
        path=path,
        url="measured:" + path,
        kind=me.SOURCE_KIND,
        trust_class=me.TRUST_CLASS,
        rank=me.MEASURED_RANK,
        content_sha256=hashlib.sha256(data).hexdigest(),
        text=text[:me.MAX_TEXT_BYTES],
        truncated=len(data) > me.MAX_TEXT_BYTES,
        size_bytes=len(data),
        score=1,
    )


def _bench_measurement(value, unit="tok/s"):
    return me.Measurement(subject="GPU decode", metric="throughput",
                          value=value, unit=unit, path=BENCH_PATH)


def test_disabled_provider_never_touches_filesystem(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("filesystem accessed while disabled")
    monkeypatch.setattr(me.os, "walk", _boom)
    monkeypatch.setattr(me.os, "scandir", _boom)

    result = me.find_measured_sources("throughput", root=tmp_path / "does-not-exist", enabled=False)

    assert result.status == me.NOT_SEARCHED
    assert result.sources == ()
    assert result.reason


def test_missing_or_nondirectory_root_is_unavailable(tmp_path):
    missing = me.find_measured_sources("throughput", root=tmp_path / "nope", enabled=True)
    assert missing.status == me.UNAVAILABLE
    assert missing.sources == ()

    a_file = tmp_path / "a-file.txt"
    a_file.write_text("throughput", encoding="utf-8")
    not_a_dir = me.find_measured_sources("throughput", root=a_file, enabled=True)
    assert not_a_dir.status == me.UNAVAILABLE


def test_finds_evidence_and_benchmark_files_only(tmp_path):
    root = tmp_path / "root"
    (root / "evidence" / "run1").mkdir(parents=True)
    (root / "benchmark" / "beta" / "results").mkdir(parents=True)
    (root / "benchmark" / "beta" / "notresults").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "evidence" / "run1" / "result.json").write_text('{"note": "throughput 12"}', encoding="utf-8")
    (root / "benchmark" / "beta" / "results" / "x.md").write_text("throughput here", encoding="utf-8")
    (root / "docs" / "notes.md").write_text("throughput was here", encoding="utf-8")
    (root / "benchmark" / "beta" / "notresults" / "y.md").write_text("throughput", encoding="utf-8")
    (root / "benchmark" / "beta" / "toplevel.md").write_text("throughput", encoding="utf-8")
    (root / "evidence" / "pic.png").write_bytes(b"\x89PNG throughput")

    result = me.find_measured_sources("throughput", root=root, enabled=True)

    assert result.status == me.OK
    assert {s.path for s in result.sources} == {
        "evidence/run1/result.json", "benchmark/beta/results/x.md"}
    for source in result.sources:
        assert source.url == "measured:" + source.path
        assert source.kind == me.SOURCE_KIND
        assert source.trust_class == me.TRUST_CLASS
        assert source.rank == me.MEASURED_RANK


def test_symlinks_and_escapes_are_not_returned(tmp_path):
    root = tmp_path / "root"
    (root / "evidence" / "run1").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("throughput secret", encoding="utf-8")
    (root / "evidence" / "run1" / "real.md").write_text("throughput real", encoding="utf-8")
    try:
        (root / "evidence" / "run1" / "link.md").symlink_to(outside / "secret.md")
        (root / "evidence" / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    result = me.find_measured_sources("throughput", root=root, enabled=True)

    assert {s.path for s in result.sources} == {"evidence/run1/real.md"}


def test_relative_path_rejects_escapes(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("x", encoding="utf-8")
    inside = root / "evidence" / "ok.md"
    inside.write_text("x", encoding="utf-8")

    assert me._relative_path(str(inside), root) == Path("evidence/ok.md")
    assert me._relative_path(str(outside / "secret.md"), root) is None
    assert me._relative_path(str(root / ".." / "outside" / "secret.md"), root) is None


def test_large_file_is_hashed_fully_and_marked_truncated(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    payload = b"alpha" + b"x" * (me.MAX_TEXT_BYTES + 1000)
    (root / "evidence" / "big.txt").write_bytes(payload)

    result = me.find_measured_sources("alpha", root=root, enabled=True)

    [source] = result.sources
    assert source.truncated is True
    assert source.size_bytes == len(payload)
    assert source.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert len(source.text) == me.MAX_TEXT_BYTES
    assert source.text.startswith("alpha")


def test_ordering_by_score_then_path_and_limit(tmp_path):
    root = tmp_path / "root"
    for name in ("aaa", "bbb", "ccc"):
        (root / "evidence" / name).mkdir(parents=True)
    (root / "evidence" / "aaa" / "zebra.md").write_text("nothing here", encoding="utf-8")
    (root / "evidence" / "bbb" / "note.md").write_text("zebra in body", encoding="utf-8")
    (root / "evidence" / "ccc" / "zebra.md").write_text("nothing here", encoding="utf-8")

    full = me.find_measured_sources("zebra", root=root, enabled=True, limit=20)
    assert [s.path for s in full.sources] == [
        "evidence/aaa/zebra.md", "evidence/ccc/zebra.md", "evidence/bbb/note.md"]
    assert [s.score for s in full.sources] == [3, 3, 1]

    capped = me.find_measured_sources("zebra", root=root, enabled=True, limit=2)
    assert [s.path for s in capped.sources] == ["evidence/aaa/zebra.md", "evidence/ccc/zebra.md"]

    clamped = me.find_measured_sources("zebra", root=root, enabled=True, limit=0)
    assert len(clamped.sources) == 1


def test_query_without_usable_tokens_is_none(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.md").write_text("anything", encoding="utf-8")

    result = me.find_measured_sources("a of", root=root, enabled=True)

    assert result.status == me.NONE
    assert result.sources == ()
    assert result.reason


def test_rank_sources_puts_measured_first():
    measured = _source("evidence/run1/bench.json", "x")
    secondary = {"kind": "secondary", "url": "https://example.test/s"}
    pinned = {"kind": "pinned", "url": "https://example.test/p"}

    ranked = me.rank_sources([secondary, measured, pinned])
    assert ranked[0] is measured
    assert ranked[1:] == [secondary, pinned]

    reverse = me.rank_sources([pinned, {"kind": "measured"}, secondary])
    assert reverse[0] == {"kind": "measured"}
    assert reverse[1:] == [pinned, secondary]


def test_as_run_record_sources_shape():
    source = _source("evidence/run1/bench.json", "throughput data")

    [record] = me.as_run_record_sources([source], start_index=3)

    assert record["source_id"] == "M3"
    assert record["url"] == "measured:evidence/run1/bench.json"
    assert record["title"] == "evidence/run1/bench.json"
    assert record["content_sha256"] == source.content_sha256
    assert record["chars"] == len(source.text)
    assert record["kind"] == me.SOURCE_KIND
    assert record["trust_class"] == me.TRUST_CLASS
    assert record["rank"] == me.MEASURED_RANK
    assert record["text"] == source.text
    assert record["retrieved_at"].endswith("+00:00")


def test_vendor_claim_vs_measurement_is_reported_as_a_conflict():
    assertion = me.Assertion(claim_id="C1", subject="  gpu   decode ", metric="Throughput",
                             value=40.0, unit="tok/s", origin="https://vendor.example/claim")

    report = me.find_conflicts([assertion], [_bench_measurement(12.0)])

    assert len(report.conflicts) == 1
    assert report.agreements == ()
    assert report.uncomparable == ()
    conflict = report.conflicts[0]
    assert conflict.claim_id == "C1"
    assert conflict.measured_path == BENCH_PATH
    assert (conflict.claimed, conflict.measured, conflict.unit) == (40.0, 12.0, "tok/s")
    assert "authoritative" in conflict.note


def test_claim_within_tolerance_is_an_agreement_not_a_conflict():
    assertion = me.Assertion("C2", "GPU decode", "throughput", 13.0, "TOK/S", "doc")

    report = me.find_conflicts([assertion], [_bench_measurement(12.0)])

    assert report.conflicts == ()
    assert len(report.agreements) == 1
    assert report.agreements[0].measured_path == BENCH_PATH
    assert report.uncomparable == ()


def test_different_units_are_uncomparable():
    assertion = me.Assertion("C3", "GPU decode", "throughput", 40.0, "tokens/s", "doc")

    report = me.find_conflicts([assertion], [_bench_measurement(12.0)])

    assert report.conflicts == ()
    assert report.agreements == ()
    assert len(report.uncomparable) == 1
    assert "different units" in report.uncomparable[0].note


def test_assertion_without_matching_measurement_is_silent():
    assertion = me.Assertion("C4", "VRAM", "capacity", 24.0, "GB", "doc")

    report = me.find_conflicts([assertion], [_bench_measurement(12.0)])

    assert report.conflicts == ()
    assert report.agreements == ()
    assert report.uncomparable == ()


def test_every_matching_measurement_is_compared():
    assertion = me.Assertion("C5", "GPU decode", "throughput", 40.0, "tok/s", "doc")

    report = me.find_conflicts([assertion], [_bench_measurement(12.0), _bench_measurement(15.0)])

    assert len(report.conflicts) == 2
    assert report.agreements == ()
    assert report.uncomparable == ()


def test_rel_tolerance_is_live():
    assertion = me.Assertion("C6", "GPU decode", "throughput", 1.5, "tok/s", "doc")
    measurements = [_bench_measurement(1.0)]

    strict = me.find_conflicts([assertion], measurements, rel_tolerance=0.2)
    loose = me.find_conflicts([assertion], measurements, rel_tolerance=0.99)

    assert len(strict.conflicts) == 1 and strict.agreements == ()
    assert loose.conflicts == () and len(loose.agreements) == 1


def test_extract_measurements_skips_malformed_entries():
    payload = {
        "measurements": [
            {"subject": "gpu", "metric": "throughput", "value": 12.5, "unit": "tok/s"},
            {"subject": "gpu", "metric": "throughput", "value": float("nan"), "unit": "tok/s"},
            {"subject": "gpu", "metric": "throughput", "value": float("inf"), "unit": "tok/s"},
            {"subject": "gpu", "metric": "throughput", "value": True, "unit": "tok/s"},
            {"subject": "gpu", "metric": "throughput", "value": 1.0},
            {"subject": 7, "metric": "throughput", "value": 1.0, "unit": "tok/s"},
            {"subject": "gpu", "metric": "throughput", "value": "12", "unit": "tok/s"},
            "not a dict",
            9,
        ]
    }
    source = _source(BENCH_PATH, json.dumps(payload))

    measurements = me.extract_measurements(source)

    assert len(measurements) == 1
    assert measurements[0].value == 12.5
    assert measurements[0].path == BENCH_PATH


def test_extract_measurements_handles_none_convention():
    assert me.extract_measurements(_source("evidence/x.json", "{not json")) == []
    assert me.extract_measurements(_source("evidence/x.json", json.dumps([1, 2]))) == []
    assert me.extract_measurements(_source("evidence/x.json", json.dumps({"measurements": 5}))) == []
    assert me.extract_measurements(_source("evidence/x.md", "plain text, no json")) == []
