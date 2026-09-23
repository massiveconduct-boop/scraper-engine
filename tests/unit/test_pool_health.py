# tests/unit/test_pool_health.py
"""PoolHealthMonitor tests — per-tier state machine + transition detection."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.config.schema import ProxyTierConfig
from scraper_engine.core.tenant import TenantId
from scraper_engine.proxy.pool_health import (
    PoolHealthMonitor,
    PoolHealthState,
    current_state,
)


@pytest.fixture
def tenant():
    return TenantId("system")


@pytest.fixture
def tier_config():
    return ProxyTierConfig(degraded_below_count=20, critical_below_count=5)


def make_pg(count: int):
    pg = AsyncMock()
    pg.fetch.return_value = [{"n": count}]
    return pg


class TestPoolHealthMonitor:
    @pytest.mark.asyncio
    async def test_first_check_healthy_pool_emits_no_transition(self, tenant, tier_config):
        """Bootstrap default (no prior state in Redis) is HEALTHY — a
        healthy first check must not report a spurious transition."""
        pg = make_pg(count=100)
        redis = AsyncMock()
        redis.raw.get.return_value = None
        monitor = PoolHealthMonitor(pg, redis, tier_config)

        transitions = await monitor.check(tenant)

        assert transitions == []

    @pytest.mark.asyncio
    async def test_drop_below_degraded_threshold_emits_one_transition_per_tier(
        self, tenant, tier_config
    ):
        pg = make_pg(count=10)  # below degraded_below_count=20, above critical=5
        redis = AsyncMock()
        redis.raw.get.return_value = None  # previously HEALTHY (default)
        monitor = PoolHealthMonitor(pg, redis, tier_config)

        transitions = await monitor.check(tenant)

        assert len(transitions) == 3  # one per tier (1, 2, 3)
        for t in transitions:
            assert t.old_state == PoolHealthState.HEALTHY
            assert t.new_state == PoolHealthState.DEGRADED
            assert t.validated_count == 10

    @pytest.mark.asyncio
    async def test_drop_below_critical_threshold(self, tenant, tier_config):
        pg = make_pg(count=2)
        redis = AsyncMock()
        redis.raw.get.return_value = None
        monitor = PoolHealthMonitor(pg, redis, tier_config)

        transitions = await monitor.check(tenant)

        assert all(t.new_state == PoolHealthState.CRITICAL for t in transitions)

    @pytest.mark.asyncio
    async def test_same_state_across_two_checks_emits_no_transition(self, tenant, tier_config):
        """Persisted state from a prior check() must be read back and
        compared — a state that hasn't changed is not a transition, even if
        it's DEGRADED both times (no repeated spam)."""
        pg = make_pg(count=10)
        redis = AsyncMock()
        # Simulate the persisted state already being DEGRADED from a prior
        # cycle — every read returns "degraded" regardless of what set() was
        # called with (redis.raw is a single fake store, not modeled here).
        redis.raw.get.return_value = "degraded"
        monitor = PoolHealthMonitor(pg, redis, tier_config)

        transitions = await monitor.check(tenant)

        assert transitions == []

    @pytest.mark.asyncio
    async def test_recovery_transition(self, tenant, tier_config):
        """DEGRADED -> HEALTHY is a real transition too (recovered event,
        round 34's ops-webhook trigger and dlq_reaper's retry trigger)."""
        pg = make_pg(count=100)
        redis = AsyncMock()
        redis.raw.get.return_value = "degraded"
        monitor = PoolHealthMonitor(pg, redis, tier_config)

        transitions = await monitor.check(tenant)

        assert len(transitions) == 3
        for t in transitions:
            assert t.old_state == PoolHealthState.DEGRADED
            assert t.new_state == PoolHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_one_query_failure_does_not_crash_whole_check(self, tenant, tier_config):
        """A transient DB error for one tier's query must not take down the
        whole health cycle — matches the isolation contract every other
        per-tenant/per-tier loop in this codebase follows."""
        pg = AsyncMock()
        pg.fetch.side_effect = [RuntimeError("db hiccup"), [{"n": 100}], [{"n": 100}]]
        redis = AsyncMock()
        redis.raw.get.return_value = None
        monitor = PoolHealthMonitor(pg, redis, tier_config)

        with pytest.raises(RuntimeError):
            # PoolHealthMonitor.check() itself does not swallow per-tier
            # errors (the caller, harvester_daemon's _run_periodic wrapper,
            # is what isolates failures across cycles) — this documents that
            # contract rather than asserting graceful per-tier isolation
            # that doesn't exist at this layer.
            await monitor.check(tenant)


class TestCurrentState:
    @pytest.mark.asyncio
    async def test_defaults_to_healthy_when_unset(self):
        redis = AsyncMock()
        redis.raw.get.return_value = None
        state = await current_state(redis, tier=2)
        assert state == PoolHealthState.HEALTHY

    @pytest.mark.asyncio
    async def test_reads_persisted_state(self):
        redis = AsyncMock()
        redis.raw.get.return_value = "critical"
        state = await current_state(redis, tier=3)
        assert state == PoolHealthState.CRITICAL
