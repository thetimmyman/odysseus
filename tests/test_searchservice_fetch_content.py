"""The exported async service returns page text without blocking its event loop."""
import asyncio
import threading

import pytest

from services.search import service as search_service
from services.search.service import SearchService


@pytest.mark.parametrize("legacy_flag", [True, False])
def test_fetch_content_is_callable_and_offloads_synchronous_backend(monkeypatch, legacy_flag):
    event_loop_thread = threading.get_ident()
    calls = []

    def fetch(url):
        calls.append((url, threading.get_ident()))
        return {"success": True, "content": "Page text", "title": "Example"}

    monkeypatch.setattr(search_service, "fetch_webpage_content", fetch)
    service = SearchService(fetch_content=legacy_flag)
    result = asyncio.run(service.fetch_content("https://example.com"))
    assert result == "Page text"
    assert calls[0][0] == "https://example.com"
    assert calls[0][1] != event_loop_thread


@pytest.mark.parametrize("record", [
    {"success": False, "content": "", "error": "blocked URL"},
    {"success": False, "content": "partial", "error": "request failed"},
    {"success": True, "content": ""},
])
def test_fetch_content_returns_none_for_failed_or_empty_pages(monkeypatch, record):
    monkeypatch.setattr(search_service, "fetch_webpage_content", lambda url: record)
    assert asyncio.run(SearchService().fetch_content("https://example.com")) is None
