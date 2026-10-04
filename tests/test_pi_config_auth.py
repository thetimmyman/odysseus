import json
from pathlib import Path
import stat

import pytest

from src.pi_config import ensure_pi_provider_auth


def test_provider_auth_is_written_and_merged_in_isolated_dir(tmp_path, monkeypatch):
    isolated = tmp_path / "agent"
    monkeypatch.setenv("ODYSSEUS_PI_AGENT_DIR", str(isolated))
    first = {"token": "first-secret", "expires": 123}
    second = {"token": "second-secret"}

    path = ensure_pi_provider_auth("clinepass", first)
    assert json.loads(Path(path).read_text()) == {"clinepass": first}

    second_path = ensure_pi_provider_auth("commandcode", second)
    assert second_path == path
    assert json.loads(Path(path).read_text()) == {
        "clinepass": first,
        "commandcode": second,
    }


def test_provider_auth_refuses_operator_agent_dir_without_isolation(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_PI_AGENT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with pytest.raises(ValueError, match="isolated ODYSSEUS_PI_AGENT_DIR"):
        ensure_pi_provider_auth("clinepass", {"token": "secret"})

    assert not (tmp_path / "home" / ".pi" / "agent" / "auth.json").exists()


def test_provider_auth_returned_path_stays_inside_isolated_dir(tmp_path, monkeypatch):
    isolated = tmp_path / "managed-agent"
    monkeypatch.setenv("ODYSSEUS_PI_AGENT_DIR", str(isolated))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    path = Path(ensure_pi_provider_auth("clinepass", {"token": "secret"}))

    assert path.parent == isolated
    assert path != Path.home() / ".pi" / "agent" / "auth.json"


def test_provider_auth_is_private_and_atomic_in_existing_isolated_dir(tmp_path, monkeypatch):
    isolated = tmp_path / "managed-agent"
    isolated.mkdir(mode=0o755)
    monkeypatch.setenv("ODYSSEUS_PI_AGENT_DIR", str(isolated))
    path = Path(ensure_pi_provider_auth("clinepass", {"token": "secret"}))
    assert stat.S_IMODE(isolated.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"clinepass": {"token": "secret"}}
    assert not list(isolated.glob(".auth-*.tmp"))


def test_provider_auth_refuses_operator_dir_even_explicit_or_configured(tmp_path, monkeypatch):
    operator_dir = tmp_path / "home" / ".pi" / "agent"
    operator_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ODYSSEUS_PI_AGENT_DIR", str(operator_dir))
    with pytest.raises(ValueError, match="operator or shared directory"):
        ensure_pi_provider_auth("clinepass", {"token": "secret"})
    monkeypatch.delenv("ODYSSEUS_PI_AGENT_DIR")
    with pytest.raises(ValueError, match="operator or shared directory"):
        ensure_pi_provider_auth("clinepass", {"token": "secret"}, directory=str(operator_dir))
    assert not (operator_dir / "auth.json").exists()


def test_provider_auth_refuses_symlinked_directory_or_auth_file(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ensure_pi_provider_auth("clinepass", {"token": "secret"}, directory=str(linked))

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (isolated / "auth.json").symlink_to(outside)
    with pytest.raises(OSError):
        ensure_pi_provider_auth("clinepass", {"token": "secret"}, directory=str(isolated))
    assert json.loads(outside.read_text()) == {}
