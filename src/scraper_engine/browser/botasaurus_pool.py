# browser/botasaurus_pool.py
"""Same-domain Botasaurus driver reuse, scoped to one rq job's lifetime.

Unlike browser/pool.py::BrowserPool (which reuses live *Camoufox* contexts
across a whole job), this pool holds a raw botasaurus.browser.Driver we
construct and key ourselves — never botasaurus's own @browser
(reuse_driver=True) mechanism. Reading botasaurus/browser_decorator.py
directly (round 26) showed its internal `_driver_pool` is a bare, unkeyed
module-level list (`.pop()`/`.append()`, no matching on proxy, profile, or
tenant at all) — naively enabling it would let one tenant's fetch silently
receive a driver still configured with a *different* tenant's proxy/profile,
a direct hit on the tenant-isolation invariant (spec §1.1 #3). This pool
applies the exact same proxy+domain matching discipline BrowserPool already
uses for Camoufox, just for a botasaurus Driver instead of a Playwright
context.

First fetch for a (proxy identity, domain) pair in this job: no match,
construct a fresh Driver directly (bypassing the @browser decorator
entirely, so botasaurus's own pool is never touched) and navigate it. A later
fetch for the same pair reuses that live driver and navigates it again,
skipping the relaunch and the Xvfb display cycle.

Round 64 — up to `botasaurus.max_pooled_drivers` drivers per job (was
exactly one behind one lock, which serialized a job's concurrent URLs at
L2). Two more corrections in the same pass:
- `budget.XVFB_LOCK` is held only around launching and closing a driver —
  the display lifecycle it exists for — not across navigation. It used to
  wrap launch AND navigate AND scroll, and since the paid gateway presents a
  new session per attempt (so reuse never matches and every fetch
  relaunches), one L2 fetch blocked every other browser launch and teardown
  in the worker for its full 40-130s.
- A fetch now holds a `BROWSER_SEMAPHORE` permit while it runs, taken via
  `budget.acquire_browser_permit()` like every other engine. This pool never
  took one, so its Chrome was invisible to the browser ceiling and to the
  RAM-aware cap. A PARKED driver deliberately holds no permit — a parked
  permit-holder is exactly the round-63 deadlock.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from scraper_engine.browser._xvfb_cleanup import cleanup_stale_display
from scraper_engine.core import budget

if TYPE_CHECKING:
    from scraper_engine.config.schema import BotasaurusConfig
    from scraper_engine.core.models import Proxy
    from scraper_engine.core.tenant import TenantId


class _PooledDriver:
    __slots__ = ("driver", "proxy_key", "domain", "busy", "last_used", "events_sink")

    def __init__(self, proxy_key: str, domain: str) -> None:
        # None until launched: an entry is reserved (busy) before its driver
        # exists, so the pool's size cap counts launches in flight too.
        self.driver: Any = None
        self.proxy_key = proxy_key
        self.domain = domain
        self.busy = True
        self.last_used = time.monotonic()
        # Round 60 finding, kept per driver now that several can run at once:
        # CDP network hooks are registered once at launch and stay live for
        # the driver's pooled lifetime, but every fetch() brings its own sink.
        # The hook reads this attribute at event time, so each fetch's events
        # land in that fetch's own list.
        self.events_sink: list[dict[str, object]] | None = None


class BotasaurusPool:
    """One instance per rq job (see orchestrator/tasks.py::_run_scrape) —
    a Botasaurus Driver's lifetime is tied to a single job's process, the
    same lifetime BrowserPool already has."""

    def __init__(
        self,
        tenant_id: TenantId,
        config: BotasaurusConfig,
        park_drivers: bool = True,
    ) -> None:
        self._tenant_id = tenant_id
        self._config = config
        # Round 66 — False under host admission (orchestrator/host_capacity.py):
        # a parked driver holds no host seat, so it is load the host budget
        # cannot see. Live, free pool, 97 URLs with admission on: 8 seats in
        # use, 20 live browsers. With a fresh proxy per attempt a parked
        # driver's proxy almost never matches again, so it was not saving a
        # relaunch either. Same fix round 65 made to BrowserPool (park_spares).
        self._park_drivers = park_drivers
        self._max_drivers = max(1, config.max_pooled_drivers)
        self._entries: list[_PooledDriver] = []
        # Guards _entries and every entry's `busy` flag; notified whenever an
        # entry is freed or dropped, which is what a fetch waiting for a
        # slot under the cap wakes on.
        self._cond = asyncio.Condition()

    async def fetch(
        self,
        url: str,
        proxy: Proxy,
        domain: str,
        session_id: str | None,
        scroll_passes: int = 0,
        scroll_wait_ms: int = 1500,
        events_sink: list[dict[str, object]] | None = None,
    ) -> str:
        """Fetch `url` with a driver belonging to this exact (proxy identity,
        domain) pair — an idle pooled one if there is one, else a fresh launch.

        Round 63 — reuse navigates for real (it used to be an in-page
        `driver.requests.get`, no JS), and is keyed on `Proxy.identity_key()`
        so a rotated paid-gateway session relaunches instead of silently
        keeping a blocked exit IP. Round 64 — see the module docstring.

        Any failure closes that driver rather than returning it to the pool:
        a driver whose navigation failed is not one to hand the next URL.
        """
        loop = asyncio.get_running_loop()
        entry = await self._checkout(proxy.identity_key(), domain)
        entry.events_sink = events_sink
        try:
            await budget.acquire_browser_permit()
            try:
                if entry.driver is None:
                    async with budget.xvfb_lock():
                        entry.driver = await loop.run_in_executor(
                            None, self._launch_driver, entry, proxy, session_id
                        )
                html = await loop.run_in_executor(
                    None, self._navigate, entry.driver, url, scroll_passes, scroll_wait_ms
                )
                parked_idle = self._park_drivers and await loop.run_in_executor(
                    None, self._park, entry.driver
                )
            finally:
                budget.BROWSER_SEMAPHORE.release()
        except BaseException:
            await self._discard(entry)
            raise
        if not parked_idle:
            await self._discard(entry)
            return html
        await self._checkin(entry)
        return html

    async def _checkout(self, proxy_key: str, domain: str) -> _PooledDriver:
        """Reserve an entry: an idle match, else a new slot under the cap,
        else the oldest idle entry's slot (closing it), else wait."""
        evicted: _PooledDriver | None = None
        async with self._cond:
            while True:
                for e in self._entries:
                    if not e.busy and e.proxy_key == proxy_key and e.domain == domain:
                        e.busy = True
                        return e
                entry = _PooledDriver(proxy_key, domain)
                if len(self._entries) < self._max_drivers:
                    self._entries.append(entry)
                    return entry
                idle = [e for e in self._entries if not e.busy]
                if idle:
                    evicted = min(idle, key=lambda e: e.last_used)
                    self._entries.remove(evicted)
                    self._entries.append(entry)
                    break
                await self._cond.wait()
        await self._close_entry(evicted)
        return entry

    async def _checkin(self, entry: _PooledDriver) -> None:
        async with self._cond:
            entry.busy = False
            entry.last_used = time.monotonic()
            self._cond.notify_all()

    async def _discard(self, entry: _PooledDriver) -> None:
        await self._close_entry(entry)
        async with self._cond:
            if entry in self._entries:
                self._entries.remove(entry)
            self._cond.notify_all()

    async def _close_entry(self, entry: _PooledDriver) -> None:
        """Close a driver under XVFB_LOCK (round 41: a teardown must never
        overlap a launch's display spinup). No-op for an unlaunched entry."""
        if entry.driver is None:
            return
        driver, entry.driver = entry.driver, None
        loop = asyncio.get_running_loop()
        async with budget.xvfb_lock():
            await loop.run_in_executor(None, self._close_driver, driver)

    def _launch_driver(
        self, entry: _PooledDriver, proxy: Proxy, session_id: str | None
    ) -> Any:
        """Synchronous — constructs and prepares a fresh Driver, run in the
        executor under XVFB_LOCK (Selenium-style driver management has no
        native asyncio API to await on). Navigation is `_navigate`'s job, run
        after the lock is released (round 64)."""
        from botasaurus.browser import Driver
        from botasaurus.user_agent import UserAgent
        from botasaurus.window_size import WindowSize

        from scraper_engine.browser._botasaurus_extension import LocalExtension
        from scraper_engine.browser._botasaurus_network_capture import register_network_capture

        cfg = self._config
        kwargs: dict[str, object] = {
            "headless": False,
            "enable_xvfb_virtual_display": True,
            "proxy": proxy.auth_url(),
            "profile": session_id,
            # tiny_profile requires a profile (verified live — botasaurus_driver's
            # Config raises ValueError("Profile must be given when using tiny
            # profile") otherwise) — see fetcher/botasaurus_wrapper.py's same gate.
            "tiny_profile": cfg.tiny_profile and session_id is not None,
            "remove_default_browser_check_argument": cfg.remove_default_browser_check_argument,
            "block_images": cfg.block_images,
            "block_images_and_css": cfg.block_images_and_css,
        }
        if cfg.extensions:
            kwargs["extensions"] = [LocalExtension(p) for p in cfg.extensions]
        if cfg.lang:
            kwargs["lang"] = cfg.lang
        if cfg.hashed_fingerprint and session_id is not None:
            kwargs["user_agent"] = UserAgent.HASHED
            kwargs["window_size"] = WindowSize.HASHED
        driver = Driver(**kwargs)
        try:
            # Round 60 finding: these three calls were originally outside this
            # try block. Any of them raising (e.g. enable_human_mode()'s
            # lazy botasaurus_humancursor import failing, or a CDP command
            # throwing — schema.py's own docstring already documents a live-
            # confirmed CDP bug on a different domain in this installed
            # version) would leak the just-launched driver/Xvfb display with
            # no _close_driver() call, reintroducing the display-contention
            # precondition round 41's XVFB_LOCK was built to close. Moved
            # inside so any failure here is caught by the except below.
            if cfg.capture_network_events:
                register_network_capture(driver, lambda: entry.events_sink)
            if cfg.humanize_mouse:
                driver.enable_human_mode()
            if cfg.locale or cfg.timezone:
                # Must be applied before navigation — driver.py:2148-2150's own
                # docstring: "call this before navigating".
                driver.set_locale_and_timezone(
                    locale=cfg.locale or None, timezone_id=cfg.timezone or None
                )
            return driver
        except BaseException:
            self._close_driver(driver)
            raise

    def _navigate(
        self, driver: Any, url: str, scroll_passes: int = 0, scroll_wait_ms: int = 1500
    ) -> str:
        """Synchronous — navigate a launched driver to `url` and return its HTML.

        Used for both a fresh launch and a reuse, so the two can never again
        fetch differently (round 63: reuse used to be an in-page
        `driver.requests.get` with no JS). Deliberately NOT under
        budget.XVFB_LOCK: no display is created or destroyed here.
        """
        from scraper_engine.browser._botasaurus_nav_check import raise_if_navigation_failed
        from scraper_engine.browser._botasaurus_scroll import botasaurus_autoscroll

        cfg = self._config
        if cfg.bypass_cloudflare:
            driver.google_get(url, bypass_cloudflare=True)
        else:
            driver.get(url)
        raise_if_navigation_failed(driver, url)
        if cfg.random_sleep_enabled:
            driver.short_random_sleep()
        if scroll_passes > 0:
            botasaurus_autoscroll(
                driver,
                max_passes=scroll_passes,
                wait_ms=scroll_wait_ms,
                humanize=cfg.humanize_mouse,
            )
        return str(driver.page_html)

    def _park(self, driver: Any) -> bool:
        """Leave a driver on about:blank before it goes back to the pool.

        Round 65 — a parked driver holds no browser permit and no host seat,
        on the premise that an idle browser costs no CPU. It was not idle: it
        kept the last page open, and that page's scripts kept running. The
        next checkout navigates for real anyway, so nothing is lost. A driver
        that cannot even do this is not one to hand the next URL (False).
        """
        try:
            driver.get("about:blank")
        except Exception:
            return False
        return True

    def _close_driver(self, driver: Any) -> None:
        with contextlib.suppress(Exception):
            driver.close()
        # Round 41 — driver.close() SIGKILLs the Xvfb display without
        # unlinking its lock/socket files (see browser/_xvfb_cleanup.py).
        # Best-effort, never lets cleanup failure mask the real close above.
        with contextlib.suppress(Exception):
            cleanup_stale_display(driver)

    async def shutdown(self) -> None:
        """Close every held driver — called once at job end, same bracket
        BrowserPool.shutdown() is called in (orchestrator/tasks.py). Each
        close runs under budget.XVFB_LOCK (round 41)."""
        async with self._cond:
            entries, self._entries = self._entries, []
            self._cond.notify_all()
        for entry in entries:
            await self._close_entry(entry)
