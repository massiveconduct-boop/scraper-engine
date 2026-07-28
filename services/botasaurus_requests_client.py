# services/botasaurus_requests_client.py
"""JA3 TLS-fingerprint-matched HTTP client for L1, via `botasaurus_requests`
(already a transitive dependency of `botasaurus`, declared directly in
pyproject.toml here since this module imports it directly). Verified against
the real installed botasaurus_requests==4.0.38 source
(botasaurus_requests/reqs.py's `request()`/`get()`, session.py's `firefox`
JA3-matched session shortcut) — not just its docs.

Config-gated via config.botasaurus.l1_ja3_client_enabled (default False,
round 26) — a brand-new code path with no live-traffic validation yet, same
env/config-gated-optional pattern as services/firecrawl_client.py.

`allow_redirects` is always forced False here — fetcher/level_1.py owns the
redirect-following loop so every hop gets SSRF re-validated (spec §1.1
invariant #4); letting this client follow redirects internally would skip
that entirely.
"""

from __future__ import annotations

import asyncio
from typing import Any, NamedTuple


class Ja3Response(NamedTuple):
    status_code: int
    text: str
    location: str | None


class Ja3Session:
    """One botasaurus_requests session, scoped to a single top-level fetch
    (e.g. one Level1Fetcher.fetch() call and every redirect hop it follows)
    — never reused across separate fetches, so there's no cross-tenant or
    cross-request cookie leakage the way a process-wide shared session would
    have. A fresh `firefox.Session()` per `.get()` call (the original round-26
    design) lost cookies between redirect hops within the *same* fetch —
    found during PR review, before merge — which this fixes by keeping one
    session for the caller's own redirect loop."""

    def __init__(self, session: Any, timeout_seconds: float) -> None:
        self._session = session
        self._timeout_seconds = timeout_seconds

    async def get(self, url: str, proxy: str | None = None) -> Ja3Response:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get, url, proxy)

    def _get(self, url: str, proxy: str | None) -> Ja3Response:
        response = self._session.get(
            url,
            proxies={"http": proxy, "https": proxy} if proxy else None,
            allow_redirects=False,
            timeout=self._timeout_seconds,
        )
        return Ja3Response(
            status_code=response.status_code,
            text=response.text,
            location=response.headers.get("location"),
        )


class BotasaurusRequestsClient:
    """Thin async wrapper over botasaurus_requests' synchronous, JA3-matched
    `firefox` session client — run in an executor (same sync-library-in-
    executor pattern as fetcher/botasaurus_wrapper.py's _botasaurus_fetch)."""

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def open_session(self) -> Ja3Session:
        """Open one session for the caller to reuse across every hop of a
        single redirect chain — see Ja3Session's docstring for why."""
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(None, self._new_session)
        return Ja3Session(session, self._timeout_seconds)

    def _new_session(self) -> Any:
        from botasaurus_requests.session import firefox

        return firefox.Session()

    async def get(self, url: str, proxy: str | None = None) -> Ja3Response:
        """One-shot convenience — opens and discards a session for a single
        call. Callers that need cookie continuity across multiple requests
        (e.g. a redirect chain) should use open_session() instead."""
        session = await self.open_session()
        return await session.get(url, proxy=proxy)


def build_ja3_client(
    enabled: bool, timeout_seconds: float = 20.0
) -> BotasaurusRequestsClient | None:
    """None disables the JA3 client entirely — Level1Fetcher falls back to
    plain httpx, same shape as firecrawl_client's build_firecrawl_client()."""
    if not enabled:
        return None
    return BotasaurusRequestsClient(timeout_seconds=timeout_seconds)
