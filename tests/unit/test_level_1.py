# tests/unit/test_level_1.py
"""Level1Fetcher — plain-httpx redirect chain, timeout/exception handling,
and JA3-path markdown conversion. Complements test_level1_ja3_wiring.py,
which covers the JA3-first/httpx-fallback wiring but not these branches."""

from unittest.mock import AsyncMock

import httpx
import pytest

from scraper_engine.core.models import FailureCategory
from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.level_1 import Level1Fetcher
from scraper_engine.fetcher.scrapling_wrapper import ScraplingResponse
from scraper_engine.services.botasaurus_requests_client import Ja3Response


class _FakeResponse:
    def __init__(self, status_code, text="<html>ok</html>", location=None, is_redirect=False):
        self.status_code = status_code
        self.text = text
        self.is_redirect = is_redirect
        self.url = "http://example.com/"
        self.headers = {"location": location} if location else {}


class _RedirectThenFinalClient:
    def __init__(self, *a, **kw):
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self._calls += 1
        if self._calls == 1:
            return _FakeResponse(302, text="", location="/next", is_redirect=True)
        return _FakeResponse(200, text="<html>final</html>")


class _TimeoutClient:
    def __init__(self, *a, **kw): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        raise httpx.TimeoutException("timed out")


class _BoomClient:
    def __init__(self, *a, **kw): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        raise ValueError("unexpected boom")


class TestPlainHttpxRedirects:
    @pytest.mark.asyncio
    async def test_follows_one_redirect_and_revalidates_ssrf(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"
        assert result.http_status == 200

    @pytest.mark.asyncio
    async def test_redirect_hop_calls_firecrawl_markdown(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        firecrawl = AsyncMock()
        firecrawl.convert_to_markdown.return_value = "# final"
        fetcher = Level1Fetcher(firecrawl_client=firecrawl)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.markdown == "# final"
        firecrawl.convert_to_markdown.assert_awaited_once_with(
            "<html>final</html>", "http://example.com"
        )


class TestPlainHttpxExceptions:
    @pytest.mark.asyncio
    async def test_timeout_returns_network_timeout_failure(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.failure_category == FailureCategory.NETWORK_TIMEOUT
        assert result.error_message == "Request timed out"

    @pytest.mark.asyncio
    async def test_generic_exception_classified_and_captured(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)
        fetcher = Level1Fetcher()

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is False
        assert result.error_message == "unexpected boom"


class TestJa3MarkdownConversion:
    @pytest.mark.asyncio
    async def test_ja3_success_path_calls_firecrawl_markdown(self):
        session = AsyncMock()
        session.get.return_value = Ja3Response(
            status_code=200, text="<html>ja3</html>", location=None
        )
        ja3 = AsyncMock()
        ja3.open_session.return_value = session
        firecrawl = AsyncMock()
        firecrawl.convert_to_markdown.return_value = "# ja3"

        fetcher = Level1Fetcher(ja3_client=ja3, firecrawl_client=firecrawl)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.markdown == "# ja3"
        firecrawl.convert_to_markdown.assert_awaited_once_with(
            "<html>ja3</html>", "http://example.com"
        )


class TestScraplingWiring:
    @pytest.mark.asyncio
    async def test_uses_scrapling_result_when_it_succeeds(self):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=200, text="<html>scrapling</html>", location=None
        )
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>scrapling</html>"
        scrapling.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_follows_redirect_and_revalidates_ssrf(self):
        scrapling = AsyncMock()
        scrapling.fetch.side_effect = [
            ScraplingResponse(status_code=302, text="", location="/next"),
            ScraplingResponse(status_code=200, text="<html>final</html>", location=None),
        ]
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"
        assert scrapling.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_redirect_to_blocked_target_falls_through_to_httpx_failure(self, monkeypatch):
        """SSRF block during the scrapling redirect loop is swallowed by
        _fetch_via_scrapling's fallback contract (returns None, same as
        _fetch_via_ja3) — fetch() then retries via plain httpx against the
        original URL, which hits the identical blocked hop and surfaces a
        real failure instead of silently succeeding."""
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=302, text="", location="http://169.254.169.254/"
        )
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        # httpx fallback path (_RedirectThenFinalClient) succeeds since it
        # redirects to a harmless "/next", not the blocked scrapling target —
        # proves control genuinely passed to the next engine in the chain.
        assert result.success is True
        assert result.html == "<html>final</html>"

    @pytest.mark.asyncio
    async def test_falls_back_when_post_redirect_scrapling_fetch_returns_none(self, monkeypatch):
        scrapling = AsyncMock()
        scrapling.fetch.side_effect = [
            ScraplingResponse(status_code=302, text="", location="/next"),
            None,
        ]
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"
        assert scrapling.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_httpx_when_scrapling_returns_none(self, monkeypatch):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = None
        monkeypatch.setattr(httpx, "AsyncClient", _RedirectThenFinalClient)
        fetcher = Level1Fetcher(scrapling_client=scrapling)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.success is True
        assert result.html == "<html>final</html>"

    @pytest.mark.asyncio
    async def test_scrapling_success_calls_firecrawl_markdown(self):
        scrapling = AsyncMock()
        scrapling.fetch.return_value = ScraplingResponse(
            status_code=200, text="<html>scrapling</html>", location=None
        )
        firecrawl = AsyncMock()
        firecrawl.convert_to_markdown.return_value = "# scrapling"
        fetcher = Level1Fetcher(scrapling_client=scrapling, firecrawl_client=firecrawl)

        result = await fetcher.fetch("http://example.com", TenantId("system"))

        assert result.markdown == "# scrapling"
        firecrawl.convert_to_markdown.assert_awaited_once_with(
            "<html>scrapling</html>", "http://example.com"
        )
