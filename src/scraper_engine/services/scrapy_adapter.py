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
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300
_SETTINGS_MODULE = "scraper_engine.scrapy_project.settings"


def _run_spider_subprocess(
    spider_name: str,
    urls: list[str],
    result_queue: multiprocessing.Queue[Any],
    proxy_url: str | None = None,
) -> None:
    """Entry point for the spawned child process — safe to start a fresh
    Twisted reactor here regardless of how many crawls the parent has run."""
    # Round 64 — named explicitly. get_project_settings() otherwise finds the
    # settings module only by walking up from the CHILD's working directory
    # to scrapy.cfg, so a worker started anywhere else silently crawled with
    # Scrapy's defaults: no SSRF middleware, no proxy, no dedup.
    os.environ["SCRAPY_SETTINGS_MODULE"] = _SETTINGS_MODULE
    try:
        from scrapy import signals
        from scrapy.crawler import CrawlerProcess
        from scrapy.spiders import Spider
        from scrapy.utils.project import get_project_settings

        items: list[dict[str, object]] = []

        def _on_item_scraped(item: dict[str, object], **_kwargs: object) -> None:
            items.append(dict(item))

        class _DynamicSpider(Spider):
            # NOTE: the class attribute must not share a name with the
            # closure variable it reads (e.g. `start_urls = start_urls`) —
            # any name assigned anywhere in a class body is local to that
            # body for its entire execution, which shadows the closure
            # variable and raises NameError when the RHS is evaluated.
            name = spider_name
            start_urls = urls

            def parse(self, response: object) -> object:
                yield {"url": response.url, "title": response.css("title::text").get()}  # type: ignore[attr-defined]

        settings = get_project_settings()
        settings.set("CRAWL_PROXY_URL", proxy_url, priority="cmdline")
        process = CrawlerProcess(settings)
        # Round 64 — items are collected from `item_scraped`, which fires only
        # for items that made it through ITEM_PIPELINES. They used to be
        # appended inside parse(), BEFORE the pipelines ran, so an item the
        # DedupPipeline dropped was still returned and persisted — caught
        # live: `pipeline/dedup_dropped: 1` in the crawl stats and both
        # copies in the API response.
        crawler = process.create_crawler(_DynamicSpider)
        crawler.signals.connect(_on_item_scraped, signal=signals.item_scraped)
        process.crawl(crawler)
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
        self, spider_name: str, start_urls: list[str], proxy_url: str | None = None
    ) -> list[dict[str, object]]:
        """Run a Scrapy spider in an isolated subprocess and return extracted
        items. `proxy_url` (auth included) is attached to every request the
        crawl sends — see scrapy_project/middlewares/proxy_middleware.py."""
        if not self._available:
            return []

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue[Any] = ctx.Queue()
        process = ctx.Process(
            target=_run_spider_subprocess,
            args=(spider_name, start_urls, result_queue, proxy_url),
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
