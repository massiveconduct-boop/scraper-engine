# scrapy_project/settings.py
"""Scrapy project settings — used by services/scrapy_adapter.py for bulk crawls."""

BOT_NAME = "scraper_engine"

SPIDER_MODULES = ["scrapy_project.spiders"]
NEWSPIDER_MODULE = "scrapy_project.spiders"

ROBOTSTXT_OBEY = False

# Concurrency + politeness
CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 8
DOWNLOAD_DELAY = 2.0
RANDOMIZE_DOWNLOAD_DELAY = True

# Auto-throttle (adaptive politeness)
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 60.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

# Retry
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 522, 524, 408, 429]

# Timeouts
DOWNLOAD_TIMEOUT = 30

# Middlewares — lower number = higher priority (closer to engine)
DOWNLOADER_MIDDLEWARES: dict[str, int] = {
    "scrapy_project.middlewares.tenant_middleware.TenantMiddleware": 100,
    "scrapy_project.middlewares.proxy_middleware.ProxyMiddleware": 200,
}

# Pipelines — lower number = higher priority
ITEM_PIPELINES: dict[str, int] = {
    "scrapy_project.pipelines.dedup_pipeline.DedupPipeline": 100,
    "scrapy_project.pipelines.storage_pipeline.StoragePipeline": 200,
}

# Logging
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
