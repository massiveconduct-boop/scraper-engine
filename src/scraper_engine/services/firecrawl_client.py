# services/firecrawl_client.py
"""Firecrawl API client for markdown conversion of scraped HTML."""

from __future__ import annotations

import os

import httpx


class FirecrawlClient:
    """Thin client over Firecrawl API for HTML-to-markdown conversion."""

    DEFAULT_BASE_URL = "https://api.firecrawl.dev"

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")

    async def convert_to_markdown(self, html: str, url: str) -> str:
        """Convert raw HTML to clean markdown via Firecrawl API."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._base_url}/v1/scrape",
                    json={"url": url},
                    headers={"Authorization": f"Bearer {self._api_key}"},
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

    Returns None when FIRECRAWL_API_KEY is unset — markdown conversion is
    simply skipped (FetchResult.markdown stays None), same env-gated,
    gracefully-inert pattern as services/captcha_solver.build_captcha_solver.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return None
    return FirecrawlClient(api_key)
