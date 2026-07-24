# browser/pool.py
"""Hot-browser pool with real reuse — live Camoufox contexts stay alive
across acquire/release cycles. Tear-down only on unhealthy release, idle
timeout, or explicit shutdown. Semaphore-gated for concurrency control.

Fingerprint-staleness: prewarm instances use persistent_profile_id so
browser fingerprints are stable across reuse (same profile = same
fingerprint). Per-blueprint §3.4, Camoufox owns 100% of fingerprint
surface — no application-level rotation needed within the profile
lifetime. Idle timeout (max_idle_seconds) ensures no profile lives
longer than ~5 minutes without use.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
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
        # Evict idle contexts beyond max_idle_seconds before acquiring
        now = time.monotonic()
        fresh: list = []
        while not self._pool.empty():
            try:
                ctx, wrapper, idle_since = self._pool.get_nowait()
                if now - idle_since > self._max_idle_seconds:
                    # Idle timeout — tear down this context
                    for w in self._active_wrappers:
                        if w is wrapper or w._context is ctx:
                            self._active_wrappers.remove(w)
                            await w.__aexit__()
                            break
                else:
                    fresh.append((ctx, wrapper, idle_since))
            except asyncio.QueueEmpty:
                break
        for item in fresh:
            await self._pool.put(item)

        if fresh:
            ctx, wrapper, _ = fresh.pop(0)
            return ctx
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


    @asynccontextmanager
    async def lease(self, proxy: Proxy | None = None, domain: str | None = None):
        """Async context manager — structural cleanup (invariant §1.1.6).

        ``async with pool.lease() as ctx:`` guarantees release() on exit
        even if the block raises. Healthy release on normal exit; unhealthy
        (teardown) on exception. Restores __aexit__ contract that raw
        acquire()/release() lost when API switched to bare context objects.
        """
        ctx = await self.acquire(proxy=proxy)
        if domain:
            await self._load_session(ctx, domain)
        try:
            yield ctx
        except Exception:
            await self.release(ctx, healthy=False)
            raise
        else:
            if domain:
                await self._save_session(ctx, domain)
            await self.release(ctx, healthy=True)

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
        # Wrapper not found (shouldn't happen) — tear down to avoid zombie
        import contextlib
        with contextlib.suppress(Exception):
            await ctx.__aexit__(None, None, None)


    async def _load_session(self, ctx, domain: str) -> None:
        """Restore stored browser session state for domain.
        
        Blueprint v2 §3.5: browser_sessions table stores cookies + 
        localStorage per (tenant_id, domain, profile_id). Loading before
        use prevents cross-domain cookie leakage with warm context reuse.
        Deferred: requires Postgres connection + browser_sessions schema
        wired into BrowserPool (currently instantiated without pg client).
        Until wired, warm contexts share the same Camoufox profile's 
        cookies across domains — acceptable for single-domain scraping.
        """
        pass  # deferred: requires Postgres + browser_sessions schema

    async def _save_session(self, ctx, domain: str) -> None:
        """Persist current browser session state for domain to Postgres."""
        pass  # deferred: requires Postgres + browser_sessions schema

    async def shutdown(self) -> None:
        """Close all live browser contexts."""
        while not self._pool.empty():
            import contextlib
            with contextlib.suppress(asyncio.QueueEmpty):
                ctx, wrapper, _ = self._pool.get_nowait()
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
        # Close any remaining active wrappers
        for w in list(self._active_wrappers):
            await w.__aexit__()
        self._active_wrappers.clear()
        self._started = False
