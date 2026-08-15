# tests/unit/test_worker.py
"""Worker state machine tests — escalation logic with mocks."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import HttpUrl

from scraper_engine.core.exceptions import PostgresClientMissingError, ProxyPoolExhaustedError
from scraper_engine.core.models import (
    ConfigOverrides,
    FailureCategory,
    FetchResult,
    JobStatus,
    Proxy,
    ProxyProtocol,
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
    # Real PostgresClient (not None) so _dispatch_level's L2/L3 guard
    # (PostgresClientMissingError, round-N fix for pg=None crashing every
    # real escalation) doesn't fire for tests that don't care about it.
    # fetchrow defaults to None so _is_cancelled/_check_cache keep their
    # pre-existing "no cancellation, no cache hit" default behavior.
    pg = AsyncMock()
    pg.fetchrow.return_value = None
    return Worker(redis=redis, circuit_breaker=cb, politeness=pc, dlq=dlq, pg=pg)


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
    async def test_process_job_partial_failure_flagged_not_masked_as_clean_success(
        self, tenant, worker
    ):
        """round 34 regression test — a job with one URL succeeding and one
        DLQ'd (PROXY_EXHAUSTED) must report status=COMPLETED (existing
        contract: "COMPLETED" means "the job ran to completion", not "every
        URL succeeded") AND partial_failure=True, so a caller/webhook can't
        mistake this for a clean run. Before this round, JobStatusResponse
        had no way to distinguish the two — this is the exact bug the
        investigation surfaced."""
        success = FetchResult(
            url="http://good.example.com", success=True, level_used=1, duration_ms=10
        )
        exhausted = FetchResult(
            url="http://bad.example.com",
            success=False,
            level_used=1,
            duration_ms=10,
            failure_category=FailureCategory.PROXY_EXHAUSTED,
            error_message="Proxy pool exhausted",
        )
        worker._fetch_url = AsyncMock(side_effect=[success, exhausted])
        request = ScrapeRequest(
            urls=[HttpUrl("http://good.example.com"), HttpUrl("http://bad.example.com")]
        )

        response = await worker.process_job(tenant, "job-partial", request)

        assert response.status == JobStatus.COMPLETED
        assert response.partial_failure is True

    @pytest.mark.asyncio
    async def test_process_job_full_success_has_no_partial_failure(self, tenant, worker):
        """Companion to the regression test above — a clean run must not be
        flagged, or every caller would have to start ignoring the flag."""
        result = FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=10)
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-clean", request)

        assert response.status == JobStatus.COMPLETED
        assert response.partial_failure is False

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
    async def test_unsolved_challenge_page_l1_escalates(self, tenant, worker):
        """L1 returns HTTP 200 for a genuine 200-status challenge/interstitial
        page (e.g. a JS proof-of-work gate) — `success = status < 400` alone
        makes this look like a real fetch. Must not be accepted as content;
        escalates to L2 which actually solves it. Regression test for a live
        production-readiness re-verification finding: `is_challenge_page` was
        declared on FetchResult and even gated dedup.py's caching decision,
        but no fetcher ever set it, so this exact case silently short-
        circuited the whole escalation ladder."""
        interstitial = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            http_status=200,
            html="<html><body>Checking your browser before continuing…</body></html>",
        )
        solved_l2 = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=50,
            http_status=200,
            html="<html><body>challenge-mirror-ok</body></html>",
        )
        worker._fetch_url = AsyncMock(side_effect=[interstitial, solved_l2])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-challenge", request)
        assert response.status == JobStatus.COMPLETED
        assert response.results is not None and response.results[0].level_used == 2

    @pytest.mark.asyncio
    async def test_gateway_error_page_escalates(self, tenant, worker):
        """Round 33: a free proxy's own upstream dying returns success=True,
        http_status=502/504 (level_2.py/level_3.py now report the real
        navigation status instead of a hardcoded 200) with a gateway-error
        HTML body as the "content" — not real target content. Worker's
        centralized is_challenge_page reclassification (fed the real status)
        must catch this and escalate, the same way an unsolved anti-bot
        challenge does. Regression test for the live bug that started this
        investigation: `success: true` with `<title>504 Gateway
        Time-out</title>` as the actual content, silently accepted."""
        gateway_error = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=50,
            http_status=504,
            html="<html><head><title>504 Gateway Time-out</title></head><body></body></html>",
        )
        real_content = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=200,
            http_status=200,
            html="<html><body>" + "real product data " * 40 + "</body></html>",
        )
        worker._fetch_url = AsyncMock(side_effect=[gateway_error, real_content])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-gateway-error", request)
        assert response.status == JobStatus.COMPLETED
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

    @pytest.mark.asyncio
    async def test_process_job_cancelled_before_starting_stops_immediately(self, tenant, worker):
        """A DELETE /v1/jobs/{job_id} landing between URLs (round 29) — the
        cooperative _is_cancelled check must stop the loop before any fetch
        for the next URL, not just log it."""
        worker._pg = AsyncMock()
        worker._pg.fetchrow.return_value = {"status": JobStatus.CANCELLED.value}
        worker._fetch_url = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-cancelled", request)

        assert response.status == JobStatus.CANCELLED
        worker._fetch_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_job_cache_hit_skips_fetch_and_calls_on_result(self, tenant, worker):
        """A fresh scrape_results row for this exact URL (round 29) is reused
        instead of re-fetching — the cache-hit FetchResult still flows
        through on_result like any other terminal outcome."""
        worker._pg = AsyncMock()
        worker._pg.fetchrow.side_effect = [
            {"status": JobStatus.PROCESSING.value},  # _is_cancelled check
            {
                "http_status": 200,
                "is_challenge_page": False,
                "level_used": 1,
                "proxy_used": None,
                "markdown": "# cached",
                "json_data": None,
                "html_snapshot_url": "snapshots/t/j/old.html",
            },
        ]
        worker._fetch_url = AsyncMock()
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-cache-hit", request, on_result=on_result)

        worker._fetch_url.assert_not_awaited()
        assert response.status == JobStatus.COMPLETED
        assert response.results is not None
        cached = response.results[0]
        assert cached.from_cache is True
        assert cached.markdown == "# cached"
        assert cached.html_snapshot_url == "snapshots/t/j/old.html"
        on_result.assert_awaited_once_with(cached)

    @pytest.mark.asyncio
    async def test_process_job_cache_miss_with_pg_configured_falls_through_to_fetch(
        self, tenant, worker
    ):
        """pg configured but no matching cache row (round 29) — must fall
        through to a real fetch, not be mistaken for the pg-is-None
        short-circuit path."""
        worker._pg = AsyncMock()
        worker._pg.fetchrow.side_effect = [
            {"status": JobStatus.PROCESSING.value},  # _is_cancelled check
            None,  # _check_cache: no matching row
        ]
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=1, duration_ms=10
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-cache-miss", request)

        worker._fetch_url.assert_awaited_once()
        assert response.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_process_job_populates_markdown_when_firecrawl_configured(self, tenant, worker):
        """Markdown conversion (round 29) is centralized here so it applies
        regardless of which escalation level actually succeeded — Firecrawl
        wiring itself moved out of Level1Fetcher entirely."""
        worker._firecrawl = AsyncMock()
        worker._firecrawl.convert_to_markdown.return_value = "# converted"
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com",
                success=True,
                level_used=1,
                duration_ms=10,
                html="<html>hi</html>",
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-markdown", request)

        assert response.results is not None
        assert response.results[0].markdown == "# converted"
        worker._firecrawl.convert_to_markdown.assert_awaited_once_with(
            "<html>hi</html>", "http://example.com/"
        )

    @pytest.mark.asyncio
    async def test_process_job_falls_back_to_local_markdown_without_firecrawl(self, tenant, worker):
        """Without Firecrawl configured (FIRECRAWL_API_KEY/FIRECRAWL_BASE_URL
        both unset — the common case, round 33), FetchResult.markdown used
        to stay None entirely, leaving a caller with only `extracted`
        (title/body/links). It must now be populated via the local
        html_to_markdown fallback instead of silently staying empty."""
        assert worker._firecrawl is None
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com",
                success=True,
                level_used=1,
                duration_ms=10,
                html="<html><body><h1>Hi</h1></body></html>",
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-markdown-fallback", request)

        assert response.results is not None
        assert response.results[0].markdown is not None
        assert "Hi" in response.results[0].markdown

    @pytest.mark.asyncio
    async def test_process_job_calls_on_result_for_success(self, tenant, worker):
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=1, duration_ms=10
            )
        )
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(
            tenant, "job-on-result-ok", request, on_result=on_result
        )

        on_result.assert_awaited_once_with(response.results[0])

    @pytest.mark.asyncio
    async def test_process_job_calls_on_result_for_non_retryable_failure(self, tenant, worker):
        result = FetchResult(
            url="http://example.com",
            success=False,
            level_used=1,
            duration_ms=10,
            failure_category=FailureCategory.SSRF_BLOCKED,
            error_message="blocked",
        )
        worker._fetch_url = AsyncMock(return_value=result)
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-on-result-nonretryable", request, on_result=on_result)

        on_result.assert_awaited_once_with(result)

    @pytest.mark.asyncio
    async def test_process_job_calls_on_result_for_circuit_open(self, tenant, worker):
        worker._circuit_breaker.allow_request.return_value = False
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(
            tenant, "job-on-result-circuit", request, on_result=on_result
        )

        assert response.status == JobStatus.FAILED
        on_result.assert_awaited_once()
        assert on_result.await_args.args[0].failure_category == FailureCategory.CIRCUIT_OPEN

    @pytest.mark.asyncio
    async def test_process_job_calls_on_result_for_exhausted_levels(self, tenant, worker):
        """All 3 levels exhausted with a retryable failure category — the
        for/else branch synthesizes its own FetchResult (round 29) since
        none of the individual level attempts produced one worth keeping."""
        timeout_failure = FetchResult(
            url="http://example.com",
            success=False,
            level_used=1,
            duration_ms=5,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
            error_message="timed out",
        )
        worker._fetch_url = AsyncMock(return_value=timeout_failure)
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(
            tenant, "job-on-result-exhausted", request, on_result=on_result
        )

        assert response.status == JobStatus.FAILED
        assert worker._fetch_url.await_count == 3  # L1, L2, L3 all attempted
        on_result.assert_awaited_once()
        exhausted = on_result.await_args.args[0]
        assert exhausted.failure_category == FailureCategory.PROXY_EXHAUSTED
        assert exhausted.error_message == "All fetch levels exhausted"


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
        fake_fetcher.fetch.assert_awaited_once_with("http://example.com", tenant, overrides=None)

    @pytest.mark.asyncio
    async def test_level2_leases_proxy_and_dispatches_via_factory(
        self, tenant, worker, monkeypatch
    ):
        proxy_sentinel = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        pm_ctor = MagicMock(return_value=pm_instance)
        monkeypatch.setattr("scraper_engine.proxy.manager.ProxyManager", pm_ctor)

        expected = FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=20)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        build_mock = MagicMock(return_value=fake_fetcher)
        monkeypatch.setattr("scraper_engine.fetcher.factory.build_level2_fetcher", build_mock)

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is expected
        # proves the pg=None regression stays fixed — ProxyManager must be
        # constructed with the worker's real PostgresClient, not a hardcoded None
        pm_ctor.assert_called_once_with(
            redis=worker._redis, pg=worker._pg, tier_config=worker._config.proxy_tiers
        )
        pm_instance.get_proxy.assert_awaited_once_with(tenant, level=2, domain="example.com")
        build_mock.assert_called_once_with(
            worker._config,
            captcha_solver=worker._captcha_solver,
            pool=worker._browser_pool,
            botasaurus_pool=worker._botasaurus_pool,
        )
        fake_fetcher.fetch.assert_awaited_once_with(
            "http://example.com", tenant, proxy=proxy_sentinel, overrides=None
        )
        # the async-context-managed lease must have been released, not leaked
        assert lease._released is True
        # Round 32: a real fetch outcome must update the proxy's score —
        # mark_success/mark_failure were built but never actually called.
        pm_instance.mark_success.assert_awaited_once_with(tenant, "1.2.3.4", 8080)
        pm_instance.mark_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_level2_proxy_exhausted_returns_failure_result(self, tenant, worker, monkeypatch):
        """Explicitly pinned to free_only (round 40): this tests what
        happens on pool exhaustion specifically WITHOUT a gateway fallback
        configured — TestDataImpulseStrategy covers the free_first-falls-
        back-to-gateway case separately. Pinning here (rather than relying
        on the worker fixture's default) keeps this test's outcome
        independent of whatever config/base.yaml's real dataimpulse.enabled
        value happens to be at any given time."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig()
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
        proxy_sentinel = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        pm_ctor = MagicMock(return_value=pm_instance)
        monkeypatch.setattr("scraper_engine.proxy.manager.ProxyManager", pm_ctor)

        expected = FetchResult(url="http://example.com", success=True, level_used=3, duration_ms=30)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        build_mock = MagicMock(return_value=fake_fetcher)
        monkeypatch.setattr("scraper_engine.fetcher.factory.build_level3_fetcher", build_mock)

        result = await worker._fetch_url(tenant, "http://example.com", 3)

        assert result is expected
        pm_ctor.assert_called_once_with(
            redis=worker._redis, pg=worker._pg, tier_config=worker._config.proxy_tiers
        )
        pm_instance.get_proxy.assert_awaited_once_with(tenant, level=3, domain="example.com")
        build_mock.assert_called_once_with(
            worker._config,
            captcha_solver=worker._captcha_solver,
            pool=worker._browser_pool,
        )
        fake_fetcher.fetch.assert_awaited_once_with(
            "http://example.com", tenant, proxy=proxy_sentinel, overrides=None
        )
        assert lease._released is True
        pm_instance.mark_success.assert_awaited_once_with(tenant, "1.2.3.4", 8080)
        pm_instance.mark_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_level3_proxy_exhausted_returns_failure_result(self, tenant, worker, monkeypatch):
        """See test_level2_proxy_exhausted_returns_failure_result — same
        round-40 free_only pin, same reasoning."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig()
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
    async def test_level2_real_fetch_failure_marks_failure_not_success(
        self, tenant, worker, monkeypatch
    ):
        """Round 32: a real fetch that comes back success=False must call
        mark_failure (bans + recomputes down), never mark_success. Round 37:
        NETWORK_TIMEOUT is a proxy-retryable category (_PROXY_RETRYABLE_
        CATEGORIES) — with _SAME_LEVEL_PROXY_RETRIES=1, a fetcher that keeps
        failing the same way gets tried twice (fresh lease each time)
        before this level gives up, so both get_proxy/fetch/mark_failure
        fire twice, not once."""
        proxy_sentinel = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        failed_result = FetchResult(
            url="http://example.com",
            success=False,
            level_used=2,
            duration_ms=10,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        )
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=failed_result)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is failed_result
        assert pm_instance.get_proxy.await_count == 2
        assert pm_instance.mark_failure.await_count == 2
        pm_instance.mark_failure.assert_awaited_with(tenant, "1.2.3.4", 8080, "example.com")
        pm_instance.mark_success.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_level3_real_fetch_failure_marks_failure_not_success(
        self, tenant, worker, monkeypatch
    ):
        """See test_level2_real_fetch_failure_marks_failure_not_success —
        same round-37 retry semantics apply to L3."""
        proxy_sentinel = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        failed_result = FetchResult(
            url="http://example.com",
            success=False,
            level_used=3,
            duration_ms=10,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        )
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=failed_result)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level3_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 3)

        assert result is failed_result
        assert pm_instance.get_proxy.await_count == 2
        assert pm_instance.mark_failure.await_count == 2
        pm_instance.mark_failure.assert_awaited_with(tenant, "1.2.3.4", 8080, "example.com")
        pm_instance.mark_success.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_level2_retryable_failure_then_success_uses_fresh_lease(
        self, tenant, worker, monkeypatch
    ):
        """Round 37 — first lease's fetch fails with a proxy-retryable
        category; the retry must lease a NEW proxy (not reuse the failed
        one) and, on success, mark_success only the second proxy."""
        proxy_a = MagicMock(ip="1.1.1.1", port=8080, source="pool")
        proxy_b = MagicMock(ip="2.2.2.2", port=8080, source="pool")
        lease_a = ProxyLease(proxy=proxy_a, tenant_id=tenant)
        lease_b = ProxyLease(proxy=proxy_b, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(side_effect=[lease_a, lease_b])
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        failed_result = FetchResult(
            url="http://example.com",
            success=False,
            level_used=2,
            duration_ms=10,
            failure_category=FailureCategory.BROWSER_CRASH,
        )
        success_result = FetchResult(
            url="http://example.com", success=True, level_used=2, duration_ms=15
        )
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(side_effect=[failed_result, success_result])
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is success_result
        pm_instance.mark_failure.assert_awaited_once_with(tenant, "1.1.1.1", 8080, "example.com")
        pm_instance.mark_success.assert_awaited_once_with(tenant, "2.2.2.2", 8080)

    @pytest.mark.asyncio
    async def test_level2_non_retryable_failure_category_gives_up_immediately(
        self, tenant, worker, monkeypatch
    ):
        """Round 37 — a failure category NOT in _PROXY_RETRYABLE_CATEGORIES
        (e.g. DETECTION_BLOCK, a page/content-level failure, not a proxy
        one) must NOT trigger a same-level retry — retrying with a
        different proxy wouldn't plausibly fix a detection/content issue,
        just burn another lease for no reason."""
        proxy_sentinel = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=proxy_sentinel, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        failed_result = FetchResult(
            url="http://example.com",
            success=False,
            level_used=2,
            duration_ms=10,
            failure_category=FailureCategory.DETECTION_BLOCK,
        )
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=failed_result)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is failed_result
        pm_instance.get_proxy.assert_awaited_once()
        pm_instance.mark_failure.assert_awaited_once_with(tenant, "1.2.3.4", 8080, "example.com")

    @pytest.mark.asyncio
    async def test_level2_dispatch_raises_when_pg_missing(self, tenant):
        """A Worker constructed with pg=None must fail loudly at the L2 guard,
        not crash inside ProxyManager with an AttributeError on None (the
        original bug) — construction-time misconfiguration propagates
        uncaught rather than degrading to a per-URL failure result."""
        redis = AsyncMock()
        cb = AsyncMock()
        pc = AsyncMock()
        dlq = AsyncMock()
        pg_missing_worker = Worker(redis=redis, circuit_breaker=cb, politeness=pc, dlq=dlq, pg=None)

        with pytest.raises(PostgresClientMissingError):
            await pg_missing_worker._fetch_url(tenant, "http://example.com", 2)

    @pytest.mark.asyncio
    async def test_level3_dispatch_raises_when_pg_missing(self, tenant):
        redis = AsyncMock()
        cb = AsyncMock()
        pc = AsyncMock()
        dlq = AsyncMock()
        pg_missing_worker = Worker(redis=redis, circuit_breaker=cb, politeness=pc, dlq=dlq, pg=None)

        with pytest.raises(PostgresClientMissingError):
            await pg_missing_worker._fetch_url(tenant, "http://example.com", 3)

    @pytest.mark.asyncio
    async def test_unhandled_level_falls_through_to_none(self, tenant, worker):
        """No level in LEVELS ever reaches this (LEVELS = [1, 2, 3]), but the
        if/elif/elif chain has no else — a defensive fallthrough that returns
        None for any other integer. Exercised directly since process_job never
        drives it."""
        result = await worker._fetch_url(tenant, "http://example.com", 99)
        assert result is None


class TestDataImpulseStrategy:
    """Round 40 — config.dataimpulse's three-way toggle
    (free_only/paid_only/free_first) inside _fetch_with_proxy. free_only's
    behavior is proven unchanged by every test above (the `worker` fixture's
    default AppConfig has dataimpulse.enabled=False, so strategy resolves to
    "free_only" and none of these branches fire) — these tests cover only
    the two new branches this round added."""

    @staticmethod
    def _gateway_proxy() -> Proxy:
        return Proxy(
            id=-1,
            ip="gw.dataimpulse.com",
            port=823,
            protocol=ProxyProtocol.HTTP,
            username="user123",
            password="pass456",
            source="paid_gateway",
        )

    @pytest.mark.asyncio
    async def test_paid_only_never_touches_free_pool(self, tenant, worker, monkeypatch):
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="paid_only")

        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock()
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy",
            MagicMock(return_value=self._gateway_proxy()),
        )

        expected = FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=5)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is expected
        pm_instance.get_proxy.assert_not_awaited()
        # gateway lease (source="paid_gateway") must never write to proxy_pool scoring
        pm_instance.mark_success.assert_not_awaited()
        pm_instance.mark_failure.assert_not_awaited()
        fake_fetcher.fetch.assert_awaited_once_with(
            "http://example.com", tenant, proxy=self._gateway_proxy(), overrides=None
        )

    @pytest.mark.asyncio
    async def test_paid_only_raises_when_gateway_misconfigured(self, tenant, worker, monkeypatch):
        """Defense in depth — Worker.__init__ should already have caught a
        bad config at construction time; _fetch_with_proxy must still never
        silently fall back to the free pool if this somehow drifts out of
        sync with the startup check."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="paid_only")
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", MagicMock(return_value=None)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=MagicMock())
        )

        with pytest.raises(RuntimeError, match="paid_only"):
            await worker._fetch_url(tenant, "http://example.com", 2)

    @pytest.mark.asyncio
    async def test_free_first_falls_back_to_gateway_on_exhaustion(
        self, tenant, worker, monkeypatch
    ):
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")

        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(
            side_effect=ProxyPoolExhaustedError(domain="example.com", level=2, attempts=10)
        )
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy",
            MagicMock(return_value=self._gateway_proxy()),
        )

        expected = FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=5)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is expected
        pm_instance.get_proxy.assert_awaited_once()
        pm_instance.mark_success.assert_not_awaited()  # gateway lease, not a pool proxy

    @pytest.mark.asyncio
    async def test_free_first_never_touches_gateway_when_pool_succeeds(
        self, tenant, worker, monkeypatch
    ):
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")

        pool_proxy = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=pool_proxy, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        gateway_mock = MagicMock(return_value=self._gateway_proxy())
        monkeypatch.setattr("scraper_engine.proxy.paid_gateway.build_gateway_proxy", gateway_mock)

        expected = FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=5)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is expected
        gateway_mock.assert_not_called()
        pm_instance.mark_success.assert_awaited_once_with(tenant, "1.2.3.4", 8080)

    @pytest.mark.asyncio
    async def test_free_first_raises_when_gateway_misconfigured_after_exhaustion(
        self, tenant, worker, monkeypatch
    ):
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")

        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(
            side_effect=ProxyPoolExhaustedError(domain="example.com", level=2, attempts=10)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", MagicMock(return_value=None)
        )

        with pytest.raises(RuntimeError, match="free_first"):
            await worker._fetch_url(tenant, "http://example.com", 2)


class TestDataImpulseStartupValidation:
    """Round 40 — Worker.__init__ fails fast when dataimpulse.enabled=True
    but the gateway env vars aren't set, instead of every _fetch_with_proxy
    call degrading silently mid-job. Not async — no fetch happens here."""

    @staticmethod
    def _new_worker(config):
        redis = AsyncMock()
        cb = AsyncMock()
        pc = AsyncMock()
        dlq = AsyncMock()
        return Worker(redis=redis, circuit_breaker=cb, politeness=pc, dlq=dlq, config=config)

    def test_raises_when_enabled_but_gateway_not_configured(self, monkeypatch):
        from scraper_engine.config.schema import AppConfig, DataImpulseConfig

        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", MagicMock(return_value=None)
        )
        cfg = AppConfig(dataimpulse=DataImpulseConfig(enabled=True, strategy="paid_only"))

        with pytest.raises(RuntimeError, match="DATAIMPULSE"):
            self._new_worker(cfg)

    def test_does_not_raise_when_enabled_and_configured(self, monkeypatch):
        from scraper_engine.config.schema import AppConfig, DataImpulseConfig

        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy",
            MagicMock(return_value=TestDataImpulseStrategy._gateway_proxy()),
        )
        cfg = AppConfig(dataimpulse=DataImpulseConfig(enabled=True, strategy="paid_only"))

        self._new_worker(cfg)  # must not raise

    def test_does_not_check_gateway_when_disabled(self, monkeypatch):
        from scraper_engine.config.schema import AppConfig

        gateway_mock = MagicMock(return_value=None)
        monkeypatch.setattr("scraper_engine.proxy.paid_gateway.build_gateway_proxy", gateway_mock)
        cfg = AppConfig()  # dataimpulse.enabled=False default

        self._new_worker(cfg)
        gateway_mock.assert_not_called()

    def test_default_dataimpulse_config_is_disabled_free_only(self):
        """Locks in the out-of-the-box default: the toggle does nothing
        until explicitly turned on, so every deployment that predates
        round 40 keeps today's free-pool-only behavior unchanged."""
        from scraper_engine.config.schema import AppConfig

        cfg = AppConfig()
        assert cfg.dataimpulse.enabled is False
        assert cfg.dataimpulse.strategy == "free_only"


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
    async def test_uses_extraction_engine_when_configured_and_schema_provided(self, tenant, worker):
        """When EXTRACTION_ENGINE_BASE_URL is configured (self._extraction_engine
        is not None) and a real schema is supplied, extraction-engine's real
        result is used instead of AdaptiveSelector's -- and the two new
        ConfigOverrides flags reach the client call."""
        worker._extraction_engine = AsyncMock()
        worker._extraction_engine.extract.return_value = {"fields": {"price": {"value": "9.99"}}}
        result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            html="<html><body>x</body></html>",
        )
        worker._fetch_url = AsyncMock(return_value=result)
        schema = {"price": "string"}
        request = ScrapeRequest(
            urls=[HttpUrl("http://example.com")],
            config_overrides=ConfigOverrides(
                extraction_schema=schema,
                extraction_enable_smallmodel=True,
                extraction_enable_llm=True,
            ),
        )

        response = await worker.process_job(tenant, "job-extract-engine", request)

        assert response.results is not None
        assert response.results[0].extracted == {"fields": {"price": {"value": "9.99"}}}
        worker._extraction_engine.extract.assert_awaited_once_with(
            "<html><body>x</body></html>",
            schema,
            enable_smallmodel=True,
            enable_llm=True,
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_adaptive_selector_when_extraction_engine_fails(
        self, tenant, worker
    ):
        """extraction-engine fails soft (returns None, never raises) -- the job
        must still complete via AdaptiveSelector, not be lost."""
        worker._extraction_engine = AsyncMock()
        worker._extraction_engine.extract.return_value = None
        result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            html="<html><body>x</body></html>",
        )
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(
            urls=[HttpUrl("http://example.com")],
            config_overrides=ConfigOverrides(extraction_schema={"price": "string"}),
        )

        response = await worker.process_job(tenant, "job-extract-fallback", request)

        assert response.results is not None
        assert response.results[0].extracted["schema"] == {"price": "string"}

    @pytest.mark.asyncio
    async def test_extraction_engine_not_called_without_schema(self, tenant, worker):
        """No schema -- AdaptiveSelector's existing autonomous-fallback path,
        extraction-engine is never invoked even if configured."""
        worker._extraction_engine = AsyncMock()
        result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            duration_ms=10,
            html="<html><body>x</body></html>",
        )
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-extract-noschema", request)

        worker._extraction_engine.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_extraction_when_no_html(self, tenant, worker):
        result = FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=10)
        worker._fetch_url = AsyncMock(return_value=result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-extract-nohtml", request)

        assert response.results is not None
        assert response.results[0].extracted is None
