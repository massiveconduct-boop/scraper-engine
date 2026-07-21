# scrapy_project/settings.py
"""Scrapy project settings — used by services/scrapy_adapter.py for bulk crawls."""

BOT_NAME = "scraper_engine"

SPIDER_MODULES = ["scrapy_project.spiders"]
NEWSPIDER_MODULE = "scrapy_project.spiders"

ROBOTSTXT_OBEY = False

CONCURRENT_REQUESTS = 16
DOWNLOAD_DELAY = 2.0

DOWNLOADER_MIDDLEWARES: dict[str, int] = {}

ITEM_PIPELINES: dict[str, int] = {}
