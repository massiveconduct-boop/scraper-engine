# tests/unit/test_worker.py
"""Worker state machine tests — escalation logic with mocks."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import HttpUrl

from scraper_engine.core.exceptions import ProxyPoolExhaustedError
from scraper_engine.core.models import (
    ConfigOverrides,
    FailureCategory,
    FetchResult,
    JobStatus,
    ScrapeRequest,
)
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.worker import Worker
from scraper_engine.proxy.lease import ProxyLease


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
        result = FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=10)
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
            url="http://example.com",
            success=False,
            level_used=1,
            duration_ms=10,
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
            url="http://example.com",
            success=False,
            level_used=1,
            duration_ms=10,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        )
        success_l2 = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=50,
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
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            http_status=200,
            html='<html><body><div id="root"></div><script src=a.js></script></body></html>',
        )
        real_l2 = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=50,
            http_status=200,
            html="<html><body>" + "real product data " * 40 + "</body></html>",
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
            url="http://nonexistent.invalid",
            success=False,
            level_used=1,
            duration_ms=5,
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

    @pytest.mark.asyncio
    async def test_politeness_slot_busy_sleeps_and_advances_to_next_level(
        self, tenant, worker, monkeypatch
    ):
        """acquire_slot returning None means no free slot right now — the real
        code sleeps then `continue`s the *level* loop (moving straight to the
        next level) rather than retrying the same level indefinitely."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr("scraper_engine.orchestrator.worker.asyncio.sleep", sleep_mock)
        worker._politeness.acquire_slot = AsyncMock(side_effect=[None, "worker-2"])
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=2, duration_ms=10
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-slot-busy", request)

        sleep_mock.assert_awaited_once_with(1)
        assert worker._fetch_url.await_count == 1
        # level 1 was skipped (busy slot) — the one real fetch is for level 2
        assert worker._fetch_url.await_args.args[2] == 2
        worker._politeness.release_slot.assert_awaited_once()
        assert response.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fetch_url_none_result_advances_to_next_level(self, tenant, worker):
        """A None result from _fetch_url (defensive: happens if a level isn't
        handled) must be neither success nor failure — process_job just moves
        on to the next level instead of recording it either way."""
        worker._fetch_url = AsyncMock(
            side_effect=[
                None,
                FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=10),
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-none-result", request)

        assert worker._fetch_url.await_count == 2
        assert response.status == JobStatus.COMPLETED


class TestFetchUrlDispatch:
    """Real `_fetch_url` dispatch — every existing test above stubs this
    method out entirely, so its body (level routing, proxy leasing, the
    ProxyPoolExhaustedError->failure-result translation) was never actually
    exercised. Only the factory functions and ProxyManager are mocked here;
    the dispatch logic under test runs for real."""

    @pytest.mark.asyncio
    async def test_level1_dispatches_via_factory(self, tenant, worker, monkeypatch):
        expected = FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=5)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        build_mock = MagicMock(return_value=fake_fetcher)
        monkeypatch.setattr("scraper_engine.fetcher.factory.build_level1_fetcher", build_mock)

        result = await worker._fetch_url(tenant, "http://example.com", 1)

        assert result is expected
        build_mock.assert_called_once_with(worker._config)
        fake_fetcher.fetch.assert_awaited_once_with("http://example.com", tenant)

    @pytest.mark.asyncio
    async def test_level2_leases_proxy_and_dispatches_via_factory(
        self, tenant, worker, monkeypatch
    ):
        proxy_sentinel = object()
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        expected = FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=20)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        build_mock = MagicMock(return_value=fake_fetcher)
        monkeypatch.setattr("scraper_engine.fetcher.factory.build_level2_fetcher", build_mock)

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is expected
        pm_instance.get_proxy.assert_awaited_once_with(tenant, level=2, domain="example.com")
        build_mock.assert_called_once_with(
            worker._config,
            captcha_solver=worker._captcha_solver,
            pool=worker._browser_pool,
            botasaurus_pool=worker._botasaurus_pool,
        )
        fake_fetcher.fetch.assert_awaited_once_with(
            "http://example.com", tenant, proxy=proxy_sentinel
        )
        # the async-context-managed lease must have been released, not leaked
        assert lease._released is True

    @pytest.mark.asyncio
    async def test_level2_proxy_exhausted_returns_failure_result(self, tenant, worker, monkeypatch):
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(
            side_effect=ProxyPoolExhaustedError(domain="example.com", level=2, attempts=5)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is not None
        assert result.success is False
        assert result.level_used == 2
        assert result.failure_category == FailureCategory.PROXY_EXHAUSTED
        assert result.error_message == "Proxy pool exhausted"

    @pytest.mark.asyncio
    async def test_level3_leases_proxy_and_dispatches_via_factory(
        self, tenant, worker, monkeypatch
    ):
        proxy_sentinel = object()
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        expected = FetchResult(url="http://example.com", success=True, level_used=3, duration_ms=30)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        build_mock = MagicMock(return_value=fake_fetcher)
        monkeypatch.setattr("scraper_engine.fetcher.factory.build_level3_fetcher", build_mock)

        result = await worker._fetch_url(tenant, "http://example.com", 3)

        assert result is expected
        pm_instance.get_proxy.assert_awaited_once_with(tenant, level=3, domain="example.com")
        build_mock.assert_called_once_with(
            worker._config,
            captcha_solver=worker._captcha_solver,
            pool=worker._browser_pool,
        )
        fake_fetcher.fetch.assert_awaited_once_with(
            "http://example.com", tenant, proxy=proxy_sentinel
        )
        assert lease._released is True

    @pytest.mark.asyncio
    async def test_level3_proxy_exhausted_returns_failure_result(self, tenant, worker, monkeypatch):
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(
            side_effect=ProxyPoolExhaustedError(domain="example.com", level=3, attempts=5)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        result = await worker._fetch_url(tenant, "http://example.com", 3)

        assert result is not None
        assert result.success is False
        assert result.level_used == 3
        assert result.failure_category == FailureCategory.PROXY_EXHAUSTED
        assert result.error_message == "Proxy pool exhausted"

    @pytest.mark.asyncio
    async def test_unhandled_level_falls_through_to_none(self, tenant, worker):
        """No level in LEVELS ever reaches this (LEVELS = [1, 2, 3]), but the
        if/elif/elif chain has no else — a defensive fallthrough that returns
        None for any other integer. Exercised directly since process_job never
        drives it."""
        result = await worker._fetch_url(tenant, "http://example.com", 99)
        assert result is None


class TestExtractionWiring:
    """FetchResult.extracted is declared on the model and persisted by
    orchestrator/tasks.py, but nothing ever populated it — AdaptiveSelector
    existed, fully tested, with zero callers. Wired here (round 28)."""

    @pytest.mark.asyncio
    async def test_populates_extracted_from_successful_html(self, tenant, worker):
        html = (
            "<html><head><title>T</title></head><body><main>"
            + ("content " * 30)
            + "</main></body></html>"
        )
        result = FetchResult(
            url="http://example.com", success=True, level_used=1, duration_ms=10, html=html
        )
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-extract", request)

        assert response.results is not None
        extracted = response.results[0].extracted
        assert extracted is not None
        assert extracted["title"] == "T"

    @pytest.mark.asyncio
    async def test_passes_extraction_schema_through_when_provided(self, tenant, worker):
        result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            html="<html><body>x</body></html>",
        )
        worker._fetch_url = AsyncMock(return_value=result)
        schema = {"field": "value"}
        request = ScrapeRequest(
            urls=[HttpUrl("http://example.com")],
            config_overrides=ConfigOverrides(extraction_schema=schema),
        )

        response = await worker.process_job(tenant, "job-extract-schema", request)

        assert response.results is not None
        assert response.results[0].extracted["schema"] == schema

    @pytest.mark.asyncio
    async def test_skips_extraction_when_no_html(self, tenant, worker):
        result = FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=10)
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-extract-nohtml", request)

        assert response.results is not None
        assert response.results[0].extracted is None
