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

# Failure taxonomy split (round 34) — both sets still land in the DLQ (still
# visible, still auditable via GET /v1/jobs/{id}/dlq), but only TRANSIENT
# categories are eligible for proxy/dlq_reaper.py's auto-retry. PERMANENT
# categories describe conditions that retrying can never fix (a blocked
# SSRF target stays blocked, an exceeded quota doesn't refill itself mid-job,
# a dead host doesn't start resolving) — auto-retrying those would just burn
# proxy/browser budget for a guaranteed repeat failure. TRANSIENT categories
# describe conditions that resolve once *external* state changes: the proxy
# pool refills (proxy/pool_health.py's recovered transition) or a circuit
# breaker's cooldown expires — previously PROXY_EXHAUSTED was grouped with
# the permanent set even though it's transient by nature, so a DLQ'd job
# never got retried until a human noticed and hit the retry endpoint by hand.
PERMANENT_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.SSRF_BLOCKED,
        FailureCategory.QUOTA_EXCEEDED,
        FailureCategory.HOST_UNREACHABLE,
    }
)
TRANSIENT_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.PROXY_EXHAUSTED,
        FailureCategory.CIRCUIT_OPEN,
    }
)
DLQ_ELIGIBLE_CATEGORIES = PERMANENT_FAILURE_CATEGORIES | TRANSIENT_FAILURE_CATEGORIES


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
                    # `FetchResult.is_challenge_page` was declared on the model,
                    # persisted, and even gated dedup.py's caching decision, but
                    # no fetcher ever actually set it — L1 in particular only
                    # checks the HTTP status code (`success = status < 400`), so
                    # a 200 response whose body is literally an unsolved
                    # challenge/interstitial page (e.g. a JS proof-of-work gate)
                    # was accepted as real content and never escalated. Classify
                    # it here, once, centrally — same rationale as the
                    # extraction/markdown wiring below — so every level's result
                    # is labeled correctly regardless of which fetcher produced
                    # it. short_page_is_suspect=False matches the convention
                    # L2/L3's own internal solve-polling loops already use, so a
                    # short-but-genuinely-solved page isn't misclassified.
                    result.is_challenge_page = self._challenge_detector.is_challenge_page(
                        result.html or "", result.http_status or 200, short_page_is_suspect=False
                    )
                    # A JS-gated shell or an unsolved challenge page from a
                    # non-final level is not real content — an HTTP-only L1
                    # fetch of a SPA returns 200 with an empty mount point, and
                    # an HTTP-only L1 fetch of a JS PoW challenge returns 200
                    # with the interstitial itself. Escalate to a browser level
                    # instead of caching either as success (round 15 for the
                    # JS-gated-shell half of this; this round for the
                    # challenge-page half). Browser levels render JS and already
                    # loop internally until solved or exhausted, so a genuine L2/
                    # L3 success won't trip this; the final level accepts
                    # whatever it got.
                    if level < LEVELS[-1] and (
                        result.is_challenge_page
                        or self._challenge_detector.looks_javascript_gated(result.html or "")
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
                        else:
                            # Firecrawl is opt-in (FIRECRAWL_API_KEY/
                            # FIRECRAWL_BASE_URL) — without it, markdown used
                            # to be left None entirely, so a caller with no
                            # Firecrawl instance only ever got `extracted`
                            # (title/body/links), not markdown. Converting
                            # the HTML already in hand locally (round 33)
                            # means markdown is populated unconditionally.
                            from scraper_engine.services.markdown_fallback import (
                                html_to_markdown,
                            )

                            result.markdown = html_to_markdown(result.html)
                    results.append(result)
                    if on_result is not None:
                        await on_result(result)
                    break
                else:
                    await self._circuit_breaker.record_failure(domain)
                    if result.failure_category in DLQ_ELIGIBLE_CATEGORIES:
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

        any_success = any(r.success for r in results)
        status = (
            JobStatus.CANCELLED
            if cancelled
            else (
                JobStatus.COMPLETED
                if not errors
                else (JobStatus.FAILED if not any_success else JobStatus.COMPLETED)
            )
        )
        # partial_failure: status says COMPLETED but it isn't a clean run —
        # some URLs DLQ'd while others succeeded. Kept as a boolean flag
        # rather than a new JobStatus value (see JobStatusResponse docstring)
        # so a caller polling status can't mistake this for full success.
        partial_failure = bool(errors) and any_success
        return JobStatusResponse(
            job_id=job_id,
            status=status,
            progress=1.0,
            results=results if results else None,
            error="; ".join(errors) if errors else None,
            partial_failure=partial_failure,
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
            from scraper_engine.core.exceptions import (
                PostgresClientMissingError,
                ProxyPoolExhaustedError,
            )
            from scraper_engine.fetcher.factory import build_level2_fetcher
            from scraper_engine.proxy.manager import ProxyManager

            if self._pg is None:
                raise PostgresClientMissingError(level=level)
            pm = ProxyManager(redis=self._redis, pg=self._pg, tier_config=self._config.proxy_tiers)

            try:
                lease = await pm.get_proxy(tenant_id, level=2, domain=self._extract_domain(url))
                async with lease:
                    l2_fetcher = build_level2_fetcher(
                        self._config,
                        captcha_solver=self._captcha_solver,
                        pool=self._browser_pool,
                        botasaurus_pool=self._botasaurus_pool,
                    )
                    l2_result = await l2_fetcher.fetch(
                        url, tenant_id, proxy=lease.proxy, overrides=overrides
                    )
                    # Round 32: mark_success/mark_failure were fully built
                    # (formula-driven reliability_score recompute) but never
                    # actually called from the real fetch path — no real L2/L3
                    # outcome has ever updated a proxy's score. Wired here,
                    # once, right after the fetch's real outcome is known.
                    if l2_result.success:
                        await pm.mark_success(tenant_id, lease.proxy.ip, lease.proxy.port)
                    else:
                        await pm.mark_failure(
                            tenant_id, lease.proxy.ip, lease.proxy.port, self._extract_domain(url)
                        )
                    return l2_result
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
            from scraper_engine.core.exceptions import (
                PostgresClientMissingError,
                ProxyPoolExhaustedError,
            )
            from scraper_engine.fetcher.factory import build_level3_fetcher
            from scraper_engine.proxy.manager import ProxyManager

            if self._pg is None:
                raise PostgresClientMissingError(level=level)
            pm = ProxyManager(redis=self._redis, pg=self._pg, tier_config=self._config.proxy_tiers)

            try:
                lease = await pm.get_proxy(tenant_id, level=3, domain=self._extract_domain(url))
                async with lease:
                    l3_fetcher = build_level3_fetcher(
                        self._config,
                        captcha_solver=self._captcha_solver,
                        pool=self._browser_pool,
                    )
                    l3_result = await l3_fetcher.fetch(
                        url, tenant_id, proxy=lease.proxy, overrides=overrides
                    )
                    if l3_result.success:
                        await pm.mark_success(tenant_id, lease.proxy.ip, lease.proxy.port)
                    else:
                        await pm.mark_failure(
                            tenant_id, lease.proxy.ip, lease.proxy.port, self._extract_domain(url)
                        )
                    return l3_result
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
