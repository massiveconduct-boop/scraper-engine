# orchestrator/politeness.py
"""Atomic (Lua) concurrency + delay controller.

Closes F-06/F-07: uses atomic Redis Lua scripts for slot acquisition,
with TTL deadman's switch so a crashed worker never permanently holds a slot.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId

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

# Round 63 — extends the deadman TTL while a slot is genuinely still held.
# slot_ttl_seconds (120) is shorter than a worst-case Level-3 attempt, so the
# deadman switch could expire the whole slot SET under a live holder and hand
# its slot to someone else while it was still fetching. Only refreshes when
# the caller is still a member, so a released or already-expired slot is never
# resurrected.
REFRESH_SLOT_LUA = """
local key = KEYS[1]
local worker_id = ARGV[1]
local ttl = tonumber(ARGV[2])
if redis.call('SISMEMBER', key, worker_id) == 1 then
    redis.call('EXPIRE', key, ttl)
    return 1
end
return 0
"""

# Round 63 — the inter-fetch delay as an atomic *reservation* rather than a
# read-sleep-write. The old shape read the last-fetch timestamp, slept the
# remainder, then wrote — so N concurrent siblings all read the same value,
# all slept the same amount, and all fetched at the same instant, defeating
# the delay exactly when it mattered most. This hands each caller its own
# instant in the sequence (wait 0, then delay, then 2*delay, ...) in one
# atomic step.
#
# The clock is Redis's own TIME, not the caller's: the previous version wrote
# time.monotonic() — a process-local epoch — into a Redis key shared by every
# worker process, so the comparison was only ever meaningful within one
# process and was nonsense across containers or hosts.
RESERVE_DELAY_LUA = """
local key = KEYS[1]
local delay_ms = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local next_at = tonumber(redis.call('GET', key) or '0')
if next_at < now_ms then
    next_at = now_ms
end
redis.call('SET', key, tostring(next_at + delay_ms), 'EX', ttl)
return next_at - now_ms
"""


class PolitenessController:
    """Atomic concurrency + delay controller for per-domain politeness.

    Guarantees:
      - At most N concurrent fetches to a domain
      - Minimum delay between successive fetches to the same domain
      - TTL deadman's switch: crashed worker slots expire automatically

    The configured ``default_concurrency``/``default_delay_seconds`` are
    defaults, not absolutes: a caller may pass per-request values (round 63,
    core/models.py::ConfigOverrides), already clamped against the operator's
    ceilings by orchestrator/worker.py. This class applies what it is given
    and does not itself decide policy.
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

    async def acquire_slot(
        self, domain: str, tenant_id: TenantId, *, concurrency: int | None = None
    ) -> str | None:
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
            self._concurrency if concurrency is None else concurrency,
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

    async def refresh_slot(self, domain: str, tenant_id: TenantId, worker_id: str) -> bool:
        """Re-arm the deadman TTL for a slot this caller still holds.

        Returns True if the slot was still held (and its TTL extended), False
        if it had already been released or expired — in which case the caller
        has lost the slot and there is nothing to keep alive.
        """
        slot_key = self._slot_key(domain, tenant_id)
        result = await self._redis.eval(REFRESH_SLOT_LUA, 1, slot_key, worker_id, self._slot_ttl)
        return bool(result)

    @contextlib.asynccontextmanager
    async def held_slot(
        self, domain: str, tenant_id: TenantId, worker_id: str
    ) -> AsyncIterator[None]:
        """Keep `worker_id`'s slot alive for the duration of the block.

        Refreshes at a third of the TTL so two consecutive missed refreshes
        still leave headroom before the deadman fires. Always releases the
        slot on exit, including on cancellation — the release is the
        load-bearing part, the refresh loop is best-effort.
        """
        interval = max(1.0, self._slot_ttl / 3)

        async def _beat() -> None:
            while True:
                await asyncio.sleep(interval)
                with contextlib.suppress(Exception):
                    await self.refresh_slot(domain, tenant_id, worker_id)

        task = asyncio.create_task(_beat())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self.release_slot(domain, tenant_id, worker_id)

    async def wait_if_needed(
        self, domain: str, tenant_id: TenantId, *, delay_seconds: float | None = None
    ) -> int:
        """Reserve this domain's next fetch instant and wait for it.

        Returns the number of milliseconds actually slept, so the caller can
        record it (round 63 — this wait used to be invisible, which is why a
        175s job with a 28s fetch could not be explained from the outside).
        """
        delay = self._delay if delay_seconds is None else delay_seconds
        delay_ms = int(delay * 1000)
        if delay_ms <= 0:
            return 0
        last_key = self._last_fetch_key(domain, tenant_id)
        # TTL outlives one full reservation queue without accumulating dead
        # keys for domains that stop being crawled.
        ttl_seconds = max(60, delay_ms // 1000 * 4)
        wait_ms = int(
            await self._redis.eval(RESERVE_DELAY_LUA, 1, last_key, delay_ms, ttl_seconds)
        )
        if wait_ms <= 0:
            return 0
        await asyncio.sleep(wait_ms / 1000)
        return wait_ms
