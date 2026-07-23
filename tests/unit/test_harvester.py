# tests/unit/test_harvester.py
"""ProxyHarvester tests — proxybroker2 queue drain + direct scrape fallback."""

import asyncio
from unittest.mock import AsyncMock, patch

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
    async def test_harvest_once_no_broker(self, pg, classifier):
        """proxybroker2 ImportError → falls back to direct scrape."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=5)
        with patch("proxy.harvester.logger"), \
             patch("proxybroker2.Broker", side_effect=ImportError, create=True):
            assert await h.harvest_once(limit=10) == 5

    @pytest.mark.asyncio
    async def test_harvest_once_no_proxybroker2(self, pg, classifier):
        """proxybroker2 not installed → direct scrape."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=3)
        with patch("proxybroker2.Broker", side_effect=ImportError, create=True):
            assert await h.harvest_once(limit=10) == 3

    @pytest.mark.asyncio
    async def test_harvest_via_broker_exception(self, pg, classifier):
        """proxybroker2 raises → direct scrape fallback."""
        h = ProxyHarvester(pg=pg, sources=["err"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=0)
        broker = AsyncMock()
        broker.find = AsyncMock(side_effect=ConnectionError("refused"))

        with patch("proxy.harvester.logger"), \
             patch("proxybroker2.Broker", return_value=broker, create=True):
            assert await h.harvest_once(limit=10) == 0

    @pytest.mark.asyncio
    async def test_harvest_once_broker_returns_zero(self, pg, classifier):
        """Broker returns 0 → falls back to direct scrape."""
        h = ProxyHarvester(pg=pg, sources=["dead"], asn_classifier=classifier)
        h._harvest_via_broker = AsyncMock(return_value=0)
        h._direct_scrape = AsyncMock(return_value=7)
        assert await h.harvest_once(limit=10) == 7

    @pytest.mark.asyncio
    async def test_direct_scrape_works(self, pg, classifier):
        """Direct scrape returns proxies."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=4)
        assert await h._direct_scrape(limit=10, tenant=pg) == 4
