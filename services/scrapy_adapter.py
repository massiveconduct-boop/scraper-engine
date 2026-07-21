# services/scrapy_adapter.py
"""Scrapy adapter for bulk crawl operations.

Used by /v1/crawl endpoint for large-scale structured crawling.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ScrapyAdapter:
    """Adapter to run Scrapy spiders programmatically from our orchestrator."""

    def __init__(self) -> None:
        try:
            import scrapy  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
            logger.warning("scrapy not installed — bulk crawl disabled")

    async def run_spider(
        self, spider_name: str, start_urls: list[str], **kwargs: object
    ) -> list[dict[str, object]]:
        """Run a Scrapy spider and return extracted items."""
        if not self._available:
            return []

        items: list[dict[str, object]] = []

        def _run() -> None:
            from scrapy.crawler import CrawlerProcess
            from scrapy.spiders import Spider
            from scrapy.utils.project import get_project_settings

            class _DynamicSpider(Spider):  # type: ignore[misc]
                name = spider_name
                start_urls = start_urls

                def parse(self, response: object) -> object:
                    item = {"url": response.url, "title": response.css("title::text").get()}  # type: ignore[attr-defined]  # noqa: F821
                    items.append(item)
                    yield item

            settings = get_project_settings()
            process = CrawlerProcess(settings)
            process.crawl(_DynamicSpider)
            process.start()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _run)
        return items
