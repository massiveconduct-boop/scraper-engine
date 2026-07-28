# api/dependencies.py
"""FastAPI dependency injection — provides tenant-scoped resources to route handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rq import Queue

    from core.ssrf_guard import SSRFGuard
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient
    from storage.s3_client import S3Client

    from .auth import TenantResolver

# Module-level singletons — initialized at startup (api/main.py lifespan)
_tenant_resolver: TenantResolver | None = None
_storage_pg: PostgresClient | None = None
_storage_redis: RedisClient | None = None
_storage_s3: S3Client | None = None
_queue: Queue | None = None
_ssrf_guard: SSRFGuard | None = None


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


async def get_s3() -> S3Client:
    """Dependency: return the S3Client singleton."""
    if _storage_s3 is None:
        raise RuntimeError("S3Client not initialized")
    return _storage_s3


async def get_queue() -> Queue:
    """Dependency: return the rq Queue producer singleton."""
    if _queue is None:
        raise RuntimeError("Queue not initialized")
    return _queue


async def get_ssrf_guard() -> SSRFGuard:
    """Dependency: return the SSRFGuard singleton (built with additional_denied_cidrs)."""
    if _ssrf_guard is None:
        raise RuntimeError("SSRFGuard not initialized")
    return _ssrf_guard
