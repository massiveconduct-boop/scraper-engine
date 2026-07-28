# fetcher/scrapling_wrapper.py
"""Thin wrapper over Scrapling for HTTP-level fetching and extraction."""

from __future__ import annotations


class ScraplingWrapper:
    """Adapter for Scrapling HTTP client and adaptive selectors."""

    def __init__(self) -> None:
        try:
            import scrapling  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    async def fetch(self, url: str, timeout: int = 20) -> str:
        """Fetch HTML content using Scrapling. Falls back to httpx if unavailable."""
        if self._available:
            import scrapling  # noqa: F401
            result = scrapling.get(url, timeout=timeout)
            return str(result.text)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                return str(response.text)
