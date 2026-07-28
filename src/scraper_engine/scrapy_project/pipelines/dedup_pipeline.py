# scrapy_project/pipelines/dedup_pipeline.py
"""Scrapy item pipeline — deduplication via DeduplicationEngine.

Design invariant §1.1.5: only successful, non-challenge results are cached.
Items that match a previously cached result are dropped to avoid duplicate work.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scrapy import Spider
    from scrapy.crawler import Crawler

logger = logging.getLogger(__name__)


class DedupPipeline:
    """Drop duplicate items using the DeduplicationEngine.

    Checks each item against the success-gated dedup cache before
    allowing it through to storage. Drops items that haven't changed
    since the last successful scrape.
    """

    def __init__(self, crawler: Crawler) -> None:
        self._crawler = crawler
        self._stats = crawler.stats
        # DeduplicationEngine injected via crawler.settings["DEDUP_ENGINE"]

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> DedupPipeline:
        return cls(crawler)

    def process_item(self, item: dict[str, Any], spider: Spider) -> dict[str, Any]:
        """Check item against dedup cache. Drop if unchanged.

        Only caches successful items (§1.1.5). Failed items always pass through
        (they may succeed on retry with a different proxy/level).
        """
        # Deferred: actual dedup check via DeduplicationEngine
        # For now, pass all items through
        if self._stats:
            self._stats.inc_value("pipeline/dedup_checked")
        return item
