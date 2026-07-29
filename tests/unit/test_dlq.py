# tests/unit/test_dlq.py
"""Dead Letter Queue tests — terminal storage for permanently failed jobs."""

from datetime import UTC, datetime

import pytest


class FakePostgresClient:
    """In-memory PostgresClient fake, mirroring test_dedup.py's FakeRedis pattern."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.fetch_rows: list[dict] = []

    async def execute(self, tenant_id, query, *args):
        self.executed.append((tenant_id, query, args))
        return "OK"

    async def fetch(self, tenant_id, query, *args):
        self.fetch_calls = (tenant_id, query, args)
        return self.fetch_rows


class TestDeadLetterQueue:
    @pytest.fixture
    def pg(self):
        return FakePostgresClient()

    @pytest.fixture
    def dlq(self, pg):
        from scraper_engine.storage.dlq import DeadLetterQueue
        return DeadLetterQueue(pg)

    @pytest.mark.asyncio
    async def test_enqueue_writes_row(self, dlq, pg) -> None:
        from scraper_engine.core.models import FailureCategory
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        await dlq.enqueue(
            tenant,
            job_id="job-1",
            url="http://example.com/dead",
            category=FailureCategory.SSRF_BLOCKED,
            error="blocked at fetch time",
            level=2,
        )

        assert len(pg.executed) == 1
        recorded_tenant, query, args = pg.executed[0]
        assert recorded_tenant == tenant
        assert "INSERT INTO dead_letter_queue" in query
        assert args[0] == "job-1"
        assert args[1] == "http://example.com/dead"
        assert args[2] == FailureCategory.SSRF_BLOCKED.value
        assert args[3] == "blocked at fetch time"
        assert args[4] == 2
        assert isinstance(args[5], datetime)
        assert args[5].tzinfo is UTC

    @pytest.mark.asyncio
    async def test_list_for_tenant_maps_rows(self, dlq, pg) -> None:
        from scraper_engine.core.models import FailureCategory
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "job_id": "job-1",
                "url": "http://example.com/dead",
                "failure_category": "ssrf_blocked",
                "error_message": "blocked",
                "level_attempted": 2,
                "enqueued_at": now,
                "dead_at": now,
            }
        ]

        entries = await dlq.list_for_tenant(tenant, limit=50, offset=10)

        assert len(entries) == 1
        entry = entries[0]
        assert entry.job_id == "job-1"
        assert entry.tenant_id == str(tenant)
        assert entry.url == "http://example.com/dead"
        assert entry.failure_category == FailureCategory.SSRF_BLOCKED
        assert entry.error_message == "blocked"
        assert entry.level_attempted == 2
        assert entry.enqueued_at == now
        assert entry.dead_at == now
        _, _, call_args = pg.fetch_calls
        assert call_args == (50, 10)

    @pytest.mark.asyncio
    async def test_list_for_tenant_empty(self, dlq, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        pg.fetch_rows = []
        entries = await dlq.list_for_tenant(TenantId("test"))
        assert entries == []

    @pytest.mark.asyncio
    async def test_retry_deletes_entry(self, dlq, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        await dlq.retry(tenant, "job-1")

        assert len(pg.executed) == 1
        recorded_tenant, query, args = pg.executed[0]
        assert recorded_tenant == tenant
        assert "DELETE FROM dead_letter_queue" in query
        assert args == ("job-1",)
