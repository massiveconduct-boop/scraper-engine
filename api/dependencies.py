# api/dependencies.py
"""FastAPI dependency injection — provides tenant-scoped resources to route handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.worker import Worker
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient

    from .auth import TenantResolver

# Module-level singletons — initialized at startup
_tenant_resolver: TenantResolver | None = None
_storage_pg: PostgresClient | None = None
_storage_redis: RedisClient | None = None
_worker: Worker | None = None


async def get_tenant_resolver() -> TenantResolver:
    """Dependency: return the TenantResolver singleton."""
    if _tenant_resolver is None:
        raise RuntimeError("TenantResolver not initialized")
    return _tenant_resolver


async def get_postgres() -> PostgresClient:
    """Dependency: return the PostgresClient singleton."""
    if _storage_pg is None:
        raise RuntimeError("PostgresClient not initialized")
    return _storage_pg


async def get_redis() -> RedisClient:
    """Dependency: return the RedisClient singleton."""
    if _storage_redis is None:
        raise RuntimeError("RedisClient not initialized")
    return _storage_redis


def init_dependencies(
    tenant_resolver: TenantResolver,
    pg: PostgresClient,
    redis: RedisClient,
    worker: Worker,
) -> None:
    """Initialize all module-level singletons at startup."""
    global _tenant_resolver, _storage_pg, _storage_redis, _worker
    _tenant_resolver = tenant_resolver
    _storage_pg = pg
    _storage_redis = redis
    _worker = worker
