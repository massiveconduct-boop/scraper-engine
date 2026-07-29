# tests/unit/test_api_routes.py
"""API route regressions.

get_job UUID coercion: `scrape_jobs.job_id` is a Postgres UUID; asyncpg returns
it as a `uuid.UUID`, but `JobStatusResponse.job_id` is typed `str`. The route
must `str()` it or Pydantic raises and the endpoint 500s on every existing job
(round 16 — caught by the full-stack e2e smoke).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import scraper_engine.api.dependencies as deps
from scraper_engine.api.routes import crawl, get_job, health, register_routes, scrape
from scraper_engine.config.schema import AppConfig
from scraper_engine.core.models import JobStatus
from scraper_engine.core.ssrf_guard import SSRFGuard


@pytest.fixture
def wired_deps(monkeypatch):
    """Wire module-level deps get_job reads: a resolver and a PG returning a row
    whose job_id is a real uuid.UUID (as asyncpg does)."""
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)

    pg = AsyncMock()
    monkeypatch.setattr(deps, "_storage_pg", pg)
    return pg


@pytest.mark.asyncio
async def test_get_job_coerces_uuid_job_id_to_str(wired_deps):
    jid = uuid.uuid4()
    # First fetch() is the scrape_jobs lookup, second is the scrape_results join.
    wired_deps.fetch.side_effect = [[{"job_id": jid, "status": "PENDING"}], []]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    # the bug: passing the raw UUID would raise pydantic ValidationError (500).
    assert resp.job_id == str(jid)
    assert isinstance(resp.job_id, str)
    assert resp.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_get_job_missing_row_404(wired_deps):
    from fastapi import HTTPException

    wired_deps.fetch.return_value = []
    with pytest.raises(HTTPException) as ei:
        await get_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 404


@pytest.fixture
def wired_scrape_deps(monkeypatch):
    """POST /v1/scrape needs tenant resolver + pg + redis + queue wired, and
    SSRF validation stubbed out (real DNS resolution is out of scope for a
    unit test)."""
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)

    pg = AsyncMock()
    pg.fetchrow.return_value = None  # no tenant row -> default quota
    monkeypatch.setattr(deps, "_storage_pg", pg)

    redis = AsyncMock()
    monkeypatch.setattr(deps, "_storage_redis", redis)

    queue = MagicMock()
    monkeypatch.setattr(deps, "_queue", queue)

    # routes.py now reads the shared deps._ssrf_guard singleton instead of
    # constructing a fresh SSRFGuard() per request (api/main.py lifespan).
    monkeypatch.setattr(deps, "_ssrf_guard", SSRFGuard())
    monkeypatch.setattr(
        "scraper_engine.core.ssrf_guard.SSRFGuard.validate", AsyncMock(return_value=None)
    )

    return pg, redis, queue


@pytest.mark.asyncio
async def test_scrape_enqueues_after_persisting_job(wired_scrape_deps):
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    request = ScrapeRequest(urls=["http://example.com"])

    resp = await scrape(request, x_api_key="sk-admin")

    queue.enqueue.assert_called_once()
    call_args = queue.enqueue.call_args
    assert call_args.args[0] == "scraper_engine.orchestrator.tasks.run_scrape_job"
    assert call_args.args[1] == "system"
    assert call_args.args[2] == resp["job_id"]


@pytest.mark.asyncio
async def test_crawl_enqueues_with_crawl_job_type(wired_scrape_deps):
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    resp = await crawl(request, x_api_key="sk-admin")

    queue.enqueue.assert_called_once()
    call_args = queue.enqueue.call_args
    assert call_args.args[0] == "scraper_engine.orchestrator.tasks.run_scrape_job"
    assert call_args.args[2] == resp["job_id"]

    # execute(tenant_id, query, job_id, urls, config_used, status, webhook)
    insert_call = next(
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_jobs" in c.args[1]
    )
    config_used = insert_call.args[4]
    assert '"_job_type": "crawl"' in config_used
    assert '"spider_name": "titles"' in config_used


# ---------------------------------------------------------------------------
# get_job — error branches (invalid UUID, uninitialized deps, auth failure,
# no-DB-configured fallback). test_get_job_coerces_uuid_job_id_to_str and
# test_get_job_missing_row_404 above only cover the happy/404 paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_invalid_uuid_returns_422():
    with pytest.raises(HTTPException) as ei:
        await get_job("not-a-uuid", x_api_key="sk-admin")
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_get_job_uninitialized_tenant_resolver_returns_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await get_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_get_job_authentication_error_returns_401(monkeypatch):
    from scraper_engine.core.exceptions import AuthenticationError

    resolver = AsyncMock()
    resolver.resolve.side_effect = AuthenticationError()
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)

    with pytest.raises(HTTPException) as ei:
        await get_job(str(uuid.uuid4()), x_api_key="sk-bad")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_get_job_without_pg_configured_returns_pending_fallback(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    monkeypatch.setattr(deps, "_storage_pg", None)

    jid = str(uuid.uuid4())
    resp = await get_job(jid, x_api_key="sk-admin")

    assert resp.job_id == jid
    assert resp.status == JobStatus.PENDING
    assert resp.progress == 0.0


# ---------------------------------------------------------------------------
# scrape/crawl — shared error branches: uninitialized deps, auth failure,
# SSRF block, quota-row-present, and quota-exceeded. wired_scrape_deps
# already wires the happy path; these tests perturb one thing at a time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scrape_uninitialized_tenant_resolver_returns_503(monkeypatch):
    from scraper_engine.core.models import ScrapeRequest

    monkeypatch.setattr(deps, "_tenant_resolver", None)
    request = ScrapeRequest(urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await scrape(request, x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_scrape_authentication_error_returns_401(monkeypatch):
    from scraper_engine.core.exceptions import AuthenticationError
    from scraper_engine.core.models import ScrapeRequest

    resolver = AsyncMock()
    resolver.resolve.side_effect = AuthenticationError()
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    request = ScrapeRequest(urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await scrape(request, x_api_key="sk-bad")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_scrape_uninitialized_ssrf_guard_returns_503(wired_scrape_deps, monkeypatch):
    from scraper_engine.core.models import ScrapeRequest

    monkeypatch.setattr(deps, "_ssrf_guard", None)
    request = ScrapeRequest(urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await scrape(request, x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_scrape_ssrf_blocked_url_returns_403(wired_scrape_deps, monkeypatch):
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import ScrapeRequest

    monkeypatch.setattr(
        "scraper_engine.core.ssrf_guard.SSRFGuard.validate",
        AsyncMock(
            side_effect=SSRFBlockedError(
                url="http://169.254.169.254", host="169.254.169.254", network="169.254.0.0/16"
            )
        ),
    )
    request = ScrapeRequest(urls=["http://169.254.169.254"])

    with pytest.raises(HTTPException) as ei:
        await scrape(request, x_api_key="sk-admin")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_scrape_reads_tenant_daily_limit_when_row_present(wired_scrape_deps):
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    pg.fetchrow.return_value = {"quota_daily_limit": 5000}
    request = ScrapeRequest(urls=["http://example.com"])

    resp = await scrape(request, x_api_key="sk-admin")

    assert resp["status"] == "PENDING"


@pytest.mark.asyncio
async def test_scrape_quota_exceeded_returns_429(wired_scrape_deps, monkeypatch):
    from scraper_engine.core.exceptions import QuotaExceededError
    from scraper_engine.core.models import ScrapeRequest

    monkeypatch.setattr(
        "scraper_engine.core.quota.QuotaManager.check_and_increment",
        AsyncMock(side_effect=QuotaExceededError(tenant_id="system", limit=10_000)),
    )
    request = ScrapeRequest(urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await scrape(request, x_api_key="sk-admin")
    assert ei.value.status_code == 429


@pytest.mark.asyncio
async def test_crawl_uninitialized_tenant_resolver_returns_503(monkeypatch):
    from scraper_engine.core.models import CrawlRequest

    monkeypatch.setattr(deps, "_tenant_resolver", None)
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await crawl(request, x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_crawl_authentication_error_returns_401(monkeypatch):
    from scraper_engine.core.exceptions import AuthenticationError
    from scraper_engine.core.models import CrawlRequest

    resolver = AsyncMock()
    resolver.resolve.side_effect = AuthenticationError()
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await crawl(request, x_api_key="sk-bad")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_crawl_uninitialized_ssrf_guard_returns_503(wired_scrape_deps, monkeypatch):
    from scraper_engine.core.models import CrawlRequest

    monkeypatch.setattr(deps, "_ssrf_guard", None)
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await crawl(request, x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_crawl_ssrf_blocked_url_returns_403(wired_scrape_deps, monkeypatch):
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import CrawlRequest

    monkeypatch.setattr(
        "scraper_engine.core.ssrf_guard.SSRFGuard.validate",
        AsyncMock(
            side_effect=SSRFBlockedError(
                url="http://169.254.169.254", host="169.254.169.254", network="169.254.0.0/16"
            )
        ),
    )
    request = CrawlRequest(spider_name="titles", start_urls=["http://169.254.169.254"])

    with pytest.raises(HTTPException) as ei:
        await crawl(request, x_api_key="sk-admin")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_crawl_reads_tenant_daily_limit_when_row_present(wired_scrape_deps):
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps
    pg.fetchrow.return_value = {"quota_daily_limit": 5000}
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    resp = await crawl(request, x_api_key="sk-admin")

    assert resp["status"] == "PENDING"


@pytest.mark.asyncio
async def test_crawl_quota_exceeded_returns_429(wired_scrape_deps, monkeypatch):
    from scraper_engine.core.exceptions import QuotaExceededError
    from scraper_engine.core.models import CrawlRequest

    monkeypatch.setattr(
        "scraper_engine.core.quota.QuotaManager.check_and_increment",
        AsyncMock(side_effect=QuotaExceededError(tenant_id="system", limit=10_000)),
    )
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await crawl(request, x_api_key="sk-admin")
    assert ei.value.status_code == 429


# ---------------------------------------------------------------------------
# health() route — composite health check wired into GET /v1/health. Distinct
# from tests/unit/test_health.py, which covers HealthChecker/check_health
# directly; these cover the route wrapper (dep guard, status->HTTP mapping).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_route_uninitialized_deps_returns_503(monkeypatch):
    monkeypatch.setattr(deps, "_storage_pg", None)
    monkeypatch.setattr(deps, "_storage_redis", AsyncMock())

    with pytest.raises(HTTPException) as ei:
        await health()
    assert ei.value.status_code == 503
    assert ei.value.detail == "Service not initialized"


@pytest.mark.asyncio
async def test_health_route_healthy_returns_ok_payload(monkeypatch):
    from scraper_engine.api.health import HealthStatus

    monkeypatch.setattr(deps, "_storage_pg", AsyncMock())
    monkeypatch.setattr(deps, "_storage_redis", AsyncMock())
    monkeypatch.setattr(
        "scraper_engine.api.health.check_health",
        AsyncMock(
            return_value=HealthStatus(
                healthy=True,
                proxy_pool_size=7,
                pgbouncer_reachable=True,
                redis_reachable=True,
                s3_reachable=True,
            )
        ),
    )

    payload = await health()

    assert payload["status"] == "ok"
    assert payload["proxy_pool_size"] == 7


@pytest.mark.asyncio
async def test_health_route_unhealthy_returns_503_with_degraded_payload(monkeypatch):
    from scraper_engine.api.health import HealthStatus

    monkeypatch.setattr(deps, "_storage_pg", AsyncMock())
    monkeypatch.setattr(deps, "_storage_redis", AsyncMock())
    monkeypatch.setattr(
        "scraper_engine.api.health.check_health",
        AsyncMock(
            return_value=HealthStatus(
                healthy=False,
                pgbouncer_reachable=False,
                checks={"pgbouncer": "connection refused"},
            )
        ),
    )

    with pytest.raises(HTTPException) as ei:
        await health()
    assert ei.value.status_code == 503
    assert ei.value.detail["status"] == "degraded"
    assert ei.value.detail["checks"] == {"pgbouncer": "connection refused"}


# ---------------------------------------------------------------------------
# GET /metrics — gauge refresh from Postgres/Redis-backed cross-process
# counters (register_routes only wires this path when pg/redis deps are
# actually set; test_metrics_gate.py covers the on/off route-mounting switch
# with both deps left at their None default, which never reaches this code).
# ---------------------------------------------------------------------------


def _metrics_app() -> FastAPI:
    app = FastAPI()
    register_routes(app, AppConfig())
    return app


def test_metrics_endpoint_refreshes_gauges_when_pg_and_redis_configured(monkeypatch):
    monkeypatch.setattr(deps, "_storage_pg", MagicMock())
    monkeypatch.setattr(deps, "_storage_redis", MagicMock())
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.count_validated_proxies",
        AsyncMock(return_value=3),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_dlq_size", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_capsolver_spend",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_redis_backed_counters",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_proxy_source_health",
        AsyncMock(return_value=None),
    )

    client = TestClient(_metrics_app())
    resp = client.get("/metrics")

    assert resp.status_code == 200


def test_metrics_endpoint_survives_gauge_refresh_failures(monkeypatch):
    """Each gauge refresh is independently try/except-wrapped so one failing
    source (e.g. a tenant schema down) doesn't 500 the whole /metrics scrape."""
    monkeypatch.setattr(deps, "_storage_pg", MagicMock())
    monkeypatch.setattr(deps, "_storage_redis", MagicMock())
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.count_validated_proxies",
        AsyncMock(side_effect=Exception("db down")),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_dlq_size",
        AsyncMock(side_effect=Exception("db down")),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_capsolver_spend",
        AsyncMock(side_effect=Exception("db down")),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_redis_backed_counters",
        AsyncMock(side_effect=Exception("redis down")),
    )
    monkeypatch.setattr(
        "scraper_engine.observability.metrics.refresh_proxy_source_health",
        AsyncMock(side_effect=Exception("redis down")),
    )

    client = TestClient(_metrics_app())
    resp = client.get("/metrics")

    assert resp.status_code == 200
