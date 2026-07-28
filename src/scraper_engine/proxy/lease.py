# proxy/lease.py
"""Sticky proxy lease with heartbeat + safety TTL deadman's switch.

Design invariant §1.1.6: guaranteed release is the primary path (context manager);
TTL is the deadman's switch — never both together for the same resource.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.core.models import Proxy
    from scraper_engine.core.tenant import TenantId


class ProxyLease:
    """A time-bounded, heartbeat-renewable proxy assignment.

    Use as an async context manager for guaranteed release.
    """

    def __init__(
        self,
        proxy: Proxy,
        tenant_id: TenantId,
        lease_ttl_seconds: int = 120,
    ) -> None:
        self.proxy = proxy
        self.tenant_id = tenant_id
        self.lease_ttl_seconds = lease_ttl_seconds
        self._acquired_at: datetime | None = None
        self._expires_at: float = 0.0
        self._released = False

    async def __aenter__(self) -> ProxyLease:
        self._acquired_at = datetime.now(UTC)
        self._expires_at = time.monotonic() + self.lease_ttl_seconds
        self._released = False
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    async def heartbeat(self) -> None:
        """Extend the lease TTL. Called periodically by long-running fetches."""
        self._expires_at = time.monotonic() + self.lease_ttl_seconds

    async def release(self) -> None:
        """Explicitly release the lease back to the pool."""
        self._released = True
        self._expires_at = 0.0

    @property
    def is_expired(self) -> bool:
        """Check if the lease has expired (deadman's switch)."""
        return time.monotonic() > self._expires_at

    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining before lease expiry."""
        return max(0.0, self._expires_at - time.monotonic())
