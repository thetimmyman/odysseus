"""Test that APIKeyManager.save() uses atomic write to prevent data loss."""
import os
import json
import pytest
from unittest.mock import patch, mock_open
from src.api_key_manager import APIKeyManager


def test_save_creates_atomic_tmp_file(tmp_path):
    """Verify save() writes to a temp file and replaces atomically."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-test")

    assert os.path.exists(mgr.api_keys_file)
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    assert "openai" in keys

    tmp_file = mgr.api_keys_file + ".tmp"
    assert not os.path.exists(tmp_file)


def test_save_preserves_existing_keys_atomically(tmp_path):
    """Verify atomic save doesn't corrupt other providers' keys."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-openai")
    mgr.save("anthropic", "sk-anthropic")

    loaded = mgr.load()
    assert loaded["openai"] == "sk-openai"
    assert loaded["anthropic"] == "sk-anthropic"


def test_save_preserves_original_on_write_failure(tmp_path):
    """If the temp file write fails, the original keys file must survive intact."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-original")

    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            mgr.save("anthropic", "sk-new")

    loaded = mgr.load()
    assert loaded == {"openai": "sk-original"}
    assert "anthropic" not in loaded


def test_save_cleans_up_tmp_on_failure(tmp_path):
    """Temp file should be removed if the write fails."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-original")

    tmp_file = mgr.api_keys_file + ".tmp"

    original_open = open

    def failing_open(*args, **kwargs):
        f = original_open(*args, **kwargs)
        if args and isinstance(args[0], str) and args[0].endswith(".tmp"):
            f.close()
            raise OSError("simulated write failure")
        return f

    with patch("builtins.open", side_effect=failing_open):
        with pytest.raises(OSError):
            mgr.save("anthropic", "sk-new")

    assert not os.path.exists(tmp_file)

    loaded = mgr.load()
    assert loaded == {"openai": "sk-original"}
