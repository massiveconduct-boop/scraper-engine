# tests/integration/test_politeness.py
"""Politeness controller integration tests — Lua scripts with mocked eval."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.orchestrator.politeness import PolitenessController


@pytest.fixture
def tenant():
    return TenantId("testtenant")


class TestPoliteness:
    @pytest.mark.asyncio
    async def test_acquire_slot_succeeds_under_limit(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1  # slot acquired
        pc = PolitenessController(redis=redis, default_concurrency=2)
        result = await pc.acquire_slot("example.com", tenant)
        assert result is not None
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_acquire_slot_fails_at_limit(self, tenant):
        redis = AsyncMock()
        redis.eval.side_effect = [1, 0]  # first succeeds, second fails
        pc = PolitenessController(redis=redis, default_concurrency=1)
        assert await pc.acquire_slot("busy.com", tenant) is not None
        assert await pc.acquire_slot("busy.com", tenant) is None

    @pytest.mark.asyncio
    async def test_release_slot_releases_the_acquired_worker_id(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1
        pc = PolitenessController(redis=redis, default_concurrency=2)
        worker_id = await pc.acquire_slot("example.com", tenant)
        assert worker_id is not None

        await pc.release_slot("example.com", tenant, worker_id)

        release_call = redis.eval.call_args
        assert worker_id in release_call.args

    @pytest.mark.asyncio
    async def test_acquire_slot_honours_per_request_concurrency(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1
        pc = PolitenessController(redis=redis, default_concurrency=2)

        await pc.acquire_slot("example.com", tenant, concurrency=9)

        assert 9 in redis.eval.call_args.args
        assert 2 not in redis.eval.call_args.args

    @pytest.mark.asyncio
    async def test_wait_if_needed_no_delay_first_time(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 0  # reservation granted immediately
        pc = PolitenessController(redis=redis, default_delay_seconds=999.0)
        import time

        start = time.monotonic()
        slept_ms = await pc.wait_if_needed("first.com", tenant)
        elapsed = time.monotonic() - start
        assert slept_ms == 0
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_wait_if_needed_enforces_delay_and_reports_it(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 50  # the reservation is 50ms out
        pc = PolitenessController(redis=redis, default_delay_seconds=0.05)
        import time

        start = time.monotonic()
        slept_ms = await pc.wait_if_needed("delayed.com", tenant)
        elapsed = time.monotonic() - start
        assert slept_ms == 50
        assert elapsed >= 0.04  # Should have waited approximately the delay

    @pytest.mark.asyncio
    async def test_wait_if_needed_honours_per_request_delay(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 0
        pc = PolitenessController(redis=redis, default_delay_seconds=10.0)

        await pc.wait_if_needed("example.com", tenant, delay_seconds=0.25)

        assert 250 in redis.eval.call_args.args

    @pytest.mark.asyncio
    async def test_wait_if_needed_skips_redis_entirely_at_zero_delay(self, tenant):
        redis = AsyncMock()
        pc = PolitenessController(redis=redis, default_delay_seconds=0.0)

        assert await pc.wait_if_needed("example.com", tenant) == 0
        redis.eval.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_slot_reports_whether_the_slot_is_still_held(self, tenant):
        redis = AsyncMock()
        redis.eval.side_effect = [1, 0]
        pc = PolitenessController(redis=redis, slot_ttl_seconds=120)

        assert await pc.refresh_slot("example.com", tenant, "abc123") is True
        assert await pc.refresh_slot("example.com", tenant, "abc123") is False

    @pytest.mark.asyncio
    async def test_held_slot_refreshes_then_releases(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1
        # slot_ttl 3 -> the heartbeat interval floors at 1.0s, so shorten the
        # wait by patching sleep rather than by sleeping for real.
        pc = PolitenessController(redis=redis, slot_ttl_seconds=3)

        real_sleep = asyncio.sleep

        async def _fast_sleep(seconds):
            await real_sleep(0.01)

        with patch.object(asyncio, "sleep", _fast_sleep):
            async with pc.held_slot("example.com", tenant, "abc123"):
                await real_sleep(0.05)

        scripts = [call.args[0] for call in redis.eval.call_args_list]
        assert any("SISMEMBER" in s for s in scripts), "slot TTL was never refreshed"
        assert "SREM" in scripts[-1], "slot was not released on exit"

    @pytest.mark.asyncio
    async def test_held_slot_releases_even_when_the_body_raises(self, tenant):
        redis = AsyncMock()
        redis.eval.return_value = 1
        pc = PolitenessController(redis=redis, slot_ttl_seconds=120)

        with pytest.raises(RuntimeError, match="boom"):
            async with pc.held_slot("example.com", tenant, "abc123"):
                raise RuntimeError("boom")

        assert "SREM" in redis.eval.call_args_list[-1].args[0]

    @pytest.mark.asyncio
    async def test_held_slot_survives_a_failing_refresh(self, tenant):
        redis = AsyncMock()
        redis.eval.side_effect = ConnectionError("redis down")
        pc = PolitenessController(redis=redis, slot_ttl_seconds=3)

        real_sleep = asyncio.sleep

        async def _fast_sleep(seconds):
            await real_sleep(0.01)

        with patch.object(asyncio, "sleep", _fast_sleep), contextlib.suppress(ConnectionError):
            async with pc.held_slot("example.com", tenant, "abc123"):
                await real_sleep(0.05)
