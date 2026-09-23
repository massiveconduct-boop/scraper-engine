# orchestrator/webhook_sweeper.py
"""Long-running supervisor that drains storage/webhook_outbox.py (round 34).

orchestrator/tasks.py's inline delivery attempt covers the common case with
low latency, but a crash mid-delivery or a target that's down for a few
minutes needs an independent process to retry later — rq's per-job
work-horse process exits right after the job it ran, so it can never be that
process (see tasks.py's force_flush comment for the same "short-lived
process" constraint applied to tracing). This is that process: same
supervisor shape as proxy/harvester_daemon.py (SIGTERM/SIGINT graceful
shutdown, one failure-isolated periodic loop), reusing core/periodic.py's
run_periodic instead of re-implementing the loop.

``python -m scraper_engine.orchestrator.webhook_sweeper`` is the entry point
docker-compose.yml's webhook-sweeper service runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime, timedelta

from scraper_engine.config.loader import load_config
from scraper_engine.config.schema import AppConfig, WebhookConfig
from scraper_engine.core.periodic import run_periodic
from scraper_engine.core.tenant import TenantId
from scraper_engine.observability.bootstrap import bootstrap_observability
from scraper_engine.orchestrator.slack_formatter import render_for_target
from scraper_engine.orchestrator.webhook import WebhookDispatcher
from scraper_engine.orchestrator.webhook_events import WebhookEvent, WebhookEventType
from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient
from scraper_engine.storage.webhook_outbox import OutboxEntry, WebhookOutbox

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30
SWEEP_BATCH_PER_TENANT = 50
# Cap on the outbox-level retry backoff — independent of and coarser than
# WebhookDispatcher's own inner httpx-level backoff (config.webhook's
# backoff_base_seconds), which already runs a full retry cycle inside each
# single sweep attempt. This caps how long a *sweep-to-sweep* retry can wait.
MAX_BACKOFF_SECONDS = 3600.0


def _backoff_seconds(attempts: int, base: float) -> float:
    return min(float(base * (2**attempts)), MAX_BACKOFF_SECONDS)


async def _deliver_entry(
    outbox: WebhookOutbox,
    tenant: TenantId,
    entry: OutboxEntry,
    webhook_cfg: WebhookConfig,
    redis: RedisClient,
) -> str:
    """Attempt one delivery and update the outbox row accordingly. Returns
    the row's resulting state: "delivered", "retrying" (still pending, will
    be swept again), or "dead" (gave up after webhook_cfg.max_retries
    sweep-level attempts)."""
    event = WebhookEvent(
        event_type=WebhookEventType(entry.event_type),
        tenant_id=entry.tenant_id,
        job_id=entry.job_id,
        payload=entry.payload,
        created_at=entry.created_at,
    )
    try:
        rendered = render_for_target(event, entry.target_url)
        delivered = await WebhookDispatcher.from_config(webhook_cfg).deliver(
            entry.target_url, rendered
        )
    except Exception:
        logger.exception("webhook_sweep_delivery_error entry_id=%s", entry.id)
        delivered = False

    if delivered:
        await outbox.mark_delivered(tenant, entry.id)
        return "delivered"

    next_attempt_at = datetime.now(UTC) + timedelta(
        seconds=_backoff_seconds(entry.attempts, webhook_cfg.backoff_base_seconds)
    )
    outcome = "retrying" if entry.attempts + 1 < webhook_cfg.max_retries else "dead"
    await outbox.mark_attempt_failed(tenant, entry.id, next_attempt_at, webhook_cfg.max_retries)
    await redis.raw.incr("metrics:webhook_delivery_failures_total")
    if outcome == "dead":
        logger.warning(
            "webhook_outbox_entry_dead entry_id=%s target=%s attempts=%d",
            entry.id,
            entry.target_url,
            entry.attempts + 1,
        )
    return outcome


async def _sweep_tenant(
    pg: PostgresClient, tenant: TenantId, webhook_cfg: WebhookConfig, redis: RedisClient
) -> tuple[int, int]:
    """Returns (delivered_count, still_pending_count) for this tenant's batch."""
    outbox = WebhookOutbox(pg)
    pending = await outbox.list_pending(tenant, limit=SWEEP_BATCH_PER_TENANT)
    delivered_count = 0
    still_pending_count = 0
    for entry in pending:
        outcome = await _deliver_entry(outbox, tenant, entry, webhook_cfg, redis)
        if outcome == "delivered":
            delivered_count += 1
        elif outcome == "retrying":
            still_pending_count += 1
    return delivered_count, still_pending_count


async def _sweep_cycle(pg: PostgresClient, redis: RedisClient, webhook_cfg: WebhookConfig) -> str:
    """One sweep across every tenant schema plus the system tenant (pool
    health events live there — see proxy/harvester_daemon.py). One tenant's
    schema being unreachable must not block the others, matching the
    isolation contract observability/metrics.py::refresh_dlq_size already
    established for the same per-tenant-schema iteration shape."""
    system = TenantId("system")
    total_delivered = 0
    total_pending = 0

    tenant_ids = [system]
    rows = await pg.fetch(system, "SELECT tenant_id FROM public.tenants")
    tenant_ids.extend(TenantId(row["tenant_id"]) for row in rows)

    for tenant in tenant_ids:
        try:
            delivered, still_pending = await _sweep_tenant(pg, tenant, webhook_cfg, redis)
            total_delivered += delivered
            total_pending += still_pending
        except Exception:
            logger.exception("webhook_sweep_tenant_failed tenant=%s", tenant)

    # Redis-backed, refreshed into the local Gauge only when /metrics is
    # scraped (by the separate, long-lived api process) — see
    # observability/metrics.py::refresh_redis_backed_counters and its
    # docstring for why an in-process .set() here would never be seen there.
    await redis.raw.set("metrics:webhook_outbox_pending", total_pending)
    return f"delivered={total_delivered} still_pending={total_pending}"


async def run(config: AppConfig | None = None, stop: asyncio.Event | None = None) -> None:
    """Start the sweep loop and block until a stop signal arrives.

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

    task = asyncio.create_task(
        run_periodic(
            "webhook_sweep",
            lambda: _sweep_cycle(pg, redis, cfg.webhook),
            SWEEP_INTERVAL_SECONDS,
            redis=redis,
        )
    )
    logger.info("webhook sweeper started (interval=%ss)", SWEEP_INTERVAL_SECONDS)

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
        logger.info("webhook sweeper stopping")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.stop()
        await pg.stop()
        logger.info("webhook sweeper stopped cleanly")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover — only true under `python -m`, not tests
    main()
