# fetcher/level_1.py
"""Level 1 fetcher: HTTP-only via httpx + basic extraction.

Lightest touch — no browser, no JavaScript execution, no proxy rotation.
Used when a target can be fetched with plain HTTP and simple selectors.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from core.models import FailureCategory
from core.ssrf_guard import SSRFGuard

from .result import FetchResult

if TYPE_CHECKING:
    from core.models import ConfigOverrides, Proxy
    from core.tenant import TenantId
    from services.firecrawl_client import FirecrawlClient

MAX_REDIRECTS = 10


class Level1Fetcher:
    """HTTP-level fetch using httpx with optional markdown conversion."""

    TIMEOUT_SECONDS = 20

    def __init__(
        self,
        firecrawl_client: FirecrawlClient | None = None,
        ssrf_guard: SSRFGuard | None = None,
    ) -> None:
        """Level 1 fetcher. firecrawl_client is optional — the factory builds it
        once from FIRECRAWL_API_KEY; None disables markdown conversion (fetch
        still runs, FetchResult.markdown just stays unset). ssrf_guard defaults
        to a fresh SSRFGuard() — a submission-time check alone leaves a
        DNS-rebinding / redirect-to-internal-target gap between enqueue and the
        worker actually connecting, so every hop is re-validated here too."""
        self._firecrawl = firecrawl_client
        self._ssrf_guard = ssrf_guard or SSRFGuard()

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
                    next_url = str(
                        response.headers.get("location") or response.url
                    )
                    next_url = str(httpx.URL(current_url).join(next_url))
                    await self._ssrf_guard.validate(next_url)
                    current_url = next_url
                    response = await client.get(current_url)

                html = response.text
                success = response.status_code < 400

                markdown = None
                if success and self._firecrawl is not None:
                    markdown = await self._firecrawl.convert_to_markdown(html, url)

                duration_ms = int((time.monotonic() - start) * 1000)

                return FetchResult(
                    url=url,
                    success=success,
                    http_status=response.status_code,
                    html=html,
                    markdown=markdown,
                    level_used=1,
                    proxy_used=proxy.key() if proxy else None,
                    duration_ms=duration_ms,
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
            from fetcher._failure import classify_fetch_exception
            return FetchResult(
                url=url,
                success=False,
                level_used=1,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=classify_fetch_exception(
                    exc, FailureCategory.NETWORK_TIMEOUT
                ),
                error_message=str(exc),
            )
