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
from scraper_engine.orchestrator.webhook_events import WebhookEventType


@pytest.fixture
def fake_clients(monkeypatch):
    pg = AsyncMock()
    pg.fetchrow.return_value = {
        "urls": ["http://example.com"],
        "config_used": "{}",
        "webhook_url": "http://hooks.example.com/cb",
        "status": "PENDING",
    }

    async def _fetchrow(tenant_id, query, *args):
        # round 34 — WebhookOutbox.enqueue also calls fetchrow (to get the
        # generated outbox row id back); this must not be confused with the
        # scrape_jobs row fetch _run_scrape_job itself does. Reads
        # pg.fetchrow.return_value dynamically (not a captured value) so
        # individual tests can keep overriding it exactly as before.
        if "webhook_outbox" in query:
            return {"id": "outbox-1"}
        return pg.fetchrow.return_value

    pg.fetchrow.side_effect = _fetchrow
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
async def test_run_scrape_job_updates_status_and_dispatches_webhook(fake_clients, monkeypatch):
    """For the non-crawl (escalation-ladder) path, _run_scrape_job no longer
    batch-persists results itself (round 29) — _run_scrape persists each
    result incrementally via the on_result callback as it lands (see
    test_run_scrape_on_result_callback_persists_incrementally below), which
    is invisible here since _run_scrape is mocked out entirely. This test
    covers what _run_scrape_job still does directly: the PROCESSING ->
    COMPLETED status transitions and the webhook dispatch."""
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
    status_updates = [
        c.args[2]
        for c in pg.execute.await_args_list
        if "UPDATE scrape_jobs SET status" in c.args[1]
    ]
    assert status_updates == [JobStatus.PROCESSING.value, JobStatus.COMPLETED.value]

    deliver_mock.assert_awaited_once()

    pg.start.assert_awaited_once()
    pg.stop.assert_awaited_once()
    redis.start.assert_awaited_once()
    redis.stop.assert_awaited_once()
    s3.start.assert_awaited_once()
    s3.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scrape_on_result_callback_persists_incrementally(fake_clients, monkeypatch):
    """_run_scrape's on_result closure (round 29) is what actually persists
    each result as it lands, in place of the old end-of-job batch persist —
    drive it directly rather than through the full Worker.process_job loop."""
    pg, redis, s3, cfg = fake_clients

    monkeypatch.setattr(
        "scraper_engine.browser.pool.BrowserPool", MagicMock(return_value=AsyncMock())
    )
    monkeypatch.setattr(
        "scraper_engine.browser.botasaurus_pool.BotasaurusPool", MagicMock(return_value=AsyncMock())
    )
    monkeypatch.setattr("scraper_engine.browser.session_state.SessionStateManager", MagicMock())
    monkeypatch.setattr("scraper_engine.orchestrator.circuit_breaker.CircuitBreaker", MagicMock())
    monkeypatch.setattr("scraper_engine.orchestrator.politeness.PolitenessController", MagicMock())
    # round 34 — _persist_one_result awaits dlq.clear() on every success, so
    # the DLQ instance needs an async-callable clear(), not a plain MagicMock.
    monkeypatch.setattr(
        "scraper_engine.storage.dlq.DeadLetterQueue", MagicMock(return_value=AsyncMock())
    )

    captured_on_result = {}

    async def fake_process_job(tenant_id, job_id, request, on_result=None, deadline=None):
        captured_on_result["cb"] = on_result
        return JobStatusResponse(job_id=job_id, status=JobStatus.COMPLETED)

    worker_instance = MagicMock()
    worker_instance.process_job = fake_process_job
    monkeypatch.setattr(
        "scraper_engine.orchestrator.worker.Worker", MagicMock(return_value=worker_instance)
    )

    tenant_id = TenantId("system")
    request = ScrapeRequest(urls=["http://example.com"])
    await tasks_module._run_scrape(tenant_id, "job-incremental", request, redis, pg, s3, cfg)

    result = FetchResult(
        url="http://example.com", success=True, level_used=1, duration_ms=5, html="<html>hi</html>"
    )
    await captured_on_result["cb"](result)

    s3.store_snapshot.assert_awaited_once()
    insert_calls = [
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_results" in c.args[1]
    ]
    assert len(insert_calls) == 1


@pytest.mark.asyncio
async def test_run_scrape_job_skips_webhook_when_not_set(fake_clients, monkeypatch):
    pg, redis, s3, cfg = fake_clients
    pg.fetchrow.return_value = {
        "urls": ["http://example.com"],
        "config_used": "{}",
        "webhook_url": None,
        "status": "PENDING",
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
async def test_run_scrape_job_crash_marks_failed_and_reraises(fake_clients, monkeypatch):
    """A worker-level crash (e.g. the pg=None bug that used to hit here) must
    not leave scrape_jobs stuck at PROCESSING forever — mark FAILED, fire the
    webhook, then re-raise so rq's own failure bookkeeping still sees it."""
    pg, redis, s3, cfg = fake_clients
    monkeypatch.setattr(tasks_module, "_run_scrape", AsyncMock(side_effect=RuntimeError("boom")))
    deliver_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )

    with pytest.raises(RuntimeError, match="boom"):
        await tasks_module._run_scrape_job("system", "job-crash")

    status_updates = [
        c.args[2]
        for c in pg.execute.await_args_list
        if "UPDATE scrape_jobs SET status" in c.args[1]
    ]
    assert status_updates == [JobStatus.PROCESSING.value, JobStatus.FAILED.value]
    deliver_mock.assert_awaited_once()

    # cleanup must still run despite the crash
    pg.stop.assert_awaited_once()
    redis.stop.assert_awaited_once()
    s3.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scrape_job_crash_without_webhook_skips_dispatch(fake_clients, monkeypatch):
    """Same crash path, but no webhook_url on the job — must mark FAILED and
    re-raise without ever dispatching (covers the `if webhook_url:` branch's
    False side, needed for the 100% coverage gate)."""
    pg, redis, s3, cfg = fake_clients
    pg.fetchrow.return_value = {
        "urls": ["http://example.com"],
        "config_used": "{}",
        "webhook_url": None,
        "status": "PENDING",
    }
    monkeypatch.setattr(tasks_module, "_run_scrape", AsyncMock(side_effect=RuntimeError("boom")))
    deliver_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )

    with pytest.raises(RuntimeError, match="boom"):
        await tasks_module._run_scrape_job("system", "job-crash-2")

    status_updates = [
        c.args[2]
        for c in pg.execute.await_args_list
        if "UPDATE scrape_jobs SET status" in c.args[1]
    ]
    assert status_updates == [JobStatus.PROCESSING.value, JobStatus.FAILED.value]
    deliver_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scrape_job_honors_cancel_that_raced_ahead_of_dequeue(fake_clients, monkeypatch):
    """A DELETE /v1/jobs/{job_id} that lands between enqueue and rq actually
    dequeuing the job (round 29) must not get silently overwritten back to
    PROCESSING — _run_scrape_job returns immediately instead."""
    pg, redis, s3, cfg = fake_clients
    pg.fetchrow.return_value = {
        "urls": ["http://example.com"],
        "config_used": "{}",
        "webhook_url": None,
        "status": "CANCELLED",
    }
    run_scrape_mock = AsyncMock()
    monkeypatch.setattr(tasks_module, "_run_scrape", run_scrape_mock)

    await tasks_module._run_scrape_job("system", "job-precancelled")

    run_scrape_mock.assert_not_awaited()
    assert pg.execute.await_count == 0


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
        "status": "PENDING",
    }

    run_spider_mock = AsyncMock(return_value=[{"url": "http://example.com", "title": "Example"}])
    monkeypatch.setattr(
        "scraper_engine.services.scrapy_adapter.ScrapyAdapter.run_spider", run_spider_mock
    )
    _FakeProxyManager.install(monkeypatch)
    run_scrape_mock = AsyncMock()
    monkeypatch.setattr(tasks_module, "_run_scrape", run_scrape_mock)

    await tasks_module._run_scrape_job("system", "job-crawl")

    run_spider_mock.assert_awaited_once_with(
        "titles", ["http://example.com"], proxy_url=_POOL_PROXY.auth_url()
    )
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

    response = await tasks_module._run_scrape(
        tenant_id, "job-run-scrape", request, redis, pg, s3, cfg
    )

    assert response is expected_response
    browser_pool_instance.start.assert_awaited_once()
    browser_pool_instance.shutdown.assert_awaited_once()
    botasaurus_pool_instance.shutdown.assert_awaited_once()
    # process_job is called with an on_result callback (round 29 — persists
    # each result as it lands) in addition to the original positional args.
    call = worker_instance.process_job.await_args
    assert call.args == (tenant_id, "job-run-scrape", request)
    assert callable(call.kwargs["on_result"])

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
        await tasks_module._run_scrape(tenant_id, "job-run-scrape-2", request, redis, pg, s3, cfg)

    browser_pool_instance.shutdown.assert_awaited_once()
    botasaurus_pool_instance.shutdown.assert_awaited_once()


def _webhook_test_cfg():
    """round 34 — enqueue_and_deliver_webhook_event needs real numeric
    webhook config values (timedelta()/range() don't accept a MagicMock),
    even though WebhookDispatcher.deliver itself is monkeypatched below."""
    cfg = MagicMock()
    cfg.webhook.max_retries = 3
    cfg.webhook.timeout_seconds = 10
    cfg.webhook.backoff_base_seconds = 2.0
    return cfg


class TestJobWebhookEventType:
    """_job_webhook_event_type is the single source of truth mapping a
    job's terminal outcome to a WebhookEventType (round 34)."""

    def test_cancelled_status_maps_to_job_cancelled(self):
        assert (
            tasks_module._job_webhook_event_type(JobStatus.CANCELLED, False)
            == WebhookEventType.JOB_CANCELLED
        )
        # partial_failure is irrelevant once the job was cancelled.
        assert (
            tasks_module._job_webhook_event_type(JobStatus.CANCELLED, True)
            == WebhookEventType.JOB_CANCELLED
        )

    def test_completed_without_partial_failure_maps_to_job_completed(self):
        assert (
            tasks_module._job_webhook_event_type(JobStatus.COMPLETED, False)
            == WebhookEventType.JOB_COMPLETED
        )

    def test_completed_with_partial_failure_maps_to_job_partial_failure(self):
        assert (
            tasks_module._job_webhook_event_type(JobStatus.COMPLETED, True)
            == WebhookEventType.JOB_PARTIAL_FAILURE
        )

    def test_failed_status_maps_to_job_failed(self):
        assert (
            tasks_module._job_webhook_event_type(JobStatus.FAILED, False)
            == WebhookEventType.JOB_FAILED
        )


@pytest.mark.asyncio
async def test_dispatch_webhook_logs_warning_when_delivery_returns_false(monkeypatch):
    """`deliver()` returning False (not raising) means the webhook endpoint
    responded but the delivery was considered unsuccessful — logged, not
    raised, since a webhook failure must never fail the job itself."""
    deliver_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )
    pg = AsyncMock()
    pg.fetchrow.return_value = {"id": "outbox-1"}
    redis = AsyncMock()
    redis.raw = AsyncMock()

    await tasks_module._dispatch_job_webhook(
        _webhook_test_cfg(),
        pg,
        redis,
        TenantId("system"),
        "http://hooks.example.com/cb",
        "job-wh-1",
        JobStatus.COMPLETED,
        [],
        None,
        False,
    )

    deliver_mock.assert_awaited_once()
    redis.raw.incr.assert_awaited_once_with("metrics:webhook_delivery_failures_total")


@pytest.mark.asyncio
async def test_dispatch_webhook_logs_exception_when_delivery_raises(monkeypatch):
    deliver_mock = AsyncMock(side_effect=RuntimeError("network down"))
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
    )
    pg = AsyncMock()
    pg.fetchrow.return_value = {"id": "outbox-2"}
    redis = AsyncMock()
    redis.raw = AsyncMock()

    await tasks_module._dispatch_job_webhook(
        _webhook_test_cfg(),
        pg,
        redis,
        TenantId("system"),
        "http://hooks.example.com/cb",
        "job-wh-2",
        JobStatus.FAILED,
        [],
        "boom",
        False,
    )  # must not raise

    deliver_mock.assert_awaited_once()
    redis.raw.incr.assert_awaited_once_with("metrics:webhook_delivery_failures_total")


class TestTimingsColumn:
    """Round 64 — escalations share the timings JSONB column."""

    def _result(self, **kw):
        return FetchResult(url="http://x.example", success=True, level_used=1, duration_ms=1, **kw)

    def test_nothing_measured_stores_null(self):
        assert tasks_module._timings_column(self._result()) is None

    def test_timings_only(self):
        stored = tasks_module._timings_column(self._result(timings={"total_ms": 5}))
        assert json.loads(stored) == {"total_ms": 5}

    def test_escalations_ride_along(self):
        esc = [{"level": 1, "reason": "status:403", "http_status": 403, "engine": None}]
        stored = tasks_module._timings_column(
            self._result(timings={"total_ms": 5}, escalations=esc)
        )
        assert json.loads(stored) == {"total_ms": 5, "escalations": esc}


# --- Round 64: crawl proxy leasing -------------------------------------------

from scraper_engine.config.schema import AppConfig, DataImpulseConfig  # noqa: E402
from scraper_engine.core.exceptions import ProxyPoolExhaustedError  # noqa: E402
from scraper_engine.core.models import Proxy, ProxyProtocol  # noqa: E402
from scraper_engine.proxy.lease import ProxyLease  # noqa: E402

_POOL_PROXY = Proxy(id=7, ip="203.0.113.7", port=3128, protocol=ProxyProtocol.HTTP, source="pool")


class _FakeProxyManager:
    exhausted = False
    instance: "_FakeProxyManager | None" = None

    def __init__(self, **_kw):
        self.get_proxy = AsyncMock(side_effect=self._get)
        self.mark_success = AsyncMock()
        self.mark_failure = AsyncMock()
        _FakeProxyManager.instance = self

    async def _get(self, tenant_id, level, domain):
        if _FakeProxyManager.exhausted:
            raise ProxyPoolExhaustedError(domain, level, 1)
        return ProxyLease(proxy=_POOL_PROXY, tenant_id=tenant_id)

    @classmethod
    def install(cls, monkeypatch, *, exhausted=False):
        cls.exhausted = exhausted
        monkeypatch.setattr("scraper_engine.proxy.manager.ProxyManager", cls)


class TestCrawlProxy:
    """Round 64 — crawls used to leave from the server's own IP. They lease a
    proxy the way scrapes do: free pool first, gateway on exhaustion under
    free_first, gateway only under paid_only."""

    CONFIG = {"spider_name": "titles", "start_urls": ["https://example.com/a"]}

    def _run(self, monkeypatch, cfg, items):
        run_spider = AsyncMock(return_value=items)
        monkeypatch.setattr(
            "scraper_engine.services.scrapy_adapter.ScrapyAdapter.run_spider", run_spider
        )
        coro = tasks_module._run_crawl_job(
            self.CONFIG, TenantId("crawler"), cfg, MagicMock(), MagicMock()
        )
        return coro, run_spider

    @pytest.mark.asyncio
    async def test_free_pool_proxy_is_used_and_scored_on_success(self, monkeypatch):
        _FakeProxyManager.install(monkeypatch)
        coro, run_spider = self._run(monkeypatch, AppConfig(), [{"url": "https://example.com/a"}])
        results = await coro
        assert run_spider.await_args.kwargs["proxy_url"] == _POOL_PROXY.auth_url()
        assert results[0].proxy_source == "pool"
        assert results[0].proxy_used == _POOL_PROXY.key()
        _FakeProxyManager.instance.mark_success.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_empty_crawl_marks_the_pool_proxy_failed(self, monkeypatch):
        _FakeProxyManager.install(monkeypatch)
        coro, _ = self._run(monkeypatch, AppConfig(), [])
        assert await coro == []
        _FakeProxyManager.instance.mark_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exhausted_pool_falls_back_to_the_gateway_under_free_first(self, monkeypatch):
        _FakeProxyManager.install(monkeypatch, exhausted=True)
        gateway = Proxy(
            id=-1,
            ip="gw.example",
            port=823,
            protocol=ProxyProtocol.HTTP,
            username="u",
            password="p",
            source="paid_gateway",
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", lambda **_k: gateway
        )
        cfg = AppConfig(dataimpulse=DataImpulseConfig(enabled=True, strategy="free_first"))
        coro, run_spider = self._run(monkeypatch, cfg, [{"url": "https://example.com/a"}])
        results = await coro
        assert run_spider.await_args.kwargs["proxy_url"] == "http://u:p@gw.example:823"
        assert results[0].proxy_source == "paid_gateway"
        _FakeProxyManager.instance.mark_success.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exhausted_pool_without_a_gateway_fails_instead_of_going_direct(
        self, monkeypatch
    ):
        _FakeProxyManager.install(monkeypatch, exhausted=True)
        coro, run_spider = self._run(monkeypatch, AppConfig(), [])
        with pytest.raises(ProxyPoolExhaustedError):
            await coro
        run_spider.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_paid_only_never_touches_the_pool(self, monkeypatch):
        _FakeProxyManager.install(monkeypatch)
        gateway = Proxy(
            id=-1, ip="gw.example", port=823, protocol=ProxyProtocol.HTTP, source="paid_gateway"
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", lambda **_k: gateway
        )
        cfg = AppConfig(dataimpulse=DataImpulseConfig(enabled=True, strategy="paid_only"))
        coro, _ = self._run(monkeypatch, cfg, [])
        await coro
        _FakeProxyManager.instance.get_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unconfigured_gateway_is_a_loud_error(self, monkeypatch):
        _FakeProxyManager.install(monkeypatch)
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", lambda **_k: None
        )
        cfg = AppConfig(dataimpulse=DataImpulseConfig(enabled=True, strategy="paid_only"))
        coro, _ = self._run(monkeypatch, cfg, [])
        with pytest.raises(RuntimeError, match="gateway is not configured"):
            await coro


@pytest.mark.asyncio
async def test_a_crawl_with_no_seeds_scores_no_proxy(monkeypatch):
    """Nothing was asked of the proxy, so an empty result says nothing about it."""
    _FakeProxyManager.install(monkeypatch)
    monkeypatch.setattr(
        "scraper_engine.services.scrapy_adapter.ScrapyAdapter.run_spider",
        AsyncMock(return_value=[]),
    )
    results = await tasks_module._run_crawl_job(
        {"spider_name": "titles", "start_urls": []},
        TenantId("crawler"),
        AppConfig(),
        MagicMock(),
        MagicMock(),
    )
    assert results == []
    _FakeProxyManager.instance.mark_success.assert_not_awaited()
    _FakeProxyManager.instance.mark_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scrape_job_tolerates_a_provider_without_force_flush(fake_clients, monkeypatch):
    """Round 64 (branch burn-down) — the default no-op tracer provider has no
    force_flush at all; the job's finally block must not assume one."""
    from opentelemetry import trace

    class _BareProvider:
        def get_tracer(self, *_a, **_k):
            return trace.NoOpTracer()

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: _BareProvider())
    monkeypatch.setattr(
        tasks_module,
        "_run_scrape",
        AsyncMock(return_value=JobStatusResponse(job_id="job-noflush", status=JobStatus.COMPLETED)),
    )
    monkeypatch.setattr(
        "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
        AsyncMock(return_value=True),
    )
    pg, *_ = fake_clients

    await tasks_module._run_scrape_job("system", "job-noflush")

    assert pg.stop.await_count == 1


def _patch_run_scrape_collaborators(monkeypatch):
    browser_pool_cls = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr("scraper_engine.browser.pool.BrowserPool", browser_pool_cls)
    monkeypatch.setattr(
        "scraper_engine.browser.botasaurus_pool.BotasaurusPool",
        MagicMock(return_value=AsyncMock()),
    )
    for name in (
        "scraper_engine.browser.session_state.SessionStateManager",
        "scraper_engine.orchestrator.circuit_breaker.CircuitBreaker",
        "scraper_engine.orchestrator.politeness.PolitenessController",
        "scraper_engine.storage.dlq.DeadLetterQueue",
    ):
        monkeypatch.setattr(name, MagicMock())
    worker_instance = MagicMock()
    worker_instance.process_job = AsyncMock(
        return_value=JobStatusResponse(job_id="j", status=JobStatus.COMPLETED)
    )
    worker_cls = MagicMock(return_value=worker_instance)
    monkeypatch.setattr("scraper_engine.orchestrator.worker.Worker", worker_cls)
    return browser_pool_cls, worker_cls, worker_instance


class TestHostAdmissionWiring:
    """Round 65 — _run_scrape builds the host admission layer, stops
    prewarming browsers outside it, and hands the worker rq's deadline."""

    @pytest.mark.asyncio
    async def test_enabled_builds_admission_and_disables_prewarm(self, fake_clients, monkeypatch):
        from scraper_engine.config.schema import HostCapacityConfig
        from scraper_engine.orchestrator.host_capacity import HostAdmission

        pg, redis, s3, cfg = fake_clients
        cfg.host_capacity = HostCapacityConfig(enabled=True)
        monkeypatch.setattr(tasks_module, "_job_deadline", lambda: 1234.5)
        pool_cls, worker_cls, worker = _patch_run_scrape_collaborators(monkeypatch)
        request = ScrapeRequest(urls=["http://example.com"])

        await tasks_module._run_scrape(TenantId("system"), "j", request, redis, pg, s3, cfg)

        assert pool_cls.call_args.kwargs["prewarm_count"] == 0
        assert isinstance(worker_cls.call_args.kwargs["admission"], HostAdmission)
        assert worker.process_job.await_args.kwargs["deadline"] == 1234.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("max_level", "prewarm"), [(None, 2), (3, 2), (1, 0)])
    async def test_disabled_keeps_prewarm_unless_no_browser_level_is_reachable(
        self, fake_clients, monkeypatch, max_level, prewarm
    ):
        from scraper_engine.config.schema import HostCapacityConfig
        from scraper_engine.core.models import ConfigOverrides

        pg, redis, s3, cfg = fake_clients
        cfg.host_capacity = HostCapacityConfig(enabled=False)
        pool_cls, worker_cls, _ = _patch_run_scrape_collaborators(monkeypatch)
        overrides = ConfigOverrides(max_level=max_level) if max_level else None
        request = ScrapeRequest(urls=["http://example.com"], config_overrides=overrides)

        await tasks_module._run_scrape(TenantId("system"), "j", request, redis, pg, s3, cfg)

        assert pool_cls.call_args.kwargs["prewarm_count"] == prewarm
        assert worker_cls.call_args.kwargs["admission"] is None


class TestJobDeadline:
    def test_none_outside_an_rq_job(self, monkeypatch):
        monkeypatch.setattr("rq.get_current_job", lambda: None)
        assert tasks_module._job_deadline() is None

    def test_none_without_a_timeout(self, monkeypatch):
        job = MagicMock(timeout=None)
        monkeypatch.setattr("rq.get_current_job", lambda: job)
        assert tasks_module._job_deadline() is None

    def test_none_before_the_job_has_started(self, monkeypatch):
        job = MagicMock(timeout=600, started_at=None)
        monkeypatch.setattr("rq.get_current_job", lambda: job)
        assert tasks_module._job_deadline() is None

    @pytest.mark.parametrize("aware", [True, False])
    def test_remaining_time_from_rq_start_and_timeout(self, monkeypatch, aware):
        import time
        from datetime import UTC, datetime, timedelta

        started = datetime.now(UTC) - timedelta(seconds=100)
        if not aware:
            started = started.replace(tzinfo=None)  # rq stores naive UTC
        job = MagicMock(timeout=600, started_at=started)
        monkeypatch.setattr("rq.get_current_job", lambda: job)
        remaining = tasks_module._job_deadline() - time.monotonic()
        assert 495 < remaining <= 500
