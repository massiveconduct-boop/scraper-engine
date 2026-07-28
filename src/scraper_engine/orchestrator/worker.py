# orchestrator/worker.py
"""RQ task definition, execution loop, state machine driver.

The worker dequeues jobs and drives the escalation state machine:
  PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                          ↘ failure → ESCALATING_L2 → ...
                                                                       ↘ DEAD_LETTER
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from scraper_engine.core.models import FailureCategory, FetchResult, JobStatus, JobStatusResponse

if TYPE_CHECKING:
    from scraper_engine.browser.botasaurus_pool import BotasaurusPool
    from scraper_engine.browser.pool import BrowserPool
    from scraper_engine.config.schema import AppConfig
    from scraper_engine.core.models import ScrapeRequest
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.dlq import DeadLetterQueue
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

    from .circuit_breaker import CircuitBreaker
    from .politeness import PolitenessController

LEVELS = [1, 2, 3]


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
        self._captcha_solver = build_captcha_solver(
            CapSolverBudget(self._redis, pg=self._pg)
        )
        # Circuit breaker and politeness use raw Redis (not tenant-scoped),
        # so pass the underlying client for system-level key operations
        if hasattr(circuit_breaker, '_redis'):
            pass  # already set by caller

    async def process_job(
        self,
        tenant_id: TenantId,
        job_id: str,
        request: ScrapeRequest,
    ) -> JobStatusResponse:
        """Execute the full escalation state machine for a job."""
        results: list[FetchResult] = []
        errors: list[str] = []

        for url in request.urls:
            url_str = str(url)
            domain = self._extract_domain(url_str)

            for level in LEVELS:
                if not await self._circuit_breaker.allow_request(domain):
                    await self._dlq.enqueue(
                        tenant_id, job_id, url_str,
                        FailureCategory.CIRCUIT_OPEN,
                        f"Circuit open for {domain}",
                        level,
                    )
                    errors.append(f"Circuit open for {domain}")
                    break

                slot_worker_id = await self._politeness.acquire_slot(domain, tenant_id)
                if slot_worker_id is None:
                    await asyncio.sleep(1)
                    continue

                try:
                    await self._politeness.wait_if_needed(domain, tenant_id)
                    result = await self._fetch_url(tenant_id, url_str, level)
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
                    results.append(result)
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
                            tenant_id, job_id, url_str,
                            result.failure_category,
                            result.error_message or "",
                            level,
                        )
                        errors.append(result.error_message or "DLQ")
                        break
            else:
                await self._dlq.enqueue(
                    tenant_id, job_id, url_str,
                    FailureCategory.PROXY_EXHAUSTED,
                    "All fetch levels exhausted",
                    3,
                )
                errors.append("All levels exhausted")

        status = JobStatus.COMPLETED if not errors else (
            JobStatus.FAILED if len(results) == 0 else JobStatus.COMPLETED
        )
        return JobStatusResponse(
            job_id=job_id,
            status=status,
            progress=1.0,
            results=results if results else None,
            error="; ".join(errors) if errors else None,
        )

    async def _fetch_url(
        self, tenant_id: TenantId, url: str, level: int
    ) -> FetchResult | None:
        """Dispatch fetch to the appropriate level fetcher."""
        if level == 1:
            from scraper_engine.fetcher.factory import build_level1_fetcher
            l1_fetcher = build_level1_fetcher(self._config)
            return await l1_fetcher.fetch(url, tenant_id)
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
                    return await l2_fetcher.fetch(url, tenant_id, proxy=lease.proxy)
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
                    return await l3_fetcher.fetch(url, tenant_id, proxy=lease.proxy)
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
