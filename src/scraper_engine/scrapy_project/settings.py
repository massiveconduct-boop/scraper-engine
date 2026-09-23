# scrapy_project/settings.py
"""Scrapy project settings — used by services/scrapy_adapter.py for bulk crawls."""

BOT_NAME = "scraper_engine"

# No SPIDER_MODULES: services/scrapy_adapter.py defines its spider inline
# (round 64 removed the never-loaded spiders/generic_spider.py).

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
# SSRFMiddleware first so a blocked hop is never proxied or fetched (round
# 64 — invariant #4 on every redirect). TenantMiddleware was removed: it
# copied a `tenant_id` the inline spider never has.
DOWNLOADER_MIDDLEWARES: dict[str, int] = {
    "scraper_engine.scrapy_project.middlewares.ssrf_middleware.SSRFMiddleware": 50,
    "scraper_engine.scrapy_project.middlewares.proxy_middleware.ProxyMiddleware": 200,
}

# Leased by orchestrator/tasks.py::_run_crawl_job and set per crawl by
# services/scrapy_adapter.py; None only when no proxy could be leased at all.
CRAWL_PROXY_URL: str | None = None

# Pipelines — lower number = higher priority
# StoragePipeline was removed (round 64): it only bumped a counter;
# persistence is orchestrator/tasks.py's job once items cross back.
ITEM_PIPELINES: dict[str, int] = {
    "scraper_engine.scrapy_project.pipelines.dedup_pipeline.DedupPipeline": 100,
}

# Logging
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
