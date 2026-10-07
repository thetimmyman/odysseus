import sys
import types
from unittest.mock import MagicMock

from tests.helpers.cli_loader import load_script


def _load_cli(monkeypatch):
    routes = types.ModuleType("src.contacts")
    routes._get_carddav_config = MagicMock()
    routes._fetch_contacts = MagicMock()
    routes._create_contact = MagicMock()
    monkeypatch.setitem(sys.modules, "src.contacts", routes)
    return load_script("odysseus-contacts")


def test_contact_rows_skips_invalid_rows(monkeypatch):
    cli = _load_cli(monkeypatch)

    assert cli._contact_rows([
        {"name": "Ada", "email": "ada@example.test"},
        "bad-row",
        None,
    ]) == [{"name": "Ada", "email": "ada@example.test"}]
