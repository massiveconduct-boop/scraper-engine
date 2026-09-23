# storage/webhook_outbox.py
"""Transactional outbox for webhook deliveries (round 34).

Mirrors storage/dlq.py's shape deliberately — same durability contract
("write the fact first, deliver/retry independently") applied to
notifications instead of permanently-failed jobs. See
migrations/versions/007_webhook_outbox.py for the table definition and
orchestrator/webhook_sweeper.py for the process that drains `pending` rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.orchestrator.webhook_events import WebhookEvent

    from .postgres_client import PostgresClient


@dataclass
class OutboxEntry:
    """One durable webhook delivery attempt, pending or resolved."""

    id: str
    tenant_id: str
    job_id: str | None
    event_type: str
    payload: dict[str, object]
    target_url: str
    status: str
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    delivered_at: datetime | None


class WebhookOutbox:
    """Durable store for webhook deliveries — written before the first
    delivery attempt, updated after every attempt (success or failure)."""

    def __init__(self, pg: PostgresClient) -> None:
        self._pg = pg

    async def enqueue(
        self, tenant_id: TenantId, event: WebhookEvent, target_url: str
    ) -> str:
        """Persist a new outbox row before any delivery attempt is made.
        Returns the generated row id (str(uuid)) so the caller can make an
        immediate best-effort inline delivery attempt and update this same
        row via mark_delivered/mark_attempt_failed."""
        import json

        row = await self._pg.fetchrow(
            tenant_id,
            """
            INSERT INTO webhook_outbox (job_id, event_type, payload, target_url)
            VALUES ($1::uuid, $2, $3::jsonb, $4)
            RETURNING id
            """,
            event.job_id,
            event.event_type.value,
            json.dumps(event.payload),
            target_url,
        )
        assert row is not None
        return str(row["id"])

    async def mark_delivered(self, tenant_id: TenantId, entry_id: str) -> None:
        await self._pg.execute(
            tenant_id,
            """
            UPDATE webhook_outbox
            SET status = 'delivered', delivered_at = NOW(), attempts = attempts + 1
            WHERE id = $1::uuid
            """,
            entry_id,
        )

    async def mark_attempt_failed(
        self,
        tenant_id: TenantId,
        entry_id: str,
        next_attempt_at: datetime,
        max_attempts: int,
    ) -> None:
        """Bump the attempt counter and either reschedule or give up
        (status='dead') once max_attempts is reached — a dead row is still
        queryable/auditable, just no longer retried automatically."""
        await self._pg.execute(
            tenant_id,
            """
            UPDATE webhook_outbox
            SET attempts = attempts + 1,
                next_attempt_at = $2,
                status = CASE WHEN attempts + 1 >= $3 THEN 'dead' ELSE 'pending' END
            WHERE id = $1::uuid
            """,
            entry_id,
            next_attempt_at,
            max_attempts,
        )

    async def list_pending(self, tenant_id: TenantId, limit: int = 100) -> list[OutboxEntry]:
        """Rows due for a delivery attempt right now — status='pending' and
        next_attempt_at has passed. Ordered oldest-due-first."""
        import json

        rows = await self._pg.fetch(
            tenant_id,
            """
            SELECT id, job_id, event_type, payload, target_url, status,
                   attempts, next_attempt_at, created_at, delivered_at
            FROM webhook_outbox
            WHERE status = 'pending' AND next_attempt_at <= NOW()
            ORDER BY next_attempt_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [
            OutboxEntry(
                id=str(r["id"]),
                tenant_id=str(tenant_id),
                job_id=str(r["job_id"]) if r["job_id"] else None,
                event_type=r["event_type"],
                payload=json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                target_url=r["target_url"],
                status=r["status"],
                attempts=r["attempts"],
                next_attempt_at=r["next_attempt_at"],
                created_at=r["created_at"],
                delivered_at=r["delivered_at"],
            )
            for r in rows
        ]
