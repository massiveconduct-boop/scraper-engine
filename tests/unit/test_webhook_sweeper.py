# tests/unit/test_webhook_sweeper.py
"""webhook_sweeper tests — outbox draining, backoff, dead-lettering (round 34)."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from scraper_engine.config.schema import WebhookConfig
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator import webhook_sweeper
from scraper_engine.storage.webhook_outbox import OutboxEntry


def make_entry(attempts=0, event_type="job.completed", target_url="https://example.com/hook"):
    now = datetime.now(UTC)
    return OutboxEntry(
        id="outbox-1",
        tenant_id="test",
        job_id="job-1",
        event_type=event_type,
        payload={"status": "COMPLETED"},
        target_url=target_url,
        status="pending",
        attempts=attempts,
        next_attempt_at=now,
        created_at=now,
        delivered_at=None,
    )


@pytest.fixture
def tenant():
    return TenantId("test")


@pytest.fixture
def webhook_cfg():
    return WebhookConfig(max_retries=3, timeout_seconds=10, backoff_base_seconds=2.0)


class TestBackoffSeconds:
    def test_grows_exponentially(self):
        assert webhook_sweeper._backoff_seconds(0, 2.0) == 2.0
        assert webhook_sweeper._backoff_seconds(1, 2.0) == 4.0
        assert webhook_sweeper._backoff_seconds(2, 2.0) == 8.0

    def test_caps_at_max(self):
        assert webhook_sweeper._backoff_seconds(20, 2.0) == webhook_sweeper.MAX_BACKOFF_SECONDS


class TestDeliverEntry:
    @pytest.mark.asyncio
    async def test_success_marks_delivered_and_returns_delivered(
        self, tenant, webhook_cfg, monkeypatch
    ):
        outbox = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
            AsyncMock(return_value=True),
        )
        entry = make_entry()

        outcome = await webhook_sweeper._deliver_entry(outbox, tenant, entry, webhook_cfg, redis)

        assert outcome == "delivered"
        outbox.mark_delivered.assert_awaited_once_with(tenant, "outbox-1")
        outbox.mark_attempt_failed.assert_not_awaited()
        redis.raw.incr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_below_cap_marks_retrying(self, tenant, webhook_cfg, monkeypatch):
        outbox = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
            AsyncMock(return_value=False),
        )
        entry = make_entry(attempts=0)  # 0 + 1 < max_retries=3

        outcome = await webhook_sweeper._deliver_entry(outbox, tenant, entry, webhook_cfg, redis)

        assert outcome == "retrying"
        outbox.mark_attempt_failed.assert_awaited_once()
        redis.raw.incr.assert_awaited_once_with("metrics:webhook_delivery_failures_total")

    @pytest.mark.asyncio
    async def test_failure_at_cap_marks_dead(self, tenant, webhook_cfg, monkeypatch):
        outbox = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
            AsyncMock(return_value=False),
        )
        entry = make_entry(attempts=2)  # 2 + 1 >= max_retries=3

        outcome = await webhook_sweeper._deliver_entry(outbox, tenant, entry, webhook_cfg, redis)

        assert outcome == "dead"

    @pytest.mark.asyncio
    async def test_delivery_exception_treated_as_failure(self, tenant, webhook_cfg, monkeypatch):
        outbox = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver",
            AsyncMock(side_effect=RuntimeError("network down")),
        )
        entry = make_entry(attempts=0)

        outcome = await webhook_sweeper._deliver_entry(outbox, tenant, entry, webhook_cfg, redis)

        assert outcome == "retrying"
        outbox.mark_delivered.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_target_renders_slack_shape(self, tenant, webhook_cfg, monkeypatch):
        deliver_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook.WebhookDispatcher.deliver", deliver_mock
        )
        outbox = AsyncMock()
        redis = AsyncMock()
        entry = make_entry(target_url="https://hooks.slack.com/services/T00/B00/XXX")

        await webhook_sweeper._deliver_entry(outbox, tenant, entry, webhook_cfg, redis)

        payload = deliver_mock.await_args.args[1]
        assert "blocks" in payload


class TestSweepTenant:
    @pytest.mark.asyncio
    async def test_counts_delivered_and_still_pending(self, tenant, webhook_cfg, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        outbox_instance = AsyncMock()
        outbox_instance.list_pending.return_value = [
            make_entry(attempts=0),
            make_entry(attempts=0),
        ]
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook_sweeper.WebhookOutbox",
            lambda pg_arg: outbox_instance,
        )
        # First entry delivers, second fails-and-retries.
        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook_sweeper._deliver_entry",
            AsyncMock(side_effect=["delivered", "retrying"]),
        )

        delivered, still_pending = await webhook_sweeper._sweep_tenant(
            pg, tenant, webhook_cfg, redis
        )

        assert delivered == 1
        assert still_pending == 1


class TestSweepCycle:
    @pytest.mark.asyncio
    async def test_sweeps_system_tenant_and_every_real_tenant(self, webhook_cfg, monkeypatch):
        pg = AsyncMock()
        pg.fetch.return_value = [{"tenant_id": "acme"}]
        redis = AsyncMock()

        seen_tenants = []

        async def fake_sweep_tenant(pg_arg, tenant_arg, cfg_arg, redis_arg):
            seen_tenants.append(str(tenant_arg))
            return (1, 0)

        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook_sweeper._sweep_tenant", fake_sweep_tenant
        )

        result = await webhook_sweeper._sweep_cycle(pg, redis, webhook_cfg)

        assert "system" in seen_tenants
        assert "acme" in seen_tenants
        assert "delivered=2" in result
        redis.raw.set.assert_awaited_once_with("metrics:webhook_outbox_pending", 0)

    @pytest.mark.asyncio
    async def test_one_tenant_failure_does_not_block_others(self, webhook_cfg, monkeypatch):
        pg = AsyncMock()
        pg.fetch.return_value = [{"tenant_id": "acme"}]
        redis = AsyncMock()

        async def flaky_sweep_tenant(pg_arg, tenant_arg, cfg_arg, redis_arg):
            if str(tenant_arg) == "system":
                raise RuntimeError("schema unreachable")
            return (3, 1)

        monkeypatch.setattr(
            "scraper_engine.orchestrator.webhook_sweeper._sweep_tenant", flaky_sweep_tenant
        )

        result = await webhook_sweeper._sweep_cycle(pg, redis, webhook_cfg)

        assert "delivered=3" in result
        assert "still_pending=1" in result


class TestRun:
    """Daemon lifecycle — mirrors test_harvester_daemon.py::TestRun, same
    supervisor shape (round 34's webhook_sweeper reuses that pattern)."""

    @pytest.mark.asyncio
    async def test_wires_from_config_and_shuts_down_cleanly(self, monkeypatch):
        from unittest.mock import MagicMock

        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(webhook_sweeper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(webhook_sweeper, "RedisClient", MagicMock(return_value=redis))

        from scraper_engine.config.schema import AppConfig

        stop = asyncio.Event()
        stop.set()  # request shutdown immediately — exercise start + clean teardown
        await webhook_sweeper.run(config=AppConfig(), stop=stop)

        pg.start.assert_awaited_once()
        redis.start.assert_awaited_once()
        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_installs_real_signal_handlers_when_stop_not_supplied(self, monkeypatch):
        import os
        import signal as signal_module
        from unittest.mock import MagicMock

        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(webhook_sweeper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(webhook_sweeper, "RedisClient", MagicMock(return_value=redis))

        from scraper_engine.config.schema import AppConfig

        task = asyncio.create_task(webhook_sweeper.run(config=AppConfig()))
        await asyncio.sleep(0.1)  # let run() reach add_signal_handler before we fire one
        os.kill(os.getpid(), signal_module.SIGTERM)
        await asyncio.wait_for(task, timeout=5)

        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()


class TestMain:
    def test_main_drives_run_via_asyncio_run(self, monkeypatch):
        calls = {"n": 0}

        async def fake_run():
            calls["n"] += 1

        monkeypatch.setattr(webhook_sweeper, "run", fake_run)
        webhook_sweeper.main()
        assert calls["n"] == 1


@pytest.mark.asyncio
async def test_sweep_counts_only_delivered_and_retrying(tenant, webhook_cfg, monkeypatch):
    """Round 64 (branch burn-down) — a dead-lettered entry is neither
    delivered nor still pending, so it must not land in either count."""
    monkeypatch.setattr(
        webhook_sweeper.WebhookOutbox,
        "list_pending",
        AsyncMock(return_value=[make_entry(), make_entry(), make_entry()]),
    )
    monkeypatch.setattr(
        webhook_sweeper,
        "_deliver_entry",
        AsyncMock(side_effect=["delivered", "retrying", "dead"]),
    )
    counts = await webhook_sweeper._sweep_tenant(AsyncMock(), tenant, webhook_cfg, AsyncMock())
    assert counts == (1, 1)
