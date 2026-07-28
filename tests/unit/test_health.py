# tests/unit/test_health.py
"""api/health.py — was fully dead code (HealthChecker/check_health never
called); GET /health hardcoded {"status": "ok"}. Also fixes a real bug in
the dead code: s3_reachable was set True unconditionally with no real call."""

from unittest.mock import AsyncMock

import pytest

from api.health import HealthChecker, check_health


@pytest.mark.asyncio
async def test_check_health_all_reachable():
    pg = AsyncMock()
    redis = AsyncMock()
    redis.get.return_value = "5"
    s3 = AsyncMock()

    status = await check_health(pg, redis, s3)

    assert status.healthy is True
    assert status.pgbouncer_reachable is True
    assert status.redis_reachable is True
    assert status.s3_reachable is True
    assert status.proxy_pool_size == 5
    s3.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_health_s3_unreachable_marks_unhealthy():
    """The bug this fixes: previously s3_reachable=True was set unconditionally
    with no real S3 call inside the try block, so a dead bucket never failed
    the health check."""
    pg = AsyncMock()
    redis = AsyncMock()
    s3 = AsyncMock()
    s3.ping.side_effect = Exception("connection refused")

    status = await HealthChecker(pg, redis, s3).check()

    assert status.s3_reachable is False
    assert status.healthy is False
    assert "s3" in status.checks


@pytest.mark.asyncio
async def test_check_health_without_s3_configured_does_not_fail_on_it():
    pg = AsyncMock()
    redis = AsyncMock()

    status = await check_health(pg, redis, s3=None)

    assert status.s3_reachable is True  # not configured — shouldn't count against health
    assert status.healthy is True


@pytest.mark.asyncio
async def test_check_health_pg_unreachable_marks_unhealthy():
    pg = AsyncMock()
    pg.fetchrow.side_effect = Exception("connection refused")
    redis = AsyncMock()

    status = await check_health(pg, redis)

    assert status.pgbouncer_reachable is False
    assert status.healthy is False
