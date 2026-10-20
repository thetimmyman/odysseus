"""Measured-evidence provider and vendor-vs-measurement conflict reporting.

Off by default; when enabled it finds our own files under evidence/** and
benchmark/<name>/results/**, ranks them first, and reports (never resolves) a
vendor claim that disagrees with a measurement.
"""
import hashlib
import os
import stat
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
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        file_sha256=hashlib.sha256(data).hexdigest(),
        text=text[:me.MAX_TEXT_BYTES],
        truncated=len(data) > me.MAX_TEXT_BYTES,
        size_bytes=len(data),
        score=1,
    )


def _read(root, rel):
    """Read ``rel`` the way the lookup does: relative to one descriptor opened on ``root``."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return me._read_regular_file(fd, rel)
    finally:
        os.close(fd)


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


def test_reads_reject_escapes_and_listing_stays_inside_the_roots(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("x", encoding="utf-8")
    (root / "evidence" / "ok.md").write_text("x", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "skip.md").write_text("x", encoding="utf-8")

    assert _read(root, Path("evidence/ok.md")) == b"x"
    assert _read(root, Path("../outside/secret.md")) is None
    assert _read(root, Path("evidence/../../outside/secret.md")) is None
    assert list(me._iter_candidate_files(root)) == [Path("evidence/ok.md")]


def test_large_file_is_hashed_fully_and_marked_truncated(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    payload = b"alpha" + b"x" * (me.MAX_TEXT_BYTES + 1000)
    (root / "evidence" / "big.txt").write_bytes(payload)

    result = me.find_measured_sources("alpha", root=root, enabled=True)

    [source] = result.sources
    assert source.truncated is True
    assert source.size_bytes == len(payload)
    assert source.file_sha256 == hashlib.sha256(payload).hexdigest()
    # content_sha256 follows the run-record contract: the hash of the exact text handed out.
    assert source.content_sha256 == hashlib.sha256(source.text.encode("utf-8")).hexdigest()
    assert source.content_sha256 != source.file_sha256
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
    assert record["file_sha256"] == source.file_sha256
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
    assert (conflict.claimed, conflict.measured, conflict.claimed_unit, conflict.measured_unit) == (
        40.0, 12.0, "tok/s", "tok/s")
    assert "authoritative" in conflict.note


def test_claim_within_tolerance_is_an_agreement_not_a_conflict():
    assertion = me.Assertion("C2", "GPU decode", "throughput", 13.0, "tok/s", "doc")

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


def test_text_cap_is_in_bytes_not_characters(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    # 40,000 two-byte characters put the query term past byte 65,536: it must not be found there.
    (root / "evidence" / "wide.txt").write_text("\u00e9" * 40000 + " needle", encoding="utf-8")
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert result.status == me.NONE


def test_text_returned_never_exceeds_the_byte_cap(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "wide.txt").write_text("needle " + "\u00e9" * 40000, encoding="utf-8")
    [source] = me.find_measured_sources("needle", root=root, enabled=True).sources
    assert len(source.text.encode("utf-8")) <= me.MAX_TEXT_BYTES + 3  # a split tail may become U+FFFD


def test_symlink_swapped_in_after_listing_is_not_followed(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("needle outside the root")
    (root / "evidence" / "link.txt").symlink_to(secret)
    assert _read(root, Path("evidence/link.txt")) is None
    (root / "evidence" / "real.txt").write_text("needle inside")
    assert _read(root, Path("evidence/real.txt")) == b"needle inside"
    assert _read(root, Path("evidence")) is None  # a directory is not a regular file


def test_a_parent_directory_swapped_for_a_symlink_is_not_followed(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "evidence").mkdir(parents=True)
    outside.mkdir()
    (outside / "x.txt").write_text("needle outside the root")
    (root / "evidence" / "run").symlink_to(outside, target_is_directory=True)
    assert _read(root, Path("evidence/run/x.txt")) is None


def test_a_file_that_changes_while_it_is_read_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "f.txt").write_text("needle")
    real = os.fstat
    calls = []

    def changing(fd):
        info = real(fd)
        calls.append(fd)
        if len(calls) >= 2 and stat.S_ISREG(info.st_mode):
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid,
                                   info.st_gid, info.st_size, int(info.st_atime), int(info.st_mtime) + 5,
                                   int(info.st_ctime), info.st_atime_ns, info.st_mtime_ns + 5_000_000_000,
                                   info.st_ctime_ns))
        return info

    monkeypatch.setattr(os, "fstat", changing)
    with pytest.raises(me._Unreadable):
        _read(root, Path("evidence/f.txt"))


def test_invalid_utf8_cannot_expand_past_the_byte_cap(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "bad.txt").write_bytes(b"needle " + b"\xff" * me.MAX_TEXT_BYTES)
    [source] = me.find_measured_sources("needle", root=root, enabled=True).sources
    assert len(source.text.encode("utf-8")) <= me.MAX_TEXT_BYTES


def test_lookup_work_is_bounded_and_keeps_only_the_best(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    for index in range(30):
        (root / "evidence" / f"f{index:02d}.txt").write_text("needle " * (index + 1))
    monkeypatch.setattr(me, "_MAX_FILES_SCANNED", 10)
    result = me.find_measured_sources("needle", root=root, enabled=True, limit=2)
    assert result.status == me.OK and len(result.sources) == 2
    assert "scan stopped" in result.reason
    monkeypatch.setattr(me, "_MAX_FILES_SCANNED", 5000)
    full = me.find_measured_sources("needle", root=root, enabled=True, limit=2)
    assert "scan stopped" not in full.reason and len(full.sources) == 2


def test_measurements_survive_a_json_file_cut_at_the_text_cap(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    doc = {"measurements": [{"subject": "GPU decode", "metric": "throughput", "value": 12, "unit": "tok/s"}],
           "samples": [0.5] * 40000}
    (root / "evidence" / "bench.json").write_text(json.dumps(doc))
    [source] = me.find_measured_sources("throughput", root=root, enabled=True).sources
    assert source.truncated is True
    assert [m.value for m in me.extract_measurements(source)] == [12.0]


def test_an_overflowing_claim_is_skipped_not_fatal():
    bad = me.Assertion("C0", "GPU decode", "throughput", 10 ** 400, "tok/s", "vendor")
    good = me.Assertion("C1", "GPU decode", "throughput", 40.0, "tok/s", "vendor")
    report = me.find_conflicts([bad, good], [_bench_measurement(12.0)])
    assert [c.claim_id for c in report.conflicts] == ["C1"]


def test_conflicts_name_where_the_claim_came_from():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 40.0, "tok/s", "https://vendor.example/claim")
    [conflict] = me.find_conflicts([assertion], [_bench_measurement(12.0)]).conflicts
    assert conflict.claimed_origin == "https://vendor.example/claim"


def test_non_latin_query_finds_matching_evidence(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "zh.txt").write_text("\u89e3\u7801\u901f\u5ea6 12 tok/s", encoding="utf-8")
    result = me.find_measured_sources("\u89e3\u7801\u901f\u5ea6", root=root, enabled=True)
    assert result.status == me.OK
    assert [src.path for src in result.sources] == ["evidence/zh.txt"]


def test_huge_integer_measurement_is_skipped_not_fatal():
    source = _source(BENCH_PATH, '{"measurements": [{"subject": "a", "metric": "m", "value": 1' + "0" * 400
                     + ', "unit": "u"}, {"subject": "a", "metric": "m", "value": 5, "unit": "u"}]}')
    measurements = me.extract_measurements(source)
    assert [m.value for m in measurements] == [5.0]
    assert me.find_conflicts([me.Assertion("C", "a", "m", 5.0, "u", "v")], measurements).agreements


def test_tiny_measurements_are_compared_by_their_own_magnitude():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 1e-10, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(1e-12)])
    assert len(report.conflicts) == 1 and report.agreements == ()


def test_zero_measurement_conflicts_with_a_nonzero_claim_without_a_ratio():
    nonzero = me.Assertion("C1", "GPU decode", "throughput", 3.0, "tok/s", "vendor")
    zero = me.Assertion("C2", "GPU decode", "throughput", 0.0, "tok/s", "vendor")
    report = me.find_conflicts([nonzero, zero], [_bench_measurement(0.0)])
    assert [c.claim_id for c in report.conflicts] == ["C1"]
    assert report.conflicts[0].relative_difference is None
    assert [c.claim_id for c in report.agreements] == ["C2"]


def test_uncomparable_result_keeps_both_units():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 40.0, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(12.0, unit="ms")])
    [item] = report.uncomparable
    assert (item.claimed_unit, item.measured_unit) == ("tok/s", "ms")


def test_settings_keys_are_registered_and_off_by_default():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["research_measured_evidence"] is False
    assert DEFAULT_SETTINGS["research_measured_evidence_root"] == ""
    assert me.measured_evidence_enabled() is False
    assert me.measured_evidence_root() is None


def test_unsupported_files_and_empty_dirs_count_toward_the_walk_cap(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    for index in range(40):
        (root / "evidence" / f"img{index:02d}.png").write_bytes(b"x")
        (root / "evidence" / f"empty{index:02d}").mkdir()
    (root / "evidence" / "zz.txt").write_text("needle")
    monkeypatch.setattr(me, "_MAX_ENTRIES_WALKED", 30)
    capped = me.find_measured_sources("needle", root=root, enabled=True)
    assert "scan stopped" in capped.reason and capped.sources == ()
    monkeypatch.setattr(me, "_MAX_ENTRIES_WALKED", 20000)
    full = me.find_measured_sources("needle", root=root, enabled=True)
    assert [s.path for s in full.sources] == ["evidence/zz.txt"]


def test_incomparable_units_report_no_ratio():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 40.0, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(12.0, unit="ms")])
    [item] = report.uncomparable
    assert item.relative_difference is None
    # Negative control: equal units still carry the ratio.
    same = me.find_conflicts([assertion], [_bench_measurement(12.0)])
    assert same.conflicts[0].relative_difference == pytest.approx(40.0 / 12.0 - 1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10 ** 400, True, "12"])
def test_invalid_directly_constructed_measurements_are_skipped(value):
    assertion = me.Assertion("C1", "GPU decode", "throughput", 40.0, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(value), _bench_measurement(12.0)])
    assert len(report.conflicts) == 1 and report.conflicts[0].measured == 12.0
    assert not report.agreements and not report.uncomparable


@pytest.mark.parametrize("tolerance", [-0.1, float("inf"), float("nan"), 10 ** 400, None, True])
def test_invalid_tolerances_are_refused(tolerance):
    assertion = me.Assertion("C1", "GPU decode", "throughput", 12.0, "tok/s", "vendor")
    with pytest.raises(ValueError, match="rel_tolerance"):
        me.find_conflicts([assertion], [_bench_measurement(12.0)], rel_tolerance=tolerance)
    # Negative control: zero is a valid, strict tolerance.
    assert me.find_conflicts([assertion], [_bench_measurement(12.0)], rel_tolerance=0).agreements


def test_infinite_limit_falls_back_to_the_default(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.txt").write_text("needle")
    result = me.find_measured_sources("needle", root=root, enabled=True, limit=float("inf"))
    assert result.status == me.OK and len(result.sources) == 1


def test_a_file_that_does_not_parse_whole_does_not_fall_back_to_its_truncated_prefix(tmp_path):
    root = tmp_path / "root"
    (root / "benchmark" / "gpu" / "results").mkdir(parents=True)
    valid = json.dumps({"measurements": [{"subject": "GPU decode", "metric": "throughput",
                                          "value": 12.0, "unit": "tok/s"}]})
    padding = " " * (me.MAX_TEXT_BYTES + 10)
    (root / "benchmark" / "gpu" / "results" / "run.json").write_text(valid + padding + "garbage")
    [source] = me.find_measured_sources("throughput", root=root, enabled=True).sources
    assert source.truncated and source.measurements == ()
    assert me.extract_measurements(source) == []
    # Negative control: a source with no prepared parse reads its own text.
    bare = _source("benchmark/gpu/results/x.json", valid)
    assert len(me.extract_measurements(bare)) == 1


def test_a_ratio_outside_the_float_range_is_reported_as_incomputable():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 1e308, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(1e-308)])
    [item] = report.conflicts
    assert item.relative_difference is None
    json.dumps(report.conflicts[0].__dict__, allow_nan=False)


def test_an_unreadable_evidence_directory_is_unavailable_not_none(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    root = tmp_path / "root"
    locked = root / "evidence"
    locked.mkdir(parents=True)
    (locked / "a.txt").write_text("needle")
    locked.chmod(0)
    try:
        result = me.find_measured_sources("needle", root=root, enabled=True)
    finally:
        locked.chmod(0o755)
    assert result.status == me.UNAVAILABLE and "could not be read" in result.reason
    # Negative control: once readable, the same tree finds the file.
    assert me.find_measured_sources("needle", root=root, enabled=True).status == me.OK


def test_an_unreadable_candidate_file_is_unavailable_not_none(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    locked = root / "evidence" / "a.txt"
    locked.write_text("needle")
    locked.chmod(0)
    try:
        result = me.find_measured_sources("needle", root=root, enabled=True)
    finally:
        locked.chmod(0o644)
    assert result.status == me.UNAVAILABLE and "a.txt" in result.reason
    # Negative control: an intentionally skipped symlink stays NONE.
    other = tmp_path / "other"
    (other / "evidence").mkdir(parents=True)
    (other / "evidence" / "link.txt").symlink_to(locked)
    assert me.find_measured_sources("needle", root=other, enabled=True).status == me.NONE


def test_a_root_replaced_after_it_was_opened_is_not_reopened_by_name(tmp_path):
    real = tmp_path / "real"
    (real / "evidence").mkdir(parents=True)
    (real / "evidence" / "a.txt").write_text("needle")
    outside = tmp_path / "outside"
    (outside / "evidence").mkdir(parents=True)
    (outside / "evidence" / "a.txt").write_text("secret needle")
    fd = os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        real.rename(tmp_path / "moved")
        real.symlink_to(outside)
        # Anchored on the descriptor: the original tree is read, never the swapped-in outside tree.
        assert me._read_regular_file(fd, Path("evidence/a.txt")) == b"needle"
    finally:
        os.close(fd)
    # And opening the swapped-in symlink by name is refused.
    with pytest.raises(OSError):
        os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def test_structured_measurements_per_file_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(me, "_MAX_MEASUREMENTS_PER_FILE", 5)
    root = tmp_path / "root"
    (root / "benchmark" / "gpu" / "results").mkdir(parents=True)
    entries = [{"subject": f"s{i}", "metric": "m", "value": float(i), "unit": "u"} for i in range(50)]
    (root / "benchmark" / "gpu" / "results" / "run.json").write_text(json.dumps({"measurements": entries}))
    [source] = me.find_measured_sources("run", root=root, enabled=True).sources
    assert len(source.measurements) == 5 and source.measurements_capped is True


def test_a_capped_scan_with_no_match_is_unavailable_not_none(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    for index in range(10):
        (root / "evidence" / f"a{index}.txt").write_text("nothing here")
    (root / "evidence" / "z.txt").write_text("needle")
    monkeypatch.setattr(me, "_MAX_FILES_SCANNED", 3)
    capped = me.find_measured_sources("needle", root=root, enabled=True)
    assert capped.status == me.UNAVAILABLE and "incomplete" in capped.reason
    monkeypatch.setattr(me, "_MAX_FILES_SCANNED", 5000)
    assert me.find_measured_sources("needle", root=root, enabled=True).status == me.OK
    # Negative control: a complete scan with no match stays NONE.
    assert me.find_measured_sources("absent", root=root, enabled=True).status == me.NONE


def test_a_root_without_search_permission_is_unavailable_not_an_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    root.chmod(0o444)
    try:
        result = me.find_measured_sources("needle", root=root, enabled=True)
    finally:
        root.chmod(0o755)
    assert result.status == me.UNAVAILABLE


def test_large_integers_are_compared_exactly():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 9007199254740993, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(9007199254740992)], rel_tolerance=0)
    assert not report.agreements and len(report.conflicts) == 1
    # Negative control: identical integers agree even at zero tolerance.
    same = me.find_conflicts([assertion], [_bench_measurement(9007199254740993)], rel_tolerance=0)
    assert len(same.agreements) == 1


def test_combining_marks_stay_with_their_word():
    tokens = me._query_tokens("سُرْعَة GPU")
    assert "سُرْعَة".casefold() in tokens and "gpu" in tokens


def test_measurements_capped_only_when_an_entry_was_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(me, "_MAX_MEASUREMENTS_PER_FILE", 5)
    root = tmp_path / "root"
    (root / "benchmark" / "gpu" / "results").mkdir(parents=True)

    def write(count):
        entries = [{"subject": f"s{i}", "metric": "m", "value": float(i), "unit": "u"} for i in range(count)]
        (root / "benchmark" / "gpu" / "results" / "run.json").write_text(json.dumps({"measurements": entries}))
        [source] = me.find_measured_sources("run", root=root, enabled=True).sources
        return source

    exact = write(5)
    assert len(exact.measurements) == 5 and exact.measurements_capped is False
    over = write(6)
    assert len(over.measurements) == 5 and over.measurements_capped is True


def test_unexpected_open_failures_are_unavailable_not_skipped(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.txt").write_text("needle")
    real_open = os.open

    def failing(path, flags, *args, **kwargs):
        if path == "a.txt":
            raise OSError(24, "Too many open files")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert result.status == me.UNAVAILABLE


def test_unit_symbols_are_case_sensitive_but_whitespace_insensitive():
    mw = me.Assertion("C1", "GPU decode", "power", 1.0, "MW", "vendor")
    report = me.find_conflicts([mw], [me.Measurement("GPU decode", "power", 1.0, "mW", BENCH_PATH)])
    assert not report.agreements and len(report.uncomparable) == 1
    spaced = me.Assertion("C2", "GPU decode", "power", 1.0, "tok /  s", "vendor")
    same = me.find_conflicts([spaced], [me.Measurement("GPU decode", "power", 1.0, "tok / s", BENCH_PATH)])
    assert len(same.agreements) == 1


def test_mixed_integer_and_float_comparisons_are_exact():
    assertion = me.Assertion("C1", "GPU decode", "throughput", 9007199254740993, "tok/s", "vendor")
    report = me.find_conflicts([assertion], [_bench_measurement(9007199254740992.0)], rel_tolerance=0)
    assert not report.agreements and len(report.conflicts) == 1
    same = me.find_conflicts([assertion], [_bench_measurement(9007199254740993)], rel_tolerance=0)
    assert len(same.agreements) == 1


def test_a_very_wide_directory_stops_at_the_entry_cap_while_it_is_read(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    for index in range(60):
        (root / "evidence" / f"img{index:02d}.png").write_bytes(b"x")
    monkeypatch.setattr(me, "_MAX_ENTRIES_WALKED", 20)
    seen = []
    real_scandir = os.scandir

    class Counting:
        def __init__(self, path):
            self.inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def __iter__(self):
            for entry in self.inner:
                seen.append(entry.name)
                yield entry

    monkeypatch.setattr(os, "scandir", lambda path: Counting(path))
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert result.status == me.UNAVAILABLE and len(seen) <= 21


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_root_is_unconfigured_not_the_working_directory(empty):
    result = me.find_measured_sources("needle", root=empty, enabled=True)
    assert result.status == me.UNAVAILABLE and "no measured-evidence root" in result.reason


def test_a_ratio_just_above_tolerance_is_a_conflict_even_if_it_rounds_to_the_float(tmp_path):
    measurement = me.Measurement(subject="s", metric="m", value=10**20, unit="u", path="p")
    assertion = me.Assertion("C1", "s", "m", 10**20 + 20000000000000001111, "u", "doc")

    report = me.find_conflicts([assertion], [measurement], rel_tolerance=0.2)

    assert len(report.conflicts) == 1 and report.agreements == ()


def test_nesting_deeper_than_the_depth_limit_is_incomplete_not_empty(tmp_path):
    root = tmp_path / "root"
    deep = root / "evidence"
    for index in range(40):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    (deep / "run.json").write_text('{"needle": 1}')

    result = me.find_measured_sources("needle", root=root, enabled=True)

    assert result.status == me.UNAVAILABLE


def test_an_entry_metadata_failure_is_a_traversal_error_not_an_absent_entry(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.json").write_text('{"needle": 1}')
    real_scandir = os.scandir

    class Failing:
        def __init__(self, path):
            self.inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def __iter__(self):
            for entry in self.inner:
                class Broken:
                    name = entry.name

                    def is_symlink(self):
                        raise OSError(116, "stale")
                yield Broken()

    monkeypatch.setattr(os, "scandir", lambda path: Failing(path))
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert result.status == me.UNAVAILABLE


def test_query_tokens_are_deduplicated_and_capped():
    query = " ".join(f"word{index:06d}" for index in range(100000)) + " word000001"
    tokens = me._query_tokens(query)
    assert len(tokens) == me._MAX_QUERY_TOKENS == len(set(tokens))


def test_a_root_path_with_a_nul_is_unavailable_not_an_error():
    result = me.find_measured_sources("needle", root="/tmp/bad\x00root", enabled=True)
    assert result.status == me.UNAVAILABLE


def test_listing_follows_the_opened_root_not_the_path_that_replaced_it(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.json").write_text('{"needle": 1}')
    real_open = os.open
    swapped = []

    def swap_after_root_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if not swapped and path == "root" and kwargs.get("dir_fd") is not None:
            swapped.append(True)
            root.rename(tmp_path / "moved")
            (root / "evidence").mkdir(parents=True)  # an empty replacement tree at the same path
        return fd

    monkeypatch.setattr(os, "open", swap_after_root_open)
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert swapped and [s.path for s in result.sources] == ["evidence/a.json"]


def test_benchmark_subtrees_other_than_results_are_never_entered(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "benchmark" / "gpu" / "results").mkdir(parents=True)
    (root / "benchmark" / "gpu" / "results" / "run.json").write_text('{"needle": 1}')
    (root / "benchmark" / "gpu" / "other").mkdir()
    for index in range(50):
        (root / "benchmark" / "gpu" / "other" / f"f{index}.json").write_text("x")
    monkeypatch.setattr(me, "_MAX_ENTRIES_WALKED", 10)

    result = me.find_measured_sources("needle", root=root, enabled=True)

    assert [s.path for s in result.sources] == ["benchmark/gpu/results/run.json"]


def test_canonically_equivalent_unicode_spellings_match(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "note.md").write_text("caf" + "é" + " latency")

    result = me.find_measured_sources("café", root=root, enabled=True)

    assert [s.path for s in result.sources] == ["evidence/note.md"]


def test_bytes_read_by_a_rejected_read_still_count_against_the_budget(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    for index in range(5):
        (root / "evidence" / f"f{index}.md").write_text("needle " * 10)

    def rejecting(fd, rel, used=None, remaining=None):
        if used is not None:
            used.append(100)
        return None

    monkeypatch.setattr(me, "_read_regular_file", rejecting)
    monkeypatch.setattr(me, "_MAX_BYTES_SCANNED", 250)
    result = me.find_measured_sources("needle", root=root, enabled=True)
    # Every rejected read cost 100 bytes, so the 250-byte budget ran out: incomplete, not "no evidence".
    assert result.status == me.UNAVAILABLE


def test_an_ancestor_swapped_for_a_symlink_before_the_root_open_is_refused(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    (parent / "root" / "evidence").mkdir(parents=True)
    (parent / "root" / "evidence" / "a.json").write_text('{"needle": 1}')
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "root" / "evidence").mkdir(parents=True)
    (elsewhere / "root" / "evidence" / "evil.json").write_text('{"needle": 2}')
    real_resolve = Path.resolve

    def swap_after_resolve(self, *args, **kwargs):
        out = real_resolve(self, *args, **kwargs)
        if out == (parent / "root").resolve() and parent.is_dir() and not parent.is_symlink():
            parent.rename(tmp_path / "parent-moved")
            parent.symlink_to(elsewhere)
        return out

    monkeypatch.setattr(Path, "resolve", swap_after_resolve)
    result = me.find_measured_sources("needle", root=parent / "root", enabled=True)
    assert "evidence/evil.json" not in [s.path for s in result.sources]


def test_canonically_equivalent_subjects_are_compared():
    measurement = me.Measurement(subject="caf" + "é", metric="latency", value=10.0, unit="ms", path="p")
    assertion = me.Assertion("C1", "café", "latency", 99.0, "ms", "doc")
    report = me.find_conflicts([assertion], [measurement])
    assert len(report.conflicts) == 1


def test_an_oversized_integer_skips_only_that_entry():
    raw = ('{"measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 1' + "0" * 5000 + '},'
           ' {"subject": "b", "metric": "m", "unit": "u", "value": 7}]}').encode()
    parsed, capped = me._parse_measurements_capped(raw, "p")
    assert [m.subject for m in parsed] == ["b"] and not capped


@pytest.mark.parametrize("value,expected", [("false", False), ("true", False), (1, False), (True, True), (False, False)])
def test_only_a_real_boolean_enables_the_provider(monkeypatch, value, expected):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: value)
    assert me.measured_evidence_enabled() is expected


def test_canonically_equivalent_unit_spellings_are_comparable():
    measurement = me.Measurement(subject="s", metric="m", value=5.0, unit="Å", path="p")
    assertion = me.Assertion("C1", "s", "m", 5.0, "Å", "doc")
    report = me.find_conflicts([assertion], [measurement])
    assert len(report.agreements) == 1 and report.uncomparable == ()


def test_non_utf8_json_is_not_parsed_as_measurements():
    raw = '{"measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 1}]}'.encode("utf-16")
    assert me._parse_measurements_capped(raw, "p") == ((), False)


def test_a_nonzero_literal_that_underflows_is_skipped_not_zero():
    raw = ('{"measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 1e-400},'
           ' {"subject": "b", "metric": "m", "unit": "u", "value": 0.0}]}').encode()
    parsed, _ = me._parse_measurements_capped(raw, "p")
    assert [m.subject for m in parsed] == ["b"]


def test_an_integral_decimal_literal_keeps_its_exact_value():
    raw = b'{"measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 9007199254740993.0}]}'
    parsed, _ = me._parse_measurements_capped(raw, "p")
    assertion = me.Assertion("C1", "a", "m", 9007199254740992, "u", "doc")
    report = me.find_conflicts([assertion], list(parsed), rel_tolerance=0)
    assert parsed[0].value == 9007199254740993 and len(report.conflicts) == 1


def test_a_candidate_larger_than_the_remaining_byte_allowance_is_not_read(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.md").write_text("needle " * 100)
    monkeypatch.setattr(me, "_MAX_BYTES_SCANNED", 100)
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert result.status == me.UNAVAILABLE and not result.sources


def test_an_in_place_rewrite_that_restores_mtime_is_rejected(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    target = root / "evidence" / "a.md"
    target.write_bytes(b"x" * 100)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    real_fstat = os.fstat
    calls = []

    def fstat(handle):
        result = real_fstat(handle)
        calls.append(1)
        if len(calls) == 1:
            os.chmod(target, 0o600)  # a metadata change moves ctime without touching size or mtime
        return result

    try:
        import unittest.mock as mock
        with mock.patch.object(os, "fstat", fstat):
            with pytest.raises(me._Unreadable):
                me._read_regular_file(fd, Path("evidence/a.md"))
    finally:
        os.close(fd)


def test_an_unsupported_platform_is_unavailable_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(me, "_platform_supported", lambda: False)
    result = me.find_measured_sources("needle", root=tmp_path, enabled=True)
    assert result.status == me.UNAVAILABLE and "POSIX" in result.reason


def test_a_decimal_literal_a_float_cannot_hold_is_skipped_not_rounded():
    raw = (b'{"measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 1.0000000000000001},'
           b' {"subject": "b", "metric": "m", "unit": "u", "value": 0.1}]}')
    parsed, _ = me._parse_measurements_capped(raw, "p")
    assert [m.subject for m in parsed] == ["b"]


def test_a_root_replaced_by_another_directory_before_the_open_is_refused(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    (parent / "root" / "evidence").mkdir(parents=True)
    (parent / "root" / "evidence" / "a.json").write_text('{"needle": 1}')
    real_open = os.open
    swapped = []

    def swap_on_first_open(path, flags, *args, **kwargs):
        if not swapped and path == "/":
            swapped.append(True)
            parent.rename(tmp_path / "parent-moved")
            (parent / "root" / "evidence").mkdir(parents=True)
            (parent / "root" / "evidence" / "evil.json").write_text('{"needle": 2}')
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_on_first_open)
    result = me.find_measured_sources("needle", root=parent / "root", enabled=True)
    assert swapped and result.status == me.UNAVAILABLE and not result.sources


def test_a_read_is_capped_at_the_remaining_allowance(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "a.md").write_bytes(b"x" * 50)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    used = []
    try:
        assert me._read_regular_file(fd, Path("evidence/a.md"), used, remaining=50) == b"x" * 50
        used.clear()
        with pytest.raises(me._OverBudget):
            me._read_regular_file(fd, Path("evidence/a.md"), used, remaining=10)
        assert used == []
    finally:
        os.close(fd)


def test_a_single_cjk_ideograph_is_a_usable_query(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "note.md").write_text("癌 results", encoding="utf-8")
    assert [s.path for s in me.find_measured_sources("癌", root=root, enabled=True).sources] == ["evidence/note.md"]
    assert me._query_tokens("a") == []


def test_invalid_bytes_do_not_join_their_neighbours_into_a_query_term(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "note.md").write_bytes(b"nee\xffdle")
    assert me.find_measured_sources("needle", root=root, enabled=True).status == me.NONE


@pytest.mark.parametrize("bad", [5, ["a"], {"a": 1}, 1.5])
def test_a_non_path_root_is_unavailable_not_an_exception(bad):
    assert me.find_measured_sources("needle", root=bad, enabled=True).status == me.UNAVAILABLE


def test_an_oversized_candidate_makes_the_lookup_incomplete_not_empty(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "needle.md").write_text("x" * 200)
    monkeypatch.setattr(me, "_MAX_CANDIDATE_BYTES", 100)
    assert me.find_measured_sources("needle", root=root, enabled=True).status == me.UNAVAILABLE


def test_partial_results_are_flagged_incomplete_when_part_of_the_tree_failed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence" / "sub").mkdir(parents=True)
    (root / "evidence" / "a.md").write_text("needle")
    (root / "evidence" / "sub" / "b.md").write_text("needle")
    real_open = os.open

    def failing(path, flags, *args, **kwargs):
        if path == "sub" and kwargs.get("dir_fd") is not None:
            raise PermissionError(13, "denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)
    result = me.find_measured_sources("needle", root=root, enabled=True)
    assert result.status == me.OK and result.incomplete and "could not be examined" in result.reason


def test_replacement_expanded_text_is_marked_truncated(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "bad.txt").write_bytes(b"needle " + b"\xff" * (me.MAX_TEXT_BYTES - 7))
    source = me.find_measured_sources("needle", root=root, enabled=True).sources[0]
    assert source.size_bytes == me.MAX_TEXT_BYTES and source.truncated


@pytest.mark.parametrize("value", [5, ["a"], 0, True])
def test_the_root_helper_ignores_non_path_values(monkeypatch, value):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: value)
    assert me.measured_evidence_root() is None


def test_decimal_boundaries_are_judged_as_written():
    measurement = me.Measurement(subject="s", metric="m", value=0.02, unit="u", path="p")
    assertion = me.Assertion("C1", "s", "m", 0.001, "u", "doc")
    report = me.find_conflicts([assertion], [measurement], rel_tolerance=0.95)
    assert len(report.agreements) == 1 and report.conflicts == ()


def test_a_large_json_document_is_not_decoded(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "big.json").write_text('{"needle": [' + "{}," * 400000 + '{}]}')
    source = me.find_measured_sources("needle", root=root, enabled=True).sources[0]
    assert source.measurements == () and source.measurements_capped


def test_a_candidate_that_changes_while_read_is_incomplete_not_empty(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    target = root / "evidence" / "needle.md"
    target.write_bytes(b"x" * 50)
    real_fstat = os.fstat
    calls = []

    def fstat(handle):
        result = real_fstat(handle)
        calls.append(1)
        if len(calls) == 2:  # the first call is the root identity check; the second is the file's
            os.chmod(target, 0o600)
        return result

    monkeypatch.setattr(os, "fstat", fstat)
    assert me.find_measured_sources("needle", root=root, enabled=True).status == me.UNAVAILABLE


def test_a_capped_json_file_does_not_fall_back_to_its_text_prefix(tmp_path):
    root = tmp_path / "root"
    (root / "evidence").mkdir(parents=True)
    head = '{"needle": 1, "measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 1}]'
    (root / "evidence" / "big.json").write_text(head + " " * (me._MAX_JSON_PARSE_BYTES + 10) + "garbage")
    source = me.find_measured_sources("needle", root=root, enabled=True).sources[0]
    assert me.extract_measurements(source) == [] and source.measurements_capped


def test_a_query_cut_inside_a_word_drops_the_partial_word():
    assert me._query_tokens("a " * 998 + "needleSuffix") == []


def test_a_utf8_bom_does_not_discard_the_measurements():
    raw = b'\xef\xbb\xbf{"measurements": [{"subject": "a", "metric": "m", "unit": "u", "value": 3}]}'
    parsed, _ = me._parse_measurements_capped(raw, "p")
    assert [m.subject for m in parsed] == ["a"]
