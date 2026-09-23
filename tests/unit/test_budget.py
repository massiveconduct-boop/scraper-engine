# tests/unit/test_budget.py
"""CapSolverBudget per-tenant ceiling resolution (core/budget.py).

Covers the `_pg`-backed ceiling lookup path that test_capsolver.py's fixture
never exercises (it always sets a fixed ceiling, which short-circuits before
ever touching `pg`).
"""

import asyncio
import gc
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


class TestAcquireBrowserPermit:
    """Round 64 — the shared permit protocol every browser engine uses."""

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch):
        from scraper_engine.core import budget

        monkeypatch.setattr(budget, "BROWSER_SEMAPHORE", asyncio.Semaphore(1))
        monkeypatch.setattr(budget, "_reclaimers", [])
        return budget

    async def test_free_permit_skips_reclaimers(self, _isolated):
        budget = _isolated
        reclaimer = _Reclaimer(frees=True)
        budget.register_permit_reclaimer(reclaimer.reclaim)

        await budget.acquire_browser_permit()

        assert reclaimer.calls == 0
        assert budget.BROWSER_SEMAPHORE.locked()

    async def test_locked_permit_asks_reclaimers(self, _isolated):
        budget = _isolated
        await budget.BROWSER_SEMAPHORE.acquire()
        reclaimer = _Reclaimer(frees=True)
        budget.register_permit_reclaimer(reclaimer.reclaim)

        await asyncio.wait_for(budget.acquire_browser_permit(), timeout=1)

        assert reclaimer.calls == 1

    async def test_nothing_to_reclaim_waits_and_counts_as_a_waiter(self, _isolated):
        budget = _isolated
        await budget.BROWSER_SEMAPHORE.acquire()
        empty = _Reclaimer(frees=False)
        budget.register_permit_reclaimer(empty.reclaim)

        waiter = asyncio.create_task(budget.acquire_browser_permit())
        await asyncio.sleep(0)
        assert budget.permit_waiters() == 1
        assert empty.calls == 1

        budget.BROWSER_SEMAPHORE.release()
        await asyncio.wait_for(waiter, timeout=1)
        assert budget.permit_waiters() == 0

    async def test_a_dropped_reclaimer_is_pruned_not_called(self, _isolated):
        budget = _isolated
        budget.register_permit_reclaimer(_Reclaimer(frees=True).reclaim)
        gc.collect()
        await budget.BROWSER_SEMAPHORE.acquire()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(budget.acquire_browser_permit(), timeout=0.1)
        assert budget._reclaimers == []

    async def test_cancelled_waiter_is_not_left_counted(self, _isolated):
        budget = _isolated
        await budget.BROWSER_SEMAPHORE.acquire()
        waiter = asyncio.create_task(budget.acquire_browser_permit())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert budget.permit_waiters() == 0

    async def test_unregister_removes_only_that_reclaimer(self, _isolated):
        budget = _isolated
        a, b = _Reclaimer(frees=True), _Reclaimer(frees=True)
        budget.register_permit_reclaimer(a.reclaim)
        budget.register_permit_reclaimer(b.reclaim)
        budget.unregister_permit_reclaimer(a.reclaim)
        assert [r() for r in budget._reclaimers] == [b.reclaim]


class _Reclaimer:
    def __init__(self, *, frees: bool) -> None:
        self.frees = frees
        self.calls = 0

    async def reclaim(self) -> bool:
        from scraper_engine.core import budget

        self.calls += 1
        if self.frees:
            budget.BROWSER_SEMAPHORE.release()
        return self.frees


class TestDisplayWaitMeter:
    """Round 66 — time spent queueing for XVFB_LOCK, per task."""

    @pytest.fixture(autouse=True)
    def _fresh_lock(self, monkeypatch):
        # The module-level lock binds to the first event loop that contends
        # for it; every test runs in its own loop.
        from scraper_engine.core import budget

        monkeypatch.setattr(budget, "XVFB_LOCK", asyncio.Lock())

    @pytest.mark.asyncio
    async def test_waiting_behind_a_holder_is_charged_to_the_waiter(self):
        from scraper_engine.core import budget

        release = asyncio.Event()
        holding = asyncio.Event()

        async def holder():
            async with budget.xvfb_lock():
                holding.set()
                await release.wait()

        async def waiter():
            meter = budget.start_display_wait_meter()
            await holding.wait()
            asyncio.get_running_loop().call_later(0.05, release.set)
            async with budget.xvfb_lock():
                pass
            return meter[0]

        holder_task = asyncio.create_task(holder())
        waited = await asyncio.create_task(waiter())
        await holder_task
        assert waited >= 0.04

    @pytest.mark.asyncio
    async def test_meters_are_per_task(self):
        from scraper_engine.core import budget

        async def uncontended():
            meter = budget.start_display_wait_meter()
            async with budget.xvfb_lock():
                await asyncio.sleep(0.02)
            return meter

        first, second = await asyncio.gather(uncontended(), uncontended())
        # One of the two queued behind the other; neither sees the other's wait.
        assert sorted([first[0] > 0.01, second[0] > 0.01]) == [False, True]

    @pytest.mark.asyncio
    async def test_no_meter_no_accounting(self):
        from scraper_engine.core import budget

        async def unmetered():
            async with budget.xvfb_lock():
                return budget._display_wait.get()

        assert await asyncio.create_task(unmetered()) is None
