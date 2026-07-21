# storage/dedup.py
"""Success-gated content deduplication cache.

Design invariant §1.1.5: nothing is cached as successful content unless
FetchResult.success is True and the response is not a classified challenge page.

Keyed on (tenant_id, url) → last successful content hash. Content hash is used
for change detection only, not as the primary cache key (closes F-09).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import FetchResult
    from core.tenant import TenantId

    from .redis_client import RedisClient


class DeduplicationEngine:
    """Success-gated content cache with content-hash change detection."""

    def __init__(
        self,
        redis: RedisClient,
        ttl_seconds: int = 86400,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    def _dedup_key(self, tenant_id: TenantId, url: str) -> str:
        """Build the dedup cache key for a tenant+URL pair."""
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        return f"dedup:{url_hash}"

    @staticmethod
    def _content_hash(result: FetchResult) -> str:
        """Compute a stable content hash from a FetchResult's HTML + markdown."""
        hasher = hashlib.sha256()
        if result.html:
            hasher.update(result.html.encode())
        if result.markdown:
            hasher.update(result.markdown.encode())
        return hasher.hexdigest()

    async def get(
        self, url: str, tenant_id: TenantId
    ) -> FetchResult | None:
        """Return cached successful FetchResult, or None if not cached/stale.

        Keyed on (tenant_id, url) → last successful content hash.
        Content hash used only for change detection, not as primary cache key.
        """
        from core.models import FetchResult

        key = self._dedup_key(tenant_id, url)
        raw = await self._redis.get(tenant_id, key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return FetchResult.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            return None

    async def store(self, result: FetchResult, tenant_id: TenantId) -> None:
        """Store a successful, non-challenge FetchResult in the cache.

        Design invariant §1.1.5: only stores if success=True and not a challenge page.
        """
        if not result.success or result.is_challenge_page:
            return

        key = self._dedup_key(tenant_id, result.url)
        data = result.model_dump_json()
        await self._redis.set(tenant_id, key, data, ttl=self._ttl_seconds)

    async def invalidate(self, url: str, tenant_id: TenantId) -> None:
        """Remove a cached entry for a specific URL."""
        key = self._dedup_key(tenant_id, url)
        await self._redis.set(tenant_id, key, "", ttl=1)

    async def has_changed(self, result: FetchResult, tenant_id: TenantId) -> bool:
        """Check if content has changed since last cached version."""
        cached = await self.get(result.url, tenant_id)
        if cached is None:
            return True
        return self._content_hash(result) != self._content_hash(cached)
