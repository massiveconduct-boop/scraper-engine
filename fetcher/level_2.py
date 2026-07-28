# fetcher/level_2.py
"""Level 2 fetcher: Botasaurus + Camoufox with sticky proxy.

Medium touch — full browser with anti-detection, CAPTCHA solving enabled.
Botasaurus always runs with parallel=1 (our orchestrator owns concurrency).
"""

from __future__ import annotations

import contextlib
import time
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
    from core.models import ConfigOverrides, Proxy
    from core.tenant import TenantId
    from services.captcha_solver import CaptchaSolver


class Level2Fetcher:
    """Browser-level fetch using Botasaurus + Camoufox with sticky proxy."""

    TIMEOUT_SECONDS = 40

    def __init__(
        self,
        *,
        goto_wait_until: str = "domcontentloaded",
        networkidle_timeout_ms: int = 5000,
        max_total_wait_ms: int = 15000,
        retry_wait_increment_ms: int = 5000,
        scroll_passes: int = 0,
        scroll_wait_ms: int = 1500,
        challenge_detector: ChallengeDetector | None = None,
        captcha_solver: CaptchaSolver | None = None,
        ssrf_guard: SSRFGuard | None = None,
        force_engine: str | None = None,  # TEST-ONLY. See guard below.
    ) -> None:
        """Level 2 fetcher with config-driven wait strategy + challenge-gated retry.

        Args:
            goto_wait_until: page.goto() wait_until strategy (default "domcontentloaded").
            networkidle_timeout_ms: max wait for networkidle after domcontentloaded.
            max_total_wait_ms: ceiling on the ChallengeDetector-gated retry loop
                that waits out a still-solving challenge (round 14 — closes the
                networkidle-vs-PoW-redirect timing race that made L2 flaky).
            retry_wait_increment_ms: poll interval for that retry loop.
            challenge_detector: classifier for challenge pages. Default instance
                if None — the same single source of truth L3 uses.
            force_engine: TEST-ONLY escape hatch. Only accepts None (production)
                or the literal "raw_playwright" (negative-control test). Any other
                value raises. Production code never passes this — enforced by the
                "force_engine test-seam never reachable from production" CI gate
                and by fetcher/factory.py never setting it.
        """
        if force_engine is not None and force_engine not in ("raw_playwright",):
            raise ValueError(
                f"force_engine must be None or 'raw_playwright', got {force_engine!r}"
            )
        self._force_engine = force_engine
        self._goto_wait_until = goto_wait_until
        self._networkidle_timeout_ms = networkidle_timeout_ms
        self._max_total_wait_ms = max_total_wait_ms
        self._retry_wait_increment_ms = retry_wait_increment_ms
        self._scroll_passes = scroll_passes
        self._scroll_wait_ms = scroll_wait_ms
        self._challenge_detector = challenge_detector or ChallengeDetector()
        self._captcha_solver = captcha_solver
        self._ssrf_guard = ssrf_guard or SSRFGuard()

    async def fetch(
        self,
        url: str,
        tenant_id: TenantId | None = None,
        proxy: Proxy | None = None,
        overrides: ConfigOverrides | None = None,
    ) -> FetchResult:
        """Fetch a URL. Dispatches to Camoufox (production) or, only when the
        test seam is armed, to raw undetected Playwright (negative control)."""
        if self._force_engine == "raw_playwright":
            return await self._fetch_via_raw_playwright(url, proxy, overrides)
        return await self._fetch_via_camoufox(url, tenant_id, proxy, overrides)

    async def _fetch_via_camoufox(
        self,
        url: str,
        tenant_id: TenantId | None,
        proxy: Proxy | None,
        overrides: ConfigOverrides | None,
    ) -> FetchResult:
        """Production path: Botasaurus+Camoufox with sticky proxy."""
        start = time.monotonic()
        timeout = overrides.timeout_seconds if overrides else self.TIMEOUT_SECONDS

        try:
            wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=tenant_id)
            async with wrapper as browser_context:
                page = await browser_context.new_page()  # type: ignore[attr-defined]
                route_guard = SSRFRouteGuard(self._ssrf_guard)
                await route_guard.install(page)
                try:
                    await page.goto(
                        url, wait_until=self._goto_wait_until, timeout=timeout * 1000
                    )
                except Exception:
                    route_guard.raise_if_blocked()
                    raise
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state(
                        "networkidle", timeout=self._networkidle_timeout_ms
                    )
                # networkidle can fire before an in-page PoW has POSTed its
                # solution and redirected — reading here would grab the unsolved
                # interstitial. Same bug class L3 already solves: guard the read
                # and poll (ChallengeDetector-gated) until real content appears
                # or the ceiling is hit. This is the round-14 flakiness fix.
                html = await poll_until_solved(
                    page,
                    self._challenge_detector,
                    max_total_wait_ms=self._max_total_wait_ms,
                    retry_wait_increment_ms=self._retry_wait_increment_ms,
                    waited_ms=0,
                )
                # Still a challenge after waiting? A token-grant widget (reCAPTCHA
                # /hCaptcha/Turnstile) won't clear by waiting — solve it: read the
                # sitekey, get a token from the provider, inject it, then re-poll.
                # Best-effort and gated on a solver being configured; no-op
                # otherwise (round 20 — wires services/captcha_solver into fetch).
                html = await self._maybe_solve_captcha(page, url, tenant_id, html)
                # Lazy-load / infinite-scroll: once past any challenge, scroll to
                # load the rest, then re-read the fully-populated DOM (round 15
                # follow-up — a single read only captured the first batch).
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
                    level_used=2,
                    proxy_used=proxy.key() if proxy else "none",
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return FetchResult(
                url=url,
                success=False,
                level_used=2,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=classify_fetch_exception(
                    exc, FailureCategory.BROWSER_CRASH
                ),
                error_message=str(exc),
                proxy_used=proxy.key() if proxy else "none",
            )

    async def _maybe_solve_captcha(
        self, page: Any, url: str, tenant_id: TenantId | None, html: str | None
    ) -> str | None:
        """Attempt an in-page CAPTCHA solve when the page still looks like a
        challenge and a solver + tenant are available. Returns the re-read HTML
        after a successful solve, or the original `html` unchanged otherwise."""
        if (
            self._captcha_solver is None
            or tenant_id is None
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

    async def _fetch_via_raw_playwright(
        self,
        url: str,
        proxy: Proxy | None,
        overrides: ConfigOverrides | None,
    ) -> FetchResult:
        """TEST-ONLY PATH. Launches vanilla Playwright Firefox with NO fingerprint
        spoofing whatsoever — deliberately the exact anti-pattern Camoufox exists
        to replace (blueprint v2 §3.4, F-02/F-03). navigator.webdriver is left at
        Playwright's default (true). Exists solely so the negative-control test can
        prove the challenge mirror correctly rejects an undetected-automation
        session. Never reachable from any production call path — enforced by the
        __init__ guard, fetcher/factory.py (never sets force_engine), and the CI
        force_engine grep-gate."""
        start = time.monotonic()
        timeout = overrides.timeout_seconds if overrides else self.TIMEOUT_SECONDS
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.firefox.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                # goto_wait_until is a config str; Playwright wants a Literal. Cast
                # rather than constrain config to the enum (test-only raw path).
                await page.goto(
                    url,
                    wait_until=cast("Any", self._goto_wait_until),
                    timeout=timeout * 1000,
                )
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state(
                        "networkidle", timeout=self._networkidle_timeout_ms
                    )
                html = await page.content()
                await browser.close()
                duration_ms = int((time.monotonic() - start) * 1000)
                return FetchResult(
                    url=url,
                    success=True,
                    http_status=200,
                    html=html,
                    level_used=2,
                    proxy_used="none",
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return FetchResult(
                url=url,
                success=False,
                level_used=2,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=classify_fetch_exception(
                    exc, FailureCategory.BROWSER_CRASH
                ),
                error_message=str(exc),
                proxy_used="none",
            )
