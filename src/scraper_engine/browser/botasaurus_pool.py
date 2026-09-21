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

First fetch for a (proxy, domain) pair in this job: no match, construct a
fresh Driver directly (bypassing the @browser decorator entirely, so
botasaurus's own pool is never touched), navigate via
`driver.google_get(url, bypass_cloudflare=True)`, and keep the live Driver.
Second+ fetch for the *same* (proxy, domain): reuse it via
`driver.requests.get(url)` — verified (botasaurus_driver/requests.py) to run
the fetch as an in-page `fetch()` call through the browser's own JS context,
so it inherits that tab's live cookies/session/TLS fingerprint natively, no
separate cookie-jar plumbing needed — skipping a full browser relaunch
entirely. A proxy or domain mismatch closes the old driver and starts fresh,
same as BrowserPool.

Only one driver is held at a time (this pool optimizes the common "N pages,
one domain" crawl-job shape, not concurrent multi-domain fetches within a
single job) — a mismatch simply replaces it rather than growing unbounded.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from scraper_engine.browser._xvfb_cleanup import cleanup_stale_display
from scraper_engine.core import budget

if TYPE_CHECKING:
    from scraper_engine.config.schema import BotasaurusConfig
    from scraper_engine.core.models import Proxy
    from scraper_engine.core.tenant import TenantId


class _PooledDriver:
    __slots__ = ("driver", "proxy_key", "domain")

    def __init__(self, driver: Any, proxy_key: str, domain: str) -> None:
        self.driver = driver
        self.proxy_key = proxy_key
        self.domain = domain


class BotasaurusPool:
    """One instance per rq job (see orchestrator/tasks.py::_run_scrape) —
    a Botasaurus Driver's lifetime is tied to a single job's process, the
    same lifetime BrowserPool already has."""

    def __init__(
        self,
        tenant_id: TenantId,
        config: BotasaurusConfig,
    ) -> None:
        self._tenant_id = tenant_id
        self._config = config
        self._entry: _PooledDriver | None = None
        # Serializes access to the single held driver — Level2Fetcher fetches
        # are already gated one-at-a-time overall by core.budget.BROWSER_
        # SEMAPHORE, but this lock keeps this pool's own reuse/evict decision
        # atomic regardless of that external ceiling.
        self._lock = asyncio.Lock()
        # Round 60 finding: CDP before_request_sent/after_response_received
        # hooks are registered once, on first launch, and stay live on that
        # Driver for its whole pooled lifetime (they're tab-scoped, not
        # per-navigation) — but each fetch() call brings its own fresh
        # events_sink for its own FetchResult. A hook capturing a *fixed*
        # list reference from registration time would keep appending every
        # later reused-driver fetch's traffic into the first call's already-
        # returned list instead of each call's own. _new_driver_fetch
        # registers a handler that reads this attribute dynamically
        # (`register_network_capture(driver, lambda: self._active_events_
        # sink)`) instead, and fetch() updates it on every call — including
        # the reuse branch, which is what actually makes reused-driver
        # capture work at all. Safe under self._lock: only one fetch() call
        # is ever in flight per pool instance.
        self._active_events_sink: list[dict[str, object]] | None = None

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
        """Fetch `url`, reusing the pooled driver when it already belongs to
        this exact (proxy identity, domain) pair, else (re)launching one.

        Round 63 — two corrections to what "reuse" meant here.

        The reuse path used to fire an in-page `driver.requests.get(url)`
        rather than navigating: no JS execution, no challenge handling, no
        scroll. So the FIRST url of a domain got a real browser render and
        every subsequent one got what amounts to an HTTP client that happens
        to live inside a browser. Whether L2 succeeded therefore depended on
        an invisible property — whether a url happened to be first in its
        domain — which is what an external consumer reported as "same URL,
        same parameters, sometimes L2, mostly L3", and why L2 and L3 returned
        structurally different HTML (and therefore different `links`) for the
        same page. Both paths navigate now; reuse still saves the launch and
        the Xvfb display cycle, which was always the real win.

        The reuse gate also keyed on `proxy.key()` (ip:port), which is
        CONSTANT for the paid rotating gateway — every DataImpulse session
        shares one host:port and the username selects the exit IP. A
        deliberately rotated session (round 62's fix for a blocked exit IP)
        therefore hit this branch and silently kept using the blocked IP.
        Keyed on `proxy.identity_key()` now, so a new session relaunches.

        events_sink applies to both paths — see `self._active_events_sink`'s
        docstring above."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._active_events_sink = events_sink
            entry = self._entry
            if (
                entry is not None
                and entry.proxy_key == proxy.identity_key()
                and entry.domain == domain
            ):
                return await loop.run_in_executor(
                    None, self._reuse_fetch, entry.driver, url, scroll_passes, scroll_wait_ms
                )

            # Round 41 — both eviction-close and (re)launch spin the display
            # lifecycle, serialized under one lock so a fresh launch can
            # never start while a just-evicted driver's Xvfb teardown is
            # still in flight. _reuse_fetch above touches no display at all
            # and stays lock-free. See core/budget.py::XVFB_LOCK.
            async with budget.XVFB_LOCK:
                if entry is not None:
                    await loop.run_in_executor(None, self._close_driver, entry.driver)
                    self._entry = None

                driver, html = await loop.run_in_executor(
                    None,
                    self._new_driver_fetch,
                    url,
                    proxy,
                    session_id,
                    scroll_passes,
                    scroll_wait_ms,
                )
            self._entry = _PooledDriver(driver, proxy.identity_key(), domain)
            return html

    def _new_driver_fetch(
        self,
        url: str,
        proxy: Proxy,
        session_id: str | None,
        scroll_passes: int = 0,
        scroll_wait_ms: int = 1500,
    ) -> tuple[Any, str]:
        """Synchronous — constructs and navigates a fresh Driver, run in the
        executor same as BotasaurusWrapper._botasaurus_fetch (Selenium-style
        driver management has no native asyncio API to await on)."""
        from botasaurus.browser import Driver
        from botasaurus.user_agent import UserAgent
        from botasaurus.window_size import WindowSize

        from scraper_engine.browser._botasaurus_extension import LocalExtension
        from scraper_engine.browser._botasaurus_nav_check import raise_if_navigation_failed
        from scraper_engine.browser._botasaurus_network_capture import register_network_capture
        from scraper_engine.browser._botasaurus_scroll import botasaurus_autoscroll

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
                register_network_capture(driver, lambda: self._active_events_sink)
            if cfg.humanize_mouse:
                driver.enable_human_mode()
            if cfg.locale or cfg.timezone:
                # Must be applied before navigation — driver.py:2148-2150's own
                # docstring: "call this before navigating".
                driver.set_locale_and_timezone(
                    locale=cfg.locale or None, timezone_id=cfg.timezone or None
                )
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
            return driver, str(driver.page_html)
        except Exception:
            self._close_driver(driver)
            raise

    def _reuse_fetch(
        self, driver: Any, url: str, scroll_passes: int = 0, scroll_wait_ms: int = 1500
    ) -> str:
        """Synchronous — navigate the already-launched driver to `url`.

        Round 63: was `driver.requests.get(url)`, an in-page HTTP call that
        executed no JS. This is the same navigation `_new_driver_fetch` does,
        minus the launch — see fetch()'s docstring for why that difference
        mattered. Deliberately NOT under budget.XVFB_LOCK: no display is
        created or destroyed here, only reused.
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

    def _close_driver(self, driver: Any) -> None:
        with contextlib.suppress(Exception):
            driver.close()
        # Round 41 — driver.close() SIGKILLs the Xvfb display without
        # unlinking its lock/socket files (see browser/_xvfb_cleanup.py).
        # Best-effort, never lets cleanup failure mask the real close above.
        with contextlib.suppress(Exception):
            cleanup_stale_display(driver)

    async def shutdown(self) -> None:
        """Close the held driver, if any — called once at job end, same
        bracket BrowserPool.shutdown() is called in (orchestrator/tasks.py).

        Round 41 — under budget.XVFB_LOCK too, same reasoning as the
        eviction-close in fetch()."""
        async with self._lock:
            if self._entry is not None:
                loop = asyncio.get_running_loop()
                async with budget.XVFB_LOCK:
                    await loop.run_in_executor(None, self._close_driver, self._entry.driver)
                self._entry = None
