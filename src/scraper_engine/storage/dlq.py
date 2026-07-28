# storage/dlq.py
"""Dead Letter Queue — terminal storage for permanently failed jobs.

Jobs land here after all escalation levels (L1→L2→L3) have been exhausted,
or when a non-retryable failure category is encountered (SSRF_BLOCKED,
PROXY_EXHAUSTED, QUOTA_EXCEEDED, CIRCUIT_OPEN).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.core.models import FailureCategory
    from scraper_engine.core.tenant import TenantId

    from .postgres_client import PostgresClient


@dataclass
class DeadLetterEntry:
    """A job that has permanently failed and landed in the DLQ."""

    job_id: str
    tenant_id: str
    url: str
    failure_category: FailureCategory
    error_message: str
    level_attempted: int
    enqueued_at: datetime
    dead_at: datetime


class DeadLetterQueue:
    """Terminal queue for jobs that cannot be retried."""

    def __init__(self, pg: PostgresClient) -> None:
        self._pg = pg

    async def enqueue(
        self,
        tenant_id: TenantId,
        job_id: str,
        url: str,
        category: FailureCategory,
        error: str,
        level: int,
    ) -> None:
        """Write a permanently failed job to the DLQ."""
        now = datetime.now(UTC)
        await self._pg.execute(
            tenant_id,
            """
            INSERT INTO dead_letter_queue (job_id, url, failure_category, error_message,
                                           level_attempted, dead_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            job_id,
            url,
            category.value,
            error,
            level,
            now,
        )

    async def list_for_tenant(
        self, tenant_id: TenantId, limit: int = 100, offset: int = 0
    ) -> list[DeadLetterEntry]:
        """List DLQ entries for a tenant, newest first."""
        rows = await self._pg.fetch(
            tenant_id,
            """
            SELECT job_id, url, failure_category, error_message, level_attempted,
                   enqueued_at, dead_at
            FROM dead_letter_queue
            ORDER BY dead_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        from scraper_engine.core.models import FailureCategory

        return [
            DeadLetterEntry(
                job_id=r["job_id"],
                tenant_id=str(tenant_id),
                url=r["url"],
                failure_category=FailureCategory(r["failure_category"]),
                error_message=r["error_message"],
                level_attempted=r["level_attempted"],
                enqueued_at=r["enqueued_at"],
                dead_at=r["dead_at"],
            )
            for r in rows
        ]

    async def retry(self, tenant_id: TenantId, job_id: str) -> None:
        """Re-enqueue a DLQ job for a fresh attempt (admin action).

        Removes from DLQ — the caller is responsible for re-enqueuing to the job queue.
        """
        await self._pg.execute(
            tenant_id,
            "DELETE FROM dead_letter_queue WHERE job_id = $1",
            job_id,
        )
