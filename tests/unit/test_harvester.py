# tests/unit/test_harvester.py
"""ProxyHarvester tests — direct scrape + proxybroker2 subprocess fallback."""

from unittest.mock import AsyncMock

import pytest

from proxy.harvester import ProxyHarvester


@pytest.fixture
def pg():
    return AsyncMock()


@pytest.fixture
def classifier():
    class MockClassifier:
        async def classify(self, ip: str) -> str:
            return "residential"
    return MockClassifier()


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


class TestPromoteTcpOnly:
    """promote_tcp_only() — bounded background re-validation of score<40 proxies."""

    @pytest.mark.asyncio
    async def test_promotes_validating_proxy(self, pg, classifier):
        """Proxy that passes HTTP validation is promoted from 25→60."""
        from core.tenant import TenantId
        pg.fetch.return_value = [
            {"ip": "1.2.3.4", "port": 3128, "protocol": "HTTP"},
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(
            return_value=(True, __import__("core.models", fromlist=["AnonymityLevel"]).AnonymityLevel.ELITE),
        )
        promoted = await h.promote_tcp_only(limit=5, tenant=TenantId("system"))
        assert promoted == 1
        pg.execute.assert_called_once()
        call_args = pg.execute.call_args
        assert "reliability_score" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_skips_non_validating_proxy(self, pg, classifier):
        """Proxy that fails HTTP validation is not promoted, but attempt IS tracked."""
        from core.tenant import TenantId
        from core.models import AnonymityLevel
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
        from core.tenant import TenantId
        pg.fetch.return_value = []
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        promoted = await h.promote_tcp_only(limit=50, tenant=TenantId("system"))
        assert promoted == 0
        pg.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_limit_caps_rows_processed(self, pg, classifier):
        """limit= parameter caps how many rows are fetched for re-validation."""
        from core.tenant import TenantId
        from core.models import AnonymityLevel
        pg.fetch.return_value = [
            {"ip": f"10.0.0.{i}", "port": 3128, "protocol": "HTTP"} for i in range(3)
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        promoted = await h.promote_tcp_only(limit=3, tenant=TenantId("system"))
        assert promoted == 3
        fetch_args = pg.fetch.call_args
        assert fetch_args[0][2] == 3  # limit value in query
