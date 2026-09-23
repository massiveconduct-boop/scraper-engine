# tests/integration/test_seat_keeper.py
"""orchestrator/host_capacity.py::SeatKeeper against REAL Redis (round 67).

The keeper is what lets a browser be parked for reuse under host admission:
it holds the seat of the render that launched the browser, renews it, and
gives it back when the pool closes that browser or when other work is waiting
for the host. The seat arithmetic is the whole point, so every case here reads
`in_use` back out of Redis rather than trusting the keeper's own bookkeeping.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from redis.asyncio import Redis

from scraper_engine.config.schema import HostCapacityConfig
from scraper_engine.core.browser_rss import BrowserMemorySample
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.host_capacity import (
    AdmissionUnavailableError,
    HostAdmission,
    SeatKeeper,
)

pytestmark = pytest.mark.integration

T1 = TenantId("hcseat1")


@pytest.fixture
async def redis():
    r = Redis(host="localhost", port=6379, decode_responses=True)
    yield r
    for pattern in ("hc:hcseat-*", "politeness:*:hcseat*"):
        keys = [k async for k in r.scan_iter(match=pattern, count=1000)]
        if keys:
            await r.delete(*keys)
    await r.aclose()


def _cfg(**overrides) -> HostCapacityConfig:
    base = HostCapacityConfig(
        enabled=True,
        default_units=4.0,
        poll_min_seconds=0.01,
        poll_max_seconds=0.02,
    )
    # model_copy skips validation, so tests may use sub-second lifetimes.
    return base.model_copy(update=overrides)


def _admission(redis, **overrides) -> HostAdmission:
    return HostAdmission(redis, f"hcseat-{uuid.uuid4().hex[:8]}", _cfg(**overrides))


def _claim(adm, *, domain="a.example", cap=5, weight=1.0):
    return adm.claim(
        tenant_id=T1,
        domain=domain,
        weight=weight,
        concurrency=cap,
        delay_seconds=0.0,
        priority_ms=int(time.time() * 1000),
        wait_budget_seconds=0.0,
    )


class _FakePool:
    """Stands in for a browser pool: `closed` counts the parked instances the
    keeper asked it to close, and closing one gives its seat back."""

    def __init__(self, keeper: SeatKeeper) -> None:
        self._keeper = keeper
        self.parked: list[str] = []
        self.closed_seats: list[str] = []

    def park(self) -> str | None:
        seat = self._keeper.retain()
        if seat is not None:
            self.parked.append(seat)
        return seat

    async def close_one(self, seat: str | None = None) -> bool:
        if seat is None:
            seat = self.parked[0] if self.parked else None
        if seat is None or seat not in self.parked:
            return False
        self.parked.remove(seat)
        self.closed_seats.append(seat)
        await self._keeper.discard(seat)
        return True

    @property
    def closed(self) -> int:
        return len(self.closed_seats)


class TestRetainedSeats:
    @pytest.mark.asyncio
    async def test_a_parked_browser_keeps_its_seat_and_gives_the_slot_back(self, redis):
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)

        async with _claim(adm, cap=1) as grant:
            assert keeper.retain() == grant.lease_id

        # The seat is still held — the browser it launched is parked, not gone.
        assert (await adm.snapshot()).in_use == 1.0
        # ...but the website's politeness slot came back, so a second render on
        # the same site (cap=1) is admitted right away.
        async with _claim(adm, cap=1):
            assert (await adm.snapshot()).in_use == 2.0

    @pytest.mark.asyncio
    async def test_closing_the_browser_returns_the_seat(self, redis):
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm) as grant:
            keeper.retain()
        await keeper.discard(grant.lease_id)
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_one_seat_is_one_browser(self, redis):
        """A render holds one seat, so only the first instance it parks can
        keep it — a pool that cannot keep its instance closes it instead."""
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm):
            assert keeper.retain() is not None
            assert keeper.retain() is None
        assert (await adm.snapshot()).in_use == 1.0

    @pytest.mark.asyncio
    async def test_nothing_to_retain_outside_a_claim(self, redis):
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)
        assert keeper.retain() is None
        await keeper.discard(None)
        await keeper.discard("never-retained")
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_shutdown_releases_everything_still_held(self, redis):
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm):
            keeper.retain()
        await keeper.shutdown()
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_a_release_that_cannot_reach_redis_lets_the_lease_lapse(self, redis):
        adm = _admission(redis, lease_ttl_seconds=1)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm) as grant:
            keeper.retain()

        async def boom(*_args, **_kwargs):
            raise AdmissionUnavailableError("redis down")

        adm.release_seat = boom  # type: ignore[method-assign]
        await keeper.discard(grant.lease_id)
        # Not raised to the pool, and the seat is no longer tracked here: it
        # lapses on its own within one lease TTL.
        await keeper.tick()


class TestKeeperTick:
    @pytest.mark.asyncio
    async def test_renews_a_held_seat(self, redis):
        adm = _admission(redis, lease_ttl_seconds=60, renew_interval_seconds=0)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm) as grant:
            keeper.retain()
        before = await redis.zscore(adm.seats_key, grant.lease_id)
        await asyncio.sleep(0.05)
        await keeper.tick()
        assert await redis.zscore(adm.seats_key, grant.lease_id) > before

    @pytest.mark.asyncio
    async def test_a_seat_renewed_too_recently_is_left_alone(self, redis):
        adm = _admission(redis, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm) as grant:
            keeper.retain()
        before = await redis.zscore(adm.seats_key, grant.lease_id)
        await keeper.tick()
        assert await redis.zscore(adm.seats_key, grant.lease_id) == before

    @pytest.mark.asyncio
    async def test_a_lost_lease_closes_the_browser_it_was_counting_on(self, redis):
        adm = _admission(redis, renew_interval_seconds=0)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm) as grant:
            pool.park()
        # The host gave those units away (an outage past the lease TTL, a purge).
        await redis.zrem(adm.seats_key, grant.lease_id)

        await keeper.tick()

        assert pool.closed == 1
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_a_lost_lease_closes_that_browser_not_the_oldest(self, redis):
        """Closing the oldest instead would leave a browser the host no longer
        counts parked, and hand back a seat that was still valid."""
        adm = _admission(redis, renew_interval_seconds=0)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm, domain="a.example"):
            oldest = pool.park()
        async with _claim(adm, domain="b.example") as grant:
            pool.park()
        await redis.zrem(adm.seats_key, grant.lease_id)

        await keeper.tick()

        assert pool.closed_seats == [grant.lease_id]
        assert pool.parked == [oldest]
        assert (await adm.snapshot()).in_use == 1.0

    @pytest.mark.asyncio
    async def test_an_idle_browser_is_handed_back_when_someone_is_waiting(self, redis):
        adm = _admission(redis, idle_grace_seconds=0.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            pool.park()
        # Someone in the host's line: a waiter with a live expiry.
        await redis.zadd(adm.waiter_exp_key, {"other": (time.time() + 60) * 1000})

        await keeper.tick()

        assert pool.closed == 1
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_a_freshly_parked_browser_survives_its_grace_period(self, redis):
        adm = _admission(redis, idle_grace_seconds=30.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            pool.park()
        await redis.zadd(adm.waiter_exp_key, {"other": (time.time() + 60) * 1000})

        await keeper.tick()

        assert pool.closed == 0
        assert (await adm.snapshot()).in_use == 1.0

    @pytest.mark.asyncio
    async def test_nobody_waiting_keeps_the_browser_warm(self, redis):
        adm = _admission(redis, idle_grace_seconds=0.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            pool.park()

        await keeper.tick()

        assert pool.closed == 0
        assert (await adm.snapshot()).in_use == 1.0

    @pytest.mark.asyncio
    async def test_an_idle_browser_does_not_hold_its_seat_forever(self, redis):
        adm = _admission(redis, idle_seat_seconds=0.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            pool.park()

        await keeper.tick()

        assert pool.closed == 1
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_a_pool_with_nothing_parked_says_so(self, redis):
        """Every instance is mid-fetch: real contention, nothing to hand back."""
        adm = _admission(redis, idle_grace_seconds=0.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            keeper.retain()  # retained, but no pool tracks it
        await redis.zadd(adm.waiter_exp_key, {"other": (time.time() + 60) * 1000})

        await keeper.tick()

        assert pool.closed == 0
        assert (await adm.snapshot()).in_use == 1.0

    @pytest.mark.asyncio
    async def test_a_tick_with_nothing_held_does_nothing(self, redis):
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)
        await keeper.tick()
        assert (await adm.snapshot()).in_use == 0.0

    @pytest.mark.asyncio
    async def test_redis_down_while_checking_for_waiters_keeps_the_browser(self, redis):
        adm = _admission(redis, idle_grace_seconds=0.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            pool.park()

        async def boom():
            raise AdmissionUnavailableError("redis down")

        adm.snapshot = boom  # type: ignore[method-assign]
        await keeper.tick()
        assert pool.closed == 0

    @pytest.mark.asyncio
    async def test_a_renewal_that_cannot_reach_redis_is_retried_next_tick(self, redis):
        adm = _admission(redis, renew_interval_seconds=0)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm):
            keeper.retain()

        async def boom(*_args, **_kwargs):
            raise AdmissionUnavailableError("redis down")

        adm.renew_lease = boom  # type: ignore[method-assign]
        await keeper.tick()
        assert (await adm.snapshot()).in_use == 1.0

    @pytest.mark.asyncio
    async def test_run_ticks_until_it_is_stopped(self, redis):
        adm = _admission(redis, idle_seat_seconds=0.0, renew_interval_seconds=600)
        keeper = SeatKeeper(adm, adm._cfg)
        pool = _FakePool(keeper)
        keeper.register_reclaimer(pool.close_one)
        async with _claim(adm):
            pool.park()

        stop = asyncio.Event()
        task = asyncio.create_task(keeper.run(stop))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if pool.closed:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert pool.closed == 1

    @pytest.mark.asyncio
    async def test_run_survives_an_admission_error(self, redis):
        adm = _admission(redis, renew_interval_seconds=0)
        keeper = SeatKeeper(adm, adm._cfg)
        async with _claim(adm):
            keeper.retain()

        calls: list[int] = []

        async def boom(*_args, **_kwargs):
            calls.append(1)
            raise AdmissionUnavailableError("redis down")

        adm.renew_lease = boom  # type: ignore[method-assign]
        adm.snapshot = boom  # type: ignore[method-assign]
        stop = asyncio.Event()
        task = asyncio.create_task(keeper.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert calls


class TestBrowserMemoryReports:
    @pytest.mark.asyncio
    async def test_one_browser_is_not_enough_to_size_a_host(self, redis):
        adm = _admission(redis, browser_memory_mb=1200)
        await adm.report_browser_memory("w1", BrowserMemorySample(count=1, mean_mb=500.0))
        assert await adm.browser_memory_mb(60) == 1200.0

    @pytest.mark.asyncio
    async def test_every_worker_s_browsers_are_averaged(self, redis):
        adm = _admission(redis, browser_memory_mb=1200, min_browser_memory_mb=100)
        await adm.report_browser_memory("w1", BrowserMemorySample(count=1, mean_mb=400.0))
        await adm.report_browser_memory("w2", BrowserMemorySample(count=3, mean_mb=800.0))
        # (400 + 3*800) / 4
        assert await adm.browser_memory_mb(60) == 700.0

    @pytest.mark.asyncio
    async def test_a_measurement_is_clamped_to_the_configured_bounds(self, redis):
        adm = _admission(redis, browser_memory_mb=1200, min_browser_memory_mb=400)
        await adm.report_browser_memory("w1", BrowserMemorySample(count=2, mean_mb=4000.0))
        assert await adm.browser_memory_mb(60) == 1200.0
        await adm.report_browser_memory("w1", BrowserMemorySample(count=2, mean_mb=50.0))
        assert await adm.browser_memory_mb(60) == 400.0

    @pytest.mark.asyncio
    async def test_a_worker_that_has_gone_quiet_is_dropped(self, redis):
        adm = _admission(redis, browser_memory_mb=1200)
        await adm.report_browser_memory("gone", BrowserMemorySample(count=4, mean_mb=300.0))
        assert await adm.browser_memory_mb(-1) == 1200.0
        assert await redis.hgetall(adm.browser_rss_key) == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_report_is_dropped(self, redis):
        adm = _admission(redis, browser_memory_mb=1200)
        await redis.hset(adm.browser_rss_key, "w1", "not-json")
        assert await adm.browser_memory_mb(60) == 1200.0
        assert await redis.hgetall(adm.browser_rss_key) == {}

    @pytest.mark.asyncio
    async def test_reporting_never_raises_at_the_caller(self, redis):
        adm = _admission(redis)
        adm._redis = None  # every call on it raises
        await adm.report_browser_memory("w1", BrowserMemorySample(count=1, mean_mb=1.0))

    @pytest.mark.asyncio
    async def test_the_keeper_publishes_what_its_browsers_weigh(self, redis, monkeypatch):
        adm = _admission(redis, controller_interval_seconds=5)
        keeper = SeatKeeper(adm, adm._cfg)
        monkeypatch.setattr(
            "scraper_engine.core.browser_rss.sample_browser_rss",
            lambda: BrowserMemorySample(count=2, mean_mb=640.0),
        )
        await keeper.tick()
        reports = await redis.hgetall(adm.browser_rss_key)
        assert len(reports) == 1
        # ...and not again until the next controller interval.
        await keeper.tick()
        assert await redis.hgetall(adm.browser_rss_key) == reports

    @pytest.mark.asyncio
    async def test_nothing_is_published_when_there_are_no_browsers(self, redis, monkeypatch):
        adm = _admission(redis)
        keeper = SeatKeeper(adm, adm._cfg)
        monkeypatch.setattr(
            "scraper_engine.core.browser_rss.sample_browser_rss", lambda: None
        )
        await keeper.tick()
        assert await redis.hgetall(adm.browser_rss_key) == {}
