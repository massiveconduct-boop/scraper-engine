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

from .result import FetchResult

if TYPE_CHECKING:
    from core.models import ConfigOverrides, Proxy
    from core.tenant import TenantId


class Level1Fetcher:
    """HTTP-level fetch using httpx with optional markdown conversion."""

    TIMEOUT_SECONDS = 20

    def __init__(self) -> None:
        """Level 1 fetcher — no initialization required."""

    async def fetch(
        self,
        url: str,
        tenant_id: TenantId,
        proxy: Proxy | None = None,
        overrides: ConfigOverrides | None = None,
    ) -> FetchResult:
        """Fetch a URL using HTTP only. No browser, no JS execution."""
        start = time.monotonic()
        timeout = overrides.timeout_seconds if overrides else self.TIMEOUT_SECONDS

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                proxy=proxy.url() if proxy else None,
            ) as client:
                response = await client.get(url)
                html = response.text
                duration_ms = int((time.monotonic() - start) * 1000)

                return FetchResult(
                    url=url,
                    success=response.status_code < 400,
                    http_status=response.status_code,
                    html=html,
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
            return FetchResult(
                url=url,
                success=False,
                level_used=1,
                duration_ms=int((time.monotonic() - start) * 1000),
                error_message=str(exc),
            )
