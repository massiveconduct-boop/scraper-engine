# api/health.py
"""Composite health check endpoint.

GET /v1/health returns infrastructure health status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scraper_engine.core.periodic import heartbeat_key

if TYPE_CHECKING:
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient
    from scraper_engine.storage.s3_client import S3Client

# Which periodic jobs (core/periodic.py::run_periodic's `name` param) each
# supervised daemon process (docker/supervisord.conf) owns — round 35's
# self-healing daemons all live inside the api container now, see
# .claude/knowledge/architecture.md -> "Container Topology (Round 35)".
_DAEMON_JOBS: dict[str, tuple[str, ...]] = {
    "proxy-harvester": ("harvest", "promotion", "health", "pool_health", "retention"),
    "dlq-reaper": ("dlq_reap",),
    "webhook-sweeper": ("webhook_sweep",),
}


@dataclass
class HealthStatus:
    healthy: bool = False
    proxy_pool_size: int = 0
    pgbouncer_reachable: bool = False
    redis_reachable: bool = False
    s3_reachable: bool = False
    daemons: dict[str, str] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)


async def _check_daemon_liveness(redis: RedisClient) -> dict[str, str]:
    """Per-daemon liveness from core/periodic.py's Redis heartbeats.

    A daemon is "healthy" only if every one of its jobs' heartbeat keys is
    present — Redis's own TTL expiry IS the staleness detector (no manual
    timestamp/age math, no clock-skew risk). A read failure for one daemon
    reports "unknown" rather than raising, so a single bad Redis read can't
    take down the whole health endpoint.
    """
    statuses: dict[str, str] = {}
    for daemon, jobs in _DAEMON_JOBS.items():
        try:
            stale = [job for job in jobs if await redis.raw.get(heartbeat_key(job)) is None]
        except Exception as e:
            statuses[daemon] = f"unknown: {e}"
            continue
        statuses[daemon] = "healthy" if not stale else f"stale ({', '.join(stale)})"
    return statuses


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

        # Informational only -- does NOT affect `healthy`/the HTTP status
        # code below. Folding it in looked appealing (surface daemon death
        # through the same 200-vs-503 signal a passive monitor already
        # watches) but breaks on any legitimate cold start: the slowest
        # daemon job (promotion, 900s by default) hasn't written its first
        # heartbeat yet for up to 15 minutes after a fresh deploy, and this
        # same api process is routinely run/tested standalone (e.g. this
        # module's own integration test) with no co-located daemons at
        # all -- both would spuriously 503 an otherwise-healthy api. A
        # health check that false-positives on ordinary startup/topology
        # variance is itself a robustness bug, not a robustness fix.
        status.daemons = await _check_daemon_liveness(self._redis)
        unhealthy_daemons = {k: v for k, v in status.daemons.items() if v != "healthy"}
        if unhealthy_daemons:
            status.checks["daemons"] = "; ".join(
                f"{k}: {v}" for k, v in unhealthy_daemons.items()
            )

        status.healthy = healthy
        return status


async def check_health(
    pg: PostgresClient,
    redis: RedisClient,
    s3: S3Client | None = None,
) -> HealthStatus:
    """Convenience function for FastAPI/CLI — runs the real composite health check."""
    return await HealthChecker(pg, redis, s3).check()
