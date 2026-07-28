# tests/unit/test_firecrawl_wiring.py
"""Firecrawl markdown conversion — env-gated wiring into Level1Fetcher."""

from unittest.mock import AsyncMock

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
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _FakeResponse()

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
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _FakeResponse()

    import httpx
    orig_client = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient  # type: ignore[assignment]
    try:
        result = await fetcher.fetch("http://example.com", TenantId("system"))
    finally:
        httpx.AsyncClient = orig_client  # type: ignore[assignment]

    assert result.markdown is None
