# tests/unit/test_tasks.py
"""orchestrator/tasks.py — the rq task function that was missing entirely.

Verifies the pipeline `_run_scrape_job` drives: PROCESSING -> (scrape or
crawl) -> persist scrape_results (+ S3 snapshot) -> final status -> webhook.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import scraper_engine.orchestrator.tasks as tasks_module
from scraper_engine.core.models import FetchResult, JobStatus, JobStatusResponse


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
    monkeypatch.setattr(
        "scraper_engine.storage.s3_client.S3Client", MagicMock(return_value=s3)
    )

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
                url="http://example.com", success=True, level_used=1, duration_ms=5,
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
    status_updates = [
        c.args[2] for c in pg.execute.await_args_list if "SET status" in c.args[1]
    ]
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
        "urls": ["http://example.com"], "config_used": "{}", "webhook_url": None,
    }
    monkeypatch.setattr(
        tasks_module, "_run_scrape",
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
        tasks_module, "_run_scrape",
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
