# tests/unit/test_harvester.py
"""ProxyHarvester tests — direct scrape + proxybroker2 subprocess fallback."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.proxy.harvester import ProxyHarvester


@pytest.fixture
def pg():
    return AsyncMock()


@pytest.fixture
def classifier():
    class MockClassifier:
        async def classify(self, ip: str) -> str:
            return "residential"
    return MockClassifier()


@pytest.fixture
def redis():
    r = AsyncMock()
    r.raw = AsyncMock()
    return r


class TestProxyHarvester:
    def test_init(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        assert h._sources == ["test"]

    def test_harvester_initial_state(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["a", "b"], asn_classifier=classifier)
        assert len(h._sources) == 2

    @pytest.mark.asyncio
    async def test_harvest_once_direct_scrape_primary(self, pg, classifier):
        """Direct scrape returns proxies — harvest_once exits early."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=10)
        assert await h.harvest_once(limit=10) == 10

    @pytest.mark.asyncio
    async def test_harvest_once_falls_back_to_broker(self, pg, classifier):
        """Direct scrape returns 0 → broker fallback."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=0)
        h._harvest_via_broker = AsyncMock(return_value=5)
        assert await h.harvest_once(limit=10) == 5

    @pytest.mark.asyncio
    async def test_harvest_once_broker_exception(self, pg, classifier):
        """Broker raises → returns direct scrape count."""
        h = ProxyHarvester(pg=pg, sources=["err"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=0)
        h._harvest_via_broker = AsyncMock(side_effect=RuntimeError("boom"))
        assert await h.harvest_once(limit=10) == 0

    @pytest.mark.asyncio
    async def test_harvest_once_merges_both_paths(self, pg, classifier):
        """Requirement B: direct returns 3, broker adds 5, total 8."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=3)
        h._harvest_via_broker = AsyncMock(return_value=5)
        assert await h.harvest_once(limit=10) == 8

    @pytest.mark.asyncio
    async def test_direct_scrape_works(self, pg, classifier):
        """Direct scrape returns proxies."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=4)
        assert await h._direct_scrape(limit=10, tenant=pg) == 4

    @pytest.mark.asyncio
    async def test_direct_scrape_https_source(self, pg, classifier):
        """Direct scrape includes HTTPS variant."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=8)
        assert await h._direct_scrape(limit=20, tenant=pg) == 8


class TestProxySourceHealthWiring:
    """Round 25: record_source_health writes to Redis (not an in-process
    Gauge) because ProxyHarvester runs in a different process than the one
    serving /metrics — see proxy/source_health.py's module docstring."""

    @pytest.mark.asyncio
    async def test_record_source_health_writes_healthy(self, redis):
        from scraper_engine.proxy.source_health import record_source_health

        await record_source_health(redis, "geonode", 5)
        redis.raw.set.assert_awaited_once_with("metrics:proxy_source_healthy:geonode", "1")

    @pytest.mark.asyncio
    async def test_record_source_health_writes_unhealthy_on_zero(self, redis):
        from scraper_engine.proxy.source_health import record_source_health

        await record_source_health(redis, "geonode", 0)
        redis.raw.set.assert_awaited_once_with("metrics:proxy_source_healthy:geonode", "0")

    @pytest.mark.asyncio
    async def test_direct_scrape_records_health_when_redis_given(self, pg, classifier, redis):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier, redis=redis)
        h._scrape_one = AsyncMock(return_value=3)

        await h._direct_scrape(limit=1000, tenant=pg)

        assert redis.raw.set.await_count == len(ProxyHarvester.SOURCES)
        first_source_name = ProxyHarvester.SOURCES[0][0]
        redis.raw.set.assert_any_await(
            f"metrics:proxy_source_healthy:{first_source_name}", "1"
        )

    @pytest.mark.asyncio
    async def test_direct_scrape_skips_health_recording_without_redis(self, pg, classifier):
        """redis=None (default) must not raise — matches every other existing
        ProxyHarvester test that constructs it without a redis client."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._scrape_one = AsyncMock(return_value=3)

        # No exception is the assertion — there's no redis client to record to.
        await h._direct_scrape(limit=1000, tenant=pg)

    @pytest.mark.asyncio
    async def test_refresh_reads_back_what_record_wrote(self, redis):
        """observability.metrics.refresh_proxy_source_health is the scrape-time
        half — reads back exactly the keys record_source_health writes."""
        from scraper_engine.observability.metrics import refresh_proxy_source_health
        from scraper_engine.proxy.source_health import proxy_source_healthy, record_source_health

        healthy_source = ProxyHarvester.SOURCES[0][0]
        dark_source = ProxyHarvester.SOURCES[1][0]

        written: dict[str, str] = {}

        async def fake_set(key, value):
            written[key] = value

        redis.raw.set = AsyncMock(side_effect=fake_set)
        await record_source_health(redis, healthy_source, 5)
        await record_source_health(redis, dark_source, 0)

        async def fake_get(key):
            return written.get(key)

        redis.raw.get = AsyncMock(side_effect=fake_get)
        await refresh_proxy_source_health(redis)

        assert proxy_source_healthy.labels(source_name=healthy_source)._value.get() == 1.0
        assert proxy_source_healthy.labels(source_name=dark_source)._value.get() == 0.0


class TestPromoteTcpOnly:
    """promote_tcp_only() — bounded background re-validation of score<40 proxies."""

    @pytest.mark.asyncio
    async def test_promotes_validating_proxy(self, pg, classifier):
        """Proxy that passes HTTP validation is promoted from 25→60."""
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.core.tenant import TenantId
        pg.fetch.return_value = [
            {"ip": "1.2.3.4", "port": 3128, "protocol": "HTTP"},
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(
            return_value=(True, AnonymityLevel.ELITE),
        )
        promoted = await h.promote_tcp_only(limit=5, tenant=TenantId("system"))
        assert promoted == 1
        pg.execute.assert_called_once()
        call_args = pg.execute.call_args
        assert "reliability_score" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_skips_non_validating_proxy(self, pg, classifier):
        """Proxy that fails HTTP validation is not promoted, but attempt IS tracked."""
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.core.tenant import TenantId
        pg.fetch.return_value = [
            {"ip": "5.6.7.8", "port": 8080, "protocol": "HTTP"},
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT))
        promoted = await h.promote_tcp_only(limit=5, tenant=TenantId("system"))
        assert promoted == 0
        pg.execute.assert_called_once()
        call_args = pg.execute.call_args
        assert "promotion_attempts" in call_args[0][1]  # tracking even on failure

    @pytest.mark.asyncio
    async def test_empty_pool_returns_zero(self, pg, classifier):
        """Empty pool (no score<40 rows) returns 0 promoted."""
        from scraper_engine.core.tenant import TenantId
        pg.fetch.return_value = []
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        promoted = await h.promote_tcp_only(limit=50, tenant=TenantId("system"))
        assert promoted == 0
        pg.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_limit_caps_rows_processed(self, pg, classifier):
        """limit= parameter caps how many rows are fetched for re-validation."""
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.core.tenant import TenantId
        pg.fetch.return_value = [
            {"ip": f"10.0.0.{i}", "port": 3128, "protocol": "HTTP"} for i in range(3)
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        promoted = await h.promote_tcp_only(limit=3, tenant=TenantId("system"))
        assert promoted == 3
        fetch_args = pg.fetch.call_args
        assert fetch_args[0][2] == 3  # limit value in query
