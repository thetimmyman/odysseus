"""Filesystem controls for immutable historical provider/model offer receipts."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from src.provider_model_offer import (
    UNKNOWN,
    AllowanceEffect,
    CreditEffect,
    OfferProvenance,
    PriceEffect,
    make_provider_model_offer_receipt,
)
from src import provider_model_offer_store as store


def make_offer(*, tariff_version: str = "2026-09-01", offered_rate=0.0, list_rate=2.0):
    return make_provider_model_offer_receipt(
        provider="provider-a",
        pool_id="pool-a",
        capacity_receipt_ref="capacity:" + "a" * 64,
        harness="command-code",
        usage_path="subscription-cli",
        native_model="vendor/model-x",
        tariff_id="standard",
        tariff_version=tariff_version,
        provenance=OfferProvenance(
            "https://provider.example/pricing",
            "b" * 64,
            "provider_pricing_page",
            "2026-09-24T11:00:00Z",
            7200,
        ),
        valid_from="2026-09-01T00:00:00Z",
        valid_until="2026-10-01T00:00:00Z",
        price=PriceEffect("USD", "1M tokens", list_rate, offered_rate),
        credit=CreditEffect("USD", UNKNOWN, "account"),
        allowance=AllowanceEffect("tokens", 0, "weekly"),
    )


def private_store(tmp_path):
    directory = tmp_path / "offers"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    return directory


def object_path(directory, receipt):
    return directory / f"{receipt.receipt_hash}.json"


def reseal_for_parser_control(payload):
    core = {key: value for key, value in payload.items() if key != "receipt_hash"}
    encoded = json.dumps(core, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    payload["receipt_hash"] = hashlib.sha256(encoded).hexdigest()
    return payload


def process_put(directory, payload):
    return store.put_receipt(directory, payload).receipt_hash


def test_unknown_and_zero_round_trip_and_identical_put_is_idempotent(tmp_path):
    directory = private_store(tmp_path)
    receipt = make_offer(list_rate=UNKNOWN, offered_rate=0.0)

    stored = store.put_receipt(directory, receipt)
    path = object_path(directory, receipt)
    original_bytes = path.read_bytes()
    original_stat = path.stat()
    assert stored == receipt
    assert store.get_receipt(directory, receipt.receipt_hash) == receipt
    assert store.get_receipt(directory, receipt.receipt_hash).price.list_rate is UNKNOWN
    assert store.get_receipt(directory, receipt.receipt_hash).price.offered_rate == 0.0
    assert store.get_receipt(directory, receipt.receipt_hash).allowance.amount == 0

    assert store.put_receipt(directory, receipt) == receipt
    assert path.read_bytes() == original_bytes
    assert path.stat().st_ino == original_stat.st_ino
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns


def test_tariff_revision_is_a_new_immutable_object_and_old_bytes_stay_exact(tmp_path):
    directory = private_store(tmp_path)
    old = make_offer(tariff_version="2026-09-01", offered_rate=0.0)
    revised = make_offer(tariff_version="2026-10-01", offered_rate=1.25)
    assert old.receipt_hash != revised.receipt_hash

    store.put_receipt(directory, old)
    old_path = object_path(directory, old)
    old_bytes = old_path.read_bytes()
    store.put_receipt(directory, revised)

    assert store.get_receipt(directory, old.receipt_hash) == old
    assert store.get_receipt(directory, revised.receipt_hash) == revised
    assert old_path.read_bytes() == old_bytes
    assert len(list(directory.glob("*.json"))) == 2


def test_invalid_input_fails_before_any_object_is_created(tmp_path):
    directory = private_store(tmp_path)
    payload = make_offer().to_dict()
    payload["unrecognized"] = "not admitted by the frozen receipt parser"
    reseal_for_parser_control(payload)
    with pytest.raises(store.OfferStorePayloadError):
        store.put_receipt(directory, payload)
    with pytest.raises(store.OfferStorePayloadError):
        store.put_receipt(directory, {"receipt_hash": "0" * 64})
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("bad_hash", ["../escape", "A" * 64, "f" * 63, "g" * 64, ""])
def test_get_rejects_malformed_or_traversing_hash_before_open(tmp_path, bad_hash):
    with pytest.raises(store.OfferStorePathError):
        store.get_receipt(private_store(tmp_path), bad_hash)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"receipt_hash":"x","receipt_hash":"y"}',
        b'{"number":NaN}',
        b'{"receipt_hash":',
        b'{"receipt_hash":"x"}\n',
        b"x" * (store.MAX_RECEIPT_BYTES + 1),
    ],
    ids=["duplicate-key", "nan", "truncated", "noncanonical-newline", "oversize"],
)
def test_get_refuses_corrupt_json_without_repairing_bytes(tmp_path, raw):
    directory = private_store(tmp_path)
    receipt = make_offer()
    target = object_path(directory, receipt)
    target.write_bytes(raw)
    target.chmod(0o600)

    with pytest.raises(store.OfferStoreCorruptError):
        store.get_receipt(directory, receipt.receipt_hash)
    with pytest.raises(store.OfferStoreCorruptError):
        store.put_receipt(directory, receipt)
    assert target.read_bytes() == raw


def test_filename_hash_binding_and_existing_conflict_never_repair_history(tmp_path):
    directory = private_store(tmp_path)
    first = make_offer(tariff_version="2026-09-01")
    second = make_offer(tariff_version="2026-10-01")
    mismatch_path = object_path(directory, first)
    mismatch_path.write_bytes(json.dumps(second.to_dict(), sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False, allow_nan=False).encode())
    mismatch_path.chmod(0o600)
    mismatched_bytes = mismatch_path.read_bytes()
    with pytest.raises(store.OfferStoreCorruptError, match="filename"):
        store.get_receipt(directory, first.receipt_hash)
    with pytest.raises(store.OfferStoreCorruptError, match="filename"):
        store.put_receipt(directory, first)
    assert mismatch_path.read_bytes() == mismatched_bytes

    conflict = make_offer()
    conflict_path = object_path(directory, conflict)
    conflict_path.write_bytes(b"preserve existing invalid bytes")
    conflict_path.chmod(0o600)
    before = conflict_path.read_bytes()
    with pytest.raises(store.OfferStoreCorruptError):
        store.put_receipt(directory, conflict)
    assert conflict_path.read_bytes() == before


def test_stored_extra_fields_are_rejected_and_never_rewritten(tmp_path):
    directory = private_store(tmp_path)
    receipt = make_offer()
    payload = receipt.to_dict()
    payload["extra"] = "unrecognized"
    reseal_for_parser_control(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    target = object_path(directory, receipt)
    target.write_bytes(raw)
    target.chmod(0o600)

    with pytest.raises(store.OfferStoreCorruptError):
        store.get_receipt(directory, receipt.receipt_hash)
    with pytest.raises(store.OfferStoreCorruptError):
        store.put_receipt(directory, receipt)
    assert target.read_bytes() == raw


def test_symlink_fifo_and_directory_leaf_objects_are_refused_without_blocking(tmp_path):
    directory = private_store(tmp_path)
    real = make_offer()
    alias_hash = "c" * 64
    store.put_receipt(directory, real)
    os.symlink(object_path(directory, real).name, directory / f"{alias_hash}.json")
    with pytest.raises(store.OfferStoreCorruptError):
        store.get_receipt(directory, alias_hash)

    fifo_hash = "d" * 64
    os.mkfifo(directory / f"{fifo_hash}.json", 0o600)
    with pytest.raises(store.OfferStoreCorruptError):
        store.get_receipt(directory, fifo_hash)

    directory_hash = "e" * 64
    (directory / f"{directory_hash}.json").mkdir(mode=0o700)
    with pytest.raises(store.OfferStoreCorruptError):
        store.get_receipt(directory, directory_hash)


def test_directory_must_exist_and_not_be_world_or_group_writable(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(store.OfferStorePathError):
        store.put_receipt(missing, make_offer())

    directory = private_store(tmp_path)
    for mode in (0o755, 0o750, 0o770):
        directory.chmod(mode)
        with pytest.raises(store.OfferStorePathError, match="permissions must be private"):
            store.put_receipt(directory, make_offer())
    assert list(directory.iterdir()) == []

    symlink = tmp_path / "store-link"
    os.symlink(directory, symlink)
    directory.chmod(0o700)
    with pytest.raises(store.OfferStorePathError):
        store.put_receipt(symlink, make_offer())


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_receipt_object_with_any_group_or_other_permission_is_refused(tmp_path, mode):
    directory = private_store(tmp_path)
    receipt = make_offer()
    target = object_path(directory, receipt)
    target.write_bytes(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False).encode())
    target.chmod(mode)

    with pytest.raises(store.OfferStoreCorruptError, match="permissions are not private"):
        store.get_receipt(directory, receipt.receipt_hash)
    assert target.stat().st_mode & 0o777 == mode


def test_receipt_chmod_and_restore_during_read_is_detected_by_ctime(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    target = object_path(directory, receipt)
    target.write_bytes(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False).encode())
    target.chmod(0o600)
    real_read = store.os.read
    changed = False

    def chmod_and_restore(fd, count):
        nonlocal changed
        if not changed:
            changed = True
            target.chmod(0o644)
            target.chmod(0o600)
        return real_read(fd, count)

    monkeypatch.setattr(store.os, "read", chmod_and_restore)
    with pytest.raises(store.OfferStoreReadUnstableError, match="contents are unknown"):
        store.get_receipt(directory, receipt.receipt_hash)
    assert changed
    assert target.stat().st_mode & 0o777 == 0o600


def test_link_churn_during_read_is_unknown_then_static_published_object_reads(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    store.put_receipt(directory, receipt)
    target = object_path(directory, receipt)
    transient_link = directory / ".transient-reader-link"
    real_read = store.os.read
    changed = False

    def link_churn_then_read(fd, count):
        nonlocal changed
        if not changed:
            changed = True
            os.link(target, transient_link)
            os.unlink(transient_link)
        return real_read(fd, count)

    monkeypatch.setattr(store.os, "read", link_churn_then_read)
    with pytest.raises(store.OfferStoreReadUnstableError, match="contents are unknown"):
        store.get_receipt(directory, receipt.receipt_hash)
    monkeypatch.setattr(store.os, "read", real_read)
    assert store.get_receipt(directory, receipt.receipt_hash) == receipt


def test_busy_writer_lock_has_bounded_wait_and_typed_refusal(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    held_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    store.fcntl.flock(held_fd, store.fcntl.LOCK_EX | store.fcntl.LOCK_NB)
    monkeypatch.setattr(store, "WRITE_LOCK_TIMEOUT_SECONDS", 0.11)
    started = time.monotonic()
    try:
        with pytest.raises(store.OfferStoreBusyError, match="remained busy"):
            store.put_receipt(directory, receipt)
    finally:
        store.fcntl.flock(held_fd, store.fcntl.LOCK_UN)
        os.close(held_fd)
    assert time.monotonic() - started < 1.0
    assert list(directory.iterdir()) == []


def test_unlock_failure_refuses_success_and_still_closes_directory_fd(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    real_flock = store.fcntl.flock
    directory_fd = None

    def fail_unlock(fd, operation):
        nonlocal directory_fd
        if operation == store.fcntl.LOCK_UN:
            directory_fd = fd
            raise OSError(errno.EIO, "injected unlock failure")
        return real_flock(fd, operation)

    monkeypatch.setattr(store.fcntl, "flock", fail_unlock)
    with pytest.raises(store.OfferStoreIOError, match="cannot unlock"):
        store.put_receipt(directory, receipt)
    assert directory_fd is not None
    with pytest.raises(OSError) as closed:
        os.fstat(directory_fd)
    assert closed.value.errno == errno.EBADF
    # Publication completed, but unlock failure means no success was acknowledged.
    assert store.get_receipt(directory, receipt.receipt_hash) == receipt


def test_partial_staging_write_failure_does_not_publish_or_acknowledge(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    real_write = store.os.write
    calls = 0

    def interrupted_write(fd, buffer):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, buffer[:12])
        raise OSError(errno.EIO, "injected staging write failure")

    monkeypatch.setattr(store.os, "write", interrupted_write)
    with pytest.raises(store.OfferStoreIOError):
        store.put_receipt(directory, receipt)
    assert not object_path(directory, receipt).exists()
    assert list(directory.iterdir()) == []


def test_staging_token_failure_releases_lock_and_directory_fd(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    real_lock = store._lock_directory
    real_token = store.secrets.token_hex
    directory_fd = None

    def capture_lock(fd):
        nonlocal directory_fd
        real_lock(fd)
        directory_fd = fd

    def fail_token(_bytes):
        raise OSError(errno.EIO, "injected staging-token failure")

    monkeypatch.setattr(store, "_lock_directory", capture_lock)
    monkeypatch.setattr(store.secrets, "token_hex", fail_token)
    with pytest.raises(store.OfferStoreIOError, match="staging-token failure"):
        store.put_receipt(directory, receipt)

    assert directory_fd is not None
    with pytest.raises(OSError) as closed:
        os.fstat(directory_fd)
    assert closed.value.errno == errno.EBADF
    assert list(directory.iterdir()) == []

    monkeypatch.setattr(store.secrets, "token_hex", real_token)
    assert store.put_receipt(directory, receipt) == receipt


def test_file_sync_link_and_directory_sync_failures_never_return_success(tmp_path, monkeypatch):
    receipt = make_offer()

    directory = private_store(tmp_path / "file-sync")
    real_fsync = store._fsync_fd

    def fail_staging_sync(fd, description):
        if description == "staged receipt file":
            raise store.OfferStoreDurabilityError("injected file sync failure")
        return real_fsync(fd, description)

    monkeypatch.setattr(store, "_fsync_fd", fail_staging_sync)
    with pytest.raises(store.OfferStoreDurabilityError):
        store.put_receipt(directory, receipt)
    assert not object_path(directory, receipt).exists()
    assert list(directory.iterdir()) == []

    monkeypatch.setattr(store, "_fsync_fd", real_fsync)
    link_directory = private_store(tmp_path / "link")
    real_link = store.os.link

    def fail_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "injected unsupported link")

    monkeypatch.setattr(store.os, "link", fail_link)
    with pytest.raises(store.OfferStoreIOError):
        store.put_receipt(link_directory, receipt)
    assert not object_path(link_directory, receipt).exists()
    assert list(link_directory.iterdir()) == []

    monkeypatch.setattr(store.os, "link", real_link)
    dirsync_directory = private_store(tmp_path / "dir-sync")

    def fail_directory_sync(fd, description):
        if description == "store directory after publication":
            raise store.OfferStoreDurabilityError("injected directory sync failure")
        return real_fsync(fd, description)

    monkeypatch.setattr(store, "_fsync_fd", fail_directory_sync)
    with pytest.raises(store.OfferStoreDurabilityError):
        store.put_receipt(dirsync_directory, receipt)
    # Publication may be visible after a failed durability acknowledgement,
    # but it is always complete and parseable; the writer never returned it.
    assert object_path(dirsync_directory, receipt).read_bytes() == json.dumps(
        receipt.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    assert store.get_receipt(dirsync_directory, receipt.receipt_hash) == receipt


def test_idempotent_path_requires_file_and_directory_sync(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    store.put_receipt(directory, receipt)
    target = object_path(directory, receipt)
    before = target.read_bytes()
    real_fsync = store._fsync_fd

    def fail_existing_file_sync(fd, description):
        if description == "stored receipt file":
            raise store.OfferStoreDurabilityError("injected existing file sync failure")
        return real_fsync(fd, description)

    monkeypatch.setattr(store, "_fsync_fd", fail_existing_file_sync)
    with pytest.raises(store.OfferStoreDurabilityError):
        store.put_receipt(directory, receipt)
    assert target.read_bytes() == before

    def fail_idempotent_directory_sync(fd, description):
        if description == "store directory after publication":
            raise store.OfferStoreDurabilityError("injected idempotent directory sync failure")
        return real_fsync(fd, description)

    monkeypatch.setattr(store, "_fsync_fd", fail_idempotent_directory_sync)
    with pytest.raises(store.OfferStoreDurabilityError):
        store.put_receipt(directory, receipt)
    assert target.read_bytes() == before


def test_readers_see_missing_then_complete_object_never_partial_publication(tmp_path, monkeypatch):
    directory = private_store(tmp_path)
    receipt = make_offer()
    staged = threading.Event()
    publish = threading.Event()
    published = threading.Event()
    finish = threading.Event()
    real_fsync = store._fsync_fd

    def pause_around_publication(fd, description):
        real_fsync(fd, description)
        if description == "staged receipt file":
            staged.set()
            assert publish.wait(timeout=5)
        elif description == "stored receipt file":
            published.set()
            assert finish.wait(timeout=5)

    monkeypatch.setattr(store, "_fsync_fd", pause_around_publication)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.put_receipt, directory, receipt)
        assert staged.wait(timeout=5)
        with pytest.raises(store.OfferStoreNotFoundError):
            store.get_receipt(directory, receipt.receipt_hash)
        publish.set()
        assert published.wait(timeout=5)
        # The atomic hard link exposes the already complete synced object.
        assert store.get_receipt(directory, receipt.receipt_hash) == receipt
        finish.set()
        assert future.result(timeout=5) == receipt

    assert store.get_receipt(directory, receipt.receipt_hash) == receipt


def test_concurrent_same_and_different_receipts_preserve_every_exact_object(tmp_path):
    directory = private_store(tmp_path)
    shared = make_offer()
    old = make_offer(tariff_version="2026-08-01", offered_rate=0.0)
    revised = make_offer(tariff_version="2026-10-01", offered_rate=1.25)
    assert old.receipt_hash != revised.receipt_hash

    with ThreadPoolExecutor(max_workers=10) as pool:
        same = list(pool.map(lambda _: store.put_receipt(directory, shared), range(8)))
    assert same == [shared] * 8
    shared_path = object_path(directory, shared)
    shared_bytes = shared_path.read_bytes()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda receipt: store.put_receipt(directory, receipt), [old, revised]))
    assert results == [old, revised]
    assert shared_path.read_bytes() == shared_bytes
    assert store.get_receipt(directory, old.receipt_hash) == old
    assert store.get_receipt(directory, revised.receipt_hash) == revised
    assert store.get_receipt(directory, shared.receipt_hash) == shared
    assert len(list(directory.glob("*.json"))) == 3


def test_concurrent_process_writers_publish_one_complete_identical_object(tmp_path):
    directory = private_store(tmp_path)
    receipt = make_offer()
    payload = receipt.to_dict()
    with ProcessPoolExecutor(max_workers=4) as pool:
        hashes = list(pool.map(process_put, [str(directory)] * 8, [payload] * 8))
    assert hashes == [receipt.receipt_hash] * 8
    assert store.get_receipt(directory, receipt.receipt_hash) == receipt
    assert len(list(directory.glob("*.json"))) == 1
