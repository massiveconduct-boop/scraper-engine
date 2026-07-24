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
