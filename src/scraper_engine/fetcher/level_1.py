# fetcher/level_1.py
"""Level 1 fetcher: HTTP-only via httpx + basic extraction.

Lightest touch — no browser, no JavaScript execution, no proxy rotation.
Used when a target can be fetched with plain HTTP and simple selectors.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from scraper_engine.core.models import FailureCategory
from scraper_engine.core.ssrf_guard import SSRFGuard
from scraper_engine.fetcher._failure import classify_http_status

from .result import FetchResult

if TYPE_CHECKING:
    from scraper_engine.core.models import ConfigOverrides, Proxy
    from scraper_engine.core.tenant import TenantId
    from scraper_engine.fetcher.scrapling_wrapper import ScraplingWrapper
    from scraper_engine.services.botasaurus_requests_client import BotasaurusRequestsClient

MAX_REDIRECTS = 10


class Level1Fetcher:
    """HTTP-level fetch using httpx. Markdown conversion happens centrally in
    Worker.process_job, not here — see the module docstring."""

    TIMEOUT_SECONDS = 20

    def __init__(
        self,
        ssrf_guard: SSRFGuard | None = None,
        ja3_client: BotasaurusRequestsClient | None = None,
        scrapling_client: ScraplingWrapper | None = None,
    ) -> None:
        """Level 1 fetcher. ssrf_guard defaults to a fresh SSRFGuard() — a
        submission-time check alone leaves a DNS-rebinding /
        redirect-to-internal-target gap between enqueue and the worker
        actually connecting, so every hop is re-validated here too.

        ja3_client is optional (round 26) — the factory builds it from
        config.botasaurus.l1_ja3_client_enabled (default off, brand-new code
        path). When set, every fetch tries the JA3-matched client first,
        falling back to the plain httpx path below on any failure — same
        first-attempt/fallback shape as Level2Fetcher's Botasaurus-then-
        Camoufox pipeline.

        scrapling_client is optional (round 28) — the factory builds it when
        config.levels.level_1.engine == "scrapling" (base.yaml's default,
        matching this level's "HTTP/Scrapling" identity, previously never
        actually wired). Tried after the JA3 client (if configured) and
        before the plain httpx fallback — same shape, one more link in the
        chain.

        Markdown conversion (round 29) moved out of every level fetcher and
        into Worker.process_job, right next to the AdaptiveSelector call —
        one place that runs regardless of which level actually succeeded,
        rather than duplicated per level and only ever wired into L1."""
        self._ssrf_guard = ssrf_guard or SSRFGuard()
        self._ja3_client = ja3_client
        self._scrapling_client = scrapling_client

    async def fetch(
        self,
        url: str,
        tenant_id: TenantId,
        proxy: Proxy | None = None,
        overrides: ConfigOverrides | None = None,
    ) -> FetchResult:
        """Fetch a URL using HTTP only. No browser, no JS execution.

        Redirects are followed manually (not via httpx's follow_redirects) so
        every hop can be re-validated against the SSRF guard before it's
        followed — a redirect to a private/metadata address is rejected the
        same as a direct request to one."""
        start = time.monotonic()
        timeout = overrides.timeout_seconds if overrides else self.TIMEOUT_SECONDS

        try:
            await self._ssrf_guard.validate(url)
            if self._ja3_client is not None:
                ja3_result = await self._fetch_via_ja3(url, proxy, timeout, start)
                if ja3_result is not None:
                    return ja3_result
            if self._scrapling_client is not None:
                scrapling_result = await self._fetch_via_scrapling(url, proxy, timeout, start)
                if scrapling_result is not None:
                    return scrapling_result
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                proxy=proxy.url() if proxy else None,
            ) as client:
                current_url = url
                response = await client.get(current_url)
                for _ in range(MAX_REDIRECTS):
                    if not response.is_redirect:
                        break
                    next_url = str(response.headers.get("location") or response.url)
                    next_url = str(httpx.URL(current_url).join(next_url))
                    await self._ssrf_guard.validate(next_url)
                    current_url = next_url
                    response = await client.get(current_url)
                if response.is_redirect:
                    return self._redirect_limit_result(url, proxy, response.status_code, start)

                html = response.text
                success = response.status_code < 400
                duration_ms = int((time.monotonic() - start) * 1000)

                return FetchResult(
                    url=url,
                    success=success,
                    http_status=response.status_code,
                    html=html,
                    level_used=1,
                    proxy_used=proxy.key() if proxy else None,
                    duration_ms=duration_ms,
                    failure_category=(
                        None if success else classify_http_status(response.status_code)
                    ),
                )
        except httpx.TimeoutException:
            return FetchResult(
                url=url,
                success=False,
                level_used=1,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=FailureCategory.NETWORK_TIMEOUT,
                error_message="Request timed out",
            )
        except Exception as exc:
            from scraper_engine.fetcher._failure import classify_fetch_exception

            return FetchResult(
                url=url,
                success=False,
                level_used=1,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=classify_fetch_exception(exc, FailureCategory.NETWORK_TIMEOUT),
                error_message=str(exc),
            )

    @staticmethod
    def _redirect_limit_result(
        url: str, proxy: Proxy | None, status: int, start: float
    ) -> FetchResult:
        """Round 64 — every engine's redirect loop used to fall out of its
        `for _ in range(MAX_REDIRECTS)` with the last hop still a 3xx and then
        compute `success = status < 400`: an endless redirect was reported as
        a SUCCESSFUL fetch whose content was the redirect body. At the
        plain-HTTP level a redirect loop is almost always a cookie or JS gate
        that a real browser clears, so it is a DETECTION_BLOCK — the
        category that escalates to L2 rather than landing in the DLQ."""
        return FetchResult(
            url=url,
            success=False,
            http_status=status,
            level_used=1,
            proxy_used=proxy.key() if proxy else None,
            duration_ms=int((time.monotonic() - start) * 1000),
            failure_category=FailureCategory.DETECTION_BLOCK,
            error_message=f"Redirect limit ({MAX_REDIRECTS}) exceeded",
        )

    async def _fetch_via_ja3(
        self, url: str, proxy: Proxy | None, timeout: int, start: float
    ) -> FetchResult | None:
        """Returns None (not a FetchResult) to signal "fall back to plain
        httpx" — any exception here (network error, a TLS-fingerprint client
        quirk, etc.) is swallowed, matching Level2Fetcher's
        Botasaurus-first-attempt fallback shape. Redirects are followed
        manually, same as the httpx path above, so every hop still gets
        SSRF-revalidated (spec §1.1 #4) — the JA3 client itself is always
        called with allow_redirects=False."""
        assert self._ja3_client is not None
        try:
            proxy_url = proxy.url() if proxy else None
            current_url = url
            # One session for this fetch's entire redirect chain — not
            # self._ja3_client.get() per hop, which would open a fresh
            # session (and lose any cookies a redirect hop set) each time.
            # Scoped to this call only, so no cross-tenant/cross-request
            # cookie continuity — found during PR review, before merge.
            session = await self._ja3_client.open_session()
            response = await session.get(current_url, proxy=proxy_url)
            for _ in range(MAX_REDIRECTS):
                if response.status_code not in (301, 302, 303, 307, 308) or not response.location:
                    break
                next_url = str(httpx.URL(current_url).join(response.location))
                await self._ssrf_guard.validate(next_url)
                current_url = next_url
                response = await session.get(current_url, proxy=proxy_url)
            if response.status_code in (301, 302, 303, 307, 308) and response.location:
                return self._redirect_limit_result(url, proxy, response.status_code, start)

            success = response.status_code < 400

            return FetchResult(
                url=url,
                success=success,
                http_status=response.status_code,
                html=response.text,
                level_used=1,
                proxy_used=proxy.key() if proxy else None,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=None if success else classify_http_status(response.status_code),
            )
        except Exception:
            return None

    async def _fetch_via_scrapling(
        self, url: str, proxy: Proxy | None, timeout: int, start: float
    ) -> FetchResult | None:
        """Returns None (not a FetchResult) to signal "fall back to plain
        httpx" — scrapling not installed, or any error mid-fetch, matching
        `_fetch_via_ja3`'s fallback shape. Redirects are followed manually,
        same as the httpx and JA3 paths, so every hop still gets
        SSRF-revalidated (spec §1.1 #4) — ScraplingWrapper.fetch() is always
        called with follow_redirects=False."""
        assert self._scrapling_client is not None
        try:
            proxy_url = proxy.url() if proxy else None
            current_url = url
            response = await self._scrapling_client.fetch(current_url, timeout, proxy=proxy_url)
            if response is None:
                return None
            for _ in range(MAX_REDIRECTS):
                if response.location is None:
                    break
                next_url = str(httpx.URL(current_url).join(response.location))
                await self._ssrf_guard.validate(next_url)
                current_url = next_url
                response = await self._scrapling_client.fetch(current_url, timeout, proxy=proxy_url)
                if response is None:
                    return None
            if response.location is not None:
                return self._redirect_limit_result(url, proxy, response.status_code, start)

            success = response.status_code < 400

            return FetchResult(
                url=url,
                success=success,
                http_status=response.status_code,
                html=response.text,
                level_used=1,
                proxy_used=proxy.key() if proxy else None,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=None if success else classify_http_status(response.status_code),
            )
        except Exception:
            return None
