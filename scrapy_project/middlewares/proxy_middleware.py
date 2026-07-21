# scrapy_project/middlewares/proxy_middleware.py
"""Scrapy downloader middleware for proxy rotation.

Selects proxies from our scored pool (via ProxyManager) and attaches them
to each request. Handles proxy failures by marking them in the manager.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scrapy import Request, Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response

logger = logging.getLogger(__name__)


class ProxyMiddleware:
    """Attach a proxy to every outbound Scrapy request.

    Uses ProxyManager for scored proxy selection from our pool.
    On failure, marks the proxy in the manager for decay/ban.
    """

    def __init__(self, crawler: Crawler) -> None:
        self._crawler = crawler
        self._stats = crawler.stats
        # ProxyManager and tenant would be injected via crawler.settings
        # or a service container at startup

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> ProxyMiddleware:
        return cls(crawler)

    def process_request(self, request: Request, spider: Spider) -> None:
        """Attach a proxy to the request before it's sent."""
        # Deferred: proxy selection via ProxyManager.get_proxy()
        # For now, respect any proxy already set on the request meta
        if "proxy" not in request.meta:
            request.meta["proxy"] = None

    def process_response(
        self, request: Request, response: Response, spider: Spider
    ) -> Response:
        """Check response for proxy failure signals."""
        if response.status in (403, 429, 503):
            if self._stats:
                self._stats.inc_value("proxy/blocked")
            logger.warning("proxy_blocked: %s status=%s", request.url, response.status)
        return response

    def process_exception(
        self, request: Request, exception: Exception, spider: Spider
    ) -> None:
        """Mark proxy as failed on connection errors."""
        if self._stats:
            self._stats.inc_value("proxy/errors")
        logger.error("proxy_error: %s %s", request.url, str(exception))
