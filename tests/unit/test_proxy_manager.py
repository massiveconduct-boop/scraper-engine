# tests/unit/test_proxy_manager.py
"""ProxyManager tests — scored selection, domain bans, exhaustion."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.core.exceptions import ProxyPoolExhaustedError
from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId
from scraper_engine.proxy.manager import ProxyManager


@pytest.fixture
def tenant():
    return TenantId("test")


@pytest.fixture
def sample_proxies():
    # Sorted by reliability_score DESC as the query would return
    return [
        Proxy(id=3, ip="3.3.3.3", port=8080, protocol=ProxyProtocol.HTTPS, reliability_score=95.0),
        Proxy(id=1, ip="1.1.1.1", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=90.0),
        Proxy(id=2, ip="2.2.2.2", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=80.0),
    ]


def make_manager(redis_get_return=None):
    redis = AsyncMock()
    redis.get.return_value = redis_get_return  # None = not banned
    redis.set.return_value = None
    pg = AsyncMock()
    pg.fetch.return_value = []
    pg.execute.return_value = "OK"
    return ProxyManager(redis=redis, pg=pg)


class TestProxyManager:
    @pytest.mark.asyncio
    async def test_exhausted_when_pool_empty(self, tenant):
        pm = make_manager()
        with pytest.raises(ProxyPoolExhaustedError) as exc:
            await pm.get_proxy(tenant, level=1, domain="example.com")
        assert exc.value.domain == "example.com"
        assert exc.value.level == 1

    @pytest.mark.asyncio
    async def test_selects_from_pool(self, tenant, sample_proxies):
        redis = AsyncMock()
        redis.get.return_value = None
        pg = AsyncMock()
        pg.fetch.return_value = [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol.value,
                "anonymity_level": p.anonymity_level.value,
                "asn_class": p.asn_class.value,
                "reliability_score": p.reliability_score,
            }
            for p in sample_proxies
        ]
        pm = ProxyManager(redis=redis, pg=pg)
        lease = await pm.get_proxy(tenant, level=1, domain="example.com")
        assert lease.proxy.ip == "3.3.3.3"  # highest score

    @pytest.mark.asyncio
    async def test_skips_banned_proxies(self, tenant, sample_proxies):
        redis = AsyncMock()
        # First two are banned, third is not
        redis.get.side_effect = ["1", "1", None]
        pg = AsyncMock()
        pg.fetch.return_value = [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol.value,
                "anonymity_level": p.anonymity_level.value,
                "asn_class": p.asn_class.value,
                "reliability_score": p.reliability_score,
            }
            for p in sample_proxies
        ]
        pm = ProxyManager(redis=redis, pg=pg)
        lease = await pm.get_proxy(tenant, level=2, domain="example.com")
        assert lease.proxy.ip == "2.2.2.2"  # 3.3.3.3 and 1.1.1.1 are banned

    @pytest.mark.asyncio
    async def test_exhausted_when_all_candidates_stay_banned(self, tenant):
        """Every candidate found across MAX_ATTEMPTS retries is domain-banned
        (never None) — the loop must fall through and raise after exhausting
        attempts, distinct from the pool-empty (proxy is None) exhaustion path."""
        proxies = [
            Proxy(
                id=i,
                ip=f"{i}.{i}.{i}.{i}",
                port=8080,
                protocol=ProxyProtocol.HTTP,
                reliability_score=90.0,
            )
            for i in range(1, ProxyManager.MAX_ATTEMPTS + 1)
        ]
        redis = AsyncMock()
        redis.get.return_value = "1"  # always banned
        pg = AsyncMock()
        pg.fetch.return_value = [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol.value,
                "anonymity_level": p.anonymity_level.value,
                "asn_class": p.asn_class.value,
                "reliability_score": p.reliability_score,
            }
            for p in proxies
        ]
        pm = ProxyManager(redis=redis, pg=pg)

        with pytest.raises(ProxyPoolExhaustedError) as exc:
            await pm.get_proxy(tenant, level=1, domain="example.com")
        assert exc.value.attempts == ProxyManager.MAX_ATTEMPTS
        redis.raw.incr.assert_awaited_once_with("metrics:proxy_exhausted_total:1")

    @pytest.mark.asyncio
    async def test_mark_success(self, tenant):
        pm = make_manager()
        await pm.mark_success(tenant, "1.2.3.4", 8080)
        # Should not raise

    @pytest.mark.asyncio
    async def test_mark_failure(self, tenant):
        pm = make_manager()
        await pm.mark_failure(tenant, "1.2.3.4", 8080, "example.com")
        # Should not raise
