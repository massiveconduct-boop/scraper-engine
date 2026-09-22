# tests/unit/test_worker_host_admission.py
"""Worker <-> host admission wiring (round 65).

With host admission on, a browser level takes its browser seat, politeness
slot and delay in one claim per render (orchestrator/host_capacity.py),
never the old slot-first path; claim failures become transient,
circuit-exempt CAPACITY_TIMEOUT / DEPENDENCY_UNAVAILABLE results.
"""

from __future__ import annotations

import contextlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.config.schema import AppConfig, HostCapacityConfig
from scraper_engine.core.models import (
    ConfigOverrides,
    FailureCategory,
    FetchResult,
    JobStatus,
    ScrapeRequest,
)
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.host_capacity import (
    AdmissionCancelledError,
    AdmissionTimeoutError,
    AdmissionUnavailableError,
    Grant,
)
from scraper_engine.orchestrator.worker import Worker, _RenderAdmission
from scraper_engine.proxy.lease import ProxyLease
from tests.unit.test_worker import make_politeness_mock, make_redis_mock

TENANT = TenantId("test")
PAGE = "<html><body>" + "real content " * 100 + "</body></html>"


class FakeAdmission:
    """Records every claim; optionally raises instead of granting."""

    def __init__(self, error: Exception | None = None, wait_ms: int = 7) -> None:
        self.calls: list[dict] = []
        self.error = error
        self.wait_ms = wait_ms

    @contextlib.asynccontextmanager
    async def claim(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        yield Grant(lease_id="l", wait_ms=self.wait_ms, in_use=1.0, target=4.0)


def _worker(admission=None, **cfg_overrides):
    cb = AsyncMock()
    cb.allow_request.return_value = True
    config = AppConfig(host_capacity=HostCapacityConfig(enabled=True, **cfg_overrides))
    pg = AsyncMock()
    pg.fetchrow.return_value = None
    return Worker(
        redis=make_redis_mock(),
        circuit_breaker=cb,
        politeness=make_politeness_mock(),
        dlq=AsyncMock(),
        config=config,
        pg=pg,
        admission=admission,
    )


def _request(min_level=2, **overrides):
    return ScrapeRequest(
        urls=["http://example.com/p"],
        config_overrides=ConfigOverrides(min_level=min_level, **overrides),
    )


class TestProcessJob:
    @pytest.mark.asyncio
    async def test_browser_levels_skip_the_slot_path_and_pass_the_claim_context(self):
        worker = _worker(FakeAdmission())
        seen = {}

        async def fake_fetch(tenant_id, url, level, overrides, **kwargs):
            seen[level] = kwargs["admission"]
            kwargs["admission"].timings["admission_wait_ms"] = 40
            return FetchResult(url=url, success=True, level_used=level, duration_ms=5, html=PAGE)

        worker._fetch_url = fake_fetch
        response = await worker.process_job(
            TENANT, "job", _request(politeness_concurrency=4, politeness_delay_seconds=2.0)
        )

        assert response.status == JobStatus.COMPLETED
        ctx = seen[2]
        assert isinstance(ctx, _RenderAdmission)
        assert (ctx.domain, ctx.concurrency, ctx.delay_seconds) == ("example.com", 4, 2.0)
        worker._politeness.acquire_slot.assert_not_awaited()
        worker._politeness.wait_if_needed.assert_not_awaited()
        timings = response.results[0].timings
        assert timings["admission_wait_ms"] == 40
        assert "slot_wait_ms" not in timings
        assert await ctx.is_cancelled() is False

    @pytest.mark.asyncio
    async def test_level_1_keeps_the_slot_path_without_a_claim(self):
        worker = _worker(FakeAdmission())
        seen = {}

        async def fake_fetch(tenant_id, url, level, overrides, **kwargs):
            seen[level] = kwargs["admission"]
            return FetchResult(url=url, success=True, level_used=level, duration_ms=5, html=PAGE)

        worker._fetch_url = fake_fetch
        await worker.process_job(TENANT, "job", _request(min_level=1))
        assert seen[1] is None
        worker._politeness.acquire_slot.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_wait_is_bounded_by_the_rq_deadline_minus_the_margin(self):
        worker = _worker(FakeAdmission(), deadline_margin_seconds=240)
        seen = {}

        async def fake_fetch(tenant_id, url, level, overrides, **kwargs):
            seen["ctx"] = kwargs["admission"]
            return FetchResult(url=url, success=True, level_used=level, duration_ms=5, html=PAGE)

        worker._fetch_url = fake_fetch
        deadline = time.monotonic() + 1000
        await worker.process_job(TENANT, "job", _request(), deadline=deadline)
        assert seen["ctx"].deadline == pytest.approx(deadline - 240)
        assert 755 < seen["ctx"].wait_budget() <= 760

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "category"),
        [
            (AdmissionTimeoutError(12_000), FailureCategory.CAPACITY_TIMEOUT),
            (AdmissionUnavailableError("redis down"), FailureCategory.DEPENDENCY_UNAVAILABLE),
        ],
    )
    async def test_claim_failures_are_transient_and_never_blame_the_domain(
        self, error, category
    ):
        worker = _worker(FakeAdmission())
        worker._fetch_url = AsyncMock(side_effect=error)
        on_result = AsyncMock()

        response = await worker.process_job(TENANT, "job", _request(), on_result=on_result)

        result = response.results[0]
        assert result.failure_category == category
        worker._circuit_breaker.record_failure.assert_not_awaited()
        worker._dlq.enqueue.assert_awaited_once()
        assert worker._dlq.enqueue.await_args.args[3] == category
        on_result.assert_awaited_once_with(result)
        if category == FailureCategory.CAPACITY_TIMEOUT:
            assert result.timings["admission_wait_ms"] == 12_000
        else:
            assert "admission_wait_ms" not in result.timings

    @pytest.mark.asyncio
    async def test_claim_failure_without_a_result_callback(self):
        worker = _worker(FakeAdmission())
        worker._fetch_url = AsyncMock(side_effect=AdmissionTimeoutError(1_000))
        response = await worker.process_job(TENANT, "job", _request())
        assert response.results[0].failure_category == FailureCategory.CAPACITY_TIMEOUT

    @pytest.mark.asyncio
    async def test_cancellation_while_waiting_ends_the_url_quietly(self):
        worker = _worker(FakeAdmission())
        worker._fetch_url = AsyncMock(side_effect=AdmissionCancelledError("cancelled"))
        response = await worker.process_job(TENANT, "job", _request())
        assert response.status == JobStatus.CANCELLED
        worker._dlq.enqueue.assert_not_awaited()


class TestRenderClaim:
    @pytest.mark.asyncio
    async def test_claim_carries_the_url_context_and_accumulates_the_wait(self):
        admission = FakeAdmission(wait_ms=30)
        worker = _worker(admission)
        timings: dict[str, int] = {"admission_wait_ms": 5}
        ctx = _RenderAdmission(
            tenant_id=TENANT,
            domain="example.com",
            concurrency=3,
            delay_seconds=1.5,
            priority_ms=42,
            deadline=time.monotonic() + 100,
            is_cancelled=AsyncMock(return_value=False),
            timings=timings,
        )
        async with worker._render_claim(ctx, 1.5):
            pass
        call = admission.calls[0]
        assert (call["weight"], call["concurrency"], call["delay_seconds"]) == (1.5, 3, 1.5)
        assert call["priority_ms"] == 42 and 99 < call["wait_budget_seconds"] <= 100
        assert timings["admission_wait_ms"] == 35

    @pytest.mark.asyncio
    async def test_no_claim_without_a_context_or_without_admission(self):
        admission = FakeAdmission()
        worker = _worker(admission)
        async with worker._render_claim(None, 1.0):
            pass
        worker_without = _worker(None)
        ctx = MagicMock()
        async with worker_without._render_claim(ctx, 1.0):
            pass
        assert admission.calls == []

    @pytest.mark.asyncio
    async def test_every_render_pass_claims_including_a_pool_retry(self, monkeypatch):
        admission = FakeAdmission()
        worker = _worker(admission)
        proxy = MagicMock(ip="1.1.1.1", port=8080, source="pool")
        pm = MagicMock()
        pm.get_proxy = AsyncMock(
            side_effect=[ProxyLease(proxy=proxy, tenant_id=TENANT)] * 2
        )
        pm.mark_success = AsyncMock()
        pm.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm)
        )
        crash = FetchResult(
            url="http://example.com", success=False, level_used=3, duration_ms=1,
            failure_category=FailureCategory.BROWSER_CRASH,
        )
        ok = FetchResult(url="http://example.com", success=True, level_used=3, duration_ms=1)
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=[crash, ok])
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level3_fetcher", MagicMock(return_value=fetcher)
        )
        ctx = _RenderAdmission(
            tenant_id=TENANT, domain="example.com", concurrency=2, delay_seconds=0.0,
            priority_ms=1, deadline=time.monotonic() + 60,
            is_cancelled=AsyncMock(return_value=False), timings={},
        )
        result = await worker._fetch_url(TENANT, "http://example.com", 3, admission=ctx)
        assert result is ok
        assert len(admission.calls) == 2


class TestClaimWeight:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("level", "skip_botasaurus", "engine", "expected"),
        [
            (2, False, "botasaurus+camoufox", 1.5),
            (2, True, "botasaurus+camoufox", 1.0),
            (2, False, "camoufox", 1.0),
            (3, False, "camoufox", 1.0),
        ],
    )
    async def test_weight_follows_the_heaviest_engine_the_render_may_use(
        self, level, skip_botasaurus, engine, expected
    ):
        worker = _worker(FakeAdmission(), botasaurus_weight=1.5, camoufox_weight=1.0)
        worker._config.levels.level_2.engine = engine
        worker._fetch_with_proxy = AsyncMock(return_value="r")
        await worker._fetch_url(
            TENANT, "http://example.com", level, skip_botasaurus=skip_botasaurus
        )
        assert worker._fetch_with_proxy.await_args.kwargs["weight"] == expected


class TestRerunAndClamp:
    @pytest.mark.asyncio
    async def test_a_rerun_skips_urls_this_job_already_scraped(self):
        """Round 65 — a DLQ re-drive re-runs the whole job; finished URLs must
        not be rendered again, even with bypass_cache."""
        worker = _worker(FakeAdmission())
        worker._pg.fetch.return_value = [{"url": "http://example.com/p"}]
        worker._fetch_url = AsyncMock()
        request = _request(bypass_cache=True)
        response = await worker.process_job(TENANT, "job", request)
        worker._fetch_url.assert_not_awaited()
        assert response.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_no_postgres_means_nothing_to_skip(self):
        worker = _worker(FakeAdmission())
        worker._pg = None
        assert await worker._succeeded_urls(TENANT, "job") == set()

    @pytest.mark.parametrize(("asked", "used"), [(900, 300), (120, 120)])
    def test_caller_timeout_is_clamped_to_the_operator_ceiling(self, asked, used):
        worker = _worker(FakeAdmission())
        clamped = worker._clamp_timeout(_request(timeout_seconds=asked))
        assert clamped.config_overrides.timeout_seconds == used

    def test_a_request_without_overrides_is_left_alone(self):
        worker = _worker(FakeAdmission())
        request = ScrapeRequest(urls=["http://example.com/p"])
        assert worker._clamp_timeout(request) is request
