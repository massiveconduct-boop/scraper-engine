# fetcher/botasaurus_wrapper.py
"""Botasaurus adapter — always parallel=1 when called from our orchestrator.

Key correction vs v1.0 (F-32): Botasaurus's @browser(parallel=N) manages its OWN
multiprocessing pool internally. Nesting it inside our run_in_executor without
coordination multiplies concurrency. Fix: always pass parallel=1 — Botasaurus
becomes a single-browser driver under our control, and OUR semaphore is the only
concurrency authority in the system.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.budget import BROWSER_SEMAPHORE

if TYPE_CHECKING:
    from core.models import Proxy
    from core.tenant import TenantId


class BotasaurusWrapper:
    """Botasaurus adapter with enforced parallel=1 (closes F-32)."""

    def __init__(self, config: dict[str, object]) -> None:
        self.config = {**config, "parallel": 1}  # enforced, not caller-configurable

    async def fetch_html(
        self,
        url: str,
        proxy: Proxy,
        tenant_id: TenantId,
        session_id: str | None = None,
    ) -> str:
        """Fetch HTML via Botasaurus, gated by the same global semaphore as Camoufox."""
        async with BROWSER_SEMAPHORE:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self._botasaurus_fetch,
                url,
                proxy.url(),
            )

    def _botasaurus_fetch(self, url: str, proxy_url: str) -> str:
        """Synchronous Botasaurus fetch, run in executor."""
        try:
            from botasaurus import bt  # noqa: F401
            from botasaurus.browser import Driver, browser

            @browser(proxy=proxy_url, **self.config)  # type: ignore[untyped-decorator]
            def _fetch(driver: Driver, url: str = url) -> str:
                driver.get(url)
                return str(driver.page_source)

            return _fetch()  # type: ignore[no-any-return]
        except ImportError:
            import httpx

            response = httpx.get(url, follow_redirects=True, timeout=30)
            return str(response.text)
