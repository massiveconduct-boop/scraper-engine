# tests/unit/test_harvester.py
"""ProxyHarvester tests — proxy discovery with mocked proxybroker2."""

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

    @pytest.mark.asyncio
    async def test_harvest_once_no_broker(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        with patch("proxy.harvester.logger"):
            count = await h.harvest_once(limit=10)
            assert count == 0  # broker not installed

    @pytest.mark.asyncio
    async def test_harvest_once_no_proxybroker2(self, pg, classifier):
        """When proxybroker2 is not installed, harvest_once returns 0."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        count = await h.harvest_once(limit=10)
        assert count == 0  # broker not installed, returns 0 gracefully

    @pytest.mark.asyncio
    async def test_harvester_initial_state(self, pg, classifier):
        """Verify harvester initializes correctly."""
        h = ProxyHarvester(pg=pg, sources=["a", "b"], asn_classifier=classifier)
        assert len(h._sources) == 2
