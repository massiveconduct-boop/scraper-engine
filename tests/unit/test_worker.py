# tests/unit/test_worker.py
"""Worker state machine tests — escalation logic with mocks."""

import asyncio
import contextlib
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


def make_politeness_mock():
    """AsyncMock PolitenessController with a usable held_slot().

    held_slot is an async context manager, which a bare AsyncMock attribute
    cannot stand in for (`async with` on a coroutine fails), and it is what
    releases the slot — so the stub delegates to the mock's own release_slot
    and existing `release_slot.assert_awaited*` expectations keep working.
    """
    pc = AsyncMock()
    pc.acquire_slot.return_value = "slot-1"
    pc.release_slot.return_value = None
    pc.refresh_slot.return_value = True
    # Round 63 — returns the milliseconds it waited, not None.
    pc.wait_if_needed.return_value = 0

    @contextlib.asynccontextmanager
    async def _held_slot(domain, tenant_id, worker_id):
        try:
            yield
        finally:
            await pc.release_slot(domain, tenant_id, worker_id)

    pc.held_slot = _held_slot
    return pc


def make_redis_mock():
    """AsyncMock RedisClient whose .raw answers the level-memory reads.

    Round 63 — orchestrator/level_memory.py reads through redis.raw. Left as
    a bare AsyncMock the hint read returns a Mock that int() rejects, which
    LevelMemory swallows, so the tests would pass for the wrong reason and
    log a warning per URL. These values are the real "no hint recorded yet"
    answers: start at the bottom of the ladder, as before round 63.
    """
    redis = AsyncMock()
    redis.raw.get.return_value = None
    redis.raw.incr.return_value = 1
    return redis


@pytest.fixture
def worker():
    redis = make_redis_mock()
    cb = AsyncMock()
    cb.allow_request.return_value = True
    cb.record_success.return_value = None
    cb.record_failure.return_value = None
    pc = make_politeness_mock()
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
    async def test_404_escalates_through_all_levels_then_dead_letters(self, tenant, worker):
        """Round 45 — a 404 is no longer treated as an immediate, definitive
        failure (round 43 did that; wrong — see
        ChallengeDetector.CHALLENGE_STATUS_CODES's round-45 comment). It now
        escalates through every level like any other block status, and only
        once the FINAL level's own result still looks blocked does it get
        downgraded to a real failure and dead-lettered — proving both the
        escalation (3 attempts, not 1) and the eventual DLQ."""
        still_404 = FetchResult(
            url="http://example.com/maybe-blocked",
            success=True,
            http_status=404,
            html="<html>not found</html>",
            level_used=1,
            duration_ms=5,
        )
        worker._fetch_url = AsyncMock(return_value=still_404)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com/maybe-blocked")])

        response = await worker.process_job(tenant, "job-404", request)
        assert response.status == JobStatus.FAILED
        assert worker._fetch_url.await_count == 3  # escalated through L1, L2, L3
        worker._dlq.enqueue.assert_called_once()
        dlq_call = worker._dlq.enqueue.await_args
        assert dlq_call.args[3] == FailureCategory.DETECTION_BLOCK
        # Only the final level's still-blocked result is downgraded and
        # penalized — L1/L2 looked like real navigations at the time
        # (record_success fires whenever a fetcher reports success=True;
        # only the challenge-detector check afterward decides whether to
        # trust that), same pre-existing shape as 403/429/5xx escalation.
        assert worker._circuit_breaker.record_failure.await_count == 1

    @pytest.mark.asyncio
    async def test_dlq_eligible_failure_records_circuit_failure(self, tenant, worker):
        """A real proxy-pool failure must still penalize the domain's
        circuit breaker, same as before."""
        exhausted = FetchResult(
            url="http://example.com/",
            success=False,
            level_used=3,
            duration_ms=5,
            failure_category=FailureCategory.PROXY_EXHAUSTED,
            error_message="Proxy pool exhausted",
        )
        worker._fetch_url = AsyncMock(return_value=exhausted)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com/")])

        await worker.process_job(tenant, "job-exhausted", request)
        worker._circuit_breaker.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_extract_domain(self, worker):
        assert worker._extract_domain("http://example.com/path") == "example.com"
        assert worker._extract_domain("https://sub.dom.com:8080/x") == "sub.dom.com"

    @pytest.mark.asyncio
    async def test_politeness_slot_busy_retries_same_level_until_available(
        self, tenant, worker, monkeypatch
    ):
        """acquire_slot returning None means no free slot right now — round
        61: the slot pool is shared across all 3 levels, so a busy slot
        means "wait for a concurrent sibling to finish," not "this level
        failed." Retries the SAME level until a slot frees up, never
        advances to the next level just because the first attempt was busy."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr("scraper_engine.orchestrator.worker.asyncio.sleep", sleep_mock)
        worker._politeness.acquire_slot = AsyncMock(side_effect=[None, "worker-2"])
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=1, duration_ms=10
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-slot-busy", request)

        sleep_mock.assert_awaited_once_with(worker._config.politeness.slot_retry_interval_seconds)
        assert worker._fetch_url.await_count == 1
        # retried level 1 until the slot freed — never advanced to level 2
        assert worker._fetch_url.await_args.args[2] == 1
        worker._politeness.release_slot.assert_awaited_once()
        assert response.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_politeness_slot_timeout_is_terminal_not_a_level_advance(self, tenant, worker):
        """Round 63 — running out the slot budget used to `continue` to the
        next level. A busy slot says nothing about the current level, so that
        walked the whole ladder without a single fetch and then DLQ'd the URL
        as PROXY_EXHAUSTED. Contention is now terminal at the level it
        happens on, and is reported as itself."""
        worker._config.politeness.slot_wait_timeout_seconds = 0.0
        worker._politeness.acquire_slot = AsyncMock(return_value=None)
        worker._fetch_url = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-slot-timeout", request)

        assert response.status == JobStatus.FAILED
        worker._fetch_url.assert_not_awaited()
        worker._politeness.release_slot.assert_not_awaited()
        # One DLQ entry from level 1, not one per level.
        assert worker._dlq.enqueue.await_count == 1
        dlq_call = worker._dlq.enqueue.await_args
        assert dlq_call.args[3] == FailureCategory.POLITENESS_TIMEOUT
        assert dlq_call.args[5] == 1
        assert "No politeness slot for example.com" in dlq_call.args[4]

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
        none of the individual level attempts produced one worth keeping.

        Round 42 — the synthesized result now carries the REAL last
        attempt's category/message (NETWORK_TIMEOUT/"timed out" here, since
        every level failed the same way in this test) instead of a
        hardcoded PROXY_EXHAUSTED/"All fetch levels exhausted" — that
        fabricated label was live-caught masking every kind of terminal
        failure (see worker.py's for/else comment)."""
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
        assert exhausted.failure_category == FailureCategory.NETWORK_TIMEOUT
        assert exhausted.error_message == "timed out"

    @pytest.mark.asyncio
    async def test_process_job_exhausted_levels_falls_back_when_no_attempt_made(
        self, tenant, worker, monkeypatch
    ):
        """Round 42 edge case — every level declined to run, so `_fetch_url`
        was never called and there is no real result to report.

        Round 63 changed what can reach this branch: politeness contention is
        now terminal at its own level, so the remaining way to attempt
        nothing is a circuit that is open under free_first (L1 has no gateway
        path and is skipped) with the gateway then unavailable for L2/L3."""
        sleep_mock = AsyncMock()
        monkeypatch.setattr("scraper_engine.orchestrator.worker.asyncio.sleep", sleep_mock)
        worker._circuit_breaker.allow_request = AsyncMock(return_value=False)
        monkeypatch.setattr(
            type(worker), "_gateway_fallback_eligible", property(lambda self: True)
        )
        worker._fetch_url = AsyncMock(return_value=None)
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-no-attempt", request, on_result=on_result)

        assert response.status == JobStatus.FAILED
        on_result.assert_awaited_once()
        exhausted = on_result.await_args.args[0]
        assert exhausted.failure_category == FailureCategory.PROXY_EXHAUSTED
        assert exhausted.error_message == (
            "All fetch levels exhausted without a single attempt"
        )

    @pytest.mark.asyncio
    async def test_process_job_unexpected_crash_in_one_url_does_not_abort_batch(
        self, tenant, worker, monkeypatch
    ):
        """Round 42 — live-caught: a RecursionError inside markdownify()
        against one real, deeply-nested article page propagated all the way
        up through this method and crashed the ENTIRE job, abandoning every
        other queued URL even though 4 earlier URLs had already genuinely
        succeeded. Reproduced generically here (any unexpected exception
        during post-fetch processing, not specifically RecursionError —
        markdown_fallback.py has its own dedicated regression test for the
        RecursionError case) to prove the containment is general, not a
        markdown-specific patch."""
        import scraper_engine.services.markdown_fallback as markdown_fallback_module

        # Raises only on the first call (url 1) — url 2 must still convert
        # normally, proving the crash didn't corrupt shared state.
        flaky = MagicMock(side_effect=[RuntimeError("simulated post-fetch crash"), "ok markdown"])
        monkeypatch.setattr(markdown_fallback_module, "html_to_markdown", flaky)

        success_result = FetchResult(
            url="unused", success=True, level_used=1, duration_ms=5, html="<p>content</p>"
        )
        worker._fetch_url = AsyncMock(return_value=success_result)
        on_result = AsyncMock()
        request = ScrapeRequest(
            urls=[HttpUrl("http://crashes.example.com"), HttpUrl("http://fine.example.com")]
        )

        response = await worker.process_job(
            tenant, "job-crash-contained", request, on_result=on_result
        )

        # Both URLs were attempted — the crash on the first did not stop the second.
        assert worker._fetch_url.await_count == 2
        assert on_result.await_count == 2
        crashed, fine = (c.args[0] for c in on_result.await_args_list)
        assert crashed.success is False
        assert crashed.failure_category == FailureCategory.PARSE_ERROR
        assert "simulated post-fetch crash" in (crashed.error_message or "")
        assert fine.success is True
        # any_success=True (url 2) -> COMPLETED, matches existing partial-failure contract.
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


class TestGatewayFallbackOnFailure:
    """Round 49 — free_first previously only fell back to the paid gateway
    on ProxyPoolExhaustedError (total free-pool exhaustion). It did nothing
    for a domain whose circuit is open (rejected before any proxy lease is
    even attempted) or a result that's still blocked after the final
    level's own real render — the two failure modes a real consuming
    service (research_agent) actually reported hitting. These tests cover
    the new force_gateway primitive and the two process_job branches built
    on it."""

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
    async def test_force_gateway_skips_free_pool_and_tags_proxy_source(
        self, tenant, worker, monkeypatch
    ):
        """force_gateway=True must bypass the strategy branch entirely —
        it's meaningful even when dataimpulse.strategy is still free_only,
        since the caller (process_job) has already made the fallback
        decision; _fetch_with_proxy shouldn't re-derive it."""
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock()
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

        result = await worker._fetch_url(tenant, "http://example.com", 2, force_gateway=True)

        assert result is not None
        assert result.proxy_source == "paid_gateway"
        pm_instance.get_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normal_pool_fetch_tags_proxy_source_pool(self, tenant, worker, monkeypatch):
        pool_proxy = MagicMock(ip="1.2.3.4", port=8080, source="pool")
        lease = ProxyLease(proxy=pool_proxy, tenant_id=tenant)
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(return_value=lease)
        pm_instance.mark_success = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        expected = FetchResult(url="http://example.com", success=True, level_used=2, duration_ms=5)
        fake_fetcher = MagicMock()
        fake_fetcher.fetch = AsyncMock(return_value=expected)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=fake_fetcher),
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result is not None
        assert result.proxy_source == "pool"

    @pytest.mark.asyncio
    async def test_force_gateway_raises_when_gateway_misconfigured(
        self, tenant, worker, monkeypatch
    ):
        """Same defense-in-depth as the existing paid_only/free_first
        misconfiguration tests above — force_gateway=True must never
        silently fall back to the free pool if the gateway turns out not
        to be configured after all."""
        monkeypatch.setattr(
            "scraper_engine.proxy.paid_gateway.build_gateway_proxy", MagicMock(return_value=None)
        )
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=MagicMock())
        )

        with pytest.raises(RuntimeError, match="force_gateway"):
            await worker._fetch_url(tenant, "http://example.com", 2, force_gateway=True)

    @pytest.mark.asyncio
    async def test_direct_fetcher_detection_block_at_final_level_retries_via_gateway(
        self, tenant, worker
    ):
        """The real-world case live-verifying this round surfaced: a clean
        403 (fetcher/_failure.py::classify_http_status) makes the FETCHER
        itself report success=False, failure_category=DETECTION_BLOCK
        directly — a different shape than round 45's "success=True but
        content still looks blocked" case. The original version of this
        retry only checked inside the `if result.success:` branch and never
        fired for this shape at all. Real crunchbase.com/organization/
        flutterwave fails exactly this way."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        direct_block = FetchResult(
            url="http://example.com",
            success=False,
            level_used=3,
            duration_ms=5,
            http_status=403,
            failure_category=FailureCategory.DETECTION_BLOCK,
            error_message="blocked",
            proxy_source="pool",
        )
        rescued = FetchResult(
            url="http://example.com",
            success=True,
            level_used=3,
            duration_ms=5,
            http_status=200,
            html="<html>real content</html>",
            proxy_source="paid_gateway",
        )
        # Round 64 — the gateway retry fires at the level that was blocked
        # (here L1), not only at the final level, so the rescue comes on the
        # second call instead of after climbing the whole ladder.
        worker._fetch_url = AsyncMock(side_effect=[direct_block, rescued])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-direct-block-rescue", request)

        assert response.status == JobStatus.COMPLETED
        assert response.results is not None
        assert response.results[0].success is True
        assert response.results[0].proxy_source == "paid_gateway"
        last_call = worker._fetch_url.await_args_list[-1]
        assert last_call.kwargs["force_gateway"] is True
        # Round 64 — the retried attempt and the retry's own time are both
        # visible: before, the result showed only `proxy_source: paid_gateway`
        # and a total_ms nothing else accounted for.
        assert response.results[0].escalations == [
            {
                "level": 1,
                "reason": "failure:detection_block",
                "http_status": 403,
                "engine": None,
                "proxy_source": "pool",
            }
        ]
        assert "level_1_gateway_retry_ms" in response.results[0].timings
        assert worker._fetch_url.await_count == 2

    @pytest.mark.asyncio
    async def test_direct_fetcher_detection_block_gateway_retry_also_fails(self, tenant, worker):
        """Same shape, but the gateway retry doesn't rescue it either —
        proves the DLQ still gets the real category via the for/else
        exhausted-levels branch, with proxy_source carried through instead
        of silently dropped."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        direct_block_pool = FetchResult(
            url="http://example.com",
            success=False,
            level_used=3,
            duration_ms=5,
            http_status=403,
            failure_category=FailureCategory.DETECTION_BLOCK,
            error_message="blocked",
            proxy_source="pool",
        )
        direct_block_gateway = FetchResult(
            url="http://example.com",
            success=False,
            level_used=3,
            duration_ms=5,
            http_status=403,
            failure_category=FailureCategory.DETECTION_BLOCK,
            error_message="blocked",
            proxy_source="paid_gateway",
        )
        worker._fetch_url = AsyncMock(
            # pool attempt + one gateway retry, at each of the three levels
            side_effect=[direct_block_pool, direct_block_gateway] * 3
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-direct-block-still-fails", request)

        assert response.status == JobStatus.FAILED
        assert response.results is not None
        assert response.results[0].success is False
        assert response.results[0].failure_category == FailureCategory.DETECTION_BLOCK
        # proxy_source carried forward from last_level_result into the
        # for/else branch's constructed exhausted_result, not dropped.
        assert response.results[0].proxy_source == "paid_gateway"
        # exactly one gateway retry per level — the gateway-sourced failure
        # must not trigger a second one.
        assert worker._fetch_url.await_count == 6
        assert [c.kwargs.get("force_gateway") for c in worker._fetch_url.await_args_list] == [
            False,
            True,
        ] * 3

    @pytest.mark.asyncio
    async def test_still_blocked_gateway_retry_itself_fails(self, tenant, worker):
        """The gateway retry can also come back as a real fetch failure
        (not just still-content-blocked) — must still be treated as
        "still blocked" and fall through to the normal downgrade, not
        crash or silently treat a failed result as success."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        blocked_result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=3,
            duration_ms=5,
            html="<html>please verify you are a human</html>",
            http_status=200,
            proxy_source="pool",
        )
        gateway_failure = FetchResult(
            url="http://example.com",
            success=False,
            level_used=3,
            duration_ms=5,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
            error_message="gateway timed out",
            proxy_source="paid_gateway",
        )
        worker._fetch_url = AsyncMock(
            side_effect=[blocked_result, blocked_result, blocked_result, gateway_failure]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-gateway-retry-fails", request)

        assert response.status == JobStatus.FAILED
        assert response.results is not None
        assert response.results[0].success is False

    @pytest.mark.asyncio
    async def test_circuit_open_free_first_routes_level2_through_gateway(self, tenant, worker):
        """Circuit open at level 1 (no gateway path there) skips straight
        to level 2, where free_first + an open circuit forces the fetch
        through the gateway instead of an immediate CIRCUIT_OPEN DLQ."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        worker._circuit_breaker.allow_request.return_value = False
        gateway_result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=5,
            proxy_source="paid_gateway",
        )
        worker._fetch_url = AsyncMock(return_value=gateway_result)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-circuit-gateway", request)

        assert response.status == JobStatus.COMPLETED
        assert response.results is not None
        assert response.results[0].proxy_source == "paid_gateway"
        # level 1 never actually calls _fetch_url (no gateway path to force
        # it through) — the only real attempt is level 2, forced.
        worker._fetch_url.assert_awaited_once_with(
            tenant, "http://example.com/", 2, None, force_gateway=True
        )

    @pytest.mark.asyncio
    async def test_circuit_open_free_only_keeps_immediate_dlq(self, tenant, worker):
        """Regression guard — the default strategy (free_only) must keep
        the exact prior behavior: immediate CIRCUIT_OPEN DLQ, no fetch
        attempted at all, not even at level 2."""
        worker._circuit_breaker.allow_request.return_value = False
        worker._fetch_url = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-circuit-no-gateway", request)

        assert response.status == JobStatus.FAILED
        worker._fetch_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_blocked_final_level_retries_via_gateway_and_rescues(self, tenant, worker):
        """Final-level result still looks blocked (challenge page) — under
        free_first, one gateway retry is attempted before conceding; here
        the gateway attempt comes back clean and rescues the URL."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        blocked_result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=3,
            duration_ms=5,
            html="<html>please verify you are a human</html>",
            http_status=200,
            proxy_source="pool",
        )
        rescued_result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=3,
            duration_ms=5,
            html="<html>real content</html>",
            http_status=200,
            proxy_source="paid_gateway",
        )
        # Level 1, level 2 both non-final and blocked -> escalate. Level 3
        # (final) is attempted twice: the normal initial call (still
        # blocked), then the forced-gateway retry (rescued).
        worker._fetch_url = AsyncMock(
            side_effect=[blocked_result, blocked_result, blocked_result, rescued_result]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-rescue", request)

        assert response.status == JobStatus.COMPLETED
        assert response.results is not None
        assert response.results[0].success is True
        assert response.results[0].html == "<html>real content</html>"
        # last call is the forced-gateway retry at the final level
        last_call = worker._fetch_url.await_args_list[-1]
        assert last_call.kwargs["force_gateway"] is True

    @pytest.mark.asyncio
    async def test_still_blocked_final_level_gateway_retry_also_fails(self, tenant, worker):
        """Gateway retry doesn't help either — falls through to the normal
        DETECTION_BLOCK downgrade, same as before this round, just one
        extra attempt first."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        blocked_result = FetchResult(
            url="http://example.com",
            success=True,
            level_used=3,
            duration_ms=5,
            html="<html>please verify you are a human</html>",
            http_status=200,
            proxy_source="pool",
        )
        still_blocked_via_gateway = FetchResult(
            url="http://example.com",
            success=True,
            level_used=3,
            duration_ms=5,
            html="<html>please verify you are a human</html>",
            http_status=200,
            proxy_source="paid_gateway",
        )
        worker._fetch_url = AsyncMock(
            side_effect=[blocked_result, still_blocked_via_gateway] * 3
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-still-blocked", request)

        assert response.status == JobStatus.FAILED
        assert response.results is not None
        assert response.results[0].success is False
        assert response.results[0].failure_category == FailureCategory.DETECTION_BLOCK

    @pytest.mark.asyncio
    async def test_still_blocked_no_double_gateway_retry(self, tenant, worker):
        """A result that already came from the gateway (e.g. this level was
        reached via the circuit-open fallback) must not be retried a
        second time even if it's still blocked — one extra attempt per URL
        per level, never a second."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="free_first")
        worker._circuit_breaker.allow_request.return_value = False
        already_gateway_blocked = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=5,
            html="<html>please verify you are a human</html>",
            http_status=200,
            proxy_source="paid_gateway",
        )
        worker._fetch_url = AsyncMock(return_value=already_gateway_blocked)
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-no-double-retry", request)

        # One call per level attempted (level 1 skipped, so levels 2 and 3
        # each call once, forced by the open circuit) — never a second call
        # at the same level for the still-blocked retry.
        assert worker._fetch_url.await_count == 2


class TestConcurrentUrlProcessing:
    """Round 49 — process_job's per-URL loop was strictly sequential
    (root-caused round 45 as the reason large batches took far longer than
    the shared browser/proxy budget required — a real research_agent
    complaint, `scraper_engine_job_timeout`). Now dispatched concurrently,
    bounded by config.politeness.max_concurrent_urls_per_job."""

    @pytest.mark.asyncio
    async def test_urls_actually_run_concurrently(self, tenant, worker):
        """Proves real overlap, not just that the job still works — tracks
        how many _fetch_url calls are simultaneously in flight."""
        active = 0
        peak = 0

        async def fake_fetch_url(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return FetchResult(url=str(args[1]), success=True, level_used=1, duration_ms=1)

        worker._fetch_url = AsyncMock(side_effect=fake_fetch_url)
        request = ScrapeRequest(urls=[HttpUrl(f"http://example{i}.com") for i in range(4)])

        response = await worker.process_job(tenant, "job-concurrent", request)

        assert response.status == JobStatus.COMPLETED
        assert peak > 1

    @pytest.mark.asyncio
    async def test_result_order_matches_input_order_despite_completion_order(self, tenant, worker):
        """First URL is the slow one — if ordering were completion-order
        instead of input-order, it would land last in `results`."""

        async def fake_fetch_url(_tenant, url, _level, _overrides, **_kwargs):
            delay = 0.03 if "slow" in str(url) else 0.0
            await asyncio.sleep(delay)
            return FetchResult(url=str(url), success=True, level_used=1, duration_ms=1)

        worker._fetch_url = AsyncMock(side_effect=fake_fetch_url)
        request = ScrapeRequest(
            urls=[HttpUrl("http://slow.example.com"), HttpUrl("http://fast.example.com")]
        )

        response = await worker.process_job(tenant, "job-order", request)

        assert response.results is not None
        assert response.results[0].url == "http://slow.example.com/"
        assert response.results[1].url == "http://fast.example.com/"

    @pytest.mark.asyncio
    async def test_cancellation_mid_job_skips_remaining_dispatch_without_extra_db_calls(
        self, tenant, worker
    ):
        """Once any task observes cancellation, every other queued task must
        take the in-memory fast path (cancelled_state check) rather than
        each re-querying the DB — proves the fast path is actually reached,
        not just that cancellation eventually works."""
        worker._pg.fetchrow = AsyncMock(return_value={"status": JobStatus.CANCELLED.value})
        worker._fetch_url = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl(f"http://example{i}.com") for i in range(5)])

        response = await worker.process_job(tenant, "job-cancelled", request)

        assert response.status == JobStatus.CANCELLED
        worker._fetch_url.assert_not_awaited()
        # Only ONE real DB round-trip for the whole job — every task after
        # the first that observes cancellation took the in-memory fast
        # path (cancelled_state["value"]) instead of calling fetchrow again.
        assert worker._pg.fetchrow.await_count == 1


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


class TestGatewayExitIpRotation:
    """Round 62 — _fetch_with_proxy rotates the paid gateway's EXIT IP when
    the target blocks, instead of accepting the first block as final.

    Live evidence (ops/research/itel-30000mah-jumia, 20 Sep 2026): the first
    Jumia catalog fetch through the gateway returned 200; every later one
    returned a Cloudflare 403 at L1, L2 and L3, and the engine gave up. Two
    separate defects combined to produce that. DETECTION_BLOCK was not a
    retryable category, so no second attempt was made at all; and
    build_gateway_proxy() took no arguments, so even the attempts that DID
    happen re-presented the identity that had just been flagged.
    """

    @staticmethod
    def _fetcher_returning(*results):
        """A fetcher whose successive fetch() calls return `results` in order."""
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=list(results))
        return fetcher

    @staticmethod
    def _blocked(level=2):
        return FetchResult(
            url="http://example.com",
            success=False,
            level_used=level,
            duration_ms=5,
            failure_category=FailureCategory.DETECTION_BLOCK,
            error_message="403",
        )

    @staticmethod
    def _ok(level=2):
        return FetchResult(
            url="http://example.com", success=True, level_used=level, duration_ms=5, html="<html/>"
        )

    def _wire(
        self,
        worker,
        monkeypatch,
        *results,
        strategy="paid_only",
        rotations=2,
        country="",
        asn=None,
    ):
        """paid_only + a scripted fetcher. Returns (fetcher, usernames-seen)."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(
            enabled=True,
            strategy=strategy,
            country=country,
            asn=asn,
            rotate_on_block_retries=rotations,
        )
        pm_instance = MagicMock()
        # paid_only never reaches the free pool; a bare AsyncMock here would
        # silently satisfy a call that must not happen, so assert it doesn't.
        pm_instance.get_proxy = AsyncMock()
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )

        def _build(*, country=None, session_id=None, asn=None):
            return Proxy(
                id=-1,
                ip="gw.dataimpulse.com",
                port=823,
                protocol=ProxyProtocol.HTTP,
                username=f"user123__cr.{country};asn.{asn};sessid.{session_id}",
                password="pass456",
                source="paid_gateway",
            )

        monkeypatch.setattr("scraper_engine.proxy.paid_gateway.build_gateway_proxy", _build)
        fetcher = self._fetcher_returning(*results)
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher", MagicMock(return_value=fetcher)
        )
        self._pm = pm_instance
        return fetcher

    @staticmethod
    def _usernames(fetcher):
        return [c.kwargs["proxy"].username for c in fetcher.fetch.await_args_list]

    @pytest.mark.asyncio
    async def test_block_rotates_to_a_new_exit_ip_and_succeeds(self, tenant, worker, monkeypatch):
        fetcher = self._wire(worker, monkeypatch, self._blocked(), self._ok())

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert result.success is True
        assert fetcher.fetch.await_count == 2
        first, second = self._usernames(fetcher)
        # Same account, DIFFERENT sticky-session label == different exit IP.
        assert first != second
        assert first.startswith("user123__") and second.startswith("user123__")
        # Rotation happens entirely within the gateway — it must never quietly
        # become a free-pool lease, which would be a different proxy source
        # with different (scored, DB-backed) semantics.
        self._pm.get_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_budget_is_bounded_then_the_block_is_returned(
        self, tenant, worker, monkeypatch
    ):
        fetcher = self._wire(worker, monkeypatch, *[self._blocked() for _ in range(4)], rotations=2)

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        # 1 initial attempt + 2 rotations. Each one is a full paid browser
        # render, so the ceiling is the config value, not "retry until done".
        assert fetcher.fetch.await_count == 3
        assert len(set(self._usernames(fetcher))) == 3
        assert result.success is False
        assert result.failure_category == FailureCategory.DETECTION_BLOCK

    @pytest.mark.asyncio
    async def test_rotation_disabled_by_config_accepts_the_first_block(
        self, tenant, worker, monkeypatch
    ):
        """rotate_on_block_retries=0 restores the exact pre-round-62 behavior."""
        fetcher = self._wire(worker, monkeypatch, self._blocked(), self._ok(), rotations=0)

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert fetcher.fetch.await_count == 1
        assert result.success is False

    @pytest.mark.asyncio
    async def test_success_shaped_challenge_page_also_rotates(self, tenant, worker, monkeypatch):
        """The L3 shape. level_3.py deliberately returns success=True with the
        real http_status for a Cloudflare interstitial and defers the verdict
        to the worker. Checking result.success first would therefore skip
        rotation for the single most common block there is."""
        challenge = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            duration_ms=5,
            http_status=403,
            html="<html>Just a moment...</html>",
        )
        fetcher = self._wire(worker, monkeypatch, challenge, self._ok())
        worker._challenge_detector.is_challenge_page = MagicMock(side_effect=[True, False])

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert fetcher.fetch.await_count == 2
        assert result.success is True
        assert result.html == "<html/>"

    @pytest.mark.asyncio
    async def test_free_pool_block_does_not_rotate(self, tenant, worker, monkeypatch):
        """Only the gateway can be asked for a different exit IP. A free-pool
        block escalates to the next level (and, under free_first, through
        process_job's gateway fallback) rather than burning a second render
        here — DETECTION_BLOCK stays out of _PROXY_RETRYABLE_CATEGORIES."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(
            enabled=True, strategy="free_first", rotate_on_block_retries=2
        )
        pool_proxy = Proxy(
            id=7, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP, source="pool"
        )
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(
            return_value=ProxyLease(proxy=pool_proxy, tenant_id=tenant)
        )
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        fetcher = self._fetcher_returning(self._blocked(), self._ok())
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher", MagicMock(return_value=fetcher)
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert fetcher.fetch.await_count == 1
        assert result.success is False
        pm_instance.mark_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_configured_country_reaches_the_gateway_username(
        self, tenant, worker, monkeypatch
    ):
        fetcher = self._wire(worker, monkeypatch, self._ok(), country="ng")

        await worker._fetch_url(tenant, "http://example.com", 2)

        assert self._usernames(fetcher)[0].startswith("user123__cr.ng;asn.None;sessid.")

    @pytest.mark.asyncio
    async def test_configured_asn_pin_reaches_the_gateway_username(
        self, tenant, worker, monkeypatch
    ):
        """The measured fix for the Jumia block. One ASN made up most of
        DataImpulse's Nigerian pool and Cloudflare blocked all of it (1 clean
        fetch in 12); pinning a clean ASN gave 12 of 12. See
        proxy/paid_gateway.py's module docstring for the raw numbers."""
        fetcher = self._wire(worker, monkeypatch, self._ok(), country="ng", asn=29465)

        await worker._fetch_url(tenant, "http://example.com", 2)

        assert self._usernames(fetcher)[0].startswith("user123__cr.ng;asn.29465;sessid.")

    @pytest.mark.asyncio
    async def test_asn_pin_still_rotates_the_session_on_a_block(self, tenant, worker, monkeypatch):
        """Pinning an ASN narrows the pool; it must not freeze the exit IP.
        Rotation still has to hand out a new sessid within that ASN."""
        fetcher = self._wire(
            worker, monkeypatch, self._blocked(), self._ok(), country="ng", asn=29465
        )

        await worker._fetch_url(tenant, "http://example.com", 2)

        first, second = self._usernames(fetcher)
        assert first != second
        assert all(u.startswith("user123__cr.ng;asn.29465;") for u in (first, second))

    @pytest.mark.asyncio
    async def test_empty_country_is_not_sent_as_a_parameter(self, tenant, worker, monkeypatch):
        """base.yaml's default is "", which must mean "no country pin" — an
        empty `cr.` parameter is a malformed login the gateway rejects with
        407 NO_USER, not a no-op."""
        from scraper_engine.config.schema import DataImpulseConfig

        worker._config.dataimpulse = DataImpulseConfig(enabled=True, strategy="paid_only")
        seen = {}

        def _build(*, country=None, session_id=None, asn=None):
            seen["country"] = country
            seen["asn"] = asn
            return Proxy(
                id=-1,
                ip="gw.dataimpulse.com",
                port=823,
                protocol=ProxyProtocol.HTTP,
                username="user123",
                password="pass456",
                source="paid_gateway",
            )

        monkeypatch.setattr("scraper_engine.proxy.paid_gateway.build_gateway_proxy", _build)
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=MagicMock())
        )
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher",
            MagicMock(return_value=self._fetcher_returning(self._ok())),
        )

        await worker._fetch_url(tenant, "http://example.com", 2)

        assert seen["country"] is None
        # Same reasoning for the ASN pin: unset must reach the builder as
        # None, since sending `asn.None` would both cost double and 407.
        assert seen["asn"] is None

    @pytest.mark.asyncio
    async def test_rotation_is_off_when_dataimpulse_is_disabled(self, tenant, worker, monkeypatch):
        """free_only never leases the gateway, so there is nothing to rotate;
        the budget must read 0 rather than inheriting the schema default."""
        pool_proxy = Proxy(
            id=7, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP, source="pool"
        )
        pm_instance = MagicMock()
        pm_instance.get_proxy = AsyncMock(
            return_value=ProxyLease(proxy=pool_proxy, tenant_id=tenant)
        )
        pm_instance.mark_success = AsyncMock()
        pm_instance.mark_failure = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.proxy.manager.ProxyManager", MagicMock(return_value=pm_instance)
        )
        fetcher = self._fetcher_returning(self._blocked(), self._ok())
        monkeypatch.setattr(
            "scraper_engine.fetcher.factory.build_level2_fetcher", MagicMock(return_value=fetcher)
        )

        result = await worker._fetch_url(tenant, "http://example.com", 2)

        assert fetcher.fetch.await_count == 1
        assert result.success is False


class TestLevelMemoryWiring:
    """Round 63 — the ladder starts where the domain last succeeded.

    An external consumer measured 169.2s in PROCESSING for a Jumia product
    page whose real fetch took 27.6s, with every successful fetch landing at
    L3. The missing ~140s was L1 and L2 failing again, once per URL, forever,
    because nothing carried "this domain needs L3" from one job to the next.
    """

    @pytest.mark.asyncio
    async def test_hint_skips_the_levels_that_already_failed(self, tenant, worker):
        worker._redis.raw.get.return_value = "3"
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=3, duration_ms=10
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-hint", request)

        assert worker._fetch_url.await_count == 1
        assert worker._fetch_url.await_args.args[2] == 3

    @pytest.mark.asyncio
    async def test_the_winning_level_is_recorded(self, tenant, worker):
        worker._fetch_url = AsyncMock(
            side_effect=[
                FetchResult(
                    url="http://example.com",
                    success=False,
                    level_used=1,
                    duration_ms=5,
                    failure_category=FailureCategory.NETWORK_TIMEOUT,
                ),
                FetchResult(
                    url="http://example.com", success=True, level_used=2, duration_ms=10
                ),
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-record", request)

        worker._redis.raw.set.assert_awaited_once()
        assert worker._redis.raw.set.await_args.args[1] == "2"

    @pytest.mark.asyncio
    async def test_a_level_that_only_looks_successful_is_not_recorded(self, tenant, worker):
        """A non-final level returning a challenge page escalates rather than
        counting as a success, so it must not teach the memory either —
        otherwise the hint would pin the domain to the level that is reliably
        getting blocked."""
        challenge = "<html><body>cf-challenge-running</body></html>"
        worker._fetch_url = AsyncMock(
            side_effect=[
                FetchResult(
                    url="http://example.com",
                    success=True,
                    level_used=1,
                    duration_ms=5,
                    html=challenge,
                ),
                FetchResult(
                    url="http://example.com",
                    success=True,
                    level_used=2,
                    duration_ms=10,
                    html="<html><body>" + "<p>Real text. </p>" * 30 + "</body></html>",
                ),
            ]
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-challenge-no-record", request)

        worker._redis.raw.set.assert_awaited_once()
        assert worker._redis.raw.set.await_args.args[1] == "2"


class TestLadderNarrowing:
    @pytest.mark.asyncio
    async def test_min_level_starts_the_ladder_higher(self, tenant, worker):
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=2, duration_ms=10
            )
        )
        request = ScrapeRequest(
            urls=[HttpUrl("http://example.com")], config_overrides=ConfigOverrides(min_level=2)
        )

        await worker.process_job(tenant, "job-min-level", request)

        assert worker._fetch_url.await_args.args[2] == 2

    @pytest.mark.asyncio
    async def test_max_level_truncates_the_ladder(self, tenant, worker):
        """max_level=1 means "never spend a browser render on this" — the
        URL must fail out after L1 rather than escalating."""
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com",
                success=False,
                level_used=1,
                duration_ms=5,
                failure_category=FailureCategory.NETWORK_TIMEOUT,
            )
        )
        request = ScrapeRequest(
            urls=[HttpUrl("http://example.com")], config_overrides=ConfigOverrides(max_level=1)
        )

        response = await worker.process_job(tenant, "job-max-level", request)

        assert worker._fetch_url.await_count == 1
        assert response.status == JobStatus.FAILED

    def test_min_level_above_max_level_is_rejected_at_validation(self):
        with pytest.raises(ValueError, match="min_level"):
            ConfigOverrides(min_level=3, max_level=2)

    def test_resolve_levels_defaults_to_the_full_ladder(self, worker):
        assert worker._resolve_levels(None) == [1, 2, 3]


class TestPerRequestPoliteness:
    """Round 63 — a caller may trade politeness for throughput, inside
    operator-set bounds. The clamp is server-side because the request is the
    untrusted half of that decision."""

    def test_defaults_come_from_config(self, worker):
        worker._config.politeness.default_concurrency = 2
        worker._config.politeness.default_delay_seconds = 5.0
        assert worker._resolve_politeness(None) == (2, 5.0)

    def test_request_values_are_used_when_within_bounds(self, worker):
        worker._config.politeness.max_request_concurrency = 10
        worker._config.politeness.min_request_delay_seconds = 0.5
        overrides = ConfigOverrides(politeness_concurrency=8, politeness_delay_seconds=1.0)
        assert worker._resolve_politeness(overrides) == (8, 1.0)

    def test_concurrency_is_capped_and_delay_is_floored(self, worker):
        worker._config.politeness.max_request_concurrency = 10
        worker._config.politeness.min_request_delay_seconds = 0.5
        overrides = ConfigOverrides(politeness_concurrency=500, politeness_delay_seconds=0.0)
        assert worker._resolve_politeness(overrides) == (10, 0.5)

    @pytest.mark.asyncio
    async def test_resolved_values_reach_the_controller(self, tenant, worker):
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=1, duration_ms=10
            )
        )
        request = ScrapeRequest(
            urls=[HttpUrl("http://example.com")],
            config_overrides=ConfigOverrides(
                politeness_concurrency=7, politeness_delay_seconds=2.0
            ),
        )

        await worker.process_job(tenant, "job-politeness-override", request)

        assert worker._politeness.acquire_slot.await_args.kwargs["concurrency"] == 7
        assert worker._politeness.wait_if_needed.await_args.kwargs["delay_seconds"] == 2.0


class TestTimings:
    @pytest.mark.asyncio
    async def test_terminal_result_carries_a_phase_breakdown(self, tenant, worker):
        """Round 63 — duration_ms is one number, written by whichever level
        finally returned, so it cannot distinguish "the fetch is slow" from
        "the fetch was fine and everything around it was slow"."""
        worker._politeness.wait_if_needed = AsyncMock(return_value=250)
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=1, duration_ms=10
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-timings", request)

        timings = response.results[0].timings
        assert timings is not None
        assert timings["politeness_wait_ms"] == 250
        assert "level_1_ms" in timings
        assert "slot_wait_ms" in timings
        assert "total_ms" in timings

    @pytest.mark.asyncio
    async def test_a_skipped_level_leaves_no_key(self, tenant, worker):
        """Which levels ran is readable from the dict itself — that is how a
        caller sees the level hint working."""
        worker._redis.raw.get.return_value = "3"
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com", success=True, level_used=3, duration_ms=10
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-timings-skip", request)

        timings = response.results[0].timings
        assert "level_1_ms" not in timings
        assert "level_2_ms" not in timings
        assert "level_3_ms" in timings


class TestEscalationReasons:
    """Round 64 — a live 10-URL Jumia job rejected every L2 result and
    nothing (logs, database, API) could say why. Every rejected level now
    leaves an entry on the terminal result."""

    @pytest.mark.asyncio
    async def test_a_blocked_level_records_the_exact_reason(self, tenant, worker):
        blocked = FetchResult(
            url="http://example.com",
            success=True,
            level_used=1,
            http_status=403,
            html="<html>forbidden</html>",
            duration_ms=10,
        )
        good = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            http_status=200,
            html="<html><body>" + "real content " * 80 + "</body></html>",
            duration_ms=10,
            engine="camoufox",
        )
        worker._fetch_url = AsyncMock(side_effect=[blocked, good])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-escalations", request)

        result = response.results[0]
        assert result.level_used == 2
        assert result.escalations == [
            {
                "level": 1,
                "reason": "status:403",
                "http_status": 403,
                "engine": None,
                "proxy_source": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_a_failed_level_records_its_category(self, tenant, worker):
        failed = FetchResult(
            url="http://example.com",
            success=False,
            level_used=1,
            duration_ms=10,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        )
        good = FetchResult(
            url="http://example.com",
            success=True,
            level_used=2,
            http_status=200,
            html="<html><body>" + "real content " * 80 + "</body></html>",
            duration_ms=10,
        )
        worker._fetch_url = AsyncMock(side_effect=[failed, good])
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-escalations-fail", request)

        assert response.results[0].escalations[0]["reason"] == "failure:network_timeout"

    @pytest.mark.asyncio
    async def test_a_clean_first_level_records_nothing(self, tenant, worker):
        worker._fetch_url = AsyncMock(
            return_value=FetchResult(
                url="http://example.com",
                success=True,
                level_used=1,
                http_status=200,
                html="<html><body>" + "real content " * 80 + "</body></html>",
                duration_ms=10,
            )
        )
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        response = await worker.process_job(tenant, "job-escalations-none", request)

        assert response.results[0].escalations is None


class TestPolitenessTimeoutStreaming:
    @pytest.mark.asyncio
    async def test_slot_timeout_result_is_streamed_to_on_result(self, tenant, worker):
        """A URL that never got a slot still has to reach the caller's
        persist callback — otherwise the job's own result set silently loses
        a URL and progress never reaches 1.0."""
        worker._config.politeness.slot_wait_timeout_seconds = 0.0
        worker._politeness.acquire_slot = AsyncMock(return_value=None)
        on_result = AsyncMock()
        request = ScrapeRequest(urls=[HttpUrl("http://example.com")])

        await worker.process_job(tenant, "job-slot-stream", request, on_result=on_result)

        on_result.assert_awaited_once()
        streamed = on_result.await_args.args[0]
        assert streamed.failure_category == FailureCategory.POLITENESS_TIMEOUT
        assert streamed.timings is not None
