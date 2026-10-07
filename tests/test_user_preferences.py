import json
import os
from pathlib import Path
import subprocess
import sys

import src.user_preferences as prefs


def test_load_ignores_non_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps(["not", "a", "prefs", "object"]), encoding="utf-8")
    monkeypatch.setattr(prefs, "PREFS_FILE", str(prefs_file))

    assert prefs.load_all() == {}
    assert prefs.load_for_user("alice") == {}


def test_load_keeps_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(prefs, "PREFS_FILE", str(prefs_file))

    assert prefs.load_for_user("alice") == {"theme": "dark"}


def test_storage_works_without_route_modules(tmp_path):
    """A background caller can persist preferences without loading route handlers."""
    code = """
import importlib.abc
import sys

class NoRouteImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'routes':
            raise ImportError('Route import forbidden: ' + fullname)

sys.meta_path.insert(0, NoRouteImports())
sys.path.insert(0, sys.argv[1])
from src import user_preferences as prefs
prefs.PREFS_FILE = sys.argv[2]
prefs.save_for_user('alice', {'theme': 'dark'})
prefs.save_for_user('bob', {'theme': 'paper'})
assert prefs.load_for_user('alice') == {'theme': 'dark'}
assert prefs.load_for_user('bob') == {'theme': 'paper'}
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1]),
         str(tmp_path / "user_prefs.json")],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "DATABASE_URL": "sqlite:///:memory:",
             "ODYSSEUS_DATA_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_user_settings_keep_owner_overrides_and_global_fallback(tmp_path, monkeypatch):
    from src import settings

    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: "global")
    prefs.save_for_user("alice", {"default_model": "alice-model", "admin_only": "private"})
    prefs.save_for_user("bob", {"default_model": "bob-model"})

    assert settings.get_user_setting("default_model", "alice") == "alice-model"
    assert settings.get_user_setting("default_model", "bob") == "bob-model"
    assert settings.get_user_setting("default_model", "missing") == "global"
    assert settings.get_user_setting("default_model") == "global"
    assert settings.get_user_setting("admin_only", "alice") == "global"
