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

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from scraper_engine.core import budget

if TYPE_CHECKING:
    from scraper_engine.core.models import Proxy
    from scraper_engine.core.tenant import TenantId

logger = logging.getLogger(__name__)

# Round 63 — hard bounds on every budget.XVFB_LOCK critical section.
#
# XVFB_LOCK is process-wide and both the launch and the teardown hold it.
# Neither was time-bounded, so a single wedged Camoufox (dead CDP pipe, an
# Xvfb that will not exit) froze every browser operation in the worker
# permanently — and since BROWSER_SEMAPHORE is released only after the
# teardown's lock block, every instance's permit leaked with it. Live-caught
# twice on a 10-URL Jumia job: 8 live browsers, an idle event loop, zero log
# output, the job stalled mid-run.
#
# Generous rather than tight: these exist to convert "never" into "eventually",
# not to police slow-but-working browsers. A launch legitimately spins up Xvfb,
# Firefox and a geoip lookup through a proxy.
_BROWSER_LAUNCH_TIMEOUT_SECONDS = 180
_BROWSER_TEARDOWN_TIMEOUT_SECONDS = 60
_CONTEXT_CLOSE_TIMEOUT_SECONDS = 30


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
        except BaseException:
            # Round 63 — BaseException, and a full __aexit__ rather than a
            # bare release(). A launch cancelled mid-flight raises
            # CancelledError, which `except Exception` let through with the
            # permit still held; and a browser that launched but whose
            # new_context() failed was left running with no owner. __aexit__
            # closes whatever did come up and always releases the permit.
            await self.__aexit__(None, None, None)
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
    # that provably exist.
    #
    # Round 51 CORRECTION — round 49's retry set fingerprint_preset=False,
    # which does NOT reach the no-vendor/renderer path above. Verified
    # against the actual installed camoufox/utils.py::launch_options: its
    # preset branch is guarded by `elif fingerprint_preset is not None:`,
    # not truthiness — False satisfies `is not None` exactly like True, so
    # it draws another random REAL preset from the same 312-preset pool and
    # can independently hit the same webgl_data.db gap. Live-caught doing
    # exactly that the same day as round 50's "fix": job
    # 05d720cc-3933-437c-a262-1f6559d05d7e crashed on retry with a second
    # colliding vendor/renderer ("Mesa"/"GeForce 8800 GTX") after the first
    # attempt's fallback had already fired. The only sentinel that actually
    # skips the preset branch is None — confirmed via camoufox/
    # fingerprints.py's from_browserforge, which calls sample_webgl(os) with
    # no vendor/renderer, matching the safe path this comment always meant.
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
        - The WebGL data-gap ValueError above (round 49, sentinel corrected
          round 51). Retries with fingerprint_preset=None — the only value
          that actually reaches camoufox's default synthetic/BrowserForge
          fingerprint generation, unaffected by this gap. False looks like
          "off" but isn't: camoufox/utils.py checks `is not None`, so False
          still samples another real preset from the same pool.

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
        fingerprint_preset: bool | None = self._fingerprint_preset
        # Round 51 — tracked separately from `fingerprint_preset`'s own
        # value. Before, the retry guard was `if not fingerprint_preset:
        # raise` — but camoufox's launch_options() treats ANY non-None
        # value (True or False) identically (`is not None` check, not
        # truthiness), so a caller-supplied fingerprint_preset=False starts
        # this loop already exposed to the exact same webgl_data.db gap as
        # True. Using the value itself as the "already tried" marker would
        # deny that starting state its one legitimate retry. This flag
        # tracks "have we already spent the fallback", independent of
        # whatever value fingerprint_preset started at.
        fingerprint_fallback_used = False
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
            async with asyncio.timeout(_BROWSER_LAUNCH_TIMEOUT_SECONDS), budget.XVFB_LOCK:
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
                    if fingerprint_fallback_used or not any(
                        marker in str(exc) for marker in self._WEBGL_DATA_GAP_MARKERS
                    ):
                        raise
                    logger.warning(
                        "camoufox_webgl_preset_data_gap_retrying_without_fingerprint_preset "
                        "error=%s",
                        exc,
                    )
                    fingerprint_preset = None
                    fingerprint_fallback_used = True
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
        import contextlib

        try:
            if self._isolated_ctx is not None:
                with contextlib.suppress(Exception):
                    # Bounded for the same reason the teardown below is: a
                    # wedged CDP connection must not hold up the release of
                    # this instance's BROWSER_SEMAPHORE permit.
                    await asyncio.wait_for(
                        self._isolated_ctx.close(), timeout=_CONTEXT_CLOSE_TIMEOUT_SECONDS
                    )
                self._isolated_ctx = None
        finally:
            try:
                if self._browser is not None:
                    await self._shutdown_browser_bounded(exc)
            finally:
                self._browser = None
                self._context = None
                budget.BROWSER_SEMAPHORE.release()

    async def _shutdown_browser_bounded(self, exc: tuple[Any, ...]) -> None:
        """Tear the browser down under XVFB_LOCK, but never indefinitely.

        Round 63 — this whole section used to be an unbounded
        `async with budget.XVFB_LOCK: await self._browser.__aexit__(*exc)`,
        and `budget.BROWSER_SEMAPHORE.release()` sits AFTER it. Both the lock
        and the teardown can hang on a wedged browser (a dead CDP pipe, an
        Xvfb that will not die), and XVFB_LOCK is process-wide — so one stuck
        teardown froze every launch AND every other teardown in the worker,
        permanently, and none of those instances ever released their permit
        either. Live-caught twice on a 10-URL Jumia job: 8 live Camoufox
        instances, an idle event loop, zero log output, the job stalled
        mid-run until RQ's job timeout killed the work-horse.

        A timeout here can leak an OS process. That is strictly the better
        failure: a leaked browser costs memory on one worker until the
        container recycles, while an unbounded wait costs every remaining URL
        of every job that worker would ever run. The permit is released by
        the caller's `finally` either way, which is what lets the job carry
        on.
        """
        try:
            async with asyncio.timeout(_BROWSER_TEARDOWN_TIMEOUT_SECONDS):
                async with budget.XVFB_LOCK:
                    await self._browser.__aexit__(*exc)
        except TimeoutError:
            # No cleanup call here on purpose: _xvfb_cleanup.cleanup_stale_display
            # reaches into a botasaurus Driver's private config._display and
            # would silently no-op on a Camoufox browser. Camoufox owns its own
            # virtual display (camoufox/virtdisplay.py), and a teardown we just
            # gave up on is exactly the case where we cannot reach into it
            # safely. Leaking the display is the accepted cost; the log is the
            # signal that it happened.
            logger.error(
                "camoufox_teardown_timed_out_after_%ss proxy=%s — abandoning the browser "
                "process and releasing its budget so the worker can keep running",
                _BROWSER_TEARDOWN_TIMEOUT_SECONDS,
                self.proxy.url() if self.proxy is not None else None,
            )
