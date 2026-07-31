# services/firecrawl_client.py
"""Firecrawl API client for markdown conversion of scraped HTML.

Firecrawl itself is open source and self-hostable, so this client is not
locked to the paid hosted API: FIRECRAWL_BASE_URL can point it at a
self-hosted instance instead, and a self-hosted instance typically needs no
API key at all — only the hosted api.firecrawl.dev requires one.
"""

from __future__ import annotations

import os

import httpx


class FirecrawlClient:
    """Thin client over Firecrawl's /v1/scrape API for HTML-to-markdown
    conversion. Works against the hosted API or a self-hosted instance —
    the API shape is the same either way, only the base URL and whether an
    API key is needed differ."""

    DEFAULT_BASE_URL = "https://api.firecrawl.dev"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")

    async def convert_to_markdown(self, html: str, url: str) -> str:
        """Convert raw HTML to clean markdown via Firecrawl's /v1/scrape API."""
        try:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._base_url}/v1/scrape",
                    json={"url": url},
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                result: str = data.get("markdown", html)
                return result
        except Exception:
            # Fallback: return raw HTML if Firecrawl is unavailable
            return html


def build_firecrawl_client() -> FirecrawlClient | None:
    """Select the Firecrawl client for production use.

    FIRECRAWL_API_KEY authenticates against the hosted api.firecrawl.dev.
    FIRECRAWL_BASE_URL (optional) points this at a self-hosted Firecrawl
    instance instead — self-hosted deployments commonly run without any
    API key, so a base URL alone is enough to build a client; the key is
    only required in practice by the hosted default.

    Returns None when neither is set — markdown conversion is simply
    skipped (FetchResult.markdown stays None), same env-gated,
    gracefully-inert pattern as services/captcha_solver.build_captcha_solver.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    base_url = os.environ.get("FIRECRAWL_BASE_URL")
    if not api_key and not base_url:
        return None
    return FirecrawlClient(api_key, base_url)
