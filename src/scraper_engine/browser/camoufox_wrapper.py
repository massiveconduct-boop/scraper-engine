# browser/camoufox_wrapper.py
"""Thin adapter over camoufox.async_api.AsyncCamoufox.

Design invariant §1.1.2: Camoufox owns 100% of fingerprint/geoip/UA/canvas/WebGL surface.
Application code never touches navigator, WebGL*, or Canvas* prototypes.

Lifecycle: strictly `async with` — never manually .launch()/.close() outside a context
manager (closes F-16 driver-process leak).

Plan §5.3a/5.3b: storage_state passed through constructor (not __aenter__ args).
Path A (AsyncCamoufox storage_state kwarg) confirmed unavailable — AsyncCamoufox
does not forward storage_state to Playwright's context creation. Path B applies:
create BrowserContext via browser.new_context(storage_state=blob) after launch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scraper_engine.core import budget

if TYPE_CHECKING:
    from scraper_engine.core.models import Proxy
    from scraper_engine.core.tenant import TenantId


class CamoufoxWrapper:
    """Context-managed Camoufox browser instance.

    Acquires budget.BROWSER_SEMAPHORE BEFORE spawning any process (closes F-14).
    Delegates 100% of fingerprint surface to camoufox.async_api.AsyncCamoufox.

    Plan §5.3a: storage_state passed via constructor, applied in __aenter__
    via browser.new_context(storage_state=blob) after launch.

    ~80MB RSS per instance (measured 2026-07-22, Camoufox v152).
    This figure is the binding constraint for max_total_instances, not CPU.
    """

    def __init__(
        self,
        proxy: Proxy | None,
        tenant_id: TenantId | None,
        persistent_profile_id: str | None = None,
        storage_state: dict[str, object] | None = None,
        geoip: bool = True,
        humanize: float = 1.5,
        headless_mode: str = "virtual",
    ) -> None:
        self.proxy = proxy
        self.tenant_id = tenant_id
        self.persistent_profile_id = persistent_profile_id
        self._storage_state = storage_state
        self._geoip = geoip
        self._humanize = humanize
        self._headless_mode = headless_mode
        self._browser: Any = None
        self._context: Any | None = None
        self._isolated_ctx: Any | None = None
        # set by BrowserPool.lease on healthy return, read for domain-match reuse
        self._last_domain: str | None = None

    async def __aenter__(self) -> object:
        """Acquire semaphore, launch Camoufox, apply storage_state if set.

        Always returns a BrowserContext (never raw Browser).
        When storage_state is loaded: context created with that state.
        When no storage_state: clean context created.

        Plan §5.3b Path B: browser.new_context(storage_state=blob) after launch.
        Path A unavailable — AsyncCamoufox does not forward storage_state
        to Playwright context creation.
        """
        await budget.BROWSER_SEMAPHORE.acquire()
        try:
            from camoufox.async_api import AsyncCamoufox

            proxy_config = None
            if self.proxy is not None:
                proxy_config = {"server": self.proxy.url()}

            self._browser = AsyncCamoufox(  # type: ignore[no-untyped-call]  # 3rd-party, untyped
                geoip=self._geoip,
                humanize=self._humanize,
                headless=self._headless_mode,
                proxy=proxy_config,
            )
            self._context = await self._browser.__aenter__()

            kwargs: dict[str, Any] = {}
            if self._storage_state is not None:
                kwargs["storage_state"] = self._storage_state
            self._isolated_ctx = await self._context.new_context(**kwargs)
            return self._isolated_ctx
        except Exception:
            budget.BROWSER_SEMAPHORE.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        """Guaranteed browser + Playwright driver cleanup, release semaphore.

        Closes isolated BrowserContext (if created) before closing the Browser.
        """
        try:
            if self._isolated_ctx is not None:
                import contextlib
                with contextlib.suppress(Exception):
                    await self._isolated_ctx.close()
                self._isolated_ctx = None
        finally:
            try:
                if self._browser is not None:
                    await self._browser.__aexit__(*exc)
            finally:
                self._browser = None
                self._context = None
                budget.BROWSER_SEMAPHORE.release()
