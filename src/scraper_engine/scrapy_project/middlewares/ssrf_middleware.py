# scrapy_project/middlewares/ssrf_middleware.py
"""SSRF check on every request a crawl sends — including every redirect hop.

Round 64 — design invariant #4 is "every outbound fetch is SSRF-checked
before enqueue and after every redirect". POST /v1/crawl checked its seed
URLs (api/routes.py) and nothing after that: the Scrapy subprocess followed
redirects on its own, so a public seed that 302s to 169.254.169.254 (cloud
metadata) was fetched. Scrapy's RedirectMiddleware turns each redirect into
a NEW Request that goes back through the downloader middleware chain, so a
process_request check here sees every hop, not just the first.

Runs synchronously (a downloader middleware has no event loop to await on
here), through SSRFGuard.validate_sync — the same resolution and deny list
the async fetchers use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scrapy.exceptions import IgnoreRequest

from scraper_engine.core.exceptions import SSRFBlockedError
from scraper_engine.core.ssrf_guard import SSRFGuard

if TYPE_CHECKING:
    from scrapy import Request, Spider
    from scrapy.crawler import Crawler

logger = logging.getLogger(__name__)


class SSRFMiddleware:
    def __init__(self, crawler: Crawler) -> None:
        self._stats = crawler.stats
        self._guard = SSRFGuard()

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> SSRFMiddleware:
        return cls(crawler)

    def process_request(self, request: Request, spider: Spider) -> None:
        try:
            self._guard.validate_sync(request.url)
        except (SSRFBlockedError, ValueError) as exc:
            if self._stats:
                self._stats.inc_value("ssrf/blocked")
            logger.warning("ssrf_blocked_crawl_request url=%s reason=%s", request.url, exc)
            raise IgnoreRequest(f"SSRF-blocked: {request.url}") from exc
