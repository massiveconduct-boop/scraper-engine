# tests/unit/test_firecrawl_wiring.py
"""Firecrawl markdown conversion — client construction (hosted key vs
self-hosted base URL) and the HTTP call shape. Level-fetcher wiring itself
moved to Worker.process_job in round 29 (see test_worker.py's markdown
tests) — Level1Fetcher no longer knows about Firecrawl at all."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.services.firecrawl_client import FirecrawlClient, build_firecrawl_client


def test_build_firecrawl_client_returns_none_when_neither_key_nor_base_url_set(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_BASE_URL", raising=False)
    assert build_firecrawl_client() is None


def test_build_firecrawl_client_returns_client_when_key_set(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.delenv("FIRECRAWL_BASE_URL", raising=False)
    client = build_firecrawl_client()
    assert isinstance(client, FirecrawlClient)
    assert client._base_url == FirecrawlClient.DEFAULT_BASE_URL


def test_build_firecrawl_client_returns_client_when_only_base_url_set(monkeypatch):
    """Self-hosted Firecrawl typically needs no API key at all — a base URL
    alone must be enough to build a working client (round 29)."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_BASE_URL", "http://firecrawl.internal:3002")
    client = build_firecrawl_client()
    assert isinstance(client, FirecrawlClient)
    assert client._api_key is None
    assert client._base_url == "http://firecrawl.internal:3002"


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
        _, kwargs = http_client.post.call_args
        assert kwargs["headers"] == {"Authorization": "Bearer fc-key"}

    @pytest.mark.asyncio
    async def test_no_authorization_header_when_no_api_key(self, monkeypatch):
        """Self-hosted instances commonly need no auth at all — sending an
        empty/garbage Authorization header would be actively wrong."""
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"markdown": "# Self-hosted"}
        http_client = AsyncMock()
        http_client.post.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = FirecrawlClient(base_url="http://firecrawl.internal:3002")
        result = await client.convert_to_markdown("<html>hi</html>", "http://x")

        assert result == "# Self-hosted"
        _, kwargs = http_client.post.call_args
        assert kwargs["headers"] == {}

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
