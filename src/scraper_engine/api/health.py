# api/health.py
"""Composite health check endpoint.

GET /v1/health returns infrastructure health status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient
    from scraper_engine.storage.s3_client import S3Client


@dataclass
class HealthStatus:
    healthy: bool = False
    proxy_pool_size: int = 0
    pgbouncer_reachable: bool = False
    redis_reachable: bool = False
    s3_reachable: bool = False
    checks: dict[str, str] = field(default_factory=dict)


class HealthChecker:
    """Composite health checker covering all infrastructure dependencies."""

    def __init__(
        self,
        pg: PostgresClient,
        redis: RedisClient,
        s3: S3Client | None = None,
    ) -> None:
        self._pg = pg
        self._redis = redis
        self._s3 = s3

    async def check(self) -> HealthStatus:
        """Run all health checks and return composite status."""
        status = HealthStatus()
        healthy = True

        try:
            from scraper_engine.core.tenant import TenantId

            await self._pg.fetchrow(TenantId("system"), "SELECT 1")
            status.pgbouncer_reachable = True
        except Exception as e:
            status.checks["pgbouncer"] = str(e)
            healthy = False

        try:
            from scraper_engine.core.tenant import TenantId

            await self._redis.get(TenantId("system"), "health:ping")
            status.redis_reachable = True
        except Exception as e:
            status.checks["redis"] = str(e)
            healthy = False

        if self._s3 is not None:
            try:
                await self._s3.ping()
                status.s3_reachable = True
            except Exception as e:
                status.checks["s3"] = str(e)
                healthy = False
        else:
            status.s3_reachable = True  # not configured for this check — don't fail on it

        try:
            from scraper_engine.core.tenant import TenantId

            raw = await self._redis.get(TenantId("system"), "metrics:proxy_pool_size")
            status.proxy_pool_size = int(raw) if raw else 0
        except Exception:
            status.proxy_pool_size = -1

        status.healthy = healthy
        return status


async def check_health(
    pg: PostgresClient,
    redis: RedisClient,
    s3: S3Client | None = None,
) -> HealthStatus:
    """Convenience function for FastAPI/CLI — runs the real composite health check."""
    return await HealthChecker(pg, redis, s3).check()
