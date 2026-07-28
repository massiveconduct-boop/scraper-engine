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

Session isolation (§3.5, plan §5.4): when session_mgr is supplied,
storage_state is loaded in acquire() and passed through CamoufoxWrapper
constructor — baked in at launch time, not patched in later via sub-context.
The classify-loop in acquire() is never touched by session code.
State is saved back to Postgres on healthy release inside lease().

Session save failures: logged at WARNING with domain, not swallowed
silently. A failed save means session state is lost but the pool must
continue serving requests — the exception is logged, not propagated.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from .camoufox_wrapper import CamoufoxWrapper

if TYPE_CHECKING:
    from browser.session_state import SessionStateManager
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
        session_mgr: SessionStateManager | None = None,
        geoip: bool = True,
        humanize: float = 1.5,
        headless_mode: str = "virtual",
        max_total_instances: int | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._prewarm_count = prewarm_count
        self._max_idle_seconds = max_idle_seconds
        self._session_mgr = session_mgr
        self._geoip = geoip
        self._humanize = humanize
        self._headless_mode = headless_mode
        # Validated in start(), not here — this is a ceiling on the shared
        # core.budget.BROWSER_SEMAPHORE, not something this pool enforces
        # itself, so a mismatch is only meaningful once we actually try to
        # prewarm past it.
        self._max_total_instances = max_total_instances
        self._pool: asyncio.Queue[Any] = asyncio.Queue()
        self._active_wrappers: list[CamoufoxWrapper] = []
        self._started = False

    async def start(self) -> None:
        """Launch prewarm_count browsers and store their live contexts."""
        if (
            self._max_total_instances is not None
            and self._prewarm_count > self._max_total_instances
        ):
            raise ValueError(
                f"prewarm_count ({self._prewarm_count}) exceeds "
                f"max_total_instances ({self._max_total_instances}) — prewarming "
                "would block waiting on core.budget.BROWSER_SEMAPHORE"
            )
        for i in range(self._prewarm_count):
            wrapper = CamoufoxWrapper(
                proxy=None,
                tenant_id=self._tenant_id,
                persistent_profile_id=f"prewarm-{i}",
                geoip=self._geoip,
                humanize=self._humanize,
                headless_mode=self._headless_mode,
            )
            ctx = await wrapper.__aenter__()
            self._active_wrappers.append(wrapper)
            await self._pool.put((ctx, wrapper, time.monotonic()))
        self._started = True

    async def acquire(self, proxy: Proxy | None = None, domain: str | None = None) -> Any:
        """Get a live browser context from the pool or launch a new one."""
        now = time.monotonic()
        drained = []
        while not self._pool.empty():
            try:
                drained.append(self._pool.get_nowait())
            except asyncio.QueueEmpty:
                break

        selected = None
        keep = []
        for ctx, wrapper, idle_since in drained:
            if now - idle_since > self._max_idle_seconds:
                # Genuinely stale — this is the one case that actually tears
                # down the browser. idle timeout, not a mismatch, is what
                # BrowserPool is meant to reclaim on (class docstring).
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx or w._isolated_ctx is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
                continue

            # A wrapper leased with proxy A must never be handed back out for a
            # request that asked for proxy B — a domain match alone isn't
            # enough (round 25). A caller that doesn't care about proxy
            # (proxy=None) accepts any pooled wrapper, matching the domain=None
            # behavior below.
            proxy_mismatch = proxy is not None and getattr(wrapper, "proxy", None) != proxy
            # domain=None on the wrapper means "never successfully leased yet"
            # (prewarm(), or a wrapper that's never been through lease()'s
            # success path) — not "leased for a different domain." Treating an
            # unclaimed wrapper as a domain mismatch was evicting every
            # prewarmed instance on its very first real acquire() call, making
            # prewarming nearly useless (round 25 fix). A wrapper that HAS
            # served a domain before still can't silently switch to another.
            domain_mismatch = (
                domain is not None
                and getattr(wrapper, "_last_domain", None) is not None
                and wrapper._last_domain != domain
            )
            if domain_mismatch or proxy_mismatch:
                # Not a match for *this* request, but still a live, warm spare.
                # BrowserPool's own docstring: "purely a latency optimization,
                # not a concurrency control" — tearing a good instance down
                # just because this particular request doesn't want it was
                # pure waste; keep it pooled for a request that does match,
                # and build a fresh one (below) for this one instead. Total
                # concurrently-alive instances still can't exceed
                # core.budget.BROWSER_SEMAPHORE either way.
                keep.append((ctx, wrapper, idle_since))
                continue
            if selected is None:
                selected = (ctx, wrapper)
            else:
                keep.append((ctx, wrapper, idle_since))

        for item in keep:
            await self._pool.put(item)

        if selected is not None:
            return selected[0]

        session_state = None
        if domain is not None and self._session_mgr is not None:
            session_state = await self._session_mgr.load(self._tenant_id, domain)

        wrapper = CamoufoxWrapper(
            proxy=proxy,
            tenant_id=self._tenant_id,
            storage_state=session_state,
            geoip=self._geoip,
            humanize=self._humanize,
            headless_mode=self._headless_mode,
        )
        self._active_wrappers.append(wrapper)
        ctx = await wrapper.__aenter__()
        return ctx

    @asynccontextmanager
    async def lease(
        self, proxy: Proxy | None = None, domain: str | None = None
    ) -> AsyncIterator[Any]:
        """Async context manager — structural cleanup (invariant §1.1.6)."""
        ctx = await self.acquire(proxy=proxy, domain=domain)
        try:
            yield ctx
        except Exception:
            await self.release(ctx, healthy=False)
            raise
        else:
            if domain and self._session_mgr is not None:
                try:
                    state = await ctx.storage_state()
                    await self._session_mgr.save(self._tenant_id, domain, state)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Failed to persist session state for domain=%s — "
                        "session will be lost on next pool recycle", domain,
                        exc_info=True,
                    )

            for w in self._active_wrappers:
                if w._context is ctx or w._isolated_ctx is ctx:
                    w._last_domain = domain if domain else None
                    break
            await self.release(ctx, healthy=True)

    async def release(self, ctx: Any, healthy: bool) -> None:
        """Return context to pool (healthy) or tear down (unhealthy)."""
        if not healthy:
            for w in self._active_wrappers:
                if w._context is ctx or w._isolated_ctx is ctx or w._context == ctx:
                    self._active_wrappers.remove(w)
                    await w.__aexit__()
                    return
            return

        for w in self._active_wrappers:
            if w._context is ctx or w._isolated_ctx is ctx or w._context == ctx:
                await self._pool.put((ctx, w, time.monotonic()))
                return
        with contextlib.suppress(Exception):
            await ctx.__aexit__(None, None, None)

    async def shutdown(self) -> None:
        """Close all live browser contexts."""
        while not self._pool.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                ctx, wrapper, _ = self._pool.get_nowait()
                for w in self._active_wrappers:
                    if w is wrapper or w._context is ctx or w._isolated_ctx is ctx:
                        self._active_wrappers.remove(w)
                        await w.__aexit__()
                        break
        for w in list(self._active_wrappers):
            await w.__aexit__()
        self._active_wrappers.clear()
        self._started = False
