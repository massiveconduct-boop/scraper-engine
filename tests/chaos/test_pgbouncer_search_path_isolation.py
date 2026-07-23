"""
Closes G-05 from the production-readiness gap audit.

PgBouncer transaction-pooling + SET search_path concurrency test.
Fires 50 concurrent acquire() calls interleaving 5 different TenantIds
and asserts every query returns rows from only its own tenant schema.

This is the test that either confirms or reopens F-11 under the specific
pooling mode chosen in BD-06.
"""

import asyncio
import random

import pytest

from core.tenant import TenantId
from storage.postgres_client import PostgresClient


@pytest.fixture
async def pg_client():
    # G-05: Routes through PgBouncer transaction-pooling (port 6432).
    # PgBouncer config: POOL_MODE=transaction, MAX_CLIENT_CONN=500,
    # DEFAULT_POOL_SIZE=20 per BD-06. auth_type=scram-sha-256 with
    # SCRAM verifier extracted from Postgres pg_authid.rolpassword.
    client = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:6432/scraper_engine",
        pool_size=20,
    )
    await client.start()
    # Create test tenant schemas
    system = TenantId("system")
    for i in range(5):
        async with client.acquire(system) as conn:
            await conn.execute(
                "SELECT public.create_tenant_schema($1)",
                f"g05tenant_{i}",
            )
    yield client
    await client.stop()


class TestPgBouncerIsolation:
    """G-05: search_path must hold under concurrent tenants."""

    @pytest.mark.asyncio
    async def test_search_path_holds_under_50_concurrent(self, pg_client):
        """50 concurrent acquire() calls across 5 tenants — no cross-tenant leaks."""
        tenants = [TenantId(f"g05tenant_{i}") for i in range(5)]

        async def probe(tenant):
            async with pg_client.acquire(tenant) as conn:
                row = await conn.fetchrow("SELECT current_schema()")
                current = row["current_schema"]
                assert current == str(tenant), (
                    f"ISOLATION LEAK: expected schema '{tenant}', got '{current}'"
                )
                return True

        results = await asyncio.gather(*[
            probe(random.choice(tenants)) for _ in range(50)
        ])
        assert all(results), "All 50 probes must pass tenant isolation"
        assert len(results) == 50
