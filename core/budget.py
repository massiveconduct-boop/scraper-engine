# core/budget.py
"""Global resource governance — semaphores + CapSolver spend ceilings.

Closes F-14/F-13/F-12: all resource acquisitions bounded by explicit ceilings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.redis_client import RedisClient

# Global semaphores — tune per host RAM
# Measured 2026-07-22: Camoufox v152 per-instance RSS = 80.1MB (virtual headless)
# 8 × 80MB ≈ 640MB, conservative for typical 4GB VPS
BROWSER_SEMAPHORE = asyncio.Semaphore(8)

# Bounds outstanding CAPTCHA long-poll tasks, preventing FD exhaustion (F-13)
CAPSOLVER_CONCURRENCY = asyncio.Semaphore(10)


class CapSolverBudget:
    """Per-tenant, per-day spend ceiling; hard-stops new solve tasks once exceeded (closes F-12).

    Enforces $1.00/day default ceiling per BD-03.
    """

    DEFAULT_DAILY_CEILING = 1.0  # $1.00/day [CONFIRMED — BD-03]

    def __init__(
        self,
        redis: RedisClient,
        daily_ceiling_credits: float | None = None,
    ) -> None:
        self._redis = redis
        self._ceiling = daily_ceiling_credits or self.DEFAULT_DAILY_CEILING

    def _spend_key(self, tenant_id: TenantId) -> str:
        return "capsolver:daily_spend"

    async def check_and_reserve(self, tenant_id: TenantId, estimated_cost: float) -> bool:
        """Check if tenant has remaining budget and atomically reserve it.

        Returns True if the spend was within budget and has been reserved.
        Returns False if the ceiling would be exceeded.
        """
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
            self._ceiling,
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
        return max(0.0, self._ceiling - await self.current_spend(tenant_id))
