# tests/unit/test_scrapling_wrapper.py
"""ScraplingWrapper — scrapling-available path, returning None (not raising
and not self-handling httpx fallback) on unavailability or any fetch error,
so Level1Fetcher can drive the httpx fallback itself (round 28 wiring).

`scrapling.fetchers` transitively imports curl_cffi, which isn't installed in
this environment (a pre-existing gap unrelated to this round's scope) — so
the AsyncFetcher path is exercised via a fake module injected into
sys.modules rather than patching attributes on the real (unimportable) one.
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.fetcher.scrapling_wrapper import ScraplingWrapper


class TestInit:
    def test_available_true_when_scrapling_importable(self):
        wrapper = ScraplingWrapper()
        assert wrapper._available is True

    def test_available_false_when_not_importable(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "scrapling":
                raise ImportError("no scrapling")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        wrapper = ScraplingWrapper()
        assert wrapper._available is False


def _fake_fetchers_module(get_mock):
    fake_async_fetcher = MagicMock()
    fake_async_fetcher.get = get_mock
    fake_fetchers_module = types.ModuleType("scrapling.fetchers")
    fake_fetchers_module.AsyncFetcher = fake_async_fetcher
    return fake_fetchers_module


class TestFetch:
    @pytest.mark.asyncio
    async def test_returns_none_when_unavailable(self):
        wrapper = ScraplingWrapper()
        wrapper._available = False

        result = await wrapper.fetch("http://example.com", timeout=5)

        assert result is None

    @pytest.mark.asyncio
    async def test_success_returns_response_with_no_location(self, monkeypatch):
        wrapper = ScraplingWrapper()
        wrapper._available = True

        fake_page = MagicMock()
        fake_page.html_content = "<html>scrapling</html>"
        fake_page.status = 200
        fake_page.headers = {}

        get_mock = AsyncMock(return_value=fake_page)
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", _fake_fetchers_module(get_mock))

        result = await wrapper.fetch("http://example.com", timeout=5, proxy="http://p:1")

        assert result is not None
        assert result.status_code == 200
        assert result.text == "<html>scrapling</html>"
        assert result.location is None
        get_mock.assert_awaited_once_with(
            "http://example.com", timeout=5, proxy="http://p:1", follow_redirects=False
        )

    @pytest.mark.asyncio
    async def test_redirect_status_extracts_location(self, monkeypatch):
        wrapper = ScraplingWrapper()
        wrapper._available = True

        fake_page = MagicMock()
        fake_page.html_content = ""
        fake_page.status = 302
        fake_page.headers = {"location": "/next"}

        get_mock = AsyncMock(return_value=fake_page)
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", _fake_fetchers_module(get_mock))

        result = await wrapper.fetch("http://example.com", timeout=5)

        assert result is not None
        assert result.status_code == 302
        assert result.location == "/next"

    @pytest.mark.asyncio
    async def test_returns_none_on_fetch_exception(self, monkeypatch):
        wrapper = ScraplingWrapper()
        wrapper._available = True

        get_mock = AsyncMock(side_effect=RuntimeError("network boom"))
        monkeypatch.setitem(sys.modules, "scrapling.fetchers", _fake_fetchers_module(get_mock))

        result = await wrapper.fetch("http://example.com", timeout=5)

        assert result is None
