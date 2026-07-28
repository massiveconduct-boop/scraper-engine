# tests/integration/test_budget_and_quota.py
"""CapSolver budget + Quota integration tests — atomic Lua with mocked eval."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.core.budget import CapSolverBudget
from scraper_engine.core.exceptions import QuotaExceededError
from scraper_engine.core.quota import QuotaManager
from scraper_engine.core.tenant import TenantId


@pytest.fixture
def tenant():
    return TenantId("testtenant")


class TestCapSolverBudget:
    @pytest.mark.asyncio
    async def test_check_and_reserve_within_budget(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1  # Lua returns 1 = OK
        budget = CapSolverBudget(redis=redis, daily_ceiling_credits=1.0)
        ok = await budget.check_and_reserve(tenant, 0.5)
        assert ok is True

    @pytest.mark.asyncio
    async def test_check_and_reserve_exceeds_budget(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 0  # Lua returns 0 = denied
        budget = CapSolverBudget(redis=redis, daily_ceiling_credits=1.0)
        ok = await budget.check_and_reserve(tenant, 2.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_budget_accumulates(self, tenant):
        redis = AsyncMock()
        redis.eval.side_effect = [1, 1, 1, 0]  # 3 OK, 1 denied
        budget = CapSolverBudget(redis=redis, daily_ceiling_credits=1.0)
        assert await budget.check_and_reserve(tenant, 0.3)
        assert await budget.check_and_reserve(tenant, 0.3)
        assert await budget.check_and_reserve(tenant, 0.3)
        assert not await budget.check_and_reserve(tenant, 0.3)

    @pytest.mark.asyncio
    async def test_current_spend_tracks_usage(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1
        redis.get.return_value = "0.25"
        budget = CapSolverBudget(redis=redis, daily_ceiling_credits=1.0)
        await budget.check_and_reserve(tenant, 0.25)
        assert await budget.current_spend(tenant) == 0.25

    @pytest.mark.asyncio
    async def test_remaining_decreases(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1
        redis.get.return_value = "0.4"
        budget = CapSolverBudget(redis=redis, daily_ceiling_credits=1.0)
        await budget.check_and_reserve(tenant, 0.4)
        assert await budget.remaining(tenant) == 0.6


class TestQuotaManager:
    @pytest.mark.asyncio
    async def test_check_and_increment_within_limit(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 5  # new count
        qm = QuotaManager(redis=redis, daily_limit=100)
        await qm.check_and_increment(tenant)

    @pytest.mark.asyncio
    async def test_check_and_increment_exceeds_limit(self, tenant):
        redis = AsyncMock()
        redis.eval.side_effect = [1, 2, -1]  # 2 OK, then exceeded
        qm = QuotaManager(redis=redis, daily_limit=2)
        await qm.check_and_increment(tenant)
        await qm.check_and_increment(tenant)
        with pytest.raises(QuotaExceededError):
            await qm.check_and_increment(tenant)

    @pytest.mark.asyncio
    async def test_current_usage(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 2
        redis.get.return_value = "2"
        qm = QuotaManager(redis=redis, daily_limit=100)
        await qm.check_and_increment(tenant)
        await qm.check_and_increment(tenant)
        assert await qm.current_usage(tenant) == 2

    @pytest.mark.asyncio
    async def test_remaining(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 25
        redis.get.return_value = "25"
        qm = QuotaManager(redis=redis, daily_limit=100)
        await qm.check_and_increment(tenant, count=25)
        assert await qm.remaining(tenant) == 75
