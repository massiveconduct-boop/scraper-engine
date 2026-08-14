# storage/dlq.py
"""Dead Letter Queue — terminal storage for failed jobs, permanent and transient.

URLs land here after all escalation levels (L1→L2→L3) have been exhausted,
or when a DLQ-eligible failure category is encountered — see
orchestrator/worker.py's PERMANENT_FAILURE_CATEGORIES (SSRF_BLOCKED,
QUOTA_EXCEEDED, HOST_UNREACHABLE — retrying can never help) and
TRANSIENT_FAILURE_CATEGORIES (PROXY_EXHAUSTED, CIRCUIT_OPEN — resolves once
external state changes; auto-retried by proxy/dlq_reaper.py, round 34).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

    from scraper_engine.core.models import FailureCategory
    from scraper_engine.core.tenant import TenantId

    from .postgres_client import PostgresClient


@dataclass
class DeadLetterEntry:
    """A job that has permanently failed and landed in the DLQ."""

    id: int
    job_id: str
    tenant_id: str
    url: str
    failure_category: FailureCategory
    error_message: str
    level_attempted: int
    auto_retry_count: int
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
        """Write a permanently (or transiently, pending auto-retry) failed
        URL to the DLQ. UPSERTs on (job_id, url) (round 34) — a URL that
        proxy/dlq_reaper.py auto-retried and that failed again lands back on
        the *same* row (auto_retry_count carried forward via the DO UPDATE's
        implicit no-op on that column), instead of a fresh INSERT resetting
        the count and defeating the reaper's retry cap."""
        now = datetime.now(UTC)
        await self._pg.execute(
            tenant_id,
            """
            INSERT INTO dead_letter_queue (job_id, url, failure_category, error_message,
                                           level_attempted, dead_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (job_id, url) DO UPDATE SET
                failure_category = EXCLUDED.failure_category,
                error_message = EXCLUDED.error_message,
                level_attempted = EXCLUDED.level_attempted,
                dead_at = EXCLUDED.dead_at
            """,
            job_id,
            url,
            category.value,
            error,
            level,
            now,
        )

    _SELECT_COLUMNS = (
        "id, job_id, url, failure_category, error_message, level_attempted, "
        "auto_retry_count, enqueued_at, dead_at"
    )

    async def list_for_tenant(
        self,
        tenant_id: TenantId,
        limit: int = 100,
        offset: int = 0,
        job_id: str | None = None,
    ) -> list[DeadLetterEntry]:
        """List DLQ entries for a tenant, newest first. When job_id is given,
        scoped to that one job (round 29 — GET /v1/jobs/{job_id}/dlq); None
        keeps the existing tenant-wide "everything currently dead" view used
        by ops tooling and the dlq_size Prometheus gauge."""
        if job_id is not None:
            rows = await self._pg.fetch(
                tenant_id,
                f"""
                SELECT {self._SELECT_COLUMNS}
                FROM dead_letter_queue
                WHERE job_id = $1::uuid
                ORDER BY dead_at DESC
                LIMIT $2 OFFSET $3
                """,
                job_id,
                limit,
                offset,
            )
        else:
            rows = await self._pg.fetch(
                tenant_id,
                f"""
                SELECT {self._SELECT_COLUMNS}
                FROM dead_letter_queue
                ORDER BY dead_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        return self._to_entries(tenant_id, rows)

    async def list_retryable(
        self,
        tenant_id: TenantId,
        categories: list[FailureCategory],
        max_auto_retries: int,
        limit: int = 50,
    ) -> list[DeadLetterEntry]:
        """DLQ entries eligible for proxy/dlq_reaper.py's auto-retry — a
        transient failure_category (see orchestrator/worker.py's
        TRANSIENT_FAILURE_CATEGORIES) that hasn't already exhausted its
        retry cap. Oldest-dead-first, so a long-stuck entry isn't starved by
        a stream of freshly-DLQ'd ones."""
        rows = await self._pg.fetch(
            tenant_id,
            f"""
            SELECT {self._SELECT_COLUMNS}
            FROM dead_letter_queue
            WHERE failure_category = ANY($1::text[]) AND auto_retry_count < $2
            ORDER BY dead_at ASC
            LIMIT $3
            """,
            [c.value for c in categories],
            max_auto_retries,
            limit,
        )
        return self._to_entries(tenant_id, rows)

    async def mark_retry_attempt(self, tenant_id: TenantId, entry_id: int) -> None:
        """Bump auto_retry_count right before re-enqueuing the owning job
        (round 34). Does not delete the row — enqueue()'s UPSERT will either
        refresh it in place if the retry fails again, or `clear()` below
        removes it if the retry succeeds. Leaving it in place between those
        two outcomes means GET /v1/jobs/{id}/dlq keeps showing the entry as
        dead (accurately — it still is, until proven otherwise) rather than
        vanishing and reappearing."""
        await self._pg.execute(
            tenant_id,
            "UPDATE dead_letter_queue SET auto_retry_count = auto_retry_count + 1 WHERE id = $1",
            entry_id,
        )

    async def clear(self, tenant_id: TenantId, job_id: str, url: str) -> None:
        """Remove a DLQ entry once its URL has since succeeded (round 34) —
        called from orchestrator/tasks.py's result-persist path. Without
        this, a URL that succeeded on auto-retry would still show as
        permanently dead in GET /v1/jobs/{id}/dlq forever."""
        await self._pg.execute(
            tenant_id,
            "DELETE FROM dead_letter_queue WHERE job_id = $1::uuid AND url = $2",
            job_id,
            url,
        )

    @staticmethod
    def _to_entries(tenant_id: TenantId, rows: list[asyncpg.Record]) -> list[DeadLetterEntry]:
        from scraper_engine.core.models import FailureCategory

        return [
            DeadLetterEntry(
                id=r["id"],
                job_id=r["job_id"],
                tenant_id=str(tenant_id),
                url=r["url"],
                failure_category=FailureCategory(r["failure_category"]),
                error_message=r["error_message"],
                level_attempted=r["level_attempted"],
                auto_retry_count=r["auto_retry_count"],
                enqueued_at=r["enqueued_at"],
                dead_at=r["dead_at"],
            )
            for r in rows
        ]
