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


def _mock_broker_factory(**kwargs):
    """Helper: create a mock broker with configurable find() behavior."""
    broker = AsyncMock()
    broker.find = AsyncMock(**kwargs)
    return broker


class TestProxyHarvester:
    def test_init(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        assert h._sources == ["test"]

    @pytest.mark.asyncio
    async def test_harvest_once_no_broker(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        with patch("proxy.harvester.logger"):
            count = await h.harvest_once(limit=10)
            assert count == 0

    @pytest.mark.asyncio
    async def test_harvest_once_no_proxybroker2(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        count = await h.harvest_once(limit=10)
        assert count == 0

    @pytest.mark.asyncio
    async def test_harvester_initial_state(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["a", "b"], asn_classifier=classifier)
        assert len(h._sources) == 2

    @pytest.mark.asyncio
    async def test_harvest_once_with_data(self, pg, classifier):
        """G-04: broker returns 2 proxies — both inserted."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        async def proxy_stream():
            yield {"ip": "5.6.7.8", "port": 3128, "protocol": "HTTP", "anonymity": "elite"}
            yield {"ip": "9.10.11.12", "port": 8080, "protocol": "HTTPS", "anonymity": "anonymous"}
        broker = _mock_broker_factory(return_value=proxy_stream())
        with (
            patch("proxy.harvester.logger"),
            patch("proxybroker2.Broker", return_value=broker, create=True),
        ):
            assert await h.harvest_once(limit=10) == 2

    @pytest.mark.asyncio
    async def test_harvest_once_broker_returns_none(self, pg, classifier):
        """G-04: broker.find() returns None (sources unreachable)."""
        h = ProxyHarvester(pg=pg, sources=["dead"], asn_classifier=classifier)
        broker = _mock_broker_factory(return_value=None)
        with (
            patch("proxy.harvester.logger"),
            patch("proxybroker2.Broker", return_value=broker, create=True),
        ):
            assert await h.harvest_once(limit=10) == 0

    @pytest.mark.asyncio
    async def test_harvest_once_exception_handled(self, pg, classifier):
        """G-04: broker.find() raises — returns 0, no crash."""
        h = ProxyHarvester(pg=pg, sources=["err"], asn_classifier=classifier)
        broker = _mock_broker_factory(side_effect=ConnectionError("refused"))
        with (
            patch("proxy.harvester.logger"),
            patch("proxybroker2.Broker", return_value=broker, create=True),
        ):
            assert await h.harvest_once(limit=10) == 0

    @pytest.mark.asyncio
    async def test_harvest_once_empty_stream(self, pg, classifier):
        """G-04: broker returns empty async generator."""
        h = ProxyHarvester(pg=pg, sources=["empty"], asn_classifier=classifier)
        broker = _mock_broker_factory(return_value=None)  # None iterable tests None handling
        with (
            patch("proxy.harvester.logger"),
            patch("proxybroker2.Broker", return_value=broker, create=True),
        ):
            assert await h.harvest_once(limit=10) == 0


