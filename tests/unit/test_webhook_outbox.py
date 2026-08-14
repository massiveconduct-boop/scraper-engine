# tests/unit/test_webhook_outbox.py
"""WebhookOutbox tests — durable delivery-attempt bookkeeping (round 34)."""

from datetime import UTC, datetime

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.webhook_events import WebhookEvent, WebhookEventType
from scraper_engine.storage.webhook_outbox import WebhookOutbox


class FakePostgresClient:
    """Mirrors tests/unit/test_dlq.py's FakePostgresClient pattern."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.fetch_rows: list[dict] = []
        self.fetchrow_result: dict | None = None

    async def execute(self, tenant_id, query, *args):
        self.executed.append((tenant_id, query, args))
        return "OK"

    async def fetch(self, tenant_id, query, *args):
        self.fetch_calls = (tenant_id, query, args)
        return self.fetch_rows

    async def fetchrow(self, tenant_id, query, *args):
        self.fetchrow_calls = (tenant_id, query, args)
        return self.fetchrow_result


@pytest.fixture
def pg():
    return FakePostgresClient()


@pytest.fixture
def outbox(pg):
    return WebhookOutbox(pg)


@pytest.fixture
def tenant():
    return TenantId("test")


def make_event(job_id="job-1"):
    return WebhookEvent(
        event_type=WebhookEventType.JOB_COMPLETED,
        tenant_id="test",
        job_id=job_id,
        payload={"status": "COMPLETED"},
    )


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_returns_generated_id(self, outbox, pg, tenant):
        pg.fetchrow_result = {"id": "outbox-123"}
        entry_id = await outbox.enqueue(tenant, make_event(), "https://example.com/hook")
        assert entry_id == "outbox-123"

    @pytest.mark.asyncio
    async def test_writes_event_type_and_payload(self, outbox, pg, tenant):
        pg.fetchrow_result = {"id": "outbox-1"}
        await outbox.enqueue(tenant, make_event(job_id="job-9"), "https://example.com/hook")

        _, query, args = pg.fetchrow_calls
        assert "INSERT INTO webhook_outbox" in query
        assert args[0] == "job-9"
        assert args[1] == "job.completed"
        assert args[3] == "https://example.com/hook"


class TestMarkDelivered:
    @pytest.mark.asyncio
    async def test_sets_status_delivered(self, outbox, pg, tenant):
        await outbox.mark_delivered(tenant, "outbox-1")
        assert len(pg.executed) == 1
        recorded_tenant, query, args = pg.executed[0]
        assert recorded_tenant == tenant
        assert "status = 'delivered'" in query
        assert args == ("outbox-1",)


class TestMarkAttemptFailed:
    @pytest.mark.asyncio
    async def test_bumps_attempts_and_reschedules(self, outbox, pg, tenant):
        next_at = datetime.now(UTC)
        await outbox.mark_attempt_failed(tenant, "outbox-1", next_at, max_attempts=3)

        assert len(pg.executed) == 1
        _, query, args = pg.executed[0]
        assert "attempts = attempts + 1" in query
        assert "'dead'" in query
        assert args == ("outbox-1", next_at, 3)


class TestListPending:
    @pytest.mark.asyncio
    async def test_maps_rows_to_entries(self, outbox, pg, tenant):
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": "outbox-1",
                "job_id": "job-1",
                "event_type": "job.partial_failure",
                "payload": {"status": "COMPLETED", "partial_failure": True},
                "target_url": "https://example.com/hook",
                "status": "pending",
                "attempts": 1,
                "next_attempt_at": now,
                "created_at": now,
                "delivered_at": None,
            }
        ]

        entries = await outbox.list_pending(tenant, limit=10)

        assert len(entries) == 1
        entry = entries[0]
        assert entry.id == "outbox-1"
        assert entry.job_id == "job-1"
        assert entry.event_type == "job.partial_failure"
        assert entry.payload == {"status": "COMPLETED", "partial_failure": True}
        assert entry.status == "pending"
        assert entry.attempts == 1

    @pytest.mark.asyncio
    async def test_parses_json_string_payload(self, outbox, pg, tenant):
        """asyncpg returns jsonb columns as Python objects already decoded in
        production, but the fake/dict-based test doubles elsewhere in this
        suite sometimes hand back a raw JSON string — both shapes must work."""
        now = datetime.now(UTC)
        pg.fetch_rows = [
            {
                "id": "outbox-2",
                "job_id": None,
                "event_type": "proxy_pool.critical",
                "payload": '{"tier": 3, "validated_count": 1}',
                "target_url": "https://ops.example.com/hook",
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": now,
                "created_at": now,
                "delivered_at": None,
            }
        ]

        entries = await outbox.list_pending(tenant)

        assert entries[0].payload == {"tier": 3, "validated_count": 1}
        assert entries[0].job_id is None

    @pytest.mark.asyncio
    async def test_query_filters_pending_and_due(self, outbox, pg, tenant):
        pg.fetch_rows = []
        await outbox.list_pending(tenant, limit=25)

        _, query, args = pg.fetch_calls
        assert "status = 'pending'" in query
        assert "next_attempt_at <= NOW()" in query
        assert args == (25,)
