# tests/unit/test_paid_gateway.py
"""proxy/paid_gateway.py — rounds 40 and 62. Pure env-var read plus pure
string building, no network I/O, so these are plain unit tests with
monkeypatched os.environ."""

import httpx
import pytest

from scraper_engine.core.models import ProxyProtocol
from scraper_engine.proxy import paid_gateway
from scraper_engine.proxy.paid_gateway import (
    build_gateway_proxy,
    build_gateway_username,
    gateway_accepts_credentials,
    new_session_id,
)

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


class TestBuildGatewayUsername:
    """Round 62 — DataImpulse encodes targeting in the USERNAME, with a very
    specific grammar: `login__key.value;key.value`. Double underscore, `;`
    between params, `.` inside them. The engine previously sent the bare
    login, and the operator's own hand-tests during the Jumia run used
    `;countries=ng` (wrong separator, wrong key, `=` instead of `.`) and got
    `407 NO_USER` back. These tests pin the exact documented shape:
    https://docs.dataimpulse.com/proxies/parameters/session-id
    """

    def test_no_params_returns_base_username_unchanged(self):
        assert build_gateway_username("user123") == "user123"

    def test_country_only(self):
        assert build_gateway_username("user123", country="ng") == "user123__cr.ng"

    def test_session_only(self):
        assert build_gateway_username("user123", session_id="45") == "user123__sessid.45"

    def test_country_and_session_are_semicolon_separated(self):
        assert (
            build_gateway_username("user123", country="ng", session_id="45")
            == "user123__cr.ng;sessid.45"
        )

    def test_country_is_normalised_to_lowercase_and_stripped(self):
        assert build_gateway_username("user123", country=" NG ") == "user123__cr.ng"

    def test_asn_pin(self):
        assert build_gateway_username("user123", asn=29465) == "user123__asn.29465"

    def test_country_asn_and_session_order(self):
        # Order is fixed (country, asn, session) so a username is reproducible
        # from a config — the gateway accepts any order, but tests and logs
        # comparing usernames should not depend on dict iteration luck.
        assert (
            build_gateway_username("user123", country="ng", asn=29465, session_id="45")
            == "user123__cr.ng;asn.29465;sessid.45"
        )

    def test_asn_zero_is_still_sent(self):
        # `if asn is not None`, not a truthiness check — AS0 is reserved and
        # nonsensical, but silently dropping a configured value is worse than
        # letting the gateway reject it.
        assert build_gateway_username("user123", asn=0) == "user123__asn.0"

    def test_empty_country_is_treated_as_absent(self):
        # base.yaml's default is the empty string, not None — an empty
        # `cr.` parameter would be a malformed login, not "no preference".
        assert build_gateway_username("user123", country="") == "user123"


class TestNewSessionId:
    def test_is_numeric(self):
        assert new_session_id().isdigit()

    def test_successive_calls_differ(self):
        # The entire point: a repeated label gets the SAME exit IP back from
        # DataImpulse for 30 minutes, which would silently defeat rotation.
        assert len({new_session_id() for _ in range(100)}) == 100


class TestGatewayProxyTargeting:
    def test_country_and_session_are_applied_to_the_username(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

        proxy = build_gateway_proxy(country="ng", session_id="45", asn=29465)

        assert proxy is not None
        assert proxy.username == "user123__cr.ng;asn.29465;sessid.45"
        assert proxy.password == "pass456"  # password is never rewritten

    def test_defaults_leave_the_username_untouched(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

        proxy = build_gateway_proxy()

        assert proxy is not None
        assert proxy.username == "user123"


class TestAsnRequiresCountry:
    """config/schema.py::DataImpulseConfig._asn_requires_country — round 62.

    Lives in this file rather than a config test module because the rule it
    enforces is a property of DataImpulse's username grammar, documented in
    proxy/paid_gateway.py, not of the config system.

    Verified live 2026-09-20: `login__asn.29465;sessid.N` fails proxy auth
    6 times out of 6; `login__cr.ng;asn.29465;sessid.N` succeeds 6 of 6.
    Caught after `DATAIMPULSE_ASN` was set without `DATAIMPULSE_COUNTRY` and
    every gateway request 407'd at fetch time — an opaque per-request proxy
    failure that pointed nowhere near the config. Failing at load time turns
    that into one startup error naming the fix.
    """

    def test_asn_with_country_is_accepted(self):
        from scraper_engine.config.schema import DataImpulseConfig

        assert DataImpulseConfig(asn=29465, country="ng").asn == 29465

    def test_asn_without_country_is_rejected(self):
        import pytest
        from pydantic import ValidationError

        from scraper_engine.config.schema import DataImpulseConfig

        with pytest.raises(ValidationError, match="dataimpulse.country is empty"):
            DataImpulseConfig(asn=29465)

    def test_asn_with_whitespace_only_country_is_rejected(self):
        """base.yaml renders country from an env placeholder, so a var set to
        spaces must not sneak past as "configured"."""
        import pytest
        from pydantic import ValidationError

        from scraper_engine.config.schema import DataImpulseConfig

        with pytest.raises(ValidationError, match="dataimpulse.country is empty"):
            DataImpulseConfig(asn=29465, country="   ")

    def test_country_without_asn_is_fine(self):
        from scraper_engine.config.schema import DataImpulseConfig

        cfg = DataImpulseConfig(country="ng")
        assert cfg.country == "ng"
        assert cfg.asn is None

    def test_default_config_has_neither(self):
        from scraper_engine.config.schema import DataImpulseConfig

        cfg = DataImpulseConfig()
        assert cfg.asn is None
        assert cfg.country == ""


class TestRefusedTtl:
    """Round 68 — how long one 407 takes the gateway out of use."""

    def test_default_is_ten_minutes(self):
        from scraper_engine.config.schema import DataImpulseConfig

        assert DataImpulseConfig().refused_ttl_seconds == 600

    @pytest.mark.parametrize("value", [0, 9, 86401])
    def test_out_of_range_is_rejected(self, value):
        from pydantic import ValidationError

        from scraper_engine.config.schema import DataImpulseConfig

        with pytest.raises(ValidationError):
            DataImpulseConfig(refused_ttl_seconds=value)

    def test_base_yaml_renders_the_default(self, monkeypatch):
        from scraper_engine.config.loader import load_config

        monkeypatch.delenv("DATAIMPULSE_REFUSED_TTL_SECONDS", raising=False)
        assert load_config().dataimpulse.refused_ttl_seconds == 600


class TestGatewayAcceptsCredentials:
    """Round 66 — the probe proxy/dlq_reaper.py runs before re-driving a URL
    the gateway refused. True only on a real 200 through the gateway."""

    @staticmethod
    def _client(monkeypatch, handler):
        seen = {}
        real = httpx.AsyncClient

        def _factory(*, proxy, timeout):
            seen["proxy"] = proxy
            return real(transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(paid_gateway.httpx, "AsyncClient", _factory)
        return seen

    @pytest.fixture
    def env(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), [(200, True), (407, False), (503, False)])
    async def test_only_a_200_counts(self, env, monkeypatch, status, expected):
        seen = self._client(monkeypatch, lambda request: httpx.Response(status))
        assert await gateway_accepts_credentials(country="ng", asn=29465) is expected
        # The credentials must travel with the probe: without them every
        # gateway answers 407 and the probe could never pass.
        assert seen["proxy"].startswith("http://user123__cr.ng;asn.29465;sessid.")
        assert seen["proxy"].endswith(":pass456@gw.dataimpulse.com:823")

    @pytest.mark.asyncio
    async def test_a_transport_error_is_not_acceptance(self, env, monkeypatch):
        def _boom(request):
            raise httpx.ConnectTimeout("timed out")

        self._client(monkeypatch, _boom)
        assert await gateway_accepts_credentials() is False

    @pytest.mark.asyncio
    async def test_missing_configuration_is_not_acceptance(self, monkeypatch):
        for key in _ALL_VARS:
            monkeypatch.delenv(key, raising=False)
        assert await gateway_accepts_credentials() is False
