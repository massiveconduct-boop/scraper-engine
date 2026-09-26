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
        # Round 70 — no site-requested wait unless the caller passes one.
        assert args[6] is None

    @pytest.mark.asyncio
    async def test_enqueue_passes_retry_not_before_through(self, dlq, pg) -> None:
        """Round 70 — a 429's Retry-After reaches the row, and is replaced
        (not kept from an earlier failure) on the upsert."""
        from scraper_engine.core.models import FailureCategory
        from scraper_engine.core.tenant import TenantId

        not_before = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        await dlq.enqueue(
            TenantId("test"),
            job_id="job-1",
            url="http://example.com/slow",
            category=FailureCategory.RATE_LIMITED,
            error="HTTP 429 (rate limited) at L3 via pool",
            level=3,
            retry_not_before=not_before,
        )

        _, query, args = pg.executed[0]
        assert args[2] == "rate_limited"
        assert args[6] == not_before
        assert "retry_not_before = EXCLUDED.retry_not_before" in query

    @pytest.mark.asyncio
    async def test_list_maps_retry_not_before(self, dlq, pg) -> None:
        from scraper_engine.core.models import FailureCategory
        from scraper_engine.core.tenant import TenantId

        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": 8,
                "job_id": "job-1",
                "url": "http://example.com/slow",
                "failure_category": "rate_limited",
                "error_message": "HTTP 429",
                "level_attempted": 3,
                "auto_retry_count": 0,
                "enqueued_at": now,
                "dead_at": now,
                "retry_not_before": now,
            }
        ]

        entries = await dlq.list_for_tenant(TenantId("test"))

        assert entries[0].failure_category == FailureCategory.RATE_LIMITED
        assert entries[0].retry_not_before == now
        _, query, _ = pg.fetch_calls
        assert "retry_not_before" in query

    @pytest.mark.asyncio
    async def test_list_for_tenant_maps_rows(self, dlq, pg) -> None:
        from scraper_engine.core.models import FailureCategory
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": 7,
                "job_id": "job-1",
                "url": "http://example.com/dead",
                "failure_category": "ssrf_blocked",
                "error_message": "blocked",
                "level_attempted": 2,
                "auto_retry_count": 1,
                "enqueued_at": now,
                "dead_at": now,
            }
        ]

        entries = await dlq.list_for_tenant(tenant, limit=50, offset=10)

        assert len(entries) == 1
        entry = entries[0]
        assert entry.id == 7
        assert entry.job_id == "job-1"
        assert entry.tenant_id == str(tenant)
        assert entry.url == "http://example.com/dead"
        assert entry.failure_category == FailureCategory.SSRF_BLOCKED
        assert entry.error_message == "blocked"
        assert entry.level_attempted == 2
        assert entry.auto_retry_count == 1
        assert entry.enqueued_at == now
        assert entry.dead_at == now
        # Round 70 — a row without the column (pre-012 shape) reads as None.
        assert entry.retry_not_before is None
        _, _, call_args = pg.fetch_calls
        assert call_args == (50, 10)

    @pytest.mark.asyncio
    async def test_list_for_tenant_casts_non_str_job_id(self, dlq, pg) -> None:
        """Round 37 — job_id is a Postgres uuid column; asyncpg returns a
        native UUID object for it, not a str, despite DeadLetterEntry.job_id
        being typed str. rq's validate_job_id() rejects anything that isn't
        a plain str, so this silently broke every real auto-retry attempt
        until _to_entries started casting explicitly. A plain string
        job_id="job-1" in other tests can't catch this — str("job-1") is a
        no-op either way — so this uses a distinct stand-in object with its
        own __str__, the way a real asyncpg UUID behaves."""
        from scraper_engine.core.tenant import TenantId

        class _FakeUUID:
            def __str__(self) -> str:
                return "11111111-1111-1111-1111-111111111111"

        tenant = TenantId("test")
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": 7,
                "job_id": _FakeUUID(),
                "url": "http://example.com/dead",
                "failure_category": "ssrf_blocked",
                "error_message": "blocked",
                "level_attempted": 2,
                "auto_retry_count": 1,
                "enqueued_at": now,
                "dead_at": now,
            }
        ]

        entries = await dlq.list_for_tenant(tenant, limit=50, offset=10)

        assert entries[0].job_id == "11111111-1111-1111-1111-111111111111"
        assert isinstance(entries[0].job_id, str)

    @pytest.mark.asyncio
    async def test_list_for_tenant_scoped_to_one_job(self, dlq, pg) -> None:
        """job_id filter (round 29) — GET /v1/jobs/{job_id}/dlq scopes to one
        job, distinct from the tenant-wide None-job_id view used elsewhere."""
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": 3,
                "job_id": "job-scoped",
                "url": "http://example.com/dead",
                "failure_category": "circuit_open",
                "error_message": "circuit open",
                "level_attempted": 1,
                "auto_retry_count": 0,
                "enqueued_at": now,
                "dead_at": now,
            }
        ]

        entries = await dlq.list_for_tenant(tenant, job_id="job-scoped")

        assert len(entries) == 1
        assert entries[0].job_id == "job-scoped"
        _, query, call_args = pg.fetch_calls
        assert "WHERE job_id = $1::uuid" in query
        assert call_args == ("job-scoped", 100, 0)

    @pytest.mark.asyncio
    async def test_list_for_tenant_empty(self, dlq, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        pg.fetch_rows = []
        entries = await dlq.list_for_tenant(TenantId("test"))
        assert entries == []

    @pytest.mark.asyncio
    async def test_mark_retry_attempt_increments_counter(self, dlq, pg) -> None:
        """round 34 — proxy/dlq_reaper.py bumps auto_retry_count in place,
        it does not delete the row (see clear() below for the removal
        path, which only fires on a subsequent success)."""
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        await dlq.mark_retry_attempt(tenant, 5)

        assert len(pg.executed) == 1
        recorded_tenant, query, args = pg.executed[0]
        assert recorded_tenant == tenant
        assert "UPDATE dead_letter_queue" in query
        assert "auto_retry_count = auto_retry_count + 1" in query
        assert args == (5,)

    @pytest.mark.asyncio
    async def test_clear_deletes_entry_by_job_and_url(self, dlq, pg) -> None:
        """round 34 — called once a previously-DLQ'd URL succeeds, so it
        stops showing as permanently dead."""
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        await dlq.clear(tenant, "job-1", "http://example.com/dead")

        assert len(pg.executed) == 1
        recorded_tenant, query, args = pg.executed[0]
        assert recorded_tenant == tenant
        assert "DELETE FROM dead_letter_queue" in query
        assert args == ("job-1", "http://example.com/dead")

    @pytest.mark.asyncio
    async def test_list_retryable_filters_by_category_and_cap(self, dlq, pg) -> None:
        from scraper_engine.core.models import FailureCategory
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": 9,
                "job_id": "job-1",
                "url": "http://example.com/dead",
                "failure_category": "proxy_exhausted",
                "error_message": "pool exhausted",
                "level_attempted": 2,
                "auto_retry_count": 1,
                "enqueued_at": now,
                "dead_at": now,
            }
        ]

        entries = await dlq.list_retryable(
            tenant,
            [FailureCategory.PROXY_EXHAUSTED, FailureCategory.CIRCUIT_OPEN],
            max_auto_retries=3,
            limit=20,
        )

        assert len(entries) == 1
        assert entries[0].id == 9
        _, query, call_args = pg.fetch_calls
        assert "auto_retry_count < $2" in query
        assert call_args == (
            [FailureCategory.PROXY_EXHAUSTED.value, FailureCategory.CIRCUIT_OPEN.value],
            3,
            20,
        )
