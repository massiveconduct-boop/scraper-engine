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

    # Round 63 — schemes that are never a followable page. Kept here rather
    # than filtered by the caller because `links` is a single, centrally
    # produced field (orchestrator/worker.py wires this once for every level).
    _NON_PAGE_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "blob:")

    def __init__(self, max_links: int = 1000) -> None:
        self._max_links = max_links
        try:
            from bs4 import BeautifulSoup  # noqa: F401

            self._bs4_available = True
        except ImportError:
            self._bs4_available = False

    def _clean_links(self, hrefs: list[str], base_url: str | None) -> list[str]:
        """Absolutize, filter and dedupe raw href values, preserving DOM order.

        Round 63. This used to be `links[:100]` over the raw hrefs, which had
        three problems that only showed up together on a real page. The list
        was relative (a caller could not follow a link without re-deriving the
        origin), un-deduped (a nav link repeated in a header and a footer cost
        two of the hundred slots), and truncated at 100 in DOM order — so on a
        Jumia catalog page rendered at L3, where the hydrated nav mega-menu
        emits ~100 links before the first product, the entire budget went to
        site navigation and NOT ONE product URL came back. The identical page
        captured earlier at L2, before that hydration, returned them all,
        which made it look like L3 was losing links. Deduping and dropping
        non-page schemes both buy real headroom; the cap stays (a
        pathological page should not return an unbounded list) but is
        configurable and set far above any genuine page's navigation.
        """
        from urllib.parse import urljoin

        seen: set[str] = set()
        cleaned: list[str] = []
        for href in hrefs:
            candidate = href.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if candidate.lower().startswith(self._NON_PAGE_SCHEMES):
                continue
            if base_url:
                candidate = urljoin(base_url, candidate)
            if candidate in seen:
                continue
            seen.add(candidate)
            cleaned.append(candidate)
            if len(cleaned) >= self._max_links:
                break
        return cleaned

    async def extract(
        self,
        html: str,
        schema: dict[str, object] | None = None,
        base_url: str | None = None,
    ) -> dict[str, object]:
        """Apply adaptive extraction, optionally guided by a schema.

        base_url (round 63), when given, is what relative links are resolved
        against — callers pass the URL the HTML was fetched from.
        """
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
            links = self._clean_links(
                [str(a.get("href", "")) for a in soup.find_all("a", href=True)], base_url
            )
            if links:
                result["links"] = links
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
