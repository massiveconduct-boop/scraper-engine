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

import scraper_engine.api.dependencies as deps
from scraper_engine.api.routes import crawl, get_job, scrape
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
