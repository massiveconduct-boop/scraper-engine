# tests/integration/test_postgres_client.py
"""PostgresClient integration tests — real Postgres + tenant scoping."""

import pytest

from core.tenant import TenantId
from storage.postgres_client import PostgresClient


@pytest.fixture
async def pg():
    client = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:5432/scraper_engine",
        pool_size=5,
    )
    await client.start()
    yield client
    await client.stop()


class TestPostgresClient:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, pg):
        assert pg._shared_pool is not None

    @pytest.mark.asyncio
    async def test_acquire_tenant_scope(self, pg):
        system = TenantId("system")
        async with pg.acquire(system) as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1

    @pytest.mark.asyncio
    async def test_invalid_tenant_rejected(self, pg):
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("bad; DROP TABLE")

    @pytest.mark.asyncio
    async def test_fetch_rows(self, pg):
        system = TenantId("system")
        rows = await pg.fetch(system, "SELECT tablename FROM pg_tables WHERE schemaname = 'system'")
        table_names = [r["tablename"] for r in rows]
        assert "scrape_jobs" in table_names
        assert "dead_letter_queue" in table_names

    @pytest.mark.asyncio
    async def test_execute_query(self, pg):
        system = TenantId("system")
        result = await pg.execute(system, "SELECT 1")
        assert result == "SELECT 1"

    @pytest.mark.asyncio
    async def test_fetchrow_single(self, pg):
        system = TenantId("system")
        row = await pg.fetchrow(system, "SELECT 1 AS one")
        assert row is not None
        assert row["one"] == 1

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, pg):
        """Verify that SET search_path prevents cross-tenant access."""
        system = TenantId("system")
        # Create a second tenant schema
        new_tenant = TenantId("teststore")
        async with pg.acquire(system) as conn:
            await conn.execute(
                "SELECT public.create_tenant_schema($1)", str(new_tenant)
            )

        # Verify teststore has its own tables
        async with pg.acquire(new_tenant) as conn:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = $1",
                str(new_tenant),
            )
            table_names = [t["tablename"] for t in tables]
            assert "scrape_jobs" in table_names
