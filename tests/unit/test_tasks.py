# tests/unit/test_tasks.py
"""orchestrator/tasks.py — the rq task function that was missing entirely.

Verifies the pipeline `_run_scrape_job` drives: PROCESSING -> (scrape or
crawl) -> persist scrape_results (+ S3 snapshot) -> final status -> webhook.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import scraper_engine.orchestrator.tasks as tasks_module
from scraper_engine.core.models import (
    FetchResult,
    JobStatus,
    JobStatusResponse,
    ScrapeRequest,
)
from scraper_engine.core.tenant import TenantId


@pytest.fixture
def fake_clients(monkeypatch):
    pg = AsyncMock()
    pg.fetchrow.return_value = {
        "urls": ["http://example.com"],
        "config_used": "{}",
        "webhook_url": "http://hooks.example.com/cb",
    }
    redis = AsyncMock()
    redis.raw = AsyncMock()
    s3 = AsyncMock()
    s3.store_snapshot.return_value = "snapshots/system/job-1/key.html"

    monkeypatch.setattr(
        "scraper_engine.storage.postgres_client.PostgresClient", MagicMock(return_value=pg)
    )
    monkeypatch.setattr(
        "scraper_engine.storage.redis_client.RedisClient", MagicMock(return_value=redis)
    )
    monkeypatch.setattr("scraper_engine.storage.s3_client.S3Client", MagicMock(return_value=s3))

    cfg = MagicMock()
    cfg.storage.database_url = "postgresql://x/db"
    cfg.storage.redis_url = "redis://x/0"
    cfg.s3.endpoint_url = "http://minio:9000"
    cfg.s3.access_key = "k"
    cfg.s3.secret_key = "s"
    cfg.s3.bucket = "b"
    monkeypatch.setattr("scraper_engine.config.loader.load_config", MagicMock(return_value=cfg))

    return pg, redis, s3, cfg


@pytest.mark.asyncio
async def test_run_scrape_job_persists_results_and_dispatches_webhook(fake_clients, monkeypatch):
    pg, redis, s3, cfg = fake_clients

    response = JobStatusResponse(
        job_id="job-1",
        status=JobStatus.COMPLETED,
        results=[
            FetchResult(
                url="http://example.com",
                success=True,
                level_used=1,
                duration_ms=5,
                html="<html>hi</html>",
            )
        ],
    )
    run_scrape_mock = AsyncMock(return_value=response)
    monkeypatch.setattr(tasks_module, "_run_scrape", run_scrape_mock)

    deliver_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )

    await tasks_module._run_scrape_job("system", "job-1")

    # execute(tenant_id, query, *args) -> args[0]=tenant_id, args[1]=query
    status_updates = [c.args[2] for c in pg.execute.await_args_list if "SET status" in c.args[1]]
    assert status_updates == [JobStatus.PROCESSING.value, JobStatus.COMPLETED.value]

    insert_calls = [
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_results" in c.args[1]
    ]
    assert len(insert_calls) == 1

    s3.store_snapshot.assert_awaited_once()
    deliver_mock.assert_awaited_once()

    pg.start.assert_awaited_once()
    pg.stop.assert_awaited_once()
    redis.start.assert_awaited_once()
    redis.stop.assert_awaited_once()
    s3.start.assert_awaited_once()
    s3.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scrape_job_skips_webhook_when_not_set(fake_clients, monkeypatch):
    pg, redis, s3, cfg = fake_clients
    pg.fetchrow.return_value = {
        "urls": ["http://example.com"],
        "config_used": "{}",
        "webhook_url": None,
    }
    monkeypatch.setattr(
        tasks_module,
        "_run_scrape",
        AsyncMock(return_value=JobStatusResponse(job_id="job-2", status=JobStatus.COMPLETED)),
    )
    deliver_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )

    await tasks_module._run_scrape_job("system", "job-2")

    deliver_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scrape_job_missing_row_returns_without_crashing(fake_clients):
    pg, redis, s3, cfg = fake_clients
    pg.fetchrow.return_value = None

    await tasks_module._run_scrape_job("system", "nonexistent-job")

    # never got far enough to touch status/results
    assert pg.execute.await_count == 0
    pg.start.assert_awaited_once()
    pg.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scrape_job_crawl_type_routes_to_scrapy_adapter(fake_clients, monkeypatch):
    pg, redis, s3, cfg = fake_clients
    pg.fetchrow.return_value = {
        "urls": [],
        "config_used": json.dumps(
            {"_job_type": "crawl", "spider_name": "titles", "start_urls": ["http://example.com"]}
        ),
        "webhook_url": None,
    }

    run_spider_mock = AsyncMock(return_value=[{"url": "http://example.com", "title": "Example"}])
    monkeypatch.setattr(
        "scraper_engine.services.scrapy_adapter.ScrapyAdapter.run_spider", run_spider_mock
    )
    run_scrape_mock = AsyncMock()
    monkeypatch.setattr(tasks_module, "_run_scrape", run_scrape_mock)

    await tasks_module._run_scrape_job("system", "job-crawl")

    run_spider_mock.assert_awaited_once_with("titles", ["http://example.com"])
    run_scrape_mock.assert_not_awaited()

    insert_calls = [
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_results" in c.args[1]
    ]
    assert len(insert_calls) == 1


@pytest.mark.asyncio
async def test_run_scrape_job_creates_traced_span_with_job_attributes(fake_clients, monkeypatch):
    """Regression test for a real bug: rq's work-horse process exits via
    os._exit() (rq/worker/base.py), bypassing atexit — BatchSpanProcessor's
    background export thread also doesn't survive fork() at all — so without
    the explicit force_flush() in _run_scrape_job's finally block, every
    job's span was silently dropped (confirmed live against a real rq
    worker; the same code invoked directly, not via a forked work-horse,
    worked immediately). This asserts the span actually exists with the
    right attributes, not just that force_flush() doesn't crash."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    pg, redis, s3, cfg = fake_clients
    monkeypatch.setattr(
        tasks_module,
        "_run_scrape",
        AsyncMock(return_value=JobStatusResponse(job_id="job-span", status=JobStatus.COMPLETED)),
    )
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
        AsyncMock(return_value=True),
    )

    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))

    await tasks_module._run_scrape_job("system", "job-span")

    scrape_spans = [s for s in exporter.get_finished_spans() if s.name == "scrape_job"]
    assert len(scrape_spans) == 1
    assert scrape_spans[0].attributes["job_id"] == "job-span"
    assert scrape_spans[0].attributes["tenant_id"] == "system"


def test_run_scrape_job_sync_wrapper_runs_the_coroutine(monkeypatch):
    called = {}

    async def fake_run_scrape_job(tenant_id, job_id):
        called["args"] = (tenant_id, job_id)

    monkeypatch.setattr(tasks_module, "_run_scrape_job", fake_run_scrape_job)
    tasks_module.run_scrape_job("system", "job-x")

    assert called["args"] == ("system", "job-x")


@pytest.mark.asyncio
async def test_run_scrape_job_metrics_update_failure_is_swallowed(fake_clients, monkeypatch):
    """The Redis-backed job_duration counter update is best-effort — a Redis
    hiccup here must not blow up an otherwise-successful job. Covers the
    `except Exception: logger.warning(...)` guard around the metric writes."""
    pg, redis, s3, cfg = fake_clients
    redis.raw.incr = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(
        tasks_module,
        "_run_scrape",
        AsyncMock(return_value=JobStatusResponse(job_id="job-metrics", status=JobStatus.COMPLETED)),
    )
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
        AsyncMock(return_value=True),
    )

    await tasks_module._run_scrape_job("system", "job-metrics")  # must not raise

    # finally block (s3/redis/pg .stop()) still ran despite the metrics failure
    pg.stop.assert_awaited_once()
    redis.stop.assert_awaited_once()
    s3.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scrape_builds_worker_and_brackets_pool_lifecycle(fake_clients, monkeypatch):
    """`_run_scrape` (the non-crawl job path) is monkeypatched away by every
    other test in this file, so its own body — building CircuitBreaker/
    PolitenessController/DLQ/BrowserPool/BotasaurusPool/Worker from config and
    bracketing the pools' lifetime around Worker.process_job — was never
    actually exercised. Drives the real function; only its collaborators
    (constructed via local imports inside _run_scrape) are mocked."""
    pg, redis, s3, cfg = fake_clients

    browser_pool_instance = AsyncMock()
    monkeypatch.setattr(
        "scraper_engine.browser.pool.BrowserPool",
        MagicMock(return_value=browser_pool_instance),
    )
    botasaurus_pool_instance = AsyncMock()
    monkeypatch.setattr(
        "scraper_engine.browser.botasaurus_pool.BotasaurusPool",
        MagicMock(return_value=botasaurus_pool_instance),
    )
    monkeypatch.setattr("scraper_engine.browser.session_state.SessionStateManager", MagicMock())
    monkeypatch.setattr("scraper_engine.orchestrator.circuit_breaker.CircuitBreaker", MagicMock())
    monkeypatch.setattr("scraper_engine.orchestrator.politeness.PolitenessController", MagicMock())
    monkeypatch.setattr("scraper_engine.storage.dlq.DeadLetterQueue", MagicMock())

    expected_response = JobStatusResponse(job_id="job-run-scrape", status=JobStatus.COMPLETED)
    worker_instance = MagicMock()
    worker_instance.process_job = AsyncMock(return_value=expected_response)
    worker_cls = MagicMock(return_value=worker_instance)
    monkeypatch.setattr("scraper_engine.orchestrator.worker.Worker", worker_cls)

    tenant_id = TenantId("system")
    request = ScrapeRequest(urls=["http://example.com"])

    response = await tasks_module._run_scrape(tenant_id, "job-run-scrape", request, redis, pg, cfg)

    assert response is expected_response
    browser_pool_instance.start.assert_awaited_once()
    browser_pool_instance.shutdown.assert_awaited_once()
    botasaurus_pool_instance.shutdown.assert_awaited_once()
    worker_instance.process_job.assert_awaited_once_with(tenant_id, "job-run-scrape", request)

    _, worker_kwargs = worker_cls.call_args
    assert worker_kwargs["browser_pool"] is browser_pool_instance
    assert worker_kwargs["botasaurus_pool"] is botasaurus_pool_instance
    assert worker_kwargs["redis"] is redis
    assert worker_kwargs["pg"] is pg
    assert worker_kwargs["config"] is cfg


@pytest.mark.asyncio
async def test_run_scrape_shuts_down_pools_even_if_process_job_raises(fake_clients, monkeypatch):
    """The pool shutdown is in a `finally` — a fetch-level crash mid-job must
    not leak a live Camoufox/Botasaurus instance."""
    pg, redis, s3, cfg = fake_clients

    browser_pool_instance = AsyncMock()
    monkeypatch.setattr(
        "scraper_engine.browser.pool.BrowserPool",
        MagicMock(return_value=browser_pool_instance),
    )
    botasaurus_pool_instance = AsyncMock()
    monkeypatch.setattr(
        "scraper_engine.browser.botasaurus_pool.BotasaurusPool",
        MagicMock(return_value=botasaurus_pool_instance),
    )
    monkeypatch.setattr("scraper_engine.browser.session_state.SessionStateManager", MagicMock())
    monkeypatch.setattr("scraper_engine.orchestrator.circuit_breaker.CircuitBreaker", MagicMock())
    monkeypatch.setattr("scraper_engine.orchestrator.politeness.PolitenessController", MagicMock())
    monkeypatch.setattr("scraper_engine.storage.dlq.DeadLetterQueue", MagicMock())

    worker_instance = MagicMock()
    worker_instance.process_job = AsyncMock(side_effect=RuntimeError("fetch pipeline exploded"))
    monkeypatch.setattr(
        "scraper_engine.orchestrator.worker.Worker", MagicMock(return_value=worker_instance)
    )

    tenant_id = TenantId("system")
    request = ScrapeRequest(urls=["http://example.com"])

    with pytest.raises(RuntimeError, match="fetch pipeline exploded"):
        await tasks_module._run_scrape(tenant_id, "job-run-scrape-2", request, redis, pg, cfg)

    browser_pool_instance.shutdown.assert_awaited_once()
    botasaurus_pool_instance.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_webhook_logs_warning_when_delivery_returns_false(monkeypatch):
    """`deliver()` returning False (not raising) means the webhook endpoint
    responded but the delivery was considered unsuccessful — logged, not
    raised, since a webhook failure must never fail the job itself."""
    deliver_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )

    await tasks_module._dispatch_webhook(
        "http://hooks.example.com/cb", "job-wh-1", JobStatus.COMPLETED, [], None
    )

    deliver_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_webhook_logs_exception_when_delivery_raises(monkeypatch):
    deliver_mock = AsyncMock(side_effect=RuntimeError("network down"))
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )

    await tasks_module._dispatch_webhook(
        "http://hooks.example.com/cb", "job-wh-2", JobStatus.FAILED, [], "boom"
    )  # must not raise

    deliver_mock.assert_awaited_once()
