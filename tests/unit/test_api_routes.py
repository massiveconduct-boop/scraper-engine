# tests/unit/test_api_routes.py
"""API route regressions.

get_job UUID coercion: `scrape_jobs.job_id` is a Postgres UUID; asyncpg returns
it as a `uuid.UUID`, but `JobStatusResponse.job_id` is typed `str`. The route
must `str()` it or Pydantic raises and the endpoint 500s on every existing job
(round 16 — caught by the full-stack e2e smoke).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import scraper_engine.api.dependencies as deps
from scraper_engine.api.routes import (
    cancel_job,
    crawl,
    get_job,
    get_job_dlq,
    get_quota,
    health,
    list_dlq,
    list_jobs,
    list_webhook_events,
    register_routes,
    scrape,
)
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


def _job_row(job_id, status, urls, *, created_at=None, started_at=None, finished_at=None):
    """A scrape_jobs row as get_job selects it.

    Round 63 added created_at/started_at/finished_at to that SELECT so the
    response can report queued_ms/runtime_ms. Defaulting the phase timestamps
    to None here keeps each test naming only what it is actually about, and
    matches a real row for a job that has not started yet.
    """
    return {
        "job_id": job_id,
        "status": status,
        "urls": urls,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }


@pytest.mark.asyncio
async def test_get_job_coerces_uuid_job_id_to_str(wired_deps):
    jid = uuid.uuid4()
    # fetch() calls, in order: the scrape_jobs lookup, the completed-count
    # (round 63 — its own query so `progress` still counts the whole job when
    # the caller pages with ?since=), then the scrape_results rows.
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "PENDING", ["https://example.com"])],
        [{"n": 0}],
        [],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    # the bug: passing the raw UUID would raise pydantic ValidationError (500).
    assert resp.job_id == str(jid)
    assert isinstance(resp.job_id, str)
    assert resp.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_get_job_surfaces_network_events_from_db_row(wired_deps):
    """Round 60 — network_events must round-trip through the DB-polling path
    (GET /v1/jobs/{id}), not just the in-memory webhook payload path. Both
    a populated and a null network_events column are exercised."""
    jid = uuid.uuid4()
    now = datetime.now(UTC)
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "COMPLETED", ["https://a.example", "https://b.example"])],
        [{"n": 2}],
        [
            {
                "url": "https://a.example",
                "success": True,
                "http_status": 200,
                "is_challenge_page": False,
                "level_used": 2,
                "proxy_used": "1.2.3.4:8080",
                "markdown": None,
                "json_data": None,
                "network_events": '[{"type": "request", "url": "https://a.example"}]',
                "html_snapshot_url": None,
                "time_taken_ms": 100,
                "error_message": None,
                "failure_category": None,
                "extracted_at": now,
                "proxy_source": None,
                "timings": None,
            },
            {
                "url": "https://b.example",
                "success": True,
                "http_status": 200,
                "is_challenge_page": False,
                "level_used": 1,
                "proxy_used": None,
                "markdown": None,
                "json_data": None,
                "network_events": None,
                "html_snapshot_url": None,
                "time_taken_ms": 50,
                "error_message": None,
                "failure_category": None,
                "extracted_at": now,
                "proxy_source": None,
                "timings": None,
            },
        ],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    assert resp.results is not None
    assert resp.results[0].network_events == [{"type": "request", "url": "https://a.example"}]
    assert resp.results[1].network_events is None


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
    # Round 42 — small jobs keep the historical 600s floor.
    assert call_args.kwargs["job_timeout"] == 600


@pytest.mark.asyncio
async def test_scrape_job_timeout_scales_with_url_count(wired_scrape_deps):
    """Round 42 — live-caught: a real 51-URL job hit RQ's flat 600s job
    timeout mid-run (~29s/URL observed against real free-pool proxies) and
    was hard-killed, leaving scrape_jobs.status stuck at PROCESSING forever
    since the kill bypasses the app's own cleanup code. job_timeout must
    scale with how many URLs will actually be attempted."""
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    request = ScrapeRequest(urls=[f"http://example.com/{i}" for i in range(20)])

    await scrape(request, x_api_key="sk-admin")

    call_args = queue.enqueue.call_args
    assert call_args.kwargs["job_timeout"] == 20 * 180


@pytest.mark.asyncio
async def test_scrape_enqueue_failure_marks_job_failed_not_orphaned_pending(wired_scrape_deps):
    """Round 54 — live-caught: 17 real research_agent jobs stuck at PENDING
    for days, none with a matching rq:job:* Redis key, because the INSERT
    above and .enqueue() below are two separate operations — a transient
    Redis error here used to leave the row committed as PENDING with
    nothing ever actually queued, invisible to both the caller (who just
    saw a 500) and stuck_job_reaper (which only looked at PROCESSING).
    Must now mark the row FAILED and tell the caller plainly."""
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    queue.enqueue.side_effect = RuntimeError("redis connection reset")
    request = ScrapeRequest(urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await scrape(request, x_api_key="sk-admin")

    assert ei.value.status_code == 503
    update_call = next(
        c
        for c in pg.execute.await_args_list
        if "UPDATE scrape_jobs SET status" in c.args[1]
    )
    assert update_call.args[2] == JobStatus.FAILED.value


@pytest.mark.asyncio
async def test_crawl_enqueue_failure_marks_job_failed_not_orphaned_pending(wired_scrape_deps):
    """Same fix as the /v1/scrape case above, applied to /v1/crawl's own
    separate enqueue call site."""
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps
    queue.enqueue.side_effect = RuntimeError("redis connection reset")
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    with pytest.raises(HTTPException) as ei:
        await crawl(request, x_api_key="sk-admin")

    assert ei.value.status_code == 503
    update_call = next(
        c
        for c in pg.execute.await_args_list
        if "UPDATE scrape_jobs SET status" in c.args[1]
    )
    assert update_call.args[2] == JobStatus.FAILED.value


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


@pytest.mark.asyncio
async def test_scrape_idempotency_key_hit_returns_existing_job_without_new_work(
    wired_scrape_deps,
):
    """A retry with the same Idempotency-Key while the original job is still
    live (round 29) must hand back the original job — no new quota charge,
    no new INSERT, no new enqueue."""
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    existing_job_id = uuid.uuid4()
    pg.fetchrow.return_value = {"job_id": existing_job_id, "status": "PROCESSING"}
    request = ScrapeRequest(urls=["http://example.com"])

    resp = await scrape(request, x_api_key="sk-admin", idempotency_key="retry-key-1")

    assert resp == {
        "job_id": str(existing_job_id),
        "status": "PROCESSING",
        "urls": 1,
        "tenant": "system",
    }
    queue.enqueue.assert_not_called()
    insert_calls = [c for c in pg.execute.await_args_list if "INSERT INTO scrape_jobs" in c.args[1]]
    assert len(insert_calls) == 0


@pytest.mark.asyncio
async def test_crawl_idempotency_key_hit_returns_existing_job_without_new_work(
    wired_scrape_deps,
):
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps
    existing_job_id = uuid.uuid4()
    pg.fetchrow.return_value = {"job_id": existing_job_id, "status": "PENDING"}
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    resp = await crawl(request, x_api_key="sk-admin", idempotency_key="retry-key-2")

    assert resp == {
        "job_id": str(existing_job_id),
        "status": "PENDING",
        "start_urls": 1,
        "tenant": "system",
    }
    queue.enqueue.assert_not_called()


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
async def test_scrape_partial_ssrf_block_proceeds_with_valid_urls(wired_scrape_deps):
    """1 bad address in a batch must not 403 the whole request (round 33) —
    only the valid URLs get quota-charged and the job still enqueues. The
    blocked URL is still passed through to the job (not dropped) because
    the L1->L2->L3 escalation pipeline re-validates every URL itself and
    already turns a blocked one into a per-URL failure result without
    crashing (see tests/unit/test_ssrf_guard.py + fetcher/_failure.py) —
    that's what gives the caller real per-URL visibility instead of routes.py
    silently swallowing it."""
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps

    async def _validate(url: str) -> None:
        if "169.254" in url:
            raise SSRFBlockedError(url=url, host="169.254.169.254", network="169.254.0.0/16")

    import scraper_engine.core.ssrf_guard as ssrf_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ssrf_module.SSRFGuard, "validate", AsyncMock(side_effect=_validate))

        request = ScrapeRequest(urls=["http://example.com", "http://169.254.169.254"])
        resp = await scrape(request, x_api_key="sk-admin")

    assert resp["urls"] == 2
    assert resp["blocked_urls"] == 1
    queue.enqueue.assert_called_once()

    # Both URLs are still persisted onto the job row — the blocked one gets
    # its own real failure result once the pipeline itself rejects it.
    insert_call = next(
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_jobs" in c.args[1]
    )
    assert insert_call.args[3] == ["http://example.com/", "http://169.254.169.254/"]


@pytest.mark.asyncio
async def test_scrape_reads_tenant_daily_limit_when_row_present(wired_scrape_deps):
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    pg.fetchrow.return_value = {"quota_daily_limit": 5000}
    request = ScrapeRequest(urls=["http://example.com"])

    # idempotency_key explicit: this test's pg.fetchrow mock always returns
    # a quota-limit-shaped dict, so leaving idempotency_key at its raw
    # FastAPI Header() marker default (truthy when the route is called
    # directly, bypassing DI) would make the dedup lookup misread that same
    # mock as a "duplicate job found" row.
    resp = await scrape(request, x_api_key="sk-admin", idempotency_key=None)

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
async def test_scrape_webhook_pointed_at_private_address_returns_403(wired_scrape_deps):
    """round 34 — the webhook URL is a POST target the worker reaches out to
    unattended, same class of SSRF risk as a scrape target, but nothing
    validated it before this. Target URL is fine; only the webhook is bad —
    must still 403 the whole request (unlike target-URL blocking, there's
    only one webhook, so partial-partition doesn't apply)."""
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import ScrapeRequest

    async def _validate(url: str) -> None:
        if "169.254" in url:
            raise SSRFBlockedError(url=url, host="169.254.169.254", network="169.254.0.0/16")

    import scraper_engine.core.ssrf_guard as ssrf_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ssrf_module.SSRFGuard, "validate", AsyncMock(side_effect=_validate))
        request = ScrapeRequest(urls=["http://example.com"], webhook="http://169.254.169.254/steal")

        with pytest.raises(HTTPException) as ei:
            await scrape(request, x_api_key="sk-admin")
        assert ei.value.status_code == 403
        assert "webhook" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_scrape_webhook_pointed_at_public_url_is_allowed(wired_scrape_deps):
    """Companion to the block test above — a normal (e.g. Slack) webhook URL
    must not be rejected by the new guard."""
    from scraper_engine.core.models import ScrapeRequest

    pg, redis, queue = wired_scrape_deps
    request = ScrapeRequest(
        urls=["http://example.com"], webhook="https://hooks.slack.com/services/T00/B00/XXX"
    )

    resp = await scrape(request, x_api_key="sk-admin")

    queue.enqueue.assert_called_once()
    assert resp["job_id"]


@pytest.mark.asyncio
async def test_crawl_webhook_pointed_at_private_address_returns_403(wired_scrape_deps):
    """Same guard, same rationale, on the /v1/crawl path (round 34)."""
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import CrawlRequest

    async def _validate(url: str) -> None:
        if "169.254" in url:
            raise SSRFBlockedError(url=url, host="169.254.169.254", network="169.254.0.0/16")

    import scraper_engine.core.ssrf_guard as ssrf_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ssrf_module.SSRFGuard, "validate", AsyncMock(side_effect=_validate))
        request = CrawlRequest(
            spider_name="titles",
            start_urls=["http://example.com"],
            webhook="http://169.254.169.254/steal",
        )

        with pytest.raises(HTTPException) as ei:
            await crawl(request, x_api_key="sk-admin")
        assert ei.value.status_code == 403
        assert "webhook" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_crawl_partial_ssrf_block_drops_blocked_seed_only(wired_scrape_deps):
    """Same round-33 fix as scrape, but ScrapyAdapter has no per-URL SSRF
    re-check of its own (unlike the L1->L2->L3 pipeline), so the blocked
    seed must actually be filtered out of start_urls rather than passed
    through — and, since it would otherwise vanish with zero trace, a
    synthetic failed scrape_results row is persisted for it directly."""
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps

    async def _validate(url: str) -> None:
        if "169.254" in url:
            raise SSRFBlockedError(url=url, host="169.254.169.254", network="169.254.0.0/16")

    import scraper_engine.core.ssrf_guard as ssrf_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ssrf_module.SSRFGuard, "validate", AsyncMock(side_effect=_validate))

        request = CrawlRequest(
            spider_name="titles",
            start_urls=["http://example.com", "http://169.254.169.254"],
        )
        resp = await crawl(request, x_api_key="sk-admin")

    assert resp["start_urls"] == 1
    assert resp["blocked_urls"] == 1
    queue.enqueue.assert_called_once()

    insert_job_call = next(
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_jobs" in c.args[1]
    )
    assert insert_job_call.args[3] == ["http://example.com/"]
    config_used = insert_job_call.args[4]
    assert "169.254" not in config_used

    insert_result_call = next(
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_results" in c.args[1]
    )
    assert insert_result_call.args[3] == "http://169.254.169.254/"
    assert insert_result_call.args[5] == "ssrf_blocked"


@pytest.mark.asyncio
async def test_crawl_unresolvable_seed_persists_as_host_unreachable(wired_scrape_deps):
    """Live-caught: SSRFGuard raises SSRFBlockedError for a dead domain too
    (network="<unresolvable>"), not just a real block. The persisted
    scrape_results row for a blocked seed must follow that distinction
    (HOST_UNREACHABLE) instead of hardcoding ssrf_blocked for every
    SSRFBlockedError, which would mislabel a dead domain as a security
    event."""
    from scraper_engine.core.exceptions import SSRFBlockedError
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps

    async def _validate(url: str) -> None:
        if "dead-domain" in url:
            raise SSRFBlockedError(
                url=url, host="dead-domain.example", network="<unresolvable>"
            )

    import scraper_engine.core.ssrf_guard as ssrf_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ssrf_module.SSRFGuard, "validate", AsyncMock(side_effect=_validate))

        request = CrawlRequest(
            spider_name="titles",
            start_urls=["http://example.com", "http://dead-domain.example"],
        )
        resp = await crawl(request, x_api_key="sk-admin")

    assert resp["blocked_urls"] == 1

    insert_result_call = next(
        c for c in pg.execute.await_args_list if "INSERT INTO scrape_results" in c.args[1]
    )
    assert insert_result_call.args[3] == "http://dead-domain.example/"
    assert insert_result_call.args[5] == "host_unreachable"


@pytest.mark.asyncio
async def test_crawl_reads_tenant_daily_limit_when_row_present(wired_scrape_deps):
    from scraper_engine.core.models import CrawlRequest

    pg, redis, queue = wired_scrape_deps
    pg.fetchrow.return_value = {"quota_daily_limit": 5000}
    request = CrawlRequest(spider_name="titles", start_urls=["http://example.com"])

    # idempotency_key explicit — same reason as the scrape() equivalent above.
    resp = await crawl(request, x_api_key="sk-admin", idempotency_key=None)

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
                daemons={
                    "proxy-harvester": "healthy",
                    "dlq-reaper": "healthy",
                    "webhook-sweeper": "healthy",
                },
            )
        ),
    )

    payload = await health()

    assert payload["status"] == "ok"
    assert payload["proxy_pool_size"] == 7
    assert payload["daemons"] == {
        "proxy-harvester": "healthy",
        "dlq-reaper": "healthy",
        "webhook-sweeper": "healthy",
    }


@pytest.mark.asyncio
async def test_health_route_includes_browser_capacity_when_present(monkeypatch):
    """Round 65 — the block rides along only when host admission reported one."""
    from scraper_engine.api.health import HealthStatus

    monkeypatch.setattr(deps, "_storage_pg", AsyncMock())
    monkeypatch.setattr(deps, "_storage_redis", AsyncMock())
    block = {"status": "ok", "in_use_units": 1.0, "target_units": 4.0, "waiters": 0}
    monkeypatch.setattr(
        "scraper_engine.api.health.check_health",
        AsyncMock(return_value=HealthStatus(healthy=True, browser_capacity=block)),
    )
    payload = await health()
    assert payload["browser_capacity"] == block


@pytest.mark.asyncio
async def test_health_route_includes_paid_gateway_when_present(monkeypatch):
    """Round 68 — whether the gateway is refusing our credentials."""
    from scraper_engine.api.health import HealthStatus

    monkeypatch.setattr(deps, "_storage_pg", AsyncMock())
    monkeypatch.setattr(deps, "_storage_redis", AsyncMock())
    block = {"status": "refused", "strategy": "free_first", "since": "t", "error": "407"}
    monkeypatch.setattr(
        "scraper_engine.api.health.check_health",
        AsyncMock(return_value=HealthStatus(healthy=True, paid_gateway=block)),
    )
    payload = await health()
    assert payload["paid_gateway"] == block


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
                daemons={"proxy-harvester": "stale (harvest)"},
                checks={
                    "pgbouncer": "connection refused",
                    "daemons": "proxy-harvester: stale (harvest)",
                },
            )
        ),
    )

    with pytest.raises(HTTPException) as ei:
        await health()
    assert ei.value.status_code == 503
    assert ei.value.detail["status"] == "degraded"
    assert ei.value.detail["daemons"] == {"proxy-harvester": "stale (harvest)"}
    assert ei.value.detail["checks"]["daemons"] == "proxy-harvester: stale (harvest)"


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


# ── GET /v1/jobs/{job_id}/dlq (round 29) ──────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_dlq_returns_entries(wired_deps, monkeypatch):
    from datetime import UTC, datetime

    from scraper_engine.core.models import FailureCategory
    from scraper_engine.storage.dlq import DeadLetterEntry

    jid = str(uuid.uuid4())
    entry = DeadLetterEntry(
        id=1,
        job_id=jid,
        tenant_id="system",
        url="http://example.com",
        failure_category=FailureCategory.PROXY_EXHAUSTED,
        error_message="All fetch levels exhausted",
        level_attempted=3,
        auto_retry_count=0,
        enqueued_at=datetime.now(UTC),
        dead_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "scraper_engine.storage.dlq.DeadLetterQueue.list_for_tenant",
        AsyncMock(return_value=[entry]),
    )

    result = await get_job_dlq(jid, x_api_key="sk-admin")

    assert len(result) == 1
    assert result[0].job_id == jid
    assert result[0].url == "http://example.com"
    assert result[0].failure_category == FailureCategory.PROXY_EXHAUSTED


@pytest.mark.asyncio
async def test_get_job_dlq_returns_empty_list_when_pg_not_initialized(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    monkeypatch.setattr(deps, "_storage_pg", None)

    result = await get_job_dlq(str(uuid.uuid4()), x_api_key="sk-admin")

    assert result == []


@pytest.mark.asyncio
async def test_get_job_dlq_service_not_initialized_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await get_job_dlq(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_get_job_dlq_invalid_api_key_401(wired_deps):
    from scraper_engine.core.exceptions import AuthenticationError

    deps._tenant_resolver.resolve.side_effect = AuthenticationError("bad key")

    with pytest.raises(HTTPException) as ei:
        await get_job_dlq(str(uuid.uuid4()), x_api_key="sk-bad")
    assert ei.value.status_code == 401


# ── GET /v1/jobs (round 56) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_paginated_summaries(wired_deps):
    from datetime import UTC, datetime

    jid = uuid.uuid4()
    now = datetime.now(UTC)
    wired_deps.fetch.return_value = [
        {
            "job_id": jid,
            "status": "COMPLETED",
            "urls": ["https://example.com", "https://example.org"],
            "created_at": now,
            "updated_at": now,
        }
    ]

    result = await list_jobs(x_api_key="sk-admin")

    assert result["count"] == 1
    assert result["limit"] == 50
    assert result["offset"] == 0
    job = result["jobs"][0]
    assert job.job_id == str(jid)
    assert job.status == JobStatus.COMPLETED
    assert job.url_count == 2


@pytest.mark.asyncio
async def test_list_jobs_passes_status_limit_offset_to_query(wired_deps):
    wired_deps.fetch.return_value = []

    await list_jobs(x_api_key="sk-admin", status="FAILED", limit=10, offset=20)

    args = wired_deps.fetch.call_args.args
    # args: (tenant_id, query, status, limit, offset)
    assert args[2] == "FAILED"
    assert args[3] == 10
    assert args[4] == 20


@pytest.mark.asyncio
async def test_list_jobs_invalid_status_422():
    with pytest.raises(HTTPException) as ei:
        await list_jobs(x_api_key="sk-admin", status="NOT_A_STATUS")
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_list_jobs_limit_out_of_range_422():
    with pytest.raises(HTTPException) as ei:
        await list_jobs(x_api_key="sk-admin", limit=501)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_list_jobs_negative_offset_422():
    with pytest.raises(HTTPException) as ei:
        await list_jobs(x_api_key="sk-admin", offset=-1)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_list_jobs_returns_empty_when_pg_not_initialized(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    monkeypatch.setattr(deps, "_storage_pg", None)

    result = await list_jobs(x_api_key="sk-admin")

    assert result == {"jobs": [], "limit": 50, "offset": 0, "count": 0}


@pytest.mark.asyncio
async def test_list_jobs_service_not_initialized_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await list_jobs(x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_list_jobs_invalid_api_key_401(wired_deps):
    from scraper_engine.core.exceptions import AuthenticationError

    deps._tenant_resolver.resolve.side_effect = AuthenticationError("bad key")

    with pytest.raises(HTTPException) as ei:
        await list_jobs(x_api_key="sk-bad")
    assert ei.value.status_code == 401


# ── GET /v1/dlq (round 56) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_dlq_returns_tenant_wide_entries(wired_deps, monkeypatch):
    from datetime import UTC, datetime

    from scraper_engine.core.models import FailureCategory
    from scraper_engine.storage.dlq import DeadLetterEntry

    jid = str(uuid.uuid4())
    entry = DeadLetterEntry(
        id=1,
        job_id=jid,
        tenant_id="system",
        url="http://example.com",
        failure_category=FailureCategory.PROXY_EXHAUSTED,
        error_message="All fetch levels exhausted",
        level_attempted=3,
        auto_retry_count=0,
        enqueued_at=datetime.now(UTC),
        dead_at=datetime.now(UTC),
    )
    list_mock = AsyncMock(return_value=[entry])
    monkeypatch.setattr("scraper_engine.storage.dlq.DeadLetterQueue.list_for_tenant", list_mock)

    result = await list_dlq(x_api_key="sk-admin")

    assert len(result) == 1
    assert result[0].job_id == jid
    # job_id kwarg must stay unset (tenant-wide mode), not scoped to one job.
    assert list_mock.call_args.kwargs.get("job_id") is None


@pytest.mark.asyncio
async def test_list_dlq_passes_limit_offset(wired_deps, monkeypatch):
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("scraper_engine.storage.dlq.DeadLetterQueue.list_for_tenant", list_mock)

    await list_dlq(x_api_key="sk-admin", limit=10, offset=5)

    assert list_mock.call_args.kwargs["limit"] == 10
    assert list_mock.call_args.kwargs["offset"] == 5


@pytest.mark.asyncio
async def test_list_dlq_limit_out_of_range_422():
    with pytest.raises(HTTPException) as ei:
        await list_dlq(x_api_key="sk-admin", limit=0)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_list_dlq_returns_empty_list_when_pg_not_initialized(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    monkeypatch.setattr(deps, "_storage_pg", None)

    result = await list_dlq(x_api_key="sk-admin")

    assert result == []


@pytest.mark.asyncio
async def test_list_dlq_service_not_initialized_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await list_dlq(x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_list_dlq_invalid_api_key_401(wired_deps):
    from scraper_engine.core.exceptions import AuthenticationError

    deps._tenant_resolver.resolve.side_effect = AuthenticationError("bad key")

    with pytest.raises(HTTPException) as ei:
        await list_dlq(x_api_key="sk-bad")
    assert ei.value.status_code == 401


# ── GET /v1/quota (round 56) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_quota_returns_remaining_and_limit(wired_scrape_deps):
    pg, redis, _queue = wired_scrape_deps
    pg.fetchrow.return_value = {"quota_daily_limit": 5000}
    redis.get.return_value = "42"

    result = await get_quota(x_api_key="sk-admin")

    assert result["tenant"] == "system"
    assert result["daily_limit"] == 5000
    assert result["used"] == 42
    assert result["remaining"] == 5000 - 42
    assert result["resets_in_seconds"] > 0


@pytest.mark.asyncio
async def test_get_quota_falls_back_to_default_limit_when_tenant_row_missing(
    wired_scrape_deps,
):
    from scraper_engine.core.quota import QuotaManager

    pg, redis, _queue = wired_scrape_deps
    pg.fetchrow.return_value = None
    redis.get.return_value = None

    result = await get_quota(x_api_key="sk-admin")

    assert result["daily_limit"] == QuotaManager.DEFAULT_DAILY_LIMIT
    assert result["used"] == 0
    assert result["remaining"] == QuotaManager.DEFAULT_DAILY_LIMIT


@pytest.mark.asyncio
async def test_get_quota_storage_not_initialized_503(wired_deps, monkeypatch):
    monkeypatch.setattr(deps, "_storage_redis", None)

    with pytest.raises(HTTPException) as ei:
        await get_quota(x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_get_quota_service_not_initialized_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await get_quota(x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_get_quota_invalid_api_key_401(wired_scrape_deps):
    from scraper_engine.core.exceptions import AuthenticationError

    deps._tenant_resolver.resolve.side_effect = AuthenticationError("bad key")

    with pytest.raises(HTTPException) as ei:
        await get_quota(x_api_key="sk-bad")
    assert ei.value.status_code == 401


# ── GET /v1/webhook-events (round 56) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_list_webhook_events_returns_types_and_schema(wired_deps):
    result = await list_webhook_events(x_api_key="sk-admin")

    assert "job.completed" in result["event_types"]
    assert "job.partial_failure" in result["event_types"]
    assert "proxy_pool.degraded" in result["event_types"]
    schema = result["payload_schema"]
    assert "event_type" in schema["properties"]
    assert "payload" in schema["properties"]


@pytest.mark.asyncio
async def test_list_webhook_events_service_not_initialized_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await list_webhook_events(x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_list_webhook_events_invalid_api_key_401(wired_deps):
    from scraper_engine.core.exceptions import AuthenticationError

    deps._tenant_resolver.resolve.side_effect = AuthenticationError("bad key")

    with pytest.raises(HTTPException) as ei:
        await list_webhook_events(x_api_key="sk-bad")
    assert ei.value.status_code == 401


# ── DELETE /v1/jobs/{job_id} (round 29) ───────────────────────────────────


@pytest.fixture
def wired_cancel_deps(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)

    pg = AsyncMock()
    monkeypatch.setattr(deps, "_storage_pg", pg)

    queue = MagicMock()
    monkeypatch.setattr(deps, "_queue", queue)

    return pg, queue


@pytest.mark.asyncio
async def test_cancel_job_success_cancels_queued_rq_job(wired_cancel_deps):
    pg, queue = wired_cancel_deps
    pg.fetchrow.return_value = {"status": "CANCELLED"}
    rq_job = MagicMock()
    queue.fetch_job.return_value = rq_job

    jid = str(uuid.uuid4())
    resp = await cancel_job(jid, x_api_key="sk-admin")

    assert resp == {"job_id": jid, "status": "CANCELLED"}
    queue.fetch_job.assert_called_once_with(jid)
    rq_job.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_job_success_when_rq_job_already_gone(wired_cancel_deps):
    """The job already finished dequeuing and rq no longer has a handle for
    it — cancellation still succeeds via the cooperative in-loop check."""
    pg, queue = wired_cancel_deps
    pg.fetchrow.return_value = {"status": "CANCELLED"}
    queue.fetch_job.return_value = None

    resp = await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")

    assert resp["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_job_rq_cancel_failure_is_logged_not_raised(wired_cancel_deps):
    pg, queue = wired_cancel_deps
    pg.fetchrow.return_value = {"status": "CANCELLED"}
    rq_job = MagicMock()
    rq_job.cancel.side_effect = RuntimeError("redis unavailable")
    queue.fetch_job.return_value = rq_job

    resp = await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")  # must not raise

    assert resp["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_job_not_found_404(wired_cancel_deps):
    pg, queue = wired_cancel_deps
    pg.fetchrow.side_effect = [None, None]  # UPDATE...RETURNING finds nothing, then existence check

    with pytest.raises(HTTPException) as ei:
        await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_job_already_terminal_409(wired_cancel_deps):
    pg, queue = wired_cancel_deps
    # UPDATE...RETURNING finds nothing (already terminal), existence check finds the row
    pg.fetchrow.side_effect = [None, {"?column?": 1}]

    with pytest.raises(HTTPException) as ei:
        await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_job_tenant_resolver_not_initialized_503(monkeypatch):
    monkeypatch.setattr(deps, "_tenant_resolver", None)

    with pytest.raises(HTTPException) as ei:
        await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_cancel_job_storage_not_initialized_503(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "system"
    monkeypatch.setattr(deps, "_tenant_resolver", resolver)
    monkeypatch.setattr(deps, "_storage_pg", None)

    with pytest.raises(HTTPException) as ei:
        await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_cancel_job_invalid_api_key_401(wired_cancel_deps):
    from scraper_engine.core.exceptions import AuthenticationError

    deps._tenant_resolver.resolve.side_effect = AuthenticationError("bad key")

    with pytest.raises(HTTPException) as ei:
        await cancel_job(str(uuid.uuid4()), x_api_key="sk-bad")
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# Round 63 — the polling contract. Partial results of a still-PROCESSING job
# were always readable (rows land one per URL as they complete), but every
# poll re-sent the whole set, so a 95-URL job re-transferred everything it had
# already delivered on each check. `since` is the cursor that was missing.
# ---------------------------------------------------------------------------


def _result_row(url, now, **over):
    row = {
        "url": url,
        "success": True,
        "http_status": 200,
        "proxy_source": "pool",
        "is_challenge_page": False,
        "level_used": 3,
        "proxy_used": "1.2.3.4:8080",
        "markdown": None,
        "json_data": None,
        "network_events": None,
        "html_snapshot_url": None,
        "time_taken_ms": 27600,
        "error_message": None,
        "failure_category": None,
        "extracted_at": now,
        "timings": None,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_get_job_since_is_passed_to_the_query(wired_deps):
    jid = uuid.uuid4()
    now = datetime.now(UTC)
    cursor = now - timedelta(minutes=5)
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "PROCESSING", ["https://a.example", "https://b.example"])],
        [{"n": 2}],
        [_result_row("https://b.example", now)],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin", since=cursor)

    assert resp.results is not None
    assert len(resp.results) == 1
    assert cursor in wired_deps.fetch.await_args.args


@pytest.mark.asyncio
async def test_get_job_progress_counts_the_whole_job_not_the_since_window(wired_deps):
    """The cursor narrows the payload, never the progress figure — a caller
    paging through a job must not see progress fall back as it advances."""
    jid = uuid.uuid4()
    now = datetime.now(UTC)
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "PROCESSING", [f"https://{i}.example" for i in range(10)])],
        [{"n": 8}],
        [_result_row("https://7.example", now)],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin", since=now - timedelta(seconds=30))

    assert resp.progress == 0.8
    assert resp.results is not None and len(resp.results) == 1


@pytest.mark.asyncio
async def test_get_job_surfaces_timings_and_proxy_source(wired_deps):
    """Both columns were persisted and then dropped on the way back out, so
    a caller could not see where a URL's time went or which proxy path served
    it (round 63)."""
    jid = uuid.uuid4()
    now = datetime.now(UTC)
    breakdown = {"level_1_ms": 20000, "level_2_ms": 120000, "level_3_ms": 27600}
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "COMPLETED", ["https://a.example"])],
        [{"n": 1}],
        [
            _result_row(
                "https://a.example", now, timings=json.dumps(breakdown), proxy_source="paid_gateway"
            )
        ],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    assert resp.results is not None
    assert resp.results[0].timings == breakdown
    assert resp.results[0].proxy_source == "paid_gateway"


@pytest.mark.asyncio
async def test_get_job_splits_escalations_out_of_the_timings_column(wired_deps):
    """Round 64 — escalations are stored inside the timings JSONB (see
    orchestrator/tasks.py::_timings_column) and must come back as their own
    field, leaving `timings` integer-only."""
    jid = uuid.uuid4()
    now = datetime.now(UTC)
    stored = {
        "level_2_ms": 40000,
        "escalations": [
            {"level": 2, "reason": "status:403", "http_status": 403, "engine": "camoufox"}
        ],
    }
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "COMPLETED", ["https://a.example"])],
        [{"n": 1}],
        [_result_row("https://a.example", now, timings=json.dumps(stored))],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    assert resp.results[0].timings == {"level_2_ms": 40000}
    assert resp.results[0].escalations == stored["escalations"]


@pytest.mark.asyncio
async def test_get_job_reports_queue_wait_and_runtime(wired_deps):
    jid = uuid.uuid4()
    created = datetime.now(UTC)
    wired_deps.fetch.side_effect = [
        [
            _job_row(
                jid,
                "COMPLETED",
                ["https://a.example"],
                created_at=created,
                started_at=created + timedelta(seconds=3),
                finished_at=created + timedelta(seconds=178),
            )
        ],
        [{"n": 1}],
        [],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    assert resp.queued_ms == 3000
    assert resp.runtime_ms == 175000


@pytest.mark.asyncio
async def test_get_job_phase_durations_are_none_before_the_transitions(wired_deps):
    jid = uuid.uuid4()
    wired_deps.fetch.side_effect = [
        [_job_row(jid, "PENDING", ["https://a.example"], created_at=datetime.now(UTC))],
        [{"n": 0}],
        [],
    ]

    resp = await get_job(str(jid), x_api_key="sk-admin")

    assert resp.queued_ms is None
    assert resp.runtime_ms is None


# --- Round 64: required dependencies + branch burn-down ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["_storage_pg", "_storage_redis", "_queue"])
@pytest.mark.parametrize("endpoint", ["scrape", "crawl"])
async def test_missing_storage_or_queue_is_503_not_a_phantom_job(
    wired_scrape_deps, monkeypatch, missing, endpoint
):
    """With pg missing these endpoints used to answer 200 with a job id that
    was never saved; with Redis missing they skipped the quota charge; with
    the queue missing they saved a PENDING row nothing would ever run."""
    from scraper_engine.core.models import CrawlRequest, ScrapeRequest

    monkeypatch.setattr(deps, missing, None)
    with pytest.raises(HTTPException) as ei:
        if endpoint == "scrape":
            await scrape(ScrapeRequest(urls=["http://example.com"]), x_api_key="sk-admin")
        else:
            await crawl(
                CrawlRequest(spider_name="t", start_urls=["http://example.com"]),
                x_api_key="sk-admin",
            )
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_cancel_job_without_a_queue_still_cancels_cooperatively(
    wired_cancel_deps, monkeypatch
):
    pg, _queue = wired_cancel_deps
    pg.fetchrow.return_value = {"status": "CANCELLED"}
    monkeypatch.setattr(deps, "_queue", None)
    resp = await cancel_job(str(uuid.uuid4()), x_api_key="sk-admin")
    assert resp["status"] == "CANCELLED"


def test_metrics_endpoint_skips_redis_gauges_without_redis(monkeypatch):
    monkeypatch.setattr(deps, "_storage_pg", MagicMock())
    monkeypatch.setattr(deps, "_storage_redis", None)
    capsolver = AsyncMock(return_value=None)
    for name, mock in [
        ("count_validated_proxies", AsyncMock(return_value=0)),
        ("refresh_dlq_size", AsyncMock(return_value=None)),
        ("refresh_capsolver_spend", capsolver),
        ("refresh_redis_backed_counters", AsyncMock(return_value=None)),
        ("refresh_proxy_source_health", AsyncMock(return_value=None)),
    ]:
        monkeypatch.setattr(f"scraper_engine.observability.metrics.{name}", mock)
    resp = TestClient(_metrics_app()).get("/metrics")
    assert resp.status_code == 200
    capsolver.assert_not_awaited()


@pytest.mark.parametrize("fails", [False, True])
def test_metrics_endpoint_refreshes_host_capacity_when_enabled(monkeypatch, fails):
    """Round 65 — host-capacity gauges refresh only with host admission on, and
    a failed refresh never 500s the scrape."""
    from scraper_engine.api import routes as routes_module
    from scraper_engine.config.schema import HostCapacityConfig

    monkeypatch.setattr(deps, "_storage_pg", None)
    monkeypatch.setattr(deps, "_storage_redis", MagicMock())
    for name in ("refresh_redis_backed_counters", "refresh_proxy_source_health"):
        monkeypatch.setattr(
            f"scraper_engine.observability.metrics.{name}", AsyncMock(return_value=None)
        )
    refresh = AsyncMock(side_effect=RuntimeError("redis down") if fails else None)
    monkeypatch.setattr("scraper_engine.observability.metrics.refresh_host_capacity", refresh)
    monkeypatch.setattr(
        routes_module, "_host_capacity_config", lambda: HostCapacityConfig(enabled=True)
    )
    resp = TestClient(_metrics_app()).get("/metrics")
    assert resp.status_code == 200
    refresh.assert_awaited_once()


def test_dataimpulse_config_is_loaded_once():
    from scraper_engine.api import routes as routes_module

    routes_module._dataimpulse_config.cache_clear()
    first = routes_module._dataimpulse_config()
    assert routes_module._dataimpulse_config() is first


def test_host_capacity_config_is_loaded_once():
    from scraper_engine.api import routes as routes_module

    routes_module._host_capacity_config.cache_clear()
    first = routes_module._host_capacity_config()
    assert routes_module._host_capacity_config() is first
