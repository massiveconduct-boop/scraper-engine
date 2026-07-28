# fetcher/botasaurus_wrapper.py
"""Botasaurus adapter — always parallel=1 when called from our orchestrator.

Key correction vs v1.0 (F-32, spec §3.6): Botasaurus's @browser(parallel=N)
manages its OWN multiprocessing pool internally. Nesting it inside our own
concurrency control without coordination multiplies concurrency. Fix: always
force parallel=1 — Botasaurus becomes a single-browser driver under our
control, and OUR semaphore (core.budget.BROWSER_SEMAPHORE) is the only
concurrency authority in the system, shared with the Camoufox path.

headless=False + enable_xvfb_virtual_display=True (never headless=True) is
the same "virtual display, not a headless flag" anti-detection posture as
Camoufox's headless_mode="virtual" (spec §3.4) — confirmed live that
botasaurus rejects the combination of headless=True with
enable_xvfb_virtual_display=True (ValueError), and headless=True is the more
easily fingerprinted mode anyway.

Round 25: restored from an orphaned, never-installed, never-imported state.
The prior version of this file called `driver.page_source` — that attribute
doesn't exist on botasaurus's Driver (confirmed against the real installed
package; it exposes `driver.page_html` instead) — so even if it had been
wired up, it would have raised AttributeError on the first real fetch. Never
actually ran for real until now.

Round 26: capability upgrade, every addition re-verified against the real
installed botasaurus==4.0.97 / botasaurus_driver==4.0.93 source (not just the
README) — see config/schema.py's BotasaurusConfig docstring and
.claude/knowledge/architecture.md -> "Botasaurus Integration". Adds
`driver.google_get(bypass_cloudflare=True)` in place of plain `driver.get()`
(free Cloudflare-tier bypass via Google-referrer spoofing + human-like
Turnstile solving — biggest single win, no CapSolver spend), `tiny_profile`
(~1KB vs ~100MB per persisted profile — real disk-growth fix),
`remove_default_browser_check_argument`/`close_on_crash` (concrete
anti-detection/reliability settings verified present in botasaurus's own
`@browser` decorator), `driver.short_random_sleep()` after load, `max_retry`
(botasaurus's own internal retry+backoff, off by default), and
`UserAgent.HASHED`/`WindowSize.HASHED` (real string constants, deterministic
per-profile) paired with the `profile=session_id` we already set — never
`UserAgent.RANDOM`/`WindowSize.RANDOM`, which botasaurus's own maintainers
advise against as a default.

Explicitly NOT done here: flipping botasaurus's own `reuse_driver=True`.
Reading `botasaurus/browser_decorator.py` directly showed its internal
`_driver_pool` is a bare unkeyed module-level list (`.pop()`/`.append()`,
no matching on proxy/profile/tenant) — naively enabling it would let one
tenant's fetch silently receive a driver still configured with a different
tenant's proxy, violating the tenant-isolation invariant (spec §1.1 #3).
Same-domain driver reuse for multi-URL jobs is instead handled by
`browser/botasaurus_pool.py::BotasaurusPool`, which holds raw `Driver`
objects we construct and key ourselves (proxy+domain matched, exactly like
`browser/pool.py::BrowserPool` already does for Camoufox), never touching
botasaurus's own pool.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import core.budget

if TYPE_CHECKING:
    from core.models import Proxy
    from core.tenant import TenantId


class BotasaurusWrapper:
    """Botasaurus adapter with enforced parallel=1 (closes F-32).

    One-shot per fetch_html() call — Botasaurus's @browser decorator launches
    and tears down its own driver per invocation (reuse_driver=False), so
    there's no persistent-instance lifecycle to pool the way BrowserPool
    pools CamoufoxWrapper. `session_id`, when given, maps to Botasaurus's own
    named profile directory (its native cookie/localStorage persistence
    mechanism) so repeat fetches for the same tenant+domain keep continuity
    without us re-deriving Camoufox's storage_state serialization here.
    """

    def __init__(
        self,
        config: dict[str, object] | None = None,
        *,
        bypass_cloudflare: bool = True,
        tiny_profile: bool = True,
        remove_default_browser_check_argument: bool = True,
        close_on_crash: bool = True,
        use_random_sleep: bool = True,
        hashed_fingerprint: bool = True,
        max_retry: int = 0,
    ) -> None:
        self.config: dict[str, object] = dict(config or {})
        self._bypass_cloudflare = bypass_cloudflare
        self._tiny_profile = tiny_profile
        self._remove_default_browser_check_argument = remove_default_browser_check_argument
        self._close_on_crash = close_on_crash
        self._use_random_sleep = use_random_sleep
        self._hashed_fingerprint = hashed_fingerprint
        self._max_retry = max_retry

    async def fetch_html(
        self,
        url: str,
        proxy: Proxy,
        tenant_id: TenantId,
        session_id: str | None = None,
    ) -> str:
        """Fetch HTML via Botasaurus, gated by the same global semaphore as Camoufox."""
        async with core.budget.BROWSER_SEMAPHORE:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self._botasaurus_fetch,
                url,
                proxy.url(),
                session_id,
            )

    def _botasaurus_fetch(self, url: str, proxy_url: str, session_id: str | None) -> str:
        """Synchronous Botasaurus fetch, run in executor — Botasaurus's driver
        management is Selenium-based (no native asyncio API to await on)."""
        from botasaurus.browser import Driver, browser
        from botasaurus.user_agent import UserAgent
        from botasaurus.window_size import WindowSize

        decorator_kwargs: dict[str, object] = {
            "headless": False,
            "enable_xvfb_virtual_display": True,
            "reuse_driver": False,  # never botasaurus's own pool — see module docstring
            "raise_exception": True,
            "output": None,  # don't write output/<fn>.json — this is a fetch, not a scrape job
            "create_error_logs": False,
            "profile": session_id,
            "proxy": proxy_url,
            # tiny_profile requires a profile (verified live — botasaurus_driver's
            # Config raises ValueError("Profile must be given when using tiny
            # profile") otherwise), so it's only meaningful paired with one.
            "tiny_profile": self._tiny_profile and session_id is not None,
            "remove_default_browser_check_argument": self._remove_default_browser_check_argument,
            "close_on_crash": self._close_on_crash,
        }
        if self._max_retry > 0:
            decorator_kwargs["max_retry"] = self._max_retry
        if self._hashed_fingerprint and session_id is not None:
            # Deterministic per-profile fingerprint — only meaningful paired
            # with a persisted profile; a bare UA/window-size hash with no
            # profile to key off of would just be noise.
            decorator_kwargs["user_agent"] = UserAgent.HASHED
            decorator_kwargs["window_size"] = WindowSize.HASHED
        decorator_kwargs.update(self.config)  # caller overrides layer on top of the defaults above
        decorator_kwargs["parallel"] = 1  # always forced last — never caller-configurable (F-32)

        bypass_cloudflare = self._bypass_cloudflare
        use_random_sleep = self._use_random_sleep

        @browser(**decorator_kwargs)  # type: ignore[untyped-decorator]
        def _fetch(driver: Driver, _data: object = None) -> str:
            # botasaurus's own decorator always calls the wrapped function as
            # func(driver, data) — POSITIONALLY (browser_decorator.py's
            # run_task) — so a second parameter with a default value (e.g.
            # `target_url: str = url`) gets silently clobbered by `data`
            # (None, since _fetch() below is invoked with no arguments) rather
            # than ever using that default. Confirmed live: this sent `None`
            # as Page.navigate's url, raising ChromeException("Invalid
            # parameters ... Failed to deserialize params.url - string value
            # expected"), which looked like a Chrome/CDP incompatibility but
            # wasn't — url must be read from the outer closure, never from a
            # same-named parameter default.
            if bypass_cloudflare:
                driver.google_get(url, bypass_cloudflare=True)
            else:
                driver.get(url)
            if use_random_sleep:
                driver.short_random_sleep()
            return str(driver.page_html)

        return str(_fetch())
