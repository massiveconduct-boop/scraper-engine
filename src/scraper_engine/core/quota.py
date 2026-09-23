# core/quota.py
"""Per-tenant daily quota tracked via Redis counters.

Uses atomic Lua scripts to avoid race conditions under concurrent workers.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from scraper_engine.core.exceptions import QuotaExceededError

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.storage.redis_client import RedisClient


def seconds_until_quota_reset() -> int:
    """Seconds until the daily quota counter resets (next UTC midnight) —
    used as the Retry-After value on a 429 quota-exceeded response, since
    the quota key itself buckets by UTC day (see _quota_key below)."""
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())


class QuotaManager:
    """Per-tenant daily quota tracked via Redis counters.

    Uses atomic Lua scripts to avoid race conditions under concurrent workers.
    """

    DEFAULT_DAILY_LIMIT = 10_000  # 10k scrapes per tenant per day

    def __init__(
        self,
        redis: RedisClient,
        daily_limit: int | None = None,
    ) -> None:
        self._redis = redis
        self._limit = daily_limit or self.DEFAULT_DAILY_LIMIT

    def _quota_key(self, tenant_id: TenantId) -> str:
        from datetime import datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        return f"quota:daily:{today}:{tenant_id}"

    async def check_and_increment(self, tenant_id: TenantId, count: int = 1) -> None:
        """Atomically check quota and increment. Raises QuotaExceededError if limit hit."""
        result = await self._redis.eval(
            """
            local key = KEYS[1]
            local limit = tonumber(ARGV[1])
            local count = tonumber(ARGV[2])
            local ttl = tonumber(ARGV[3])
            local current = tonumber(redis.call('GET', key) or '0')
            if current + count > limit then
                return -1
            end
            local new_val = redis.call('INCRBY', key, count)
            redis.call('EXPIRE', key, ttl)
            return new_val
            """,
            1,
            self._quota_key(tenant_id),
            self._limit,
            count,
            86400 * 2,
        )
        if result == -1:
            raise QuotaExceededError(tenant_id=str(tenant_id), limit=self._limit)

    async def current_usage(self, tenant_id: TenantId) -> int:
        """Return current usage count for today."""
        raw = await self._redis.get(tenant_id, self._quota_key(tenant_id))
        return int(raw) if raw else 0

    async def remaining(self, tenant_id: TenantId) -> int:
        """Return remaining quota for today."""
        return max(0, self._limit - await self.current_usage(tenant_id))
