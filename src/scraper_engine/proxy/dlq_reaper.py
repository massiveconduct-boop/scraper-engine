# proxy/dlq_reaper.py
"""Auto-retries transient DLQ entries once their underlying condition has
cleared (round 34).

orchestrator/worker.py's TRANSIENT_FAILURE_CATEGORIES (PROXY_EXHAUSTED,
CIRCUIT_OPEN) describe failures that resolve once *external* state changes —
unlike PERMANENT_FAILURE_CATEGORIES, retrying them isn't futile, it just has
to wait for the right moment. Before this, a DLQ'd job sat there forever
until a human noticed and manually retried it; there was no automated path
at all (storage/dlq.py's old `retry()` had zero callers).

This is a poll-driven check against current state (proxy/pool_health.py's
persisted per-tier state for PROXY_EXHAUSTED, CircuitBreaker.state() for
CIRCUIT_OPEN) rather than a push-only trigger — the same belt-and-suspenders
choice proxy/harvester_daemon.py's kick watcher makes, for the same reason:
a push signal can be missed on daemon restart, a poll against current truth
can't be.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from urllib.parse import urlparse

from rq import Queue

from scraper_engine.config.loader import load_config
from scraper_engine.config.schema import AppConfig, DlqReaperConfig
from scraper_engine.core.models import FailureCategory
from scraper_engine.core.periodic import run_periodic
from scraper_engine.core.tenant import TenantId
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.orchestrator.circuit_breaker import CircuitBreaker, CircuitState
from scraper_engine.orchestrator.job_queue import build_queue
from scraper_engine.proxy.pool_health import current_state as pool_current_state
from scraper_engine.storage.dlq import DeadLetterEntry, DeadLetterQueue
from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

_SCRAPE_JOB_TIMEOUT_SECONDS = 600
_TRANSIENT_CATEGORIES = [FailureCategory.PROXY_EXHAUSTED, FailureCategory.CIRCUIT_OPEN]


def _domain(url: str) -> str:
    return urlparse(url).hostname or "unknown"


async def _is_eligible(
    entry: DeadLetterEntry, redis: RedisClient, circuit_breaker: CircuitBreaker
) -> bool:
    """PROXY_EXHAUSTED is eligible once its tier (level_attempted maps 1:1 to
    a proxy/pool_health.py tier) is no longer DEGRADED/CRITICAL. CIRCUIT_OPEN
    is eligible once the breaker has fully closed for that domain — checked
    via the pure-read state() rather than allow_request(), which would
    itself consume a HALF_OPEN probe slot meant for real traffic, not the
    reaper's own bookkeeping."""
    from scraper_engine.proxy.pool_health import PoolHealthState

    if entry.failure_category == FailureCategory.PROXY_EXHAUSTED:
        pool_state = await pool_current_state(redis, entry.level_attempted)
        return pool_state == PoolHealthState.HEALTHY
    if entry.failure_category == FailureCategory.CIRCUIT_OPEN:
        circuit_state = await circuit_breaker.state(_domain(entry.url))
        return circuit_state == CircuitState.CLOSED
    return False


async def _retry_entry(
    pg: PostgresClient,
    dlq: DeadLetterQueue,
    tenant: TenantId,
    entry: DeadLetterEntry,
    queue: Queue,
) -> None:
    """Bump the retry counter, reset the job back to PENDING (only if it's
    not already active — a job can have multiple DLQ'd URLs, and the first
    eligible one to be retried shouldn't stomp a job an unrelated cause
    already re-activated), and re-enqueue under the same job_id. Reusing the
    original job_id (rather than minting a new one) means a caller polling
    GET /v1/jobs/{job_id} keeps seeing the same job transition PENDING ->
    PROCESSING -> terminal again, instead of the retry becoming invisible
    under a different id. Worker.process_job's cache check (CACHE_TTL_DAYS)
    means URLs that already succeeded are served from cache, not re-fetched
    — only the still-failing URL(s) actually do real work again."""
    await dlq.mark_retry_attempt(tenant, entry.id)
    row = await pg.fetchrow(
        tenant,
        """UPDATE scrape_jobs SET status = 'PENDING', updated_at = NOW()
           WHERE job_id = $1::uuid AND status IN ('FAILED', 'DEAD_LETTER')
           RETURNING job_id""",
        entry.job_id,
    )
    if row is None:
        # Job is already PENDING/PROCESSING/COMPLETED/CANCELLED for some
        # other reason — don't re-enqueue a duplicate rq job on top of it.
        return
    queue.enqueue(
        "scraper_engine.orchestrator.tasks.run_scrape_job",
        str(tenant),
        entry.job_id,
        job_id=entry.job_id,
        job_timeout=_SCRAPE_JOB_TIMEOUT_SECONDS,
    )
    logger.info(
        "dlq_auto_retry job_id=%s url=%s category=%s attempt=%d",
        entry.job_id,
        entry.url,
        entry.failure_category.value,
        entry.auto_retry_count + 1,
    )


async def _reap_tenant(
    pg: PostgresClient,
    redis: RedisClient,
    circuit_breaker: CircuitBreaker,
    queue: Queue,
    tenant: TenantId,
    cfg: DlqReaperConfig,
) -> int:
    dlq = DeadLetterQueue(pg)
    candidates = await dlq.list_retryable(
        tenant, _TRANSIENT_CATEGORIES, cfg.max_auto_retries, limit=cfg.batch_size_per_tenant
    )
    retried = 0
    for entry in candidates:
        if await _is_eligible(entry, redis, circuit_breaker):
            await _retry_entry(pg, dlq, tenant, entry, queue)
            retried += 1
    return retried


async def _reap_cycle(
    pg: PostgresClient,
    redis: RedisClient,
    circuit_breaker: CircuitBreaker,
    queue: Queue,
    cfg: AppConfig,
) -> str:
    """One reap cycle across every real tenant (not 'system' — DLQ entries
    belong to real tenants' jobs, unlike webhook_outbox's pool-health rows).
    One tenant's schema being unreachable must not block the others, same
    isolation contract as observability/metrics.py::refresh_dlq_size."""
    system = TenantId("system")
    rows = await pg.fetch(system, "SELECT tenant_id FROM public.tenants")
    total_retried = 0
    for row in rows:
        tenant = TenantId(row["tenant_id"])
        try:
            total_retried += await _reap_tenant(
                pg, redis, circuit_breaker, queue, tenant, cfg.dlq_reaper
            )
        except Exception:
            logger.exception("dlq_reaper_tenant_failed tenant=%s", tenant)
    return f"retried={total_retried}"


async def run(config: AppConfig | None = None, stop: asyncio.Event | None = None) -> None:
    """Start the reap loop and block until a stop signal arrives.

    ``stop`` lets a caller (or a test) drive shutdown directly; when omitted
    the daemon installs SIGTERM/SIGINT handlers so ``docker compose stop``
    is graceful — same shape as proxy/harvester_daemon.py::run.
    """
    cfg = config or load_config()
    bootstrap_observability(cfg.observability)

    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    await redis.start()
    circuit_breaker = CircuitBreaker(
        redis.raw,
        failure_threshold=cfg.circuit_breaker.failure_threshold,
        attempt_threshold=cfg.circuit_breaker.attempt_threshold,
        cooldown_seconds=cfg.circuit_breaker.cooldown_seconds,
        max_cooldown_seconds=cfg.circuit_breaker.max_cooldown_seconds,
    )
    queue = build_queue(cfg.storage.redis_url)

    task = asyncio.create_task(
        run_periodic(
            "dlq_reap",
            lambda: _reap_cycle(pg, redis, circuit_breaker, queue, cfg),
            cfg.dlq_reaper.interval_seconds,
            redis=redis,
        )
    )
    logger.info("dlq reaper started (interval=%ss)", cfg.dlq_reaper.interval_seconds)

    external_stop = stop is not None
    stop = stop or asyncio.Event()
    if not external_stop:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # pragma: no cover
                loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        logger.info("dlq reaper stopping")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.stop()
        await pg.stop()
        logger.info("dlq reaper stopped cleanly")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover — only true under `python -m`, not tests
    main()
