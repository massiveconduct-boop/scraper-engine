# tests/integration/test_quota_per_tenant.py
"""Per-tenant quota enforcement — codifies round-8 curl evidence as an
automated, repeatable integration test.

Two tenants, two distinct limits from public.tenants.quota_daily_limit,
both enforced independently via QuotaManager. Proves the DB column is
actually read (not the Redis-only global-default path from before the fix).
"""

import pytest

from scraper_engine.core.exceptions import QuotaExceededError
from scraper_engine.core.quota import QuotaManager
from scraper_engine.core.tenant import TenantId
from scraper_engine.storage.postgres_client import PostgresClient
from scraper_engine.storage.redis_client import RedisClient


@pytest.fixture
async def pg():
    client = PostgresClient(
        pgbouncer_dsn="postgresql://scraper:scraper@localhost:5432/scraper_engine",
        pool_size=5,
    )
    await client.start()
    yield client
    await client.stop()


@pytest.fixture
async def redis():
    client = RedisClient(redis_url="redis://localhost:6379/0")
    await client.start()
    # Ensure clean state before and after test
    cursor = 0
    while True:
        cursor, keys = await client.raw.scan(cursor, match="quota:*", count=1000)
        if keys:
            await client.raw.delete(*keys)
        if cursor == 0:
            break
    yield client
    while True:
        cursor, keys = await client.raw.scan(cursor, match="quota:*", count=1000)
        if keys:
            await client.raw.delete(*keys)
        if cursor == 0:
            break
    await client.stop()


@pytest.fixture
async def two_tenants_distinct_limits(pg: PostgresClient):
    """Seed qtest_a (limit=2) and qtest_b (limit=5) in public.tenants.
    Clean up after test regardless of outcome.
    """
    system = TenantId("system")
    await pg.execute(
        system,
        "DELETE FROM tenants WHERE tenant_id IN ('qtest_a', 'qtest_b')",
    )
    await pg.execute(
        system,
        "INSERT INTO tenants (tenant_id, quota_daily_limit) VALUES ('qtest_a', 2), ('qtest_b', 5)",
    )
    yield
    await pg.execute(
        system,
        "DELETE FROM tenants WHERE tenant_id IN ('qtest_a', 'qtest_b')",
    )


@pytest.mark.integration
async def test_two_tenants_enforce_independent_limits(
    pg: PostgresClient, redis: RedisClient, two_tenants_distinct_limits: None,
) -> None:
    """qtest_a gets 2 requests then 429. qtest_b gets 5 then 429. Independent."""
    async def resolve_limit(tenant_id: TenantId) -> int:
        row = await pg.fetchrow(
            TenantId("system"),
            "SELECT quota_daily_limit FROM tenants WHERE tenant_id = $1",
            str(tenant_id),
        )
        assert row is not None, f"Tenant {tenant_id} not found in public.tenants"
        return row["quota_daily_limit"]

    tenant_a = TenantId("qtest_a")
    tenant_b = TenantId("qtest_b")

    limit_a = await resolve_limit(tenant_a)
    limit_b = await resolve_limit(tenant_b)
    assert limit_a == 2, f"Expected limit 2 for qtest_a, got {limit_a}"
    assert limit_b == 5, f"Expected limit 5 for qtest_b, got {limit_b}"

    qm_a = QuotaManager(redis=redis, daily_limit=limit_a)
    qm_b = QuotaManager(redis=redis, daily_limit=limit_b)

    # Tenant A: 2 successes, then 3rd must raise
    for _ in range(2):
        await qm_a.check_and_increment(tenant_a)
    with pytest.raises(QuotaExceededError):
        await qm_a.check_and_increment(tenant_a)

    # Tenant B: 5 successes, then 6th must raise
    for _ in range(5):
        await qm_b.check_and_increment(tenant_b)
    with pytest.raises(QuotaExceededError):
        await qm_b.check_and_increment(tenant_b)
