"""CalDAV account passwords must never sit in plaintext in data/user_prefs.json.

Background (POS-AI-14 follow-up, 2026-08-25 cross-stack audit): every
*interactive* CalDAV write path already encrypted, but the legacy
``caldav`` -> ``caldav_accounts`` migration in ``_load_caldav_accounts``
copied ``legacy["password"]`` verbatim. Because ``decrypt()`` passes
plaintext straight through, the resulting plaintext value was read happily
forever and nothing ever re-encrypted it — so a live deployment ended up with
a world-readable plaintext password on disk while the code "supported
encryption".

These tests pin both halves: the migration must encrypt, and an account
already stored plaintext must be upgraded in place on the next load.
"""
import json
import os
import stat
import sys

import pytest


@pytest.fixture
def prefs_env(tmp_path, monkeypatch):
    """Point prefs storage and the Fernet key at a tmp dir, freshly keyed."""
    prefs_file = tmp_path / "user_prefs.json"

    from routes import prefs_routes
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    from src import secret_storage
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)

    yield prefs_file

    # Drop the module-level Fernet so a later test cannot inherit this key.
    monkeypatch.setattr(secret_storage, "_fernet", None, raising=False)


def _write_prefs(prefs_file, payload):
    prefs_file.write_text(json.dumps(payload), encoding="utf-8")


# ── the migration hole ────────────────────────────────────────────────────


def test_legacy_migration_encrypts_password(prefs_env):
    """legacy `caldav` -> `caldav_accounts` must not carry plaintext across."""
    from src.caldav_sync import _load_caldav_accounts

    _write_prefs(prefs_env, {"_users": {"alice": {"caldav": {
        "url": "https://dav.example/",
        "username": "alice",
        "password": "legacy-plaintext-pw",
    }}}})

    accounts = _load_caldav_accounts("alice")

    assert len(accounts) == 1
    assert accounts[0]["password"].startswith("enc:")

    on_disk = prefs_env.read_text(encoding="utf-8")
    assert "legacy-plaintext-pw" not in on_disk
    assert "caldav" not in json.loads(on_disk)["_users"]["alice"]

    from src.secret_storage import decrypt
    assert decrypt(accounts[0]["password"]) == "legacy-plaintext-pw"


# ── the existing-value upgrade ────────────────────────────────────────────


def test_existing_plaintext_account_is_upgraded_in_place(prefs_env):
    """An account already stored plaintext is encrypted on the next load."""
    from src.caldav_sync import _load_caldav_accounts

    _write_prefs(prefs_env, {"_users": {"alice": {"caldav_accounts": [{
        "id": "acct-1",
        "label": "CalDAV",
        "url": "https://dav.example/",
        "username": "alice",
        "password": "sixteencharpwd!!",
    }]}}})

    accounts = _load_caldav_accounts("alice")

    assert accounts[0]["password"].startswith("enc:")
    assert accounts[0]["id"] == "acct-1", "the account id must be preserved"

    on_disk = prefs_env.read_text(encoding="utf-8")
    assert "sixteencharpwd!!" not in on_disk, "plaintext still on disk"
    stored = json.loads(on_disk)["_users"]["alice"]["caldav_accounts"][0]
    assert stored["password"].startswith("enc:"), "upgrade was not persisted"


def test_upgrade_is_idempotent(prefs_env):
    """A second load must not double-encrypt or rewrite."""
    from src.caldav_sync import _load_caldav_accounts

    _write_prefs(prefs_env, {"_users": {"alice": {"caldav_accounts": [{
        "id": "acct-1", "url": "https://dav.example/",
        "username": "alice", "password": "plain-pw",
    }]}}})

    first = _load_caldav_accounts("alice")
    after_first = prefs_env.read_text(encoding="utf-8")
    second = _load_caldav_accounts("alice")

    assert second[0]["password"] == first[0]["password"]
    assert prefs_env.read_text(encoding="utf-8") == after_first

    from src.secret_storage import decrypt
    assert decrypt(second[0]["password"]) == "plain-pw"


def test_other_users_prefs_are_not_clobbered(prefs_env):
    """The upgrade writes through the per-user path — a sibling user survives."""
    from src.caldav_sync import _load_caldav_accounts

    _write_prefs(prefs_env, {"_users": {
        "alice": {"theme": "dark", "caldav_accounts": [{
            "id": "a1", "url": "https://dav.example/",
            "username": "alice", "password": "plain-pw",
        }]},
        "lacey": {"theme": "light"},
    }})

    _load_caldav_accounts("alice")

    stored = json.loads(prefs_env.read_text(encoding="utf-8"))["_users"]
    assert stored["lacey"] == {"theme": "light"}
    assert stored["alice"]["theme"] == "dark"


def test_multiple_accounts_all_encrypted(prefs_env):
    from src.caldav_sync import _load_caldav_accounts

    _write_prefs(prefs_env, {"_users": {"alice": {"caldav_accounts": [
        {"id": "a1", "url": "https://one.example/", "username": "a", "password": "pw-one"},
        {"id": "a2", "url": "https://two.example/", "username": "b", "password": "pw-two"},
    ]}}})

    accounts = _load_caldav_accounts("alice")

    assert all(a["password"].startswith("enc:") for a in accounts)
    on_disk = prefs_env.read_text(encoding="utf-8")
    assert "pw-one" not in on_disk and "pw-two" not in on_disk


def test_empty_password_left_alone(prefs_env):
    """An unconfigured account must not gain an `enc:` blob for the empty string."""
    from src.caldav_sync import _load_caldav_accounts

    _write_prefs(prefs_env, {"_users": {"alice": {"caldav_accounts": [
        {"id": "a1", "url": "https://one.example/", "username": "a", "password": ""},
    ]}}})

    accounts = _load_caldav_accounts("alice")
    assert accounts[0]["password"] == ""


# ── fail-safe ─────────────────────────────────────────────────────────────


def test_broken_key_leaves_plaintext_rather_than_bricking_sync(prefs_env, monkeypatch):
    """If encryption cannot round-trip, keep the working plaintext credential.

    Swapping in a token we cannot decrypt would silently destroy calendar sync
    (decrypt() returns "" on InvalidToken, which reads as 'unconfigured').
    """
    from src import secret_storage
    from src.caldav_sync import _load_caldav_accounts

    monkeypatch.setattr(secret_storage, "decrypt", lambda value: "")

    _write_prefs(prefs_env, {"_users": {"alice": {"caldav_accounts": [
        {"id": "a1", "url": "https://one.example/", "username": "a", "password": "plain-pw"},
    ]}}})

    accounts = _load_caldav_accounts("alice")
    assert accounts[0]["password"] == "plain-pw"


def test_encrypt_helper_is_a_noop_on_encrypted_values(prefs_env):
    from src.caldav_sync import _encrypt_account_passwords
    from src.secret_storage import encrypt

    token = encrypt("already-secret")
    out, changed = _encrypt_account_passwords([{"id": "a1", "password": token}])

    assert changed is False
    assert out[0]["password"] == token


# ── at-rest file permissions ──────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_prefs_file_written_0600(prefs_env):
    """user_prefs.json holds credentials — it must not be world-readable.

    Regression guard for the live finding: the file was mode 0644 on the
    framework deployment. A chmod applied by hand does not survive, because
    `_save` replaces the inode; the mode has to be set by the writer.
    """
    from routes.prefs_routes import _save_for_user

    _save_for_user("alice", {"theme": "dark"})

    mode = stat.S_IMODE(os.stat(prefs_env).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_prefs_save_reclaims_loosened_mode(prefs_env):
    """A 0644 file left behind by an out-of-band root write self-heals."""
    from routes.prefs_routes import _save_for_user

    _save_for_user("alice", {"theme": "dark"})
    os.chmod(prefs_env, 0o644)

    _save_for_user("alice", {"theme": "light"})

    mode = stat.S_IMODE(os.stat(prefs_env).st_mode)
    assert mode == 0o600, f"expected 0600 after re-save, got {oct(mode)}"
