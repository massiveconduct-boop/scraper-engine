# tests/integration/test_redis_client.py
"""RedisClient integration tests — real Redis, tenant-prefixed key wrapper.

Covers both branches of every method: the pre-start() RuntimeError guard
(defense against using the client before connecting) and the real-Redis
success path (proves the tenant prefix actually reaches Redis correctly).
"""

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.storage.redis_client import RedisClient

_PREFIX = "rctest"


@pytest.fixture
async def redis():
    client = RedisClient(redis_url="redis://localhost:6379/0")
    await client.start()
    yield client
    cursor = 0
    while True:
        cursor, keys = await client.raw.scan(cursor, match=f"*{_PREFIX}*", count=1000)
        if keys:
            await client.raw.delete(*keys)
        if cursor == 0:
            break
    await client.stop()


@pytest.mark.integration
class TestRedisClientUnstarted:
    """Every accessor must refuse to touch a None connection."""

    async def test_raw_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before accessing raw"):
            _ = client.raw

    async def test_get_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before get\(\)"):
            await client.get(TenantId("system"), "k")

    async def test_set_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before set\(\)"):
            await client.set(TenantId("system"), "k", "v")

    async def test_incrby_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before incrby\(\)"):
            await client.incrby(TenantId("system"), "k")

    async def test_sadd_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before sadd\(\)"):
            await client.sadd(TenantId("system"), "k", "m")

    async def test_srem_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before srem\(\)"):
            await client.srem(TenantId("system"), "k", "m")

    async def test_scard_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before scard\(\)"):
            await client.scard(TenantId("system"), "k")

    async def test_expire_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before expire\(\)"):
            await client.expire(TenantId("system"), "k", 10)

    async def test_eval_before_start_raises(self):
        client = RedisClient()
        with pytest.raises(RuntimeError, match=r"start\(\) must be called before eval\(\)"):
            await client.eval("return 1", 0)


@pytest.mark.integration
class TestRedisClientConnected:
    async def test_raw_after_start(self, redis: RedisClient) -> None:
        assert redis.raw is not None

    async def test_stop_closes_connection(self) -> None:
        client = RedisClient(redis_url="redis://localhost:6379/0")
        await client.start()
        await client.stop()  # must not raise

    async def test_get_returns_none_for_missing_key(self, redis: RedisClient) -> None:
        tenant = TenantId("system")
        result = await redis.get(tenant, f"{_PREFIX}:missing")
        assert result is None

    async def test_set_then_get_without_ttl(self, redis: RedisClient) -> None:
        tenant = TenantId("system")
        await redis.set(tenant, f"{_PREFIX}:noexp", "hello")
        result = await redis.get(tenant, f"{_PREFIX}:noexp")
        assert result == "hello"
        assert await redis.raw.ttl(f"{tenant}:{_PREFIX}:noexp") == -1

    async def test_set_with_ttl(self, redis: RedisClient) -> None:
        tenant = TenantId("system")
        await redis.set(tenant, f"{_PREFIX}:withexp", 42, ttl=120)
        result = await redis.get(tenant, f"{_PREFIX}:withexp")
        assert result == "42"
        ttl = await redis.raw.ttl(f"{tenant}:{_PREFIX}:withexp")
        assert 0 < ttl <= 120

    async def test_incrby(self, redis: RedisClient) -> None:
        tenant = TenantId("system")
        first = await redis.incrby(tenant, f"{_PREFIX}:counter")
        second = await redis.incrby(tenant, f"{_PREFIX}:counter", amount=5)
        assert first == 1
        assert second == 6

    async def test_sadd_srem_scard(self, redis: RedisClient) -> None:
        tenant = TenantId("system")
        key = f"{_PREFIX}:set"
        added = await redis.sadd(tenant, key, "a", "b", "c")
        assert added == 3
        assert await redis.scard(tenant, key) == 3
        removed = await redis.srem(tenant, key, "a")
        assert removed == 1
        assert await redis.scard(tenant, key) == 2

    async def test_expire(self, redis: RedisClient) -> None:
        tenant = TenantId("system")
        key = f"{_PREFIX}:expkey"
        await redis.set(tenant, key, "v")
        result = await redis.expire(tenant, key, 60)
        assert result is True
        ttl = await redis.raw.ttl(f"{tenant}:{key}")
        assert 0 < ttl <= 60

    async def test_eval(self, redis: RedisClient) -> None:
        result = await redis.eval("return ARGV[1]", 0, "echoed")
        assert result == "echoed"
