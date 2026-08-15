# tests/unit/test_dlq_reaper.py
"""dlq_reaper tests — transient-failure auto-retry eligibility + re-enqueue
(round 34)."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.config.schema import DlqReaperConfig, ProxyTierConfig
from scraper_engine.core.models import FailureCategory
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.circuit_breaker import CircuitState
from scraper_engine.proxy import dlq_reaper
from scraper_engine.proxy.pool_health import PoolHealthState
from scraper_engine.storage.dlq import DeadLetterEntry


def make_entry(
    category=FailureCategory.PROXY_EXHAUSTED,
    level_attempted=2,
    url="http://example.com/dead",
    job_id="job-1",
    auto_retry_count=0,
):
    now = datetime.now(UTC)
    return DeadLetterEntry(
        id=1,
        job_id=job_id,
        tenant_id="test",
        url=url,
        failure_category=category,
        error_message="dead",
        level_attempted=level_attempted,
        auto_retry_count=auto_retry_count,
        enqueued_at=now,
        dead_at=now,
    )


@pytest.fixture
def tenant():
    return TenantId("test")


class TestIsEligible:
    @pytest.mark.asyncio
    async def test_proxy_exhausted_eligible_when_tier_healthy(self, monkeypatch):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state",
            AsyncMock(return_value=PoolHealthState.HEALTHY),
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True

    @pytest.mark.asyncio
    async def test_proxy_exhausted_not_eligible_when_tier_still_degraded(self, monkeypatch):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state",
            AsyncMock(return_value=PoolHealthState.DEGRADED),
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False

    @pytest.mark.asyncio
    async def test_level3_checks_tier2_when_fallback_enabled(self, monkeypatch):
        """Round 37 — allow_tier2_fallback_for_tier3 changes what a level-3
        lease actually depends on; the eligibility check must follow that,
        not check tier 3's raw (structurally-always-CRITICAL-on-free-only-
        sources) pool_health count."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier2_fallback_for_tier3=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=3)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [2]

    @pytest.mark.asyncio
    async def test_level3_checks_tier3_when_fallback_disabled(self, monkeypatch):
        """Default config (fallback off) — unchanged behavior, tier 3's own
        health still gates a level-3 exhaustion's retry eligibility."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier2_fallback_for_tier3=False)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=3)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [3]

    @pytest.mark.asyncio
    async def test_level2_exhaustion_unaffected_by_fallback_flag(self, monkeypatch):
        """The fallback only applies to level-3 leases (round 33) — a
        level-2 exhaustion must always check tier 2, regardless of the
        flag's value."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier2_fallback_for_tier3=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=2)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [2]

    @pytest.mark.asyncio
    async def test_level2_checks_tier1_when_fallback_enabled(self, monkeypatch):
        """Round 39 — same substitution as round 37's level-3/tier-2 case,
        one tier down: allow_tier1_fallback_for_tier2 changes what a
        level-2 lease actually depends on."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier1_fallback_for_tier2=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=2)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [1]

    @pytest.mark.asyncio
    async def test_level2_checks_tier2_when_fallback_disabled(self, monkeypatch):
        """Default config (fallback off) — unchanged behavior, tier 2's own
        health still gates a level-2 exhaustion's retry eligibility."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier1_fallback_for_tier2=False)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=2)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [2]

    @pytest.mark.asyncio
    async def test_level3_exhaustion_unaffected_by_tier1_fallback_flag(self, monkeypatch):
        """allow_tier1_fallback_for_tier2 is level-2-specific — a level-3
        exhaustion must not be affected by it, only by its own
        allow_tier2_fallback_for_tier3 flag."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig(allow_tier1_fallback_for_tier2=True)
        checked_tiers = []

        async def fake_pool_current_state(redis_arg, tier):
            checked_tiers.append(tier)
            return PoolHealthState.HEALTHY

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.pool_current_state", fake_pool_current_state
        )
        entry = make_entry(category=FailureCategory.PROXY_EXHAUSTED, level_attempted=3)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        assert checked_tiers == [3]

    @pytest.mark.asyncio
    async def test_circuit_open_eligible_when_breaker_closed(self):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        cb.state.return_value = CircuitState.CLOSED
        entry = make_entry(category=FailureCategory.CIRCUIT_OPEN, url="http://stillbad.com/x")

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is True
        cb.state.assert_awaited_once_with("stillbad.com")

    @pytest.mark.asyncio
    async def test_circuit_open_not_eligible_when_breaker_still_open(self):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        cb.state.return_value = CircuitState.OPEN
        entry = make_entry(category=FailureCategory.CIRCUIT_OPEN)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False

    @pytest.mark.asyncio
    async def test_circuit_open_not_eligible_when_half_open(self):
        """Deliberately conservative — a HALF_OPEN breaker is still probing,
        not confirmed healthy; the reaper waits for a real CLOSED."""
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        cb.state.return_value = CircuitState.HALF_OPEN
        entry = make_entry(category=FailureCategory.CIRCUIT_OPEN)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False

    @pytest.mark.asyncio
    async def test_unknown_category_is_never_eligible(self):
        redis = AsyncMock()
        cb = AsyncMock()
        tier_config = ProxyTierConfig()
        entry = make_entry(category=FailureCategory.SSRF_BLOCKED)

        assert await dlq_reaper._is_eligible(entry, redis, cb, tier_config) is False


class TestRetryEntry:
    @pytest.mark.asyncio
    async def test_bumps_counter_resets_status_and_reenqueues(self, tenant):
        pg = AsyncMock()
        pg.fetchrow.return_value = {"job_id": "job-1"}  # UPDATE ... RETURNING succeeded
        dlq = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        entry = make_entry(job_id="job-1")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        dlq.mark_retry_attempt.assert_awaited_once_with(tenant, entry.id)
        queue.enqueue.assert_called_once()
        call = queue.enqueue.call_args
        assert call.args == (
            "scraper_engine.orchestrator.tasks.run_scrape_job",
            "test",
            "job-1",
        )
        assert call.kwargs["job_id"] == "job-1"

    @pytest.mark.asyncio
    async def test_skips_reenqueue_when_job_already_active(self, tenant):
        """A job that's already PENDING/PROCESSING/CANCELLED must not get a
        duplicate rq job stacked on top of it."""
        pg = AsyncMock()
        pg.fetchrow.return_value = None  # UPDATE matched 0 rows (active/cancelled)
        dlq = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        entry = make_entry(job_id="job-2")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        dlq.mark_retry_attempt.assert_awaited_once()
        queue.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_guard_includes_completed_not_just_failed(self, tenant):
        """Round 37 — a batch job where most URLs succeeded and this one
        didn't settles at status='COMPLETED', not 'FAILED'; the guard must
        cover that case, not just a wholesale job failure. Asserts the
        actual SQL string, since the AsyncMock pg.fetchrow above can't
        catch a WHERE clause that's syntactically fine but semantically
        wrong (it would just always return the same canned value either
        way)."""
        pg = AsyncMock()
        pg.fetchrow.return_value = {"job_id": "job-3"}
        dlq = AsyncMock()
        queue = MagicMock()
        entry = make_entry(job_id="job-3")

        await dlq_reaper._retry_entry(pg, dlq, tenant, entry, queue)

        sql = pg.fetchrow.call_args.args[1]
        assert "NOT IN ('PENDING', 'PROCESSING', 'CANCELLED')" in sql
        assert "'FAILED', 'DEAD_LETTER'" not in sql


class TestReapTenant:
    @pytest.mark.asyncio
    async def test_only_retries_eligible_candidates(self, tenant, monkeypatch):
        redis = AsyncMock()
        cb = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        cfg = DlqReaperConfig(max_auto_retries=3, batch_size_per_tenant=20)

        eligible = make_entry(job_id="job-eligible")
        ineligible = make_entry(job_id="job-ineligible")

        monkeypatch.setattr(
            "scraper_engine.proxy.dlq_reaper.DeadLetterQueue",
            lambda pg: AsyncMock(list_retryable=AsyncMock(return_value=[eligible, ineligible])),
        )

        async def fake_is_eligible(entry, redis_arg, cb_arg, tier_config_arg):
            return entry.job_id == "job-eligible"

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._is_eligible", fake_is_eligible)
        retried_jobs = []

        async def fake_retry_entry(pg_arg, dlq_arg, tenant_arg, entry_arg, queue_arg):
            retried_jobs.append(entry_arg.job_id)

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._retry_entry", fake_retry_entry)

        pg = AsyncMock()
        tier_config = ProxyTierConfig()
        count = await dlq_reaper._reap_tenant(pg, redis, cb, queue, tenant, cfg, tier_config)

        assert count == 1
        assert retried_jobs == ["job-eligible"]


class TestReapCycle:
    @pytest.mark.asyncio
    async def test_sums_across_tenants_and_isolates_failures(self, monkeypatch):
        from scraper_engine.config.schema import AppConfig

        pg = AsyncMock()
        pg.fetch.return_value = [{"tenant_id": "acme"}, {"tenant_id": "widgets"}]
        redis = AsyncMock()
        cb = AsyncMock()
        queue = MagicMock()  # rq.Queue.enqueue is a sync call
        cfg = AppConfig()

        async def fake_reap_tenant(
            pg_arg, redis_arg, cb_arg, queue_arg, tenant_arg, reaper_cfg, tier_config_arg
        ):
            if str(tenant_arg) == "acme":
                raise RuntimeError("schema unreachable")
            return 2

        monkeypatch.setattr("scraper_engine.proxy.dlq_reaper._reap_tenant", fake_reap_tenant)

        result = await dlq_reaper._reap_cycle(pg, redis, cb, queue, cfg)

        assert result == "retried=2"


class TestDomain:
    def test_extracts_hostname(self):
        assert dlq_reaper._domain("http://example.com:8080/path") == "example.com"

    def test_falls_back_to_unknown_for_unparseable(self):
        assert dlq_reaper._domain("not-a-url") == "unknown"


class TestRun:
    """Daemon lifecycle — mirrors test_harvester_daemon.py::TestRun, same
    supervisor shape (round 34's dlq_reaper reuses that pattern)."""

    @pytest.mark.asyncio
    async def test_wires_from_config_and_shuts_down_cleanly(self, monkeypatch):
        pg = AsyncMock()
        redis = AsyncMock()
        monkeypatch.setattr(dlq_reaper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(dlq_reaper, "RedisClient", MagicMock(return_value=redis))
        monkeypatch.setattr(dlq_reaper, "build_queue", MagicMock(return_value=MagicMock()))

        from scraper_engine.config.schema import AppConfig

        stop = asyncio.Event()
        stop.set()  # request shutdown immediately — exercise start + clean teardown
        await dlq_reaper.run(config=AppConfig(), stop=stop)

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
        monkeypatch.setattr(dlq_reaper, "PostgresClient", MagicMock(return_value=pg))
        monkeypatch.setattr(dlq_reaper, "RedisClient", MagicMock(return_value=redis))
        monkeypatch.setattr(dlq_reaper, "build_queue", MagicMock(return_value=MagicMock()))

        from scraper_engine.config.schema import AppConfig

        task = asyncio.create_task(dlq_reaper.run(config=AppConfig()))
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

        monkeypatch.setattr(dlq_reaper, "run", fake_run)
        dlq_reaper.main()
        assert calls["n"] == 1
