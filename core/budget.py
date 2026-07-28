# core/budget.py
"""Global resource governance — semaphores + CapSolver spend ceilings.

Closes F-14/F-13/F-12: all resource acquisitions bounded by explicit ceilings.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient

# Global semaphores — tune per host RAM. Sized from config.camoufox.max_total_instances
# and config.capsolver.max_concurrent_solves at process startup via configure_budget();
# these defaults only apply if a process never calls it (e.g. a unit test importing
# this module directly).
# Measured 2026-07-22: Camoufox v152 per-instance RSS = 80.1MB (virtual headless)
# 8 × 80MB ≈ 640MB, conservative for typical 4GB VPS
BROWSER_SEMAPHORE = asyncio.Semaphore(8)

# Bounds outstanding CAPTCHA long-poll tasks, preventing FD exhaustion (F-13)
CAPSOLVER_CONCURRENCY = asyncio.Semaphore(10)


def configure_budget(
    *, browser_max_total_instances: int, capsolver_max_concurrent_solves: int
) -> None:
    """Resize the process-wide semaphores from config. Call once at process
    startup, before any fetch/solve begins — resizing after acquisition would
    change the ceiling underneath in-flight holders.

    Consumers must reference `core.budget.BROWSER_SEMAPHORE` /
    `core.budget.CAPSOLVER_CONCURRENCY` through the module (not
    `from core.budget import BROWSER_SEMAPHORE`) so this reassignment is
    visible to them — a name bound at import time would keep pointing at the
    original object.
    """
    global BROWSER_SEMAPHORE, CAPSOLVER_CONCURRENCY
    BROWSER_SEMAPHORE = asyncio.Semaphore(browser_max_total_instances)
    CAPSOLVER_CONCURRENCY = asyncio.Semaphore(capsolver_max_concurrent_solves)


class CapSolverBudget:
    """Per-tenant, per-day spend ceiling; hard-stops new solve tasks once exceeded (closes F-12).

    Enforces $1.00/day default ceiling per BD-03, overridable per tenant via
    the `tenants.capsolver_daily_credit_ceiling` column (read through `pg`,
    short-cached in-process — round 25, was previously written at tenant
    creation but never read back).
    """

    DEFAULT_DAILY_CEILING = 1.0  # $1.00/day [CONFIRMED — BD-03]
    _CEILING_CACHE_TTL_SECONDS = 300.0

    def __init__(
        self,
        redis: RedisClient,
        pg: PostgresClient | None = None,
        daily_ceiling_credits: float | None = None,
    ) -> None:
        self._redis = redis
        self._pg = pg
        # Explicit override always wins (tests, or a caller that already knows
        # the ceiling) — skips the per-tenant DB lookup entirely.
        self._fixed_ceiling = daily_ceiling_credits
        self._ceiling_cache: dict[str, tuple[float, float]] = {}

    def _spend_key(self, tenant_id: TenantId) -> str:
        return f"capsolver:daily_spend:{tenant_id}"

    async def _get_ceiling(self, tenant_id: TenantId) -> float:
        if self._fixed_ceiling is not None:
            return self._fixed_ceiling
        if self._pg is None:
            return self.DEFAULT_DAILY_CEILING
        cache_key = str(tenant_id)
        cached = self._ceiling_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self._CEILING_CACHE_TTL_SECONDS:
            return cached[0]
        rows = await self._pg.fetch(
            tenant_id,
            "SELECT capsolver_daily_credit_ceiling FROM public.tenants WHERE tenant_id = $1",
            cache_key,
        )
        ceiling = (
            float(rows[0]["capsolver_daily_credit_ceiling"])
            if rows and rows[0]["capsolver_daily_credit_ceiling"] is not None
            else self.DEFAULT_DAILY_CEILING
        )
        self._ceiling_cache[cache_key] = (ceiling, now)
        return ceiling

    async def check_and_reserve(self, tenant_id: TenantId, estimated_cost: float) -> bool:
        """Check if tenant has remaining budget and atomically reserve it.

        Returns True if the spend was within budget and has been reserved.
        Returns False if the ceiling would be exceeded.
        """
        ceiling = await self._get_ceiling(tenant_id)
        # Use Redis Lua for atomic check-and-increment
        result = await self._redis.eval(
            """
            local key = KEYS[1]
            local ceiling = tonumber(ARGV[1])
            local cost = tonumber(ARGV[2])
            local ttl = tonumber(ARGV[3])
            local current = tonumber(redis.call('GET', key) or '0')
            if current + cost > ceiling then
                return 0
            end
            redis.call('INCRBYFLOAT', key, cost)
            redis.call('EXPIRE', key, ttl)
            return 1
            """,
            1,
            self._spend_key(tenant_id),
            ceiling,
            estimated_cost,
            86400,
        )
        return bool(result)

    async def current_spend(self, tenant_id: TenantId) -> float:
        """Return current daily spend for the tenant."""
        raw = await self._redis.get(tenant_id, self._spend_key(tenant_id))
        return float(raw) if raw else 0.0

    async def remaining(self, tenant_id: TenantId) -> float:
        """Return remaining daily budget for the tenant."""
        ceiling = await self._get_ceiling(tenant_id)
        return max(0.0, ceiling - await self.current_spend(tenant_id))
