# tests/integration/test_host_capacity.py
"""orchestrator/host_capacity.py against REAL Redis (round 65).

The claim rules live in Lua, which Python coverage cannot see, so every rule
gets an explicit case here: seat limit, first-render-always-admitted,
full-website skip, no jumping an older eligible waiter, per-site delay,
tenant share cap, lease and waiter expiry, self-claim only, timeout,
cancellation, nesting, renewal and the renewal cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from scraper_engine.config.schema import HostCapacityConfig
from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator import host_capacity
from scraper_engine.orchestrator.host_capacity import (
    CLAIM_LUA,
    AdmissionCancelledError,
    AdmissionTimeoutError,
    AdmissionUnavailableError,
    HostAdmission,
    NestedClaimError,
)
from scraper_engine.orchestrator.politeness import delay_key, slot_key

pytestmark = pytest.mark.integration

T1 = TenantId("hctest1")
T2 = TenantId("hctest2")


@pytest.fixture
async def redis():
    r = Redis(host="localhost", port=6379, decode_responses=True)
    yield r
    for pattern in ("hc:hctest-*", "politeness:*:hctest*"):
        keys = [k async for k in r.scan_iter(match=pattern, count=1000)]
        if keys:
            await r.delete(*keys)
    await r.aclose()


def _cfg(**overrides):
    base = HostCapacityConfig(
        enabled=True,
        default_units=2.0,
        poll_min_seconds=0.01,
        poll_max_seconds=0.02,
        cancel_check_interval_seconds=0.01,
    )
    # model_copy skips validation, so tests may use sub-second lifetimes.
    return base.model_copy(update=overrides)


def _admission(redis, **overrides) -> HostAdmission:
    return HostAdmission(redis, f"hctest-{uuid.uuid4().hex[:8]}", _cfg(**overrides))


def _claim(
    adm,
    *,
    tenant=T1,
    domain="a.example",
    weight=1.0,
    cap=5,
    delay=0.0,
    priority=None,
    budget=0.0,
    is_cancelled=None,
):
    return adm.claim(
        tenant_id=tenant,
        domain=domain,
        weight=weight,
        concurrency=cap,
        delay_seconds=delay,
        priority_ms=priority if priority is not None else int(time.time() * 1000),
        wait_budget_seconds=budget,
        is_cancelled=is_cancelled,
    )


class _Holder:
    """Holds one claim in its own task, the way concurrent URL tasks do —
    one task may never hold two claims (NestedClaimError)."""

    def __init__(self, adm, **kwargs):
        self._adm, self._kwargs = adm, kwargs
        self._release = asyncio.Event()
        self._granted: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            async with _claim(self._adm, **self._kwargs) as grant:
                self._granted.set_result(grant)
                await self._release.wait()
        except BaseException as exc:
            if not self._granted.done():
                self._granted.set_exception(exc)
            raise

    async def grant(self):
        return await self._granted

    async def release(self):
        self._release.set()
        await self._task


@contextlib.asynccontextmanager
async def _holding(adm, *claims):
    holders = [_Holder(adm, **kw) for kw in claims]
    try:
        yield [await h.grant() for h in holders]
    finally:
        for h in holders:
            await h.release()


async def _enqueue_waiter(
    adm, *, waiter_id, priority, tenant=T1, domain="a.example", weight=1.0, cap=5, delay_ms=0
):
    """One raw CLAIM call that leaves the waiter in line when not granted —
    stands in for a live waiter that is between polls."""
    info = json.dumps(
        {
            "tenant": str(tenant),
            "slot_key": slot_key(domain, tenant),
            "delay_key": delay_key(domain, tenant),
            "cap": cap,
            "delay_ms": delay_ms,
            "weight": weight,
        }
    )
    keys = [
        adm.seats_key,
        adm.seat_weight_key,
        adm.seat_tenant_key,
        adm.waiters_key,
        adm.waiter_exp_key,
        adm.waiter_info_key,
        adm.target_key,
        adm.stats_key,
    ]
    reply = await adm._eval(
        CLAIM_LUA,
        keys,
        [
            waiter_id,
            priority,
            info,
            f"lease-{waiter_id}",
            adm._cfg.lease_ttl_seconds * 1000,
            adm._cfg.waiter_ttl_seconds * 1000,
            adm.default_target,
            adm._cfg.tenant_share,
            3_600_000,
            0,
            "1000",
        ],
    )
    return reply[0] == "1"


class TestGrantAndRelease:
    @pytest.mark.asyncio
    async def test_grant_takes_seat_and_slot_and_release_frees_both(self, redis):
        adm = _admission(redis)
        async with _claim(adm) as grant:
            assert grant.in_use == 1.0 and grant.target == 2.0
            assert await redis.zscore(adm.seats_key, grant.lease_id) is not None
            assert await redis.zscore(slot_key("a.example", T1), grant.lease_id) is not None
            snap = await adm.snapshot()
            assert (snap.in_use, snap.waiters, snap.target) == (1.0, 0, 2.0)
        assert await redis.zcard(adm.seats_key) == 0
        assert await redis.zcard(slot_key("a.example", T1)) == 0
        stats = await redis.hgetall(adm.stats_key)
        assert stats["granted"] == "1" and stats["wait_le_1000"] == "1"

    @pytest.mark.asyncio
    async def test_target_bounds_units_and_a_release_admits_the_next(self, redis):
        adm = _admission(redis)
        first = _Holder(adm, domain="a.example")
        second = _Holder(adm, domain="b.example")
        await first.grant()
        await second.grant()
        with pytest.raises(AdmissionTimeoutError):
            async with _claim(adm, domain="c.example", budget=0.05):
                pass
        assert await redis.hget(adm.stats_key, "timeouts") == "1"
        # The timed-out waiter left the line.
        assert await redis.zcard(adm.waiters_key) == 0
        await second.release()
        async with _claim(adm, domain="c.example", budget=1.0) as third:
            assert third.in_use == 2.0
        await first.release()

    @pytest.mark.asyncio
    async def test_controller_target_overrides_the_default(self, redis):
        adm = _admission(redis)
        await redis.set(adm.target_key, "1")
        async with _holding(adm, {}):
            with pytest.raises(AdmissionTimeoutError):
                async with _claim(adm, domain="b.example", budget=0.05):
                    pass

    @pytest.mark.asyncio
    async def test_first_render_on_an_idle_host_is_admitted_whatever_its_weight(self, redis):
        adm = _admission(redis)
        async with _claim(adm, weight=5.0) as grant:
            assert grant.in_use == 5.0


class TestFairness:
    @pytest.mark.asyncio
    async def test_a_full_website_is_skipped_not_waited_behind(self, redis):
        adm = _admission(redis, default_units=4.0)
        async with _holding(adm, {"domain": "full.example", "cap": 1}):
            # Older waiter for the full site stays in line…
            assert not await _enqueue_waiter(
                adm, waiter_id="old", priority=1, domain="full.example", cap=1
            )
            # …and a newer one for another site is still served.
            async with _claim(adm, domain="free.example", priority=2) as grant:
                assert grant.in_use == 2.0

    @pytest.mark.asyncio
    async def test_an_older_eligible_waiter_is_not_jumped(self, redis):
        adm = _admission(redis, default_units=2.0)
        async with _holding(adm, {"domain": "x.example"}):
            # One seat left. An older waiter could take it right now…
            await redis.zadd(adm.waiters_key, {"old": 1})
            await redis.zadd(adm.waiter_exp_key, {"old": int(time.time() * 1000) + 60_000})
            await redis.hset(
                adm.waiter_info_key,
                "old",
                json.dumps(
                    {
                        "tenant": str(T1),
                        "slot_key": slot_key("o.example", T1),
                        "delay_key": delay_key("o.example", T1),
                        "cap": 5,
                        "delay_ms": 0,
                        "weight": 1.0,
                    }
                ),
            )
            # …so a newer caller is refused.
            with pytest.raises(AdmissionTimeoutError):
                async with _claim(adm, domain="y.example", priority=2, budget=0.05):
                    pass
            # The older waiter then gets it.
            assert await _enqueue_waiter(adm, waiter_id="old", priority=1, domain="o.example")

    @pytest.mark.asyncio
    async def test_delay_blocks_a_second_render_until_it_elapses(self, redis):
        adm = _admission(redis, default_units=4.0)
        async with _claim(adm, domain="slow.example", delay=0.3):
            pass
        with pytest.raises(AdmissionTimeoutError):
            async with _claim(adm, domain="slow.example", delay=0.3, budget=0.05):
                pass
        async with _claim(adm, domain="slow.example", delay=0.3, budget=2.0) as grant:
            assert grant.wait_ms >= 100
        nxt = int(await redis.get(delay_key("slow.example", T1)))
        assert nxt > int(time.time() * 1000)

    @pytest.mark.asyncio
    async def test_tenant_share_cap_applies_while_another_tenant_waits(self, redis):
        adm = _admission(redis, default_units=4.0, tenant_share=0.5)  # cap = 2
        # No other tenant waiting: T1 may go past its share.
        async with (
            _holding(
                adm,
                {"tenant": T1, "domain": "a.example"},
                {"tenant": T1, "domain": "b.example"},
                {"tenant": T1, "domain": "c.example"},
            ),
            _claim(adm, tenant=T2, domain="z.example"),
        ):
            pass
        async with _holding(
            adm,
            {"tenant": T1, "domain": "a.example"},
            {"tenant": T1, "domain": "b.example"},
            {"tenant": T2, "domain": "t2full.example", "cap": 1},
        ):
            # T2 is waiting (between polls, for a full site): T1 is capped at 2.
            assert not await _enqueue_waiter(
                adm, waiter_id="t2", priority=1, tenant=T2, domain="t2full.example", cap=1
            )
            with pytest.raises(AdmissionTimeoutError):
                async with _claim(adm, tenant=T1, domain="c.example", budget=0.05):
                    pass


class TestExpiry:
    @pytest.mark.asyncio
    async def test_a_crashed_holders_seat_lapses_and_is_reclaimed(self, redis):
        adm = _admission(redis, default_units=1.0, lease_ttl_seconds=1)
        # A "crashed" holder: granted, never renewed, never released.
        assert await _enqueue_waiter(adm, waiter_id="crashed", priority=1)
        with pytest.raises(AdmissionTimeoutError):
            async with _claim(adm, domain="b.example", budget=0.05):
                pass
        await asyncio.sleep(1.1)
        async with _claim(adm, domain="b.example") as grant:
            assert grant.in_use == 1.0
        assert "lease-crashed" not in await redis.hkeys(adm.seat_weight_key)

    @pytest.mark.asyncio
    async def test_a_dead_waiter_stops_blocking_the_line(self, redis):
        adm = _admission(redis, default_units=1.0, waiter_ttl_seconds=1)
        await redis.zadd(adm.waiters_key, {"dead": 1})
        await redis.zadd(adm.waiter_exp_key, {"dead": int(time.time() * 1000) + 300})
        await redis.hset(
            adm.waiter_info_key,
            "dead",
            json.dumps(
                {
                    "tenant": str(T1),
                    "slot_key": slot_key("d.example", T1),
                    "delay_key": delay_key("d.example", T1),
                    "cap": 5,
                    "delay_ms": 0,
                    "weight": 1.0,
                }
            ),
        )
        with pytest.raises(AdmissionTimeoutError):
            async with _claim(adm, priority=2, budget=0.05):
                pass
        await asyncio.sleep(0.4)
        async with _claim(adm, priority=2):
            pass
        assert await redis.zscore(adm.waiters_key, "dead") is None


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_nested_claim_is_refused(self, redis):
        adm = _admission(redis)
        async with _claim(adm):
            with pytest.raises(NestedClaimError):
                async with _claim(adm, domain="b.example"):
                    pass
        # A fresh claim after exit is fine.
        async with _claim(adm):
            pass

    @pytest.mark.asyncio
    async def test_cancellation_is_checked_while_waiting_and_leaves_the_line(self, redis):
        adm = _admission(redis, default_units=1.0)
        async with _holding(adm, {}):
            cancelled = AsyncMock(side_effect=[False, True])
            with pytest.raises(AdmissionCancelledError):
                async with _claim(adm, domain="b.example", budget=5.0, is_cancelled=cancelled):
                    pass
        assert await redis.zcard(adm.waiters_key) == 0

    @pytest.mark.asyncio
    async def test_task_cancellation_while_waiting_leaves_the_line(self, redis):
        adm = _admission(redis, default_units=1.0)
        async with _holding(adm, {}):

            async def wait_forever():
                async with _claim(adm, domain="b.example", budget=30.0):
                    pass

            task = asyncio.create_task(wait_forever())
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await redis.zcard(adm.waiters_key) == 0

    @staticmethod
    def _swallow_cancels(redis):
        """Make every EVAL absorb a cancellation that lands mid-call and return
        normally — what redis-py 8.0.1 was measured doing on Python 3.11 (round
        67: 7-9 of 120 waiter cancels lost, `eval` returned 2196 times with the
        task still `cancelling()`). Reproduces it on any Python version."""
        orig = redis.eval

        async def swallowing_eval(*args, **kwargs):
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.05)
            return await orig(*args, **kwargs)

        redis.eval = swallowing_eval

    @pytest.mark.asyncio
    async def test_a_cancel_swallowed_by_the_redis_client_still_ends_the_wait(self, redis):
        adm = _admission(redis, default_units=1.0)
        async with _holding(adm, {}):
            self._swallow_cancels(redis)

            async def wait_forever():
                async with _claim(adm, domain="b.example", budget=30.0):
                    pass

            task = asyncio.create_task(wait_forever())
            await asyncio.sleep(0.02)  # inside a (slowed) EVAL
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        assert await redis.zcard(adm.waiters_key) == 0

    @pytest.mark.asyncio
    async def test_a_cancel_swallowed_during_renewal_does_not_hang_the_release(self, redis):
        """claim()'s exit cancels its renewer and awaits it: a renewer that
        lost that cancel inside EVAL would keep renewing forever."""
        adm = _admission(redis, renew_interval_seconds=0.0)
        async with _claim(adm):
            self._swallow_cancels(redis)
            await asyncio.sleep(0.12)  # the renewer is mid-EVAL most of the time
        assert (await adm.snapshot()).in_use == 0

    @pytest.mark.asyncio
    async def test_renewal_keeps_a_long_hold_alive(self, redis):
        adm = _admission(redis, lease_ttl_seconds=1, renew_interval_seconds=0.2)
        async with _claim(adm) as grant:
            await asyncio.sleep(1.5)
            assert await redis.zscore(adm.seats_key, grant.lease_id) is not None
            assert await redis.zscore(slot_key("a.example", T1), grant.lease_id) is not None

    @pytest.mark.asyncio
    async def test_renewal_stops_at_the_hold_cap_without_cancelling_work(self, redis, caplog):
        adm = _admission(
            redis, lease_ttl_seconds=1, renew_interval_seconds=0.2, max_hold_seconds=0.3
        )
        async with _claim(adm) as grant:
            await asyncio.sleep(1.6)
            # The block is still running; only the lease lapsed (expired
            # members are dropped by the next claim, not the instant they lapse).
            expires_at = await redis.zscore(adm.seats_key, grant.lease_id)
            assert expires_at < time.time() * 1000
            assert (await adm.snapshot()).in_use == 0
        assert "seat_overheld" in caplog.text

    @pytest.mark.asyncio
    async def test_a_lost_lease_stops_renewal(self, redis, caplog):
        adm = _admission(redis, lease_ttl_seconds=5, renew_interval_seconds=0.1)
        async with _claim(adm) as grant:
            await redis.zrem(adm.seats_key, grant.lease_id)
            await asyncio.sleep(0.3)
        assert "host_claim_lease_lost" in caplog.text


class TestRedisFailures:
    @pytest.mark.asyncio
    async def test_redis_errors_surface_as_admission_unavailable(self):
        broken = AsyncMock()
        broken.eval.side_effect = ConnectionError("redis down")
        adm = HostAdmission(broken, "hctest-broken", _cfg())
        with pytest.raises(AdmissionUnavailableError):
            async with _claim(adm):
                pass
        with pytest.raises(AdmissionUnavailableError):
            await adm.snapshot()

    @pytest.mark.asyncio
    async def test_a_failed_release_or_renewal_is_logged_not_raised(self, redis, caplog):
        adm = _admission(redis, renew_interval_seconds=0.05)
        real_eval = redis.eval

        async def flaky(script, *args):
            if script in (host_capacity.RELEASE_LUA, host_capacity.RENEW_LUA):
                raise ConnectionError("blip")
            return await real_eval(script, *args)

        adm._redis = AsyncMock(wraps=redis)
        adm._redis.eval = flaky
        async with _claim(adm):
            await asyncio.sleep(0.15)
        assert "host_claim_renew_failed" in caplog.text
        assert "host_claim_release_failed" in caplog.text

    def test_wait_buckets(self):
        assert host_capacity._wait_bucket(0) == "1000"
        assert host_capacity._wait_bucket(4_000) == "5000"
        assert host_capacity._wait_bucket(10**9) == "inf"

    def test_default_target_follows_cpu_count_when_unset(self):
        cfg = HostCapacityConfig()
        assert HostAdmission(AsyncMock(), "h", cfg, cpu_count=6).default_target == 6.0
