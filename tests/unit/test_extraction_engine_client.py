# tests/unit/test_extraction_engine_client.py
"""extraction-engine client construction (EXTRACTION_ENGINE_BASE_URL/_API_KEY)
and the HTTP call shape. Mirrors test_firecrawl_wiring.py's mocking technique
and coverage exactly, including the fail-soft-to-None error path Worker.
process_job relies on to fall back to AdaptiveSelector."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.services.extraction_engine_client import (
    ExtractionEngineClient,
    build_extraction_engine_client,
)


def test_build_extraction_engine_client_returns_none_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("EXTRACTION_ENGINE_BASE_URL", raising=False)
    monkeypatch.delenv("EXTRACTION_ENGINE_API_KEY", raising=False)
    assert build_extraction_engine_client() is None


def test_build_extraction_engine_client_returns_client_when_base_url_set(monkeypatch):
    monkeypatch.setenv("EXTRACTION_ENGINE_BASE_URL", "http://extraction-engine:8080")
    monkeypatch.delenv("EXTRACTION_ENGINE_API_KEY", raising=False)
    client = build_extraction_engine_client()
    assert isinstance(client, ExtractionEngineClient)
    assert client._base_url == "http://extraction-engine:8080"
    assert client._api_key is None


def test_build_extraction_engine_client_picks_up_api_key(monkeypatch):
    monkeypatch.setenv("EXTRACTION_ENGINE_BASE_URL", "http://extraction-engine:8080")
    monkeypatch.setenv("EXTRACTION_ENGINE_API_KEY", "ee-test-key")
    client = build_extraction_engine_client()
    assert isinstance(client, ExtractionEngineClient)
    assert client._api_key == "ee-test-key"


class TestExtractionEngineClientExtract:
    @pytest.mark.asyncio
    async def test_returns_parsed_result_on_success(self, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"fields": {"price": {"value": "9.99"}}}
        http_client = AsyncMock()
        http_client.post.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = ExtractionEngineClient("http://extraction-engine:8080", api_key="ee-key")
        result = await client.extract("<html>hi</html>", {"price": "string"})

        assert result == {"fields": {"price": {"value": "9.99"}}}
        args, kwargs = http_client.post.call_args
        assert args[0] == "http://extraction-engine:8080/v1/extract"
        assert kwargs["json"] == {
            "html": "<html>hi</html>",
            "schema": {"price": "string"},
            "enable_smallmodel": False,
            "enable_llm": False,
        }
        assert kwargs["headers"] == {"Authorization": "Bearer ee-key"}

    @pytest.mark.asyncio
    async def test_passes_enable_flags_through(self, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        http_client = AsyncMock()
        http_client.post.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = ExtractionEngineClient("http://extraction-engine:8080")
        await client.extract(
            "<html/>", {"x": "string"}, enable_smallmodel=True, enable_llm=True
        )

        _, kwargs = http_client.post.call_args
        assert kwargs["json"]["enable_smallmodel"] is True
        assert kwargs["json"]["enable_llm"] is True

    @pytest.mark.asyncio
    async def test_no_authorization_header_when_no_api_key(self, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {}
        http_client = AsyncMock()
        http_client.post.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = ExtractionEngineClient("http://extraction-engine:8080")
        await client.extract("<html/>", {"x": "string"})

        _, kwargs = http_client.post.call_args
        assert kwargs["headers"] == {}

    @pytest.mark.asyncio
    async def test_returns_none_on_connection_error(self, monkeypatch):
        """Fails soft, not raises -- Worker.process_job relies on this to fall
        back to AdaptiveSelector instead of losing the whole job."""
        http_client = AsyncMock()
        http_client.__aenter__.side_effect = RuntimeError("connection refused")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = ExtractionEngineClient("http://extraction-engine:8080")
        result = await client.extract("<html/>", {"x": "string"})

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_non_2xx_response(self, monkeypatch):
        import httpx

        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "422", request=MagicMock(), response=MagicMock()
        )
        http_client = AsyncMock()
        http_client.post.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = ExtractionEngineClient("http://extraction-engine:8080")
        result = await client.extract("<html/>", {"x": "string"})

        assert result is None

    def test_custom_base_url_strips_trailing_slash(self):
        client = ExtractionEngineClient("http://extraction-engine:8080/")
        assert client._base_url == "http://extraction-engine:8080"
