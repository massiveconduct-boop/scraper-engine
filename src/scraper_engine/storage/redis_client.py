# storage/redis_client.py
"""Redis client with tenant-prefixed key wrapper.

All keys are automatically prefixed with the tenant_id to prevent cross-tenant
key collisions in a shared Redis instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as aioredis

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId


class RedisClient:
    """Tenant-scoped Redis client with automatic key prefixing."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis_url = redis_url
        self._client: aioredis.Redis[str] | None = None

    async def start(self) -> None:
        """Connect to Redis."""
        self._client = aioredis.from_url(
            self._redis_url, encoding="utf-8", decode_responses=True
        )

    @property
    def raw(self) -> aioredis.Redis[str]:
        """Return the underlying Redis client for system-level (non-tenant) operations.

        Circuit breaker, politeness controller, and other system-level components
        use this raw access. Never expose this to tenant-scoped handlers.
        """
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before accessing raw")
        return self._client

    async def stop(self) -> None:
        """Close the Redis connection."""
        if self._client:
            # types-redis' stub doesn't know about aclose() (added in redis-py
            # 5.0.1, replacing the now-deprecated close()) — real runtime has
            # it, this is a third-party stub gap, not our code.
            await self._client.aclose()  # type: ignore[attr-defined]

    def _prefix(self, tenant_id: TenantId, key: str) -> str:
        return f"{tenant_id}:{key}"

    async def get(self, tenant_id: TenantId, key: str) -> str | None:
        """Get a value with automatic tenant prefix."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before get()")
        result = await self._client.get(self._prefix(tenant_id, key))
        return str(result) if result else None

    async def set(
        self, tenant_id: TenantId, key: str, value: str | int | float, ttl: int | None = None
    ) -> None:
        """Set a value with automatic tenant prefix and optional TTL."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before set()")
        prefixed = self._prefix(tenant_id, key)
        if ttl is not None:
            await self._client.setex(prefixed, ttl, str(value))
        else:
            await self._client.set(prefixed, str(value))

    async def incrby(self, tenant_id: TenantId, key: str, amount: int = 1) -> int:
        """Increment a counter with tenant prefix."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before incrby()")
        return await self._client.incrby(self._prefix(tenant_id, key), amount)

    async def sadd(self, tenant_id: TenantId, key: str, *members: str) -> int:
        """Add members to a set with tenant prefix."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before sadd()")
        return await self._client.sadd(self._prefix(tenant_id, key), *members)

    async def srem(self, tenant_id: TenantId, key: str, *members: str) -> int:
        """Remove members from a set with tenant prefix."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before srem()")
        return await self._client.srem(self._prefix(tenant_id, key), *members)

    async def scard(self, tenant_id: TenantId, key: str) -> int:
        """Get the cardinality of a set with tenant prefix."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before scard()")
        return await self._client.scard(self._prefix(tenant_id, key))

    async def expire(self, tenant_id: TenantId, key: str, ttl: int) -> bool:
        """Set TTL on a tenant-prefixed key."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before expire()")
        return await self._client.expire(self._prefix(tenant_id, key), ttl)

    async def eval(
        self,
        script: str,
        num_keys: int,
        *args: str | int | float,
    ) -> object:
        """Execute a Lua script (not tenant-scoped — caller provides keys)."""
        if self._client is None:
            raise RuntimeError("RedisClient.start() must be called before eval()")
        # types-redis' stub leaves eval() untyped — real redis-py has it, this
        # is a third-party stub gap, not our code.
        return await self._client.eval(script, num_keys, *args)  # type: ignore[no-untyped-call]
