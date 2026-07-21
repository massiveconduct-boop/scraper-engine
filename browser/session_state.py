# browser/session_state.py
"""Cookie, localStorage, and storage_state persistence helpers.

Persists Camoufox storage_state/config references, not hand-built fingerprints
(design invariant §1.1.2).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.redis_client import RedisClient


class SessionStateManager:
    """Persist and restore browser session state (cookies, localStorage, etc.)."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def save(
        self, tenant_id: TenantId, profile_id: str, storage_state: dict[str, object]
    ) -> None:
        """Persist a storage_state blob keyed by (tenant, profile)."""
        data = json.dumps(storage_state, default=str)
        await self._redis.set(
            tenant_id, f"browser:state:{profile_id}", data, ttl=86400 * 30
        )

    async def load(
        self, tenant_id: TenantId, profile_id: str
    ) -> dict[str, object] | None:
        """Load a previously persisted storage_state, or None if not found."""
        raw = await self._redis.get(tenant_id, f"browser:state:{profile_id}")
        if raw is None:
            return None
        result: dict[str, object] = json.loads(raw)
        return result

    async def delete(self, tenant_id: TenantId, profile_id: str) -> None:
        """Remove a persisted storage_state."""
        await self._redis.set(tenant_id, f"browser:state:{profile_id}", "", ttl=1)
