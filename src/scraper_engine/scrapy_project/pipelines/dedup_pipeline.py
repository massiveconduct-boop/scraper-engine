# scrapy_project/pipelines/dedup_pipeline.py
"""Drop an item whose URL this crawl already produced.

Round 64 — this was a stub that only bumped a stats counter ("Deferred:
actual dedup check") and passed everything through. A crawl whose seeds
redirect to the same final URL, or that lists a URL twice, returned
duplicate items and so persisted duplicate scrape_results rows. One crawl
runs in one subprocess, so an in-memory set is exactly the right scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scrapy.exceptions import DropItem

if TYPE_CHECKING:
    from scrapy import Spider
    from scrapy.crawler import Crawler


class DedupPipeline:
    def __init__(self, crawler: Crawler) -> None:
        self._stats = crawler.stats
        self._seen: set[str] = set()

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DedupPipeline:
        return cls(crawler)

    def process_item(self, item: Any, spider: Spider) -> Any:
        url = str(item.get("url", ""))
        if url in self._seen:
            if self._stats:
                self._stats.inc_value("pipeline/dedup_dropped")
            raise DropItem(f"duplicate url: {url}")
        self._seen.add(url)
        return item
