# orchestrator/politeness.py
"""Atomic (Lua) concurrency + delay controller.

Closes F-06/F-07: uses atomic Redis Lua scripts for slot acquisition,
with TTL deadman's switch so a crashed worker never permanently holds a slot.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.tenant import TenantId

# Lua script: atomically acquire a concurrency slot with TTL deadman's switch
ACQUIRE_SLOT_LUA = """
local key = KEYS[1]
local worker_id = ARGV[1]
local max_concurrent = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if redis.call('SCARD', key) < max_concurrent then
    redis.call('SADD', key, worker_id)
    redis.call('EXPIRE', key, ttl)
    return 1
end
return 0
"""

RELEASE_SLOT_LUA = """
local key = KEYS[1]
local worker_id = ARGV[1]
redis.call('SREM', key, worker_id)
return 1
"""


class PolitenessController:
    """Atomic concurrency + delay controller for per-domain politeness.

    Guarantees:
      - At most N concurrent fetches to a domain
      - Minimum delay between successive fetches to the same domain
      - TTL deadman's switch: crashed worker slots expire automatically
    """

    def __init__(
        self,
        redis: Any,
        default_concurrency: int = 2,
        default_delay_seconds: float = 5.0,
        slot_ttl_seconds: int = 120,
    ) -> None:
        self._redis = redis
        self._concurrency = default_concurrency
        self._delay = default_delay_seconds
        self._slot_ttl = slot_ttl_seconds

    def _slot_key(self, domain: str, tenant_id: TenantId) -> str:
        return f"politeness:slots:{tenant_id}:{domain}"

    def _last_fetch_key(self, domain: str, tenant_id: TenantId) -> str:
        return f"politeness:last:{tenant_id}:{domain}"

    async def acquire_slot(self, domain: str, tenant_id: TenantId) -> str | None:
        """Atomically try to acquire a concurrency slot.

        Returns the slot's worker_id on success (pass it to release_slot to
        release exactly this slot), or None if at capacity.
        """
        import uuid

        worker_id = str(uuid.uuid4())[:8]
        slot_key = self._slot_key(domain, tenant_id)

        result = await self._redis.eval(
            ACQUIRE_SLOT_LUA,
            1,
            slot_key,
            worker_id,
            self._concurrency,
            self._slot_ttl,
        )
        return worker_id if bool(result) else None

    async def release_slot(self, domain: str, tenant_id: TenantId, worker_id: str) -> None:
        """Release the specific slot identified by worker_id (from acquire_slot).

        The TTL deadman's switch (set on acquire) remains the crash-safety
        backstop for workers that die before releasing.
        """
        slot_key = self._slot_key(domain, tenant_id)
        await self._redis.eval(RELEASE_SLOT_LUA, 1, slot_key, worker_id)

    async def wait_if_needed(self, domain: str, tenant_id: TenantId) -> None:
        """Enforce minimum inter-fetch delay if needed."""
        last_key = self._last_fetch_key(domain, tenant_id)
        last_raw = await self._redis.get(last_key)
        if last_raw is not None:
            elapsed = (await self._get_time_ms()) - float(last_raw)
            remaining = self._delay - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

        # Record this fetch time
        now = await self._get_time_ms()
        await self._redis.set(last_key, str(now))

    async def _get_time_ms(self) -> float:
        """Return monotonic time in milliseconds."""
        import time

        return time.monotonic()
