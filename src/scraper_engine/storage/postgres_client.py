# storage/postgres_client.py
"""PgBouncer-fronted PostgreSQL client with tenant-scoped access.

Design: ONE shared asyncpg pool → PgBouncer (transaction mode) → PostgreSQL.
Per-tenant isolation via SET search_path per connection checkout, NOT per-tenant
connection pools (avoids N tenants × M workers connection multiplication, closes F-23).

BD-06: max_client_conn=500, default_pool_size=20.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId

# Strict allow-list for SQL identifiers — design invariant §1.1.7
_VALID_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


class PostgresClient:
    """PgBouncer-fronted asyncpg pool with tenant-scoped connection checkout."""

    def __init__(self, pgbouncer_dsn: str, pool_size: int = 20) -> None:
        self._dsn = pgbouncer_dsn
        self.pool_size = pool_size
        self._shared_pool: asyncpg.Pool | None = None  # ONE pool, not per-tenant

    async def start(self) -> None:
        """Create the shared connection pool to PgBouncer."""
        self._shared_pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=self.pool_size,
            # PgBouncer transaction-pooling reassigns the backend per transaction,
            # so asyncpg's prepared-statement cache (keyed to a specific backend)
            # is unsafe through the pooler. Disabling it makes this client correct
            # for the shared PgBouncer pool (invariant G-05). Safe: no code path
            # calls conn.prepare(), so nothing depends on statement caching.
            statement_cache_size=0,
        )

    async def stop(self) -> None:
        """Close the shared pool gracefully."""
        if self._shared_pool:
            await self._shared_pool.close()

    @asynccontextmanager
    async def acquire(self, tenant_id: TenantId) -> AsyncIterator[asyncpg.Connection]:
        """Check out a connection, issue SET search_path = {validated_tenant_id}, yield.

        Transaction-scoped, PgBouncer transaction-pooling mode compatible.
        The tenant_id has already been validated by TenantId.__new__ before
        reaching this point (defense in depth — design invariant §1.1.3).
        """
        if self._shared_pool is None:
            raise RuntimeError("PostgresClient.start() must be called before acquire()")

        # Defense in depth: re-validate before constructing DDL/DSN (invariant §1.1.7)
        tenant_str = str(tenant_id)
        if not _VALID_IDENTIFIER_RE.match(tenant_str):
            raise ValueError(f"Invalid tenant_id rejected at storage boundary: {tenant_str}")

        async with self._shared_pool.acquire() as conn:
            # PgBouncer transaction-pooling: each autocommit statement may
            # land on a different backend. BEGIN...COMMIT guarantees SET
            # search_path and all queries within yield hit the same backend.
            await conn.execute("BEGIN")
            await conn.execute(f"SET search_path = {tenant_str}, public")
            try:
                yield conn
            finally:
                await conn.execute("SET search_path = public")
                await conn.execute("COMMIT")

    async def execute(self, tenant_id: TenantId, query: str, *args: Any) -> str:
        """Execute a query within a tenant scope. Returns status string."""
        async with self.acquire(tenant_id) as conn:
            result: str = await conn.execute(query, *args)
            return result

    async def fetch(self, tenant_id: TenantId, query: str, *args: Any) -> list[asyncpg.Record]:
        """Fetch rows within a tenant scope."""
        async with self.acquire(tenant_id) as conn:
            rows: list[asyncpg.Record] = await conn.fetch(query, *args)
            return rows

    async def fetchrow(self, tenant_id: TenantId, query: str, *args: Any) -> asyncpg.Record | None:
        """Fetch a single row within a tenant scope."""
        async with self.acquire(tenant_id) as conn:
            return await conn.fetchrow(query, *args)
