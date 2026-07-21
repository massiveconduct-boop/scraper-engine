# fetcher/level_2.py
"""Level 2 fetcher: Botasaurus + Camoufox with sticky proxy.

Medium touch — full browser with anti-detection, CAPTCHA solving enabled.
Botasaurus always runs with parallel=1 (our orchestrator owns concurrency).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.models import FailureCategory

from .result import FetchResult

if TYPE_CHECKING:
    from browser.camoufox_wrapper import CamoufoxWrapper
    from core.models import ConfigOverrides, Proxy
    from core.tenant import TenantId


class Level2Fetcher:
    """Browser-level fetch using Botasaurus + Camoufox with sticky proxy."""

    TIMEOUT_SECONDS = 40

    def __init__(self) -> None:
        """Level 2 fetcher — no initialization required."""

    async def fetch(
        self,
        url: str,
        tenant_id: TenantId,
        proxy: Proxy,
        overrides: ConfigOverrides | None = None,
    ) -> FetchResult:
        """Fetch a URL using Botasaurus+Camoufox. Requires sticky proxy."""
        start = time.monotonic()
        timeout = overrides.timeout_seconds if overrides else self.TIMEOUT_SECONDS

        try:
            wrapper = CamoufoxWrapper(proxy=proxy, tenant_id=tenant_id)
            async with wrapper as browser_context:
                page = await browser_context.new_page()  # type: ignore[attr-defined]
                await page.goto(url, timeout=timeout * 1000)
                html = await page.content()
                duration_ms = int((time.monotonic() - start) * 1000)

                return FetchResult(
                    url=url,
                    success=True,
                    http_status=200,
                    html=html,
                    level_used=2,
                    proxy_used=proxy.key(),
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return FetchResult(
                url=url,
                success=False,
                level_used=2,
                duration_ms=int((time.monotonic() - start) * 1000),
                failure_category=FailureCategory.BROWSER_CRASH,
                error_message=str(exc),
                proxy_used=proxy.key(),
            )
