# browser/camoufox_wrapper.py
"""Thin adapter over camoufox.async_api.AsyncCamoufox.

Design invariant §1.1.2: Camoufox owns 100% of fingerprint/geoip/UA/canvas/WebGL surface.
Application code never touches navigator, WebGL*, or Canvas* prototypes.

Lifecycle: strictly `async with` — never manually .launch()/.close() outside a context
manager (closes F-16 driver-process leak).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.budget import BROWSER_SEMAPHORE

if TYPE_CHECKING:
    from core.models import Proxy
    from core.tenant import TenantId


class CamoufoxWrapper:
    """Context-managed Camoufox browser instance.

    Acquires core.budget.BROWSER_SEMAPHORE BEFORE spawning any process (closes F-14).
    Delegates 100% of fingerprint surface to camoufox.async_api.AsyncCamoufox.

    ~80MB RSS per instance (measured 2026-07-22, Camoufox v152).
    This figure is the binding constraint for max_total_instances, not CPU.
    """

    def __init__(
        self,
        proxy: Proxy | None,
        tenant_id: TenantId,
        persistent_profile_id: str | None = None,
    ) -> None:
        self.proxy = proxy
        self.tenant_id = tenant_id
        self.persistent_profile_id = persistent_profile_id
        self._browser: Any = None
        self._context: Any | None = None

    async def __aenter__(self) -> object:
        """Acquire semaphore, launch Camoufox, return BrowserContext."""
        await BROWSER_SEMAPHORE.acquire()
        try:
            from camoufox.async_api import AsyncCamoufox

            proxy_config = None
            if self.proxy is not None:
                proxy_config = {"server": self.proxy.url()}

            self._browser = AsyncCamoufox(
                geoip=True,
                humanize=1.5,
                headless="virtual",
                proxy=proxy_config,
            )
            self._context = await self._browser.__aenter__()
            return self._context
        except Exception:
            BROWSER_SEMAPHORE.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        """Guaranteed browser + Playwright driver cleanup, release semaphore."""
        try:
            if self._browser is not None:
                await self._browser.__aexit__(*exc)
        finally:
            self._browser = None
            self._context = None
            BROWSER_SEMAPHORE.release()
