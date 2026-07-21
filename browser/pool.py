# browser/pool.py
"""Semaphore-bounded pre-warmed browser pool.

acquire() NEVER falls back to an unbounded _launch(). Every path goes through
CamoufoxWrapper.__aenter__, gated by the SAME global semaphore. Pool is purely
latency optimization (warm spares), not concurrency control.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import Proxy
    from core.tenant import TenantId

    from .camoufox_wrapper import CamoufoxWrapper


class BrowserPool:
    """Pre-warmed pool of Camoufox browser instances."""

    def __init__(
        self,
        tenant_id: TenantId,
        prewarm_count: int = 3,
        max_idle_seconds: int = 300,
    ) -> None:
        self._tenant_id = tenant_id
        self._prewarm_count = prewarm_count
        self._max_idle_seconds = max_idle_seconds
        self._pool: asyncio.Queue[CamoufoxWrapper] = asyncio.Queue()

    async def start(self) -> None:
        """Pre-warm the pool to prewarm_count instances, all semaphore-gated."""
        for _ in range(self._prewarm_count):
            wrapper = CamoufoxWrapper(proxy=None, tenant_id=self._tenant_id)
            await self._pool.put(wrapper)

    async def acquire(self, proxy: Proxy | None) -> CamoufoxWrapper:
        """Get a browser instance from the pool or create one (semaphore-gated)."""
        try:
            wrapper = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=self._tenant_id)
        return wrapper

    async def release(self, wrapper: CamoufoxWrapper, healthy: bool) -> None:
        """Return a browser to the pool or discard if unhealthy."""
        if healthy:
            await self._pool.put(wrapper)
        # Unhealthy wrappers are discarded and garbage-collected

    async def shutdown(self) -> None:
        """Close all pooled instances gracefully."""
        while not self._pool.empty():
            try:
                wrapper = self._pool.get_nowait()
                await wrapper.__aexit__()
            except asyncio.QueueEmpty:
                break
