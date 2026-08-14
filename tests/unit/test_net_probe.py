# tests/unit/test_net_probe.py
"""tcp_probe/http_probe/lease_preflight — shared fast liveness checks.
tcp_probe was extracted from ProxyHarvester._tcp_probe (round 37); http_probe
and lease_preflight were added the same round after a live test showed a
proxy that passed a TCP-only check, then dropped mid-navigation ("connects
but doesn't forward"). Only the network boundary (asyncio.open_connection /
httpx.AsyncClient) is faked."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scraper_engine.proxy.net_probe import http_probe, lease_preflight, tcp_probe


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class FakeHttpxClient:
    """Stands in for the httpx.AsyncClient http_probe constructs itself
    (`async with httpx.AsyncClient(...) as client`)."""

    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise_exc = raise_exc

    async def __aenter__(self):
        if self._raise_exc:
            raise self._raise_exc
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        return self._resp


class TestTcpProbe:
    @pytest.mark.asyncio
    async def test_returns_true_on_successful_connect(self):
        writer = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open_connection(ip, port):
            return MagicMock(), writer

        with patch(
            "scraper_engine.proxy.net_probe.asyncio.open_connection",
            side_effect=fake_open_connection,
        ):
            result = await tcp_probe("1.2.3.4", 8080)
        assert result is True
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        async def fake_open_connection(ip, port):
            raise ConnectionRefusedError("refused")

        with patch(
            "scraper_engine.proxy.net_probe.asyncio.open_connection",
            side_effect=fake_open_connection,
        ):
            result = await tcp_probe("1.2.3.4", 8080)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        async def hang(ip, port):
            import asyncio as _asyncio

            await _asyncio.sleep(10)

        with patch(
            "scraper_engine.proxy.net_probe.asyncio.open_connection",
            side_effect=hang,
        ):
            result = await tcp_probe("1.2.3.4", 8080, timeout=0.01)
        assert result is False


class TestHttpProbe:
    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        client = FakeHttpxClient(resp=FakeResponse(status_code=200))
        with patch(
            "scraper_engine.proxy.net_probe.httpx.AsyncClient", return_value=client
        ):
            result = await http_probe("1.2.3.4", 8080, "HTTP")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200(self):
        client = FakeHttpxClient(resp=FakeResponse(status_code=502))
        with patch(
            "scraper_engine.proxy.net_probe.httpx.AsyncClient", return_value=client
        ):
            result = await http_probe("1.2.3.4", 8080, "HTTP")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """The exact live-caught case this closes: a proxy that connects
        but then drops the connection mid-request."""
        client = FakeHttpxClient(raise_exc=ConnectionError("connection reset"))
        with patch(
            "scraper_engine.proxy.net_probe.httpx.AsyncClient", return_value=client
        ):
            result = await http_probe("1.2.3.4", 8080, "HTTP")
        assert result is False


class TestLeasePreflight:
    @pytest.mark.asyncio
    async def test_tcp_failure_short_circuits_before_http(self):
        """If the TCP layer fails, http_probe must never be attempted —
        no point spending an HTTP round trip on a proxy that can't even
        be connected to."""
        with (
            patch(
                "scraper_engine.proxy.net_probe.tcp_probe", AsyncMock(return_value=False)
            ) as fake_tcp,
            patch(
                "scraper_engine.proxy.net_probe.http_probe", AsyncMock(return_value=True)
            ) as fake_http,
        ):
            result = await lease_preflight("1.2.3.4", 8080, "HTTP")
        assert result is False
        fake_tcp.assert_awaited_once()
        fake_http.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tcp_passes_http_fails(self):
        with (
            patch("scraper_engine.proxy.net_probe.tcp_probe", AsyncMock(return_value=True)),
            patch("scraper_engine.proxy.net_probe.http_probe", AsyncMock(return_value=False)),
        ):
            result = await lease_preflight("1.2.3.4", 8080, "HTTP")
        assert result is False

    @pytest.mark.asyncio
    async def test_both_pass(self):
        with (
            patch("scraper_engine.proxy.net_probe.tcp_probe", AsyncMock(return_value=True)),
            patch("scraper_engine.proxy.net_probe.http_probe", AsyncMock(return_value=True)),
        ):
            result = await lease_preflight("1.2.3.4", 8080, "HTTP")
        assert result is True
