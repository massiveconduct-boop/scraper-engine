"""
Closes G-03 from the production-readiness gap audit.

Worker escalation state machine tests — one test per state table row
from blueprint v2 §4.1. Targets 90% coverage on worker.py.
"""

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
    return TenantId("escalationtest")


@pytest.fixture
def worker():
    redis = AsyncMock()
    cb = AsyncMock()
    cb.allow_request.return_value = True
    pc = AsyncMock()
    pc.acquire_slot.return_value = True
    dlq = AsyncMock()
    return Worker(redis=redis, circuit_breaker=cb, politeness=pc, dlq=dlq)


def success_l1(url):
    return FetchResult(url=url, success=True, level_used=1, duration_ms=10)


def fail_l1_timeout(url):
    return FetchResult(
        url=url,
        success=False,
        level_used=1,
        duration_ms=10,
        failure_category=FailureCategory.NETWORK_TIMEOUT,
    )


def fail_l2_detection(url):
    return FetchResult(
        url=url,
        success=False,
        level_used=2,
        duration_ms=50,
        failure_category=FailureCategory.DETECTION_BLOCK,
    )


def fail_ssrf(url):
    return FetchResult(
        url=url,
        success=False,
        level_used=1,
        duration_ms=10,
        failure_category=FailureCategory.SSRF_BLOCKED,
        error_message="SSRF blocked",
    )


def fail_proxy_exhausted(url):
    return FetchResult(
        url=url,
        success=False,
        level_used=2,
        duration_ms=50,
        failure_category=FailureCategory.PROXY_EXHAUSTED,
        error_message="No proxies available",
    )


class TestWorkerEscalation:
    """Covers every row of the blueprint v2 §4.1 state table."""

    @pytest.mark.asyncio
    async def test_pending_to_circuit_check_to_l1_success(self, tenant, worker):
        """PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1 (success)."""
        worker._fetch_url = AsyncMock(return_value=success_l1("http://example.com"))
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-1", request)
        assert resp.status == JobStatus.COMPLETED
        assert resp.results is not None and resp.results[0].level_used == 1

    @pytest.mark.asyncio
    async def test_l1_timeout_escalates_to_l2_success(self, tenant, worker):
        """FETCHING_L1 failure → ESCALATING_L2 → FETCHING_L2 → PARSING_L2 (success)."""
        worker._fetch_url = AsyncMock(
            side_effect=[
                fail_l1_timeout("http://example.com"),
                success_l1("http://example.com"),  # L2 success (uses same mock)
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-2", request)
        assert resp.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_l2_detection_escalates_to_l3_success(self, tenant, worker):
        """L1 fails → L2 detection block → L3 succeeds."""
        worker._fetch_url = AsyncMock(
            side_effect=[
                fail_l1_timeout("http://example.com"),
                fail_l2_detection("http://example.com"),
                success_l1("http://example.com"),  # L3 success
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-3", request)
        assert resp.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_all_levels_exhausted_goes_to_dead_letter(self, tenant, worker):
        """L1 fails → L2 fails → L3 fails → DEAD_LETTER."""
        worker._fetch_url = AsyncMock(
            side_effect=[
                fail_l1_timeout("http://example.com"),
                fail_l2_detection("http://example.com"),
                fail_l1_timeout("http://example.com"),  # L3 also fails
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-4", request)
        assert resp.status == JobStatus.FAILED
        worker._dlq.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_ssrf_blocked_goes_directly_to_dlq(self, tenant, worker):
        """SSRF blocked → DEAD_LETTER (no retry, per matrix)."""
        worker._fetch_url = AsyncMock(return_value=fail_ssrf("http://example.com"))
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-5", request)
        assert resp.status == JobStatus.FAILED
        worker._dlq.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_proxy_exhausted_goes_directly_to_dlq(self, tenant, worker):
        """ProxyPoolExhausted → DEAD_LETTER (no retry, per matrix)."""
        worker._fetch_url = AsyncMock(return_value=fail_proxy_exhausted("http://example.com"))
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-6", request)
        assert resp.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_circuit_open_blocks_immediately(self, tenant, worker):
        """CIRCUIT_CHECK → circuit open → DEAD_LETTER (never fetches)."""
        worker._circuit_breaker.allow_request.return_value = False
        worker._fetch_url = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-7", request)
        assert resp.status == JobStatus.FAILED
        # Never called fetch
        worker._fetch_url.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_parse_retry_then_escalate(self, tenant, worker):
        """PARSING_L1 returns null → retry → still null → escalate to L2 → success.

        Covers the PARSING_RETRY_L1 → ESCALATING_L2 path from §4.1 state table.
        """
        null_l1 = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            html="",
            extracted=None,
        )
        worker._fetch_url = AsyncMock(
            side_effect=[
                null_l1,
                null_l1,  # retry — still null
                success_l1("http://example.com"),  # L2 success
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        resp = await worker.process_job(tenant, "job-8", request)
        assert resp.status == JobStatus.COMPLETED
