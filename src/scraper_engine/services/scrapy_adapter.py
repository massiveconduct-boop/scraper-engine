# services/scrapy_adapter.py
"""Scrapy adapter for bulk crawl operations.

Used by POST /v1/crawl for large-scale structured crawling (ScrapeRequest
caps single scrape jobs at 500 URLs and points callers here for more).

Each crawl runs in its own spawned subprocess, not in-process via
loop.run_in_executor. Twisted's reactor (which CrawlerProcess.start() drives)
can only be started once per OS process — calling it twice in the same
interpreter raises ReactorNotRestartable. Since orchestrator/tasks.py runs
inside a long-lived `rq worker` process handling many jobs over its life,
running in-process would work for exactly one crawl job and then crash every
one after it. A fresh subprocess per crawl sidesteps that entirely.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300


def _run_spider_subprocess(
    spider_name: str, urls: list[str], result_queue: multiprocessing.Queue[Any]
) -> None:
    """Entry point for the spawned child process — safe to start a fresh
    Twisted reactor here regardless of how many crawls the parent has run."""
    try:
        from scrapy.crawler import CrawlerProcess
        from scrapy.spiders import Spider
        from scrapy.utils.project import get_project_settings

        items: list[dict[str, object]] = []

        class _DynamicSpider(Spider):
            # NOTE: the class attribute must not share a name with the
            # closure variable it reads (e.g. `start_urls = start_urls`) —
            # any name assigned anywhere in a class body is local to that
            # body for its entire execution, which shadows the closure
            # variable and raises NameError when the RHS is evaluated.
            name = spider_name
            start_urls = urls

            def parse(self, response: object) -> object:
                item = {"url": response.url, "title": response.css("title::text").get()}  # type: ignore[attr-defined]
                items.append(item)
                yield item

        settings = get_project_settings()
        process = CrawlerProcess(settings)
        process.crawl(_DynamicSpider)
        process.start()
        result_queue.put(items)
    except Exception as exc:  # noqa: BLE001 -- must cross the process boundary as data
        result_queue.put(exc)


class ScrapyAdapter:
    """Adapter to run Scrapy spiders programmatically from our orchestrator."""

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds
        try:
            import scrapy  # noqa: F401

            self._available = True
        except ImportError:
            self._available = False
            logger.warning("scrapy not installed — bulk crawl disabled")

    async def run_spider(
        self, spider_name: str, start_urls: list[str]
    ) -> list[dict[str, object]]:
        """Run a Scrapy spider in an isolated subprocess and return extracted items."""
        if not self._available:
            return []

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue[Any] = ctx.Queue()
        process = ctx.Process(
            target=_run_spider_subprocess, args=(spider_name, start_urls, result_queue)
        )
        process.start()

        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, result_queue.get), timeout=self._timeout_seconds
            )
        except TimeoutError:
            logger.error(
                "scrapy crawl timed out spider=%s after %ss", spider_name, self._timeout_seconds
            )
            process.terminate()
            await loop.run_in_executor(None, process.join)
            return []

        await loop.run_in_executor(None, process.join)

        if isinstance(result, Exception):
            logger.error("scrapy crawl failed spider=%s: %s", spider_name, result)
            return []
        result_list: list[dict[str, object]] = result
        return result_list
