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
    async def test_reconciles_pending_job_rq_never_enqueued(self, tenant, cfg, monkeypatch):
        """Round 54 — a PENDING row whose enqueue() call raised (transient
        Redis error, or the process dying between the INSERT and the
        enqueue) never gets an rq job record at all. Must be reconciled the
        same as a hard-killed PROCESSING job, not left invisible forever."""
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "pending-job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value=None))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 1
        assert still_processing == 0

    @pytest.mark.asyncio
    async def test_leaves_genuinely_queued_pending_job_alone(self, tenant, cfg, monkeypatch):
        """A PENDING row that WAS actually enqueued (rq shows it queued,
        legitimately waiting under real backlog) must not be touched — only
        a row rq has no record of, or reports terminal, is reconcilable."""
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "pending-job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="queued"))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 0
        assert still_processing == 1
        pg.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_query_covers_both_pending_and_processing_grace_windows(
        self, tenant, cfg, monkeypatch
    ):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = []
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value=None))

        await stuck_job_reaper._reconcile_tenant(pg, redis, tenant, cfg)

        query_args = pg.fetch.await_args.args
        assert query_args[2] == JobStatus.PROCESSING.value
        assert query_args[3] == stuck_job_reaper._STALE_PROCESSING_GRACE_SECONDS
        assert query_args[4] == JobStatus.PENDING.value
        assert query_args[5] == stuck_job_reaper._STALE_PENDING_GRACE_SECONDS

    @pytest.mark.asyncio
    async def test_dispatches_webhook_when_configured(self, tenant, cfg, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": "https://example.com/hook"}]
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
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": "https://example.com/hook"}]
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


class TestRqJobIsReachable:
    """Round 62 — a non-terminal status on the rq job HASH is not evidence
    that a worker can still reach the job.

    Live-found: 20 rows sat PENDING in Postgres from 2026-08-12 to
    2026-08-27 while this reaper logged `reconciled=0 still_processing=20`
    every 60s, forever. Their `rq:job:*` hashes existed, reported
    `status=queued`, and had TTL -1 — but the ids were in no queue and no
    registry, so no worker was ever going to run them.
    """

    @pytest.mark.asyncio
    async def test_reachable_when_still_in_the_queue_list(self):
        redis = AsyncMock()
        redis.raw.lpos.return_value = 3

        assert await stuck_job_reaper._rq_job_is_reachable(redis, "job-1") is True
        redis.raw.lpos.assert_awaited_once_with("rq:queue:scraper-jobs", "job-1")

    @pytest.mark.asyncio
    async def test_reachable_when_in_a_registry_zset(self):
        redis = AsyncMock()
        redis.raw.lpos.return_value = None
        redis.raw.zscore.side_effect = [None, 1234.0, None]

        assert await stuck_job_reaper._rq_job_is_reachable(redis, "job-1") is True

    @pytest.mark.asyncio
    async def test_orphan_in_no_queue_and_no_registry(self):
        redis = AsyncMock()
        redis.raw.lpos.return_value = None
        redis.raw.zscore.return_value = None

        assert await stuck_job_reaper._rq_job_is_reachable(redis, "job-1") is False

    @pytest.mark.asyncio
    async def test_redis_error_fails_safe_as_reachable(self):
        """Wrongly reconciling a genuinely queued job cancels real work;
        wrongly skipping one costs another 60s sweep. Errors pick the
        cheaper mistake."""
        redis = AsyncMock()
        redis.raw.lpos.side_effect = RuntimeError("redis exploded")

        assert await stuck_job_reaper._rq_job_is_reachable(redis, "job-1") is True


class TestOrphanedQueuedJobReconciliation:
    @pytest.mark.asyncio
    async def test_queued_but_unreachable_job_is_reconciled(self, tenant, cfg, monkeypatch):
        """The exact five-week stall, as a test: rq says "queued", nothing
        can reach it, so it must be failed rather than counted as live."""
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "pending-job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="queued"))
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_is_reachable", AsyncMock(return_value=False))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 1
        assert still_processing == 0
        pg.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_queued_and_reachable_job_is_left_alone(self, tenant, cfg, monkeypatch):
        """Real backlog: 3 workers, one shared queue. A job genuinely waiting
        its turn must survive every sweep."""
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "pending-job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="queued"))
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_is_reachable", AsyncMock(return_value=True))

        reconciled, still_processing = await stuck_job_reaper._reconcile_tenant(
            pg, redis, tenant, cfg
        )

        assert reconciled == 0
        assert still_processing == 1
        pg.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_status_never_consults_reachability(self, tenant, cfg, monkeypatch):
        """A terminal status is already decisive — no extra Redis round trips."""
        pg = AsyncMock()
        redis = AsyncMock()
        pg.fetch.return_value = [{"job_id": "job-1", "webhook_url": None}]
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_status", AsyncMock(return_value="failed"))
        reachable = AsyncMock(return_value=True)
        monkeypatch.setattr(stuck_job_reaper, "_rq_job_is_reachable", reachable)

        reconciled, _ = await stuck_job_reaper._reconcile_tenant(pg, redis, tenant, cfg)

        assert reconciled == 1
        reachable.assert_not_awaited()
