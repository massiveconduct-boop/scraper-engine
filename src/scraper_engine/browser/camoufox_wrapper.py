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

import logging
from typing import TYPE_CHECKING, Any

from scraper_engine.core import budget

if TYPE_CHECKING:
    from scraper_engine.core.models import Proxy
    from scraper_engine.core.tenant import TenantId

logger = logging.getLogger(__name__)


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
        fingerprint_preset: bool = True,
        os: str = "linux",
    ) -> None:
        self.proxy = proxy
        self.tenant_id = tenant_id
        self.persistent_profile_id = persistent_profile_id
        self._storage_state = storage_state
        self._geoip = geoip
        self._humanize = humanize
        self._headless_mode = headless_mode
        self._fingerprint_preset = fingerprint_preset
        self._os = os
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
            self._context = await self._launch_with_geoip_fallback()

            kwargs: dict[str, Any] = {}
            if self._storage_state is not None:
                kwargs["storage_state"] = self._storage_state
            self._isolated_ctx = await self._context.new_context(**kwargs)
            return self._isolated_ctx
        except Exception:
            budget.BROWSER_SEMAPHORE.release()
            raise

    # Round 49 — camoufox's fingerprint_preset (round 46) samples a random
    # REAL captured fingerprint from its 312-preset bundle; some of those
    # presets' (vendor, renderer) pair isn't covered by camoufox's SEPARATE,
    # smaller webgl_data.db lookup table (camoufox/webgl/sample.py::
    # sample_webgl, called with the preset's pinned vendor/renderer —
    # verified against the actual installed package source, not guessed).
    # Confirmed a genuine upstream data gap between camoufox's two internal
    # datasets, not something our config controls — live-caught crashing an
    # ENTIRE job (BrowserPool.start()'s prewarm loop runs outside
    # process_job's per-URL try/except, so a launch failure there aborts
    # the whole RQ job before any URL is attempted) on real fingerprint
    # picks ("Intel Open Source Technology Center"/"Intel(R) HD Graphics
    # 400", "NVIDIA Corporation"/"NVIDIA GeForce 8800 GTX" — both old,
    # under-covered real hardware). A random (no pinned vendor/renderer)
    # WebGL sample can never raise this — it only ever draws from rows
    # that provably exist — so fingerprint_preset=False is a real, working
    # degradation, not a guess.
    _WEBGL_DATA_GAP_MARKERS = ("No WebGL data found", "combination not valid for")

    async def _launch_with_geoip_fallback(self) -> Any:
        """Launch Camoufox, retrying with degraded settings on two known
        third-party-data failure modes — each spent at most once, so at
        most 3 total launch attempts:

        - InvalidIP (round 37): Camoufox's own internal IP lookup
          (camoufox/ip.py::public_ip, 6 third-party IP-echo services)
          couldn't determine an IP through this proxy. A proxy that
          demonstrably reaches real target sites fine can still fail all 6
          of those specific, unrelated services — doesn't mean the proxy
          is dead. Retries with geoip=False.
        - The WebGL data-gap ValueError above (round 49). Retries with
          fingerprint_preset=False (Camoufox's default synthetic/
          BrowserForge fingerprint generation, unaffected by this gap).

        Either fallback preserves the fetch (with reduced anti-detection
        fidelity for this one session — invariant §1.1.2's fingerprint/geoip
        surface is best-effort, not something we can force through) instead
        of losing the entire lease — or, for the WebGL case, the entire
        job's browser prewarm — to an unrelated third-party data gap.
        """
        from camoufox.async_api import AsyncCamoufox
        from camoufox.exceptions import InvalidIP

        proxy_config = None
        if self.proxy is not None:
            proxy_config = {"server": self.proxy.url()}
            # Round 40 — paid gateway proxies (proxy/paid_gateway.py) carry
            # credentials; free-pool proxies never do (both None), so this
            # is a no-op for every proxy this system used before round 40.
            if self.proxy.username is not None and self.proxy.password is not None:
                proxy_config["username"] = self.proxy.username
                proxy_config["password"] = self.proxy.password

        geoip = self._geoip
        fingerprint_preset = self._fingerprint_preset
        for _attempt in range(3):
            self._browser = AsyncCamoufox(  # type: ignore[no-untyped-call]  # 3rd-party, untyped
                geoip=geoip,
                humanize=self._humanize,
                headless=self._headless_mode,
                proxy=proxy_config,
                fingerprint_preset=fingerprint_preset,
                os=self._os,
            )
            # Round 41 — serialize just the Xvfb-spinup moment against a
            # concurrent Botasaurus launch (or a still-in-flight teardown
            # of a just-crashed browser, see __aexit__ below) racing the
            # same virtual display number (core/budget.py::XVFB_LOCK
            # docstring). Held only across __aenter__, not the fetch that
            # follows — reacquired fresh on each retry below.
            async with budget.XVFB_LOCK:
                try:
                    return await self._browser.__aenter__()
                except InvalidIP:
                    if not geoip:
                        raise
                    logger.warning(
                        "camoufox_geoip_lookup_failed_retrying_without_geoip proxy=%s",
                        self.proxy.url() if self.proxy is not None else None,
                    )
                    geoip = False
                    continue
                except ValueError as exc:
                    if not fingerprint_preset or not any(
                        marker in str(exc) for marker in self._WEBGL_DATA_GAP_MARKERS
                    ):
                        raise
                    logger.warning(
                        "camoufox_webgl_preset_data_gap_retrying_without_fingerprint_preset "
                        "error=%s",
                        exc,
                    )
                    fingerprint_preset = False
                    continue
        # Unreachable in practice — each of the 3 iterations either returns
        # or continues after spending one of the two one-time fallbacks;
        # the third iteration's failure re-raises instead of continuing.
        # Satisfies mypy's "function must return" without a bare `raise`
        # outside an except block.
        raise RuntimeError("camoufox launch retry loop exited without returning or raising")

    async def __aexit__(self, *exc: object) -> None:
        """Guaranteed browser + Playwright driver cleanup, release semaphore.

        Closes isolated BrowserContext (if created) before closing the Browser.

        Round 41 — the actual browser teardown (which tears down this
        instance's Xvfb display, camoufox/virtdisplay.py::kill()) is held
        under budget.XVFB_LOCK too, same as launch. A crashed browser
        (dead CDP/websocket mid-navigation) still needs its Xvfb process
        killed here; without serializing this against a concurrent launch,
        a fresh retry (orchestrator/worker.py's same-level fresh-proxy
        retry, which fires immediately on a BROWSER_CRASH-category
        failure) could start launching before this teardown finishes,
        colliding on the still-live display number.
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
                    async with budget.XVFB_LOCK:
                        await self._browser.__aexit__(*exc)
            finally:
                self._browser = None
                self._context = None
                budget.BROWSER_SEMAPHORE.release()
