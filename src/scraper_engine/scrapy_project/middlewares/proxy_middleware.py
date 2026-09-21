# scrapy_project/middlewares/proxy_middleware.py
"""Route every crawl request through the proxy the orchestrator leased.

Round 64 — this used to set `request.meta["proxy"] = None` under a
"Deferred: proxy selection via ProxyManager" comment, so every crawl left
from the server's own IP. The subprocess cannot reach ProxyManager (it is
async and lives in the parent), so the parent leases one proxy per crawl —
free pool first, then the paid gateway, the same order the scrape ladder
uses (orchestrator/tasks.py::_run_crawl_job) — and passes its URL in as the
CRAWL_PROXY_URL setting. Scrapy's own HttpProxyMiddleware (priority 750,
after this one) turns `meta["proxy"]` into the connection, including any
user:password in the URL.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from scrapy.exceptions import IgnoreRequest

if TYPE_CHECKING:
    from scrapy import Request, Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response

logger = logging.getLogger(__name__)

# Statuses that mean "this exit IP is being refused", counted so the parent
# can see a burned proxy in the crawl's stats.
_BLOCK_STATUSES = (403, 429, 503)


class ProxyMiddleware:
    def __init__(self, crawler: Crawler) -> None:
        self._stats = crawler.stats
        self._proxy_url: str | None = crawler.settings.get("CRAWL_PROXY_URL") or None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> ProxyMiddleware:
        return cls(crawler)

    def process_request(self, request: Request, spider: Spider) -> None:
        if self._proxy_url and "proxy" not in request.meta:
            request.meta["proxy"] = self._proxy_url

    def process_response(self, request: Request, response: Response, spider: Spider) -> Response:
        if response.status in _BLOCK_STATUSES:
            if self._stats:
                self._stats.inc_value("proxy/blocked")
            logger.warning("proxy_blocked: %s status=%s", request.url, response.status)
        return response

    def process_exception(self, request: Request, exception: Exception, spider: Spider) -> None:
        # A request another middleware refused (SSRFMiddleware's IgnoreRequest)
        # never reached the proxy; counting it as a proxy error blamed the
        # proxy for our own block (seen in a real crawl run, round 64).
        if isinstance(exception, IgnoreRequest):
            return
        if self._stats:
            self._stats.inc_value("proxy/errors")
        logger.error("proxy_error: %s %s", request.url, str(exception))
