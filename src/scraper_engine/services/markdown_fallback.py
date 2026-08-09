# services/markdown_fallback.py
"""Local HTML-to-markdown conversion — the default markdown source.

FirecrawlClient (firecrawl_client.py) is preferred when configured, but it
re-fetches the URL through Firecrawl's own scraper rather than converting
the HTML this process already has. Firecrawl is opt-in
(FIRECRAWL_API_KEY/FIRECRAWL_BASE_URL), so without it FetchResult.markdown
was simply left None — a caller who wants markdown always had to stand up a
separate tool first. This module needs nothing external: it converts the
already-fetched HTML in-process, so orchestrator/worker.py can populate
markdown unconditionally.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdownify import markdownify


def html_to_markdown(html: str) -> str:
    """markdownify's own `strip=` only skips tag *formatting* — the text
    content of a stripped tag still passes through unconverted. script/style
    bodies (JS source, CSS rules) are never real page content, so they're
    removed as nodes before conversion rather than merely left unstyled."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return str(markdownify(str(soup), heading_style="ATX")).strip()
