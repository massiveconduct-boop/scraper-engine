# tests/integration/test_politeness.py
"""Politeness controller integration tests — Lua scripts with mocked eval."""

from unittest.mock import AsyncMock

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
    async def test_wait_if_needed_no_delay_first_time(self, tenant):
        redis = AsyncMock()
        redis.get.return_value = None  # no previous fetch
        redis.set.return_value = True
        pc = PolitenessController(redis=redis, default_delay_seconds=999.0)
        import time

        start = time.monotonic()
        await pc.wait_if_needed("first.com", tenant)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_wait_if_needed_enforces_delay(self, tenant):
        redis = AsyncMock()
        # Set last fetch to just now, so the next call must wait
        import time

        redis.get.return_value = str(time.monotonic())
        redis.set.return_value = True
        pc = PolitenessController(redis=redis, default_delay_seconds=0.05)
        start = time.monotonic()
        await pc.wait_if_needed("delayed.com", tenant)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04  # Should have waited approximately the delay
