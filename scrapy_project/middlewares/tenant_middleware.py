# scrapy_project/middlewares/tenant_middleware.py
"""Tenant-aware Scrapy middleware.

Attaches tenant_id to every request and response, enabling per-tenant
quota tracking, deduplication, and storage routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scrapy import Request, Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response


class TenantMiddleware:
    """Inject tenant context into every Scrapy request/response cycle.

    Tenant ID is read from spider attributes (set at spider initialization)
    and threaded through request meta for downstream pipeline processing.
    """

    def __init__(self, crawler: Crawler) -> None:
        self._crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> TenantMiddleware:
        return cls(crawler)

    def process_request(self, request: Request, spider: Spider) -> None:
        """Attach tenant_id from spider to the request."""
        tenant_id = getattr(spider, "tenant_id", None)
        if tenant_id:
            request.meta["tenant_id"] = str(tenant_id)

    def process_response(
        self, request: Request, response: Response, spider: Spider
    ) -> Response:
        """Ensure tenant context flows through responses."""
        return response
