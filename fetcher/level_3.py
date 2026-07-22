# fetcher/level_3.py
"""Level 3 fetcher: Camoufox-only (nuclear option).

Heaviest touch — full Camoufox browser with elite proxy, CAPTCHA solving.
Used only when L1 and L2 have both failed. Most expensive path.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from browser.camoufox_wrapper import CamoufoxWrapper
from core.models import FailureCategory

from .result import FetchResult

if TYPE_CHECKING:
    from core.models import ConfigOverrides, Proxy
    from core.tenant import TenantId


class Level3Fetcher:
    """Full Camoufox browser fetch with elite proxy. Nuclear option."""

    TIMEOUT_SECONDS = 60

    def __init__(self) -> None:
        """Level 3 fetcher — no initialization required."""

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
            wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=tenant_id)
            async with wrapper as browser_context:
                page = await browser_context.new_page()  # type: ignore[attr-defined]
                await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
                html = await page.content()
                duration_ms = int((time.monotonic() - start) * 1000)

                return FetchResult(
                    url=url,
                    success=True,
                    http_status=200,
                    html=html,
                    level_used=3,
                    proxy_used=proxy.key(),
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return FetchResult(
                url=url,
                success=False,
                level_used=3,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=FailureCategory.BROWSER_CRASH,
                error_message=str(exc),
                proxy_used=proxy.key(),
            )
