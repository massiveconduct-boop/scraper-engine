# tests/unit/test_worker.py
"""Worker state machine tests — escalation logic with mocks."""

from unittest.mock import AsyncMock

import pytest
from pydantic import HttpUrl

from scraper_engine.core.models import (
    FailureCategory,
    FetchResult,
    JobStatus,
    ScrapeRequest,
)
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.worker import Worker


@pytest.fixture
def tenant():
    return TenantId("test")


@pytest.fixture
def worker():
    redis = AsyncMock()
    cb = AsyncMock()
    cb.allow_request.return_value = True
    cb.record_success.return_value = None
    cb.record_failure.return_value = None
    pc = AsyncMock()
    pc.acquire_slot.return_value = True
    pc.release_slot.return_value = None
    pc.wait_if_needed.return_value = None
    dlq = AsyncMock()
    dlq.enqueue.return_value = None
    return Worker(redis=redis, circuit_breaker=cb, politeness=pc, dlq=dlq)


class TestWorker:
    @pytest.mark.asyncio
    async def test_process_job_all_success(self, tenant, worker):
        """All URLs succeed on L1 — no escalation needed."""
        result = FetchResult(
            url="http://example.com", success=True, level_used=1, duration_ms=10
        )
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-1", request)
        assert response.status == JobStatus.COMPLETED
        assert response.results is not None and len(response.results) == 1

    @pytest.mark.asyncio
    async def test_process_job_circuit_open(self, tenant, worker):
        """Circuit breaker open — job goes to DLQ immediately."""
        worker._circuit_breaker.allow_request.return_value = False
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-2", request)
        assert response.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_process_job_non_retryable(self, tenant, worker):
        """SSRF blocked — goes to DLQ without retry."""
        result = FetchResult(
            url="http://example.com", success=False, level_used=1, duration_ms=10,
            failure_category=FailureCategory.SSRF_BLOCKED,
            error_message="blocked",
        )
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-3", request)
        assert response.status == JobStatus.FAILED
        worker._dlq.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_job_escalation(self, tenant, worker):
        """L1 fails → escalates to L2 → succeeds."""
        fail_l1 = FetchResult(
            url="http://example.com", success=False, level_used=1, duration_ms=10,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        )
        success_l2 = FetchResult(
            url="http://example.com", success=True, level_used=2, duration_ms=50,
        )
        worker._fetch_url = AsyncMock(side_effect=[fail_l1, success_l2])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-4", request)
        assert response.status == JobStatus.COMPLETED
        assert response.results is not None and response.results[0].level_used == 2

    @pytest.mark.asyncio
    async def test_js_gated_l1_escalates(self, tenant, worker):
        """L1 returns 200 but a JS-gated SPA shell (round 15) — must NOT be
        accepted as content; escalates to L2 which renders the real page."""
        shell = FetchResult(
            url="http://example.com", success=True, level_used=1, duration_ms=10,
            http_status=200,
            html='<html><body><div id="root"></div><script src=a.js></script></body></html>',
        )
        real_l2 = FetchResult(
            url="http://example.com", success=True, level_used=2, duration_ms=50,
            http_status=200, html="<html><body>" + "real product data " * 40 + "</body></html>",
        )
        worker._fetch_url = AsyncMock(side_effect=[shell, real_l2])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-jsgate", request)
        assert response.status == JobStatus.COMPLETED
        # accepted the L2 render, not the L1 shell
        assert response.results is not None and response.results[0].level_used == 2

    @pytest.mark.asyncio
    async def test_host_unreachable_dead_letters_without_escalation(self, tenant, worker):
        """A dead/unresolvable host (round 15) dead-letters immediately — a
        browser can't resolve DNS the HTTP client couldn't, so escalating is
        futile. _fetch_url must be called exactly once (no L2/L3 attempts)."""
        dead = FetchResult(
            url="http://nonexistent.invalid", success=False, level_used=1, duration_ms=5,
            failure_category=FailureCategory.HOST_UNREACHABLE,
            error_message="NS_ERROR_UNKNOWN_HOST",
        )
        worker._fetch_url = AsyncMock(return_value=dead)
        request = ScrapeRequest(urls=[HttpUrl("http://nonexistent.invalid")])

        response = await worker.process_job(tenant, "job-dns", request)
        assert response.status == JobStatus.FAILED
        worker._dlq.enqueue.assert_called_once()
        assert worker._fetch_url.await_count == 1  # no escalation to L2/L3

    @pytest.mark.asyncio
    async def test_extract_domain(self, worker):
        assert worker._extract_domain("http://example.com/path") == "example.com"
        assert worker._extract_domain("https://sub.dom.com:8080/x") == "sub.dom.com"
