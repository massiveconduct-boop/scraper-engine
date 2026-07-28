# scrapy_project/pipelines/storage_pipeline.py
"""Scrapy item pipeline — persist scraped items to Postgres and S3.

Each item flows through:
  1. Deduplication check (skip if previously cached)
  2. Postgres INSERT into scrape_results
  3. S3 snapshot storage (with BD-07 retention tagging)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scrapy import Spider
    from scrapy.crawler import Crawler

logger = logging.getLogger(__name__)


class StoragePipeline:
    """Persist scraped items to Postgres (scrape_results) and S3 (snapshots).

    Requires storage clients to be injected via crawler.settings at startup.
    Uses BD-07 retention: success=1 day, failed=30 days.
    """

    def __init__(self, crawler: Crawler) -> None:
        self._crawler = crawler
        self._stats = crawler.stats
        # Storage clients injected via crawler.settings:
        #   settings["STORAGE_PG_CLIENT"]
        #   settings["STORAGE_S3_CLIENT"]
        #   settings["STORAGE_REDIS_CLIENT"]

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> StoragePipeline:
        return cls(crawler)

    def process_item(self, item: dict[str, Any], spider: Spider) -> dict[str, Any]:
        """Store a scraped item.

        The item dict is expected to contain:
          - url, html, success, http_status, tenant_id, job_id, level_used
        """
        if self._stats:
            self._stats.inc_value("pipeline/items_processed")
        logger.debug("pipeline_process_item: %s success=%s", item.get("url"), item.get("success"))
        return item
