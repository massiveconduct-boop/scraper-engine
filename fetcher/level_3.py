# fetcher/level_3.py
"""Level 3 fetcher: Camoufox-only (nuclear option).

Heaviest touch — full Camoufox browser with elite proxy, CAPTCHA solving.
Used only when L1 and L2 have both failed. Most expensive path.
"""

from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, cast

from browser.camoufox_wrapper import CamoufoxWrapper
from core.models import FailureCategory
from core.ssrf_guard import SSRFGuard
from fetcher._content_utils import (
    SSRFRouteGuard,
    autoscroll,
    poll_until_solved,
    safe_content,
)
from fetcher._failure import classify_fetch_exception
from fetcher.challenge_detector import ChallengeDetector

from .result import FetchResult

if TYPE_CHECKING:
    from browser.pool import BrowserPool
    from core.models import ConfigOverrides, Proxy
    from core.tenant import TenantId
    from services.captcha_solver import CaptchaSolver


class Level3Fetcher:
    """Full Camoufox browser fetch with elite proxy. Nuclear option."""

    TIMEOUT_SECONDS = 60

    def __init__(
        self,
        *,
        goto_wait_until: str = "load",
        post_load_fixed_wait_ms: int = 10000,
        max_total_wait_ms: int = 30000,
        retry_wait_increment_ms: int = 5000,
        scroll_passes: int = 0,
        scroll_wait_ms: int = 1500,
        challenge_detector: ChallengeDetector | None = None,
        captcha_solver: CaptchaSolver | None = None,
        ssrf_guard: SSRFGuard | None = None,
        pool: BrowserPool | None = None,
    ) -> None:
        """Level 3 fetcher with config-driven bounded retry for CPU-bound challenges.

        Args:
            goto_wait_until: page.goto() wait_until strategy (default "load").
            post_load_fixed_wait_ms: initial post-load delay for PoW solver.
            max_total_wait_ms: hard ceiling on total post-load wait time.
            retry_wait_increment_ms: additional wait per retry cycle when
                the page still looks like an unsolved challenge interstitial.
            challenge_detector: classifier for challenge pages. A
                default-configured instance is used when None.
        """
        self._goto_wait_until = goto_wait_until
        self._post_load_fixed_wait_ms = post_load_fixed_wait_ms
        self._max_total_wait_ms = max_total_wait_ms
        self._retry_wait_increment_ms = retry_wait_increment_ms
        self._scroll_passes = scroll_passes
        self._scroll_wait_ms = scroll_wait_ms
        self._challenge_detector = challenge_detector or ChallengeDetector()
        self._captcha_solver = captcha_solver
        self._ssrf_guard = ssrf_guard or SSRFGuard()
        self._pool = pool

    async def fetch(
        self,
        url: str,
        tenant_id: TenantId,
        proxy: Proxy,
        overrides: ConfigOverrides | None = None,
    ) -> FetchResult:
        """Fetch a URL using Camoufox-only with elite proxy."""
        start = time.monotonic()
        timeout = overrides.timeout_seconds if overrides else self.TIMEOUT_SECONDS

        try:
            browser_ctx_mgr: AbstractAsyncContextManager[Any]
            if self._pool is not None:
                from urllib.parse import urlparse

                domain = urlparse(url).hostname or "unknown"
                browser_ctx_mgr = cast(
                    "AbstractAsyncContextManager[Any]",
                    self._pool.lease(proxy=proxy, domain=domain),
                )
            else:
                browser_ctx_mgr = cast(
                    "AbstractAsyncContextManager[Any]",
                    CamoufoxWrapper(proxy=proxy, tenant_id=tenant_id),
                )
            async with browser_ctx_mgr as browser_context:
                page = await browser_context.new_page()
                route_guard = SSRFRouteGuard(self._ssrf_guard)
                await route_guard.install(page)
                try:
                    await page.goto(
                        url, wait_until=self._goto_wait_until, timeout=timeout * 1000
                    )
                except Exception:
                    route_guard.raise_if_blocked()
                    raise
                # CPU-bound client-side JS (e.g. PoW solvers) cannot be detected
                # by networkidle — the browser is computing, not fetching. Use a
                # config-driven bounded retry loop: wait an initial fixed period,
                # then poll at retry_wait_increment_ms intervals until
                # ChallengeDetector no longer classifies the page as a challenge
                # interstitial, or max_total_wait_ms ceiling is hit.
                await page.wait_for_timeout(self._post_load_fixed_wait_ms)
                html = await poll_until_solved(
                    page,
                    self._challenge_detector,
                    max_total_wait_ms=self._max_total_wait_ms,
                    retry_wait_increment_ms=self._retry_wait_increment_ms,
                    waited_ms=self._post_load_fixed_wait_ms,
                )
                # Token-grant CAPTCHA (reCAPTCHA/hCaptcha/Turnstile) won't clear
                # by waiting — solve it (read sitekey → provider token → inject →
                # re-poll). Best-effort, no-op without a configured solver
                # (round 20 — wires services/captcha_solver into fetch).
                html = await self._maybe_solve_captcha(page, url, tenant_id, html)
                # Lazy-load / infinite-scroll: scroll to load the rest once past
                # the challenge, then re-read the fully-populated DOM.
                if self._scroll_passes > 0:
                    await autoscroll(
                        page,
                        max_passes=self._scroll_passes,
                        wait_ms=self._scroll_wait_ms,
                    )
                    html = await safe_content(page)
                duration_ms = int((time.monotonic() - start) * 1000)

                return FetchResult(
                    url=url,
                    success=True,
                    http_status=200,
                    html=html,
                    level_used=3,
                    proxy_used=proxy.key() if proxy else "none",
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return FetchResult(
                url=url,
                success=False,
                level_used=3,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=classify_fetch_exception(
                    exc, FailureCategory.BROWSER_CRASH
                ),
                error_message=str(exc),
                proxy_used=proxy.key() if proxy else "none",
            )

    async def _maybe_solve_captcha(
        self, page: Any, url: str, tenant_id: TenantId, html: str | None
    ) -> str | None:
        """Attempt an in-page CAPTCHA solve when the page still looks like a
        challenge and a solver is configured. Returns the re-read HTML after a
        successful solve, or the original `html` unchanged otherwise."""
        if (
            self._captcha_solver is None
            or html is None
            or not self._challenge_detector.is_challenge_page(
                html, 200, short_page_is_suspect=False
            )
        ):
            return html
        from fetcher._captcha import solve_captcha_on_page

        solved = await solve_captcha_on_page(
            page, solver=self._captcha_solver, tenant_id=tenant_id, url=url
        )
        if not solved:
            return html
        # Token injected — let the site validate it / redirect, then re-poll.
        await page.wait_for_timeout(self._retry_wait_increment_ms)
        return await poll_until_solved(
            page,
            self._challenge_detector,
            max_total_wait_ms=self._max_total_wait_ms,
            retry_wait_increment_ms=self._retry_wait_increment_ms,
            waited_ms=0,
        )
