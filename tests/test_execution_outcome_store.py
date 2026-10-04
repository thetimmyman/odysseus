"""Real temporary-directory controls for immutable execution outcome history."""
from __future__ import annotations

import errno
import base64
import fcntl
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.execution_outcomes import build_outcome_record
from src import execution_outcome_store as store
from test_execution_outcomes import make_evidence, make_facts


MAX = 2 * 1024 * 1024


def private_store(tmp_path):
    directory = tmp_path / "outcomes"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def make_record(tmp_path, *, run_id="synthetic-store-run", prompt_tokens=0):
    evidence, extensions, _, _, _, _ = make_evidence(
        tmp_path / f"repo-{run_id}", run_id=run_id, prompt_tokens=prompt_tokens)
    return build_outcome_record(evidence_package=evidence,
                                companion_facts=make_facts(evidence, prompt_zero=prompt_tokens == 0),
                                artifact_extensions=extensions)


def path_for(directory, record_hash):
    return directory / f"{record_hash}.json"


def assert_no_staging(directory):
    assert not list(directory.glob(".*.tmp"))


def test_build_put_get_and_explicit_replay_preserve_unknown_absent_and_zero(tmp_path):
    directory = private_store(tmp_path)
    record = make_record(tmp_path, prompt_tokens=0)
    raw = record.to_bytes()
    stored = store.put_record(directory, record, max_record_bytes=MAX)
    target = path_for(directory, record.raw_record_hash)
    original_stat = target.stat()
    assert stored.to_bytes() == raw
    assert target.read_bytes() == raw
    assert store.put_record(directory, record, max_record_bytes=MAX).to_bytes() == raw
    assert target.stat().st_ino == original_stat.st_ino

    loaded = store.get_record(directory, record.raw_record_hash, max_record_bytes=MAX)
    assert loaded.to_bytes() == raw
    view = loaded.to_dict()
    metrics = view["companion_facts"]["metrics"]
    assert metrics["prompt_tokens"][0]["value"] == 0
    assert metrics["completion_tokens"][0]["value"] == 9
    assert metrics["elapsed_s"][0]["value"] == 12.5
    assert metrics["ttft_s"][0]["status"] == "UNKNOWN"
    assert metrics["realized_cost"][0]["status"] == "ABSENT"

    first = store.aggregate_stored_outcomes(
        directory, [record.raw_record_hash, record.raw_record_hash],
        scoring_version="replay-a", max_record_bytes=MAX)
    second = store.aggregate_stored_outcomes(
        directory, (record.raw_record_hash,), scoring_version="replay-b",
        max_record_bytes=MAX)
    assert first["scoring_version"] == "replay-a"
    assert first["source_record_hashes"] == [record.raw_record_hash]
    assert first["arms"][0]["distinct_execution_n"] == 1
    assert first["confidence_status"] == "INSUFFICIENT"
    assert first["ranking_allowed"] is False
    assert second["scoring_version"] == "replay-b"
    assert target.read_bytes() == raw
    assert target.stat().st_ino == original_stat.st_ino
    assert target.stat().st_mtime_ns == original_stat.st_mtime_ns


def test_input_validation_and_explicit_byte_bound_precede_filesystem_mutation(tmp_path):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    with pytest.raises(store.OutcomeStorePathError):
        store.put_record(directory, record, max_record_bytes=True)
    with pytest.raises(store.OutcomeStorePathError):
        store.put_record(directory, record, max_record_bytes=0)
    with pytest.raises(store.OutcomeStorePayloadError):
        store.put_record(directory, record.to_bytes() + b"\n", max_record_bytes=MAX)
    with pytest.raises(store.OutcomeStorePayloadError):
        store.put_record(directory, record.to_bytes(), max_record_bytes=len(record.to_bytes()) - 1)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("bad_hash", ["../escape", "A" * 64, "g" * 64, "f" * 63, ""])
def test_malformed_hashes_refuse_without_path_traversal(tmp_path, bad_hash):
    directory = private_store(tmp_path)
    with pytest.raises(store.OutcomeStorePathError):
        store.get_record(directory, bad_hash, max_record_bytes=MAX)
    with pytest.raises(store.OutcomeStorePathError):
        store.aggregate_stored_outcomes(directory, [bad_hash], scoring_version="v1",
                                        max_record_bytes=MAX)
    assert list(directory.iterdir()) == []


def test_exact_missing_hash_is_typed_and_never_falls_back(tmp_path):
    directory = private_store(tmp_path)
    with pytest.raises(store.OutcomeStoreNotFoundError):
        store.get_record(directory, "f" * 64, max_record_bytes=MAX)
    assert list(directory.iterdir()) == []


def test_directory_must_be_existing_owned_private_real_directory(tmp_path):
    missing = tmp_path / "missing"
    record = make_record(tmp_path)
    with pytest.raises(store.OutcomeStorePathError):
        store.put_record(missing, record, max_record_bytes=MAX)
    directory = private_store(tmp_path)
    for mode in (0o755, 0o750, 0o770):
        directory.chmod(mode)
        with pytest.raises(store.OutcomeStorePathError):
            store.put_record(directory, record, max_record_bytes=MAX)
    directory.chmod(0o700)
    alias = tmp_path / "directory-alias"
    os.symlink(directory, alias)
    with pytest.raises(store.OutcomeStorePathError):
        store.get_record(alias, record.raw_record_hash, max_record_bytes=MAX)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_record_object_permissions_are_private_and_never_repaired(tmp_path, mode):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    target = path_for(directory, record.raw_record_hash)
    target.write_bytes(record.to_bytes())
    target.chmod(mode)
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.get_record(directory, record.raw_record_hash, max_record_bytes=MAX)
    assert target.stat().st_mode & 0o777 == mode


def test_symlink_fifo_directory_and_hardlink_leafs_are_refused(tmp_path):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    store.put_record(directory, record, max_record_bytes=MAX)
    symlink_hash = "a" * 64
    os.symlink(path_for(directory, record.raw_record_hash).name,
               path_for(directory, symlink_hash))
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.get_record(directory, symlink_hash, max_record_bytes=MAX)
    fifo_hash = "b" * 64
    os.mkfifo(path_for(directory, fifo_hash), 0o600)
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.get_record(directory, fifo_hash, max_record_bytes=MAX)
    directory_hash = "c" * 64
    path_for(directory, directory_hash).mkdir(mode=0o700)
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.get_record(directory, directory_hash, max_record_bytes=MAX)
    linked_hash = "d" * 64
    os.link(path_for(directory, record.raw_record_hash), path_for(directory, linked_hash))
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.get_record(directory, linked_hash, max_record_bytes=MAX)


@pytest.mark.parametrize("bad", [
    b'{"x":1,"x":2}', b'{"x":NaN}', b'{"broken":', b'{}\n',
])
def test_noncanonical_duplicate_nonfinite_and_truncated_objects_are_not_repaired(tmp_path, bad):
    directory = private_store(tmp_path)
    target = path_for(directory, "e" * 64)
    target.write_bytes(bad)
    target.chmod(0o600)
    before = target.read_bytes()
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.get_record(directory, "e" * 64, max_record_bytes=MAX)
    assert target.read_bytes() == before


def test_oversized_stored_object_is_refused_from_bounded_metadata(tmp_path):
    directory = private_store(tmp_path)
    target = path_for(directory, "e" * 64)
    target.write_bytes(b"x" * (MAX + 1))
    target.chmod(0o600)
    with pytest.raises(store.OutcomeStoreCorruptError, match="byte bound"):
        store.get_record(directory, "e" * 64, max_record_bytes=MAX)
    assert target.stat().st_size == MAX + 1


def test_existing_bad_bytes_are_never_overwritten_by_put(tmp_path):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    target = path_for(directory, record.raw_record_hash)
    target.write_bytes(b"damaged legacy bytes")
    target.chmod(0o600)
    before = target.read_bytes()
    with pytest.raises(store.OutcomeStoreCorruptError):
        store.put_record(directory, record, max_record_bytes=MAX)
    assert target.read_bytes() == before


def test_unstable_read_is_typed_and_a_later_static_read_can_succeed(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    store.put_record(directory, record, max_record_bytes=MAX)
    target = path_for(directory, record.raw_record_hash)
    real_read = store.os.read
    changed = False

    def chmod_restore_during_read(fd, count):
        nonlocal changed
        data = real_read(fd, count)
        if data and not changed:
            changed = True
            target.chmod(0o640)
            target.chmod(0o600)
        return data

    monkeypatch.setattr(store.os, "read", chmod_restore_during_read)
    with pytest.raises(store.OutcomeStoreUnstableError):
        store.get_record(directory, record.raw_record_hash, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "read", real_read)
    assert store.get_record(directory, record.raw_record_hash,
                            max_record_bytes=MAX).to_bytes() == record.to_bytes()


def test_writer_lock_contention_has_a_bounded_typed_refusal(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    monkeypatch.setattr(store, "WRITE_LOCK_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(store, "WRITE_LOCK_POLL_INTERVAL_SECONDS", 0.01)
    held_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(store.OutcomeStoreBusyError):
            store.put_record(directory, record, max_record_bytes=MAX)
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)
    assert list(directory.iterdir()) == []


def test_concurrent_identical_puts_publish_one_immutable_object(tmp_path):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.put_record(directory, record,
                                                            max_record_bytes=MAX), range(8)))
    target = path_for(directory, record.raw_record_hash)
    assert all(item.to_bytes() == record.to_bytes() for item in results)
    assert target.read_bytes() == record.to_bytes()
    assert target.stat().st_nlink == 1
    assert len(list(directory.glob("*.json"))) == 1
    assert_no_staging(directory)


def _install_equal_competing_destination(directory, record, monkeypatch):
    real_link = store.os.link
    real_fsync = store.os.fsync
    directory_syncs = []
    link_calls = 0

    def compete_with_link(source, destination, *, src_dir_fd, dst_dir_fd,
                         follow_symlinks=False):
        nonlocal link_calls
        link_calls += 1
        assert link_calls == 1
        target_fd = store.os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600, dir_fd=dst_dir_fd)
        try:
            store.os.write(target_fd, record.to_bytes())
        finally:
            store.os.close(target_fd)
        raise FileExistsError(errno.EEXIST, "synthetic equal-byte competing publisher")

    def record_syncs(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "link", compete_with_link)
    monkeypatch.setattr(store.os, "fsync", record_syncs)
    return real_link, real_fsync, directory_syncs


def test_equal_competing_destination_syncs_publication_and_staging_cleanup(
        tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path, run_id="synthetic-equal-destination-race")
    _, real_fsync, directory_syncs = _install_equal_competing_destination(
        directory, record, monkeypatch)

    stored = store.put_record(directory, record, max_record_bytes=MAX)

    monkeypatch.setattr(store.os, "fsync", real_fsync)
    target = path_for(directory, record.raw_record_hash)
    assert stored.to_bytes() == record.to_bytes()
    assert target.read_bytes() == record.to_bytes()
    assert len(directory_syncs) == 2  # destination link, then staging unlink
    assert_no_staging(directory)


@pytest.mark.parametrize("directory_sync_to_fail", [1, 2])
def test_equal_competing_destination_directory_sync_failure_refuses_without_losing_history(
        tmp_path, monkeypatch, directory_sync_to_fail):
    directory = private_store(tmp_path)
    record = make_record(tmp_path, run_id="synthetic-equal-destination-fsync-failure")
    real_fsync = store.os.fsync
    _, _, directory_syncs = _install_equal_competing_destination(
        directory, record, monkeypatch)

    def fail_collision_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append(fd)
            if len(directory_syncs) == directory_sync_to_fail:
                raise OSError(errno.EIO, "injected collision-branch directory sync failure")
            return real_fsync(fd)
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", fail_collision_directory_sync)
    with pytest.raises(store.OutcomeStoreDurabilityError):
        store.put_record(directory, record, max_record_bytes=MAX)

    monkeypatch.setattr(store.os, "fsync", real_fsync)
    target = path_for(directory, record.raw_record_hash)
    assert target.read_bytes() == record.to_bytes()
    assert store.get_record(directory, record.raw_record_hash,
                            max_record_bytes=MAX).to_bytes() == record.to_bytes()
    assert_no_staging(directory)


def test_nul_directory_paths_refuse_with_typed_error_before_filesystem_open(
        tmp_path, monkeypatch):
    directory = private_store(tmp_path)

    class NulPathLike:
        def __fspath__(self):
            return str(directory) + "\x00suffix"

    real_open = store.os.open
    calls = 0

    def count_open(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(store.os, "open", count_open)
    for bad_path in (str(directory) + "\x00suffix", NulPathLike()):
        with pytest.raises(store.OutcomeStorePathError):
            store.get_record(bad_path, "f" * 64, max_record_bytes=MAX)
    assert calls == 0
    assert list(directory.iterdir()) == []


def test_staging_fsync_failure_refuses_before_publication_and_cleans_owned_stage(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    real_fsync = store.os.fsync

    def fail_first_fsync(fd):
        raise OSError(errno.EIO, "injected staging fsync failure")

    monkeypatch.setattr(store.os, "fsync", fail_first_fsync)
    with pytest.raises(store.OutcomeStoreDurabilityError):
        store.put_record(directory, record, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "fsync", real_fsync)
    assert not path_for(directory, record.raw_record_hash).exists()
    assert_no_staging(directory)


def test_partial_staging_write_failure_refuses_without_publishing(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    real_write = store.os.write
    calls = 0

    def fail_second_write(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[:max(1, len(payload) // 3)])
        raise OSError(errno.ENOSPC, "injected partial staging write failure")

    monkeypatch.setattr(store.os, "write", fail_second_write)
    with pytest.raises(store.OutcomeStoreIOError):
        store.put_record(directory, record, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "write", real_write)
    assert not path_for(directory, record.raw_record_hash).exists()
    assert_no_staging(directory)


def test_post_publication_directory_sync_failure_never_acknowledges_or_deletes_history(
        tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    real_fsync = store.os.fsync
    calls = 0

    def fail_second_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", fail_second_sync)
    with pytest.raises(store.OutcomeStoreDurabilityError):
        store.put_record(directory, record, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "fsync", real_fsync)
    target = path_for(directory, record.raw_record_hash)
    assert target.read_bytes() == record.to_bytes()
    assert store.get_record(directory, record.raw_record_hash,
                            max_record_bytes=MAX).to_bytes() == record.to_bytes()
    assert_no_staging(directory)


def test_create_link_failure_refuses_without_publishing_or_external_cleanup(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    external = tmp_path / "external"
    external.write_bytes(b"keep")
    record = make_record(tmp_path)
    real_link = store.os.link

    def fail_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "injected link failure")

    monkeypatch.setattr(store.os, "link", fail_link)
    with pytest.raises(store.OutcomeStoreIOError):
        store.put_record(directory, record, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "link", real_link)
    assert not path_for(directory, record.raw_record_hash).exists()
    assert external.read_bytes() == b"keep"
    assert_no_staging(directory)


def test_cleanup_failure_after_publication_refuses_but_preserves_object(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    real_unlink = store.os.unlink
    calls = 0

    def fail_first_unlink(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "injected staging unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(store.os, "unlink", fail_first_unlink)
    with pytest.raises(store.OutcomeStoreIOError):
        store.put_record(directory, record, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "unlink", real_unlink)
    assert path_for(directory, record.raw_record_hash).read_bytes() == record.to_bytes()
    assert_no_staging(directory)


def test_idempotent_put_requires_existing_object_file_sync(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    record = make_record(tmp_path)
    store.put_record(directory, record, max_record_bytes=MAX)
    target = path_for(directory, record.raw_record_hash)
    before = target.read_bytes()
    real_fsync = store.os.fsync

    def fail_file_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected existing object fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", fail_file_sync)
    with pytest.raises(store.OutcomeStoreDurabilityError):
        store.put_record(directory, record, max_record_bytes=MAX)
    monkeypatch.setattr(store.os, "fsync", real_fsync)
    assert target.read_bytes() == before
    assert store.get_record(directory, record.raw_record_hash,
                            max_record_bytes=MAX).to_bytes() == before


def test_replay_refuses_missing_corrupt_and_conflicting_selection_without_partial_scorecard(tmp_path):
    directory = private_store(tmp_path)
    record = make_record(tmp_path, run_id="synthetic-replay-conflict")
    store.put_record(directory, record, max_record_bytes=MAX)
    with pytest.raises(store.OutcomeStorePartialReplayError):
        store.aggregate_stored_outcomes(directory,
            [record.raw_record_hash, "f" * 64], scoring_version="v1", max_record_bytes=MAX)

    changed = record.to_dict()
    changed["companion_facts"]["task_class"]["reason"] = "distinct unknown reason"
    changed_record = build_outcome_record(
        evidence_package=changed["raw_evidence_package"],
        companion_facts=changed["companion_facts"],
        artifact_extensions={key: base64.b64decode(value, validate=True)
                             for key, value in changed["artifact_extensions_b64"].items()})
    assert changed_record.execution_key == record.execution_key
    assert changed_record.raw_record_hash != record.raw_record_hash
    store.put_record(directory, changed_record, max_record_bytes=MAX)
    before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
    with pytest.raises(store.OutcomeStorePartialReplayError):
        store.aggregate_stored_outcomes(directory,
            [record.raw_record_hash, changed_record.raw_record_hash],
            scoring_version="v1", max_record_bytes=MAX)
    assert {p.name: p.read_bytes() for p in directory.glob("*.json")} == before


def test_replay_requires_explicit_finite_hash_selection_and_scoring_version(tmp_path):
    directory = private_store(tmp_path)
    with pytest.raises(store.OutcomeStorePathError):
        store.aggregate_stored_outcomes(directory, (), scoring_version="v1", max_record_bytes=MAX)
    with pytest.raises(store.OutcomeStorePathError):
        store.aggregate_stored_outcomes(directory, iter(["a" * 64]),
                                        scoring_version="v1", max_record_bytes=MAX)
    with pytest.raises(store.OutcomeStorePathError):
        store.aggregate_stored_outcomes(directory, ["a" * 64],
                                        scoring_version=" ", max_record_bytes=MAX)
