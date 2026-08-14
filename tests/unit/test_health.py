# tests/unit/test_health.py
"""api/health.py — was fully dead code (HealthChecker/check_health never
called); GET /health hardcoded {"status": "ok"}. Also fixes a real bug in
the dead code: s3_reachable was set True unconditionally with no real call."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.api.health import HealthChecker, _check_daemon_liveness, check_health


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


@pytest.mark.asyncio
async def test_check_health_redis_unreachable_marks_unhealthy_and_pool_size_unknown():
    """redis.get() backs both the health:ping check and the
    metrics:proxy_pool_size read — a single Redis outage must fail both
    independent try/except blocks, not just the first one reached."""
    pg = AsyncMock()
    redis = AsyncMock()
    redis.get.side_effect = Exception("connection refused")

    status = await check_health(pg, redis)

    assert status.redis_reachable is False
    assert status.healthy is False
    assert "redis" in status.checks
    assert status.proxy_pool_size == -1


@pytest.mark.asyncio
async def test_check_daemon_liveness_all_heartbeats_present():
    redis = AsyncMock()
    redis.raw.get.return_value = "1234567890"  # any non-None = heartbeat present

    statuses = await _check_daemon_liveness(redis)

    assert statuses == {
        "proxy-harvester": "healthy",
        "dlq-reaper": "healthy",
        "webhook-sweeper": "healthy",
    }


@pytest.mark.asyncio
async def test_check_daemon_liveness_reports_stale_jobs_by_name():
    redis = AsyncMock()

    async def fake_get(key: str) -> str | None:
        # "harvest" job's heartbeat expired; every other job's is present.
        return None if key == "heartbeat:harvest" else "1234567890"

    redis.raw.get.side_effect = fake_get

    statuses = await _check_daemon_liveness(redis)

    assert statuses["proxy-harvester"] == "stale (harvest)"
    assert statuses["dlq-reaper"] == "healthy"
    assert statuses["webhook-sweeper"] == "healthy"


@pytest.mark.asyncio
async def test_check_daemon_liveness_redis_error_reports_unknown_not_raise():
    redis = AsyncMock()
    redis.raw.get.side_effect = ConnectionError("redis unreachable")

    statuses = await _check_daemon_liveness(redis)

    assert statuses["proxy-harvester"].startswith("unknown:")
    assert statuses["dlq-reaper"].startswith("unknown:")
    assert statuses["webhook-sweeper"].startswith("unknown:")


@pytest.mark.asyncio
async def test_check_health_all_daemons_healthy_does_not_affect_overall_status():
    pg = AsyncMock()
    redis = AsyncMock()
    redis.get.return_value = "5"
    redis.raw.get.return_value = "1234567890"
    s3 = AsyncMock()

    status = await check_health(pg, redis, s3)

    assert status.healthy is True
    assert status.daemons == {
        "proxy-harvester": "healthy",
        "dlq-reaper": "healthy",
        "webhook-sweeper": "healthy",
    }
    assert "daemons" not in status.checks


@pytest.mark.asyncio
async def test_check_health_stale_daemon_is_informational_only():
    """Daemon liveness must NOT flip the overall healthy/HTTP status —
    the slowest daemon job (promotion, 900s by default) hasn't written
    its first heartbeat for up to 15 minutes after any fresh deploy, and
    this same api process is routinely run standalone (e.g. this
    module's own integration test) with no co-located daemons at all;
    folding daemon staleness into `healthy` would 503 both of those
    legitimate cases. Surfaced via `daemons`/`checks` for an operator to
    read, without affecting whether the api itself is fit to serve
    traffic."""
    pg = AsyncMock()
    redis = AsyncMock()
    redis.get.return_value = "5"
    redis.raw.get.return_value = None  # every job's heartbeat expired
    s3 = AsyncMock()

    status = await check_health(pg, redis, s3)

    assert status.healthy is True
    assert status.daemons["proxy-harvester"].startswith("stale")
    assert "daemons" in status.checks
