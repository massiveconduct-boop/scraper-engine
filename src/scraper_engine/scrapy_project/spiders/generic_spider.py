# scrapy_project/spiders/generic_spider.py
"""Generic Scrapy spider — configurable per tenant.

Used by /v1/crawl endpoint for bulk structured crawling operations.
Spider parameters are set at initialization time from tenant config.
"""

from __future__ import annotations

from typing import Any

from scrapy import Request, Spider
from scrapy.http import Response


class GenericSpider(Spider):
    """Configurable spider driven by tenant configuration.

    Parameters set via __init__ kwargs:
      - tenant_id: str — tenant slug for quota/storage routing
      - start_urls: list[str] — URLs to crawl
      - allowed_domains: list[str] — domain allowlist
      - link_extractor_rules: list[tuple] — (allow, deny) patterns
      - max_pages: int — per-crawl page limit
      - extraction_schema: dict — field definitions for structured extraction
    """

    name = "generic"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.tenant_id = kwargs.pop("tenant_id", "system")
        self.max_pages = int(kwargs.pop("max_pages", 100))
        self.extraction_schema = kwargs.pop("extraction_schema", {})
        super().__init__(*args, **kwargs)
        self._page_count = 0

    def start_requests(self):  # type: ignore[no-untyped-def]
        """Yield initial requests for configured start URLs."""
        for url in self.start_urls:
            yield Request(url, callback=self.parse, errback=self.handle_error)

    def parse(self, response: Response):  # type: ignore[no-untyped-def]
        """Extract structured data from each page."""
        if self._page_count >= self.max_pages:
            self.logger.info("max_pages_reached", count=self._page_count)
            return

        self._page_count += 1

        item: dict[str, Any] = {
            "url": response.url,
            "tenant_id": self.tenant_id,
            "html": response.text,
            "success": response.status < 400,
            "http_status": response.status,
            "title": response.css("title::text").get(),
            "links": [link.attrib.get("href", "") for link in response.css("a[href]")[:50]],
        }

        # Apply extraction schema if configured
        if self.extraction_schema:
            for field_name, css_selector in self.extraction_schema.items():
                item[field_name] = response.css(css_selector).get()

        yield item

        # Follow links for crawling
        if self._page_count < self.max_pages:
            for link in response.css("a[href]"):
                href = link.attrib.get("href", "")
                if href:
                    yield response.follow(href, callback=self.parse, errback=self.handle_error)

    def handle_error(self, failure: object) -> None:
        """Log request failures."""
        url = getattr(getattr(failure, "request", None), "url", "unknown")
        err = str(getattr(failure, "value", failure))
        self.logger.error("request_failed: %s %s", url, err)
