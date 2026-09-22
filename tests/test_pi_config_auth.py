import json
from pathlib import Path

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
