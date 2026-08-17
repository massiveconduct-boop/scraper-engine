# tests/unit/test_budget.py
"""CapSolverBudget per-tenant ceiling resolution (core/budget.py).

Covers the `_pg`-backed ceiling lookup path that test_capsolver.py's fixture
never exercises (it always sets a fixed ceiling, which short-circuits before
ever touching `pg`).
"""

from unittest.mock import AsyncMock, patch

import pytest

from scraper_engine.core.budget import CapSolverBudget, resolve_browser_max_total_instances
from scraper_engine.core.tenant import TenantId


@pytest.fixture
def tenant():
    return TenantId("budgettest")


@pytest.fixture
def redis():
    r = AsyncMock()
    r.eval.return_value = 1
    r.get.return_value = "0.0"
    return r


class TestGetCeiling:
    @pytest.mark.asyncio
    async def test_no_pg_and_no_fixed_ceiling_uses_default(self, tenant, redis) -> None:
        budget = CapSolverBudget(redis=redis, pg=None)
        ceiling = await budget._get_ceiling(tenant)
        assert ceiling == CapSolverBudget.DEFAULT_DAILY_CEILING

    @pytest.mark.asyncio
    async def test_pg_row_with_ceiling_value_is_used(self, tenant, redis) -> None:
        pg = AsyncMock()
        pg.fetch.return_value = [{"capsolver_daily_credit_ceiling": 5.5}]
        budget = CapSolverBudget(redis=redis, pg=pg)

        ceiling = await budget._get_ceiling(tenant)

        assert ceiling == 5.5
        pg.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pg_row_with_null_ceiling_falls_back_to_default(self, tenant, redis) -> None:
        pg = AsyncMock()
        pg.fetch.return_value = [{"capsolver_daily_credit_ceiling": None}]
        budget = CapSolverBudget(redis=redis, pg=pg)

        ceiling = await budget._get_ceiling(tenant)

        assert ceiling == CapSolverBudget.DEFAULT_DAILY_CEILING

    @pytest.mark.asyncio
    async def test_pg_no_matching_row_falls_back_to_default(self, tenant, redis) -> None:
        pg = AsyncMock()
        pg.fetch.return_value = []
        budget = CapSolverBudget(redis=redis, pg=pg)

        ceiling = await budget._get_ceiling(tenant)

        assert ceiling == CapSolverBudget.DEFAULT_DAILY_CEILING

    @pytest.mark.asyncio
    async def test_second_lookup_within_ttl_hits_cache_not_pg(self, tenant, redis) -> None:
        pg = AsyncMock()
        pg.fetch.return_value = [{"capsolver_daily_credit_ceiling": 2.0}]
        budget = CapSolverBudget(redis=redis, pg=pg)

        first = await budget._get_ceiling(tenant)
        second = await budget._get_ceiling(tenant)

        assert first == second == 2.0
        pg.fetch.assert_awaited_once()  # second call served from cache, not a re-query

    @pytest.mark.asyncio
    async def test_remaining_uses_pg_backed_ceiling(self, tenant, redis) -> None:
        pg = AsyncMock()
        pg.fetch.return_value = [{"capsolver_daily_credit_ceiling": 3.0}]
        redis.get.return_value = "1.0"
        budget = CapSolverBudget(redis=redis, pg=pg)

        remaining = await budget.remaining(tenant)

        assert remaining == 2.0


class TestResolveBrowserMaxTotalInstances:
    """Round 59 — RAM-aware BROWSER_SEMAPHORE ceiling."""

    def test_disabled_returns_configured_max_unchanged(self) -> None:
        with patch(
            "botasaurus.calc_max_parallel_browsers.calc_max_parallel_browsers"
        ) as calc:
            result = resolve_browser_max_total_instances(
                8, enabled=False, average_ram_per_instance_gb=0.8
            )
        assert result == 8
        calc.assert_not_called()

    def test_enabled_delegates_to_calc_max_parallel_browsers(self) -> None:
        with patch(
            "botasaurus.calc_max_parallel_browsers.calc_max_parallel_browsers",
            return_value=3,
        ) as calc:
            result = resolve_browser_max_total_instances(
                8, enabled=True, average_ram_per_instance_gb=0.8
            )
        assert result == 3
        calc.assert_called_once_with(average_ram_per_instance=0.8, min=1, max=8)

    def test_enabled_result_is_coerced_to_int(self) -> None:
        with patch(
            "botasaurus.calc_max_parallel_browsers.calc_max_parallel_browsers",
            return_value=4.0,
        ):
            result = resolve_browser_max_total_instances(
                8, enabled=True, average_ram_per_instance_gb=0.8
            )
        assert result == 4
        assert isinstance(result, int)
