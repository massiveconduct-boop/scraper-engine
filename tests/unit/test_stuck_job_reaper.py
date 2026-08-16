# tests/unit/test_stuck_job_reaper.py
"""stuck_job_reaper tests — round 52: reconciling scrape_jobs rows stuck at
PROCESSING after rq hard-kills a work-horse that missed its own in-process
timeout handling (see module docstring for the full rq-internals root cause)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.models import JobStatus
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator import stuck_job_reaper


@pytest.fixture
def tenant():
    return TenantId("test")


@pytest.fixture
def cfg():
    from scraper_engine.config.schema import AppConfig

    return AppConfig()


class TestRqJobStatus:
    @pytest.mark.asyncio
    async def test_returns_status_field(self):
        redis = AsyncMock()
        redis.raw.hget.return_value = "failed"

        status = await stuck_job_reaper._rq_job_status(redis, "job-1")

        assert status == "failed"
        redis.raw.hget.assert_awaited_once_with("rq:job:job-1", "status")

    @pytest.mark.asyncio
    async def test_returns_none_when_job_hash_gone(self):
        redis = AsyncMock()
        redis.raw.hget.return_value = None

        status = await stuck_job_reaper._rq_job_status(redis, "job-1")

        assert status is None


class TestReconcileTenant:
    @pytest.mark.asyncio
    async def test_reconciles_job_rq_reports_failed(self, tenant, cfg, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="failed"))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 1
        assert still_processing == 0
        pg.execute.assert_awaited_once()
        args = pg.execute.await_args.args
        assert args[2] == JobStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_reconciles_job_rq_has_no_record_of(self, tenant, cfg, monkeypatch):
        """A vanished rq hash (expired failure_ttl) is treated the same as a
        confirmed terminal status — PROCESSING-forever is worse than
        reconciling on the assumption."""
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value=None))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 1
        assert still_processing == 0

    @pytest.mark.asyncio
    async def test_leaves_genuinely_still_running_job_alone(self, tenant, cfg, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="started"))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 0
        assert still_processing == 1
        pg.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatches_webhook_when_configured(self, tenant, cfg, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [
            {"job_id": "job-1", "webhook_url": "https://example.com/hook"}
        ]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="failed"))
        dispatch_mock = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.orchestrator.tasks._dispatch_job_webhook", dispatch_mock
        )

        await stuck_job_reaper._reconcile_tenant(pg, redis, tenant, cfg)

        dispatch_mock.assert_awaited_once()
        assert dispatch_mock.await_args.args[6] == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_webhook_configured_skips_dispatch(self, tenant, cfg, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="failed"))
        dispatch_mock = AsyncMock()
        monkeypatch.setattr(
            "scraper_engine.orchestrator.tasks._dispatch_job_webhook", dispatch_mock
        )

        await stuck_job_reaper._reconcile_tenant(pg, redis, tenant, cfg)

        dispatch_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_webhook_dispatch_failure_does_not_break_reconciliation(
        self, tenant, cfg, monkeypatch
    ):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [
            {"job_id": "job-1", "webhook_url": "https://example.com/hook"}
        ]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="failed"))
        monkeypatch.setattr(
            "scraper_engine.orchestrator.tasks._dispatch_job_webhook",
            AsyncMock(side_effect=RuntimeError("target down")),
        )

        reconciled, _ = await stuck_job_reaper._reconcile_tenant(pg, redis, tenant, cfg)

        assert reconciled == 1  # DB reconciliation already happened before the webhook attempt


class TestSweepCycle:
    @pytest.mark.asyncio
    async def test_sweeps_every_real_tenant(self, cfg, monkeypatch):
        pg = AsyncMock()
        pg.fetch.return_value = [{"tenant_id": "acme"}]
        redis = AsyncMock()

        seen_tenants = []

        async def fake_reconcile(pg_arg, redis_arg, tenant_arg, cfg_arg):
            seen_tenants.append(str(tenant_arg))
            return (1, 0)

        monkeypatch.setattr(stuck_job_reaper, "_reconcile_tenant", fake_reconcile)

        result = await stuck_job_reaper._sweep_cycle(pg, redis, cfg)

        assert seen_tenants == ["acme"]
        assert "reconciled=1" in result
        assert "still_processing=0" in result

    @pytest.mark.asyncio
    async def test_one_tenant_failure_does_not_block_others(self, cfg, monkeypatch):
        pg = AsyncMock()
        pg.fetch.return_value = [{"tenant_id": "acme"}, {"tenant_id": "other"}]
        redis = AsyncMock()

        async def flaky_reconcile(pg_arg, redis_arg, tenant_arg, cfg_arg):
            if str(tenant_arg) == "acme":
                raise RuntimeError("schema unreachable")
            return (2, 1)

        monkeypatch.setattr(stuck_job_reaper, "_reconcile_tenant", flaky_reconcile)

        result = await stuck_job_reaper._sweep_cycle(pg, redis, cfg)

        assert "reconciled=2" in result
        assert "still_processing=1" in result


class TestRun:
    """Daemon lifecycle — mirrors test_webhook_sweeper.py::TestRun, same
    supervisor shape."""

    @pytest.mark.asyncio
    async def test_wires_from_config_and_shuts_down_cleanly(self, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(stuck_job_reaper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(stuck_job_reaper, "RedisClient", MagicMock(return_value=redis))

        from scraper_engine.config.schema import AppConfig

        stop = asyncio.Event()
        stop.set()
        await stuck_job_reaper.run(config=AppConfig(), stop=stop)

        pg.start.assert_awaited_once()
        redis.start.assert_awaited_once()
        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_installs_real_signal_handlers_when_stop_not_supplied(self, monkeypatch):
        import os
        import signal as signal_module

        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(stuck_job_reaper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(stuck_job_reaper, "RedisClient", MagicMock(return_value=redis))

        from scraper_engine.config.schema import AppConfig

        task = asyncio.create_task(stuck_job_reaper.run(config=AppConfig()))
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal_module.SIGTERM)
        await asyncio.wait_for(task, timeout=5)

        pg.stop.assert_awaited_once()
        redis.stop.assert_awaited_once()


class TestMain:
    def test_main_drives_run_via_asyncio_run(self, monkeypatch):
        calls = {"n": 0}

        async def fake_run():
            calls["n"] += 1

        monkeypatch.setattr(stuck_job_reaper, "run", fake_run)
        stuck_job_reaper.main()
        assert calls["n"] == 1
