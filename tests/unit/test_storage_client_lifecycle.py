"""Round 64 (branch burn-down) — stop() on a client that never started.

A lifespan that fails before start() (or a test that never starts one) still
runs the shutdown path; stop() must be a no-op there, not an AttributeError.
"""

import pytest

from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient


@pytest.mark.asyncio
async def test_postgres_stop_before_start_is_a_noop():
    await PostgresClient("postgresql://unused/unused").stop()


@pytest.mark.asyncio
async def test_redis_stop_before_start_is_a_noop():
    await RedisClient(redis_url="redis://unused:6379/0").stop()
