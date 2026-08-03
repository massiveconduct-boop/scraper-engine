# orchestrator/worker.py
"""RQ task definition, execution loop, state machine driver.

The worker dequeues jobs and drives the escalation state machine:
  PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                          ↘ failure → ESCALATING_L2 → ...
                                                                       ↘ DEAD_LETTER
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from scraper_engine.core.models import FailureCategory, FetchResult, JobStatus, JobStatusResponse

if TYPE_CHECKING:
    from scraper_engine.browser.botasaurus_pool import BotasaurusPool
    from scraper_engine.browser.pool import BrowserPool
    from scraper_engine.config.schema import AppConfig
    from scraper_engine.core.models import ConfigOverrides, ScrapeRequest
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.dlq import DeadLetterQueue
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

    from .circuit_breaker import CircuitBreaker
    from .politeness import PolitenessController

LEVELS = [1, 2, 3]

# round 29 — how long a successful scrape_results row is considered a valid
# cache hit before it must be re-scraped. Sliding: a hit refreshes freshness
# by inserting a new row with a fresh extracted_at (see _persist_one_result
# in orchestrator/tasks.py), not a fixed clock from the very first scrape.
# Must stay <= S3Client.SUCCESS_RETENTION_DAYS, otherwise a cache hit could
# reference an html_snapshot_url whose S3 object has already expired.
CACHE_TTL_DAYS = 7


class Worker:
    """RQ worker: dequeues jobs, drives the escalation state machine."""

    def __init__(
        self,
        redis: RedisClient,
        circuit_breaker: CircuitBreaker,
        politeness: PolitenessController,
        dlq: DeadLetterQueue,
        config: AppConfig | None = None,
        pg: PostgresClient | None = None,
        browser_pool: BrowserPool | None = None,
        botasaurus_pool: BotasaurusPool | None = None,
    ) -> None:
        self._redis = redis
        self._circuit_breaker = circuit_breaker
        self._politeness = politeness
        self._dlq = dlq
        self._pg = pg
        # None keeps pre-round-25 behavior (fetchers cold-start their own
        # CamoufoxWrapper). One pool per job process — see orchestrator/tasks.py.
        self._browser_pool = browser_pool
        # None keeps every Botasaurus fetch one-shot. One pool per job process
        # (round 26), same lifetime as browser_pool — see orchestrator/tasks.py.
        self._botasaurus_pool = botasaurus_pool
        # config drives fetcher construction via fetcher/factory.py. Loaded once
        # here (not per-fetch) so production.yaml values are authoritative for
        # every fetch this worker dispatches. Falls back to load_config() so
        # callers that don't pass one still get YAML-driven fetchers, never
        # bare constructor defaults.
        if config is None:
            from scraper_engine.config.loader import load_config

            config = load_config()
        self._config = config
        # One ChallengeDetector for escalation decisions (challenge pages and
        # JS-gated shells) — same single source of truth the fetchers use.
        from scraper_engine.fetcher.challenge_detector import ChallengeDetector

        self._challenge_detector = ChallengeDetector()
        # Build the CAPTCHA solver once (env keys + per-tenant budget on Redis)
        # and thread it into the browser fetchers via the factory. None when no
        # provider key is set — solving stays disabled, fetch still runs. Built
        # here (not per-fetch) for the same reason config is: one authoritative
        # construction site (round 20 — wires services/captcha_solver in).
        from scraper_engine.core.budget import CapSolverBudget
        from scraper_engine.services.captcha_solver import build_captcha_solver

        self._captcha_solver = build_captcha_solver(CapSolverBudget(self._redis, pg=self._pg))
        # Circuit breaker and politeness use raw Redis (not tenant-scoped),
        # so pass the underlying client for system-level key operations
        if hasattr(circuit_breaker, "_redis"):
            pass  # already set by caller
        # Built once here (not per-fetch), same rationale as captcha_solver
        # above. None when neither FIRECRAWL_API_KEY nor FIRECRAWL_BASE_URL
        # is set — markdown conversion is simply skipped (round 29 — moved
        # out of Level1Fetcher so it applies to whichever level succeeds,
        # not just L1).
        from scraper_engine.services.firecrawl_client import build_firecrawl_client

        self._firecrawl = build_firecrawl_client()
        # Built once here, same rationale as firecrawl/captcha_solver above. None
        # when EXTRACTION_ENGINE_BASE_URL isn't set — schema-driven extraction
        # simply falls back to AdaptiveSelector (see process_job below).
        from scraper_engine.services.extraction_engine_client import (
            build_extraction_engine_client,
        )

        self._extraction_engine = build_extraction_engine_client()

    async def process_job(
        self,
        tenant_id: TenantId,
        job_id: str,
        request: ScrapeRequest,
        on_result: Callable[[FetchResult], Awaitable[None]] | None = None,
    ) -> JobStatusResponse:
        """Execute the full escalation state machine for a job.

        on_result (round 29), when given, is awaited once for every URL that
        reaches a terminal outcome (success, a DLQ'd failure, or a cache
        hit) — orchestrator/tasks.py threads this in to persist each result
        to Postgres/S3 as it lands, instead of batching everything until the
        whole job finishes. This is also what makes real per-URL progress
        and mid-job cancellation possible (see _is_cancelled below)."""
        results: list[FetchResult] = []
        errors: list[str] = []
        cancelled = False
        bypass_cache = bool(request.config_overrides and request.config_overrides.bypass_cache)

        for url in request.urls:
            url_str = str(url)

            if await self._is_cancelled(tenant_id, job_id):
                cancelled = True
                break

            if not bypass_cache:
                cached = await self._check_cache(tenant_id, url_str)
                if cached is not None:
                    results.append(cached)
                    if on_result is not None:
                        await on_result(cached)
                    continue

            domain = self._extract_domain(url_str)

            for level in LEVELS:
                if not await self._circuit_breaker.allow_request(domain):
                    circuit_result = FetchResult(
                        url=url_str,
                        success=False,
                        level_used=level,
                        duration_ms=0,
                        failure_category=FailureCategory.CIRCUIT_OPEN,
                        error_message=f"Circuit open for {domain}",
                    )
                    await self._dlq.enqueue(
                        tenant_id,
                        job_id,
                        url_str,
                        FailureCategory.CIRCUIT_OPEN,
                        f"Circuit open for {domain}",
                        level,
                    )
                    errors.append(f"Circuit open for {domain}")
                    results.append(circuit_result)
                    if on_result is not None:
                        await on_result(circuit_result)
                    break

                slot_worker_id = await self._politeness.acquire_slot(domain, tenant_id)
                if slot_worker_id is None:
                    await asyncio.sleep(1)
                    continue

                try:
                    await self._politeness.wait_if_needed(domain, tenant_id)
                    result = await self._fetch_url(
                        tenant_id, url_str, level, request.config_overrides
                    )
                finally:
                    await self._politeness.release_slot(domain, tenant_id, slot_worker_id)

                if result is None:
                    continue

                if result.success:
                    await self._circuit_breaker.record_success(domain)
                    # A JS-gated shell from a non-final level is not real content
                    # — an HTTP-only L1 fetch of a SPA returns 200 with an empty
                    # mount point. Escalate to a browser level that runs JS instead
                    # of caching the shell (round 15 — closes the "200 but
                    # under-rendered" gap). Browser levels render JS so they won't
                    # trip this; the final level accepts whatever it got.
                    if level < LEVELS[-1] and self._challenge_detector.looks_javascript_gated(
                        result.html or ""
                    ):
                        continue
                    if result.html:
                        # FetchResult.extracted was declared on the model and
                        # persisted by orchestrator/tasks.py, but nothing ever
                        # populated it — AdaptiveSelector existed, fully
                        # tested, with zero callers (round 28). Wired here,
                        # once, so it applies uniformly regardless of which
                        # level actually succeeded.
                        from scraper_engine.fetcher.adaptive_selector import AdaptiveSelector

                        schema = (
                            request.config_overrides.extraction_schema
                            if request.config_overrides
                            else None
                        )
                        # extraction-engine is used only when both a real schema was
                        # supplied AND EXTRACTION_ENGINE_BASE_URL is configured;
                        # otherwise (and on any extraction-engine failure — it fails
                        # soft, returning None rather than raising) this falls back
                        # to today's exact AdaptiveSelector behavior unchanged.
                        extracted = None
                        if self._extraction_engine is not None and schema:
                            extracted = await self._extraction_engine.extract(
                                result.html,
                                schema,
                                enable_smallmodel=(
                                    request.config_overrides.extraction_enable_smallmodel
                                    if request.config_overrides
                                    else False
                                ),
                                enable_llm=(
                                    request.config_overrides.extraction_enable_llm
                                    if request.config_overrides
                                    else False
                                ),
                            )
                        if extracted is None:
                            extracted = await AdaptiveSelector().extract(
                                result.html, schema=schema
                            )
                        result.extracted = extracted
                        # Markdown conversion (round 29) — same "wired once,
                        # applies regardless of level" rationale as
                        # extraction above. Previously only L1 ever produced
                        # markdown (inline Firecrawl calls in
                        # fetcher/level_1.py); centralizing here means a
                        # page that had to escalate to L2/L3 still gets
                        # clean markdown, not just raw HTML. markdown is its
                        # own field on FetchResult, independent of
                        # `extracted` — a caller who only wants the markdown
                        # (e.g. to hand to their own extraction model) can
                        # just read that field and ignore `extracted`.
                        if self._firecrawl is not None:
                            result.markdown = await self._firecrawl.convert_to_markdown(
                                result.html, url_str
                            )
                    results.append(result)
                    if on_result is not None:
                        await on_result(result)
                    break
                else:
                    await self._circuit_breaker.record_failure(domain)
                    if result.failure_category in (
                        FailureCategory.SSRF_BLOCKED,
                        FailureCategory.QUOTA_EXCEEDED,
                        FailureCategory.PROXY_EXHAUSTED,
                        # Escalating a dead/unresolvable host is futile — a browser
                        # can't resolve DNS the HTTP client couldn't (round 15).
                        FailureCategory.HOST_UNREACHABLE,
                    ):
                        await self._dlq.enqueue(
                            tenant_id,
                            job_id,
                            url_str,
                            result.failure_category,
                            result.error_message or "",
                            level,
                        )
                        errors.append(result.error_message or "DLQ")
                        results.append(result)
                        if on_result is not None:
                            await on_result(result)
                        break
            else:
                exhausted_result = FetchResult(
                    url=url_str,
                    success=False,
                    level_used=LEVELS[-1],
                    duration_ms=0,
                    failure_category=FailureCategory.PROXY_EXHAUSTED,
                    error_message="All fetch levels exhausted",
                )
                await self._dlq.enqueue(
                    tenant_id,
                    job_id,
                    url_str,
                    FailureCategory.PROXY_EXHAUSTED,
                    "All fetch levels exhausted",
                    LEVELS[-1],
                )
                errors.append("All levels exhausted")
                results.append(exhausted_result)
                if on_result is not None:
                    await on_result(exhausted_result)

        status = (
            JobStatus.CANCELLED
            if cancelled
            else (
                JobStatus.COMPLETED
                if not errors
                else (
                    JobStatus.FAILED
                    if not any(r.success for r in results)
                    else JobStatus.COMPLETED
                )
            )
        )
        return JobStatusResponse(
            job_id=job_id,
            status=status,
            progress=1.0,
            results=results if results else None,
            error="; ".join(errors) if errors else None,
        )

    async def _is_cancelled(self, tenant_id: TenantId, job_id: str) -> bool:
        """Cooperative mid-loop cancellation check (round 29) — a cheap point
        read before starting work on the next URL. Worst-case cancellation
        latency is however long the in-flight URL's own L1->L2->L3 escalation
        takes, matching the granularity on_result already persists at."""
        if self._pg is None:
            return False
        row = await self._pg.fetchrow(
            tenant_id,
            "SELECT status FROM scrape_jobs WHERE job_id = $1::uuid",
            job_id,
        )
        return row is not None and row["status"] == JobStatus.CANCELLED.value

    async def _check_cache(self, tenant_id: TenantId, url: str) -> FetchResult | None:
        """Reuse a recent successful scrape of this exact URL for this tenant
        instead of re-fetching (round 29). Sliding TTL — see CACHE_TTL_DAYS.
        Deliberately does not restore `html` (no need to re-store an S3
        snapshot on a cache hit — orchestrator/tasks.py's persist step just
        carries the existing html_snapshot_url pointer forward)."""
        if self._pg is None:
            return None
        row = await self._pg.fetchrow(
            tenant_id,
            f"""
            SELECT http_status, is_challenge_page, level_used, proxy_used,
                   markdown, json_data, html_snapshot_url
            FROM scrape_results
            WHERE url = $1 AND success = true
              AND extracted_at > NOW() - INTERVAL '{CACHE_TTL_DAYS} days'
            ORDER BY extracted_at DESC
            LIMIT 1
            """,
            url,
        )
        if row is None:
            return None
        return FetchResult(
            url=url,
            success=True,
            http_status=row["http_status"],
            is_challenge_page=row["is_challenge_page"],
            level_used=row["level_used"],
            proxy_used=row["proxy_used"],
            markdown=row["markdown"],
            extracted=json.loads(row["json_data"]) if row["json_data"] else None,
            html_snapshot_url=row["html_snapshot_url"],
            from_cache=True,
            duration_ms=0,
        )

    async def _fetch_url(
        self,
        tenant_id: TenantId,
        url: str,
        level: int,
        overrides: ConfigOverrides | None = None,
    ) -> FetchResult | None:
        """Dispatch fetch to the appropriate level fetcher."""
        if level == 1:
            from scraper_engine.fetcher.factory import build_level1_fetcher

            l1_fetcher = build_level1_fetcher(self._config)
            return await l1_fetcher.fetch(url, tenant_id, overrides=overrides)
        elif level == 2:
            from scraper_engine.fetcher.factory import build_level2_fetcher
            from scraper_engine.proxy.manager import ProxyManager

            pm = ProxyManager(redis=self._redis, pg=None)  # type: ignore[arg-type]
            from scraper_engine.core.exceptions import ProxyPoolExhaustedError

            try:
                lease = await pm.get_proxy(tenant_id, level=2, domain=self._extract_domain(url))
                async with lease:
                    l2_fetcher = build_level2_fetcher(
                        self._config,
                        captcha_solver=self._captcha_solver,
                        pool=self._browser_pool,
                        botasaurus_pool=self._botasaurus_pool,
                    )
                    return await l2_fetcher.fetch(
                        url, tenant_id, proxy=lease.proxy, overrides=overrides
                    )
            except ProxyPoolExhaustedError:
                return FetchResult(
                    url=url,
                    success=False,
                    level_used=level,
                    duration_ms=0,
                    failure_category=FailureCategory.PROXY_EXHAUSTED,
                    error_message="Proxy pool exhausted",
                )
        elif level == 3:
            from scraper_engine.fetcher.factory import build_level3_fetcher
            from scraper_engine.proxy.manager import ProxyManager

            pm = ProxyManager(redis=self._redis, pg=None)  # type: ignore[arg-type]
            from scraper_engine.core.exceptions import ProxyPoolExhaustedError

            try:
                lease = await pm.get_proxy(tenant_id, level=3, domain=self._extract_domain(url))
                async with lease:
                    l3_fetcher = build_level3_fetcher(
                        self._config,
                        captcha_solver=self._captcha_solver,
                        pool=self._browser_pool,
                    )
                    return await l3_fetcher.fetch(
                        url, tenant_id, proxy=lease.proxy, overrides=overrides
                    )
            except ProxyPoolExhaustedError:
                return FetchResult(
                    url=url,
                    success=False,
                    level_used=level,
                    duration_ms=0,
                    failure_category=FailureCategory.PROXY_EXHAUSTED,
                    error_message="Proxy pool exhausted",
                )
        return None

    @staticmethod
    def _extract_domain(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.hostname or "unknown"
