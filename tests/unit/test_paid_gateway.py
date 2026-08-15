# tests/unit/test_paid_gateway.py
"""proxy/paid_gateway.py::build_gateway_proxy — round 40. Pure env-var read,
no network I/O, so these are plain unit tests with monkeypatched os.environ."""

from scraper_engine.core.models import ProxyProtocol
from scraper_engine.proxy.paid_gateway import build_gateway_proxy

_ALL_VARS = {
    "DATAIMPULSE_PROXY_HOST": "gw.dataimpulse.com",
    "DATAIMPULSE_PORT": "823",
    "DATAIMPULSE_USERNAME": "user123",
    "DATAIMPULSE_PASSWORD": "pass456",
}


class TestBuildGatewayProxy:
    def test_returns_configured_proxy_when_all_vars_set(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

        proxy = build_gateway_proxy()

        assert proxy is not None
        assert proxy.ip == "gw.dataimpulse.com"
        assert proxy.port == 823
        assert proxy.protocol == ProxyProtocol.HTTP
        assert proxy.username == "user123"
        assert proxy.password == "pass456"
        assert proxy.source == "paid_gateway"

    def test_returns_none_when_host_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_PROXY_HOST", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_port_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_PORT", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_username_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_USERNAME", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_password_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_port_not_an_integer(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DATAIMPULSE_PORT", "not-a-port")

        assert build_gateway_proxy() is None

    def test_returns_none_when_nothing_set(self, monkeypatch):
        for key in _ALL_VARS:
            monkeypatch.delenv(key, raising=False)

        assert build_gateway_proxy() is None
