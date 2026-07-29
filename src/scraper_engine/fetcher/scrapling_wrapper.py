# fetcher/scrapling_wrapper.py
"""Thin wrapper over Scrapling for HTTP-level fetching — the "Scrapling" half
of L1's "HTTP/Scrapling" identity (base.yaml's `levels.level_1.engine:
scrapling`), wired into Level1Fetcher (round 28) as a first-attempt engine,
falling back to plain httpx on failure or when scrapling isn't installed —
same first-attempt/fallback shape as Level1Fetcher's JA3 client and
Level2Fetcher's Botasaurus-then-Camoufox pipeline.
"""

from __future__ import annotations

from typing import NamedTuple


class ScraplingResponse(NamedTuple):
    status_code: int
    text: str
    location: str | None


class ScraplingWrapper:
    """Adapter over Scrapling's `AsyncFetcher` for HTTP-level fetching."""

    def __init__(self) -> None:
        try:
            import scrapling  # noqa: F401

            self._available = True
        except ImportError:
            self._available = False

    async def fetch(
        self,
        url: str,
        timeout: int = 20,
        *,
        proxy: str | None = None,
        follow_redirects: bool = False,
    ) -> ScraplingResponse | None:
        """Fetch a URL via Scrapling. Returns None (not a response) to signal
        "fall back to httpx" — either scrapling isn't installed, or the
        request itself failed. `follow_redirects=False` by default so the
        caller can revalidate each redirect hop against the SSRF guard
        itself (spec §1.1 #4) instead of Scrapling silently following a
        redirect straight to a private/metadata address."""
        if not self._available:
            return None
        from scrapling.fetchers import AsyncFetcher

        try:
            page = await AsyncFetcher.get(
                url, timeout=timeout, proxy=proxy, follow_redirects=follow_redirects
            )
        except Exception:
            return None
        location = page.headers.get("location") if 300 <= page.status < 400 else None
        return ScraplingResponse(
            status_code=page.status, text=str(page.html_content), location=location
        )
