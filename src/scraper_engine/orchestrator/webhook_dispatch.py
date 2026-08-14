# orchestrator/webhook_dispatch.py
"""Shared durable webhook dispatch (round 34).

Split out of orchestrator/tasks.py so proxy/harvester_daemon.py (which needs
this for pool-health ops alerts) doesn't have to import tasks.py — that
module runs bootstrap side effects (load_config(), bootstrap_observability(),
configure_budget()) at import time, meant to run once per rq work-horse
process, not a second time inside the proxy daemon process. This module has
no import-time side effects, so both processes can depend on it safely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.config.schema import AppConfig
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.orchestrator.webhook_events import WebhookEvent
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient

import logging

logger = logging.getLogger(__name__)


async def enqueue_and_deliver_webhook_event(
    cfg: AppConfig,
    pg: PostgresClient,
    redis: RedisClient,
    tenant_id: TenantId,
    webhook_url: str,
    event: WebhookEvent,
) -> None:
    """Durable webhook dispatch shared by job events (orchestrator/tasks.py)
    and pool health events (proxy/harvester_daemon.py's ops channel) — write
    the outbox row first (a crash after this point still leaves a durable,
    sweepable fact instead of nothing), then make one immediate best-effort
    delivery attempt so the common case keeps today's low latency. A failed
    attempt leaves the row `pending` for orchestrator/webhook_sweeper.py to
    retry, rather than dropping it."""
    from scraper_engine.orchestrator.slack_formatter import render_for_target
    from scraper_engine.orchestrator.webhook import WebhookDispatcher
    from scraper_engine.storage.webhook_outbox import WebhookOutbox

    outbox = WebhookOutbox(pg)
    entry_id = await outbox.enqueue(tenant_id, event, webhook_url)

    async def _record_failure() -> None:
        next_attempt_at = datetime.now(UTC) + timedelta(seconds=cfg.webhook.backoff_base_seconds)
        await outbox.mark_attempt_failed(
            tenant_id, entry_id, next_attempt_at, cfg.webhook.max_retries
        )
        await redis.raw.incr("metrics:webhook_delivery_failures_total")

    try:
        rendered = render_for_target(event, webhook_url)
        delivered = await WebhookDispatcher.from_config(cfg.webhook).deliver(webhook_url, rendered)
        if delivered:
            await outbox.mark_delivered(tenant_id, entry_id)
        else:
            await _record_failure()
            logger.warning("webhook_delivery_failed job_id=%s url=%s", event.job_id, webhook_url)
    except Exception:
        await _record_failure()
        logger.exception("webhook_delivery_error job_id=%s url=%s", event.job_id, webhook_url)
