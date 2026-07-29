# tests/unit/test_firecrawl_wiring.py
"""Firecrawl markdown conversion — env-gated wiring into Level1Fetcher."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.fetcher.level_1 import Level1Fetcher
from scraper_engine.services.firecrawl_client import FirecrawlClient, build_firecrawl_client


def test_build_firecrawl_client_returns_none_when_key_unset(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert build_firecrawl_client() is None


def test_build_firecrawl_client_returns_client_when_key_set(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    client = build_firecrawl_client()
    assert isinstance(client, FirecrawlClient)


@pytest.mark.asyncio
async def test_level1_fetcher_populates_markdown_when_firecrawl_configured():
    firecrawl = AsyncMock()
    firecrawl.convert_to_markdown.return_value = "# Hello"
    fetcher = Level1Fetcher(firecrawl_client=firecrawl)

    class _FakeResponse:
        status_code = 200
        text = "<html><body>Hello</body></html>"
        is_redirect = False

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse()

    import httpx

    orig_client = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient  # type: ignore[assignment]
    try:
        result = await fetcher.fetch("http://example.com", TenantId("system"))
    finally:
        httpx.AsyncClient = orig_client  # type: ignore[assignment]

    assert result.markdown == "# Hello"
    firecrawl.convert_to_markdown.assert_awaited_once_with(
        "<html><body>Hello</body></html>", "http://example.com"
    )


@pytest.mark.asyncio
async def test_level1_fetcher_skips_markdown_when_firecrawl_not_configured():
    fetcher = Level1Fetcher(firecrawl_client=None)

    class _FakeResponse:
        status_code = 200
        text = "<html></html>"
        is_redirect = False

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse()

    import httpx

    orig_client = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient  # type: ignore[assignment]
    try:
        result = await fetcher.fetch("http://example.com", TenantId("system"))
    finally:
        httpx.AsyncClient = orig_client  # type: ignore[assignment]

    assert result.markdown is None


class TestFirecrawlClientConvertToMarkdown:
    @pytest.mark.asyncio
    async def test_returns_markdown_on_success(self, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"markdown": "# Converted"}
        http_client = AsyncMock()
        http_client.post.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = FirecrawlClient(api_key="fc-key")
        result = await client.convert_to_markdown("<html>hi</html>", "http://x")

        assert result == "# Converted"

    @pytest.mark.asyncio
    async def test_falls_back_to_raw_html_on_api_error(self, monkeypatch):
        http_client = AsyncMock()
        http_client.__aenter__.side_effect = RuntimeError("connection refused")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = FirecrawlClient(api_key="fc-key")
        result = await client.convert_to_markdown("<html>raw</html>", "http://x")

        assert result == "<html>raw</html>"

    def test_custom_base_url_strips_trailing_slash(self):
        client = FirecrawlClient(api_key="k", base_url="http://custom.example/")
        assert client._base_url == "http://custom.example"
