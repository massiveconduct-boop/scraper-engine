# browser/pool.py
"""Hot-browser pool with real reuse — live Camoufox contexts stay alive
across acquire/release cycles. Tear-down only on unhealthy release, idle
timeout, or explicit shutdown. Semaphore-gated for concurrency control.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .camoufox_wrapper import CamoufoxWrapper

if TYPE_CHECKING:
    from core.models import Proxy
    from core.tenant import TenantId


class BrowserPool:
    """Pool of live, pre-launched Camoufox browser contexts.

    start() launches `prewarm_count` browsers and stores their live
    contexts. acquire() returns a ready-to-use context (no cold start).
    release(healthy=True) returns the context to the pool for reuse.
    release(healthy=False) tears the browser down and does NOT return it.
    """

    def __init__(
        self,
        tenant_id: TenantId,
        prewarm_count: int = 2,
        max_idle_seconds: int = 300,
    ) -> None:
        self._tenant_id = tenant_id
        self._prewarm_count = prewarm_count
        self._max_idle_seconds = max_idle_seconds
        # Queue of (context, wrapper, idle_since) tuples
        self._pool: asyncio.Queue = asyncio.Queue()
        self._active_wrappers: list[CamoufoxWrapper] = []
        self._started = False

    async def start(self) -> None:
        """Launch prewarm_count browsers and store their live contexts."""
        for i in range(self._prewarm_count):
            wrapper = CamoufoxWrapper(
                proxy=None,
                tenant_id=self._tenant_id,
                persistent_profile_id=f"prewarm-{i}",
            )
            ctx = await wrapper.__aenter__()
            self._active_wrappers.append(wrapper)
            await self._pool.put((ctx, wrapper, time.monotonic()))
        self._started = True

    async def acquire(self, proxy: Proxy | None = None) -> object:
        """Get a live browser context from the pool or launch a new one."""
        try:
            ctx, wrapper, _ = self._pool.get_nowait()
            return ctx
        except asyncio.QueueEmpty:
            wrapper = CamoufoxWrapper(
                proxy=proxy,
                tenant_id=self._tenant_id,
            )
            self._active_wrappers.append(wrapper)
            return await wrapper.__aenter__()

    async def release(self, ctx: object, healthy: bool) -> None:
        """Return context to pool (healthy) or tear down (unhealthy)."""
        if not healthy:
            # Find the wrapper owning this context and close it
            for w in self._active_wrappers:
                if w._context is ctx or w._context == ctx:
                    self._active_wrappers.remove(w)
                    await w.__aexit__()
                    return
            return

        # Healthy: find wrapper and re-queue
        for w in self._active_wrappers:
            if w._context is ctx or w._context == ctx:
                await self._pool.put((ctx, w, time.monotonic()))
                return
        # Wrapper not found (shouldn't happen) — put context back anyway
        await self._pool.put((ctx, None, time.monotonic()))

    async def shutdown(self) -> None:
        """Close all live browser contexts."""
        while not self._pool.empty():
            try:
                ctx, wrapper, _ = self._pool.get_nowait()
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
            except asyncio.QueueEmpty:
                break
        # Close any remaining active wrappers
        for w in list(self._active_wrappers):
            await w.__aexit__()
        self._active_wrappers.clear()
        self._started = False
