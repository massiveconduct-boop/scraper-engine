# fetcher/adaptive_selector.py
"""Adaptive content extraction — tries multiple selector strategies and picks the best."""

from __future__ import annotations

import re


class AdaptiveSelector:
    """Try multiple extraction strategies against an HTML document.

    Picks the strategy that yields the most structured content.
    """

    SELECTORS: list[tuple[str, str]] = [
        ("article", "css"),
        ("main", "css"),
        ("[role=main]", "css"),
        ("body", "css"),
    ]

    def __init__(self) -> None:
        try:
            from bs4 import BeautifulSoup  # noqa: F401

            self._bs4_available = True
        except ImportError:
            self._bs4_available = False

    async def extract(
        self, html: str, schema: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Apply adaptive extraction, optionally guided by a schema."""
        result: dict[str, object] = {}

        if self._bs4_available:
            from bs4 import BeautifulSoup  # noqa: F401

            soup = BeautifulSoup(html, "html.parser")

            # Try each selector strategy
            for selector, _kind in self.SELECTORS:
                element = soup.select_one(selector)
                if element:
                    text = element.get_text(separator="\n", strip=True)
                    if len(text) > 100:
                        result["content"] = text
                        result["selector_used"] = selector
                        break

            # Extract title
            title = soup.find("title")
            if title:
                result["title"] = title.get_text(strip=True)

            # Extract links
            links = [a.get("href", "") for a in soup.find_all("a", href=True)]
            if links:
                result["links"] = links[:100]
        else:
            # Fallback: basic regex extraction
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            result["content"] = text

            title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
            if title_match:
                result["title"] = title_match.group(1).strip()

        if schema:
            result["schema"] = schema

        return result
