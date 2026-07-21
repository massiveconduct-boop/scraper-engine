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

from core.models import FailureCategory, FetchResult, JobStatus, JobStatusResponse

if TYPE_CHECKING:
    from core.models import ScrapeRequest
    from core.tenant import TenantId
    from storage.dlq import DeadLetterQueue
    from storage.redis_client import RedisClient

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
    ) -> None:
        self._redis = redis
        self._circuit_breaker = circuit_breaker
        self._politeness = politeness
        self._dlq = dlq

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

                acquired = await self._politeness.acquire_slot(domain, tenant_id)
                if not acquired:
                    await asyncio.sleep(1)
                    continue

                try:
                    await self._politeness.wait_if_needed(domain, tenant_id)
                    result = await self._fetch_url(tenant_id, url_str, level)
                finally:
                    await self._politeness.release_slot(domain, tenant_id)

                if result is None:
                    continue

                if result.success:
                    await self._circuit_breaker.record_success(domain)
                    results.append(result)
                    break
                else:
                    await self._circuit_breaker.record_failure(domain)
                    if result.failure_category in (
                        FailureCategory.SSRF_BLOCKED,
                        FailureCategory.QUOTA_EXCEEDED,
                        FailureCategory.PROXY_EXHAUSTED,
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
            from fetcher.level_1 import Level1Fetcher
            l1_fetcher = Level1Fetcher()
            return await l1_fetcher.fetch(url, tenant_id)
        elif level == 2:
            from fetcher.level_2 import Level2Fetcher
            from proxy.manager import ProxyManager

            pm = ProxyManager(redis=self._redis, pg=None)  # type: ignore[arg-type]
            from core.exceptions import ProxyPoolExhaustedError
            try:
                lease = await pm.get_proxy(tenant_id, level=2, domain=self._extract_domain(url))
                async with lease:
                    l2_fetcher = Level2Fetcher()
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
            from fetcher.level_3 import Level3Fetcher
            from proxy.manager import ProxyManager

            pm = ProxyManager(redis=self._redis, pg=None)  # type: ignore[arg-type]
            from core.exceptions import ProxyPoolExhaustedError
            try:
                lease = await pm.get_proxy(tenant_id, level=3, domain=self._extract_domain(url))
                async with lease:
                    l3_fetcher = Level3Fetcher()
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
